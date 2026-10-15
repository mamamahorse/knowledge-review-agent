"""Optional local history tools; discussion references never grant source rights."""

import json
import math
import re
from typing import Literal
from dataclasses import replace
from hashlib import sha256

from pydantic import Field

from answers import AnswerBlock, NoteAnswer, SourceOnlyAnswer, StrictModel
from chunking import Chunk
from conversation_store import ConversationBusy
from embedding_config import select_embedding_config
from embeddings import LocalEmbedder


class MemoryReference(StrictModel):
    block_index: int = Field(ge=0, description="Answer block associated with this discussion reference.")
    conversation_id: str
    message_id: str
    message_revision: int = Field(ge=1)
    start_offset: int = Field(ge=0, description="Original message character offset, inclusive; not a document line.")
    end_offset: int = Field(ge=1, description="Original message character offset, exclusive.")


class DiscussionAnswerBlock(AnswerBlock):
    kind: Literal["grounded", "inference", "supplement"] = Field(description=(
        "grounded/inference require original document citations. Recalled discussions and explanations "
        "without document evidence belong to supplement, even when an exact history reference is available. "
        "History provenance proves what was said, not the technical correctness of that statement."))


class MemoryAnswer(NoteAnswer):
    coverage: Literal["sufficient", "partial", "none"] = Field(description="Coverage by supplied DOCUMENT evidence only; history references do not increase document coverage.")
    blocks: list[DiscussionAnswerBlock]
    history_references: list[MemoryReference] = Field(max_length=12, description="Discussion provenance only; not technical source evidence. Empty when unused.")


class SourceMemoryAnswer(SourceOnlyAnswer):
    history_references: list[MemoryReference] = Field(max_length=12, description="Discussion provenance only. Document citations remain required for every source-only block.")


def memory_tool_definitions():
    return [{"name": "search_conversations", "description":
             "Find relevant completed local discussions, including archived chats, when continuity needs earlier history. Assistant explanations are not document evidence. Returns exact original spans; no age cutoff.",
             "parameters": {"type": "object", "properties": {
                 "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                 "top_k": {"type": "integer", "minimum": 1, "maximum": 5}},
                 "required": ["query"], "additionalProperties": False}},
            {"name": "read_conversation", "description":
             "Read an exact original range of a known local message plus its paired question/answer for context. Deleted or invalid derived messages are unavailable.",
             "parameters": {"type": "object", "properties": {
                 "message_id": {"type": "string"}, "start_offset": {"type": "integer", "minimum": 0},
                 "end_offset": {"type": "integer", "minimum": 1}},
                 "required": ["message_id", "start_offset", "end_offset"], "additionalProperties": False}}]


def terms(text):
    # Explicit Chinese bigrams instead of pretending SQLite's default tokenizer
    # provides Chinese word segmentation. Keep numbers and code symbols intact.
    words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text.lower())
    return {part for word in words for part in
            ([word] if re.fullmatch(r"[a-z0-9_]+", word) else [word[i:i+2] for i in range(max(1, len(word)-1))])}


def fit_memory(spans, accepts):
    """Shrink views, never archives; offsets and allowed reference ranges follow."""
    kept = []
    for span in spans:
        if accepts(kept + [span]):
            kept.append(span)
            continue
        low, high, best = 32, len(span["content"]) - 1, None
        while low <= high:
            width = (low + high) // 2
            part = {**span, "content": span["content"][:width], "end_offset": span["start_offset"] + width}
            if accepts(kept + [part]):
                best, low = part, width + 1
            else:
                high = width - 1
        if best:
            kept.append(best)
    return kept


