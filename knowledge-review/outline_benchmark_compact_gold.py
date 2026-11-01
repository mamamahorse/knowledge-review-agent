"""Scope-specific Gold and review packet; no model calls and no approval writer."""

from copy import deepcopy
import json
from pathlib import Path

from outline_benchmark import load_source
from outline_benchmark_compact import COMPACT_RUN, PARENT_RUN, check_pending, verify_parent
from outline_benchmark_prepare import write_json, digest


def compress(numbers):
    result=[]
    for number in numbers:
        if result and result[-1][1]+1==number:result[-1][1]=number
        else:result.append([number,number])
    return result


def annotations(directory):
    corpus=json.loads((directory/'corpus.json').read_text(encoding='utf-8'))
    docs={d['id']:d for d in corpus['documents']}
    maps={key:json.loads((directory/d['mapping_file']).read_text(encoding='utf-8')) for key,d in docs.items()}
    rows=[]
    old_gold=json.loads((PARENT_RUN/'GOLD.json').read_text(encoding='utf-8'))['goals']
    # These two scopes retain all their previous necessary facts; renumber evidence.
    for old in old_gold:
        if old['document_id'] not in ('D01','D06'):continue
        r=deepcopy(old);r['parent_gold_ids']=[r['id']];r['id']=r['id'].replace('-G','-SG')
        lookup={e['original_line']:e['derived_line'] for e in maps[r['document_id']]['lines'] if e['original_line'] is not None}
        ranges=[]
        for e in r['evidence_ranges']:
            selected=[lookup[n] for n in range(e['line_start'],e['line_end']+1)]
            ranges.extend(compress(selected))
        r['evidence_ranges']=[dict(line_start=a,line_end=b) for a,b in ranges]
        r['annotation_basis']='重新核对精简范围后继承必要条件；新来源身份、行号和证据重新绑定。旧Gold同样未获人工批准。'
        rows.append(r)

    def add(doc,topic,ability,conditions,ranges,*,cross=False,uncertainty='Agent初标；待用户本次审核',parser='intact',parents=()):
        number=1+sum(r['document_id']==doc for r in rows)
        rows.append(dict(id=f'{doc}-SG{number:02d}',document_id=doc,topic=topic,ability=ability,
            required_conditions=[dict(id=f'C{i+1:02d}',text=c) for i,c in enumerate(conditions)],
            evidence_ranges=[dict(line_start=a,line_end=b) for a,b in ranges],
            cross_section_dependency=cross,parsed_evidence_status=parser,uncertainty=uncertainty,
            annotation_basis='基于精简原文的明确规则、公式或示例重新标注，不用生成候选反向补标。',parent_gold_ids=list(parents)))

    add('D02','过滤与一致读瓶颈','解释旧路径为何让局部LIST需求付出全量处理成本',
        ['namespace可直接在etcd过滤','标签/字段选择器需要取回数据集再由apiserver内存过滤','kubelet只需本节点Pod，但旧路径处理集群Pod数据，造成资源浪费'],[(3,11)],parents=['D02-G01'])
    add('D02','watch cache','区分收到watch更新和已经足够新',
        ['缓存保存集群快照并通过etcd watch接收更新','单靠接收更新不足以保证缓存足够新，旧实现因此不能直接提供一致读'],[(15,17)],parents=['D02-G02'])
    add('D02','progress notifications','按步骤解释如何确认新鲜度再从缓存读取',
        ['先检查缓存是否足够新','不足时向etcd查询progress notifications，直到确认足够新才从缓存服务','并非完全不访问etcd：仍需少量元数据'],[(19,29)],parents=['D02-G03'])
    add('D02','版本和默认启用','判断支持条件及旧版本回退',
        ['文章语境为Kubernetes v1.31的Beta默认启用，并非所有版本的无条件保证','etcd须为3.4.31+或3.5.13+','更旧etcd自动回退到直接从etcd提供一致读'],[(31,32),(37,39)],cross=True,
        uncertainty='标题/来源元信息提供v1.31历史语境；不要求模型从节选推断2026年的最新版本行为。',parents=['D02-G04'])
    add('D02','未来工作','把已可用的一致读与未宣称完成的分页区分开',
        ['支持版本上的一致读机制已进入默认启用阶段','watch cache分页在本文是未来探索，不能据此断言已经实现'],[(37,43)],parents=['D02-G06'])

    add('D03','11.5 / 反转映射','实现保留多对一关系的字典反转',
        ['相同原值可能对应多个原键，因此反转后的值使用列表','首次遇到原值创建单元素列表，后续append','不能用后一个原键覆盖之前已保存的原键'],[(5,30)],parents=['D03-G04'])
    add('D03','11.5 / 哈希约束','解释列表为何能作值却不能作键',
        ['字典键要求可哈希，列表作为键导致TypeError','若键的哈希随修改改变，会使存储位置与后续查找不一致','列表或字典可以作为值；不把正文简化表述推广为所有可变自定义对象一概不可哈希'],[(59,92)],parents=['D03-G07'])
    add('D03','12.6 / items遍历','用键值对解包遍历字典',
        ['items提供可迭代的键值对，每对可用key,value解包','能区分遍历键值对与只遍历字典键'],[(99,120),(160,167)],
        uncertainty='正文将dict_items称iterator并说items无特定顺序，存在历史/术语问题；Gold只要求可迭代键值对及解包，不教授现代Python无序断言。')
    add('D03','12.6 / 构造与补充映射','用成对序列构造或补充字典',
        ['dict可以接收键值元组列表','dict与zip组合可将对应元素构造成映射','update也可接收键值元组列表以补充已有字典；不编造本文未给出的冲突处理策略'],[(125,145)])
    add('D03','11.5 → 12.6 / 复合键','把哈希约束和姓名复合键关联起来',
        ['列表不能作键，示例用姓、名字符串组成的元组表达复合键','directory[last,first]中方括号表达的是元组键','遍历复合键可按last,first解包；不能泛化为任意含不可哈希成员的元组均可作键'],[(59,89),(147,167)],cross=True,
        uncertainty='可哈希成员限制用来防止过度泛化；不要求候选额外讲解正文未展开的Python哈希实现。',parents=['D03-G07'])

    add('D04','§4.1 / Vary匹配','按Vary条件判断何时可不经重验证复用',
        ['Vary指定的请求字段必须与原请求匹配，才可不经重验证复用该表示','规范化只能采用字段规范认可的等价变换，不能任意改大小写/顺序','字段缺失只能与同样缺失匹配'],[(3,29)],parents=['D04-G02'])
    add('D04','§4.1 / 不匹配','识别Vary通配符和无匹配时的处理',
        ['Vary包含*时始终不匹配，不是匹配所有请求','没有存储响应匹配时缓存不能满足本次请求，通常向源站转发而非凭空选择一个变体'],[(31,32),(51,54)])
    add('D04','§4.1 / 多个变体','按规范选择多个匹配响应',
        ['已知偏好排序机制如qvalues时可使用，不能说必须按该机制','没有偏好机制或并列时按Date选最新','部分存储响应省略Vary时，规范SHOULD选择最新的有效Vary响应，不将SHOULD说成绝对MUST'],[(34,49)])
    add('D04','§4.2 / fresh阈值','用寿命和年龄判断新鲜度',
        ['freshness_lifetime严格大于current_age才为fresh，等于不算','年龄从源站生成或成功验证后计算；新鲜度是缓存语义，不能强制浏览器刷新显示'],[(62,74),(94,99),(122,125)],
        uncertainty='引言“未超过”的直觉描述可能让人误读等号边界，本Gold以明确公式的严格>为准；Age详细计算章节未入选，不要求补讲不可见公式。',parents=['D04-G03'])
    add('D04','§4.2.1 / 优先级','按首次匹配规则计算新鲜寿命',
        ['共享缓存优先s-maxage，之后max-age，再Expires-Date','没有Date时按规范使用接收时刻作为该计算的替代','缺少显式过期信息时只是可能适用启发式，不是无条件可缓存或永久新鲜'],[(129,146)],parents=['D04-G03'])
    add('D04','§4.2.1 / 冲突与无效值','处理重复和矛盾的新鲜度指令',
        ['同一指令有多个值时取首次出现或按stale处理，不能任意取最大值','指令冲突应遵守最严格者','无效新鲜度信息如非整数max-age鼓励按stale处理；保留规则语气而非捏造强制要求'],[(152,159)])

    add('D05','派生p1 / 原p5 / SUH','解释简单均匀散列的假设',
        ['每个键落入任一槽位的概率相同','与其他键落在哪个槽位独立；这是分析假设，不能声称任意实现天然满足'],[(7,10)],parents=['D05-G04'])
    add('D05','派生p1 / 原p5 / 负载与性能','按负载因子推导期望查找复杂度',
        ['n为键数、m为槽数，α=n/m','期望查找Θ(1+α)，1来自散列/随机访问、α来自链搜索','只有α=O(1)即m=Ω(n)时才得到期望O(1)，不是最坏恒定'],[(12,28)],parents=['D05-G04'])
    add('D05','派生p1 / 原p5 / 取余散列','解释取余散列及参数的实践取舍',
        ['h(k)=k mod m','讲义建议m为不接近2或10幂的质数，不能推广为保证无碰撞','选择质数不方便、除法较慢是讲义给出的实现取舍，不是本项目实测'],[(34,41)],parents=['D05-G05'])
    add('D05','派生p2 / 原p6 / universal hashing','保留不同键前提解释碰撞概率',
        ['对固定的两个不同键随机选择哈希函数，不是随机改变键','讲义给出该模型碰撞概率1/m，不是0，也不将等式推广到所有散列族','必须保留不同键前提；不能把解析文本错误等号当成原PDF事实'],[(84,95)],
        uncertainty='原PDF写k1≠k2，解析L90却成k1=k2；原公式1/m也被压平。按实际文字可见证据评分时需标parser_conflict/uncertain，不能假装看到了正确符号。',
        parser='contradictory_symbol',parents=['D05-G06'])
    add('D05','派生p2 / 原p6 / 期望线性性','用碰撞指示变量求期望链长',
        ['把对其他n个键的碰撞计数表示为指示变量求和','用期望线性性把1/m逐项求和得到n/m=α','结论是期望值，不是每次执行都恰有α个碰撞'],[(96,128)],
        uncertainty='求和/分数二维公式在解析L98–126压平；原PDF页6为视觉核对依据，纯文字自动评分证据可能不足。',
        parser='formula_flattened',parents=['D05-G07'])

    for r in rows:
        doc=docs[r['document_id']];source=load_source(directory,doc);lines=source.text.splitlines()
        r.update(source_sha256=doc['sha256'],source_id=source.source_id,
            scope='compact excerpt only; no claim of complete source coverage')
        line_map={e['derived_line']:e for e in maps[r['document_id']]['lines']}
        for e in r['evidence_ranges']:
            a,b=e['line_start'],e['line_end']
            if not 1<=a<=b<=len(lines):raise ValueError((r['id'],e))
            e['text']='\n'.join(lines[a-1:b])
            original=[line_map[n]['original_line'] for n in range(a,b+1) if line_map[n]['original_line'] is not None]
            e['original_line_ranges']=compress(original)
            e['original_canonical']=maps[r['document_id']]['original_canonical']
            if r['document_id']=='D05':
                e['original_pdf_pages']=sorted({line_map[n]['original_page'] for n in range(a,b+1)})
                e['derived_pdf_pages']=sorted({line_map[n]['derived_page'] for n in range(a,b+1)})
        if r['document_id']=='D05':
            r['original_pdf']='corpus/originals/raw/D05-MIT6_006F11-lec08.pdf'
            r['derived_pdf']='corpus/raw/D05-pages5-6.pdf'
    return sorted(rows,key=lambda r:r['id'])


