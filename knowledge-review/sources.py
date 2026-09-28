"""Read Markdown into immutable, version-specific source snapshots."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


@dataclass(frozen=True)
class Source:
    source_id: str
    path: Path
    text: str


def read_note(path: Path) -> str:
    if path.suffix.lower() != ".md":
        raise ValueError("Please choose a Markdown (.md) file.")
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise ValueError(f"The note is empty: {path}")
    return text


def add_line_numbers(text: str) -> str:
    return "\n".join(f"[L{number}] {line}" for number, line in enumerate(text.splitlines(), start=1))


def load_sources(paths: list[Path]) -> list[Source]:
    if not paths:
        raise ValueError("Please choose at least one Markdown file.")
    sources = []
    seen = set()
    for path in paths:
        path = path.resolve()
        if path in seen:
            continue
        text = read_note(path)
        source_id = sha256((str(path) + "\0" + text).encode("utf-8")).hexdigest()
        sources.append(Source(source_id, path, text))
        seen.add(path)
    return sources


def format_sources(sources: list[Source]) -> str:
    return "\n\n".join(
        f"Source ID: {source.source_id}\nFile: {source.path.name}\n{add_line_numbers(source.text)}"
        for source in sources
    )
