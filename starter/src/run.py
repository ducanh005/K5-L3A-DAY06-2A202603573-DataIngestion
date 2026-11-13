"""End-to-end Day 06 pipeline orchestrator: ingest, chunk, embed, pgvector search, evaluate & audit prep."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as pkg_version
import json
import os
import platform
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
from src.evaluation import evaluate_query, normalize_for_match, summarize  # noqa: E402
from src.ingestion import ingest_corpus_from_manifest  # noqa: E402
from src.models import Chunk, DocumentPage  # noqa: E402
from src.store import PgVectorStore  # noqa: E402

PRELOCKED_SELECTION_CRITERION = (
    "Pre-test selection rule: prioritize Evidence Hit@3 first, then Hit@1 and filter compliance, "
    "followed by Coherence/Grounding quality and resource footprint (chunk count, embed time/cost). "
    "Strategy hyperparameters were frozen on dev before inspecting test split scores."
)


def _pkg_ver(name: str) -> str:
    try:
        return pkg_version(name)
    except PackageNotFoundError:
        return "not_installed"


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


def _select_fixed_coherence_positions(pages: list[DocumentPage], per_doc: int = 5) -> list[dict[str, Any]]:
    """Select 10 deterministic source positions sorted by (document_id, page) prior to seeing scores."""
    by_doc: dict[str, list[DocumentPage]] = {}
    for page in sorted(pages, key=lambda p: (p.document_id, p.page)):
        by_doc.setdefault(page.document_id, []).append(page)

    positions: list[dict[str, Any]] = []
    idx = 0
    for doc_id in sorted(by_doc):
        for page in by_doc[doc_id][:per_doc]:
            idx += 1
            text = page.text
            # Sample deterministically near the 500-char mark to expose boundary splits cleanly.
            center = min(490, max(120, len(text) // 2))
            start = max(0, center - 45)
            end = min(len(text), center + 45)
            while start > 0 and not text[start - 1].isspace():
                start -= 1
            while end < len(text) and not text[end].isspace():
                end += 1
            anchor = " ".join(text[start:end].split())
            positions.append(
                {
                    "position_id": f"pos_{idx:02d}",
                    "document_id": page.document_id,
                    "source": page.source,
                    "page": page.page,
                    "section": page.metadata.get("section"),
                    "target_char_window": [start, end],
                    "anchor_excerpt": anchor,
                }
            )
    return positions


def _find_chunks_covering_position(chunks: list[Chunk], pos: dict[str, Any]) -> list[dict[str, Any]]:
    doc_id = pos["document_id"]
    page_num = int(pos["page"])
    anchor_norm = normalize_for_match(pos["anchor_excerpt"])
    mid = len(anchor_norm) // 2
    left_probe = anchor_norm[: max(24, mid)]
    right_probe = anchor_norm[max(0, mid - 12) :]

    page_chunks = [c for c in chunks if c.document_id == doc_id and c.page == page_num]
    matched: list[Chunk] = []
    for chunk in page_chunks:
        cnorm = normalize_for_match(chunk.text)
        if anchor_norm in cnorm or (left_probe and left_probe in cnorm) or (right_probe and right_probe in cnorm):
            matched.append(chunk)
    if not matched and page_chunks:
        matched = [page_chunks[0]]
    return [
        {
            "chunk_id": c.chunk_id,
            "char_len": len(c.text),
            "section": c.metadata.get("section"),
            "text": c.text,
        }
        for c in matched
    ]


def _update_experiment_log(
    csv_path: Path,
    summary: dict[str, Any],
    strategy_notes: dict[str, str],
) -> None:
    if not csv_path.exists():
        return
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    for row in rows:
        strat = row.get("strategy", "").strip().upper()
        if strat not in summary:
            continue
        dev = summary[strat]["dev"]
        test = summary[strat]["test"]
        row["dev_hit1"] = f"{dev['hit1_fraction']} ({dev['hit1_rate']:.3f})"
        row["dev_hit3"] = f"{dev['hit3_fraction']} ({dev['hit3_rate']:.3f})"
        row["test_hit1"] = f"{test['hit1_fraction']} ({test['hit1_rate']:.3f})"
        row["test_hit3"] = f"{test['hit3_fraction']} ({test['hit3_rate']:.3f})"
        if strat in strategy_notes:
            row["notes"] = strategy_notes[strat]

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_manual_audit_markdown(out_path: Path, worksheet: dict[str, Any]) -> None:
    lines: list[str] = [
        "# Day 06 Manual Audit Worksheet (Template — Populate Human Labels After Review)",
        "",
        f"Generated at: `{worksheet['generated_at']}`",
        "Scoring scales per `GUIDE.md`: Coherence `0|1|2`, Metadata `pass|fail`, Grounding `0|1|2`.",
        "",
        "## Part 1 — Chunk Coherence (10 Fixed Source Positions Across A and B/C)",
        "",
    ]
    for item in worksheet["coherence_positions"]:
        lines.append(
            f"### {item['position_id']} — `{item['document_id']}` page {item['page']} (`{item['section']}`)"
        )
        lines.append(f"- Anchor excerpt: `{item['anchor_excerpt']}`")
        for strat in ("A", "B"):
            label = "A (fixed 500/50)" if strat == "A" else "B/C (by_paragraph max 900)"
            lines.append(f"- **{label}** (`human_score: TBD` | `reason: TBD`):")
            for ch in item["strategies"][strat]["covering_chunks"]:
                snippet = " ".join(ch["text"].split())
                lines.append(f"  - `{ch['chunk_id']}` ({ch['char_len']} chars): {snippet}")
        lines.append("")

    lines.extend(
        [
            "## Part 2 — Test Split Grounding & Metadata Inspection (5 Test Questions x Top-3 x A/B/C)",
            "",
        ]
    )
    for tq in worksheet["test_grounding_and_metadata"]:
        lines.append(f"### {tq['question_id']} (`type={tq['type']}`, `filter={tq['benchmark_filter']}`)")
        lines.append(f"- Question: {tq['question']}")
        lines.append(f"- Gold: `{tq['gold_evidences']}`")
        lines.append(f"- Gold evidence: `{tq['evidence_quote']}`")
        for strat in ("A", "B", "C"):
            sdata = tq["strategies"][strat]
            lines.append(
                f"- **Strategy {strat}** (auto hit@1={sdata['auto_hit1']}, hit@3={sdata['auto_hit3']}, "
                f"filter_ok={sdata['auto_filter_compliant']} | `grounding_score: TBD` | `metadata_ok: TBD` | `reason: TBD`):"
            )
            for h in sdata["top3"]:
                snippet = " ".join(h["text"].split())
                lines.append(
                    f"  - Rank {h['rank']} `{h['chunk_id']}` | `{h['source']}` p.{h['page']} | "
                    f"score={h['score']:.4f} | meta=(year={h['metadata'].get('year')}, "
                    f"type={h['metadata'].get('document_type')}, section={h['metadata'].get('section')})"
                )
                lines.append(f"    > {snippet}")
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def run_pipeline(*, reuse_existing_index: bool = False) -> dict[str, Any]:
    """Execute the full Day 06 corpus, chunking, embedding, retrieval, evaluation, and audit export flow."""
    load_dotenv(STARTER_DIR / ".env")
    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        raise RuntimeError("DATABASE_URL is not set in starter/.env")

    results_dir = REPO_ROOT / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = REPO_ROOT / "data" / "manifest.json"
    questions_path = REPO_ROOT / "benchmark" / "questions.json"

    # 1. Load corpus via manifest and assert >= 30 valid content pages.
    pages, ingestion_log = ingest_corpus_from_manifest(
        manifest_path,
        repo_root=REPO_ROOT,
        log_output_path=results_dir / "ingestion_log.json",
    )
    if ingestion_log["included_pages"] < 30:
        raise RuntimeError(f"Expected >= 30 valid content pages, got {ingestion_log['included_pages']}.")

    # 2. Build strategies A, B, and C.
    t_c0 = time.perf_counter()
    chunks_a = [c for page in pages for c in fixed_size(page, size=500, overlap=50, strategy_prefix="A")]
    chunks_b = [c for page in pages for c in by_paragraph(page, max_size=900, strategy_prefix="B")]
    chunks_c = _clone_chunks_for_strategy_c(chunks_b)
    chunk_seconds = round(time.perf_counter() - t_c0, 4)

    model_name = os.getenv("EMBEDDING_MODEL", "").strip() or DEFAULT_GEMINI_MODEL
    embedder = MyEmbedder(model_name=model_name, dimensions=DEFAULT_EMBEDDING_DIMENSIONS, batch_size=50)
    store = PgVectorStore(db_url, dimensions=embedder.dimensions)
    store.create_schema()

    expected_counts = {"A": len(chunks_a), "B": len(chunks_b), "C": len(chunks_c)}
    existing_counts = {s: store.count(s) for s in ("A", "B", "C")}

    doc_embed_timing: dict[str, Any]
    if reuse_existing_index and existing_counts == expected_counts:
        cp4_path = results_dir / "cp4_vector_summary.json"
        cp4_data = json.loads(cp4_path.read_text(encoding="utf-8")) if cp4_path.exists() else {}
        doc_embed_timing = {
            "mode": "reused_verified_pgvector_index",
            "counts": existing_counts,
            "previous_ingest_embedding": cp4_data.get("embedding", {}),
            "previous_strategy_timings": {
                s: {
                    "embed_seconds": cp4_data.get("strategies", {}).get(s, {}).get("embed_seconds"),
                    "upsert_seconds": cp4_data.get("strategies", {}).get(s, {}).get("upsert_seconds"),
                }
                for s in ("A", "B", "C")
            },
        }
    else:
        embedder.reset_stats()
        t_ea0 = time.perf_counter()
        vectors_a = embedder.embed_documents([c.text for c in chunks_a])
        ea_s = round(time.perf_counter() - t_ea0, 3)
        store.upsert("A", chunks_a, vectors_a, prune_missing_documents=True)

        t_eb0 = time.perf_counter()
        vectors_b = embedder.embed_documents([c.text for c in chunks_b])
        eb_s = round(time.perf_counter() - t_eb0, 3)
        store.upsert("B", chunks_b, vectors_b, prune_missing_documents=True)
        store.upsert("C", chunks_c, vectors_b, prune_missing_documents=True)
        doc_embed_timing = {
            "mode": "fresh_full_ingest",
            "counts": {s: store.count(s) for s in ("A", "B", "C")},
            "doc_embed_stats": embedder.get_stats(),
            "strategy_embed_seconds": {"A": ea_s, "B": eb_s, "C": 0.0},
        }

    # 3. Run all benchmark questions (dev + test) with identical query vectors across A/B/C.
    bench = json.loads(questions_path.read_text(encoding="utf-8"))
    questions = bench["questions"]

    embedder.reset_stats()
    query_vectors: dict[str, list[float]] = {}
    for q in questions:
        query_vectors[q["id"]] = embedder.embed_query(q["question"])
    query_embed_stats = embedder.get_stats()

    all_traces: list[dict[str, Any]] = []
    traces_by_strategy: dict[str, list[dict[str, Any]]] = {"A": [], "B": [], "C": []}
    search_latencies_ms: dict[str, list[float]] = {"A": [], "B": [], "C": []}

    for strategy in ("A", "B", "C"):
        for q in questions:
            flt = q.get("filter") if strategy == "C" else None
            t_s0 = time.perf_counter()
            hits = store.search(strategy, query_vectors[q["id"]], top_k=3, filters=flt)
            search_latencies_ms[strategy].append((time.perf_counter() - t_s0) * 1000.0)
            trace = evaluate_query(q, hits, strategy=strategy, applied_filter=flt)
            traces_by_strategy[strategy].append(trace)
            all_traces.append(trace)

    # Save full traces per strategy and unified trace bundle.
    for strategy in ("A", "B", "C"):
        (results_dir / f"traces_{strategy}.json").write_text(
            json.dumps(traces_by_strategy[strategy], indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    (results_dir / "cp5_traces.json").write_text(
        json.dumps(all_traces, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    eval_summary = summarize(all_traces)

    # Record versions, timings, and summary metrics.
    with store._connect() as conn:
        pg_ver = conn.execute("SHOW server_version;").fetchone()["server_version"]
        pgvec_ver = conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector';"
        ).fetchone()["extversion"]

    summary_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "selection_criterion": PRELOCKED_SELECTION_CRITERION,
        "environment": {
            "python": platform.python_version(),
            "google_genai": _pkg_ver("google-genai"),
            "psycopg": _pkg_ver("psycopg"),
            "pymupdf": _pkg_ver("PyMuPDF"),
            "pypdf": _pkg_ver("pypdf"),
            "postgres_server": pg_ver,
            "pgvector": pgvec_ver,
        },
        "embedding": {
            "provider": "google-gemini",
            "model": embedder.model_name,
            "dimensions": embedder.dimensions,
            "distance_metric": "cosine",
            "document_index": doc_embed_timing,
            "query_eval_stats": query_embed_stats,
        },
        "corpus_and_chunks": {
            "included_pages": ingestion_log["included_pages"],
            "chunk_build_seconds": chunk_seconds,
            "strategies": {
                "A": _length_stats(chunks_a),
                "B": _length_stats(chunks_b),
                "C": _length_stats(chunks_c),
            },
        },
        "search_latency_ms": {
            s: {
                "mean_ms": round(statistics.mean(vals), 2),
                "max_ms": round(max(vals), 2),
                "total_ms": round(sum(vals), 2),
            }
            for s, vals in search_latencies_ms.items()
        },
        "metrics": eval_summary,
    }
    (results_dir / "cp5_eval_summary.json").write_text(
        json.dumps(summary_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # Prepare clean manual audit worksheet (unlabeled templates backed by exact trace outputs).
    coherence_positions = _select_fixed_coherence_positions(pages, per_doc=5)
    coherence_items = [
        {
            **pos,
            "strategies": {
                "A": {
                    "covering_chunks": _find_chunks_covering_position(chunks_a, pos),
                    "human_score": None,
                    "reason": "",
                },
                "B": {
                    "covering_chunks": _find_chunks_covering_position(chunks_b, pos),
                    "human_score": None,
                    "reason": "",
                },
            },
        }
        for pos in coherence_positions
    ]

    test_questions = [q for q in questions if q.get("split") == "test"]
    trace_index = {(t["strategy"], t["question_id"]): t for t in all_traces}
    test_audit_items: list[dict[str, Any]] = []
    for q in test_questions:
        strat_map: dict[str, Any] = {}
        for strat in ("A", "B", "C"):
            tr = trace_index[(strat, q["id"])]
            strat_map[strat] = {
                "auto_hit1": tr["hit1"],
                "auto_hit3": tr["hit3"],
                "auto_source_page_hit3": tr["source_page_hit3"],
                "auto_evidence_split_across_top3": tr["evidence_split_across_top3"],
                "auto_filter_compliant": tr["filter_compliant"],
                "grounding_score": None,
                "metadata_ok": None,
                "reason": "",
                "top3": tr["hits"],
            }
        test_audit_items.append(
            {
                "question_id": q["id"],
                "type": q.get("type"),
                "question": q["question"],
                "expected_answer": q["expected_answer"],
                "gold_evidences": q["gold_evidences"],
                "evidence_quote": q["evidence_quote"],
                "benchmark_filter": q.get("filter"),
                "strategies": strat_map,
            }
        )

    worksheet = {
        "generated_at": summary_payload["generated_at"],
        "coherence_positions": coherence_items,
        "test_grounding_and_metadata": test_audit_items,
    }
    (results_dir / "manual_audit_worksheet.json").write_text(
        json.dumps(worksheet, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _write_manual_audit_markdown(results_dir / "manual_audit_worksheet.md", worksheet)

    # Update experiment_log.csv with observed dev/test Hit@1 and Hit@3.
    strategy_notes = {
        s: (
            f"{expected_counts[s]} chunks; mean_search {summary_payload['search_latency_ms'][s]['mean_ms']}ms; "
            f"filter_dev={eval_summary[s]['dev']['filter_compliance_fraction']}; "
            f"filter_test={eval_summary[s]['test']['filter_compliance_fraction']}"
        )
        for s in ("A", "B", "C")
    }
    _update_experiment_log(REPO_ROOT / "artifacts" / "experiment_log.csv", eval_summary, strategy_notes)
    return summary_payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Day 06 RAG data-strategy pipeline & evaluation.")
    parser.add_argument(
        "--reuse-existing-index",
        action="store_true",
        help="Reuse existing pgvector tables when row counts match the current manifest chunks.",
    )
    args = parser.parse_args()
    summary = run_pipeline(reuse_existing_index=args.reuse_existing_index)
    for strat in ("A", "B", "C"):
        m = summary["metrics"][strat]
        print(
            f"Strategy {strat} | dev Hit@1={m['dev']['hit1_fraction']} ({m['dev']['hit1_rate']:.3f}), "
            f"Hit@3={m['dev']['hit3_fraction']} ({m['dev']['hit3_rate']:.3f}) | "
            f"test Hit@1={m['test']['hit1_fraction']} ({m['test']['hit1_rate']:.3f}), "
            f"Hit@3={m['test']['hit3_fraction']} ({m['test']['hit3_rate']:.3f}) | "
            f"filter_test={m['test']['filter_compliance_fraction']}"
        )


if __name__ == "__main__":
    main()

