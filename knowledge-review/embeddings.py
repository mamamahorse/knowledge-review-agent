"""Local SentenceTransformer embeddings with model-specific input conventions."""

import os
import json
from dataclasses import asdict
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

from chunking import Chunk, section_label
from embedding_config import EmbeddingConfig, select_embedding_config, effective_token_limit

MODEL_CACHE = Path(__file__).resolve().parent / ".models"


def passage_text(chunk: Chunk, prefix: str) -> str:
    # Metadata adds context for small chunks without changing their source text.
    heading = section_label(chunk) if any(chunk.section_paths or (chunk.heading_path,)) else ""
    return f"{prefix}{heading}\n{chunk.text}" if heading else f"{prefix}{chunk.text}"


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
                "format": "local-embedding-v1", "config": asdict(self.config),
                "max_tokens": self.max_tokens, "model_fingerprint": fingerprint.hexdigest(),
                "runtime": {name: version(name) for name in ("sentence-transformers", "transformers", "torch", "tokenizers")},
            }
        return self._cache_profile

    def _encode(self, texts: list[str], labels: list[str]) -> list[list[float]]:
        # SentenceTransformer normally truncates. Reject oversized input first.
        for text, label in zip(texts, labels, strict=True):
            tokens = self.model.tokenizer(text, truncation=False, add_special_tokens=True)["input_ids"]
            if len(tokens) > self.max_tokens:
                raise ValueError(
                    f"{label}: {len(tokens)} tokens exceeds active limit {self.max_tokens} "
                    f"for {self.config.model_name}. "
                    "Use a smaller --chunk-size or divide the long paragraph/code block; "
                    "shorten the query if the query exceeds the limit. Nothing was truncated."
                )
        return self.model.encode(
            texts, batch_size=16, normalize_embeddings=True,
            convert_to_numpy=True, show_progress_bar=False,
            prompt="",  # Prefixes are already included; suppress saved default prompts.
        ).tolist()

    def embed_chunks(self, chunks: list[Chunk]) -> list[list[float]]:
        if not chunks:
            return []
        return self._encode(
            [passage_text(chunk, self.config.passage_prefix) for chunk in chunks],
            [f"Chunk {chunk.chunk_id[:12]} (L{chunk.line_start}-L{chunk.line_end})" for chunk in chunks],
        )

    def embed_query(self, question: str) -> list[float]:
        if not question.strip():
            raise ValueError("The search query must not be blank.")
        return self._encode([f"{self.config.query_prefix}{question.strip()}"], ["Query"])[0]
