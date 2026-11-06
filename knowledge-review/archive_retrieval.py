"""Disk-backed whole-document retrieval. Only page batches and top hits hold text."""
from collections import Counter
from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
import heapq
import json
from math import log1p
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

from agent_cancel import checkpoint
from chunking import Chunk,chunk_source,with_table_context,CHUNK_ALGORITHM
from source_reader import StoredSource,range_chunks,line_count
from vector_index import get_document_vectors,vector_is_valid
from retrieval import SearchHit,unit_vector
from hybrid_retrieval import lexical_tokens,HybridConfig,reciprocal_rank_fusion,TOKENIZER_VERSION
from outline_store import OutlineStore


def encoded(value): return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
def digest(value): return sha256(value.encode()).hexdigest()


def restore(payload):
    c=json.loads(payload)
    for key in ('heading_path','context_ranges','section_paths'):
        c[key]=tuple(tuple(v) if isinstance(v,list) else v for v in c[key])
    if c['table_header_range'] is not None: c['table_header_range']=tuple(c['table_header_range'])
    return Chunk(**c)


def groups(source,max_chars,min_chars):
    if getattr(source,'is_representation',False):
        yield from source.groups()
        return
    if isinstance(source,StoredSource) and source.version==2:
        for page in range(1,source.document.page_count+1):
            checkpoint()
            yield page,range_chunks(source,*source.document.page(page),max_chars)
    elif isinstance(source,StoredSource):
        for start in range(1,line_count(source)+1,200):
            checkpoint()
            yield start,range_chunks(source,start,min(start+199,line_count(source)),max_chars)
    else:
        yield 1,chunk_source(source,max_chars,min_chars)


def build(db,path,source,embedder,dimensions,max_chars,min_chars,*,no_cache=False,rebuild=False):
    profile=embedder.cache_profile() if not no_cache else dict(uncached=True,model=embedder.config.model_name,max_tokens=embedder.max_tokens)
    scope=digest(encoded(dict(version=1,algorithm=CHUNK_ALGORITHM,source_id=source.source_id,embedding=profile,
                             dimensions=dimensions,max_chars=max_chars,min_chars=min_chars,lexical=TOKENIZER_VERSION)))
    owner=SimpleNamespace(practice=SimpleNamespace(path=path))
    computed=reused=0
    with OutlineStore.lock(owner,scope[:32]):
        if rebuild:
            with db:
                for table in ('ar_terms','ar_entries','ar_runs'): db.execute(f'DELETE FROM {table} WHERE scope=?',(scope,))
        row=db.execute('SELECT cursor,ready FROM ar_runs WHERE scope=?',(scope,)).fetchone()
        if row and row[1]:
            return scope,0,db.execute('SELECT COUNT(*) FROM ar_entries WHERE scope=?',(scope,)).fetchone()[0]
        cursor=row[0] if row else 0
        with db: db.execute('INSERT OR IGNORE INTO ar_runs VALUES (?,0,0)',(scope,))
        for number,chunks in groups(source,max_chars,min_chars):
            if number<=cursor: continue
            # Capacity preparation is the existing tokenizer/table-window logic.
            prepared=embedder.prepare_chunks(chunks) if not getattr(source,'is_representation',False) and callable(getattr(type(embedder),'prepare_chunks',None)) else chunks
            for offset in range(0,len(prepared),16):
                checkpoint(); batch=prepared[offset:offset+16]
                if no_cache:
                    vectors=embedder.embed_chunks(batch); computed+=len(batch)
                else:
                    result=get_document_vectors(batch,embedder,dimensions,max_chars=max_chars,min_chars=min_chars,path=path,rebuild=rebuild)
                    vectors=result.vectors; computed+=result.computed; reused+=result.reused
                if len(vectors)!=len(batch) or any(not vector_is_valid(v,dimensions) for v in vectors):
                    raise ValueError('向量数量、维度或数值无效，整份索引未完成。')
                with db:
                    for chunk,vector in zip(batch,vectors,strict=True):
                        payload=encoded(asdict(chunk)); value=encoded(vector)
                        terms=Counter(lexical_tokens((chunk.retrieval_context+'\n' if chunk.retrieval_context else '')+chunk.text))
                        inserted=db.execute('INSERT OR IGNORE INTO ar_entries(scope,id,payload,vector,checksum,length) VALUES (?,?,?,?,?,?)',
                            (scope,chunk.chunk_id,payload,value,digest(payload+'\0'+value),sum(terms.values()))).rowcount
                        if inserted:
                            db.executemany('INSERT INTO ar_terms VALUES (?,?,?,?)',((scope,chunk.chunk_id,t,n) for t,n in terms.items()))
            with db: db.execute('UPDATE ar_runs SET cursor=? WHERE scope=?',(number,scope))
        checkpoint()
        with db: db.execute('UPDATE ar_runs SET ready=1 WHERE scope=?',(scope,))
    return scope,computed,reused


