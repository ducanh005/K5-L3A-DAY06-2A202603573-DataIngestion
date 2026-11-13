"""Runnable CP3 verification script: ingestion log, normalization checks, and A/B chunking."""
from __future__ import annotations

import json
import re
import statistics
import sys
import time
import unicodedata
from pathlib import Path

STARTER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = STARTER_DIR.parent
if str(STARTER_DIR) not in sys.path:
    sys.path.insert(0, str(STARTER_DIR))

from src.chunking import by_paragraph, fixed_size  # noqa: E402
from src.ingestion import ingest_corpus_from_manifest  # noqa: E402
from src.models import Chunk  # noqa: E402


def _norm_compare(text: str) -> str:
    cleaned = unicodedata.normalize("NFKC", text or "")
    cleaned = re.sub(r"([A-Za-z0-9])-\s*\n\s*([a-z0-9])", r"\1\2", cleaned)
    cleaned = (
        cleaned.replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("–", "-")
        .replace("—", "-")
    )
    return " ".join(cleaned.split())


def _chunk_stats(chunks: list[Chunk], elapsed: float) -> dict[str, float | int]:
    lengths = sorted(len(c.text) for c in chunks)
    p95_idx = min(len(lengths) - 1, int(0.95 * len(lengths)))
    return {
        "chunk_count": len(chunks),
        "total_chars": sum(lengths),
        "min_chars": lengths[0] if lengths else 0,
        "mean_chars": round(statistics.mean(lengths), 1) if lengths else 0.0,
        "median_chars": statistics.median(lengths) if lengths else 0,
        "p95_chars": lengths[p95_idx] if lengths else 0,
        "max_chars": lengths[-1] if lengths else 0,
        "chunking_seconds": round(elapsed, 4),
    }


def _export_chunks(chunks: list[Chunk], out_path: Path) -> None:
    payload = [
        {
            "chunk_id": c.chunk_id,
            "document_id": c.document_id,
            "source": c.source,
            "page": c.page,
            "metadata": c.metadata,
            "text": c.text,
        }
        for c in chunks
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    manifest_path = REPO_ROOT / "data" / "manifest.json"
    results_dir = REPO_ROOT / "results"
    log_path = results_dir / "ingestion_log.json"

    pages, log_payload = ingest_corpus_from_manifest(
        manifest_path,
        repo_root=REPO_ROOT,
        log_output_path=log_path,
    )
    if log_payload["included_pages"] < 30:
        raise RuntimeError(f"Expected >= 30 content pages, got {log_payload['included_pages']}")

    t_a0 = time.perf_counter()
    chunks_a = [c for page in pages for c in fixed_size(page, size=500, overlap=50, strategy_prefix="A")]
    stats_a = _chunk_stats(chunks_a, time.perf_counter() - t_a0)

    t_b0 = time.perf_counter()
    chunks_b = [c for page in pages for c in by_paragraph(page, max_size=900, strategy_prefix="B")]
    stats_b = _chunk_stats(chunks_b, time.perf_counter() - t_b0)

    _export_chunks(chunks_a, results_dir / "chunks_A.json")
    _export_chunks(chunks_b, results_dir / "chunks_B.json")

    # Sanity-check that benchmark evidence quotes survive ingestion normalization intact.
    benchmark = json.loads((REPO_ROOT / "benchmark" / "questions.json").read_text(encoding="utf-8"))
    page_lookup = {(p.document_id, p.page): _norm_compare(p.text) for p in pages}
    missing_evidence = [
        q["id"]
        for q in benchmark["questions"]
        if _norm_compare(q["evidence_quote"]) not in page_lookup.get((q["gold_source_id"], q["gold_page"]), "")
    ]

    # Verify required metadata on all chunks.
    required_keys = {"year", "document_type", "section", "strategy", "chunk_index", "char_len"}
    bad_meta = [
        c.chunk_id
        for c in (*chunks_a, *chunks_b)
        if not c.document_id or not c.source or not c.page or not required_keys.issubset(c.metadata)
    ]

    print("=== CP3 Ingestion Log Summary ===")
    print(
        json.dumps(
            {
                "included_pages": log_payload["included_pages"],
                "excluded_pages": log_payload["excluded_pages"],
                "empty_or_unreadable_pages": log_payload["empty_or_unreadable_pages"],
                "total_normalized_chars": log_payload["total_normalized_chars"],
                "pdf_parse_seconds": log_payload["pdf_parse_seconds"],
                "log_file": str(log_path.relative_to(REPO_ROOT)),
            },
            indent=2,
        )
    )

    print("\n=== CP3 Chunking Comparison (A vs B) ===")
    print("A (fixed_size 500/50):   ", stats_a)
    print("B (by_paragraph max=900):", stats_b)
    print("Metadata check failures: ", bad_meta)
    print("Benchmark quote failures:", missing_evidence)

    # Show concrete comparison on NIST AI 600-1 page 18 + mid-page section transitions.
    sample_a = [c for c in chunks_a if c.document_id == "nist-ai-600-1" and c.page == 18]
    sample_b = [c for c in chunks_b if c.document_id == "nist-ai-600-1" and c.page == 18]
    print(f"\n=== Sample Page: nist-ai-600-1 p.18 (A={len(sample_a)} chunks | B={len(sample_b)} chunks) ===")
    print("--- A[0] ---")
    print(sample_a[0].text)
    print("\n--- B[0] ---")
    print(sample_b[0].text)

    transitions = [
        (c.document_id, c.page, c.chunk_id, c.metadata.get("section"))
        for c in chunks_b
        if (c.document_id, c.page) in {("nist-ai-100-1", 33), ("nist-ai-600-1", 26), ("nist-ai-600-1", 32)}
    ]
    print("\n=== Mid-Page Section Transitions in B ===")
    for row in transitions:
        print(" ", row)

    if bad_meta or missing_evidence:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
