"""Evaluate retrieved top-k chunks against verified gold evidence without fabricating manual labels."""
from __future__ import annotations

from collections import defaultdict
from difflib import SequenceMatcher
import re
import unicodedata
from typing import Any


def normalize_for_match(text: str) -> str:
    """Normalize Unicode, quotes/dashes, hyphen-breaks, and whitespace for fair evidence matching."""
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
    return " ".join(cleaned.split()).lower()


def _quote_coverage(quote_norm: str, chunk_norm: str) -> float:
    """Estimate normalized substring/span overlap in [0, 1] for split-evidence diagnostics."""
    if not quote_norm:
        return 0.0
    if quote_norm in chunk_norm:
        return 1.0
    matcher = SequenceMatcher(None, quote_norm, chunk_norm, autojunk=False)
    matched_chars = sum(block.size for block in matcher.get_matching_blocks() if block.size >= 8)
    return round(min(1.0, matched_chars / len(quote_norm)), 4)


def _hit_matches_filter(hit: dict[str, Any], flt: dict[str, Any]) -> bool:
    meta = hit.get("metadata") or {}
    for key, expected in flt.items():
        if expected is None:
            continue
        actual = hit.get(key) if key in ("document_id", "source", "page") else meta.get(key)
        if isinstance(expected, (list, tuple, set)):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def _extract_gold_specs(question: dict[str, Any]) -> tuple[set[tuple[str, int]], list[str]]:
    pairs: set[tuple[str, int]] = set()
    quotes: list[str] = []

    base_quote = str(question.get("evidence_quote") or "").strip()
    if base_quote:
        quotes.append(normalize_for_match(base_quote))

    evidences = question.get("gold_evidences") or []
    if evidences:
        for ev in evidences:
            src_id = str(ev.get("source_id") or "").strip()
            page = ev.get("page")
            if src_id and page is not None:
                pairs.add((src_id, int(page)))
            alt_q = str(ev.get("evidence_quote") or "").strip()
            if alt_q:
                norm_alt = normalize_for_match(alt_q)
                if norm_alt not in quotes:
                    quotes.append(norm_alt)
    elif question.get("gold_source_id") and question.get("gold_page") is not None:
        pairs.add((str(question["gold_source_id"]), int(question["gold_page"])))

    if not pairs:
        raise ValueError(f"Question '{question.get('id')}' has no gold (source_id, page) pairs.")
    if not quotes:
        raise ValueError(f"Question '{question.get('id')}' has no evidence_quote.")
    return pairs, quotes


def _merge_ordered_gold_texts(gold_hits: list[dict[str, Any]]) -> str:
    """Merge top-k chunks on gold pages in document order, stripping fixed-size window overlap."""
    ordered = sorted(
        gold_hits,
        key=lambda h: (
            str(h.get("document_id") or ""),
            int(h.get("page", -1)),
            int((h.get("metadata") or {}).get("chunk_index", 0)),
            str(h.get("chunk_id") or ""),
        ),
    )
    merged = ""
    for hit in ordered:
        nxt = str(hit.get("text") or "")
        if not nxt:
            continue
        if not merged:
            merged = nxt
            continue
        overlap_len = 0
        max_k = min(len(merged), len(nxt), 160)
        for k in range(max_k, 5, -1):
            if merged.endswith(nxt[:k]):
                overlap_len = k
                break
        merged = (merged + nxt[overlap_len:]) if overlap_len else (merged + " " + nxt)
    return normalize_for_match(merged)


