import argparse
import sys
from pathlib import Path

from answers import render_answer
from chunking import chunk_source, format_chunks
from embedding_config import select_embedding_config
from model_client import answer_question
from sources import load_sources, format_sources


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Read and ask questions about Markdown notes.")
    parser.add_argument("note", nargs="*", type=Path, help="Paths to UTF-8 Markdown files.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("-q", "--question", help="Ask a question instead of previewing notes.")
    mode.add_argument("--chunks", action="store_true", help="Preview Markdown chunks locally.")
    mode.add_argument("--search", help="Find relevant chunks using local embeddings (no generated answer).")
    mode.add_argument("--download-model", action="store_true", help="Download the local embedding model once.")
    parser.add_argument("--rag", action="store_true", help="Retrieve excerpts before answering --question (cloud generation).")
    parser.add_argument("--agent", action="store_true", help="Let the model choose search/read tools before answering --question.")
    parser.add_argument("--focus", nargs=3, metavar=("NOTE", "START", "END"),
                        help="With --agent, explain selected original lines in a Markdown file.")
    parser.add_argument("--max-tool-calls", type=int, default=4, help="Agent tool-call budget (1-6; default 4).")
    parser.add_argument("--top-k", type=int, default=3, help="Number of search results.")
    parser.add_argument("--chunk-size", type=int, default=1200, help="Soft chunk size in characters (with --chunks or --search).")
    parser.add_argument("--chunk-min", type=int, default=100, help="Merge small adjacent chunks toward this character count; 0 disables merging.")
    parser.add_argument("--embedding-model", default="e5-small", help="e5-small, multilingual-minilm, or a custom SentenceTransformer name/path.")
    parser.add_argument("--embedding-max-tokens", type=int, help="Optional token cap, no higher than the selected model's limit.")
    parser.add_argument("--query-prefix", help="Override the embedding query prefix.")
    parser.add_argument("--passage-prefix", help="Override the embedding document prefix.")
    parser.add_argument("--strict", action="store_true", help="Answer only from supplied sources.")
    cache = parser.add_mutually_exclusive_group()
    cache.add_argument("--rebuild-index", action="store_true", help="Recompute document vectors for the current files and settings.")
    cache.add_argument("--no-cache", action="store_true", help="Compute vectors without reading or writing the local index.")
    args = parser.parse_args()
    if args.rag and args.question is None:
        parser.error("--rag requires --question.")
    if args.agent and args.question is None:
        parser.error("--agent requires --question.")
    if args.agent and args.rag:
        parser.error("--agent and --rag are separate answer modes.")
    if args.focus and not (args.agent and args.question is not None):
        parser.error("--focus requires --question and --agent.")
    if not 1 <= args.max_tool_calls <= 6:
        parser.error("--max-tool-calls must be between 1 and 6.")
    if (args.rebuild_index or args.no_cache) and not (args.search is not None or args.rag or args.agent):
        parser.error("Index options require --search, --question --rag, or --question --agent.")
    if args.chunk_size <= 0:
        parser.error("--chunk-size must be positive.")
    if not 0 <= args.chunk_min <= args.chunk_size:
        parser.error("--chunk-min must be between 0 and --chunk-size.")
    try:
        embedding_config = select_embedding_config(
            args.embedding_model, args.embedding_max_tokens, args.query_prefix, args.passage_prefix,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.top_k <= 0:
        parser.error("--top-k must be positive.")
    if args.search is not None and not args.search.strip():
        parser.error("The search query must not be blank.")
    if args.download_model:
        if args.note:
            parser.error("--download-model does not accept note paths.")
        from embeddings import LocalEmbedder, MODEL_CACHE
        print(f"Preparing {embedding_config.model_name} in {MODEL_CACHE} ...", flush=True)
        try:
            embedder = LocalEmbedder(embedding_config, allow_download=True)
            vector = embedder.embed_query("model check")
        except (OSError, ValueError, RuntimeError) as exc:
            parser.exit(status=1, message=f"Cannot prepare embeddings: {exc}\n")
        print(f"Model ready: {len(vector)} dimensions | token limit: {embedder.max_tokens}. Search will use local files only.")
        return
    if args.question is not None and not args.question.strip():
        parser.exit(status=1, message="The question must not be blank.\n")
    focus_path = None
    focus_start = focus_end = None
    if args.focus:
        focus_path = Path(args.focus[0]).resolve()
        try:
            focus_start, focus_end = int(args.focus[1]), int(args.focus[2])
        except ValueError:
            parser.error("--focus START and END must be integers.")
    paths = list(args.note)
    if focus_path is not None:
        paths.append(focus_path)
    if not paths:
        paths = [Path(__file__).resolve().parent / "examples" / "project-notes.md"]
    try:
        sources = load_sources(paths)
    except (OSError, UnicodeError, ValueError) as exc:
        parser.exit(status=1, message=f"Cannot read notes: {exc}\n")
    for source in sources:
        print(f"Source: {source.path} | version: {source.source_id[:12]}")
    print()
    retrieved_chunks = None
    if args.search is not None or args.rag:
        from knowledge_tools import KnowledgeTools
        from retrieval import format_hits
        print("步骤 1：本地检索，随后进行云端问答。" if args.rag else "Mode: local semantic search (no cloud model call).", flush=True)
        try:
            tools = KnowledgeTools(
                sources, embedding_config, max_chars=args.chunk_size, min_chars=args.chunk_min,
                no_cache=args.no_cache, rebuild_index=args.rebuild_index,
            )
            result = tools.search_notes(args.search if args.search is not None else args.question, args.top_k)
        except (OSError, ValueError, RuntimeError) as exc:
            parser.exit(status=1, message=f"Cannot search notes: {exc}\n")
        print(f"Embedding: {result.model_name} | token limit: {result.max_tokens}")
        if args.no_cache:
            print(f"Index disabled: computed {result.computed} document vectors.")
        else:
            print(f"Index: reused {result.reused}, computed {result.computed} document vectors.")
        print(f"Chunks: {len(tools.chunks)} | dimensions: {result.dimensions}")
        print("Similarity ranks candidates; it is not an answer confidence score.")
        print(format_hits(result.hits, sources, args.chunk_size))
        if not args.rag:
            return
        retrieved_chunks = [hit.chunk for hit in result.hits]
        print("\n检索完成：将以上片段发送给对话模型生成回答。", flush=True)
    if args.chunks:
        chunks = [chunk for source in sources for chunk in chunk_source(source, args.chunk_size, args.chunk_min)]
        print(f"Mode: local chunk preview (no model call). Total: {len(chunks)}")
        print(format_chunks(chunks, sources, args.chunk_size))
        return
    if args.question is None:
        print("Mode: local file preview (no model call).")
        print(format_sources(sources))
        return
    print("模式：仅依据资料" if args.strict else "模式：资料优先，允许模型补充")
    print(f"Question: {args.question}\n", flush=True)
    try:
        if args.agent:
            from agent_loop import focus_on_lines, run_agent
            from knowledge_tools import KnowledgeTools

            tools = KnowledgeTools(
                sources, embedding_config, max_chars=args.chunk_size, min_chars=args.chunk_min,
                no_cache=args.no_cache, rebuild_index=args.rebuild_index,
            )
            focus = None
            if focus_path is not None:
                source = next(source for source in sources if source.path == focus_path)
                focus = focus_on_lines(tools, source.source_id, focus_start, focus_end)
                print(
                    f"Focus: {source.path} | selected L{focus_start}-L{focus_end} | "
                    f"visible context L{focus.chunk.line_start}-L{focus.chunk.line_end}",
                    flush=True,
                )
            run = run_agent(
                sources, args.question, tools, strict=args.strict,
                max_tool_calls=args.max_tool_calls,
                focus=focus,
            )
            for number, step in enumerate(run.steps, 1):
                state = f"error: {step.error}" if step.error else "ok"
                print(f"Tool {number}: {step.name} {step.arguments} → {state}")
            print(f"Agent stopped: {run.stop_reason}; visible excerpts: {len(run.visible_chunks)}\n")
            answer = run.answer
        elif args.rag:
            answer = answer_question(sources, args.question, strict=args.strict, chunks=retrieved_chunks)
        else:
            answer = answer_question(sources, args.question, strict=args.strict)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(status=1, message=f"Cannot answer question: {exc}\n")
    print(render_answer(answer, sources))


if __name__ == "__main__":
    main()
