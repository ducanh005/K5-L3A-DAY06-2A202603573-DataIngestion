"""Runnable CP4 verification script: Gemini embeddings, pgvector storage, A/B/C top-3 & re-ingest check."""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

STARTER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = STARTER_DIR.parent
if str(STARTER_DIR) not in sys.path:
    sys.path.insert(0, str(STARTER_DIR))

from src.chunking import by_paragraph, fixed_size  # noqa: E402
from src.embeddings import DEFAULT_EMBEDDING_DIMENSIONS, DEFAULT_GEMINI_MODEL, MyEmbedder  # noqa: E402
from src.ingestion import ingest_corpus_from_manifest  # noqa: E402
from src.models import Chunk  # noqa: E402
from src.store import PgVectorStore  # noqa: E402

GEMINI_PAID_USD_PER_1M_TOKENS = 0.15


def _load_chunks_json(path: Path) -> list[Chunk]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [
        Chunk(
            chunk_id=r["chunk_id"],
            document_id=r["document_id"],
            source=r["source"],
            page=int(r["page"]),
            text=r["text"],
            metadata=dict(r.get("metadata") or {}),
        )
        for r in rows
    ]


def _load_or_build_chunks() -> tuple[list[Chunk], list[Chunk]]:
    results_dir = REPO_ROOT / "results"
    path_a = results_dir / "chunks_A.json"
    path_b = results_dir / "chunks_B.json"
    if path_a.exists() and path_b.exists():
        return _load_chunks_json(path_a), _load_chunks_json(path_b)

    pages, _ = ingest_corpus_from_manifest(REPO_ROOT / "data" / "manifest.json", repo_root=REPO_ROOT)
    chunks_a = [c for page in pages for c in fixed_size(page, size=500, overlap=50, strategy_prefix="A")]
    chunks_b = [c for page in pages for c in by_paragraph(page, max_size=900, strategy_prefix="B")]
    return chunks_a, chunks_b


def _clone_chunks_for_strategy_c(chunks_b: list[Chunk]) -> list[Chunk]:
    cloned: list[Chunk] = []
    for chunk in chunks_b:
        cid = f"C:{chunk.chunk_id.split(':', 1)[1]}" if ":" in chunk.chunk_id else f"C:{chunk.chunk_id}"
        meta = dict(chunk.metadata)
        meta["strategy"] = "C"
        cloned.append(
            Chunk(
                chunk_id=cid,
                document_id=chunk.document_id,
                source=chunk.source,
                page=chunk.page,
                text=chunk.text,
                metadata=meta,
            )
        )
    return cloned


def _length_stats(chunks: list[Chunk]) -> dict[str, float | int]:
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
    }


def _verify_obsolete_cleanup(db_url: str, dimensions: int) -> dict[str, Any]:
    probe = PgVectorStore(db_url, dimensions=dimensions, table_prefix="day6_cp4_prune_check")
    probe.drop_tables()
    try:
        probe.create_schema()
        vec_a = [1.0] + [0.0] * (dimensions - 1)
        vec_b = [0.8, 0.6] + [0.0] * (dimensions - 2)
        initial = [
            Chunk("A:probe:p001:c01", "probe-doc", "data/probe.pdf", 1, "chunk 1", {"year": 2024}),
            Chunk("A:probe:p001:c02", "probe-doc", "data/probe.pdf", 1, "chunk 2", {"year": 2024}),
        ]
        probe.upsert("A", initial, [vec_a, vec_b])
        count_initial = probe.count("A")

        updated = [
            Chunk("A:probe:p001:c01_new", "probe-doc", "data/probe.pdf", 1, "updated chunk", {"year": 2024}),
        ]
        probe.upsert("A", updated, [vec_a])
        deleted_obsolete = probe.last_upsert_stats["deleted_obsolete"]
        count_after = probe.count("A")
        if count_initial != 2 or deleted_obsolete != 2 or count_after != 1:
            raise RuntimeError(
                f"Obsolete cleanup check failed: initial={count_initial}, "
                f"deleted={deleted_obsolete}, after={count_after}"
            )
        return {
            "verified": True,
            "initial_rows": count_initial,
            "deleted_obsolete_rows": deleted_obsolete,
            "rows_after_update": count_after,
        }
    finally:
        probe.drop_tables()


def _matches_filter(hit: dict[str, Any], flt: dict[str, Any] | None) -> bool:
    if not flt:
        return True
    meta = hit.get("metadata", {})
    for key, expected in flt.items():
        actual = hit.get(key) if key in ("document_id", "source", "page") else meta.get(key)
        if actual != expected:
            return False
    return True


