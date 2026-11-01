"""Prepare public, attributed outline benchmark artifacts; never calls an LLM."""

import argparse
import json
import re
import shutil
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DEFAULT_RUN = ROOT / '.data/evaluations/outline-v2-20261026-review'


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def section(text, start, end):
    left = text.index(start)
    right = text.index(end,left+len(start)) if end else len(text)
    return text[left:right].strip()+'\n'


def synthetic():
    """An original fictional protocol; cases repeat rules without introducing gold labels."""
    parts = ['# Cedar 队列服务教学协议 v1（合成资料）',
        '本资料是用于非商业工程评测的虚构协议，不代表任何供应商服务。金额单位均为虚构 credits。',
        '## 第一章 请求身份与状态',
        'R01：幂等键是 (tenant_id, action, request_id) 三元组，只在同一租户和动作内比较。不同租户相同 request_id 不是重复请求。',
        'R02：同键同载荷且已 completed 时重放原结果，不再执行；同键不同载荷一律返回 payload_conflict。running 状态同键同载荷返回 in_progress，不声称成功。',
        'R03：幂等记录的 TTL 是首次接受起 24 小时，读取和重放不续期。过期后同键可成为新执行，所以不保证无限时间的 exactly-once。',
        'R04：failed 记录只有显式 retry 才能领取新 attempt；网络超时不证明远端操作没有完成。',
        'R05：queued、running、completed、failed、paused 分别表示排队、实际执行、正式提交、失败、主动暂停；HTTP 202 只表示已接受。',
        'R06：大小写敏感的 action 必须保留，不能将字符串归一化后跨动作去重。',
        '## 第二章 发布与执行资源',
        'R07：状态领取和结果发布使用短 SQLite 事务，网络调用放在事务外；结果必须同时满足当前 attempt 和 source_revision 才能发布。',
        'R08：pause 先撤销 attempt，再请求协作取消；旧执行晚到不得发布，也不得覆盖已完成的其他批次。',
        'R09：每进程最多 2 个实际执行工作；HTTP 断连只发取消信号，Future 真正退出并清理 I/O 后才能释放名额，不能证明供应商已停止计算。',
        'R10：等待队列最多 4 个请求且总载荷不超过 32768 字节，任一限制达到即拒绝；等待超过 2 秒也拒绝，不能无界排队。',
        'R11：健康检查不得调用模型，服务可用不代表模型答案正确；计数重启归零，不是持久账单。',
        'R12：用户等待选择时必须保存 waiting_user 并释放工作名额；选择确认后重新准入。',
        '## 第三章 限流、重试与计量',
        'R13：租户限流为首次接受起 10 秒的固定窗口，每窗口最多 50 次请求。Redis Lua 原子读取、计数和设置到期；它不是滑动窗口或 token bucket。',
        'R14：速率上限与第二章的并发上限独立，不能用请求数证明实际工作线程数受限。',
        'R15：下面表格只定义本协议可重试条件，重试必须同时满足第四章的幂等约束和剩余额度。',
        '| 返回情况 | 行为 | 必要条件 |', '| --- | --- | --- |',
        '| 429 rate_limited | 最多两次显式重试 | 尊重 Retry-After，且剩余调用额度大于零 |',
        '| 401 unauthorized | 不自动重试 | 必须先修复凭据 |',
        '| 409 payload_conflict | 不重试原请求 | 同键不同载荷不能改作成功重放 |',
        '| transport_timeout | 状态未知，先查询 | 只有第四章允许时才能重发 |',
        '注：两次重试包含在任务总额度内，不是额外赠送；不得通过换 request_id 绕过业务幂等。',
        'R16：每个模型任务最多 12 次显式请求，失败、协议回退及重做均占额度；SDK 隐式重试关闭。',
        'R17：input_tokens 包含 cached_input_tokens，缓存数量是输入子集；计算输入费用时不能把缓存再次加到总输入上。',
        '| 计费项 | 每 1000 tokens 的 credits |', '| --- | --- |',
        '| 非缓存输入 | 2 |', '| 缓存输入 | 0.5 |', '| 输出 | 8 |',
        '单位：每 1000 tokens 的虚构 credits，不是元/美元，不代表真实模型价格；缺失 usage 记未知，不能填零。',
        'R18：总量应从唯一请求记录汇总；顶层展示副本及 superseded 父批的副本不得重复计数，但父批实际调用仍需计费。',
        '## 第四章 跨章节约束与练习案例',
        'R19：第三章的 timeout 重发仅在已向服务确认原键未 completed 且业务操作支持同键重放时允许；状态无法确认则保持 unknown，不能盲目重发有副作用请求。',
        'R20：即使重试满足 R19，仍须使用第一章的相同租户、动作、键和载荷，不得扩大幂等作用域；每次仍受 R16 总额度约束。',
        'R21：所有图表标题只作导航，未读取图像不得声称从图中提取事实；未处理内容必须显式保留范围。',
        'R22：清单候选只是建议，用户选择加入后才创建复习记录；无作答记录不推断用户已掌握或不会。',
        'R23：同一知识的解释和应用可分别是能力目标；不能仅凭主题相同就判为冗余。',
        'R24：下面案例仅帮助应用前文规则，不增加新的协议例外或新的必须背诵目标。']
    for i in range(90):
        tenant = f'tenant-{i%7}'
        parts += [f'**案例 {i+1:03d}：{tenant} 的记录核查**',
            (f'请求 {i+1} 使用 tenant_id={tenant}、action=publish、request_id=case-{i:03d}。'
             '操作者观察到客户端连接关闭，但没有看到 Future 完成记录。请依照第二章区分请求连接、实际工作和供应商远端计算，'
             '不要把断连当作名额已释放，也不要把排队接受当作结果完成。分析时应先明确已知记录和未知状态，再指出允许采取的核查动作。'
             '如果引用结果，必须保留当前 attempt 与来源版本的前提；辅助表格只补充条件，不能替代实际状态。'),
            ('随后收到 transport_timeout，运营人员提出换一个 request_id 直接再发。应回到第一章和第四章检查身份、载荷、'
             '原结果状态和幂等支持，并回到第三章检查 Retry-After、剩余调用额度及价格单位。这里重复同一规则是为了练习条件迁移，'
             '不是要求生成同义目标。不能用期望输出猜测服务已经完成，不能把未知 usage 当作零，也不能重复累计诊断展示副本。')]
    text='\n\n'.join(parts)+'\n'
    # Markdown table rows must be adjacent; blank lines would make them prose.
    return re.sub(r'(\|[^\n]*\|)\n\n(?=\|)',r'\1\n',text)


