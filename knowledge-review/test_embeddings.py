import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from chunking import Chunk
from embeddings import LocalEmbedder
from embedding_config import EmbeddingConfig, select_embedding_config, effective_token_limit


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
            self.embedder.embed_chunks([self.chunk])
        self.embedder.model.encode.assert_not_called()
        with self.assertRaisesRegex(ValueError, "Query"):
            self.embedder.embed_query("too long")

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
            self.embedder.embed_query("Question")
        self.embedder.model.encode.assert_not_called()


if __name__ == "__main__":
    unittest.main()
