"""Derived object navigation. Descriptions never enter original-text evidence."""
from contextlib import closing,ExitStack
from pathlib import Path
import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

from archive_retrieval import encoded,digest
from source_reader import read_lines
from chunking import Chunk
from agent_cancel import checkpoint


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS object_descriptions(id TEXT PRIMARY KEY,source_id TEXT,payload TEXT,checksum TEXT)')
    db.execute('CREATE INDEX IF NOT EXISTS object_source ON object_descriptions(source_id)')


def publish(state,db):
    """Runs inside the already-authorized job publication transaction."""
    if not state.get('events'): return
    event=state['events'][-1]
    if event.get('action')!='read': return
    value=event.get('output',{}); source_id=state['inputs']['snapshot']['source_id']
    if value.get('source_id')!=source_id: return
    records=[]
    if event['tool']=='discover_visual':
        for obj in value['objects']:
            unit=next((u for u in state['units'] if (u['page']==obj['page'] if obj['page'] is not None else obj['root_asset_id'] in u['asset_ids'])),None)
            if unit:
                records.append(dict(source_id=source_id,asset_id=obj['asset_id'],kind=obj['kind'],page=obj['page'],bbox=obj['bbox'],
                    line_start=unit['line_start'],line_end=unit['line_end'],label=obj['label'],
                    description=obj.get('description',obj['label'])+'\n'+obj['uncertainty'],origin='visual_discovery',
                    limitations=obj['uncertainty'],model=value.get('model'),endpoint=value.get('endpoint')))
    elif event['tool']=='inspect_visual':
        for view in value.get('viewed_assets',[value]):
            records.append(dict(source_id=source_id,asset_id=view['asset_id'],kind='visual',page=view['page'],bbox=view.get('bbox'),
                line_start=view['line_start'],line_end=view['line_end'],label='Visual reading',
                description=value['observation']+'\n'+encoded(value.get('formulas',[])),origin='visual_observation',
                limitations=value['limitations'],reading_question=event.get('arguments_json',''),
                model=state['inputs'].get('vision_model',{}).get('name')))
    for record in records:
        record['origin_job']=state['id']
        record['origin_event']=len(state['events'])-1
    save_records(db,records)


def save_records(db,records):
    """Shared navigation publication; callers own the transaction, never citation grants."""
    initialize(db)
    for record in records:
        payload=encoded(record)
        identity=digest(encoded({k:record.get(k) for k in ('source_id','asset_id','block_id','origin','description','limitations','reading_question','viewed_assets')}))
        db.execute('INSERT OR IGNORE INTO object_descriptions VALUES (?,?,?,?)',
                   (identity,record['source_id'],payload,digest(payload)))


def observation_records(value,question=None):
    # A multi-image observation can describe relationships across views. Preserve
    # that scope instead of pretending it describes each crop independently.
    return [dict(source_id=value['source_id'],asset_id=view['asset_id'],kind='visual',page=view['page'],
        bbox=view.get('bbox'),line_start=view['line_start'],line_end=view['line_end'],label='Visual reading',
        description=value['observation']+'\n'+encoded(value.get('formulas',[])),origin='visual_observation',
        limitations=value.get('limitations',''),viewed_assets=value.get('viewed_assets',[view]),
        next_read=value.get('next_read',''),suggested_regions=value.get('suggested_regions',[]),
        suggested_pages=value.get('suggested_pages',[]),reading_question=question)
        for view in value.get('viewed_assets',[value])]


def publish_observation(path,value,question=None):
    """Best-effort derived indexing must not turn a successful read into a failure."""
    if not path: return
    try:
        with closing(sqlite3.connect(path,timeout=1)) as db,db:
            save_records(db,observation_records(value,question))
    except (OSError,sqlite3.Error,ValueError,KeyError):
        import logging
        logging.getLogger(__name__).warning('Visual navigation publication failed; original reading remains valid.')


def native(source):
    if not source.document: return
    for b in source.document.blocks:
        checkpoint()
        if b.kind not in ('table','formula','figure') and not b.asset_ids: continue
        if b.kind=='page': continue
        # Neighbourhood is a positional hint, not a claim that prose describes it.
        start=max(1,b.line_start-2); end=min(b.line_end,b.line_start+3)
        context=read_lines(source,start,end)
        for asset in b.asset_ids or (None,):
            yield dict(source_id=source.source_id,asset_id=asset,block_id=b.block_id,kind=b.kind,
                page=b.locations[0].page if b.locations else None,line_start=b.line_start,line_end=b.line_end,
                label=' / '.join(b.heading_path) or b.kind,description=' / '.join(b.heading_path)+'\n'+context,
                origin='source_context',limitations='Partial neighbouring source context; not proof of object meaning or complete coverage.')


