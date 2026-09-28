"""Bind retrieved excerpts to source snapshots and the actual visible line ranges."""

from chunking import Chunk, section_label
from sources import Source


def validate_chunks(chunks: list[Chunk], sources: list[Source]) -> None:
    by_id = {source.source_id: source for source in sources}
    for chunk in chunks:
        source = by_id.get(chunk.source_id)
        if source is None:
            raise ValueError("Retrieved chunk has an unknown source version.")
        lines = source.text.splitlines()
        if not 1 <= chunk.line_start <= chunk.line_end <= len(lines):
            raise ValueError("Retrieved chunk has an invalid source line range.")
        original = "\n".join(lines[chunk.line_start - 1:chunk.line_end])
        if chunk.text != original:
            raise ValueError("Retrieved chunk text does not match the source snapshot.")


def format_context(chunks: list[Chunk], sources: list[Source]) -> str:
    validate_chunks(chunks, sources)
    by_id = {source.source_id: source for source in sources}
    excerpts = []
    for chunk in chunks:
        numbered = "\n".join(
            f"[L{number}] {line}"
            for number, line in enumerate(chunk.text.splitlines(), chunk.line_start)
        )
        excerpts.append(
            f"Source ID: {chunk.source_id}\nFile: {by_id[chunk.source_id].path.name}\n"
            f"Section: {section_label(chunk)}\n"
            f"Visible lines: L{chunk.line_start}-L{chunk.line_end}\n{numbered}"
        )
    return "\n\n".join(excerpts) if excerpts else "(No retrieved excerpts.)"


def citation_is_visible(source_id: str, start: int, end: int, chunks: list[Chunk]) -> bool:
    """Every cited line must belong to the union of supplied ranges, with no gaps."""
    cursor = start
    ranges = sorted((c.line_start, c.line_end) for c in chunks if c.source_id == source_id)
    for left, right in ranges:
        if right < cursor:
            continue
        if left > cursor:
            return False
        cursor = right + 1
        if cursor > end:
            return True
    return False
