"""Safe, reusable knowledge operations for the future Agent loop."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from time import perf_counter

from chunking import Chunk, chunk_source, section_label
from embedding_config import EmbeddingConfig
from embeddings import LocalEmbedder
from retrieval import SearchHit, rank_chunks
from sources import Source
from vector_index import get_document_vectors
from hybrid_retrieval import BM25Index, HybridConfig, reciprocal_rank_fusion

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
    retrieval_mode: str = 'dense'
    branches: dict | None = None

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
                **({'branches':self.branches.get(hit.chunk.chunk_id,{})} if self.branches is not None else {}),
            } for hit in self.hits],
            "retrieval_mode": self.retrieval_mode,
            "score_kind": 'rrf' if self.retrieval_mode == 'hybrid' else 'cosine',
            "notice": "Scores rank candidates; this is not an answer confidence score or proof of relevance.",
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
        retrieval_mode: str = 'dense',
    ):
        if not sources:
            raise ValueError("Knowledge tools require at least one source.")
        if max_chars <= 0 or not 0 <= min_chars <= max_chars:
            raise ValueError("Chunk limits must satisfy 0 <= min_chars <= max_chars.")
        if retrieval_mode not in ('dense', 'hybrid'):
            raise ValueError('查找方式必须为dense或hybrid；重排暂仅用于离线评测。')
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
        self.retrieval_mode = retrieval_mode
        self._bm25 = None
        self.retrieval_trace = []

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
        trace = {'mode':self.retrieval_mode,'status':'failed','requested_top_k':top_k,
                 'source_ids':[s.source_id for s in self.sources]}
        start = perf_counter()
        try:
            result = self._search_notes(query, top_k, trace)
            trace.update(status='ok', returned=[{'chunk_id':h.chunk.chunk_id,'source_id':h.chunk.source_id,
                'line_start':h.chunk.line_start,'line_end':h.chunk.line_end,'score':h.score,
                **({'branches':result.branches[h.chunk.chunk_id]} if result.branches is not None else {})} for h in result.hits])
            return result
        except Exception as exc:
            # Keep the failure visible without storing query text or transport data.
            trace['error_type'] = type(exc).__name__
            raise
        finally:
            trace['total_ms'] = (perf_counter()-start)*1000
            self.retrieval_trace.append(trace)

    def _search_notes(self, query, top_k, trace):
        start = perf_counter()
        query_vector = self.embedder.embed_query(query)
        trace['query_embedding_ms'] = (perf_counter()-start)*1000
        # Custom embedders may provide their own preparation; constructor mocks
        # and older adapters remain usable without depending on a concrete type.
        if self._vectors is None and callable(getattr(type(self.embedder), "prepare_chunks", None)):
            self.chunks = self.embedder.prepare_chunks(self.chunks)
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
        trace.update(permitted_chunks=len(self.chunks),computed_vectors=computed,reused_vectors=reused,
                     vectors_in_memory=memory_hit)
        start = perf_counter()
        config = HybridConfig()
        hits = rank_chunks(self.chunks, vectors, query_vector,
                           config.branch_m if self.retrieval_mode == 'hybrid' else top_k)
        trace['dense_ms'] = (perf_counter()-start)*1000
        branches = None
        if self.retrieval_mode == 'hybrid':
            # Build only on real search, from the same prepared permission scope.
            # Instance-local reuse cannot leak removed versions to other requests.
            start = perf_counter()
            built = self._bm25 is None
            if built:
                self._bm25 = BM25Index(self.chunks, config)
            trace['bm25_build_ms'] = (perf_counter()-start)*1000
            start = perf_counter()
            lexical_hits = self._bm25.search(query, config.branch_m)
            trace['bm25_search_ms'] = (perf_counter()-start)*1000
            start = perf_counter()
            fused, branches = reciprocal_rank_fusion(self.chunks, hits, lexical_hits, config)
            trace.update(rrf_ms=(perf_counter()-start)*1000, bm25_built=built,
                         dense_candidates=len(hits), bm25_candidates=len(lexical_hits),
                         fused_candidates=len(fused), protocol=config.protocol())
            hits = fused[:top_k]
        return SearchResult(
            hits, reused, computed, memory_hit, len(query_vector),
            self.embedding_config.model_name, self.embedder.max_tokens,
            self.retrieval_mode, branches,
        )

    def read_source(self, source_id: str, line_start: int, line_end: int) -> ReadResult:
        # Read an exact inclusive line range from a source version returned by search_notes
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
            "description": "Search the currently loaded notes for relevant meaning and terms. Scores rank candidates and do not prove relevance.",
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
