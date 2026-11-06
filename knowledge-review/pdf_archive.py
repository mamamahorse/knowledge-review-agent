"""File-backed PyMuPDF extraction with page checkpoints and resumable assembly."""
from contextlib import closing
from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from agent_cancel import checkpoint
from document_model import Asset, ContentBlock, Document, Location, digest, attach_table_context
from source_reader import file_digest, open_snapshot
from pdf_parse_cache import read_page, encoded, write_record

VERSION=2


def page_document(raw_hash,number,items,issues,raw_file,parser):
    """Bounded page adapter; locations keep physical page numbers before rebasing."""
    lines=[f'## 第 {number} 页','']; page_asset=f'page-{number}'
    assets=[Asset(page_asset,'page',number)]
    blocks=[ContentBlock(page_asset,'page',1,1,(Location(number),),(page_asset,),(f'第 {number} 页',),'navigation')]
    for index,item in enumerate(items):
        kind=item['kind']; refs=(); box=tuple(float(v) for v in item['bbox'])
        if kind in ('table','figure'):
            identity=f'{kind}-{number}-{index}'; assets.append(Asset(identity,kind,number,box)); refs=(identity,)
        text=item['text'] if kind!='figure' else f'图像区域（第 {number} 页；可查看原图）'
        start=len(lines)+1; lines.extend(text.splitlines()); end=len(lines)
        if end<start: continue
        positions=tuple(Location(p['page'],tuple(p['bbox'])) for p in item['locations']) if item.get('locations') else (Location(number,box),)
        blocks.append(ContentBlock(digest(f'{raw_hash}:{number}:{index}'),kind,start,end,positions,refs,
            (f'第 {number} 页',),item.get('method',json.loads(parser)['engine']),
            min(start+1,end) if kind=='table' else None,item.get('table_data')))
        lines.append('')
    if not items: lines.extend(['本页没有提取到可检索正文；可查看原始页面。',''])
    # Preserve a final empty line as a real global line, including on the last page.
    text='\n'.join(lines)+'\n'
    return attach_table_context(Document('pdf',raw_hash,parser,text,tuple(blocks),tuple(assets),
                                         number,tuple(issues),raw_file))


def enhance_page(page,parsed,eligible_index,config):
    """Reuse import-time policy: at most three problematic pages, only when enabled.

Successful and failed remote attempts are persisted before assembly. Recovery
never silently repeats a recorded failed/unknown remote attempt.
"""
    result={**parsed,'attempts':[]}
    if not config.get('vlm') or not parsed['issues']: return result
    if eligible_index>3:
        result['issues']=[*parsed['issues'],dict(page=page.number+1,code='vlm_page_budget_exhausted')]
        return result
    from pdf_parser import render_page
    from pdf_vision import transcribe_page, VisionUnsupported
    attempt=dict(page=page.number+1,method='vlm_transcription',original_items=parsed['items'])
    try:
        reading=transcribe_page(render_page(page))
        attempt.update(status='returned',candidate=reading.model_dump())
        blocks=[dict(kind=b.kind,text=b.text,bbox=tuple(page.rect),method='vlm_transcription')
                for b in reading.blocks if b.text.strip()]
        if blocks:
            result['items']=blocks+[i for i in parsed['items'] if i['kind']=='figure']
            result['issues']=[dict(page=page.number+1,code='vlm_unresolved',details=reading.unresolved)] if reading.unresolved else []
        else: result['issues']=[*parsed['issues'],dict(page=page.number+1,code='vlm_no_reliable_text')]
    except Exception as exc:
        from agent_cancel import GenerationCancelled
        if isinstance(exc,GenerationCancelled): raise
        attempt.update(status='unsupported' if isinstance(exc,VisionUnsupported) else 'failed',error_type=type(exc).__name__)
        result['issues']=[*parsed['issues'],dict(page=page.number+1,code='vlm_unsupported' if isinstance(exc,VisionUnsupported) else 'vlm_failed')]
    result['attempts']=[attempt]
    return result


