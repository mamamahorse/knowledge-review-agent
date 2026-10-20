"""Read Markdown into immutable, version-specific source snapshots."""

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from document_model import Document, markdown_document


@dataclass(frozen=True)
class Source:
    source_id: str
    path: Path
    text: str
    document: Document | None = field(default=None, compare=False, repr=False)

    def __post_init__(self):
        if self.document is None and self.path.suffix.lower() == '.md':
            object.__setattr__(self, 'document', markdown_document(self.text))
        if self.document is not None and self.document.text != self.text:
            raise ValueError('文档结构与文本快照不一致。')


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
        if path.suffix.lower() == '.pdf':
            from pdf_parser import load_pdf
            document = load_pdf(path)
            sources.append(source_from_document(path, document))
        else:
            text = read_note(path)
            source_id = sha256((str(path) + "\0" + text).encode("utf-8")).hexdigest()
            sources.append(Source(source_id, path, text))
        seen.add(path)
    return sources


def source_from_document(path: Path, document: Document) -> Source:
    path = path.resolve()
    identity = sha256((str(path) + '\0pdf\0' + document.parse_id).encode('utf-8')).hexdigest()
    return Source(identity, path, document.text, document)


def format_sources(sources: list[Source]) -> str:
    return "\n\n".join(
        f"Source ID: {source.source_id}\nFile: {source.path.name}\n" + (
            f"PDF extracted text view; {source.document.page_count} physical pages; parsing status={source.document.status}; issues={source.document.issues}. Line numbers locate extracted text, not original PDF lines. Figure asset labels are navigation only, not descriptions of image contents.\n"
            if source.document and source.document.format == 'pdf' else '') + add_line_numbers(source.text)
        for source in sources
    )