def main() -> None:
    load_dotenv(STARTER_DIR / ".env")
    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        raise RuntimeError("DATABASE_URL is not set in starter/.env")

    model_name = os.getenv("EMBEDDING_MODEL", "").strip() or DEFAULT_GEMINI_MODEL
    embedder = MyEmbedder(model_name=model_name, dimensions=DEFAULT_EMBEDDING_DIMENSIONS, batch_size=50)
    store = PgVectorStore(db_url, dimensions=embedder.dimensions)

    t_schema0 = time.perf_counter()
    store.create_schema()
    schema_seconds = round(time.perf_counter() - t_schema0, 4)

    chunks_a, chunks_b = _load_or_build_chunks()
    chunks_c = _clone_chunks_for_strategy_c(chunks_b)

    # Embed Strategy A and Strategy B once; Strategy C reuses Strategy B's chunks and vectors.
    embedder.reset_stats()
    t_ea0 = time.perf_counter()
    vectors_a = embedder.embed_documents([c.text for c in chunks_a])
    embed_a_seconds = round(time.perf_counter() - t_ea0, 3)

    t_ua0 = time.perf_counter()
    store.upsert("A", chunks_a, vectors_a, prune_missing_documents=True)
    upsert_a_seconds = round(time.perf_counter() - t_ua0, 3)

    t_eb0 = time.perf_counter()
    vectors_b = embedder.embed_documents([c.text for c in chunks_b])
    embed_b_seconds = round(time.perf_counter() - t_eb0, 3)

    t_ub0 = time.perf_counter()
    store.upsert("B", chunks_b, vectors_b, prune_missing_documents=True)
    upsert_b_seconds = round(time.perf_counter() - t_ub0, 3)

    t_uc0 = time.perf_counter()
    store.upsert("C", chunks_c, vectors_b, prune_missing_documents=True)
    upsert_c_seconds = round(time.perf_counter() - t_uc0, 3)

    counts_before_reingest = {s: store.count(s) for s in ("A", "B", "C")}

    # Re-ingest idempotency check on live tables.
    store.upsert("A", chunks_a, vectors_a)
    del_a = store.last_upsert_stats["deleted_obsolete"]
    store.upsert("B", chunks_b, vectors_b)
    del_b = store.last_upsert_stats["deleted_obsolete"]
    store.upsert("C", chunks_c, vectors_b)
    del_c = store.last_upsert_stats["deleted_obsolete"]
    counts_after_reingest = {s: store.count(s) for s in ("A", "B", "C")}

    if counts_before_reingest != counts_after_reingest:
        raise RuntimeError(f"Re-ingest count mismatch: {counts_before_reingest} != {counts_after_reingest}")
    if any(d != 0 for d in (del_a, del_b, del_c)):
        raise RuntimeError(f"Unexpected obsolete deletions during identical re-ingest: {(del_a, del_b, del_c)}")

    obsolete_check = _verify_obsolete_cleanup(db_url, embedder.dimensions)

    # Top-3 retrieval smoke check strictly using dev split questions (never test split in CP4).
    all_questions = json.loads((REPO_ROOT / "benchmark" / "questions.json").read_text(encoding="utf-8"))["questions"]
    dev_questions = [q for q in all_questions if q.get("split") == "dev"]
    smoke_questions = [q for q in dev_questions if not q.get("filter")][:1] + [
        q for q in dev_questions if q.get("filter")
    ]

    smoke_results: list[dict[str, Any]] = []
    for q in smoke_questions:
        q_vec = embedder.embed_query(q["question"])
        flt = q.get("filter")
        per_strategy: dict[str, Any] = {}
        for strategy in ("A", "B", "C"):
            active_filter = flt if strategy == "C" else None
            t_s0 = time.perf_counter()
            hits = store.search(strategy, q_vec, top_k=3, filters=active_filter)
            search_ms = round((time.perf_counter() - t_s0) * 1000.0, 2)
            if len(hits) != 3:
                raise RuntimeError(
                    f"Expected top-3 hits for query {q['id']} strategy {strategy}, got {len(hits)}."
                )
            if strategy == "C" and active_filter:
                bad = [h["chunk_id"] for h in hits if not _matches_filter(h, active_filter)]
                if bad:
                    raise RuntimeError(f"Strategy C filter violated on {q['id']}: {bad}")
            per_strategy[strategy] = {
                "filter_applied": active_filter,
                "search_ms": search_ms,
                "top3": [
                    {
                        "rank": i + 1,
                        "chunk_id": h["chunk_id"],
                        "source": h["source"],
                        "page": h["page"],
                        "score": round(h["score"], 4),
                        "year": h["metadata"].get("year"),
                        "document_type": h["metadata"].get("document_type"),
                        "section": h["metadata"].get("section"),
                    }
                    for i, h in enumerate(hits)
                ],
            }
        smoke_results.append(
            {
                "question_id": q["id"],
                "split": q["split"],
                "question": q["question"],
                "benchmark_filter": flt,
                "strategies": per_strategy,
            }
        )

    embed_stats = embedder.get_stats()
    token_basis = embed_stats["reported_tokens"] or embed_stats["estimated_tokens"]
    paid_ref_cost = round((token_basis / 1_000_000.0) * GEMINI_PAID_USD_PER_1M_TOKENS, 6)

    summary = {
        "embedding": {
            "provider": embed_stats["provider"],
            "model": embed_stats["model"],
            "dimensions": embed_stats["dimensions"],
            "normalized_non_default_dims": embed_stats["normalized_non_default_dims"],
            "document_task_type": embed_stats["document_task_type"],
            "query_task_type": embed_stats["query_task_type"],
            "distance_metric": "cosine",
            "api_calls": embed_stats["api_calls"],
            "document_texts_embedded": embed_stats["document_texts"],
            "query_texts_embedded": embed_stats["query_texts"],
            "input_chars": embed_stats["input_chars"],
            "reported_tokens": embed_stats["reported_tokens"],
            "estimated_tokens": embed_stats["estimated_tokens"],
            "retries": embed_stats["retries"],
            "api_embed_seconds": round(embed_stats["elapsed_seconds"], 3),
            "pricing_reference_usd_per_1m_tokens": GEMINI_PAID_USD_PER_1M_TOKENS,
            "estimated_paid_tier_cost_usd": paid_ref_cost,
            "free_tier_cost_usd": 0.0,
        },
        "database": {
            "engine": "PostgreSQL + pgvector",
            "schema_seconds": schema_seconds,
            "tables": {"A": "day6_chunks_a", "B": "day6_chunks_b", "C": "day6_chunks_c"},
            "counts": counts_after_reingest,
        },
        "strategies": {
            "A": {
                "params": {"method": "fixed_size", "size": 500, "overlap": 50, "metadata_filter": False},
                "stats": _length_stats(chunks_a),
                "embed_seconds": embed_a_seconds,
                "upsert_seconds": upsert_a_seconds,
                "db_rows": counts_after_reingest["A"],
            },
            "B": {
                "params": {"method": "by_paragraph", "max_size": 900, "metadata_filter": False},
                "stats": _length_stats(chunks_b),
                "embed_seconds": embed_b_seconds,
                "upsert_seconds": upsert_b_seconds,
                "db_rows": counts_after_reingest["B"],
            },
            "C": {
                "params": {
                    "method": "by_paragraph",
                    "max_size": 900,
                    "metadata_filter": True,
                    "reused_embeddings_from": "B",
                },
                "stats": _length_stats(chunks_c),
                "embed_seconds": 0.0,
                "upsert_seconds": upsert_c_seconds,
                "db_rows": counts_after_reingest["C"],
            },
        },
        "reingest_verification": {
            "counts_before": counts_before_reingest,
            "counts_after": counts_after_reingest,
            "idempotent_exact_match": counts_before_reingest == counts_after_reingest,
            "identical_reingest_deleted_obsolete": {"A": del_a, "B": del_b, "C": del_c},
            "obsolete_cleanup_probe": obsolete_check,
        },
        "top3_dev_smoke_checks": smoke_results,
    }

    out_path = REPO_ROOT / "results" / "cp4_vector_summary.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(
        f"Embedder: {summary['embedding']['model']} | dims={summary['embedding']['dimensions']} "
        f"| metric=cosine | api_calls={summary['embedding']['api_calls']} "
        f"| reported_tokens={summary['embedding']['reported_tokens']} "
        f"| est_tokens={summary['embedding']['estimated_tokens']}"
    )
    for s in ("A", "B", "C"):
        st = summary["strategies"][s]
        print(
            f"Strategy {s}: rows={st['db_rows']}, total_chars={st['stats']['total_chars']}, "
            f"embed_s={st['embed_seconds']}, upsert_s={st['upsert_seconds']}"
        )
    print(
        f"Re-ingest check: identical={summary['reingest_verification']['idempotent_exact_match']}, "
        f"obsolete_probe_deleted={obsolete_check['deleted_obsolete_rows']}"
    )
    for item in smoke_results:
        hits_str = ", ".join(
            f"{s}=>{item['strategies'][s]['top3'][0]['source']}#p{item['strategies'][s]['top3'][0]['page']}"
            f"({item['strategies'][s]['top3'][0]['score']})"
            for s in ("A", "B", "C")
        )
        print(f"Top-3 smoke [{item['question_id']}] (filter={item['benchmark_filter']}): {hits_str}")
    print(f"Saved CP4 report -> {out_path}")


if __name__ == "__main__":
    main()
