"""Explicit, bounded visual navigation enrichment. Successful units survive retries."""
from contextlib import closing
import json
import sqlite3
from time import perf_counter

from agent_cancel import checkpoint,GenerationCancelled
from job_lock import job_lock
from model_request_budget import RequestBudget,RequestLimit,request_budget
from object_retrieval import encoded,digest,save_records,observation_records
from source_reader import read_lines,line_count

PROTOCOL='visual-search-v1'


def targets(source):
    document=source.document
    if not document: return []
    if document.format=='pdf':
        from pdf_regions import page_anchor
        return [dict(asset_id=f'page-{n}',page=n,label=f'第 {n} 页',
                     line_start=page_anchor(source,n).line_start,line_end=page_anchor(source,n).line_end)
                for n in range(1,(document.page_count or 0)+1)]
    result=[]; seen=set()
    for block in document.blocks:
        for identity in block.asset_ids:
            if identity in seen: continue
            seen.add(identity)
            result.append(dict(asset_id=identity,page=None,label=' / '.join(block.heading_path) or identity,
                line_start=block.line_start,line_end=block.line_end))
    return result


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS visual_description_pages(key TEXT PRIMARY KEY,payload TEXT NOT NULL)')


def load_unit(db,key):
    row=db.execute('SELECT payload FROM visual_description_pages WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else None


def save_unit(db,key,unit):
    db.execute('INSERT INTO visual_description_pages VALUES (?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload',
               (key,encoded(unit)))


def existing_observation(db,source_id,asset_id,question):
    from object_retrieval import initialize as initialize_objects
    initialize_objects(db)
    for payload,checksum in db.execute('SELECT payload,checksum FROM object_descriptions WHERE source_id=? ORDER BY rowid DESC',(source_id,)):
        if digest(payload)!=checksum: continue
        record=json.loads(payload)
        if (record.get('asset_id')==asset_id and record['origin']=='visual_observation'
                and record.get('reading_question')==question): return record


def prior_observations(db,source_id,target):
    from object_retrieval import initialize as initialize_objects
    initialize_objects(db)
    result=[]; remaining=6000
    for payload,checksum in db.execute('SELECT payload,checksum FROM object_descriptions WHERE source_id=? ORDER BY rowid DESC',(source_id,)):
        if digest(payload)!=checksum: continue
        record=json.loads(payload)
        same=(record.get('page')==target['page']) if target['page'] is not None else (
            record.get('asset_id')==target['asset_id'] or any(v.get('resource_id')==target['asset_id'] for v in record.get('viewed_assets',[])))
        if same and record['origin']=='visual_observation':
            text=record['description'][:remaining]
            result.append(dict(asset_id=record['asset_id'],description=text,limitations=record['limitations']))
            remaining-=len(text)
            if remaining<=0 or len(result)>=8: break
    return result
    return None


def run(source,database,selected,max_requests,report,*,guard=lambda:None):
    """No background queue/resume state machine. Call again to reuse successful units."""
    from pdf_vision import inspect_visual
    from visual_discovery import discover
    available={t['asset_id']:t for t in targets(source)}
    if not selected or len(selected)!=len(set(selected)) or not set(selected)<=available.keys():
        raise ValueError('请选择当前资料中不重复的页面或图片。')
    if not 1<=max_requests<=128: raise ValueError('请求额度应为1至128。')
    result=dict(source_id=source.source_id,status='running',total=len(selected),completed=0,reused=0,
                units=[],requests=[],max_requests=max_requests,error=None,elapsed_ms=0,
                notice='检查进度不代表对象发现完整或语义正确；描述只用于检索导航。')
    start=perf_counter()
    def progress():
        result['elapsed_ms']=(perf_counter()-start)*1000
        report(result)
    budget=RequestBudget(max_requests,progress); result['requests']=budget.records
    with job_lock(database,digest('visual:'+source.source_id)[:32]),closing(sqlite3.connect(database)) as db:
        initialize(db); db.commit()
        try:
            with request_budget(budget):
                for identity in selected:
                    checkpoint(); guard()
                    target=available[identity]; key=digest(encoded([PROTOCOL,source.source_id,identity]))
                    unit=load_unit(db,key)
                    if unit and unit['status']=='completed':
                        result['reused']+=1; result['completed']+=1; result['units'].append(unit); progress(); continue
                    if unit is None:
                        unit=dict(asset_id=identity,page=target['page'],status='running',objects=[],pending=[],read=[],unresolved=[])
                    result['units'].append(unit); unit['status']='running'; unit.pop('error',None); progress()
                    try:
                        if 'discovery' not in unit:
                            # A question-specific observation is reusable for its region,
                            # but cannot certify that the whole page was scanned.
                            context=read_lines(source,target['line_start'],min(line_count(source),target['line_start']+12))[:6000]
                            value=discover(source,identity,dict(nearby_text=context,
                                successful_readings=prior_observations(db,source.source_id,target)),describe=True)
                            checkpoint(); guard()
                            unit['discovery']=value; unit['objects']=value['objects']
                            records=[]
                            for obj in value['objects']:
                                records.append(dict(source_id=source.source_id,asset_id=obj['asset_id'],page=obj['page'],
                                    bbox=obj['bbox'],kind=obj['kind'],label=obj['label'],line_start=target['line_start'],
                                    line_end=target['line_end'],description=obj['description'],limitations=obj['uncertainty'],
                                    origin='visual_description',model=value['model'],protocol=PROTOCOL))
                                if obj['needs_detail']:
                                    unit['pending'].append(dict(asset_id=obj['asset_id'],question=
                                        ('Describe this object for retrieval: '+obj['label']+'. Resolve visible details: '+obj['uncertainty'])[:2000]))
                                elif obj['uncertainty']: unit['unresolved'].append(obj['uncertainty'])
                            if value['status']=='needs_detail' and not unit['pending']:
                                unit['unresolved'].append(value['note'] or '页面仍有无法辨认的细节。')
                            with db:
                                save_records(db,records); save_unit(db,key,unit)
                        while unit['pending']:
                            checkpoint(); guard()
                            item=unit['pending'][0]
                            existing=existing_observation(db,source.source_id,item['asset_id'],item['question'])
                            if existing:
                                value=dict(observation=existing['description'],limitations=existing['limitations'],
                                    next_read=existing.get('next_read',''),suggested_regions=existing.get('suggested_regions',[]),
                                    suggested_pages=existing.get('suggested_pages',[]))
                            else:
                                value,_=inspect_visual(source,item['asset_id'],item['question'])
                            checkpoint(); guard()
                            unit['read'].append(item['asset_id']); unit['pending'].pop(0)
                            if value.get('limitations'): unit['unresolved'].append(value['limitations'])
                            if value.get('next_read'): unit['unresolved'].append(value['next_read'])
                            # Follow concrete model-requested crops, never infer a tool call
                            # from prose. The global HTTP budget bounds nested reading.
                            seen=set(unit['read'])|{p['asset_id'] for p in unit['pending']}
                            for region in value.get('suggested_regions',[])+value.get('suggested_pages',[]):
                                if region['asset_id'] not in seen:
                                    unit['pending'].append(dict(asset_id=region['asset_id'],question=region['reason'][:2000]))
                                    seen.add(region['asset_id'])
                            with db:
                                if not existing: save_records(db,observation_records(value,item['question']))
                                save_unit(db,key,unit)
                            progress()
                        unit['status']='completed'
                        with db: save_unit(db,key,unit)
                        result['completed']+=1
                    except Exception as exc:
                        # No silent model switch or unbounded page retry. Other pages may
                        # still be useful; authentication/capacity errors stop this run.
                        unit.update(status='failed',error=type(exc).__name__)
                        with db: save_unit(db,key,unit)
                        if getattr(exc,'status_code',None) in (401,403,429) or type(exc).__name__=='VisionUnsupported':
                            result['error']='视觉服务鉴权、容量或能力受限，请检查配置后重试。'; break
                    progress()
                result['status']='completed' if result['completed']==len(selected) else 'partial'
        except RequestLimit as exc:
            result.update(status='partial',error=str(exc))
            if result['units'] and result['units'][-1]['status']=='running':
                unit=result['units'][-1]; unit.update(status='partial',error='RequestLimit')
                with db: save_unit(db,key,unit)
        except GenerationCancelled:
            result.update(status='interrupted',error='执行已停止，成功描述已保存。'); raise
        except Exception as exc:
            result.update(status='failed',error=type(exc).__name__)
        finally:
            result['unprocessed']=[i for i in selected if not any(u['asset_id']==i and u['status']=='completed' for u in result['units'])]
            progress()
    return result
