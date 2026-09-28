"""An in-memory cosine search, independent of the embedding model's runtime."""

from dataclasses import dataclass
from math import hypot, isfinite

from chunking import Chunk, format_chunks
from sources import Source


@dataclass(frozen=True)
class SearchHit:
    chunk: Chunk
    score: float


def unit_vector(vector: list[float]) -> list[float]:
    if not vector or not all(isfinite(value) for value in vector):
        raise ValueError("Embedding vectors must contain finite numbers and must not be empty.")
    norm = hypot(*vector)
    if not isfinite(norm) or norm == 0:
        raise ValueError("Embedding vectors must have a finite, nonzero length.")
    return [value / norm for value in vector]


def rank_chunks(
    chunks: list[Chunk], vectors: list[list[float]], query_vector: list[float], top_k: int = 3,
) -> list[SearchHit]:
    if top_k <= 0:
        raise ValueError("top_k must be positive.")
    if len(chunks) != len(vectors):
        raise ValueError("Each chunk must have exactly one embedding vector.")
    query = unit_vector(query_vector)
    hits = []
    for chunk, vector in zip(chunks, vectors, strict=True):
        if len(vector) != len(query):
            raise ValueError("Document and query embeddings must have the same dimensions.")
        document = unit_vector(vector)
        score = sum(a * b for a, b in zip(query, document, strict=True))
        hits.append(SearchHit(chunk, max(-1.0, min(1.0, score))))
    # Python's stable sort preserves source order when scores tie.
    return sorted(hits, key=lambda hit: hit.score, reverse=True)[:top_k]


def format_hits(hits: list[SearchHit], sources: list[Source], max_chars: int) -> str:
    return "\n\n".join(
        f"Rank {rank} | cosine: {hit.score:.4f}\n"
        + format_chunks([hit.chunk], sources, max_chars)
        for rank, hit in enumerate(hits, 1)
    )
