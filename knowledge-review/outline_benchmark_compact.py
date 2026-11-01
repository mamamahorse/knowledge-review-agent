"""Prepare the approved smaller *design*, never approve Gold or run a model.

Original artifacts are read-only inputs. Cropping ranges come from the user's
selected report, not from model outputs. All derivative identities are new.
"""

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from difflib import SequenceMatcher
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import platform
import shutil

from outline_benchmark import CODE_FILES, plan_one, snapshot, load_source, validate_artifacts, reduction_minimum
from outline_benchmark_prepare import ROOT, DEFAULT_RUN as PARENT_RUN, digest, write_json
from outline_benchmark_review import protocol as original_protocol, RUBRIC

COMPACT_RUN=ROOT/'.data/evaluations/outline-v2-20261027-compact-review'
RANGES={'D01':[(1,24)],'D02':[(16,48),(69,78)],
        'D03':[(9,9),(321,411),(758,758),(1130,1234)],
        'D04':[(526,685)],'D06':[(1,1),(5,292)]}
TITLES={'D01':'Python 3.13 sqlite3：Connection上下文管理器说明与Note',
        'D02':'Kubernetes v1.31：一致读机制、版本条件与未来工作（节选）',
        'D03':'Think Python 2e：原11/12章标题及11.5、12.6跨章小节（节选）',
        'D04':'RFC9111：§4.1至§4.2.1 Vary与新鲜度（连续节选）',
        'D05':'MIT 6.006 Lecture8：原PDF第5–6页（散列公式节选）',
        'D06':'Cedar合成协议：全部24条规则、四章与36个案例（节选）'}
OVERRIDES={'D06-A':24,'D06-B':24,'D05-B':24}


def protect_parent():
    return {str(p.relative_to(PARENT_RUN)):digest(p) for p in PARENT_RUN.rglob('*') if p.is_file()}


def verify_parent(directory):
    receipt=json.loads((directory/'parent-preservation.json').read_text(encoding='utf-8'))
    for name,expected in receipt['sha256'].items():
        if digest(PARENT_RUN/name)!=expected:raise ValueError('Original preparation changed: '+name)
    return len(receipt['sha256'])


def check_pending(directory):
    if Path(directory).resolve().is_relative_to(PARENT_RUN.resolve()):
        raise ValueError('The original preparation directory is read-only for compact preparation')
    if (directory/'gold-approved.json').exists():raise ValueError('Approved artifacts are immutable')


def excerpt_with_map(text,ranges):
    original=text.splitlines();parts=[];positions={};offset=0;segments=[]
    for index,(a,b) in enumerate(ranges):
        if not 1<=a<=b<=len(original):raise ValueError('Invalid original range')
        if index:parts.append('\n\n');offset+=2
        start=offset+1;part='\n'.join(original[a-1:b]);parts.append(part)
        for delta,old_line in enumerate(range(a,b+1)):positions[start+delta]=old_line
        segments.append(dict(original_start=a,original_end=b,derived_start=start,derived_end=start+b-a))
        offset+=part.count('\n')
    derived=''.join(parts)+'\n'
    mapping=[dict(derived_line=n,original_line=positions.get(n),kind='original' if n in positions else 'separator')
             for n in range(1,len(derived.splitlines())+1)]
    return derived,dict(segments=segments,lines=mapping)


def pdf_map(original,derived):
    def pages(document):
        starts=[(b.locations[0].page,b.line_start) for b in document.blocks if b.kind=='page']
        return {page:(a,(starts[i+1][1]-1 if i+1<len(starts) else len(document.text.splitlines())))
                for i,(page,a) in enumerate(starts)}
    old_lines=original.text.splitlines();new_lines=derived.text.splitlines();old_pages=pages(original);new_pages=pages(derived)
    rows={};page_map=[]
    for new_page,old_page in [(1,5),(2,6)]:
        a,b=old_pages[old_page];x,y=new_pages[new_page]
        for n in range(x,y+1):rows[n]=dict(derived_line=n,original_line=None,derived_page=new_page,
            original_page=old_page,kind='reparsed_pdf_page_text')
        for m in SequenceMatcher(None,old_lines[a-1:b],new_lines[x-1:y],autojunk=False).get_matching_blocks():
            for delta in range(m.size):
                rows[x+m.b+delta].update(original_line=a+m.a+delta,kind='exact_text_match')
        page_map.append(dict(derived_page=new_page,original_page=old_page,
            original_line_start=a,original_line_end=b,derived_line_start=x,derived_line_end=y))
    return dict(page_map=page_map,lines=[rows[n] for n in sorted(rows)],
        caveat='Reparsed PDF lines may change. Physical original page and unchanged page coordinates are authoritative; exact line matches are supplemental.')


