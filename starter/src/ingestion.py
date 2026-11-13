"""Read and normalize source data while keeping full source/page traceability."""
from __future__ import annotations

import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

import pymupdf

from .models import DocumentPage

_KEEP_HYPHEN_LEFT = {
    "ai",
    "cross",
    "decision",
    "energy",
    "gai",
    "high",
    "human",
    "large",
    "long",
    "low",
    "multi",
    "non",
    "open",
    "post",
    "pre",
    "privacy",
    "real",
    "re",
    "risk",
    "self",
    "short",
    "socio",
    "sub",
    "third",
    "well",
}

_KEEP_HYPHEN_RIGHT = {
    "based",
    "centric",
    "class",
    "deployment",
    "driven",
    "enhanced",
    "enhancing",
    "focused",
    "generated",
    "led",
    "level",
    "oriented",
    "purpose",
    "related",
    "relevant",
    "sectoral",
    "specific",
    "term",
    "world",
}

_KEEP_HYPHEN_EXACT = {
    ("pre", "trained"),
    ("pre", "deployment"),
    ("high", "or"),
    ("up", "front"),
}

_CHROME_LINE_PATTERNS = (
    re.compile(r"^NIST AI 100-1$"),
    re.compile(r"^AI RMF 1\.0$"),
    re.compile(r"^NIST Trustworthy and Responsible AI$"),
    re.compile(r"^NIST AI 600-1$"),
    re.compile(r"^Page\s+\d+$", re.IGNORECASE),
    re.compile(r"^Categories Subcategories$"),
    re.compile(r"^Continued on next page$", re.IGNORECASE),
    re.compile(r"^\d{1,3}$"),
)

_TIGHT_HYPHEN_WRAP_RE = re.compile(r"([A-Za-z0-9/]+)-\s*\n\s*([A-Za-z0-9]+)")
_SPACED_HYPHEN_WRAP_RE = re.compile(r"([A-Za-z0-9/]+)-[ \t]+\n[ \t]*([A-Za-z0-9]+)")


def _is_chrome(text: str) -> bool:
    """Return True if a single line or collapsed block matches repeating PDF boilerplate."""
    collapsed = " ".join(text.split())
    return bool(collapsed) and any(pattern.match(collapsed) for pattern in _CHROME_LINE_PATTERNS)


def _strip_running_chrome(lines: list[str]) -> list[str]:
    """Remove repeating PDF running headers, footers, and standalone page numbers."""
    cleaned: list[str] = []
    for raw_line in lines:
        if not raw_line.strip() or _is_chrome(raw_line):
            continue
        # Keep trailing space on hyphen wraps like "energy- " so suspended hyphens stay spaced.
        cleaned.append(raw_line.rstrip("\r\n").lstrip())
    return cleaned


def _dehyphenate(text: str, *, join_soft_hyphens: bool) -> str:
    """Resolve end-of-line hyphenation while preserving compound terms and suspended hyphens."""
    # Suspended hyphens like "energy-\nand" keep the trailing space after '-'.
    text = _SPACED_HYPHEN_WRAP_RE.sub(r"\1- \2", text)
    if not join_soft_hyphens:
        return _TIGHT_HYPHEN_WRAP_RE.sub(r"\1-\2", text)

    def _replace(match: re.Match[str]) -> str:
        left, right = match.group(1), match.group(2)
        pair = (left.lower(), right.lower())
        if (
            pair in _KEEP_HYPHEN_EXACT
            or left.lower() in _KEEP_HYPHEN_LEFT
            or right.lower() in _KEEP_HYPHEN_RIGHT
            or right[0].isupper()
            or right[0].isdigit()
            or "/" in left
        ):
            return f"{left}-{right}"
        return f"{left}{right}"

    return _TIGHT_HYPHEN_WRAP_RE.sub(_replace, text)


def normalize_text(text: str, *, join_soft_hyphens: bool = True) -> str:
    """Normalize Unicode, line wraps, and whitespace while keeping paragraph breaks."""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")

    paragraphs: list[str] = []
    for raw_para in re.split(r"\n\s*\n", normalized):
        kept_lines = _strip_running_chrome(raw_para.split("\n"))
        if not kept_lines:
            continue
        joined_para = _dehyphenate("\n".join(kept_lines), join_soft_hyphens=join_soft_hyphens)
        collapsed = " ".join(joined_para.split())
        if collapsed and not _is_chrome(collapsed):
            paragraphs.append(collapsed)

    return "\n\n".join(paragraphs)


def _extract_page_blocks(page: pymupdf.Page, *, join_soft_hyphens: bool) -> tuple[str, int]:
    """Extract and normalize text blocks from a single PyMuPDF page."""
    raw_chunks: list[str] = []
    norm_blocks: list[str] = []
    for block in page.get_text("blocks"):
        # Skip non-text blocks (e.g. embedded figures/images).
        if len(block) >= 7 and block[6] != 0:
            continue
        block_text = str(block[4])
        raw_chunks.append(block_text)
        cleaned = normalize_text(block_text, join_soft_hyphens=join_soft_hyphens)
        if cleaned:
            norm_blocks.append(cleaned)

    raw_chars = sum(len(chunk) for chunk in raw_chunks)
    return "\n\n".join(norm_blocks), raw_chars