def evaluate_query(
    question: dict[str, Any],
    hits: list[dict[str, Any]],
    *,
    strategy: str | None = None,
    applied_filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate top-3 hits against documented gold evidence and filter rules; keep full trace."""
    valid_pairs, quote_norms = _extract_gold_specs(question)
    top3 = list(hits[:3])

    evaluated_hits: list[dict[str, Any]] = []
    gold_hits: list[dict[str, Any]] = []

    for rank, hit in enumerate(top3, start=1):
        doc_id = str(hit.get("document_id") or "")
        page = int(hit.get("page", -1))
        text = str(hit.get("text") or "")
        chunk_norm = normalize_for_match(text)
        source_page_match = (doc_id, page) in valid_pairs

        if source_page_match:
            gold_hits.append(hit)

        best_cov = max((_quote_coverage(qn, chunk_norm) for qn in quote_norms), default=0.0)
        full_quote_match = any(qn in chunk_norm for qn in quote_norms)
        is_hit = source_page_match and full_quote_match

        evaluated_hits.append(
            {
                "rank": rank,
                "chunk_id": hit.get("chunk_id"),
                "document_id": doc_id,
                "source": hit.get("source"),
                "page": page,
                "score": round(float(hit.get("score", 0.0)), 6),
                "distance": round(float(hit.get("distance", 0.0)), 6),
                "metadata": dict(hit.get("metadata") or {}),
                "text": text,
                "source_page_match": source_page_match,
                "full_quote_match": full_quote_match,
                "quote_coverage": 1.0 if full_quote_match else best_cov,
                "is_hit": is_hit,
                "filter_match": _hit_matches_filter(hit, applied_filter) if applied_filter else None,
            }
        )

    hit1 = bool(evaluated_hits and evaluated_hits[0]["is_hit"])
    hit3 = any(h["is_hit"] for h in evaluated_hits)
    source_page_hit1 = bool(evaluated_hits and evaluated_hits[0]["source_page_match"])
    source_page_hit3 = any(h["source_page_match"] for h in evaluated_hits)

    # Check if evidence was split across multiple gold-page chunks in top-3 (accounting for overlap).
    merged_gold_norm = _merge_ordered_gold_texts(gold_hits) if gold_hits else ""
    multi_chunk_union_match = any(qn in merged_gold_norm for qn in quote_norms) if merged_gold_norm else False
    evidence_split_across_top3 = (not hit3) and source_page_hit3 and multi_chunk_union_match

    filter_compliant: bool | None = None
    if applied_filter:
        filter_compliant = bool(evaluated_hits) and all(
            bool(h["filter_match"]) for h in evaluated_hits
        )

    return {
        "question_id": question["id"],
        "type": question.get("type"),
        "split": question["split"],
        "strategy": strategy,
        "question": question["question"],
        "expected_answer": question.get("expected_answer"),
        "gold_evidences": [{"source_id": s, "page": p} for s, p in sorted(valid_pairs)],
        "evidence_quote": question.get("evidence_quote"),
        "benchmark_filter": question.get("filter"),
        "applied_filter": applied_filter,
        "hit1": hit1,
        "hit3": hit3,
        "source_page_hit1": source_page_hit1,
        "source_page_hit3": source_page_hit3,
        "evidence_split_across_top3": evidence_split_across_top3,
        "filter_compliant": filter_compliant,
        "hits": evaluated_hits,
    }


def _aggregate_bucket(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    hit1_count = sum(1 for r in rows if r["hit1"])
    hit3_count = sum(1 for r in rows if r["hit3"])
    sp1_count = sum(1 for r in rows if r["source_page_hit1"])
    sp3_count = sum(1 for r in rows if r["source_page_hit3"])
    split_count = sum(1 for r in rows if r.get("evidence_split_across_top3"))

    filtered = [r for r in rows if r.get("applied_filter")]
    filter_n = len(filtered)
    filter_ok = sum(1 for r in filtered if r.get("filter_compliant") is True)

    return {
        "total": total,
        "hit1_count": hit1_count,
        "hit1_rate": round(hit1_count / total, 4) if total else 0.0,
        "hit1_fraction": f"{hit1_count}/{total}",
        "hit3_count": hit3_count,
        "hit3_rate": round(hit3_count / total, 4) if total else 0.0,
        "hit3_fraction": f"{hit3_count}/{total}",
        "source_page_hit1_fraction": f"{sp1_count}/{total}",
        "source_page_hit3_fraction": f"{sp3_count}/{total}",
        "split_evidence_count": split_count,
        "filter_queries": filter_n,
        "filter_compliant_count": filter_ok if filter_n else None,
        "filter_compliance_fraction": f"{filter_ok}/{filter_n}" if filter_n else "N/A",
    }


def summarize(traces: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate Hit@1 and Evidence Hit@3 by strategy and dev/test split."""
    by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trace in traces:
        key = str(trace.get("strategy") or "UNKNOWN")
        by_strategy[key].append(trace)

    report: dict[str, Any] = {}
    for strategy in sorted(by_strategy):
        strat_rows = by_strategy[strategy]
        splits: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in strat_rows:
            splits[str(row["split"])].append(row)
        report[strategy] = {
            "overall": _aggregate_bucket(strat_rows),
            "dev": _aggregate_bucket(splits.get("dev", [])),
            "test": _aggregate_bucket(splits.get("test", [])),
        }
    return report