def build(directory=COMPACT_RUN):
    check_pending(directory);verify_parent(directory)
    rows=annotations(directory)
    write_json(directory/'GOLD.json',dict(version=2,status='agent_draft_pending_user_review',
        parent_gold_unchanged=True,exhaustive=False,scope='Only annotated key-goal coverage within compact excerpts; not full textbook/RFC coverage',goals=rows))
    corpus=json.loads((directory/'corpus.json').read_text(encoding='utf-8'));docs={d['id']:d for d in corpus['documents']}
    for ident,doc in docs.items():
        own=[r for r in rows if r['document_id']==ident]
        lines=[f'# {ident} 精简版Gold：{doc["title"]}','',
            '**尚未人工确认。以下必要条件须被正式候选明确覆盖；只在引用中出现不算覆盖。**','',
            f"[派生原文](../{doc['source_file'].replace(chr(92),'/')}) · [原文位置映射](../{doc['mapping_file'].replace(chr(92),'/')})",'',
            f"派生内容SHA-256：`{doc['sha256']}`。原内容SHA-256：`{doc['original_source_sha256']}`。",'']
        for r in own:
            lines += [f"## {r['id']} · {r['topic']}",'',r['ability'],'','完整覆盖所需条件：','']
            lines += [f"- {c['id']}：{c['text']}" for c in r['required_conditions']]
            lines += ['',f"跨节/跨章依赖：{'是' if r['cross_section_dependency'] else '否'}；解析证据：`{r['parsed_evidence_status']}`。",
                '原Gold关联（不代表旧标注已批准）：'+(', '.join(r['parent_gold_ids']) or '本轮节选中新标注目标')+'。',
                '标注依据：'+r['annotation_basis'], '不确定性：'+r['uncertainty'],'',
                '<details><summary>派生原文证据及原始位置</summary>','']
            for e in r['evidence_ranges']:
                original=', '.join(f'L{a}–L{b}' for a,b in e['original_line_ranges'])
                pages=f"；原PDF页{e['original_pdf_pages']}，派生页{e['derived_pdf_pages']}" if ident=='D05' else ''
                lines += [f"派生L{e['line_start']}–L{e['line_end']} → 原规范化文本{original}{pages}",'',
                    '```text',e['text'],'```','']
            lines += ['</details>','']
        (directory/'review'/(ident+'-GOLD.md')).write_text('\n'.join(lines)+'\n',encoding='utf-8')
    report(directory,rows,corpus)
    print('New draft Gold:',len(rows),{key:sum(r['document_id']==key for r in rows) for key in docs})


