"""Split Markdown at block boundaries while preserving original line positions."""

from dataclasses import dataclass
from hashlib import sha256

from markdown_it import MarkdownIt

from sources import Source

CHUNK_ALGORITHM = "markdown-blocks-v2"


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    source_id: str
    heading_path: tuple[str, ...]
    line_start: int
    line_end: int
    text: str
    section_paths: tuple[tuple[str, ...], ...] = ()


def section_label(chunk: Chunk) -> str:
    return " / ".join(
        " > ".join(path) or "(no heading)"
        for path in (chunk.section_paths or (chunk.heading_path,))
    )


def chunk_source(source: Source, max_chars: int = 1200, min_chars: int = 100) -> list[Chunk]:
    """Use a soft character budget: an individual Markdown block stays whole."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive.")
    if not 0 <= min_chars <= max_chars:
        raise ValueError("min_chars must be between 0 and max_chars.")
    lines = source.text.splitlines()
    tokens = MarkdownIt("commonmark").enable("table").parse(source.text)
    # Only outer blocks: a heading inside a quote/list is part of that block.
    starts = {0}
    headings = {}
    for index, token in enumerate(tokens):
        if token.level != 0 or token.map is None:
            continue
        starts.add(token.map[0])
        if token.type == "heading_open":
            headings[token.map[0]] = (int(token.tag[1:]), tokens[index + 1].content)
    boundaries = sorted(starts | {len(lines)})
    chunks = []
    hierarchy = []
    pending_start = None
    pending_end = 0
    has_body = False

    def emit():
        if pending_start is None:
            return
        start, end = pending_start, pending_end
        # Trim boundary whitespace, keeping all original nonblank lines intact.
        while start < end and not lines[start].strip():
            start += 1
        while end > start and not lines[end - 1].strip():
            end -= 1
        if start == end:
            return
        identity = f"{source.source_id}:{CHUNK_ALGORITHM}:{start + 1}:{end}"
        chunks.append(Chunk(
            sha256(identity.encode("utf-8")).hexdigest(), source.source_id,
            tuple(title for _, title in hierarchy), start + 1, end,
            "\n".join(lines[start:end]), (tuple(title for _, title in hierarchy),),
        ))

    for start, end in zip(boundaries, boundaries[1:]):
        if start in headings:
            emit()
            pending_start = None
            has_body = False
            level, title = headings[start]
            while hierarchy and hierarchy[-1][0] >= level:
                hierarchy.pop()
            hierarchy.append((level, title))
        elif pending_start is not None and has_body:
            candidate = "\n".join(lines[pending_start:end]).strip("\n")
            if len(candidate) > max_chars:
                emit()
                pending_start = None
        if pending_start is None:
            pending_start = start
        pending_end = end
        if start not in headings and any(line.strip() for line in lines[start:end]):
            has_body = True
    emit()
    # Greedy adjacent merging: either side being small is enough, but never
    # exceed max_chars for a merge. A final small tail may merge backwards.
    merged = []
    for chunk in chunks:
        if merged and (len(merged[-1].text) < min_chars or len(chunk.text) < min_chars):
            previous = merged[-1]
            text = "\n".join(lines[previous.line_start - 1:chunk.line_end])
            if len(text) <= max_chars:
                paths = tuple(dict.fromkeys(previous.section_paths + chunk.section_paths))
                common = list(paths[0])
                for path in paths[1:]:
                    while tuple(common) != path[:len(common)]:
                        common.pop()
                identity = f"{source.source_id}:{CHUNK_ALGORITHM}:{previous.line_start}:{chunk.line_end}"
                merged[-1] = Chunk(
                    sha256(identity.encode("utf-8")).hexdigest(), source.source_id,
                    tuple(common), previous.line_start, chunk.line_end, text, paths,
                )
                continue
        merged.append(chunk)
    return merged


def format_chunks(chunks: list[Chunk], sources: list[Source], max_chars: int) -> str:
    paths = {source.source_id: source.path for source in sources}
    previews = []
    for chunk in chunks:
        oversized = " | exceeds soft limit" if len(chunk.text) > max_chars else ""
        numbered = "\n".join(
            f"[L{number}] {line}"
            for number, line in enumerate(chunk.text.splitlines(), chunk.line_start)
        )
        previews.append(
            f"Chunk: {chunk.chunk_id[:12]} | File: {paths[chunk.source_id]}\n"
            f"Section: {section_label(chunk)}\n"
            f"Lines: L{chunk.line_start}-L{chunk.line_end} | {len(chunk.text)} chars{oversized}\n"
            f"{numbered}"
        )
    return "\n\n".join(previews)
