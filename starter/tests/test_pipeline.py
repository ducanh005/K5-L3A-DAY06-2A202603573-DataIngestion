"""Unit and integration tests for CP3 ingestion/chunking and CP4 embeddings/pgvector store."""
import math
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from dotenv import load_dotenv
import pymupdf

from src.chunking import by_paragraph, fixed_size
from src.embeddings import MyEmbedder
from src.evaluation import evaluate_query, summarize
from src.ingestion import load_pdf, normalize_text
from src.models import Chunk, DocumentPage
from src.store import PgVectorStore


class _FakeModelsAPI:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def embed_content(self, *, model: str, contents, config) -> SimpleNamespace:
        items = [contents] if isinstance(contents, str) else list(contents)
        self.calls.append({"model": model, "contents": items, "config": config})
        dim = int(config.output_dimensionality)
        embeddings = []
        for idx, text in enumerate(items):
            vec = [0.0] * dim
            vec[0] = 3.0
            vec[1] = 4.0 + idx + (len(text) % 3)
            embeddings.append(SimpleNamespace(values=vec, statistics=SimpleNamespace(token_count=len(text.split()))))
        return SimpleNamespace(embeddings=embeddings)


class _FakeGenAIClient:
    def __init__(self) -> None:
        self.models = _FakeModelsAPI()


class Cp3PipelineTests(unittest.TestCase):
    def test_normalize_text_unicode_hyphens_and_chrome(self) -> None:
        raw = (
            "NIST AI 100-1\nAI RMF 1.0\n\n"
            "14\n\n"
            "GAI models may conﬁdently produce orga-\nnizational pre-\ntrained GOV-\nERN\n"
            "and energy- \nand water footprints.\n\n"
            "Page 27"
        )
        cleaned = normalize_text(raw, join_soft_hyphens=True)
        self.assertNotIn("NIST AI 100-1", cleaned)
        self.assertNotIn("AI RMF 1.0", cleaned)
        self.assertNotIn("Page 27", cleaned)
        self.assertNotRegex(cleaned, r"(?m)^14$")
        self.assertIn("confidently", cleaned)
        self.assertIn("organizational", cleaned)
        self.assertIn("pre-trained", cleaned)
        self.assertIn("GOV-ERN", cleaned)
        self.assertIn("energy- and water", cleaned)

        # Preserve explicit hyphens when soft-hyphen joining is disabled (e.g. NIST AI 600-1).
        word_raw = "Evaluate real-\nworld and third-\nparty risks."
        word_cleaned = normalize_text(word_raw, join_soft_hyphens=False)
        self.assertIn("real-world", word_cleaned)
        self.assertIn("third-party", word_cleaned)

    def test_load_pdf_logs_excluded_and_empty_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = Path(tmp_dir) / "sample.pdf"
            doc = pymupdf.open()
            p1 = doc.new_page()
            p1.insert_text((72, 72), "Cover page excluded by manifest")
            doc.new_page()  # p2: blank/unreadable page
            p3 = doc.new_page()
            p3.insert_text((72, 72), "Valid substantive content for retrieval testing.")
            doc.save(pdf_path)
            doc.close()

            logs: list[dict] = []
            pages = load_pdf(
                pdf_path,
                document_id="sample-doc",
                source="data/sample.pdf",
                include_pages={2, 3},
                exclude_reasons={1: "cover_page"},
                base_metadata={"year": 2024, "document_type": "core_framework"},
                page_sections={3: "govern"},
                page_logs=logs,
            )

        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].page, 3)
        self.assertEqual(pages[0].metadata["section"], "govern")
        self.assertEqual([row["status"] for row in logs], ["excluded", "empty_or_unreadable", "included"])
        self.assertEqual(logs[0]["reason"], "cover_page")
        self.assertEqual(logs[1]["reason"], "no_extractable_text_ocr_needed")

    def test_fixed_size_overlap_and_offsets(self) -> None:
        text = ("Alpha beta gamma delta epsilon. " * 25).strip()
        page = DocumentPage(
            document_id="doc1",
            source="data/doc1.pdf",
            page=6,
            text=text,
            metadata={"year": 2023, "section": "executive_summary"},
        )
        chunks = fixed_size(page, size=200, overlap=50)
        self.assertGreater(len(chunks), 2)
        self.assertEqual(chunks[0].chunk_id, "A:doc1:p006:c01")
        # Overlap window from c01 [150:200] appears in c02, and offsets match exact slices.
        overlap_slice = text[150:chunks[0].metadata["char_end"]].strip()
        self.assertTrue(chunks[1].text.startswith(overlap_slice))
        for chunk in chunks:
            self.assertEqual(text[chunk.metadata["char_start"]:chunk.metadata["char_end"]], chunk.text)
            self.assertLessEqual(len(chunk.text), 200)
            self.assertEqual(chunk.document_id, "doc1")
            self.assertEqual(chunk.metadata["year"], 2023)
            self.assertEqual(chunk.metadata["section"], "executive_summary")

    def test_by_paragraph_preserves_headings_and_sections(self) -> None:
        long_sentence = " ".join(["sentence"] * 45) + ". " + " ".join(["tail"] * 45) + "."
        text = (
            "GOVERN 1.2: Governance Heading\n\n"
            "Action ID Suggested Action GAI Risks\n\n"
            "GV-1.2-001\n\n"
            "Establish transparency policies.\n\n"
            "AI Actor Tasks: Governance and Oversight\n\n"
            f"MAP 1.1: Context Is Established\n\n{long_sentence}"
        )
        page = DocumentPage(
            document_id="doc600",
            source="data/doc600.pdf",
            page=26,
            text=text,
            metadata={"year": 2024, "document_type": "generative_ai_profile", "section": "govern"},
        )
        chunks = by_paragraph(page, max_size=400)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertIn("GOVERN 1.2: Governance Heading", chunks[0].text)
        self.assertIn("Establish transparency policies.", chunks[0].text)
        self.assertIn("AI Actor Tasks: Governance and Oversight", chunks[0].text)
        self.assertEqual(chunks[0].metadata["section"], "govern")
        # Section changes cleanly mid-page when MAP 1.1 starts.
        self.assertEqual(chunks[1].metadata["section"], "map")
        for chunk in chunks:
            self.assertLessEqual(len(chunk.text), 400)