def report(directory,rows,corpus):
    plan=json.loads((directory/'planning.json').read_text(encoding='utf-8'))
    lines=['# 精简版V1/V2实验：新的Gold审核材料','',
        f'**设计和限额已获用户同意；本轮{len(rows)}条Gold尚未确认。没有真实提取/Judge请求，没有gold-approved.json。**','',
        '本轮是六份精简资料、16任务的工程选型实验。原资料及旧Gold保留不变，本目录是新版本，不替代旧版本。','',
        '## 唯一人工审核范围','',
        '请审核下方六份Gold的必要条件、原文映射和争议。确认后才冻结本版原文/Gold/评分规范/配置，并自动继续真实冒烟、剩余实验、自动Judge与报告；无需再人工逐条检查候选。',
        '本轮结果不得外推完整教材、完整RFC或原90案例资料；99是离线成功逻辑调用下界，不是实际调用、Token、耗时或费用。','',
        '## 派生资料、结构、哈希和Gold','',
        '|ID|固定范围|行数|Gold|审核入口|','|---|---|---:|---:|---|']
    scopes={'D01':'原API说明全部24行，未改变内容','D02':'原L16–48 + L69–78：机制/版本/未来工作',
        'D03':'原章标题+11.5与12.6两个小节；不是完整章节',
        'D04':'原L526–685：§4.1至§4.2.1连续节选；不是完整RFC',
        'D05':'原PDF第5–6页 → 派生物理页1–2，重新本地解析',
        'D06':'原总标题+第一至四章全部24条规则，保留前36案例，省略文首独立说明'}
    for d in corpus['documents']:
        ident=d['id'];count=sum(r['document_id']==ident for r in rows)
        lines.append(f"|{ident}|{scopes[ident]}|{d['lines']}|{count}|[Gold及证据](review/{ident}-GOLD.md)|")
    lines += ['', '[完整Gold JSON](GOLD.json) · [资料/许可/哈希manifest](corpus.json) · [来源署名与裁剪说明](ATTRIBUTION.md)','',
        '每份corpus/*.mapping.json记录逐行来源；拼接空行明确标separator而不冒充原文。PDF记录新页→原页与行匹配；新旧页渲染像素一致，但提取文本仍可能失真。原始全文/PDF与法律声明的副本在corpus/originals。','']
    for d in corpus['documents']:
        lines += [f"- {d['id']} 派生SHA-256：`{d['sha256']}`；许可[{d['license']}]({d['license_url']})。"]
    lines += ['', '## 必须审核的争议与遗漏','',
        '- D01的5个目标条件有部分关联，允许同一候选覆盖多条Gold，不要求人为制造5个重复候选。',
        '- D02不再包含5000节点收益数字，不从旧Gold补回不在模型可见范围的事实；按v1.31历史文章语境评分。',
        '- D03仅是跨章节选，不再评价memo、global、完整元组语法等被删范围。正文dict_items的iterator称谓和无序表述有历史/术语问题，不作为现代Python正向Gold。书中图片链接未访问，不能获得图像事实。',
        '- D04只评选定范围。no-store/private/304/stale例外等已移出，不能将原RFC八条Gold全部套用。保留的六条包括Vary、变体选择、严格fresh阈值、寿命优先级和冲突约束；HTTP-date详细处理未穷举。',
        '- D05的派生p2/原p6中“≠”仍被解析为“=”；1/m和求和公式也被压平。D05-SG04是parser_conflict，SG05是formula_flattened。全部Gold与解析完整子集分列，不能悄悄修复原文、删除难题或将uncertain当正确。乘法散列完整公式暂未标Gold。',
        '- D06保留R10原句：容量“最多/不超过”与“达到即拒绝”在等号边界有歧义，SG04不评分恰好等于边界。不修改资料来消除歧义。36案例重复应用已有规则，不是36个新知识点。',
        '- 所有Gold仅代表已标注关键目标，不是节选或原文的全部知识。Gold初标与Judge均自动完成，语义分数不等同人工验证的真实质量。','',
        'PDF核对：[派生两页](corpus/raw/D05-pages5-6.pdf) · [实际解析文本](corpus/canonical/D05.md) · [派生p1/原p5页图](review/pdf-preview/derived-page-1-original-5.png) · [派生p2/原p6页图](review/pdf-preview/derived-page-2-original-6.png)。','',
        '## 16任务规划与调用保护','',
        'A=V1/32768，B=V1/131072，C=V2/32768，D=V2/131072；四组使用相同生成模型、端点、语言、thinking high、输出4096与安全2048。完整矩阵仅D01、D06，其余B/D。','',
        '|资料|A|B|C|D|合计|','|---|---:|---:|---:|---:|---:|']
    for doc in corpus['documents']:
        own=[t for t in plan['tasks'] if t['document_id']==doc['id']]
        arms={t['arm']:t['successful_logical_calls_lower_bound'] for t in own}
        lines.append('|'+doc['id']+'|'+'|'.join(str(arms.get(a,'—')) for a in 'ABCD')+'|'+str(sum(arms.values()))+'|')
    lines += ['', '[逐阶段/窗口规划](PLANNING.md) · [完整计划JSON](planning.json) · [执行及费用配置](PROTOCOL.json) · [固定评分规范](RUBRIC.md)','',
        '合计16任务、99次生成逻辑下界；V1归并可能因真实概述大小增加，V2输出8项/coverage_risk/截断触发的动态拆分未知，Judge不在99中。',
        'D06完整输入保守估计约145k，大于128k输入上限124928；32k/128k下初始7/2窗口。V2生成8项契约的饱和压力保留，但不保证必触发拆分。',
        '- 普通任务：最多16次实际HTTP。仅D06-A、D06-B、D05-B为24次。',
        '- 生成全局128、Judge独立96、全部最多224；真实请求串行，SDK隐式重试关闭。失败、兼容回退、父批重做均计数。',
        '- 每生成请求保守预留US$0.05，Judge US$0.025；全局预留停止线US$10。未知usage不填0、失败不退回预留；按全部请求上限预留最多US$8.80。此线不是供应商账单硬封顶；未充值、未启用US$40备选。',
        '- 沿用2026-10-26核对的官方峰值价格快照，实际执行前若价格/端点无法可靠对齐就停止并报告，不静默采用不明价格。真实扣费未发生且不可提前断言。',
        '- 所有限额触发后保存partial/failed，不能扩额或切模型。协议、认证、服务容量异常按固定规则停止。',
        '- Judge96是保护上限，不保证所有候选/父子诊断都能评分；分包数量取决于真实候选/证据。达到上限的部分记unscorable/uncertain，报告评分覆盖率，不当作正确或完整评测通过。','',
        '按当前每包最多2个Gold、每任务候选卡片去重各1包计算，覆盖判定基线52包、去重基线16包；候选质量与父子诊断尚未计入。若一包不能适配还需细分，96不保证评分全部完成；保留这一风险而不伪造Judge调用预测。','',
        '## 协议能力缓存与公平性','',
        '首次D01四组冒烟开始前只清空一次能力缓存。成功结构化返回后，同模型、同端点复用原实现已确认的协议能力；不再逐任务清空。只有失败响应不能当成已确认成功能力。',
        '每次实际HTTP保留protocol/schema、开始/结束时间、返回model、失败及usage；记录缓存状态变化与建立能力的请求ID。启动探测消费计入生成全局128并单列，不能从账单删掉；质量/速度对比同时披露启动开销。',
        '生成四组共享同一兼容条件，不为了救某组静默切协议。Judge因思考配置不同使用独立缓存命名空间；模型/端点漂移停止。',
        '离线计划已绑定审核配置的公开端点指纹，不带凭据；所有计划只在本目录独立SQLite中保存并暂停，不能被正式服务自动执行。','',
        '## 尚未授权的后续步骤','',
        '等待你明确确认本版Gold。设计/限额同意不等于Gold已批准，当前没有任何真实提取/Judge调用。后续自动步骤仍需先完成隔离执行器/Judge接入和受控验证，再D01真实冒烟；能力不可用即停止，不能绕过审核。',
        '新准备模块只复用解析、Source、原规划器和存储；原核心算法与默认路径未变。评分规则原文保留，仅将18任务/原Gold计数表述更新为本轮范围。',
        '受控验证：14项本轮检查+14项原准备检查通过（28项），不含任何外部模型质量验证。检查覆盖派生位置映射、34条Gold证据、例外限额不扩大全局、协议缓存按模型/端点隔离、失败请求/usage保留、拒绝将原目录作为写入目标。',
        '原准备目录保存校验见[parent-preservation.json](parent-preservation.json)；单元/隔离检查见[validation.json](validation.json)。','']
    (directory/'GOLD_REVIEW.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


if __name__=='__main__':build()
