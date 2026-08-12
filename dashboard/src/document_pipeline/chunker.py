"""Markdown-aware document chunking.

Splits Docling markdown output into chunks that respect document structure:
headings, tables, and list items are never split mid-element.

Chunk overlap ensures context continuity at boundaries.
"""

import re
from dataclasses import dataclass, field


@dataclass
class Chunk:
    """A single chunk of document text with positional metadata."""

    text: str
    index: int
    char_start: int
    char_end: int
    heading: str = ""
    page_hint: int | None = None


@dataclass
class ChunkResult:
    """Output of chunking a single document."""

    src_id: int
    chunks: list[Chunk] = field(default_factory=list)
    total_chars: int = 0


# Patterns for structural boundaries (best to worst split points)
_HEADING_RE = re.compile(r"^(#{1,4})\s", re.MULTILINE)
_TABLE_START_RE = re.compile(r"^\|", re.MULTILINE)
_BLANK_LINE_RE = re.compile(r"\n\n+")


def chunk_document(
    full_text: str,
    src_id: int = 0,
    target_size: int = 2048,
    overlap: int = 200,
    min_chunk_size: int = 100,
) -> ChunkResult:
    """Split markdown text into overlapping chunks respecting structure.

    Strategy:
    1. Split into "blocks" at heading boundaries and double-newlines.
    2. Merge small blocks until they reach target_size.
    3. Add overlap from the previous chunk's tail.
    4. Track the current heading context for each chunk.
    """
    if not full_text or len(full_text) < min_chunk_size:
        if full_text and full_text.strip():
            return ChunkResult(
                src_id=src_id,
                chunks=[Chunk(text=full_text.strip(), index=0, char_start=0, char_end=len(full_text))],
                total_chars=len(full_text),
            )
        return ChunkResult(src_id=src_id, total_chars=0)

    blocks = _split_into_blocks(full_text)
    chunks = _merge_blocks_into_chunks(blocks, target_size, overlap, min_chunk_size)

    for i, chunk in enumerate(chunks):
        chunk.index = i

    return ChunkResult(src_id=src_id, chunks=chunks, total_chars=len(full_text))


def _split_into_blocks(text: str) -> list[dict]:
    """Split text into structural blocks at headings and paragraph breaks.

    Each block is a dict with 'text', 'char_start', 'is_table', and 'heading'.
    Tables are kept as atomic blocks to avoid splitting mid-row.
    """
    blocks: list[dict] = []
    current_heading = ""
    pos = 0

    lines = text.split("\n")
    current_block_lines: list[str] = []
    block_start = 0
    in_table = False

    for line in lines:
        line_len = len(line) + 1  # +1 for the \n we split on

        heading_match = _HEADING_RE.match(line)
        is_table_line = line.startswith("|")
        is_blank = not line.strip()

        if heading_match:
            # Flush current block before starting new heading section
            if current_block_lines:
                block_text = "\n".join(current_block_lines)
                if block_text.strip():
                    blocks.append({
                        "text": block_text,
                        "char_start": block_start,
                        "is_table": in_table,
                        "heading": current_heading,
                    })
                current_block_lines = []
                block_start = pos
                in_table = False
            current_heading = line.strip()

        elif in_table and not is_table_line and not line.strip().startswith("|"):
            # End of table — flush table block
            if current_block_lines:
                block_text = "\n".join(current_block_lines)
                if block_text.strip():
                    blocks.append({
                        "text": block_text,
                        "char_start": block_start,
                        "is_table": True,
                        "heading": current_heading,
                    })
                current_block_lines = []
                block_start = pos
                in_table = False

        elif is_blank and not in_table and current_block_lines:
            # Paragraph break outside a table
            block_text = "\n".join(current_block_lines)
            if block_text.strip():
                blocks.append({
                    "text": block_text,
                    "char_start": block_start,
                    "is_table": False,
                    "heading": current_heading,
                })
            current_block_lines = []
            block_start = pos + line_len
            in_table = False
            pos += line_len
            continue

        if is_table_line:
            in_table = True

        current_block_lines.append(line)
        pos += line_len

    # Flush remaining
    if current_block_lines:
        block_text = "\n".join(current_block_lines)
        if block_text.strip():
            blocks.append({
                "text": block_text,
                "char_start": block_start,
                "is_table": in_table,
                "heading": current_heading,
            })

    return blocks


def _merge_blocks_into_chunks(
    blocks: list[dict],
    target_size: int,
    overlap: int,
    min_chunk_size: int,
) -> list[Chunk]:
    """Greedily merge small blocks into chunks up to target_size.

    Oversized atomic blocks (large tables) become their own chunk.
    Overlap text is prepended from the previous chunk's tail.
    """
    if not blocks:
        return []

    chunks: list[Chunk] = []
    current_texts: list[str] = []
    current_size = 0
    current_start = blocks[0]["char_start"]
    current_heading = blocks[0].get("heading", "")
    prev_tail = ""

    for block in blocks:
        block_text = block["text"]
        block_len = len(block_text)

        # Oversized atomic block (e.g. large table) — flush current, emit standalone
        if block_len > target_size and block["is_table"]:
            if current_texts:
                chunks.append(_make_chunk(
                    current_texts, current_start, current_heading, prev_tail, overlap,
                ))
                prev_tail = "\n".join(current_texts)[-overlap:] if overlap else ""
                current_texts = []
                current_size = 0

            chunks.append(Chunk(
                text=block_text,
                index=0,
                char_start=block["char_start"],
                char_end=block["char_start"] + block_len,
                heading=block.get("heading", current_heading),
            ))
            prev_tail = block_text[-overlap:] if overlap else ""
            current_start = block["char_start"] + block_len
            current_heading = block.get("heading", current_heading)
            continue

        # Would exceed target — flush current chunk first
        if current_size + block_len > target_size and current_texts:
            chunks.append(_make_chunk(
                current_texts, current_start, current_heading, prev_tail, overlap,
            ))
            prev_tail = "\n".join(current_texts)[-overlap:] if overlap else ""
            current_texts = []
            current_size = 0
            current_start = block["char_start"]

        if block.get("heading"):
            current_heading = block["heading"]

        current_texts.append(block_text)
        current_size += block_len

    # Flush remaining
    if current_texts:
        merged = "\n\n".join(current_texts)
        if len(merged.strip()) >= min_chunk_size:
            chunks.append(_make_chunk(
                current_texts, current_start, current_heading, prev_tail, overlap,
            ))
        elif chunks:
            # Too small — append to previous chunk
            chunks[-1].text += "\n\n" + merged
            chunks[-1].char_end = current_start + len(merged)

    return chunks


def _make_chunk(
    texts: list[str],
    char_start: int,
    heading: str,
    prev_tail: str,
    overlap: int,
) -> Chunk:
    """Create a Chunk, optionally prepending overlap from the previous chunk."""
    body = "\n\n".join(texts)
    if overlap and prev_tail:
        text = prev_tail + "\n\n" + body
    else:
        text = body

    return Chunk(
        text=text,
        index=0,
        char_start=char_start,
        char_end=char_start + len(body),
        heading=heading,
    )
