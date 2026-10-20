"""Local SentenceTransformer embeddings with model-specific input conventions."""

import os
import json
from dataclasses import asdict, replace
from math import sqrt
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

from chunking import Chunk, section_label
from embedding_config import EmbeddingConfig, select_embedding_config, effective_token_limit

MODEL_CACHE = Path(__file__).resolve().parent / ".models"


def passage_text(chunk: Chunk, prefix: str) -> str:
    # Metadata adds context for small chunks without changing their source text.
    heading = section_label(chunk) if any(chunk.section_paths or (chunk.heading_path,)) else ""
    context = '\n'.join(x for x in (heading, chunk.retrieval_context) if x)
    return f"{prefix}{context}\n{chunk.text}" if context else f"{prefix}{chunk.text}"


class LocalEmbedder:
    def __init__(self, config: EmbeddingConfig | None = None, allow_download: bool = False):
        self.config = config or select_embedding_config()
        # Keep heavy imports out of file previews and unit tests.
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError("Install local embedding dependencies: pip install -r requirements-local.txt") from exc
        try:
            self.model = SentenceTransformer(
                self.config.model_name, device="cpu", cache_folder=str(MODEL_CACHE),
                local_files_only=not allow_download, trust_remote_code=False,
                token=False,
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                "Cannot load the local embedding model. Run main.py --download-model first; "
                "the download needs access to huggingface.co. "
                f"Underlying error: {type(exc).__name__}: {exc}"
            ) from exc
        # Read this model's actual configuration, not a global 512-token constant.
        self.max_tokens = effective_token_limit(self.model.max_seq_length, self.config.max_tokens)
        self.model.max_seq_length = self.max_tokens
        self._cache_profile = None

    def cache_profile(self) -> dict:
        """Identify loaded weights/tokenizer, not just a potentially reused model name."""
        if self._cache_profile is None:
            import torch

            fingerprint = sha256(str(self.model).encode("utf-8"))
            tokenizer = self.model.tokenizer
            backend = getattr(tokenizer, "backend_tokenizer", None)
            if backend is None:
                # Custom slow tokenizers need an explicit identity adapter first.
                raise RuntimeError("Persistent caching requires a fast tokenizer; use --no-cache for this model.")
            tokenizer_config = json.loads(backend.to_str())
            # These are mutable batch options, not the tokenizer's vocabulary/rules.
            tokenizer_config.pop("padding", None)
            tokenizer_config.pop("truncation", None)
            fingerprint.update(json.dumps(tokenizer_config, sort_keys=True).encode("utf-8"))
            fingerprint.update(json.dumps(tokenizer.special_tokens_map, sort_keys=True).encode("utf-8"))
            for name, tensor in self.model.state_dict().items():
                fingerprint.update(f"{name}:{tensor.dtype}:{tuple(tensor.shape)}".encode("utf-8"))
                fingerprint.update(tensor.detach().cpu().contiguous().reshape(-1).view(torch.uint8).numpy().tobytes())
            self._cache_profile = {
                "format": "local-embedding-v2-token-windows", "config": asdict(self.config),
                "max_tokens": self.max_tokens, "model_fingerprint": fingerprint.hexdigest(),
                "runtime": {name: version(name) for name in ("sentence-transformers", "transformers", "torch", "tokenizers")},
            }
        return self._cache_profile

    def _encode(self, texts: list[str], labels: list[str]) -> list[list[float]]:
        # A final guard: every automatically prepared window must fit exactly.
        for text, label in zip(texts, labels, strict=True):
            tokens = self.model.tokenizer(text, truncation=False, add_special_tokens=True, verbose=False)["input_ids"]
            if len(tokens) > self.max_tokens:
                raise ValueError(
                    f"{label}: {len(tokens)} tokens exceeds active limit {self.max_tokens} "
                    f"for {self.config.model_name}. "
                    "Embedding window preparation failed; no text was truncated."
                )
        return self.model.encode(
            texts, batch_size=16, normalize_embeddings=True,
            convert_to_numpy=True, show_progress_bar=False,
            prompt="",  # Prefixes are already included; suppress saved default prompts.
        ).tolist()

    def fits(self, text: str) -> bool:
        return len(self.model.tokenizer(
            text, truncation=False, add_special_tokens=True, verbose=False,
        )["input_ids"]) <= self.max_tokens

    def prepare_chunks(self, chunks: list[Chunk]) -> list[Chunk]:
        """Split oversized Markdown blocks at original line boundaries for retrieval."""
        result = []
        for chunk in chunks:
            if self.fits(passage_text(chunk, self.config.passage_prefix)):
                result.append(chunk)
                continue
            lines = chunk.text.split("\n")
            start = 0
            while start < len(lines):
                def part(end):
                    first, last = chunk.line_start + start, chunk.line_start + end - 1
                    identity = f"{chunk.chunk_id}:token-lines-v1:{first}:{last}"
                    context_ranges = chunk.context_ranges
                    if chunk.kind == 'table':
                        header_start, header_end = chunk.table_header_range or (chunk.line_start, min(chunk.line_start + 1, chunk.line_end))
                        if not (first <= header_start and last >= header_end):
                            context_ranges = tuple(dict.fromkeys((*context_ranges, (header_start, header_end))))
                    return replace(chunk, chunk_id=sha256((identity + (':table-v1' if chunk.kind == 'table' else '')).encode()).hexdigest(),
                                   line_start=first, line_end=last,
                                   text="\n".join(lines[start:end]), context_ranges=context_ranges)

                # A single long line is kept intact for honest line-based citations.
                # Its embedding uses complete text windows rather than truncation.
                end = start + 1
                if chunk.kind == 'table' and start == 0:
                    end = min(3, len(lines))
                low, high = end, len(lines)
                while low <= high:
                    mid = (low + high) // 2
                    if self.fits(passage_text(part(mid), self.config.passage_prefix)):
                        end, low = mid, mid + 1
                    else:
                        high = mid - 1
                result.append(part(end))
                start = end
        return result

    def _windows(self, body: str, prefix: str) -> list[str]:
        """Keep every original character, repeating the model's role prefix."""
        if self.fits(prefix + body):
            return [prefix + body]
        result = []
        start = 0
        while start < len(body):
            low, high, end = start + 1, len(body), start
            while low <= high:
                mid = (low + high) // 2
                if self.fits(prefix + body[start:mid]):
                    end, low = mid, mid + 1
                else:
                    high = mid - 1
            if end == start:
                raise ValueError("The embedding token limit cannot hold the role prefix and one character.")
            result.append(prefix + body[start:end])
            start = end
        return result

    def _encode_complete(self, bodies: list[str], prefix: str, labels: list[str]) -> list[list[float]]:
        windows, groups = [], []
        for body in bodies:
            pieces = self._windows(body, prefix)
            groups.append((len(windows), len(pieces)))
            windows.extend(pieces)
        window_labels = [label for label, (_, count) in zip(labels, groups, strict=True)
                         for _ in range(count)]
        vectors = self._encode(windows, window_labels)
        result = []
        for start, count in groups:
            if count == 1:
                result.append(vectors[start])
                continue
            weights = [len(text) - len(prefix) for text in windows[start:start + count]]
            pooled = [sum(vector[d] * weight for vector, weight in
                          zip(vectors[start:start + count], weights, strict=True)) / sum(weights)
                      for d in range(len(vectors[start]))]
            norm = sqrt(sum(value * value for value in pooled))
            if norm == 0:
                raise ValueError("Embedding windows produced a zero vector.")
            result.append([value / norm for value in pooled])
        return result

    def embed_chunks(self, chunks: list[Chunk]) -> list[list[float]]:
        if not chunks:
            return []
        return self._encode_complete(
            [passage_text(chunk, "") for chunk in chunks], self.config.passage_prefix,
            [f"Chunk {chunk.chunk_id[:12]} (L{chunk.line_start}-L{chunk.line_end})" for chunk in chunks],
        )

    def embed_query(self, question: str) -> list[float]:
        if not question.strip():
            raise ValueError("The search query must not be blank.")
        return self._encode_complete([question.strip()], self.config.query_prefix, ["Query"])[0]