def download(url, path):
    if path.exists():
        return
    req = Request(url,headers={'User-Agent':'KnowledgeReview-public-evaluation/1.0'})
    with urlopen(req,timeout=40) as response:
        content=response.read(20*1024*1024+1)
    if len(content)>20*1024*1024:
        raise ValueError('Public download exceeded 20MB limit')
    path.write_bytes(content)


def acquire(directory, web_cache):
    directory=directory.resolve()
    if (directory/'gold-approved.json').exists():
        raise ValueError('Approved corpus cannot be regenerated')
    raw=directory/'corpus/raw'; canonical=directory/'corpus/canonical'
    raw.mkdir(parents=True,exist_ok=True); canonical.mkdir(parents=True,exist_ok=True)
    docs=[]
    def add(identity,title,kind,urls,license_name,license_url,reason,text=None,raw_files=()):
        if text is not None:
            path=canonical/(identity+'.md'); path.write_text(text,encoding='utf-8')
        else:
            path=canonical/(identity+'.md')
        docs.append(dict(id=identity,title=title,type=kind,urls=urls,license=license_name,
            license_url=license_url,selection_reason=reason,source_file=str(path.relative_to(directory)),
            sha256=digest(path),bytes=path.stat().st_size,lines=len(path.read_text(encoding='utf-8').splitlines()),
            raw_files=[dict(path=str(p.relative_to(directory)),sha256=digest(p)) for p in raw_files]))
    p=raw/'D01-python-sqlite3-full.md'; shutil.copyfile(web_cache/'outline-d01-python.md',p)
    original=p.read_text(encoding='utf-8')
    ctx=section(original,'### How to use the connection context manager [','### How to work with SQLite URIs')
    # Declared prose excerpt: keep every condition and Note, exclude examples.
    ctx=ctx.split('```')[0].rstrip().removesuffix('Copy').rstrip()
    add('D01','Python 3.13 sqlite3：连接上下文管理器（完整说明及Note，不含代码示例）','official-api',
        ['https://docs.python.org/3.13/library/sqlite3.html#how-to-use-the-connection-context-manager'],
        'PSF-2.0; documentation code examples additionally 0BSD','https://docs.python.org/3.13/license.html',
        '固定选取单个API的全部说明及Note，排除示例和导航；作为可走全文提取的小资料对照。',
        '# Python 3.13 sqlite3 官方文档节选\n\n'+ctx+'\n', [p])
    search=json.loads((web_cache/'outline-d02-search.json').read_text(encoding='utf-8'))
    entry=(search.get('data') or search)['web'][0]
    p=raw/'D02-kubernetes-blog.md'; p.write_text(entry['markdown'],encoding='utf-8')
    text=entry['markdown'].split('- [←Previous]')[0].strip()+'\n'
    add('D02','Kubernetes v1.31：Consistent Reads from Cache','engineering-blog',[entry['url']],
        'CC-BY-4.0','https://github.com/kubernetes/website/blob/main/LICENSE',
        '机制、etcd版本回退条件、收益宣传与未来计划并存，考察条件保留和冗余过滤。',text,[p])
    chapters=[]; raws=[]
    for index,name in enumerate(('outline-d03-thinkpython.md','outline-d03-ch12.md'),11):
        p=raw/f'D03-thinkpython-ch{index}.md';shutil.copyfile(web_cache/name,p);raws.append(p)
        chapters.append(p.read_text(encoding='utf-8'))
    add('D03','Think Python 2e：完整第11章 Dictionaries 与第12章 Tuples','textbook-contiguous-chapters',
        ['https://greenteapress.com/thinkpython2/html/thinkpython2012.html',
         'https://greenteapress.com/thinkpython2/html/thinkpython2013.html'],
        'CC-BY-NC-3.0','https://greenteapress.com/wp/think-python-2e/',
        '保留两个连续完整章节，检查哈希性、容器关系、记忆化与跨章依赖；注意教材版本语境。',
        '\n\n'.join(chapters),raws)
    p=raw/'D04-rfc9111.txt';download('https://www.rfc-editor.org/rfc/rfc9111.txt',p)
    # Keep the original RFC bytes separately; source loading uses UTF-8-sig.
    (canonical/'D04.md').write_text(p.read_text(encoding='utf-8-sig'),encoding='utf-8')
    add('D04','RFC 9111：HTTP Caching（完整RFC，保留法律声明）','rfc',
        ['https://www.rfc-editor.org/rfc/rfc9111.txt'],'IETF Trust TLP 5.0; unmodified complete RFC',
        'https://trustee.ietf.org/documents/trust-legal-provisions/tlp-5/',
        '完整规范含精确约束和跨节依赖，原始字节不改写。',None,[p])
    resource=(web_cache/'outline-d05-resource.md').read_text(encoding='utf-8')
    pdf_url=next(u for u in re.findall(r'https://[^\s)]+\.pdf',resource) if 'MIT6_006F11_lec08.pdf' in u)
    p=raw/'D05-MIT6_006F11-lec08.pdf';download(pdf_url,p)
    from pdf_parser import parse_pdf, PARSER_VERSION, pdf_library
    config=dict(algorithm=PARSER_VERSION,engine='pymupdf',pymupdf=pdf_library().VersionBind,ocr=False,vlm=False)
    parsed=parse_pdf(p.read_bytes(),raw_file=p.name,config=config)
    snapshot=raw/'D05.document.json';write_json(snapshot,parsed.to_dict())
    (canonical/'D05.md').write_text(parsed.text,encoding='utf-8')
    add('D05','MIT 6.006 Fall 2011 Lecture 8：Hashing with Chaining','pdf-course-notes',
        [pdf_url,'https://ocw.mit.edu/courses/6-006-introduction-to-algorithms-fall-2011/resources/mit6_006f11_lec08/'],
        'CC-BY-NC-SA-4.0 (OCW terms; retain any document-specific notices)',
        'https://ocw.mit.edu/pages/privacy-and-terms-of-use/',
        '课程PDF含复杂度表、散列公式与结构图；保留解析问题以区分解析和提取。',None,[p,snapshot])
    docs[-1].update(document_snapshot=str(snapshot.relative_to(directory)),pages=parsed.page_count,
                    parse_status=parsed.status,parse_issues=list(parsed.issues))
    add('D06','Cedar 教学队列协议 v1：密集目标、重复案例与跨章条件','synthetic-stress',
        [],'CC0-1.0 (original synthetic evaluation fixture)','https://creativecommons.org/publicdomain/zero/1.0/',
        '原创虚构协议，24条规则与90个重复迁移案例；Gold仅抽取关键规则，不把重复案例当新目标。',synthetic())
    manifest=dict(version=1,status='draft_gold_pending_user_review',prepared_at=datetime.now(timezone.utc).isoformat(),
        commercial_use=False,documents=docs,notes='NC资料仅用于本次非商业个人学习评测；不能据此扩展到商业训练或无许可发布。')
    write_json(directory/'corpus.json',manifest)
    print(json.dumps([dict(id=d['id'],bytes=d['bytes'],lines=d['lines'],pages=d.get('pages')) for d in docs],ensure_ascii=False))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--directory',type=Path,default=DEFAULT_RUN)
    parser.add_argument('--web-cache',type=Path,default=ROOT/'.firecrawl')
    args=parser.parse_args()
    acquire(args.directory,args.web_cache)


if __name__=='__main__':
    main()
