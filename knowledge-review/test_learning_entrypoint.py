"""Offline product surface and dependency checks; no generation/model requests."""
import ast
from pathlib import Path
import unittest
from job_lock import job_lock, JobBusy
from task_routing import capabilities
from test_support import temporary_notes


class LearningEntrypointTests(unittest.TestCase):
    def test_active_learning_modules_do_not_import_legacy_services(self):
        forbidden={'outline_service','outline_store','outline_v2','outline_context'}
        for name in ('learning_planner','learning_pipeline','learning_batches','learning_contracts','source_excerpt','job_lock','web_app'):
            tree=ast.parse(Path(name+'.py').read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node,ast.ImportFrom): self.assertNotIn(node.module,forbidden,name)
                if isinstance(node,ast.Import):
                    for alias in node.names: self.assertNotIn(alias.name,forbidden,name)

    def test_product_exposes_only_new_learning_catalog_routes(self):
        from web_app import create_app
        with temporary_notes() as root:
            app=create_app(root/'notes',root/'practice.sqlite3',root/'conversation.sqlite3')
            try:
                paths={route.path for route in app.routes if hasattr(route,'path')}
                self.assertIn('/api/documents/{identity}/learning-plans',paths)
                self.assertIn('/api/learning-plans/{identity}/questions',paths)
                self.assertFalse(any('/outline' in path for path in paths))
                self.assertFalse(hasattr(app.state,'outline_store'))
                page=Path('web/templates/index.html').read_text(encoding='utf-8')
                self.assertNotIn('outline-algorithm',page)
                self.assertNotIn('/static/outlines.js',page)
                tasks={c['id']:c for c in capabilities()}
                self.assertEqual(tasks['extract_outline']['destination'],'learning_plan')
                self.assertNotIn('plan_route',tasks)
                self.assertNotIn('organize_outline',tasks)
            finally:
                app.state.stream_runner.pool.shutdown(wait=True)

    def test_job_lock_is_exclusive_and_releases_without_legacy_store(self):
        with temporary_notes() as root:
            database=root/'practice.sqlite3'
            with job_lock(database,'a'*32):
                with self.assertRaises(JobBusy):
                    with job_lock(database,'a'*32): pass
            with job_lock(database,'a'*32): pass
            with self.assertRaises(KeyError):
                with job_lock(database,'../bad'): pass
