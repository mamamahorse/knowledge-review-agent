from contextlib import closing
import shutil
import sqlite3
import unittest
from unittest.mock import patch

from test_support import temporary_notes
from test_pdf_documents import text_pdf
from test_archive_retrieval import Embedder
from pdf_archive import parse_pdf_stored
from learning_planner import PlanningStore,PlanningStopped,remember
from knowledge_tools import KnowledgeTools


class ObjectRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp=temporary_notes(); self.root=self.temp.__enter__()
        self.addCleanup(self.temp.__exit__,None,None,None); self.addCleanup(self.clean)
        path=self.root/'book.pdf'; path.write_bytes(text_pdf(table=True))
        self.source=parse_pdf_stored(path,self.root/'archive',config=dict(engine='pymupdf',vlm=False))
        settings=patch('learning_planner.model_settings',return_value=('test',dict(api_key='test')))
        settings.start(); self.addCleanup(settings.stop)
        self.store=PlanningStore(self.root/'practice.sqlite3'); self.identity=self.store.create(self.source,'Learn','objects')
        self.embedder=Embedder()

    def clean(self):
        for p in self.root.iterdir():
            if p.is_dir(): shutil.rmtree(p)

    def publish(self,source_id=None):
        job=self.store.get(self.identity)
        job['events'].append(dict(action='read',tool='inspect_visual',arguments_json='{}',output=dict(
            source_id=source_id or self.source.source_id,asset_id='page-2',page=2,line_start=self.source.document.page(2)[0],
            line_end=self.source.document.page(2)[0],observation='Rareformula violet relationship',limitations='Question-specific partial reading',
            formulas=[dict(latex='x_i',variables=['x'],conditions=['positive'],unreadable=[])])))
        self.store.save(job)

    def tools(self,**kwargs):
        return KnowledgeTools([self.source],self.embedder.config,embedder=self.embedder,index_path=self.root/'vectors.sqlite3',
            object_db=self.store.practice.path,retrieval_mode='hybrid',**kwargs)

    def test_saved_visual_description_is_searchable_but_never_visible_evidence(self):
        self.publish()
        tools=self.tools()
        with patch('pdf_vision.vision_request',side_effect=AssertionError('no paid search')):
            execution=tools.execute('search_notes',dict(query='Rareformula violet',top_k=10))
        objects=execution.output['object_candidates']
        found=next(o for o in objects if o['origin']=='visual_observation')
        self.assertEqual(found['asset_id'],'page-2')
        self.assertNotIn('evidence_id',found)
        self.assertTrue(all(c.kind!='object_representation' for c in execution.visible_chunks))
        self.assertTrue(all('Rareformula violet' not in c.text for c in execution.visible_chunks))
        state=self.store.get(self.identity); remember(state,self.source,execution)
        self.assertTrue(all(r['kind']=='source_text' for r in state['evidence'].values()))
        self.assertFalse(list(self.root.glob('object-query-*.sqlite3')))

    def test_primary_range_excludes_auxiliary_objects_and_warm_reuses(self):
        self.publish(); first=self.source.document.page(1)
        tools=self.tools(reading_range=first)
        result=tools.execute('search_notes',dict(query='Rareformula violet',top_k=10))
        self.assertTrue(all(o['line_start']<=first[1] for o in result.output['object_candidates']))
        before=self.embedder.calls
        again=tools.execute('search_notes',dict(query='Rareformula violet',top_k=10))
        self.assertEqual(again.output['object_search']['computed_vectors'],0)
        self.assertEqual(self.embedder.calls,before)
        expanded=tools.execute('search_notes',dict(query='Rareformula violet',top_k=10,scope='document'))
        self.assertTrue(any(o['origin']=='visual_observation' for o in expanded.output['object_candidates']))
        self.assertEqual(expanded.output['search_scope'],'document')

    def test_cancelled_publication_does_not_publish_object_description(self):
        state=self.store.get(self.identity); state.update(status='running',attempt='old'); self.store.save(state)
        self.store.pause(self.identity)
        state['events'].append(dict(action='read',tool='inspect_visual',output=dict(source_id=self.source.source_id,
            asset_id='page-1',page=1,line_start=1,line_end=1,observation='Should never publish',limitations='')))
        with self.assertRaises(PlanningStopped): self.store.save(state,attempt='old')
        with self.store.practice.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM object_descriptions').fetchone()[0],0)

    def test_corrupt_object_cache_does_not_discard_valid_original_hits(self):
        self.publish()
        with closing(sqlite3.connect(self.store.practice.path)) as db,db:
            db.execute("UPDATE object_descriptions SET payload='corrupt'")
        result=self.tools().execute('search_notes',dict(query='Second-page',top_k=3))
        self.assertTrue(result.visible_chunks)
        self.assertEqual(result.output['object_search']['status'],'failed')
        self.assertEqual(result.output['object_candidates'],[])


if __name__=='__main__': unittest.main()
