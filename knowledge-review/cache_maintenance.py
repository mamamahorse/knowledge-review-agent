"""Best-effort cleanup of explicitly owned scratch files and rebuildable indexes."""
from contextlib import closing,ExitStack,contextmanager
import logging
from pathlib import Path
import re
import shutil
import sqlite3
import time
from hashlib import sha256
from job_lock import job_lock,JobBusy


@contextmanager
def temporary_use(path,role='request'):
    path=Path(path).resolve()
    key=sha256((str(path)+'\0'+role).encode()).hexdigest()[:32]
    with job_lock(path.parent/'.temporary',key): yield


def cleanup(root,index_path=None,*,now=None):
    """Run at application startup, bounded and safe to fail. Never traverses notes."""
    root=Path(root).resolve(); now=time.time() if now is None else now
    result=dict(files=0,index_scopes=0,vectors=0,skipped=0)
    candidates=[]
    if root.is_dir():
        for path in root.iterdir():
            if re.fullmatch(r'(object-query-|search-)[0-9a-f]{32}\.sqlite3',path.name):
                candidates.append((path,root,24*3600))
            if len(candidates)>=200: break
    staging=root/'pdf-import-staging'
    if staging.is_dir() and not staging.is_symlink() and not staging.is_junction():
        for path in staging.iterdir():
            if re.fullmatch('[0-9a-f]{32}',path.name): candidates.append((path,staging,7*86400))
            if len(candidates)>=200: break
    for path,parent,age in candidates:
        try:
            if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(parent.resolve()): continue
            if now-path.stat().st_mtime<age: continue
            with ExitStack() as stack:
                stack.enter_context(temporary_use(path))
                stack.enter_context(temporary_use(path,'parser'))
                if path.is_dir():
                    # Upload staging is flat (raw file + document.json); an unexpected
                    # nested tree is not ours to recursively infer ownership of.
                    if any(p.is_dir() or p.is_symlink() or p.is_junction() for p in path.iterdir()): continue
                    shutil.rmtree(path)
                else: path.unlink(missing_ok=True)
                result['files']+=1
        except (OSError,ValueError): result['skipped']+=1
    if index_path and Path(index_path).is_file() and not Path(index_path).is_symlink():
        try:
            with closing(sqlite3.connect(index_path,timeout=.1)) as db:
                tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                with db:
                    if 'ar_runs' in tables:
                        db.execute('CREATE TABLE IF NOT EXISTS ar_access(scope TEXT PRIMARY KEY,last_used REAL NOT NULL)')
                        db.execute('INSERT OR IGNORE INTO ar_access SELECT scope,? FROM ar_runs',(now,))
                        tables.add('ar_access')
                    if 'vectors' in tables:
                        db.execute('CREATE TABLE IF NOT EXISTS vector_access(profile_id TEXT,chunk_id TEXT,last_used REAL NOT NULL,PRIMARY KEY(profile_id,chunk_id))')
                        db.execute('INSERT OR IGNORE INTO vector_access SELECT profile_id,chunk_id,? FROM vectors',(now,))
                        tables.add('vector_access')
                if {'ar_access','ar_runs','ar_terms','ar_entries'}<=tables:
                    rows=list(db.execute('SELECT scope FROM ar_access WHERE last_used<? LIMIT 100',(now-7*86400,)))
                    for (scope,) in rows:
                        try:
                            with job_lock(index_path,scope[:32]),db:
                                db.execute('BEGIN IMMEDIATE')
                                row=db.execute('SELECT last_used FROM ar_access WHERE scope=?',(scope,)).fetchone()
                                if row and row[0]<now-7*86400:
                                    for table in ('ar_terms','ar_entries','ar_runs','ar_access'):
                                        db.execute(f'DELETE FROM {table} WHERE scope=?',(scope,))
                                    result['index_scopes']+=1
                        except (JobBusy,sqlite3.Error): result['skipped']+=1
                if {'vector_access','vectors'}<=tables:
                    with db:
                        db.execute('BEGIN IMMEDIATE')
                        rows=list(db.execute('SELECT profile_id,chunk_id FROM vector_access WHERE last_used<? LIMIT 1000',(now-7*86400,)))
                        for profile,chunk in rows:
                            db.execute('DELETE FROM vectors WHERE profile_id=? AND chunk_id=?',(profile,chunk))
                            db.execute('DELETE FROM vector_access WHERE profile_id=? AND chunk_id=?',(profile,chunk))
                        result['vectors']=len(rows)
        except (OSError,sqlite3.Error): result['skipped']+=1
    logging.getLogger(__name__).info('Temporary cache maintenance: %s',result)
    return result