class Cp4EmbeddingsAndStoreTests(unittest.TestCase):
    def test_my_embedder_contract_normalization_and_no_mock_fallback(self) -> None:
        fake_client = _FakeGenAIClient()
        embedder = MyEmbedder("gemini-embedding-001", dimensions=768, batch_size=2, client=fake_client)

        doc_vecs = embedder.embed_documents(["first doc", "second doc", "third doc"])
        self.assertEqual(len(doc_vecs), 3)
        self.assertEqual(len(fake_client.models.calls), 2)
        self.assertEqual(fake_client.models.calls[0]["config"].task_type, "RETRIEVAL_DOCUMENT")
        self.assertEqual(fake_client.models.calls[0]["config"].output_dimensionality, 768)

        query_vec = embedder.embed_query("what is risk?")
        self.assertEqual(fake_client.models.calls[-1]["config"].task_type, "RETRIEVAL_QUERY")
        self.assertEqual(len(query_vec), 768)
        for vec in [*doc_vecs, query_vec]:
            norm = math.sqrt(sum(v * v for v in vec))
            self.assertAlmostEqual(norm, 1.0, places=6)

        stats = embedder.get_stats()
        self.assertEqual(stats["api_calls"], 3)
        self.assertEqual(stats["document_texts"], 3)
        self.assertEqual(stats["query_texts"], 1)
        self.assertEqual(stats["reported_tokens"], 9)

        # Missing API key must raise instead of falling back to mock vectors.
        prev = os.environ.pop("GEMINI_API_KEY", None)
        try:
            with self.assertRaises(RuntimeError):
                MyEmbedder("gemini-embedding-001", api_key="")
        finally:
            if prev is not None:
                os.environ["GEMINI_API_KEY"] = prev

        with self.assertRaises(ValueError):
            MyEmbedder("unknown-model", client=fake_client)
        with self.assertRaises(ValueError):
            MyEmbedder("gemini-embedding-001", dimensions=64, client=fake_client)

    def test_pgvector_store_validation(self) -> None:
        store = PgVectorStore("postgresql://day6:day6_local_only@127.0.0.1:55433/day6_lab", dimensions=4)
        chunk = Chunk("A:doc1:p001:c01", "doc1", "data/doc1.pdf", 1, "hello", {"year": 2023})

        with self.assertRaises(ValueError):
            store.upsert("Z", [chunk], [[1.0, 0.0, 0.0, 0.0]])
        with self.assertRaises(ValueError):
            store.upsert("A", [chunk], [[1.0, 0.0]])
        with self.assertRaises(ValueError):
            store.upsert("A", [chunk, chunk], [[1.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]])
        with self.assertRaises(ValueError):
            store.search("C", [1.0, 0.0, 0.0, 0.0], filters={"unknown_col": "x"})

    def test_pgvector_store_live_idempotent_upsert_and_filters(self) -> None:
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
        db_url = os.getenv("DATABASE_URL", "postgresql://day6:day6_local_only@127.0.0.1:55433/day6_lab")
        store = PgVectorStore(db_url, dimensions=4, table_prefix="day6_unit_test")
        store.drop_tables()
        try:
            store.create_schema()
            chunks = [
                Chunk(
                    "B:doc1:p006:c01",
                    "doc1",
                    "data/doc1.pdf",
                    6,
                    "AI RMF risk measurement govern rule",
                    {"year": 2023, "document_type": "core_framework", "section": "govern"},
                ),
                Chunk(
                    "B:doc1:p010:c01",
                    "doc1",
                    "data/doc1.pdf",
                    10,
                    "Map context overview",
                    {"year": 2023, "document_type": "core_framework", "section": "map"},
                ),
                Chunk(
                    "B:doc2:p005:c01",
                    "doc2",
                    "data/doc2.pdf",
                    5,
                    "Generative AI governance actions",
                    {"year": 2024, "document_type": "generative_ai_profile", "section": "govern"},
                ),
            ]
            vectors = [
                [0.9, 0.1, 0.0, 0.0],
                [0.6, 0.4, 0.0, 0.0],
                [1.0, 0.0, 0.0, 0.0],
            ]

            store.upsert("B", chunks, vectors)
            self.assertEqual(store.count("B"), 3)

            # Re-upserting identical batch keeps exact row count (no duplicates).
            store.upsert("B", chunks, vectors)
            self.assertEqual(store.count("B"), 3)
            self.assertEqual(store.last_upsert_stats["deleted_obsolete"], 0)

            # Filtered SQL search picks year=2023 + section=govern, excluding 2024 doc2 even with higher similarity.
            hits = store.search("B", [1.0, 0.0, 0.0, 0.0], top_k=3, filters={"year": 2023, "section": "govern"})
            self.assertEqual([h["chunk_id"] for h in hits], ["B:doc1:p006:c01"])
            self.assertGreater(hits[0]["score"], 0.9)

            # Re-ingesting modified doc1 removes obsolete B:doc1:p010:c01 while keeping doc2 intact.
            updated_doc1 = [
                Chunk(
                    "B:doc1:p006:c01_v2",
                    "doc1",
                    "data/doc1.pdf",
                    6,
                    "Updated govern content",
                    {"year": 2023, "document_type": "core_framework", "section": "govern"},
                )
            ]
            store.upsert("B", updated_doc1, [[0.8, 0.2, 0.0, 0.0]])
            self.assertEqual(store.last_upsert_stats["deleted_obsolete"], 2)
            self.assertEqual(store.count("B"), 2)
        finally:
            store.drop_tables()