def parse_pdf_stored(path,archive_dir,*,config=None,force=False,identity_path=None,raw_file=None,progress=None):
    from types import SimpleNamespace
    from outline_store import OutlineStore
    root=Path(archive_dir).resolve(); root.mkdir(parents=True,exist_ok=True)
    key=digest(str(Path(identity_path or path).resolve()))[:32]
    owner=SimpleNamespace(practice=SimpleNamespace(path=root/'parse'))
    with OutlineStore.lock(owner,key):
        return _parse_pdf_stored(path,root,config=config,force=force,identity_path=identity_path,raw_file=raw_file,progress=progress)


def _parse_pdf_stored(path,archive_dir,*,config=None,force=False,identity_path=None,raw_file=None,progress=None):
    from pdf_parser import pdf_library,profile,MAX_PDF_BYTES,MAX_PAGES,extract_page
    path=Path(path).resolve(); identity_path=Path(identity_path or path).resolve()
    if not 0<path.stat().st_size<=MAX_PDF_BYTES: raise ValueError('PDF不能为空，且不能超过20MB。')
    config=profile() if config is None else config
    if config['engine']!='pymupdf': raise ValueError('按页存档当前使用PyMuPDF；Docling请使用原解析入口。')
    raw_hash=file_digest(path); parser=encoded(config); raw_file=raw_file or identity_path.name
    root=Path(archive_dir).resolve(); root.mkdir(parents=True,exist_ok=True)
    key=digest(encoded(dict(version=VERSION,raw_hash=raw_hash,config=config,path=str(identity_path),raw_file=raw_file)))
    completed=root/(key+'.json'); building=root/(key+'.building.sqlite3')
    if completed.exists() and not force:
        ref=json.loads(completed.read_text(encoding='utf-8'))
        stored=open_snapshot(ref)
        if stored.file_hash!=raw_hash: raise ValueError('存档原件身份不匹配。')
        if progress: progress(dict(phase='sealed',completed_pages=stored.document.page_count,total_pages=stored.document.page_count))
        return stored
    if force:
        # Explicit reparsing uses a separate build, never mutates a sealed archive.
        from uuid import uuid4
        building=root/(key+'.'+uuid4().hex+'.building.sqlite3')
    with closing(sqlite3.connect(building)) as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS lines(number INTEGER PRIMARY KEY,text TEXT NOT NULL,digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS blocks(seq INTEGER PRIMARY KEY,line_start INTEGER,line_end INTEGER,payload TEXT);
            CREATE INDEX IF NOT EXISTS blocks_range ON blocks(line_start,line_end);
            CREATE TABLE IF NOT EXISTS assets(seq INTEGER PRIMARY KEY,page INTEGER,payload TEXT);
            CREATE INDEX IF NOT EXISTS assets_page ON assets(page);
            CREATE TABLE IF NOT EXISTS pages(number INTEGER PRIMARY KEY,line_start INTEGER,line_end INTEGER);
            CREATE TABLE IF NOT EXISTS issues(seq INTEGER PRIMARY KEY,payload TEXT);
            CREATE TABLE IF NOT EXISTS attempts(page INTEGER PRIMARY KEY,payload TEXT);
            CREATE TABLE IF NOT EXISTS metadata(payload TEXT NOT NULL);''')
        try: handle=pdf_library().open(path)
        except Exception as exc: raise ValueError('PDF损坏或无法打开。') from exc
        with handle:
            if handle.needs_pass or not 1<=len(handle)<=MAX_PAGES: raise ValueError('PDF需要密码、无页面或超过页数上限。')
            next_page=db.execute('SELECT COALESCE(MAX(number),0)+1 FROM pages').fetchone()[0]
            if progress: progress(dict(phase='parsing',completed_pages=next_page-1,total_pages=len(handle)))
            offset=db.execute('SELECT COALESCE(MAX(number),0) FROM lines').fetchone()[0]
            eligible=0
            for number in range(1,len(handle)+1):
                checkpoint(); page=handle[number-1]
                parsed=read_page(root/'pages',raw_hash,config,number,lambda:extract_page(page),force=force)
                if parsed['issues']: eligible+=1
                if number<next_page: continue
                # Persist remote attempts separately from cheap local extraction.
                visual_key=digest(encoded([VERSION,raw_hash,config,number]))
                visual_path=root/'pages'/f'{visual_key}-visual.json'
                if config.get('vlm') and parsed['issues']:
                    if visual_path.exists() and not force:
                        record=json.loads(visual_path.read_text(encoding='utf-8'))
                        if record['status']=='pending':
                            parsed={**parsed,'attempts':[dict(page=number,status='usage_unknown')],
                                'issues':[*parsed['issues'],dict(page=number,code='vlm_failed')]}
                        elif record.get('digest')==digest(encoded(record['result'])): parsed=record['result']
                        else: raise ValueError('视觉处理检查点损坏，未自动重发付费请求。')
                    else:
                        write_record(visual_path,dict(status='pending'))
                        parsed=enhance_page(page,parsed,eligible,config)
                        write_record(visual_path,dict(status='completed',result=parsed,digest=digest(encoded(parsed))))
                local=page_document(raw_hash,number,parsed['items'],parsed['issues'],raw_file,parser)
                lines=local.text.splitlines()
                with db:
                    db.executemany('INSERT INTO lines VALUES (?,?,?)',((offset+i,text,digest(text)) for i,text in enumerate(lines,1)))
                    for b in local.blocks:
                        value=replace(b,line_start=b.line_start+offset,line_end=b.line_end+offset,
                            header_end=b.header_end+offset if b.header_end else None,
                            context_ranges=tuple((a+offset,z+offset) for a,z in b.context_ranges))
                        db.execute('INSERT INTO blocks(line_start,line_end,payload) VALUES (?,?,?)',
                                   (value.line_start,value.line_end,encoded(asdict(value))))
                    db.executemany('INSERT INTO assets(page,payload) VALUES (?,?)',((a.page,encoded(asdict(a))) for a in local.assets))
                    db.executemany('INSERT INTO issues(payload) VALUES (?)',((encoded(i),) for i in parsed['issues']))
                    db.execute('INSERT INTO attempts VALUES (?,?)',(number,encoded(parsed.get('attempts',[]))))
                    db.execute('INSERT INTO pages VALUES (?,?,?)',(number,offset+1,offset+len(lines)))
                offset+=len(lines)
                if progress: progress(dict(phase='parsing',completed_pages=number,total_pages=len(handle)))
            checkpoint()
            if file_digest(path)!=raw_hash: raise ValueError('解析期间原件变化，未发布。')
            # Hash content incrementally, without reading all rows into Python.
            content=sha256()
            for table,columns in (('lines','number,text,digest'),('blocks','payload'),('assets','payload'),('issues','payload'),('attempts','payload')):
                for row in db.execute(f'SELECT {columns} FROM {table} ORDER BY rowid'):
                    content.update((encoded([table,*row])+'\n').encode())
            source_id=digest(encoded([VERSION,str(identity_path),raw_hash,parser,content.hexdigest()]))
            destination=root/(source_id+'.sqlite3')
            data=dict(version=VERSION,state='sealed',source_id=source_id,line_count=offset,file_hash=raw_hash,
                content_hash=content.hexdigest(),document=dict(format='pdf',raw_hash=raw_hash,parser=parser,
                page_count=len(handle),raw_file=raw_file,status='partial' if db.execute('SELECT 1 FROM issues LIMIT 1').fetchone() else 'ready'))
            with db:
                db.execute('DELETE FROM metadata'); db.execute('INSERT INTO metadata VALUES (?)',(encoded(data),))
    checkpoint()
    if destination.exists(): building.unlink()
    else: building.replace(destination)
    ref=dict(source_id=source_id,path=str(identity_path),reader=dict(version=2,archive=str(destination),line_count=offset,file_hash=raw_hash))
    write_record(completed,ref)
    if progress: progress(dict(phase='sealed',completed_pages=data['document']['page_count'],total_pages=data['document']['page_count']))
    return open_snapshot(ref)