def search(tools,query,top_k,trace):
    """Spool the permitted representation snapshot, then reuse bounded ranking."""
    from archive_retrieval import search as rank
    parent=Path(tools.index_path or Path(__file__).resolve().parent/'.data/embeddings.sqlite3').parent
    parent.mkdir(parents=True,exist_ok=True)
    path=parent/('object-query-'+uuid4().hex+'.sqlite3')
    source_map={s.source_id:s for s in tools.sources}
    from cache_maintenance import temporary_use
    lease=ExitStack(); lease.enter_context(temporary_use(path))
    try:
        with closing(sqlite3.connect(path)) as spool:
            spool.execute('CREATE TABLE records(seq INTEGER PRIMARY KEY,payload TEXT)')
            def add(record):
                if record['source_id'] not in source_map: raise ValueError('对象表示来源不属于当前权限。')
                scope=tools.reading_range
                if scope and (record['line_start']>scope[1] or record['line_end']<scope[0]): return
                spool.execute('INSERT INTO records(payload) VALUES (?)',(encoded(record),))
            for source in tools.sources:
                for record in native(source): add(record)
            if tools.object_db and Path(tools.object_db).is_file():
                uri=Path(tools.object_db).resolve().as_uri()+'?mode=ro'
                with closing(sqlite3.connect(uri,uri=True)) as db:
                    db.execute('BEGIN')
                    exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='object_descriptions'").fetchone()
                    if exists:
                        for source_id in source_map:
                            for payload,checksum in db.execute('SELECT payload,checksum FROM object_descriptions WHERE source_id=? ORDER BY rowid',(source_id,)):
                                if digest(payload)!=checksum: raise ValueError('对象描述缓存损坏。')
                                record=json.loads(payload)
                                if record['source_id']!=source_id: raise ValueError('对象表示版本身份不匹配。')
                                add(record)
            spool.commit()
            fingerprint=__import__('hashlib').sha256(b'object-representation-v2-labels')
            for row in spool.execute('SELECT payload FROM records ORDER BY seq'): fingerprint.update((row[0]+'\n').encode())
            count=spool.execute('SELECT COUNT(*) FROM records').fetchone()[0]
            trace['representations']=count
            if not count: return []
            class Representations:
                source_id='objects-'+fingerprint.hexdigest()
                is_representation=True
                def groups(self):
                    batch=[]; number=1
                    for (payload,) in spool.execute('SELECT payload FROM records ORDER BY seq'):
                        record=json.loads(payload)
                        batch.append(Chunk(digest(payload),record['source_id'],(),record['line_start'],record['line_end'],
                            record.get('label','')+'\n'+record['description'],block_id=payload,kind='object_representation'))
                        if len(batch)==16:
                            yield number,batch; number+=1; batch=[]
                    if batch: yield number,batch
            adapter=SimpleNamespace(sources=[Representations()],index_path=tools.index_path,embedder=tools.embedder,
                max_chars=tools.max_chars,min_chars=tools.min_chars,no_cache=tools.no_cache,rebuild_index=tools.rebuild_index,
                embedding_config=tools.embedding_config,retrieval_mode=tools.retrieval_mode,representation_mode=True)
            ranked=rank(adapter,query,50,trace); result=[]; seen=set()
            for hit in ranked.hits:
                record=json.loads(hit.chunk.block_id); identity=(record['source_id'],record.get('asset_id') or record['block_id'])
                if identity in seen: continue
                seen.add(identity)
                record.update(score=hit.score,score_kind='rrf' if tools.retrieval_mode=='hybrid' else 'cosine',
                    object_id=digest(encoded(identity)),
                    notice='Navigation only. Read the original object before citing; this is not evidence.',
                    suggested_tool='inspect_visual' if record.get('asset_id') else 'read_table' if record['kind']=='table' else 'read_source')
                result.append(record)
                if len(result)>=top_k: break
            return result
    finally:
        try: path.unlink(missing_ok=True)
        finally: lease.close()