def derive(directory=COMPACT_RUN):
    check_pending(directory)
    if (directory/'corpus.json').exists():raise ValueError('Corpus already exists; do not overwrite it')
    before=protect_parent();parent=validate_artifacts(PARENT_RUN)
    raw=directory/'corpus/raw';canonical=directory/'corpus/canonical';originals=directory/'corpus/originals'
    for folder in (raw,canonical,originals,directory/'review/pdf-preview'):folder.mkdir(parents=True,exist_ok=True)
    documents=[]
    for old in parent['documents']:
        ident=old['id'];doc=deepcopy(old);doc['title']=TITLES[ident]
        parent_text=(PARENT_RUN/old['source_file']).read_text(encoding='utf-8')
        local_original=originals/(ident+'.md');shutil.copyfile(PARENT_RUN/old['source_file'],local_original)
        inherited=[local_original]
        for entry in old['raw_files']:
            target=originals/'raw'/Path(entry['path']).name;target.parent.mkdir(exist_ok=True)
            shutil.copyfile(PARENT_RUN/entry['path'],target);inherited.append(target)
        if ident!='D05':
            text,mapping=excerpt_with_map(parent_text,RANGES[ident]);files=[]
        else:
            from document_model import Document
            from pdf_parser import parse_pdf,pdf_library,PARSER_VERSION
            old_parsed=Document.from_dict(json.loads((PARENT_RUN/old['document_snapshot']).read_text(encoding='utf-8')))
            original_pdf=next(p for p in inherited if p.suffix=='.pdf')
            pdf=pdf_library();target=raw/'D05-pages5-6.pdf'
            with pdf.open(stream=original_pdf.read_bytes(),filetype='pdf') as full:
                with pdf.open() as selected:
                    selected.insert_pdf(full,from_page=4,to_page=5)
                    target.write_bytes(selected.tobytes(no_new_id=True))
                with pdf.open(stream=target.read_bytes(),filetype='pdf') as selected:
                    visual=[]
                    for index in range(2):
                        original_pix=full[index+4].get_pixmap(matrix=pdf.Matrix(1,1))
                        pix=selected[index].get_pixmap(matrix=pdf.Matrix(1,1))
                        same=(original_pix.width,original_pix.height,original_pix.samples)==(pix.width,pix.height,pix.samples)
                        if not same:raise ValueError('Derived PDF rendering changed')
                        preview=directory/'review/pdf-preview'/f'derived-page-{index+1}-original-{index+5}.png'
                        pix.save(preview);visual.append(dict(derived_page=index+1,original_page=index+5,pixel_identical=True))
            parsed=parse_pdf(target.read_bytes(),raw_file=target.name,config=dict(algorithm=PARSER_VERSION,
                engine='pymupdf',pymupdf=pdf.VersionBind,ocr=False,vlm=False))
            parsed_path=raw/'D05.document.json';write_json(parsed_path,parsed.to_dict())
            text=parsed.text;mapping=pdf_map(old_parsed,parsed);mapping['visual_checks']=visual
            files=[target,parsed_path] # Derived PDF MUST precede original full PDF.
            doc.update(document_snapshot=str(parsed_path.relative_to(directory)),pages=2,original_pages=[5,6],
                parse_status=parsed.status,parse_issues=list(parsed.issues))
        source=canonical/(ident+'.md');source.write_text(text,encoding='utf-8')
        map_path=directory/'corpus'/(ident+'.mapping.json')
        mapping.update(document_id=ident,original_canonical=str(local_original.relative_to(directory)),
            original_sha256=old['sha256'],derived_sha256=digest(source),original_source_run=str(PARENT_RUN))
        write_json(map_path,mapping)
        doc.update(source_file=str(source.relative_to(directory)),sha256=digest(source),bytes=source.stat().st_size,
            lines=len(text.splitlines()),mapping_file=str(map_path.relative_to(directory)),
            selection_reason='用户确认CALL_COUNT_REPORT方案二的固定范围；未使用候选或Judge反向筛选。',
            original_source_sha256=old['sha256'],derivation_ranges=RANGES.get(ident),
            raw_files=[dict(path=str(p.relative_to(directory)),sha256=digest(p)) for p in [*files,*inherited,map_path]])
        documents.append(doc)
    write_json(directory/'corpus.json',dict(version=2,status='compact_gold_pending_user_review',
        prepared_at=datetime.now(timezone.utc).isoformat(),parent_run=str(PARENT_RUN),commercial_use=False,
        scope='Six compact excerpts. No generalization to complete textbook or whole RFC.',documents=documents))
    write_json(directory/'parent-preservation.json',dict(parent_run=str(PARENT_RUN),sha256=before))
    write_json(directory/'environment.json',dict(python=platform.python_version(),platform=platform.system(),
        packages={p:version(p) for p in ['openai','httpx','pydantic','PyMuPDF','markdown-it-py']}))
    license_lines=['# 来源、许可与改动说明','',
        '本目录为非商业个人学习评测；原件及法律声明在corpus/originals保留。派生文件仅裁取固定范围，不改写正文/公式。',
        'PDF由MIT 6.006 Fall2011 Lecture8原件选取第5–6页，原课程和原完整PDF的许可声明与页7保留于originals。页像未改，派生物理页1/2对应原页5/6。','']
    for d in documents:
        license_lines += [f"- {d['id']}：{d['title']}；许可[{d['license']}]({d['license_url']})；来源："+'；'.join(d['urls'])]
    (directory/'ATTRIBUTION.md').write_text('\n'.join(license_lines)+'\n',encoding='utf-8')
    print('Derived sources:',[(d['id'],d['lines'],d['bytes']) for d in documents],flush=True)
    print('Original files unchanged:',verify_parent(directory),flush=True)


