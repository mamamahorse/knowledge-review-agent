"""Persist document vectors in SQLite; always search the currently loaded sources."""

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from chunking import Chunk, CHUNK_ALGORITHM
from embeddings import LocalEmbedder, passage_text
from retrieval import unit_vector

DEFAULT_INDEX = Path(__file__).resolve().parent / ".data" / "embeddings.sqlite3"


@dataclass(frozen=True)
class IndexResult:
    vectors: list[list[float]]
    reused: int
    computed: int


def vector_is_valid(vector, dimensions: int) -> bool:
    if not isinstance(vector, list) or len(vector) != dimensions:
        return False
    if any(type(value) not in (int, float) for value in vector):
        return False
    try:
        unit_vector(vector)
    except (ValueError, OverflowError):
        return False
    return True


def get_document_vectors(
    chunks: list[Chunk], embedder: LocalEmbedder, dimensions: int,
    *, max_chars: int, min_chars: int, path: Path | None = None, rebuild: bool = False,
) -> IndexResult:
    if dimensions <= 0:
        raise ValueError("Embedding dimension must be positive.")
    profile = {
        "schema": 1, "embedding": embedder.cache_profile(), "dimensions": dimensions,
        "chunking": {"algorithm": CHUNK_ALGORITHM, "max_chars": max_chars, "min_chars": min_chars},
    }
    metadata = json.dumps(profile, sort_keys=True, ensure_ascii=False)
    profile_id = sha256(metadata.encode("utf-8")).hexdigest()
    input_hashes = [sha256(passage_text(c, embedder.config.passage_prefix).encode("utf-8")).hexdigest() for c in chunks]
    path = DEFAULT_INDEX if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(path, timeout=10)) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, metadata TEXT NOT NULL)")
            connection.execute("""CREATE TABLE IF NOT EXISTS vectors (
                profile_id TEXT NOT NULL, chunk_id TEXT NOT NULL, source_id TEXT NOT NULL,
                line_start INTEGER NOT NULL, line_end INTEGER NOT NULL,
                input_hash TEXT NOT NULL, vector TEXT NOT NULL, checksum TEXT NOT NULL,
                PRIMARY KEY (profile_id, chunk_id))""")
            vectors = [None] * len(chunks)
            missing = []
            for index, chunk in enumerate(chunks):
                row = None if rebuild else connection.execute(
                    "SELECT source_id, line_start, line_end, input_hash, vector, checksum FROM vectors WHERE profile_id=? AND chunk_id=?",
                    (profile_id, chunk.chunk_id),
                ).fetchone()
                if row and row[:4] == (chunk.source_id, chunk.line_start, chunk.line_end, input_hashes[index]):
                    try:
                        vector = json.loads(row[4])
                        intact = sha256(row[4].encode("utf-8")).hexdigest() == row[5]
                        if intact and vector_is_valid(vector, dimensions):
                            vectors[index] = vector
                    except (ValueError, TypeError, AttributeError):
                        pass  # A damaged row is a cache miss, never trusted evidence.
                if vectors[index] is None:
                    missing.append(index)
            if missing:
                fresh = embedder.embed_chunks([chunks[index] for index in missing])
                if len(fresh) != len(missing) or not all(vector_is_valid(v, dimensions) for v in fresh):
                    raise ValueError("Embedding returned an invalid number, dimension, or value of vectors; index not updated.")
                # Compute first, then commit all updates together. Failure leaves old rows intact.
                with connection:
                    connection.execute("INSERT OR IGNORE INTO profiles VALUES (?, ?)", (profile_id, metadata))
                    for index, vector in zip(missing, fresh, strict=True):
                        chunk = chunks[index]
                        encoded = json.dumps(vector, allow_nan=False)
                        connection.execute(
                            "INSERT OR REPLACE INTO vectors VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (profile_id, chunk.chunk_id, chunk.source_id, chunk.line_start, chunk.line_end,
                             input_hashes[index], encoded, sha256(encoded.encode("utf-8")).hexdigest()),
                        )
                        vectors[index] = vector
            return IndexResult(vectors, len(chunks) - len(missing), len(missing))
    except sqlite3.Error as exc:
        raise RuntimeError(f"Cannot use vector index {path}: {exc}. Use --no-cache to search without this index.") from exc