class Cp5EvaluationTests(unittest.TestCase):
    def test_evaluate_query_exact_alternative_split_and_filter_compliance(self) -> None:
        q = {
            "id": "q_sample",
            "type": "metadata_filter",
            "split": "dev",
            "question": "Sample question?",
            "expected_answer": "Expected answer.",
            "gold_evidences": [
                {"source_id": "doc1", "page": 6},
                {"source_id": "doc2", "page": 10},
            ],
            "evidence_quote": "An AI system is engineered to produce outputs and operates safely.",
            "filter": {"year": 2023, "section": "govern"},
        }

        # Case 1: Alternative gold_evidences page matches full quote at rank 2 (hit@1=False, hit@3=True).
        hits_alt = [
            {
                "chunk_id": "C:doc3:p001:c01",
                "document_id": "doc3",
                "source": "data/doc3.pdf",
                "page": 1,
                "score": 0.85,
                "distance": 0.15,
                "metadata": {"year": 2023, "section": "govern"},
                "text": "Unrelated chunk text.",
            },
            {
                "chunk_id": "C:doc2:p010:c01",
                "document_id": "doc2",
                "source": "data/doc2.pdf",
                "page": 10,
                "score": 0.80,
                "distance": 0.20,
                "metadata": {"year": 2023, "section": "govern"},
                "text": "Context: An AI system is engineered to produce outputs and operates safely. Further notes follow.",
            },
        ]
        tr_alt = evaluate_query(q, hits_alt, strategy="C", applied_filter=q["filter"])
        self.assertFalse(tr_alt["hit1"])
        self.assertTrue(tr_alt["hit3"])
        self.assertTrue(tr_alt["filter_compliant"])

        # Case 2: Evidence split across two overlapping chunks ranked out of page order -> split diagnostic=True.
        hits_split = [
            {
                "chunk_id": "A:doc1:p006:c02",
                "document_id": "doc1",
                "source": "data/doc1.pdf",
                "page": 6,
                "score": 0.78,
                "distance": 0.22,
                "metadata": {"year": 2023, "section": "govern", "chunk_index": 2},
                "text": "engineered to produce outputs and operates safely.",
            },
            {
                "chunk_id": "A:doc1:p006:c01",
                "document_id": "doc1",
                "source": "data/doc1.pdf",
                "page": 6,
                "score": 0.75,
                "distance": 0.25,
                "metadata": {"year": 2023, "section": "govern", "chunk_index": 1},
                "text": "An AI system is engineered to pro",
            },
        ]
        tr_split = evaluate_query(q, hits_split, strategy="A", applied_filter=None)
        self.assertFalse(tr_split["hit3"])
        self.assertTrue(tr_split["source_page_hit3"])
        self.assertTrue(tr_split["evidence_split_across_top3"])
        self.assertIsNone(tr_split["filter_compliant"])

        agg = summarize([tr_alt, tr_split])
        self.assertEqual(agg["C"]["dev"]["hit3_fraction"], "1/1")
        self.assertEqual(agg["C"]["dev"]["filter_compliance_fraction"], "1/1")
        self.assertEqual(agg["A"]["dev"]["split_evidence_count"], 1)
        self.assertEqual(agg["A"]["dev"]["filter_compliance_fraction"], "N/A")


if __name__ == "__main__":
    unittest.main()
