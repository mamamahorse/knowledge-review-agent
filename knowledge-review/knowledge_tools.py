"""Safe, reusable knowledge operations for the future Agent loop."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from chunking import Chunk, chunk_source, section_label
from embedding_config import EmbeddingConfig
from embeddings import LocalEmbedder
from retrieval import SearchHit, rank_chunks
from sources import Source
from vector_index import get_document_vectors

MAX_SEARCH_RESULTS = 10
MAX_READ_LINES = 200


@dataclass(frozen=True)
class SearchResult:
    hits: list[SearchHit]
    reused: int
    computed: int
    reused_in_memory: bool
    dimensions: int
    model_name: str
    max_tokens: int

    def as_dict(self, sources: list[Source]) -> dict:
        paths = {source.source_id: source.path.name for source in sources}
        return {
            "hits": [{
                "score": hit.score,
                "chunk_id": hit.chunk.chunk_id,
                "source_id": hit.chunk.source_id,
                "file": paths[hit.chunk.source_id],
                "section": section_label(hit.chunk),
                "line_start": hit.chunk.line_start,
                "line_end": hit.chunk.line_end,
                "text": hit.chunk.text,
            } for hit in self.hits],
            "notice": "Similarity ranks candidates; it is not an answer confidence score.",
        }


@dataclass(frozen=True)
class ReadResult:
    source: Source
    chunk: Chunk

    def as_dict(self) -> dict:
        return {
            "source_id": self.source.source_id,
            "file": self.source.path.name,
            "line_start": self.chunk.line_start,
            "line_end": self.chunk.line_end,
            "text": self.chunk.text,
        }


@dataclass(frozen=True)
class ToolExecution:
    output: dict
    visible_chunks: tuple[Chunk, ...]


class KnowledgeTools:
    """Search and read only the immutable source snapshots supplied at construction."""

    def __init__(
        self, sources: list[Source], embedding_config: EmbeddingConfig, *,
        max_chars: int = 1200, min_chars: int = 100,
        no_cache: bool = False, rebuild_index: bool = False,
        embedder: LocalEmbedder | None = None, index_path: Path | None = None,
    ):
        if not sources:
            raise ValueError("Knowledge tools require at least one source.")
        if max_chars <= 0 or not 0 <= min_chars <= max_chars:
            raise ValueError("Chunk limits must satisfy 0 <= min_chars <= max_chars.")
        self.sources = list(sources)
        self.embedding_config = embedding_config
        self.max_chars = max_chars
        self.min_chars = min_chars
        self.no_cache = no_cache
        self.rebuild_index = rebuild_index
        self.index_path = index_path
        self.chunks = [
            chunk for source in self.sources
            for chunk in chunk_source(source, max_chars, min_chars)
        ]
        self._embedder = embedder
        self._vectors = None

    @property
    def embedder(self) -> LocalEmbedder:
        if self._embedder is None:
            self._embedder = LocalEmbedder(self.embedding_config)
        return self._embedder

    def search_notes(self, query: str, top_k: int = 3) -> SearchResult:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("search_notes query must not be blank.")
        if type(top_k) is not int or not 1 <= top_k <= MAX_SEARCH_RESULTS:
            raise ValueError(f"search_notes top_k must be between 1 and {MAX_SEARCH_RESULTS}.")
        query_vector = self.embedder.embed_query(query)
        memory_hit = self._vectors is not None
        if memory_hit:
            vectors = self._vectors
            reused, computed = len(self.chunks), 0
        elif self.no_cache:
            vectors = self.embedder.embed_chunks(self.chunks)
            reused, computed = 0, len(self.chunks)
            self._vectors = vectors
        else:
            indexed = get_document_vectors(
                self.chunks, self.embedder, len(query_vector),
                max_chars=self.max_chars, min_chars=self.min_chars,
                path=self.index_path, rebuild=self.rebuild_index,
            )
            vectors = indexed.vectors
            reused, computed = indexed.reused, indexed.computed
            self._vectors = vectors
        hits = rank_chunks(self.chunks, vectors, query_vector, top_k)
        return SearchResult(
            hits, reused, computed, memory_hit, len(query_vector),
            self.embedding_config.model_name, self.embedder.max_tokens,
        )

    def read_source(self, source_id: str, line_start: int, line_end: int) -> ReadResult:
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("read_source requires an exact source_id.")
        if type(line_start) is not int or type(line_end) is not int:
            raise ValueError("read_source line numbers must be integers.")
        source = next((item for item in self.sources if item.source_id == source_id), None)
        if source is None:
            raise ValueError("read_source can only access a currently loaded source version.")
        lines = source.text.splitlines()
        if not 1 <= line_start <= line_end <= len(lines):
            raise ValueError("read_source line range is outside the source.")
        if line_end - line_start + 1 > MAX_READ_LINES:
            raise ValueError(f"read_source can return at most {MAX_READ_LINES} lines per call.")
        text = "\n".join(lines[line_start - 1:line_end])
        paths = []
        for chunk in self.chunks:
            if chunk.source_id != source_id or chunk.line_end < line_start or chunk.line_start > line_end:
                continue
            for path in chunk.section_paths or (chunk.heading_path,):
                if path not in paths:
                    paths.append(path)
        common = list(paths[0]) if paths else []
        for path in paths[1:]:
            while tuple(common) != path[:len(common)]:
                common.pop()
        identity = f"{source_id}:read-source-v1:{line_start}:{line_end}"
        chunk = Chunk(
            sha256(identity.encode("utf-8")).hexdigest(), source_id, tuple(common),
            line_start, line_end, text, tuple(paths),
        )
        return ReadResult(source, chunk)

    def execute(self, name: str, arguments: dict) -> ToolExecution:
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object.")
        if name == "search_notes":
            unexpected = set(arguments) - {"query", "top_k"}
            if unexpected or "query" not in arguments:
                raise ValueError("search_notes accepts only query and optional top_k.")
            result = self.search_notes(arguments["query"], arguments.get("top_k", 3))
            return ToolExecution(result.as_dict(self.sources), tuple(hit.chunk for hit in result.hits))
        if name == "read_source":
            if set(arguments) != {"source_id", "line_start", "line_end"}:
                raise ValueError("read_source requires source_id, line_start, and line_end.")
            result = self.read_source(**arguments)
            return ToolExecution(result.as_dict(), (result.chunk,))
        raise ValueError(f"Unknown knowledge tool: {name}")


def knowledge_tool_definitions() -> list[dict]:
    """Provider-neutral JSON schemas; the Agent transport will wrap them later."""
    return [
        {
            "name": "search_notes",
            "description": "Semantically search the currently loaded notes. Scores rank candidates and do not prove relevance.",
            "parameters": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "query": {"type": "string", "description": "A focused semantic search query."},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_RESULTS, "default": 3},
                },
                "required": ["query"],
            },
        },
        {
            "name": "read_source",
            "description": f"Read an exact inclusive line range from a source version returned by search_notes, up to {MAX_READ_LINES} lines.",
            "parameters": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "source_id": {"type": "string"},
                    "line_start": {"type": "integer", "minimum": 1},
                    "line_end": {"type": "integer", "minimum": 1},
                },
                "required": ["source_id", "line_start", "line_end"],
            },
        },
    ]