def load_pdf(
    path: Path,
    document_id: str,
    source: str,
    *,
    include_pages: set[int] | None = None,
    exclude_reasons: dict[int, str] | None = None,
    join_soft_hyphens: bool | None = None,
    base_metadata: dict[str, Any] | None = None,
    page_sections: dict[int, str] | None = None,
    page_logs: list[dict[str, Any]] | None = None,
) -> list[DocumentPage]:
    """Extract content pages from a PDF while preserving page numbers, metadata, and error logs."""
    pdf_path = Path(path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")

    if join_soft_hyphens is None:
        # Acrobat-exported Word PDFs (like NIST AI 600-1) break mainly on real hyphens.
        join_soft_hyphens = "600-1" not in pdf_path.name and "600-1" not in document_id

    excluded_map = exclude_reasons or {}
    sections_map = page_sections or {}
    shared_meta = dict(base_metadata or {})
    pages: list[DocumentPage] = []

    with pymupdf.open(pdf_path) as pdf_doc:
        for page_num, pdf_page in enumerate(pdf_doc, start=1):
            if include_pages is not None and page_num not in include_pages:
                if page_logs is not None:
                    page_logs.append(
                        {
                            "document_id": document_id,
                            "source": source,
                            "physical_page": page_num,
                            "status": "excluded",
                            "reason": excluded_map.get(page_num, "excluded_by_manifest"),
                        }
                    )
                continue

            norm_text, raw_chars = _extract_page_blocks(pdf_page, join_soft_hyphens=join_soft_hyphens)
            norm_chars = len(norm_text)

            if norm_chars == 0:
                if page_logs is not None:
                    page_logs.append(
                        {
                            "document_id": document_id,
                            "source": source,
                            "physical_page": page_num,
                            "status": "empty_or_unreadable",
                            "reason": "no_extractable_text_ocr_needed",
                            "raw_chars": raw_chars,
                            "normalized_chars": 0,
                        }
                    )
                continue

            page_meta = dict(shared_meta)
            if page_num in sections_map:
                page_meta["section"] = sections_map[page_num]
            page_meta["physical_page"] = page_num
            page_meta["raw_chars"] = raw_chars
            page_meta["normalized_chars"] = norm_chars

            pages.append(
                DocumentPage(
                    document_id=document_id,
                    source=source,
                    page=page_num,
                    text=norm_text,
                    metadata=page_meta,
                )
            )

            if page_logs is not None:
                page_logs.append(
                    {
                        "document_id": document_id,
                        "source": source,
                        "physical_page": page_num,
                        "page_id": f"{document_id}-p{page_num:03d}",
                        "status": "included",
                        "section": page_meta.get("section"),
                        "raw_chars": raw_chars,
                        "normalized_chars": norm_chars,
                    }
                )

    return pages


def ingest_corpus_from_manifest(
    manifest_path: Path,
    *,
    repo_root: Path | None = None,
    log_output_path: Path | None = None,
) -> tuple[list[DocumentPage], dict[str, Any]]:
    """Load all documents defined in manifest.json and optionally persist the ingestion log."""
    root = repo_root or manifest_path.resolve().parents[1]
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))

    t0 = time.perf_counter()
    all_pages: list[DocumentPage] = []
    page_logs: list[dict[str, Any]] = []
    doc_summaries: list[dict[str, Any]] = []

    for doc_entry in manifest["documents"]:
        source_rel = doc_entry["source_path_or_url"]
        pdf_path = root / source_rel
        include_set = set(doc_entry["included_pages"])
        exclude_map = {item["page"]: item["reason"] for item in doc_entry.get("excluded_pages", [])}
        sec_map = {
            page_num: sr["section"]
            for sr in doc_entry.get("section_ranges", [])
            for page_num in sr["pages"]
        }
        base_meta = {
            "year": doc_entry["year"],
            "document_type": doc_entry["document_type"],
        }
        join_soft = doc_entry.get("join_soft_hyphens")
        if join_soft is None:
            join_soft = doc_entry["source_id"] == "nist-ai-100-1"

        doc_pages = load_pdf(
            pdf_path,
            document_id=doc_entry["source_id"],
            source=source_rel,
            include_pages=include_set,
            exclude_reasons=exclude_map,
            join_soft_hyphens=join_soft,
            base_metadata=base_meta,
            page_sections=sec_map,
            page_logs=page_logs,
        )
        all_pages.extend(doc_pages)
        doc_summaries.append(
            {
                "source_id": doc_entry["source_id"],
                "source_path": source_rel,
                "physical_pages": doc_entry["physical_pages"],
                "included_pages": len(doc_pages),
                "excluded_pages": len(exclude_map),
                "normalized_chars": sum(len(p.text) for p in doc_pages),
            }
        )

    elapsed = round(time.perf_counter() - t0, 3)
    included_count = sum(1 for row in page_logs if row["status"] == "included")
    excluded_count = sum(1 for row in page_logs if row["status"] == "excluded")
    empty_count = sum(1 for row in page_logs if row["status"] == "empty_or_unreadable")

    log_payload: dict[str, Any] = {
        "manifest_path": str(manifest_path),
        "pdf_parse_seconds": elapsed,
        "included_pages": included_count,
        "excluded_pages": excluded_count,
        "empty_or_unreadable_pages": empty_count,
        "total_normalized_chars": sum(len(p.text) for p in all_pages),
        "documents": doc_summaries,
        "pages": page_logs,
    }

    if log_output_path is not None:
        out_path = Path(log_output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(log_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return all_pages, log_payload