def search(tools,query,top_k,trace):
    path=Path(tools.index_path or Path(__file__).resolve().parent/'.data/embeddings.sqlite3')
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=tools.no_cache
    if temporary: path=path.with_name('search-'+uuid4().hex+'.sqlite3')
    embedder=tools.embedder; query_vector=unit_vector(embedder.embed_query(query)); dimensions=len(query_vector)
    try:
        with closing(sqlite3.connect(path,timeout=10)) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS ar_runs(scope TEXT PRIMARY KEY,cursor INTEGER,ready INTEGER);
                CREATE TABLE IF NOT EXISTS ar_entries(seq INTEGER PRIMARY KEY,scope TEXT,id TEXT,payload TEXT,vector TEXT,checksum TEXT,length INTEGER,UNIQUE(scope,id));
                CREATE INDEX IF NOT EXISTS ar_scope ON ar_entries(scope);
                CREATE TABLE IF NOT EXISTS ar_terms(scope TEXT,id TEXT,term TEXT,frequency INTEGER,PRIMARY KEY(scope,id,term));
                CREATE INDEX IF NOT EXISTS ar_term ON ar_terms(scope,term);''')
            scopes=[]; computed=reused=0
            for source in tools.sources:
                scope,c,r=build(db,path,source,embedder,dimensions,tools.max_chars,tools.min_chars,no_cache=temporary,rebuild=tools.rebuild_index)
                scopes.append(scope); computed+=c; reused+=r
            placeholders=','.join('?' for _ in scopes); config=HybridConfig()
            db.execute('BEGIN')
            if db.execute(f'SELECT COUNT(*) FROM ar_runs WHERE scope IN ({placeholders}) AND ready=1',scopes).fetchone()[0]!=len(scopes):
                raise ValueError('索引正在重建，未使用不完整范围。')
            count,average=db.execute(f'SELECT COUNT(*),COALESCE(AVG(length),0) FROM ar_entries WHERE scope IN ({placeholders})',scopes).fetchone()
            trace.update(permitted_chunks=count,computed_vectors=computed,reused_vectors=reused,
                         vectors_in_memory=False,index_backend='sqlite-page-batches-v1',protocol=config.protocol())
            scope_order={scope:i for i,scope in enumerate(scopes)}
            limit=config.branch_m if tools.retrieval_mode=='hybrid' else top_k
            best=[]
            for seq,scope,identity,payload,value,checksum in db.execute(f'SELECT seq,scope,id,payload,vector,checksum FROM ar_entries WHERE scope IN ({placeholders})',scopes):
                checkpoint()
                if digest(payload+'\0'+value)!=checksum: raise ValueError('检索索引校验失败，请明确重建索引。')
                vector=json.loads(value)
                if not vector_is_valid(vector,dimensions): raise ValueError('索引向量无效，请重建。')
                score=max(-1.,min(1.,sum(a*b for a,b in zip(unit_vector(vector),query_vector,strict=True))))
                item=(score,-scope_order[scope],-seq,scope,identity)
                if len(best)<limit: heapq.heappush(best,item)
                elif item>best[0]: heapq.heapreplace(best,item)
            def hit(scope,identity,score):
                row=db.execute('SELECT payload,vector,checksum,seq FROM ar_entries WHERE scope=? AND id=?',(scope,identity)).fetchone()
                if digest(row[0]+'\0'+row[1])!=row[2]: raise ValueError('候选索引损坏。')
                return SearchHit(restore(row[0]),score),(scope_order[scope],row[3])
            dense=[]; order={}
            for score,_,_,scope,identity in sorted(best,reverse=True):
                value,key=hit(scope,identity,score); dense.append(value); order[value.chunk.chunk_id]=key
            hits=dense; branches=None
            if tools.retrieval_mode=='hybrid':
                db.execute('PRAGMA temp_store=FILE')
                db.execute('CREATE TEMP TABLE scores(scope TEXT,id TEXT,score REAL,PRIMARY KEY(scope,id))')
                for term in dict.fromkeys(lexical_tokens(query)):
                    checkpoint()
                    frequency=db.execute(f'SELECT COUNT(*) FROM ar_terms WHERE scope IN ({placeholders}) AND term=?',(*scopes,term)).fetchone()[0]
                    if not frequency or not average: continue
                    idf=log1p((count-frequency+.5)/(frequency+.5))
                    rows=db.execute(f'''SELECT t.scope,t.id,t.frequency,e.length FROM ar_terms t JOIN ar_entries e ON t.scope=e.scope AND t.id=e.id
                        WHERE t.scope IN ({placeholders}) AND t.term=?''',(*scopes,term))
                    for scope,identity,tf,length in rows:
                        score=idf*tf*(config.k1+1)/(tf+config.k1*(1-config.b+config.b*length/average))
                        db.execute('INSERT INTO scores VALUES (?,?,?) ON CONFLICT(scope,id) DO UPDATE SET score=score+excluded.score',(scope,identity,score))
                lexical=[]
                # Only scores and identities, never all matching bodies, enter Python.
                lexical_best=[]
                for scope,identity,score,seq in db.execute('SELECT s.scope,s.id,s.score,e.seq FROM scores s JOIN ar_entries e ON s.scope=e.scope AND s.id=e.id'):
                    item=(score,-scope_order[scope],-seq,scope,identity)
                    if len(lexical_best)<config.branch_m: heapq.heappush(lexical_best,item)
                    elif item>lexical_best[0]: heapq.heapreplace(lexical_best,item)
                for score,_,_,scope,identity in sorted(lexical_best,reverse=True):
                    value,key=hit(scope,identity,score); lexical.append(value); order[value.chunk.chunk_id]=key
                pool={h.chunk.chunk_id:h.chunk for h in dense+lexical}
                allowed=sorted(pool.values(),key=lambda c:order[c.chunk_id])
                hits,branches=reciprocal_rank_fusion(allowed,dense,lexical,config)
                hits=hits[:top_k]
                trace.update(dense_candidates=len(dense),bm25_candidates=len(lexical),fused_candidates=len(hits),bm25_built=computed>0)
            from context import validate_chunks
            if getattr(tools,'representation_mode',False):
                if any(h.chunk.kind!='object_representation' for h in hits): raise ValueError('对象表示与原文索引混用。')
            else: validate_chunks(with_table_context([h.chunk for h in hits],tools.sources),tools.sources)
            from knowledge_tools import SearchResult
            return SearchResult(hits,reused,computed,False,dimensions,tools.embedding_config.model_name,embedder.max_tokens,tools.retrieval_mode,branches)
    finally:
        if temporary: path.unlink(missing_ok=True)
