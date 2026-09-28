"""Model selection and input conventions, separate from model execution."""

from dataclasses import dataclass


@dataclass(frozen=True)
class EmbeddingConfig:
    model_name: str
    query_prefix: str
    passage_prefix: str
    max_tokens: int | None = None

    def __post_init__(self):
        if not self.model_name.strip():
            raise ValueError("Embedding model name must not be blank.")
        if self.max_tokens is not None and self.max_tokens <= 0:
            raise ValueError("Embedding max_tokens must be positive.")


MODEL_PRESETS = {
    "e5-small": EmbeddingConfig("intfloat/multilingual-e5-small", "query: ", "passage: "),
    "multilingual-minilm": EmbeddingConfig(
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", "", "",
    ),
}


def select_embedding_config(
    name: str = "e5-small", max_tokens: int | None = None,
    query_prefix: str | None = None, passage_prefix: str | None = None,
) -> EmbeddingConfig:
    preset = MODEL_PRESETS.get(name)
    if preset is None:
        preset = next((p for p in MODEL_PRESETS.values() if p.model_name == name), None)
    if preset is None:
        if query_prefix is None or passage_prefix is None:
            raise ValueError(
                "Custom models require explicit --query-prefix and --passage-prefix "
                "(use --query-prefix= --passage-prefix= for empty prefixes)."
            )
        return EmbeddingConfig(name, query_prefix, passage_prefix, max_tokens)
    return EmbeddingConfig(
        preset.model_name,
        preset.query_prefix if query_prefix is None else query_prefix,
        preset.passage_prefix if passage_prefix is None else passage_prefix,
        max_tokens,
    )


def effective_token_limit(model_limit: int, requested_limit: int | None) -> int:
    if not isinstance(model_limit, int) or model_limit <= 0:
        raise ValueError("The selected model must expose a positive max_seq_length.")
    if requested_limit is None:
        return model_limit
    if not 0 < requested_limit <= model_limit:
        raise ValueError(f"Requested token limit must be between 1 and model limit {model_limit}.")
    return requested_limit
