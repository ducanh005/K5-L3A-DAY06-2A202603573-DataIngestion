"""Implement fixed-size and structure-aware chunking with full source traceability."""
from __future__ import annotations

import re

from .models import Chunk, DocumentPage

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+")

# Short headings/IDs that should stay attached to the following content block.
_FORWARD_ATTACH_RE = re.compile(
    r"^(?:"
    r"Action ID Suggested Action GAI Risks"
    r"|Categories Subcategories"
    r"|Table \d+:.*"
    r"|(?:GV|MP|MS|MG)-\d+\.\d+-\d+"
    r"|(?:GOVERN|MAP|MEASURE|MANAGE)\s+\d+(?:\.\d+)?\s*:.*"
    r"|(?:\d+(?:\.\d+)?\.?\s+[A-Z].*)"
    r"|Appendix [A-D]\b.*"
    r"|Part \d+:.*"
    r"|Executive Summary"
    r")$"
)

# Short footer/role lines that belong to the block right before them.
_BACKWARD_ATTACH_RE = re.compile(
    r"^(?:AI Actor(?:\s+Tasks|s)?:.*|Trustworthy AI Characteristics:.*|Continued on next page)$"
)

# Detect mid-page section transitions so chunks retain accurate section filter metadata.
_SECTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(?:5\.1\s+Govern\b|GOVERN\s+\d|GV-\d|Table 1:.*\bGOVERN function\b)"), "govern"),
    (re.compile(r"^(?:5\.2\s+Map\b|MAP\s+\d|MP-\d|Table 2:.*\bMAP function\b)"), "map"),
    (re.compile(r"^(?:5\.3\s+Measure\b|MEASURE\s+\d|MS-\d|Table 3:.*\bMEASURE function\b)"), "measure"),
    (re.compile(r"^(?:5\.4\s+Manage\b|MANAGE\s+\d|MG-\d|Table 4:.*\bMANAGE function\b)"), "manage"),
    (re.compile(r"^6\.\s+AI RMF Profiles\b"), "profiles"),
    (re.compile(r"^4\.\s+Effectiveness\b"), "effectiveness"),
    (re.compile(r"^Appendix A\b"), "appendix_a"),
    (re.compile(r"^Appendix B\b"), "appendix_b"),
    (re.compile(r"^Appendix C\b"), "appendix_c"),
    (re.compile(r"^Appendix D\b"), "appendix_d"),
    (re.compile(r"^3\.\s+AI Risks and Trustworthiness\b"), "ai_risks_and_trustworthiness"),
    (re.compile(r"^2\.\s+Audience\b"), "audience"),
    (re.compile(r"^1\.\s+Framing Risk\b"), "framing_risk"),
)


def _detect_block_section(block: str) -> str | None:
    """Return updated canonical section tag if block begins a new section or function."""
    stripped = block.strip()
    for pattern, section_name in _SECTION_PATTERNS:
        if pattern.match(stripped):
            return section_name
    return None


def _split_oversized_block(text: str, max_size: int) -> list[str]:
    """Split an oversized block cleanly along sentence boundaries, or words as fallback."""
    if len(text) <= max_size:
        return [text]

    sentences = [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]
    units: list[str] = []
    for sentence in sentences:
        if len(sentence) <= max_size:
            units.append(sentence)
            continue
        words = sentence.split()
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if len(candidate) <= max_size:
                current = candidate
            else:
                if current:
                    units.append(current)
                current = word
        if current:
            units.append(current)

    packed: list[str] = []
    buf = ""
    for unit in units:
        candidate = f"{buf} {unit}".strip()
        if len(candidate) <= max_size:
            buf = candidate
        else:
            if buf:
                packed.append(buf)
            buf = unit
    if buf:
        packed.append(buf)
    return packed


def fixed_size(
    page: DocumentPage,
    size: int = 500,
    overlap: int = 50,
    *,
    strategy_prefix: str = "A",
) -> list[Chunk]:
    """Split normalized page text into overlapping fixed-size character windows."""
    if size <= 0:
        raise ValueError("Chunk size must be positive")
    if overlap < 0 or overlap >= size:
        raise ValueError("Overlap must satisfy 0 <= overlap < size")

    clean_text = " ".join(page.text.split())
    if not clean_text:
        return []

    chunks: list[Chunk] = []
    step = size - overlap
    total_len = len(clean_text)
    start = 0
    chunk_idx = 0

    while start < total_len:
        end = min(start + size, total_len)
        raw_window = clean_text[start:end]
        window = raw_window.strip()
        if window:
            lead_ws = len(raw_window) - len(raw_window.lstrip())
            trail_ws = len(raw_window) - len(raw_window.rstrip())
            actual_start = start + lead_ws
            actual_end = end - trail_ws
            chunk_idx += 1
            meta = dict(page.metadata)
            meta.update(
                {
                    "strategy": strategy_prefix,
                    "chunk_index": chunk_idx,
                    "char_start": actual_start,
                    "char_end": actual_end,
                    "char_len": len(window),
                }
            )
            chunks.append(
                Chunk(
                    chunk_id=f"{strategy_prefix}:{page.document_id}:p{page.page:03d}:c{chunk_idx:02d}",
                    document_id=page.document_id,
                    source=page.source,
                    page=page.page,
                    text=window,
                    metadata=meta,
                )
            )
        if end >= total_len:
            break
        start += step

    return chunks


