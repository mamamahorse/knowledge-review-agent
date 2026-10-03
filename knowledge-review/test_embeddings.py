import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from chunking import Chunk
from embeddings import LocalEmbedder
from embedding_config import EmbeddingConfig, select_embedding_config, effective_token_limit
from context import validate_chunks
from sources import Source


class EmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.chunk = Chunk("chunk", "source", ("操作系统", "死锁"), 5, 7, "互相等待资源。")
        self.embedder = LocalEmbedder.__new__(LocalEmbedder)
        self.embedder.config = select_embedding_config()
        self.embedder.max_tokens = 512
        self.embedder.model = Mock()
        self.embedder.model.max_seq_length = 512
        self.embedder.model.tokenizer.return_value = {"input_ids": [1, 2, 3]}
        self.embedder.model.encode.return_value.tolist.return_value = [[1.0, 0.0]]

    def test_role_prefixes_and_heading_context(self):
        original = self.chunk.text
        self.embedder.embed_chunks([self.chunk])
        sent = self.embedder.model.encode.call_args.args[0][0]
        self.assertEqual(sent, "passage: 操作系统 > 死锁\n互相等待资源。")
        self.assertEqual(self.chunk.text, original)
        self.embedder.embed_query("  为什么会卡住？  ")
        self.assertEqual(self.embedder.model.encode.call_args.args[0], ["query: 为什么会卡住？"])

    def test_no_silent_truncation(self):
        self.embedder.model.tokenizer.return_value = {"input_ids": list(range(513))}
        with self.assertRaisesRegex(ValueError, "L5-L7"):
            self.embedder._encode([self.chunk.text], ["Chunk chunk (L5-L7)"])
        self.embedder.model.encode.assert_not_called()
        with self.assertRaisesRegex(ValueError, "Query"):
            self.embedder._encode(["too long"], ["Query"])

    def character_tokenizer(self, limit=48):
        self.embedder.max_tokens = limit
        self.embedder.model.tokenizer.side_effect = lambda text, **kwargs: {
            "input_ids": list(range(len(text) + 2)),
        }
        self.embedder.model.encode.side_effect = lambda texts, **kwargs: SimpleNamespace(
            tolist=lambda: [[1.0, 0.0] for _ in texts],
        )

    def test_long_single_line_and_query_keep_all_text_in_bounded_windows(self):
        self.character_tokenizer()
        body = "中文 TCP ACK SEQ 代码😀" * 20 + "末尾独有事实"
        chunk = Chunk("long", "source", (), 1, 1, body)
        self.assertEqual(self.embedder.embed_chunks([chunk]), [[1.0, 0.0]])
        sent = self.embedder.model.encode.call_args.args[0]
        self.assertGreater(len(sent), 1)
        self.assertTrue(all(text.startswith("passage: ") for text in sent))
        self.assertTrue(all(len(text) + 2 <= 48 for text in sent))
        self.assertEqual("".join(text[len("passage: "):] for text in sent), body)
        self.assertEqual(chunk.text, body)
        self.embedder.embed_query(body)
        sent = self.embedder.model.encode.call_args.args[0]
        self.assertTrue(all(text.startswith("query: ") for text in sent))
        self.assertTrue(all(len(text) + 2 <= 48 for text in sent))
        self.assertEqual("".join(text[len("query: "):] for text in sent), body)

    def test_long_code_block_splits_into_exact_original_line_ranges(self):
        self.character_tokenizer(limit=64)
        lines = ["# Code", "```py"] + [f"print('中文内容{i}')" for i in range(12)] + ["```"]
        source = Source("source", Path("code.md"), "\n".join(lines))
        chunk = Chunk("block", "source", ("Code",), 1, len(lines), source.text)
        parts = self.embedder.prepare_chunks([chunk])
        self.assertGreater(len(parts), 1)
        validate_chunks(parts, [source])
        self.assertEqual("\n".join(part.text for part in parts), source.text)
        self.assertEqual(parts[0].line_start, 1)
        self.assertEqual(parts[-1].line_end, len(lines))
        self.assertTrue(all(self.embedder.fits("passage: Code\n" + part.text) for part in parts))
        self.assertEqual(len(self.embedder.embed_chunks(parts)), len(parts))

    def test_long_heading_is_encoded_completely_with_its_body(self):
        self.character_tokenizer()
        chunk = Chunk("heading", "source", ("超长标题" * 40,), 1, 1, "末尾正文")
        self.embedder.embed_chunks([chunk])
        sent = self.embedder.model.encode.call_args.args[0]
        self.assertEqual("".join(text[len("passage: "):] for text in sent),
                         "超长标题" * 40 + "\n末尾正文")

    def test_empty_inputs(self):
        self.assertEqual(self.embedder.embed_chunks([]), [])
        self.embedder.model.encode.assert_not_called()
        with self.assertRaises(ValueError):
            self.embedder.embed_query("   ")

    def test_loader_defaults_to_local_cpu_and_explicit_download(self):
        constructor = Mock()
        constructor.return_value.max_seq_length = 512
        module = SimpleNamespace(SentenceTransformer=constructor)
        with patch.dict("sys.modules", {"sentence_transformers": module}):
            LocalEmbedder()
            self.assertTrue(constructor.call_args.kwargs["local_files_only"])
            self.assertFalse(constructor.call_args.kwargs["trust_remote_code"])
            self.assertEqual(constructor.call_args.kwargs["device"], "cpu")
            LocalEmbedder(allow_download=True)
            self.assertFalse(constructor.call_args.kwargs["local_files_only"])

    def test_select_models_and_read_each_models_limit(self):
        for name, limit in [("e5-small", 512), ("multilingual-minilm", 128)]:
            constructor = Mock()
            constructor.return_value.max_seq_length = limit
            module = SimpleNamespace(SentenceTransformer=constructor)
            with patch.dict("sys.modules", {"sentence_transformers": module}):
                config = select_embedding_config(name)
                embedder = LocalEmbedder(config)
                self.assertEqual(constructor.call_args.args[0], config.model_name)
                self.assertEqual(embedder.max_tokens, limit)
                embedder = LocalEmbedder(select_embedding_config(name, max_tokens=64))
                self.assertEqual(embedder.max_tokens, 64)
                self.assertEqual(embedder.model.max_seq_length, 64)

    def test_custom_model_requires_input_conventions_and_valid_cap(self):
        with self.assertRaises(ValueError):
            select_embedding_config("custom/model")
        config = select_embedding_config("custom/model", query_prefix="", passage_prefix="")
        self.assertEqual(config, EmbeddingConfig("custom/model", "", ""))
        for requested in (0, -1, 513):
            with self.assertRaises(ValueError):
                effective_token_limit(512, requested)
        with self.assertRaises(ValueError):
            select_embedding_config(max_tokens=0)

    def test_no_prefix_model_and_merged_section_context(self):
        self.embedder.config = select_embedding_config("multilingual-minilm")
        self.embedder.max_tokens = 128
        chunk = Chunk("merged", "source", ("OS",), 1, 8, "Original text",
                      (("OS", "Threads"), ("OS", "Deadlocks")))
        self.embedder.embed_chunks([chunk])
        call = self.embedder.model.encode.call_args
        self.assertEqual(call.args[0], ["OS > Threads / OS > Deadlocks\nOriginal text"])
        self.assertEqual(call.kwargs["prompt"], "")
        self.embedder.embed_query("Question")
        self.assertEqual(self.embedder.model.encode.call_args.args[0], ["Question"])
        self.embedder.model.encode.reset_mock()
        self.embedder.model.tokenizer.return_value = {"input_ids": list(range(129))}
        with self.assertRaisesRegex(ValueError, "128"):
            self.embedder._encode(["Question"], ["Query"])
        self.embedder.model.encode.assert_not_called()


if __name__ == "__main__":
    unittest.main()
