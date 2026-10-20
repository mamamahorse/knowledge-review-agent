"""Split Markdown at block boundaries while preserving original line positions."""

from dataclasses import dataclass, replace
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
    block_id: str | None = None
    kind: str = 'text'
    context_ranges: tuple[tuple[int, int], ...] = ()
    retrieval_context: str = ''
    atomic: bool = False
    table_header_range: tuple[int, int] | None = None


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
    if source.document and source.document.format == 'pdf':
        return document_chunks(source, max_chars)
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
    return protect_tables(merged, source)


def block_chunk(source, block, start=None, end=None, *, lines=None):
    start = block.line_start if start is None else start
    end = block.line_end if end is None else end
    lines = source.text.splitlines() if lines is None else lines
    context = tuple((a, b) for a, b in block.context_ranges if not (start <= a and end >= b))
    header = ''
    if block.header_end is not None:
        header = '\n'.join(lines[block.line_start - 1:block.header_end])
        if start > block.line_start or end < block.header_end:
            context = (*context, (block.line_start, block.header_end))
    if block.context_ranges:
        header = '\n'.join(['\n'.join(lines[a - 1:b]) for a, b in block.context_ranges] + [header])
    identity = f'{source.source_id}:structured-v1:{start}:{end}:{block.block_id}'
    if block.context_ranges:
        identity += ':context-v1:' + str(block.context_ranges)
    return Chunk(sha256(identity.encode()).hexdigest(),
        source.source_id, block.heading_path, start, end, '\n'.join(lines[start - 1:end]),
        (block.heading_path,), block.block_id, block.kind, context, header,
        block.kind in ('table', 'figure', 'formula'),
        (block.line_start, block.header_end) if block.header_end is not None else None)


def document_chunks(source, max_chars=1200):
    chunks = []
    lines = source.text.splitlines()
    for block in source.document.blocks:
        if block.kind == 'page':
            continue
        chunk = block_chunk(source, block, lines=lines)
        # Structured objects remain parents; their retrieval windows are prepared later.
        if chunk.atomic or len(chunk.text) <= max_chars:
            chunks.append(chunk)
            continue
        start, size = block.line_start, 0
        for line in range(start, block.line_end + 1):
            if size and size + len(lines[line - 1]) > max_chars:
                chunks.append(block_chunk(source, block, start, line - 1, lines=lines))
                start, size = line, 0
            size += len(lines[line - 1]) + 1
        chunks.append(block_chunk(source, block, start, block.line_end, lines=lines))
    return chunks


def protect_tables(chunks, source):
    tables = [b for b in source.document.blocks if b.kind == 'table'] if source.document else []
    if not tables:
        return chunks
    lines, result = source.text.splitlines(), []
    for chunk in chunks:
        tables_here = [b for b in tables if b.line_start <= chunk.line_end and b.line_end >= chunk.line_start]
        start = chunk.line_start
        for table in tables_here:
            if start < table.line_start:
                end = table.line_start - 1
                result.append(replace(chunk, line_start=start, line_end=end,
                    text='\n'.join(lines[start - 1:end]),
                    chunk_id=sha256(f'{chunk.chunk_id}:before-table:{start}:{end}'.encode()).hexdigest()))
            result.append(replace(block_chunk(source, table, lines=lines), heading_path=chunk.heading_path,
                                  section_paths=chunk.section_paths))
            start = table.line_end + 1
        if start <= chunk.line_end:
            if not tables_here:
                result.append(chunk)
            else:
                result.append(replace(chunk, line_start=start, text='\n'.join(lines[start - 1:chunk.line_end]),
                    chunk_id=sha256(f'{chunk.chunk_id}:after-table:{start}'.encode()).hexdigest()))
    return result


def with_table_context(chunks, sources):
    """Real header excerpts get separate ranges; never invent a continuous citation."""
    by_id = {s.source_id: s for s in sources}
    result, seen = [], set()
    for chunk in chunks:
        for start, end in chunk.context_ranges:
            key = (chunk.source_id, start, end)
            if key not in seen:
                source = by_id[chunk.source_id]
                result.append(Chunk(sha256(f'{key}:table-header-v1'.encode()).hexdigest(), chunk.source_id,
                    chunk.heading_path, start, end, '\n'.join(source.text.splitlines()[start - 1:end]),
                    chunk.section_paths, chunk.block_id,
                    'table_header' if (start, end) == chunk.table_header_range else 'table_context', atomic=True))
                seen.add(key)
        key = (chunk.source_id, chunk.line_start, chunk.line_end)
        if key not in seen:
            result.append(chunk)
            seen.add(key)
    return result


def protect_selection(chunk, source):
    tables = [b for b in source.document.blocks if b.kind == 'table'
              and b.line_start <= chunk.line_end and b.line_end >= chunk.line_start] if source.document else []
    if not tables:
        return [chunk]
    ranges = []
    for block in tables:
        related = list(block.context_ranges)
        if block.header_end:
            related.append((block.line_start, block.header_end))
        for start, end in related:
            if not (chunk.line_start <= start and chunk.line_end >= end) and (start, end) not in ranges:
                ranges.append((start, end))
    return with_table_context([replace(chunk, kind='table', atomic=True, context_ranges=tuple(ranges))], [source])


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
