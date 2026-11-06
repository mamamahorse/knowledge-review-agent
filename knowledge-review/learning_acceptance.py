"""One isolated, explicitly run synthetic acceptance case; no Judge or default changes."""
import argparse
from datetime import datetime,timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from generation_budget import GenerationBudget
from learning_planner import PlanningStore,run_planning
from learning_metrics import http_policy,summary
from structured_output import request_structured
from structured_recovery import audit_scope
from sources import load_sources
from markdown_images import import_images
from dotenv import load_dotenv

PRICE_SOURCE='https://api-docs.deepseek.com/quick_start/pricing/'


class Guard:
    def __init__(self): self.count=0
    def before(self,request,payload,protocol):
        if request.url.host!='api.deepseek.com' or payload.get('model')!='deepseek-flash':
            raise ValueError('Acceptance is restricted to the verified configured endpoint/model.')
        if self.count>=10: raise ValueError('Acceptance HTTP limit reached; no additional requests allowed.')
        mode=os.getenv('LLM_THINKING','enabled').strip().lower()
        submission=any(t.get('function',{}).get('name')=='submit_result' for t in payload.get('tools',[]))
        expected_choice='auto' if mode=='enabled' or not submission else {'type':'function','function':{'name':'submit_result'}}
        if payload.get('thinking')!={'type':mode} or payload.get('max_tokens')!=12288:
            raise ValueError('Acceptance protocol/thinking/output configuration mismatch.')
        if payload.get('tool_choice')!=expected_choice:
            raise ValueError('Acceptance tool choice does not match thinking mode.')
        self.count+=1
    def after(self,record):
        record['price_source']=PRICE_SOURCE; record['currency']='USD'; record['price_basis']='verified peak rates, 2026-11-06; estimate, not bill'
        a,b,c=record['input_tokens'],record['output_tokens'],record['cached_tokens']
        if a is not None and b is not None:
            cached=c if c is not None and c<=a else 0
            record['cost_estimate']=((a-cached)*.3+cached*.006+b*1.2)/1_000_000


class Proxy:
    def __init__(self,client):
        self.client=client
        self.chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))
    def __getattr__(self,name): return getattr(self.client,name)
    def create(self,**kwargs):
        result=self.client.chat.completions.create(**kwargs)
        if any(choice.finish_reason=='length' for choice in result.choices):
            raise RuntimeError('Acceptance output truncated; stopped without raising output budget.')
        return result


def forced(client,*args,**kwargs):
    return request_structured(Proxy(client),*args,**{**kwargs,'protocol_policy':'forced_tools'})


def prepare(root):
    from PIL import Image,ImageDraw,ImageFont
    root.mkdir(parents=True,exist_ok=False)
    image=Image.new('RGB',(1000,520),'white'); draw=ImageDraw.Draw(image)
    font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',30)
    for y,text in enumerate(['SYNTHETIC LEARNING FIXTURE','y = 2*x + 1; x in {0, 1, 2}; y is a count.',
        'Request -> Check cache','Cache HIT -> reuse stored result','Cache MISS -> compute -> store -> return']):
        draw.text((35,35+y*88),text,fill='black',font=font)
    image_path=root/'diagram.png'; image.save(image_path)
    text='# Synthetic worksheet\n\nThe formula y = 2*x + 1 applies to x in {0,1,2}. y is a count.\n\nThe image adds the cache branch conditions.\n\n![Formula and pipeline](diagram.png)\n'
    path=root/'worksheet.md'; path.write_text(text,encoding='utf-8')
    import_images(path,text,{'diagram.png':image_path.read_bytes()})
    return load_sources([path])[0]


def main():
    load_dotenv(Path(__file__).resolve().parent / '.env')
    parser=argparse.ArgumentParser(); parser.add_argument('--run',action='store_true')
    parser.add_argument('--output',type=Path,default=Path('.data')/('learning-acceptance-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')))
    args=parser.parse_args(); root=args.output.resolve(); source=prepare(root)
    manifest=dict(status='prepared',fixture_sha256=sha256(source.text.encode()).hexdigest(),
        image_sha256=sha256((root/'diagram.png').read_bytes()).hexdigest(),
        limits=dict(http=10,logical_reservations=10,context=131072,output=12288,margin=2048,
                    conservative_request_reserve_usd=.32,conservative_total_reserve_usd=3.2,stop_line_usd=10),
        pricing_source=PRICE_SOURCE,code_sha256={p.name:sha256(p.read_bytes()).hexdigest() for p in sorted(Path(__file__).parent.glob('*.py'))},
        thinking=os.getenv('LLM_THINKING','enabled'),reasoning_effort=os.getenv('LLM_REASONING_EFFORT','high'),
        limitations=['One synthetic case, not coverage benchmark','Core pipeline plus real HTTP; browser/UI not exercised','No account-level hard cap','No Judge API'])
    report=root/'report.json'; report.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    if not args.run:
        print(json.dumps(dict(status='prepared',output=str(root)))); return
    store=PlanningStore(root/'acceptance.sqlite3'); guard=Guard()
    identity=None
    try:
        with patch.dict(os.environ,dict(LLM_CONTEXT_TOKENS='131072',LLM_OUTPUT_TOKENS='12288',LLM_SAFETY_TOKENS='2048')), \
             patch('learning_planner.request_structured',side_effect=forced),patch('pdf_vision.request_structured',side_effect=forced), \
             http_policy(guard),audit_scope(root/'structured-output'):
            identity=store.create(source,'Create one or two assessable goals retaining the formula domain and the cache hit/miss conditions. Check the image; exclude decorative content explicitly.',
                'one-synthetic-case',formalize=True,visual_coverage=True,max_requests=10,budget=GenerationBudget(131072,12288,2048,'acceptance-fixed'))
            state=run_planning(store,identity,lambda:load_sources([source.path])[0])
            if state['status']=='completed' and state.get('stage')=='ready' and state['formal']['goals']:
                goal=next(iter(state['formal']['goals']))
                state=run_planning(store,identity,lambda:load_sources([source.path])[0],question_goal_id=goal)
            manifest.update(status=state['status'],stage=state['stage'],job_id=identity,
                            goal_count=len(state.get('formal',{}).get('goals',{})),question_count=len(state.get('questions',{})))
            (root/'result.json').write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
    except BaseException as exc:
        manifest.update(status='stopped',error_type=type(exc).__name__,job_id=identity)
        if identity: (root/'result.json').write_text(json.dumps(store.get(identity),ensure_ascii=False,indent=2),encoding='utf-8')
    finally:
        if identity: manifest['metrics']=summary(store,identity)
        manifest['guard_http_attempts']=guard.count
        report.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in manifest.items() if k not in ('code_sha256','metrics')},ensure_ascii=False))


if __name__=='__main__': main()
