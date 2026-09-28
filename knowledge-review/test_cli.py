import os
import subprocess
import sys
import unittest
import io
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout
from test_support import temporary_notes

ENTRY = Path(__file__).resolve().parent / "main.py"


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(ENTRY), *map(str, args)], capture_output=True,
            encoding="utf-8", env={**os.environ, "LLM_API_KEY": ""}, timeout=15,
        )

    def test_default_preview(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[L9]", result.stdout)

    def test_chunk_preview_is_offline_and_preserves_line_numbers(self):
        with temporary_notes() as directory:
            paths = [directory / "a.md", directory / "b.md"]
            for path in paths:
                path.write_text("# Title\n\nIntro.\n\n## Child\n\nBody.", encoding="utf-8")
            result = self.run_cli(*paths, "--chunks", "--chunk-size", "20", "--chunk-min", "0")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("no model call", result.stdout)
            self.assertIn("[L7] Body.", result.stdout)
            for path in paths:
                self.assertIn(str(path), result.stdout)

    def test_invalid_chunk_options(self):
        for args in [("--chunks", "--question", "test"), ("--chunks", "--chunk-size", "0")]:
            self.assertNotEqual(self.run_cli(*args).returncode, 0)

    def test_invalid_search_options(self):
        for args in [("--search", " "), ("--search", "test", "--top-k", "0"),
                     ("--search", "test", "--question", "test"),
                     ("--download-model", "notes.md")]:
            self.assertNotEqual(self.run_cli(*args).returncode, 0)
        for args in [("--rag",), ("--rag", "--search", "test"),
                     ("--agent",), ("--agent", "--question", "test", "--rag"),
                     ("--agent", "--question", "test", "--max-tool-calls", "0")]:
            self.assertNotEqual(self.run_cli(*args).returncode, 0)

    def test_agent_mode_passes_budget_and_displays_trace(self):
        import main
        from agent_loop import AgentRun, AgentStep
        from answers import NoteAnswer

        class Output(io.StringIO):
            def reconfigure(self, **kwargs):
                pass

        with patch("main.answer_question") as fixed_answer, patch("agent_loop.run_agent") as agent:
            agent.return_value = AgentRun(
                NoteAnswer(coverage="none", coverage_note="No evidence.", blocks=[]),
                (AgentStep("search_notes", {"query": "topic"}),),
                "model_finished", (),
            )
            output = Output()
            argv = [str(ENTRY), "--question", "topic", "--agent", "--strict", "--max-tool-calls", "2"]
            with patch.object(sys, "argv", argv), redirect_stdout(output):
                main.main()
            self.assertEqual(agent.call_args.kwargs["max_tool_calls"], 2)
            self.assertTrue(agent.call_args.kwargs["strict"])
            self.assertIn("Tool 1: search_notes", output.getvalue())
            self.assertIn("Agent stopped: model_finished", output.getvalue())
            fixed_answer.assert_not_called()

    def test_rag_passes_only_retrieved_chunks_and_strict_flag(self):
        import main
        from answers import NoteAnswer

        class Output(io.StringIO):
            def reconfigure(self, **kwargs):
                pass

        with temporary_notes() as directory:
            path = directory / "rag.md"
            path.write_text("# A\n\nAlpha\n\n# B\n\nBeta", encoding="utf-8")
            with patch("knowledge_tools.LocalEmbedder") as factory, patch("main.answer_question") as answer, patch("vector_index.DEFAULT_INDEX", directory / "index.sqlite3"):
                from embedding_config import select_embedding_config
                factory.return_value.config = select_embedding_config()
                factory.return_value.cache_profile.return_value = {"model": "test"}
                factory.return_value.embed_query.return_value = [0, 1]
                factory.return_value.embed_chunks.return_value = [[1, 0], [0, 1]]
                answer.return_value = NoteAnswer(coverage="none", coverage_note="Insufficient excerpts", blocks=[])
                output = Output()
                argv = [str(ENTRY), str(path), "--question", "Beta", "--rag", "--strict", "--top-k", "1", "--chunk-min", "0"]
                with patch.object(sys, "argv", argv), redirect_stdout(output):
                    main.main()
                call = answer.call_args
                self.assertEqual(answer.call_count, 1)
                self.assertTrue(call.kwargs["strict"])
                self.assertEqual(len(call.kwargs["chunks"]), 1)
                self.assertEqual(call.kwargs["chunks"][0].text, "# B\n\nBeta")
                self.assertEqual(call.args[1], "Beta")
                self.assertIn("Insufficient excerpts", output.getvalue())
                answer.reset_mock()
                factory.return_value.embed_query.side_effect = RuntimeError("Embedding failed")
                with patch.object(sys, "argv", argv), redirect_stdout(Output()), patch.object(sys, "stderr", Output()), self.assertRaises(SystemExit):
                    main.main()
                answer.assert_not_called()

    def test_search_wires_embeddings_to_ranked_original_text(self):
        import main

        class Output(io.StringIO):
            def reconfigure(self, **kwargs):
                pass

        with temporary_notes() as directory:
            path = directory / "search.md"
            path.write_text("# A\n\nAlpha\n\n# B\n\nBeta", encoding="utf-8")
            with patch("knowledge_tools.LocalEmbedder") as factory, patch("main.answer_question") as answer, patch("vector_index.DEFAULT_INDEX", directory / "index.sqlite3"):
                from embedding_config import select_embedding_config
                factory.return_value.config = select_embedding_config("multilingual-minilm", max_tokens=64)
                factory.return_value.cache_profile.return_value = {"model": "test"}
                factory.return_value.embed_query.return_value = [0, 1]
                factory.return_value.embed_chunks.return_value = [[1, 0], [0, 1]]
                output = Output()
                with patch.object(sys, "argv", [str(ENTRY), str(path), "--search", "Beta", "--top-k", "1", "--chunk-min", "0", "--embedding-model", "multilingual-minilm", "--embedding-max-tokens", "64"]), redirect_stdout(output):
                    main.main()
                self.assertIn("[L7] Beta", output.getvalue())
                self.assertNotIn("[L3] Alpha", output.getvalue())
                from embedding_config import select_embedding_config
                factory.assert_called_once_with(select_embedding_config("multilingual-minilm", max_tokens=64))
                answer.assert_not_called()
                factory.return_value.embed_chunks.side_effect = AssertionError("Should reuse saved vectors")
                second = Output()
                with patch.object(sys, "argv", [str(ENTRY), str(path), "--search", "Another question", "--top-k", "1", "--chunk-min", "0", "--embedding-model", "multilingual-minilm", "--embedding-max-tokens", "64"]), redirect_stdout(second):
                    main.main()
                self.assertIn("reused 2, computed 0", second.getvalue())
                factory.return_value.embed_chunks.side_effect = None
                uncached = Output()
                with patch.object(sys, "argv", [str(ENTRY), str(path), "--search", "Beta", "--chunk-min", "0", "--no-cache"]), redirect_stdout(uncached), patch("knowledge_tools.get_document_vectors", side_effect=AssertionError("Must not access index")):
                    main.main()
                self.assertIn("Index disabled", uncached.getvalue())
                answer.assert_not_called()

    def test_invalid_index_options(self):
        for args in [("--no-cache",), ("--rebuild-index",), ("--search", "test", "--no-cache", "--rebuild-index")]:
            self.assertNotEqual(self.run_cli(*args).returncode, 0)

    def test_chunk_min_and_embedding_arguments(self):
        for args in [("--chunks", "--chunk-min", "-1"), ("--chunks", "--chunk-min", "1201"),
                     ("--search", "test", "--embedding-max-tokens", "0"),
                     ("--search", "test", "--embedding-model", "unknown")]:
            self.assertNotEqual(self.run_cli(*args).returncode, 0)
        with temporary_notes() as directory:
            path = directory / "dense.md"
            path.write_text("# A\n\nOne\n\n# B\n\nTwo", encoding="utf-8")
            result = self.run_cli(path, "--chunks", "--chunk-min", "30", "--chunk-size", "60")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Total: 1", result.stdout)
            self.assertIn("Section: A / B", result.stdout)
            self.assertIn("[L7] Two", result.stdout)

    def test_multiple_sources_and_strict_flag(self):
        with temporary_notes() as directory:
            paths = [Path(directory) / name for name in ["a.md", "b.md"]]
            for path in paths:
                path.write_text(path.name, encoding="utf-8")
            result = self.run_cli(*paths, "--strict")
            self.assertEqual(result.returncode, 0, result.stderr)
            for path in paths:
                self.assertIn(path.name, result.stdout)

    def test_missing_input_and_blank_question(self):
        with temporary_notes() as directory:
            self.assertNotEqual(self.run_cli(Path(directory) / "missing.md").returncode, 0)
        result = self.run_cli("--question", "   ")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("blank", result.stderr)


if __name__ == "__main__":
    unittest.main()