class ConversationMemory:
    def __init__(self, store, owner_id, dependencies=(), *, embedder=None):
        self.store, self.owner_id = store, owner_id
        self.dependencies = {d["conversation_id"]: d["revision"] for d in dependencies}
        self.visible = []
        self._embedder = embedder
        self.diagnostics = {}

    def guard(self):
        self.store.assert_dependencies(self.dependency_list())

    def dependency_list(self):
        return [{"conversation_id": identity, "revision": revision} for identity, revision in self.dependencies.items()]

    def bind(self, context):
        return replace(context, memory_spans=tuple(self.visible), memory_dependencies=tuple(self.dependency_list()))

    @staticmethod
    def eligible_sql():
        return ("SELECT m.id AS message_id,m.role,m.content,m.created_at,t.id AS turn_id,t.seq,"
                "c.id AS conversation_id,c.title,c.memory_revision AS conversation_revision "
                "FROM messages m JOIN turns t ON t.id=m.turn_id JOIN conversations c ON c.id=t.conversation_id "
                "WHERE t.status='completed' AND (m.role='user' OR t.memory_valid=1)")

    def messages(self):
        with self.store.connection() as db:
            return [dict(r) for r in db.execute(self.eligible_sql())]

    def message(self, identity):
        with self.store.connection() as db:
            row = db.execute(self.eligible_sql() + " AND m.id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("历史原话已删除、失效或不可用于记忆。")
        return dict(row)

    @staticmethod
    def span(message, start, end):
        return {**{k: v for k, v in message.items() if k != "content"}, "message_revision": 1,
                "start_offset": start, "end_offset": end, "content": message["content"][start:end]}

    def remember(self, spans):
        for span in spans:
            identity, revision = span["conversation_id"], span["conversation_revision"]
            if identity in self.dependencies and self.dependencies[identity] != revision:
                raise ConversationBusy("聊天来源版本已变化，请重试。")
            self.dependencies[identity] = revision
        self.guard()
        keys = {(s["message_id"], s["start_offset"], s["end_offset"]) for s in self.visible}
        self.visible.extend(s for s in spans if (s["message_id"], s["start_offset"], s["end_offset"]) not in keys)

    def execute(self, name, arguments):
        definitions = {d["name"]: d for d in memory_tool_definitions()}
        if name not in definitions or not isinstance(arguments, dict):
            raise ValueError("Unknown history tool or invalid arguments.")
        spec = definitions[name]["parameters"]
        if set(arguments) - set(spec["properties"]) or not set(spec["required"]) <= set(arguments):
            raise ValueError("历史工具参数不合法。")
        self.guard()
        if name == "search_conversations":
            query, k = arguments["query"], arguments.get("top_k", 3)
            if not isinstance(query, str) or not query.strip() or len(query) > 2000 or type(k) is not int or not 1 <= k <= 5:
                raise ValueError("历史搜索需要非空查询，top_k 为 1–5。")
            spans = self.search(query, k)
        else:
            start, end = arguments["start_offset"], arguments["end_offset"]
            if not isinstance(arguments["message_id"], str) or type(start) is not int or type(end) is not int:
                raise ValueError("历史原话标识与范围类型不合法。")
            message = self.message(arguments["message_id"])
            if not 0 <= start < end <= len(message["content"]) or end - start > 1400:
                raise ValueError("每次读取最多 1400 字符，范围必须位于原始消息内。")
            spans = self.with_neighbor([self.span(message, start, end)], self.messages())
        return {"history_spans": spans, "notice": "Discussion records only, not document evidence. Similarity is not confidence.",
                "retrieval": self.diagnostics}, spans

    def with_neighbor(self, spans, messages):
        result = []
        seen = set()
        for span in spans:
            if span["message_id"] not in seen:
                result.append(span)
                seen.add(span["message_id"])
            for other in messages:
                if other["turn_id"] == span["turn_id"] and other["message_id"] not in seen:
                    result.append(self.span(other, 0, min(800, len(other["content"]))))
                    seen.add(other["message_id"])
        return result

    def search(self, query, top_k):
        self.diagnostics = {}
        originals = self.messages()
        # Current chat already has complete/recent history. Early originals remain
        # eligible, but its latest messages should not crowd out other discussions.
        segments = [(m, start, min(start + 800, len(m["content"])))
                    for m in originals for start in range(0, len(m["content"]), 640)]
        qterms = terms(query)
        lexical = {}
        for i, (m, start, end) in enumerate(segments):
            overlap = qterms & terms(m["content"][start:end])
            if overlap:
                lexical[i] = len(overlap) / max(1, len(qterms))
        semantic, warning = {}, None
        if segments:
            try:
                semantic = self.semantic_scores(query, segments)
            except (OSError, RuntimeError, ValueError):
                # Local model failures must not lose a turn; literal recall works.
                warning = "本地语义检索暂不可用，本次仅使用关键词匹配。"
        # Minimum semantic similarity is an explicit application heuristic, not
        # a calibrated relevance probability. Permit zero results for unrelated queries.
        ranks = {}
        for scores in (lexical, semantic):
            for rank, (i, _) in enumerate(sorted(scores.items(), key=lambda x: x[1], reverse=True), 1):
                ranks[i] = ranks.get(i, 0) + 1 / (60 + rank)
        ordered = sorted(ranks, key=lambda i: (ranks[i], segments[i][0]["created_at"]), reverse=True)
        selected, used = [], set()
        for i in ordered:
            m, start, end = segments[i]
            if m["message_id"] in used:
                continue
            selected.append({**self.span(m, start, end), "retrieval_reason": {
                "literal_terms": sorted(qterms & terms(m["content"][start:end])),
                "semantic_similarity": semantic.get(i), "rank_fusion": ranks[i]}})
            used.add(m["message_id"])
            if len(selected) == top_k:
                break
        self.diagnostics = {**self.diagnostics, "strategy": "literal Chinese bigrams/code tokens + semantic RRF", "semantic_floor": 0.78,
                            "eligible_messages": len(originals), "warning": warning}
        return self.with_neighbor(selected, originals)

    def semantic_scores(self, query, segments):
        if self._embedder is None:
            self._embedder = LocalEmbedder(select_embedding_config("e5-small"))
        profile = sha256(json.dumps(self._embedder.cache_profile(), sort_keys=True).encode()).hexdigest()
        vectors, missing = {}, []
        with self.store.connection() as db:
            for i, (message, start, end) in enumerate(segments):
                digest = sha256(message["content"][start:end].encode()).hexdigest()
                row = db.execute("SELECT vector_json FROM memory_vectors WHERE message_id=? AND start_offset=? AND end_offset=? AND profile=? AND content_hash=?",
                                 (message["message_id"], start, end, profile, digest)).fetchone()
                if row:
                    vectors[i] = json.loads(row[0])
                else:
                    missing.append((i, digest))
        # Bounded embedding batches; no SQLite write transaction during encoding.
        for offset in range(0, len(missing), 32):
            batch = missing[offset:offset+32]
            chunks = [Chunk(str(i), segments[i][0]["conversation_id"], (), 1, 1,
                            segments[i][0]["content"][segments[i][1]:segments[i][2]]) for i, _ in batch]
            encoded = self._embedder.embed_chunks(chunks)
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                for (i, digest), vector in zip(batch, encoded, strict=True):
                    message, start, end = segments[i]
                    self.store.check_dependencies(db, [{"conversation_id": message["conversation_id"], "revision": message["conversation_revision"]}])
                    db.execute("INSERT OR REPLACE INTO memory_vectors VALUES (?,?,?,?,?,?)",
                               (message["message_id"], start, end, profile, digest, json.dumps(vector)))
                    vectors[i] = vector
        q = self._embedder.embed_query(query)
        result = {}
        for i, vector in vectors.items():
            if len(vector) != len(q):
                raise ValueError("历史向量维度与查询不一致。")
            denominator = math.sqrt(sum(v*v for v in vector) * sum(v*v for v in q))
            score = sum(a*b for a, b in zip(vector, q, strict=True)) / denominator if denominator else 0
            if score >= 0.78:
                result[i] = score
        self.diagnostics["vectors_computed"] = len(missing)
        return result

    def validate_references(self, references, block_count):
        self.guard()
        enriched = []
        for ref in references:
            if ref.block_index >= block_count:
                raise ValueError("历史引用指向不存在的回答区块。")
            span = next((s for s in self.visible if s["conversation_id"] == ref.conversation_id and
                         s["message_id"] == ref.message_id and s["message_revision"] == ref.message_revision and
                         s["start_offset"] <= ref.start_offset < ref.end_offset <= s["end_offset"]), None)
            if span is None:
                raise ValueError("历史引用超出本轮实际可见的原话范围。")
            original = self.message(ref.message_id)
            excerpt = original["content"][ref.start_offset:ref.end_offset]
            offset = ref.start_offset - span["start_offset"]
            if excerpt != span["content"][offset:offset + len(excerpt)]:
                raise ValueError("历史引用与原始记录不一致。")
            enriched.append({**ref.model_dump(), "title": original["title"], "role": original["role"],
                             "seq": original["seq"], "excerpt": excerpt})
        return enriched
