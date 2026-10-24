"""Importing web helpers must not initialize real application storage."""
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


class ImportIsolationTests(unittest.TestCase):
    def run_child(self, code):
        result = subprocess.run(
            [sys.executable, '-B', '-c', textwrap.dedent(code)],
            cwd=Path(__file__).resolve().parent, capture_output=True, text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_factory_import_never_opens_sqlite_or_writes_default_storage(self):
        self.run_child('''
            import os
            from pathlib import Path
            import sys
            from unittest.mock import patch

            private = Path('.data').resolve()
            def protect(event, args):
                if event == 'open':
                    path, mode, flags = args
                    writes = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
                elif event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.rename'):
                    path, writes = args[0], True
                else:
                    return
                if writes and isinstance(path, (str, bytes, os.PathLike)):
                    target = Path(os.fsdecode(path)).resolve()
                    if target == private or private in target.parents:
                        raise AssertionError('Import attempted a default-storage write')

            sys.addaudithook(protect)
            with patch('sqlite3.connect', side_effect=AssertionError('Import opened SQLite')):
                import web_app
                from web_app import create_app, render_markdown
                assert callable(create_app) and callable(render_markdown)
                assert 'app' not in vars(web_app)
        ''')

    def test_explicit_default_app_access_initializes_once_under_concurrency(self):
        self.run_child('''
            from concurrent.futures import ThreadPoolExecutor
            import time
            from unittest.mock import patch
            import web_app

            sentinel = object()
            def make():
                time.sleep(.02)
                return sentinel
            with patch('web_app.create_app', side_effect=make) as factory:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    results = list(pool.map(lambda _: web_app.app, range(16)))
                assert all(value is sentinel for value in results)
                factory.assert_called_once_with()
                assert web_app.app is sentinel
                from uvicorn.importer import import_from_string
                assert import_from_string('web_app:app') is sentinel
            try:
                web_app.unknown_public_attribute
            except AttributeError:
                pass
            else:
                raise AssertionError('Unknown attributes must remain unknown')
        ''')


if __name__ == '__main__':
    unittest.main()