def _group_semantic_units(
    blocks: list[str],
    initial_section: str | None,
    max_size: int,
) -> list[tuple[str, str | None]]:
    """Tag blocks with dynamic section names and keep headers/footnotes attached to neighbors."""
    tagged: list[tuple[str, str | None]] = []
    current_section = initial_section

    for block in blocks:
        detected = _detect_block_section(block)
        if detected is not None:
            current_section = detected
        if tagged and _BACKWARD_ATTACH_RE.match(block):
            prev_text, prev_sec = tagged[-1]
            if prev_sec == current_section and len(prev_text) + 2 + len(block) <= max_size:
                tagged[-1] = (f"{prev_text}\n\n{block}", prev_sec)
                continue
        tagged.append((block, current_section))

    merged_units: list[tuple[str, str | None]] = []
    pending_header = ""
    pending_section = initial_section

    for block_text, block_sec in tagged:
        is_short_header = (
            len(block_text) <= 180
            and _FORWARD_ATTACH_RE.match(block_text) is not None
        )
        if is_short_header:
            if pending_header and pending_section == block_sec and len(pending_header) + 2 + len(block_text) <= max_size:
                pending_header = f"{pending_header}\n\n{block_text}"
            elif pending_header:
                for piece in _split_oversized_block(pending_header, max_size):
                    merged_units.append((piece, pending_section))
                pending_header = block_text
                pending_section = block_sec
            else:
                pending_header = block_text
                pending_section = block_sec
            continue

        combined_text = block_text
        if pending_header:
            if pending_section == block_sec:
                combined_text = f"{pending_header}\n\n{block_text}"
            else:
                for piece in _split_oversized_block(pending_header, max_size):
                    merged_units.append((piece, pending_section))
            pending_header = ""

        for piece in _split_oversized_block(combined_text, max_size):
            merged_units.append((piece, block_sec))

    if pending_header:
        for piece in _split_oversized_block(pending_header, max_size):
            merged_units.append((piece, pending_section))

    return merged_units


def by_paragraph(
    page: DocumentPage,
    max_size: int = 900,
    *,
    strategy_prefix: str = "B",
) -> list[Chunk]:
    """Group paragraphs and headings up to max_size while preserving section boundaries."""
    if max_size <= 0:
        raise ValueError("max_size must be positive")

    raw_blocks = [block.strip() for block in page.text.split("\n\n") if block.strip()]
    if not raw_blocks:
        return []

    initial_section = page.metadata.get("section")
    atomic_units = _group_semantic_units(raw_blocks, initial_section, max_size)

    packed_chunks: list[tuple[str, str | None]] = []
    current_text = ""
    current_section: str | None = None

    for unit_text, unit_sec in atomic_units:
        if not current_text:
            current_text = unit_text
            current_section = unit_sec
            continue
        candidate = f"{current_text}\n\n{unit_text}"
        if unit_sec == current_section and len(candidate) <= max_size:
            current_text = candidate
        else:
            packed_chunks.append((current_text, current_section))
            current_text = unit_text
            current_section = unit_sec

    if current_text:
        packed_chunks.append((current_text, current_section))

    chunks: list[Chunk] = []
    for idx, (chunk_text, chunk_sec) in enumerate(packed_chunks, start=1):
        meta = dict(page.metadata)
        if chunk_sec is not None:
            meta["section"] = chunk_sec
        meta.update(
            {
                "strategy": strategy_prefix,
                "chunk_index": idx,
                "char_len": len(chunk_text),
            }
        )
        chunks.append(
            Chunk(
                chunk_id=f"{strategy_prefix}:{page.document_id}:p{page.page:03d}:c{idx:02d}",
                document_id=page.document_id,
                source=page.source,
                page=page.page,
                text=chunk_text,
                metadata=meta,
            )
        )

    return chunks
