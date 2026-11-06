from io import BytesIO
import shutil
import unittest
from unittest.mock import patch
from PIL import Image

from test_support import temporary_notes
from markdown_images import import_images,render,relative_name
from sources import load_sources
from knowledge_tools import KnowledgeTools
from embedding_config import select_embedding_config
from pdf_vision import VisualReading
from learning_planner import navigation,remember,covered


def png(color):
    buf=BytesIO(); Image.new('RGB',(80,60),color).save(buf,format='PNG'); return buf.getvalue()


class MarkdownImageTests(unittest.TestCase):
    def setUp(self):
        self.temp=temporary_notes(); self.root=self.temp.__enter__()
        self.addCleanup(self.temp.__exit__,None,None,None)
        self.addCleanup(self.clean)
        self.path=self.root/'note.md'; self.text='Two diagrams.\n\n![a](a.png) ![b](b.png)'
        self.path.write_text(self.text,encoding='utf-8')
        import_images(self.path,self.text,{'a.png':png('red'),'b.png':png('blue')})
        self.source=load_sources([self.path])[0]

    def clean(self):
        for p in self.root.iterdir():
            if p.is_dir(): shutil.rmtree(p)

    def test_versions_crop_and_old_image_survive_manifest_replacement(self):
        old=self.source; asset=old.document.assets[0]
        full,_,_=render(old,asset.asset_id)
        _,crop,_=render(old,asset.asset_id,[0,0,.5,.5])
        self.assertEqual(render(old,crop.asset_id)[1].bbox,(0,0,.5,.5))
        import_images(self.path,self.text,{'a.png':png('green'),'b.png':png('blue')})
        new=load_sources([self.path])[0]
        self.assertNotEqual(new.source_id,old.source_id)
        self.assertEqual(render(old,asset.asset_id)[0],full)
        with self.assertRaises(ValueError): render(new,crop.asset_id)

    def test_real_tool_receipts_distinguish_images_and_partial_views(self):
        tools=KnowledgeTools([self.source],select_embedding_config()); units=[u for u in navigation(self.source) if u['kind']=='image']
        state=dict(evidence={})
        with patch('pdf_vision.vision_request',return_value=VisualReading(observation='Red conditions.',limitations='Controlled.')):
            partial=tools.execute('inspect_visual',dict(source_id=self.source.source_id,asset_id=units[0]['asset_ids'][0],question='Read',bbox=[0,0,.5,.5]))
            remember(state,self.source,partial)
            self.assertFalse(covered(next(iter(state['evidence'].values())),units[0]))
            full=tools.execute('inspect_visual',dict(source_id=self.source.source_id,asset_id=units[0]['asset_ids'][0],question='Read'))
            remember(state,self.source,full)
        ref=next(r for r in state['evidence'].values() if r['bbox'] is None)
        self.assertTrue(covered(ref,units[0])); self.assertFalse(covered(ref,units[1]))
        from learning_pipeline import selected_evidence
        selected_evidence(state,[k for k,v in state['evidence'].items() if v is ref],self.source,path=['evidence'])
        from learning_planner import PlanningStore
        from learning_pipeline import resolve_exercise,ExerciseStep,commit_exercise
        from practice_store import public_session,archived_sources
        from test_learning_pipeline import LearningPipelineTests
        store=PlanningStore(self.root/'practice.sqlite3')
        with patch('learning_planner.model_settings',return_value=('test',dict(api_key='test'))):
            identity=store.create(self.source,'Read images','images-test')
        job=store.get(identity); job['evidence']=state['evidence']
        goal=LearningPipelineTests.goal(None,job)
        job.update(stage='question',question_goal_id='g',attempt='current',status='running',
                   formal=dict(goals={'g':goal},abilities={},reviews={}))
        store.save(job)
        step=ExerciseStep(action='save',exercise=dict(objective='Explain',question='Explain conditions',
            rubric_points=['A','B','C','D','E'],criterion_indices=[1,2,3,4,5],evidence_ids=list(job['evidence'])))
        draft=resolve_exercise(step,job,self.source)
        session=store.practice.get(commit_exercise(store,job,self.source,draft,'current'))
        self.assertEqual(archived_sources(session['snapshots'])[0].source_id,self.source.source_id)
        self.assertEqual(public_session(session)['questions'][0]['citations'][0]['excerpt'],'Red conditions.')

    def test_traversal_unsupported_and_tampered_data_rejected(self):
        for name in ('../a.png','C:/a.png','https://host/a.png'):
            with self.assertRaises(ValueError): relative_name(name)
        with self.assertRaises(Exception): import_images(self.path,self.text,{'a.png':b'not-image'})
        asset=self.source.document.assets[0]; (self.root/asset.uri).write_bytes(png('white'))
        with self.assertRaisesRegex(ValueError,'冻结'): render(self.source,asset.asset_id)

    def test_web_import_render_asset_and_unimported_external_image(self):
        from test_pdf_documents import PDFStorageAndWebTests
        fixture=PDFStorageAndWebTests('runTest'); fixture.setUp()
        try:
            client=fixture.client
            response=client.post('/api/documents',files=[('files',('note.md',self.text+'\n\n![remote](https://example.com/x.png)','text/markdown')),
                ('images',('a.png',png('red'),'image/png')),('images',('b.png',png('blue'),'image/png'))])
            self.assertEqual(response.status_code,201,response.text)
            doc=client.get('/api/documents/'+response.json()[0]['id']).json()
            self.assertEqual(len(doc['assets']),2)
            self.assertNotIn('src="https:',doc['html'])
            self.assertEqual(client.get(doc['assets'][0]['url']).status_code,200)
        finally:
            fixture.client.app.state.stream_runner.pool.shutdown(wait=True)
            fixture.tearDown()


if __name__=='__main__': unittest.main()
