from contextlib import closing
import shutil
import sqlite3
import unittest
from unittest.mock import patch

from embedding_config import select_embedding_config
from knowledge_tools import KnowledgeTools
from pdf_archive import parse_pdf_stored
from sources import Source
from source_reader import range_chunks
from hybrid_retrieval import BM25Index,HybridConfig,reciprocal_rank_fusion
from retrieval import rank_chunks
from test_pdf_documents import text_pdf
from test_support import temporary_notes


class Embedder:
    config=select_embedding_config()
    max_tokens=512
    def __init__(self): self.calls=0; self.fail=False
    def cache_profile(self): return dict(test='page-index-v1')
    def embed_query(self,query): return [1.,1.]
    def prepare_chunks(self,chunks): return chunks
    def embed_chunks(self,chunks):
        if self.fail: raise RuntimeError('Interrupted')
        self.calls+=len(chunks)
        return [[1.,float('Second-page' in c.text)] for c in chunks]


class ArchiveRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp=temporary_notes(); self.root=self.temp.__enter__()
        self.addCleanup(self.temp.__exit__,None,None,None); self.addCleanup(self.clean)
        path=self.root/'book.pdf'; path.write_bytes(text_pdf(table=True))
        self.source=parse_pdf_stored(path,self.root/'archive',config=dict(engine='pymupdf',vlm=False))
        self.path=self.root/'index.sqlite3'; self.embedder=Embedder()

    def clean(self):
        for p in self.root.iterdir():
            if p.is_dir(): shutil.rmtree(p)

    def tools(self,sources=None,**kwargs):
        return KnowledgeTools(sources or [self.source],self.embedder.config,embedder=self.embedder,index_path=self.path,**kwargs)

    def test_hybrid_matches_existing_scoped_bm25_rrf_and_reopen_reuses(self):
        tools=self.tools(retrieval_mode='hybrid')
        self.assertEqual(tools.chunks,[])
        result=tools.search_notes('Second-page',3)
        chunks=[c for p in range(1,3) for c in range_chunks(self.source,*self.source.document.page(p))]
        dense=rank_chunks(chunks,self.embedder.embed_chunks(chunks),[1.,1.],50)
        lexical=BM25Index(chunks).search('Second-page',50)
        expected,_=reciprocal_rank_fusion(chunks,dense,lexical,HybridConfig())
        self.assertEqual([(h.chunk.chunk_id,h.score) for h in result.hits],[(h.chunk.chunk_id,h.score) for h in expected[:3]])
        before=self.embedder.calls
        again=self.tools(retrieval_mode='hybrid').search_notes('Second-page',3)
        self.assertEqual(self.embedder.calls,before)
        self.assertEqual(again.computed,0)
        execution=tools.execute('search_notes',dict(query='Requests per second',top_k=3))
        from context import validate_chunks
        validate_chunks(list(execution.visible_chunks),[self.source])

    def test_mixed_scope_omission_cannot_search_cached_other_source(self):
        secret=Source('private',self.root/'secret.md','Secret unique private fact')
        both=self.tools([self.source,secret]); both.search_notes('private')
        current=self.tools(); result=current.search_notes('private',10)
        self.assertTrue(all(h.chunk.source_id==self.source.source_id for h in result.hits))
        trace=current.retrieval_trace[-1]
        self.assertEqual(trace['source_ids'],[self.source.source_id])

    def test_interruption_never_publishes_partial_and_recovery_finishes(self):
        original=self.embedder.embed_chunks
        def interrupted(chunks):
            if any('Second-page' in c.text for c in chunks): raise RuntimeError('Interrupted page')
            return original(chunks)
        with patch.object(self.embedder,'embed_chunks',side_effect=interrupted):
            with self.assertRaises(RuntimeError): self.tools().search_notes('query')
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT ready FROM ar_runs').fetchone()[0],0)
        result=self.tools().search_notes('query')
        self.assertTrue(result.hits)

    def test_damage_rejected_and_no_cache_leaves_no_persistent_query_index(self):
        self.tools().search_notes('fact')
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute("UPDATE ar_entries SET payload='{}'")
        with self.assertRaisesRegex(ValueError,'校验'): self.tools().search_notes('fact')
        repaired=self.tools(rebuild_index=True).search_notes('fact')
        self.assertTrue(repaired.hits)
        result=self.tools(no_cache=True).search_notes('fact')
        self.assertTrue(result.hits)
        self.assertEqual(list(self.root.glob('search-*.sqlite3')),[])


if __name__=='__main__': unittest.main()
