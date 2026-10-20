"""Provider-independent conservative request accounting (never the E5 tokenizer)."""

import json
import os
from dataclasses import dataclass, replace
from hashlib import sha256

from chunking import Chunk


class BudgetError(ValueError):
    pass


@dataclass(frozen=True)
class GenerationBudget:
    context_tokens: int = 32768
    output_tokens: int = 4096
    margin_tokens: int = 2048
    capacity_source: str = "application fallback; service capacity unverified"

    def __post_init__(self):
        if min(self.context_tokens, self.output_tokens, self.margin_tokens) <= 0:
            raise BudgetError("生成预算配置必须为正整数。")
        if self.input_limit < 512:
            raise BudgetError("生成预算不足：上下文容量必须大于输出预留和安全余量。")

    @property
    def input_limit(self):
        return self.context_tokens - self.output_tokens - self.margin_tokens

    @staticmethod
    def estimate(value):
        # One token per UTF-8 byte deliberately overcounts ordinary BPE text.
        # Unknown providers may differ: this is a labelled estimate, not a tokenizer.
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    def usage(self, messages, **protocol):
        return self.estimate({"messages": messages, **protocol}) + 256 + 32 * len(messages)

    def fits(self, messages, **protocol):
        return self.usage(messages, **protocol) <= self.input_limit

    def check(self, messages, **protocol):
        used = self.usage(messages, **protocol)
        if used > self.input_limit:
            raise BudgetError(
                f"本轮内容超过生成预算（保守估计 {used}，输入上限 {self.input_limit}）。"
                "输入已保留；请缩小选段或由开发者核对服务容量后调整生成预算，再重试。"
            )
        return self.diagnostic(used)

    def diagnostic(self, used):
        return {"estimated_input_tokens": used, "input_limit": self.input_limit,
                "context_tokens": self.context_tokens, "output_reserved": self.output_tokens,
                "margin_tokens": self.margin_tokens, "capacity_source": self.capacity_source,
                "count_method": "conservative UTF-8 bytes + protocol allowance"}

    @classmethod
    def from_env(cls):
        try:
            return cls(int(os.getenv("LLM_CONTEXT_TOKENS", "32768")),
                       int(os.getenv("LLM_OUTPUT_TOKENS", "4096")),
                       int(os.getenv("LLM_SAFETY_TOKENS", "2048")),
                       "explicit LLM_CONTEXT_TOKENS" if os.getenv("LLM_CONTEXT_TOKENS") else
                       "application fallback; service capacity unverified")
        except ValueError as exc:
            if isinstance(exc, BudgetError):
                raise
            raise BudgetError("生成预算环境变量必须为整数。") from exc


def fit_chunks(chunks, accepts):
    """Drop optional excerpts or shorten at whole original lines; permissions follow."""
    kept = []
    for chunk in chunks:
        if chunk in kept:
            continue
        supports = []
        for start, end in chunk.context_ranges:
            support = next((c for c in [*kept, *chunks] if c.source_id == chunk.source_id
                            and c.line_start <= start and c.line_end >= end), None)
            if support is None:
                break
            if support not in kept and support not in supports:
                supports.append(support)
        else:
            if accepts(kept + supports + [chunk]):
                kept.extend(supports + [chunk])
                continue
        if chunk.atomic or chunk.context_ranges:
            continue
        lines = chunk.text.splitlines()
        low, high, best = 1, len(lines) - 1, None
        while low <= high:
            count = (low + high) // 2
            end = chunk.line_start + count - 1
            candidate = replace(chunk, text="\n".join(lines[:count]), line_end=end,
                                chunk_id=sha256(f"{chunk.chunk_id}:budget:{end}".encode()).hexdigest())
            if accepts(kept + [candidate]):
                best, low = candidate, count + 1
            else:
                high = count - 1
        if best is not None:
            kept.append(best)
    return kept