def compact_protocol():
    p=original_protocol(COMPACT_RUN.name);p['version']=2;p.pop('alternative_for_user_review',None)
    p['generation'].update(task_request_limit_overrides=OVERRIDES,
        protocol_cache='clear once before D01 smoke; after successful structured validation reuse confirmed capability only for the same model+endpoint; retain every failure and usage; isolated judge cache',
        capability_fairness='record cache state/protocol/schema per attempt; report startup probe costs separately and in total; never silently switch protocol/model to rescue one algorithm')
    p['failure_policy']['dynamic_limit']='Core depth4/calls200 unchanged; external per-task16/24 and generation global128 take precedence'
    p['scope']='Only six compact excerpts; cannot extrapolate to complete textbook or whole RFC; offline lower bound is not actual cost'
    p['expected_logical_lower_bound']=99
    p['full_matrix_documents']=['D01','D06']
    p['gold_approval_status']='pending; design and limits approved, new Gold not yet approved'
    return p


def plan(directory=COMPACT_RUN):
    check_pending(directory);verify_parent(directory);manifest=validate_artifacts(directory)
    if (directory/'offline-plans.sqlite3').exists():raise ValueError('Existing planning database is not overwritten')
    tasks=[]
    for i,doc in enumerate(manifest['documents']):
        arms=list('ABCD') if doc['id'] in ('D01','D06') else list('BD')
        offset=0 if doc['id']=='D01' else i%len(arms);arms=arms[offset:]+arms[:offset]
        for arm in arms:
            key=doc['id']+'-'+arm;cap=OVERRIDES.get(key,16);print('Planning:',key,flush=True)
            t=plan_one(directory,doc,arm,model_options={'base_url':'https://api.deepseek.com'},actual_request_limit=cap)
            t['task_id']=key
            if t['algorithm']=='v1' and t['mode']=='hierarchical':
                sizes=[sum(b['section_id']==s['id'] for b in t['batches_plan'] if b.get('status','pending')=='pending')
                    for s in t['sections_plan'] if s['has_content']]
                t['local_overview_calls']=t['initial_pending']
                t['section_merge_calls_minimum']=sum(reduction_minimum(n) for n in sizes)
                t['document_merge_calls_minimum']=reduction_minimum(sum(n>0 for n in sizes))
            else:t.update(local_overview_calls=0,section_merge_calls_minimum=0,document_merge_calls_minimum=0)
            tasks.append(t)
    from learning_store import LearningStore
    from library_store import LibraryStore
    from outline_store import OutlineStore
    from outline_service import scope_fingerprint
    practice=LearningStore(directory/'offline-plans.sqlite3');library=LibraryStore(practice);store=OutlineStore(practice)
    docs={d['id']:d for d in manifest['documents']}
    for task in tasks:
        doc=docs[task['document_id']];source=load_source(directory,doc);snapshots=[snapshot(doc,source)]
        collection=library.create(task['task_id'])['id']
        task['offline_job_id']=store.create(collection,'offline-only','系统提取资料中值得学习的知识与可判分能力目标，保留事实、适用条件、例外及跨章关系，避免无必要重复。',
            'zh',scope_fingerprint(snapshots),snapshots,task['sections_plan'],task['batches_plan'],task['plan'])
        store.pause(task['offline_job_id'])
    result=dict(version=2,stage='offline_only_compact',live_model_requests=0,gold_review_required=True,
        code_revision='4f06786',code_sha256={name:digest(ROOT/name) for name in CODE_FILES},
        full_matrix_documents=['D01','D06'],tasks=tasks,
        logical_calls_lower_bound=sum(t['successful_logical_calls_lower_bound'] for t in tasks),
        boundary='Logical fresh-success lower bound only; no Judge, fallback or output-dependent splitting included')
    write_json(directory/'planning.json',result);write_json(directory/'PROTOCOL.json',compact_protocol())
    # The semantics of the rubric stay fixed; only the corpus/task scope changes.
    rubric=RUBRIC.replace('43条不是全文所有知识的穷尽枚举','本轮条目不是节选全部知识的穷尽枚举').replace('全18任务状态','全16任务状态')
    (directory/'RUBRIC.md').write_text(rubric+'\n本轮仅适用于精简资料；不得把覆盖率外推完整教材/RFC，调用下界不是实际成本。\n',encoding='utf-8')
    lines=['# 精简16任务离线规划','',
        'A=V1/32k，B=V1/128k，C=V2/32k，D=V2/128k；输出4096、安全2048。所有作业只持久化计划并暂停。',
        '所有归并数为成功四路归并下界；动态拆分未知，不能用0声称不会发生。','',
        '|任务|路径|全文输入估计|局部概述|小节归并下界|文档归并下界|初始提取|逻辑下界|实际请求上限|初始失败范围数|',
        '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for t in tasks:
        lines.append(f"|{t['task_id']}|{t['mode']}|{t['complete_request']['estimated_input_tokens']}|{t['local_overview_calls']}|{t['section_merge_calls_minimum']}|{t['document_merge_calls_minimum']}|{t['initial_pending']}|{t['successful_logical_calls_lower_bound']}|{t['actual_request_limit']}|{len(t['initial_unprocessed'])}|")
    lines += ['',f"合计{len(tasks)}任务；生成逻辑调用下界{result['logical_calls_lower_bound']}；生成实际请求上限128，Judge独立96，费用预留停止线US$10。",'',
        '完整请求采用项目保守UTF8估算而非实际服务token；32k输入上限26624，128k输入上限124928。',
        '实际协议失败/回退也计入上限；首次冒烟后同模型同端点复用已确认的协议能力，不逐任务反复清空。',
        'D06的输出饱和/coverage_risk/截断能否触发、拆分收益与实际费用均未知。']
    (directory/'PLANNING.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print('Tasks / logical lower bound:',len(tasks),result['logical_calls_lower_bound'],flush=True)
    print('Original files unchanged:',verify_parent(directory),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('stage',choices=['derive','plan']);args=parser.parse_args()
    (derive if args.stage=='derive' else plan)()
