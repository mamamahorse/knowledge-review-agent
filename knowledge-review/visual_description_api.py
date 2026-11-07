"""Small synchronous enrichment endpoint on the existing bounded worker pool."""
import json
from pathlib import Path
import re
from fastapi import HTTPException,Request
from pydantic import Field
from answers import StrictModel
from job_lock import JobBusy
from pdf_parse_cache import write_record
from visual_descriptions import run,targets


class DescriptionRequest(StrictModel):
    source_id: str
    request_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    asset_ids: list[str] = Field(min_length=1,max_length=200)
    max_requests: int = Field(default=16,ge=1,le=128)


def install(app,catalog,source_for,store,streams,root):
    root=Path(root)/'visual-description-runs'

    @app.get('/api/documents/{identity}/visual-descriptions')
    def describe_scope(identity:str):
        source=source_for(catalog.get(identity))
        return dict(source_id=source.source_id,targets=targets(source))

    @app.get('/api/visual-descriptions/{identity}')
    def progress(identity:str):
        if not re.fullmatch('[0-9a-f]{32}',identity): raise HTTPException(404,'补全记录不存在。')
        path=root/(identity+'.json')
        if not path.is_file(): raise HTTPException(404,'补全记录不存在。')
        return json.loads(path.read_text(encoding='utf-8'))

    @app.post('/api/documents/{identity}/visual-descriptions')
    async def describe(identity:str,payload:DescriptionRequest,request:Request):
        document=catalog.get(identity); source=source_for(document)
        if source.source_id!=payload.source_id: raise HTTPException(409,'资料版本已变化，请刷新。')
        allowed={t['asset_id'] for t in targets(source)}
        if len(set(payload.asset_ids))!=len(payload.asset_ids) or not set(payload.asset_ids)<=allowed:
            raise HTTPException(400,'页面或图片不属于当前资料。')
        path=root/(payload.request_id+'.json')
        root.mkdir(parents=True,exist_ok=True)
        try:
            with path.open('x',encoding='utf-8') as file:
                json.dump(dict(status='queued',source_id=source.source_id,completed=0,total=len(payload.asset_ids),requests=[]),file)
        except FileExistsError: raise HTTPException(409,'这个请求已提交，请读取进度或使用新的请求标识。')
        def guard():
            current=catalog.get(identity)
            if current.revision!=document.revision or source_for(current).source_id!=source.source_id:
                raise ValueError('资料版本已变化，停止补全旧版本。')
        def work():
            return run(source,store.path,payload.asset_ids,payload.max_requests,
                       lambda value:write_record(path,value),guard=guard)
        try: return await streams.run(work,request)
        except JobBusy as exc:
            write_record(path,dict(status='failed',error=str(exc)))
            raise HTTPException(409,str(exc)) from exc
        except HTTPException as exc:
            # Cancellation has a worker-owned final record, possibly still being
            # written. Never replace its usage/partial results with an HTTP error.
            if exc.status_code!=499:
                value=json.loads(path.read_text(encoding='utf-8'))
                if value['status']=='queued':
                    write_record(path,{**value,'status':'failed','error':str(exc.detail)})
            raise
