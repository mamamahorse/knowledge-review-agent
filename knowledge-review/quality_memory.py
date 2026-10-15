"""Isolated synthetic history evaluation; smoke is controlled, live is explicit."""

import argparse
import json
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

import httpx
from openai import OpenAI

from agent_loop import run_agent
from conversation_context import build_context
from conversation_memory import ConversationMemory
from conversation_store import ConversationStore
from embedding_config import select_embedding_config
from generation_budget import GenerationBudget
from knowledge_tools import KnowledgeTools
from sources import Source


def seed(store, chat, question, text):
    turn = store.begin(chat, uuid4().hex, {"question": question})
    answer = {"coverage": "none", "coverage_note": "Synthetic discussion record", "blocks": [
        {"kind": "supplement", "text": text, "citations": []}]}
    store.finish(turn["id"], turn["attempt"], answer, {})
    return turn


class ControlledEmbedder:
    def cache_profile(self):
        return {"model": "controlled", "version": 1}

    def embed_chunks(self, chunks):
        return [[1., 0.] if "ACK" in c.text else [0., 1.] for c in chunks]

    def embed_query(self, question):
        return [1., 0.]


def controlled_client(memory):
    calls = []
    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        if "tools" in body and body["tools"][0]["function"]["name"] != "submit_result":
            if len(calls) == 1:
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": "h", "type": "function",
                    "function": {"name": "search_conversations", "arguments": '{"query":"ACK SEQ=100 LEN=20"}'}}]}
            else:
                message = {"role": "assistant", "content": "ready"}
        else:
            span = next(s for s in memory.visible if s["role"] == "user")
            ref = {"block_index": 0, **{k:span[k] for k in ("conversation_id", "message_id", "message_revision", "start_offset", "end_offset")}}
            message = {"role": "assistant", "content": json.dumps({"coverage": "none", "coverage_note": "Synthetic controlled response",
                "blocks": [{"kind": "supplement", "text": "ACK=130", "citations": []}], "history_references": [ref]})}
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
            "model": "controlled", "choices": [{"index": 0, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop", "message": message}]})
    return lambda **options: OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(respond)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("smoke", "live"), default="smoke")
    parser.add_argument("--output-tokens", type=int, help="Evaluation-only output reserve; leaves application configuration unchanged.")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    directory = root / ".data" / "evaluations" / f"memory-{args.mode}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:6]}"
    directory.mkdir(parents=True)
    store = ConversationStore(directory / "conversations.sqlite3")
    old, other, current = [store.create(t)["id"] for t in ("旧 TCP 例子", "烘焙讨论", "新聊天")]
    seed(store, old, "SEQ=100，LEN=20，ACK=120。我的决定：先讨论边界，不加入学习计划。", "ACK 表示下一期待收到的字节序号。")
    seed(store, other, "烘焙馅饼的面团怎么处理？", "这里是完全无关的合成聊天。")
    store.archive(old, True)
    with store.connection() as db:
        db.execute("UPDATE messages SET created_at='1990-01-01' WHERE turn_id IN (SELECT id FROM turns WHERE conversation_id=?)", (old,))
    source = Source("synthetic-current-doc", directory / "scope.md", "# 操作说明\n当前资料没有 TCP 定义，仅允许查阅本文件。")
    source.path.write_text(source.text, encoding="utf-8")
    question = "请找回以前其他聊天中 SEQ=100、LEN=20 的 ACK 例子；现在长度改成30呢？"
    if args.mode == "live":
        from dotenv import load_dotenv
        load_dotenv(root / ".env")
    budget = GenerationBudget.from_env()
    if args.output_tokens is not None:
        budget = replace(budget, output_tokens=args.output_tokens)
    report = {"mode": args.mode, "synthetic_histories": True, "semantic_quality_rate": None, "cases": [], "budget": budget.diagnostic(0)}
    memory = ConversationMemory(store, current, [{"conversation_id": current, "revision": 1}],
                                embedder=ControlledEmbedder() if args.mode == "smoke" else None)
    def run():
        background = build_context(store, current, question, "zh", budget)
        return run_agent([source], question, KnowledgeTools([source], select_embedding_config()),
                         conversation_context=background, response_language="zh", memory_tools=memory, budget=budget)
    try:
        if args.mode == "smoke":
            factory = controlled_client(memory)
            with patch("agent_loop.OpenAI", side_effect=factory), patch("model_client.OpenAI", side_effect=factory), \
                 patch("agent_loop.model_settings", return_value=("controlled", {})), patch("model_client.model_settings", return_value=("controlled", {})):
                result = run()
        else:
            # Uses the existing configured model service; never prints credentials.
            result = run()
        text = "\n".join(b.text for b in result.answer.blocks)
        report["cases"].append({"name": "archived_old_cross_chat_ack", "status": "completed",
            "mentions_130": "130" in text, "answer": result.answer.model_dump(), "history_references": result.history_references,
            "steps": [{"name": s.name, "error": s.error} for s in result.steps], "context": result.context_snapshot})
        # Exercise real local semantic recall separately from the model's choice.
        semantic_spans = memory.search("下一期待收到的字节序号", 3)
        report["cases"].append({"name": "semantic_paraphrase", "contains_tcp_chat": any(s["conversation_id"] == old for s in semantic_spans),
                                "retrieval": memory.diagnostics})
        if args.mode == "live":
            greeting = "你好，开始学习前先打个招呼。"
            blank = store.create("问候验收")["id"]
            background = build_context(store, blank, greeting, "zh", budget)
            greeting_memory = ConversationMemory(store, blank, background.memory_dependencies)
            greeting_run = run_agent([source], greeting, KnowledgeTools([source], select_embedding_config()),
                conversation_context=background, response_language="zh", memory_tools=greeting_memory, budget=budget)
            report["cases"].append({"name": "greeting", "tool_calls": len(greeting_run.steps),
                "embedding_loaded": greeting_memory._embedder is not None, "answer": greeting_run.answer.model_dump()})
        store.delete(old)
        report["deletion"] = {"old_chat_removed": all(c["id"] != old for c in store.list(archived=True)),
                              "remaining_archived": len(store.list(archived=True))}
    except Exception as exc:
        # Preserve a partial report without raw transport errors/credentials.
        report["failure"] = type(exc).__name__
        safe_prefixes = ("the model ", "the agent ", "line references ", "strict mode ", "model supplements ",
                         "grounded statements ", "evidence coverage ", "历史引用", "本轮内容超过生成预算")
        if str(exc).startswith(safe_prefixes):
            report["validation_failure"] = str(exc)
    report["code_hashes"] = {name: __import__("hashlib").sha256((root/name).read_bytes()).hexdigest()
        for name in ("conversation_memory.py", "conversation_store.py", "conversation_context.py", "agent_loop.py", "model_client.py")}
    path = directory / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(path), "mode": args.mode, "completed_cases": len(report["cases"]), "failure": report.get("failure")}, ensure_ascii=False))
    return 1 if "failure" in report else 0


if __name__ == "__main__":
    raise SystemExit(main())
