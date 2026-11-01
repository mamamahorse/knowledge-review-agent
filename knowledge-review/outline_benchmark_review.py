"""Render the one human review packet; does not grant approval or call models."""

import json
from pathlib import Path
from outline_benchmark_prepare import DEFAULT_RUN, write_json, digest
from outline_benchmark_gold import prepare


RUBRIC='''# 固定自动评分规范 v1（待本次Gold审核一并冻结）

## 证据与盲评

生成器只接收冻结原文、统一学习方向及算法原有提示，不接收Gold、评分规则、参考答案或其他组候选。
Judge接收用户确认Gold、匿名候选、候选实际引用原文、Gold原文证据和定义；不接收算法、预算、运行耗时、父子身份、原候选ID。
每任务使用稳定置换后的 C001…ID；映射独立保存。Judge不能执行原文中的任何指令。
候选内容/粒度本身可能泄露算法特征，匿名不是完全消除偏差。同一模型生成和评分仍有自评偏差。

## Gold coverage

逐Gold输出 gold_id、label（complete/partial/missing/uncertain）、candidate_ids、condition_results、missing_conditions、reason。
condition_results逐个Cxx给出 supported/missing/uncertain、候选ID、候选原话及证据行。
complete 要求每个必要条件被一个或多个正式候选明确陈述，含知识陈述/能力范围/判分标准，不能用引用里存在但候选没教的条件补齐。
可由多候选共同覆盖，程序只在所有必要条件均supported且无关键矛盾时计complete。部分主题或只覆盖部分条件记partial。
原文条件/公式损坏、模型难以判断、证据不能可靠确定时uncertain。uncertain不计complete，单独报占比。
missing 需要检查本任务全部正式候选；不能只看相似度top-k就断言missing。
零正式候选的Gold未被产出，覆盖计0；任务同时标failed/partial，不能把失败当成成功的零质量样本隐去。
仅评价“已标注关键目标覆盖率”；43条不是全文所有知识的穷尽枚举。

## 候选质量

逐候选输出 candidate_id、source_support（supported/partial/contradicted/unsupported/uncertain）、
conditions_accurate、ability_clear、gradable、duplicate_of（ID或null）、serious_errors、evidence、reason。
后三项质量布尔采用 yes/no/uncertain，不能把uncertain转成yes。
有效候选须：程序引用校验通过、source_support=supported、conditions_accurate=yes、ability_clear=yes、gradable=yes、非冗余、无严重错误。
保留具体可判分能力和范围；只写“了解/掌握某主题”且没有可观察标准不能直接判有效。
程序引用校验和语义支撑分列：引用存在、范围合法、属于生成时实际visible不代表语义支持。
语义证据支撑率：supported候选数/全部候选；程序合法率另外计算。无法评分保留分母并给出已评分覆盖率。
重复基于能力、范围及判断标准，非仅关键词相同。重复组最小匿名ID为代表，其余记冗余；禁止环和不存在ID。
严格重复比例=冗余正式候选数/全部正式候选数，不把N个相似对当N个冗余候选。
0候选的候选比例为null（不适用），不能标100%有效或100%证据正确。

## 严重错误

逐条保留候选、原话、证据行、错误类型及理由：捏造事实、与原文矛盾、遗漏必要条件导致技术结论错误、表格/公式/单位/例外误用、越权引用。
程序发现越界/不在实际visible立即记错误，不由Judge否决；Judge不确定的语义严重性单列suspected，不伪造已证实结论。
教材本身过时但忠于原文，单列source_obsolete，不能自动认定抽取捏造；候选把历史语境升级为当前保证须单列context_overclaim。
PDF原始图像与解析文本冲突单列parser_conflict。主评分用冻结Gold全部条目，另列parsed-intact子集，不能悄悄删除难题。

## 分包、超限与自动一致性检查

Judge预算65536/输出4096/余量2048。用项目GenerationBudget计算完整结构化请求，保持现有估计方法。
覆盖包固定每包最多2个Gold、全部正式候选及各自证据；超预算按稳定候选ID拆包，不丢top-k外候选。
每个覆盖包须返回按条件的支持片段；跨包联合覆盖由同一Judge规则的汇总包确认，不能用程序简单拼凑有矛盾的条件。
候选质量包每包最多4个目标及其证据；重复判定另用所有候选的完整知识/能力/范围/criteria卡片，超预算则明确duplicate_uncertain，禁止靠有损摘要宣称全局无重复。
程序按完整请求容量继续细分到单个项目；单项仍不适配、Judge96次上限或费用保护触发时该部分unscorable，其余已有结果保留。
覆盖complete却缺必要条件、ID不在输入、没有关键依据、引用与候选不匹配等均拒绝该条分数，标uncertain/invalid_judge，不自动重问到满意。
不做重复采样/可靠性研究，不使用候选反向补Gold，不改规则追分。Judge失败同样占实际请求数和未知费用预留。

## V2父子诊断

正式指标只用最终有效叶批次候选。父批临时候选保留在诊断中，不并入正式清单。
父批和全部最终后代使用同一匿名规则评分；父/子关系不给Judge。只在两侧都可评分时报告完整Gold集合的新增、丢失和净变化。
只针对两侧实际可见主范围所涉Gold作归因；跨范围Gold另外列出。无法评分记unknown，不能说“拆分无收益”。
拆分原因用saturated_8/coverage_risk/truncated独立多标签，事件总数按唯一父批ID，防止原因重复相加。
新增完整Gold是观察到的集合差，不等于拆分的因果收益；未做同窗口不拆分的独立随机对照。
额外成本分开列父批真实usage、子树usage、请求数、时间；不把顶层诊断副本重复求和。

## 工程结果及结论边界

首个正式候选时间只认finish_batch后正式持久化，superseded父批临时产物不算；无产物为null。
完成时间和运行至停止时间分列。实际HTTP请求按唯一ledger request_id统计，失败、协议回退均计，任务数与请求数分列。
输入缓存是输入子集；reasoning token只有服务返回时才报，不从输出长度反推；不保存隐藏思考内容。
费用若价格与usage齐全为按公开单价计算值；供应商实际扣费/优惠账单不可获取则明确未知，不能冒充实扣金额。
来源主范围覆盖按有效完成叶批次主范围行号并集/应处理主范围并集；排除空白、纯标题/页面导航，失败范围不算完成。
另报全文件行覆盖和Gold条件可见覆盖，不用大量重复案例行数代表知识质量。完成范围比例不是已提取知识比例。
先报逐文档、逐组及失败案例，再报配对差异；失败不删除，宏平均须列可比成功对数及全18任务状态。
不做加权总分、不作统计显著性结论，不将V2自动设为默认。
'''


def protocol(run_id=None):
    return dict(version=1,status='pending_user_gold_review',run_id=run_id or DEFAULT_RUN.name,
        source_revision='4f06786',request_execution='serial',max_in_flight=1,
        generation=dict(model='deepseek-flash',endpoint='https://api.deepseek.com',
            endpoint_sha256='a34e2a4708ed1c61008a151688838dcf1c44d4e7f08054633e72ba7c0b16cfc1',
            expected_model_version='DeepSeek-V4.1-Flash-1017',thinking=dict(type='enabled'),reasoning_effort='high',
            language='zh',output_tokens=4096,safety_margin=2048,context_tokens=[32768,131072],
            sdk_max_retries=0,timeout_seconds=120,protocol='native json_schema then existing explicit tool fallback',
            protocol_cache='clear FUNCTION_OUTPUT_PROVIDERS before each task; all actual attempts counted',
            cap_per_task=16,cap_total=128),
        judge=dict(model='deepseek-flash',endpoint='https://api.deepseek.com',thinking=dict(type='disabled'),
            context_tokens=65536,output_tokens=4096,safety_margin=2048,sdk_max_retries=0,
            timeout_seconds=120,cap_total=96,same_model_bias=True,coverage_gold_per_packet=2,quality_candidates_per_packet=4,
            failure='no blind retry; invalid semantic rows uncertain; authentication/protocol/service capacity errors stop campaign'),
        all_actual_requests_cap=224,soft_conservative_reservation_usd=10,
        per_request_reservation_usd=dict(generation=0.05,judge=0.025),
        maximum_full_campaign_reservations_usd=8.8,
        alternative_for_user_review=dict(selected=False,generation_per_task=128,generation_total=512,
            judge_total=256,all_actual_requests_cap=768,soft_conservative_reservation_usd=40,
            maximum_request_reservations_usd=32,estimated_no_cache_max_token_price_usd=27.52512,
            reason='18 tasks logical lower bound423 exceeds original128; select only by explicit user approval at this Gold review, never automatically expand'),
        price=dict(currency='USD',unit='per 1 million tokens',input_miss=0.3,input_hit=0.006,output=1.2,
            rate='official peak conservative',checked_at='2026-10-26',source='https://api-docs.deepseek.com/quick_start/pricing/',
            assumed_no_cache_max_token_price_estimate_usd=7.6087296,
            caveat='应用字节估算非精确token或账单封顶；失败usage可能未知，预留不退款；实扣未知单列，不改费率或充值'),
        smoke_document='D01',smoke_order=['A','B','C','D'],
        failure_policy=dict(service='401/403/authentication/429/server capacity/unrecoverable protocol -> stop campaign',
            task_limit='stop affected task and persist partial/failed; continue other tasks within total cap',
            aggregate_limit='stop remaining campaign, preserve results; no automatic expansion',
            dynamic_limit='keep core depth4/calls200 unchanged; external16-actual-request guard takes precedence',
            model_drift='record returned model/time/protocol; unexpected returned model change stops rather than mixing',
            usage_missing='record unknown and retain full conservative reservation; never convert to zero'),
        approval=dict(required=True,marker='gold-approved.json',digest_files=['corpus.json','GOLD.json','PROTOCOL.json','RUBRIC.md','planning.json','environment.json'],
            mutation_policy='Any user edits before approval regenerate affected hashes/plans; after approval immutable'),
        isolation='Independent corpus + SQLite/output directory; no production store, env-file writes, default changes or service restarts',
        implementation_status='Phase1: offline planner, initial Gold, HTTP ledger guard unit-tested. Live executor/Judge pipeline integrated only after explicit approval; real smoke still required.')


def render(directory=DEFAULT_RUN):
    directory=Path(directory);prepare(directory)
    write_json(directory/'PROTOCOL.json',protocol(directory.name))
    (directory/'RUBRIC.md').write_text(RUBRIC,encoding='utf-8')
    corpus=json.loads((directory/'corpus.json').read_text(encoding='utf-8'))
    plan=json.loads((directory/'planning.json').read_text(encoding='utf-8'))
    gold=json.loads((directory/'GOLD.json').read_text(encoding='utf-8'))
    ds={d['document_id']:d for d in plan['request_scale_diagnostics']}
    out=['# V1/V2 学习清单实验：唯一人工审核材料','',
        '**阶段1完成；未发出真实提取或Judge请求。Gold尚未批准，不能进入真实实验。**','',
        '## 请审核哪些内容','',
        '请审核六份资料、43条关键Gold的必要条件/证据、争议及下列费用/调用保护。可直接提出删改；收到明确批准后冻结原文、Gold、规范及配置，随后自动完成冒烟、剩余实验、盲评和报告。','',
        '本次不要求人工逐条审核生成候选或Judge，不切默认算法。你已明确要求此处等待确认，这是本轮唯一人工节点。','',
        '## 资料与规模','',
        '来源已保存原始快照和SHA-256，详细许可及原始文件哈希见[corpus.json](corpus.json)。以下数字是项目完整请求的**保守估计单位**，不是服务实际token。V1/V2协议不同，分别计算。','',
        '|ID|资料/结构与范围|UTF-8文件字节/行|V1全文请求估计|V2全文请求估计|Gold|',
        '|---|---|---:|---:|---:|---:|']
    for d in corpus['documents']:
        v1=next(t for t in plan['tasks'] if t['document_id']==d['id'] and t['algorithm']=='v1')
        count=sum(g['document_id']==d['id'] for g in gold['goals'])
        out.append(f"|{d['id']}|{d['title']}|{d['bytes']} / {d['lines']}|{v1['complete_request']['estimated_input_tokens']}|{ds[d['id']]['complete_request']['estimated_input_tokens']}|{count}|")
    out += ['', '每组容量先减4096输出、2048安全余量：32k输入上限26624，128k输入上限124928。小于32k、32k–128k、大于128k三个档位均存在，但同一资料因协议包装不同可跨档。',
        'D05只有7页、约6.6KB文本，V2全文估计却很大，原因包括细碎结构块/重复元信息，不应称为一本超长讲义。D03连续完整教材章节与D06压力文负责真正的长资料场景。','']
    for d in corpus['documents']:
        out += [f"### {d['id']} · {d['type']}",'',
            f"选择理由：{d['selection_reason']}",
            '来源：'+('；'.join(f'[{u}]({u})' for u in d['urls']) or '本次原创合成，生成程序 outline_benchmark_prepare.synthetic；不含Gold/期望输出标签'),
            f"许可：[{d['license']}]({d['license_url']})。",
            f"冻结文本SHA-256：`{d['sha256']}`。",f"Gold详细审核：[review/{d['id']}-GOLD.md](review/{d['id']}-GOLD.md)。",'']
    out += ['## 重要争议及未标注范围','',
        '- D01是固定API说明节选，非整个sqlite3模块；5个Gold有局部条件重叠，允许同一候选覆盖多个Gold，不应为凑5条强行生成重复目标。',
        '- D02按2024年Kubernetes v1.31文章评分；性能数字是该文5000节点测试，不拿来当本项目实测或2026现状。',
        '- D03教材的字典无序、dict_items被称为iterator等表述存在历史/技术语境问题；不把它们作现代Python正向Gold。未穷尽zip、gather/scatter、排序/练习等目标；图片URL未访问，不授予图片事实。',
        '- D04八个Gold重点缓存约束，尚未枚举Age详细公式、请求指令、范围请求、断连等全部细节。no-store/must-understand和qualified指令例外是有意保留的难点。',
        '- D05保留原PDF及解析Document：4项结构/阅读顺序警告；图被当表格；页5指数变平，页6“≠”被误成“=”。G06/G07分别标解析冲突/公式压平；全部Gold与解析完整子集分列。不会在提取前偷偷修正解析来改善成绩。',
        '  可查看[原始PDF](corpus/raw/D05-MIT6_006F11-lec08.pdf)、[解析文本](corpus/canonical/D05.md)、[第6页渲染图](review/pdf-preview/page-6.png)，区分原始证据与实际模型可见文本。',
        '- D05/V2已在规划时把L121、L223两个图片导航行标为failed/unprocessed，不会声称它们被模型处理；初始9个待提取窗口以外还有这2个失败范围。',
        '- D05的乘法散列完整位运算、全部示例未列Gold；本轮不能证明复杂PDF表格普适效果。',
        '- D06仅从24条规则选8个目标，90案例是重复条件迁移，非90个新知识点。R10“最多/不超过”与“达到即拒绝”在等号边界有歧义，当前Gold不评分恰好等于边界；请在本次审核决定是否改写。',
        '- Gold由同一Agent初标，存在选择偏差；Judge同模型、无重复采样。即使审核通过，语义分数仍是自动评价，不等于人工验证真实质量。','',
        '## 18任务和调用保护','',
        '完整矩阵D01/D03/D06：分别提供小输入全文路径、连续章节跨章依赖、大量重复和动态拆批压力。D02/D04/D05仅B/D。选择依据在候选生成前固定，见[PLANNING.md](PLANNING.md)与[planning.json](planning.json)。',
        'D01先A/B/C/D冒烟；其余按规划表旋转顺序串行，同一时刻最多一个HTTP评测请求。每任务重置结构化协议能力缓存，保留原协议回退逻辑并计每个实际请求。',
        '当前7/18个任务的逻辑调用下界已超过16，均为V1分层任务；这些是**预先可见的资源受限失败风险**。128k并不能绕过V1输出启发式和80行窗口，所以不能承诺18个成功清单。',
        '**18任务全部成功的逻辑调用下界合计423次**，尚未包含协议回退/动态拆批。即使每任务到16即停止，逐任务规划所需尝试合计也达158次（不含更早错误）；因此生成全局128次不允许全部18任务按这些规划走完。',
        '保持16/任务、128/生成全局上限，不为凑成功自动扩额。主报告同时给出完成率、受限产出与成功任务质量，不把未产出的候选解释为语义错误。若需要“所有任务完整结束”的纯算法比较，本矩阵与上限组合证据不足，结论须明确限制。',
        '供本次唯一审核选择的备选（当前未选中）：每任务128、生成总512、Judge总256、全部768、保守预留停止线US$40；按既定每次预留最大US$32，无缓存跑满预算估计约US$27.53。它给423次下界留出余量，仍不保证所有任务/评分成功。只有你在本次审核明确选择才冻结该配置，运行时绝不自动从默认升级。',
        '如果主要目标是大资料的完整质量对照，我建议在这一次审核选择备选；如果主要目标是验证低调用预算下的可交付性，则默认16/128更符合该目标。两者回答的问题不同，不能混称。',
        'V2规划下界不含8项饱和、coverage_risk和截断后的动态拆批；离线不能预测这些事件或收益。原核心深度4、调用200保留，本实验外层16次保护优先。','',
        '|项目|冻结提案|','|---|---|',
        '|生成|deepseek-flash；官方端点；thinking enabled/high；输出4096；安全2048；32k或128k|',
        '|Judge|同一模型/端点，thinking disabled；64k/输出4096/安全2048；固定规范与匿名候选|',
        '|实际请求|每清单最多16；生成总128；Judge总96（包含父批诊断/汇总）；全部最多224|',
        '|费用保护|每生成请求预留US$0.05、Judge预留US$0.025，失败/未知usage不退还；总预留保护US$10|',
        '|预估|全部224次都跑满时，按无缓存/峰值价和保守预算估计约US$7.61；固定调用预留总量最多US$8.80|',
        '|SDK/时间|隐式重试0；全组120秒请求超时；协议显式回退计次；认证/容量/不可恢复协议错误停实验|',
        '|模型漂移|保存实际返回model、时间、协议；意外标识变化停止，不混用模型|','',
        '价格依据：[DeepSeek官方价格，2026-10-26](https://api-docs.deepseek.com/quick_start/pricing/)：Flash峰值每百万输入未缓存US$0.30、缓存US$0.006、输出US$1.20；官方当前标识DeepSeek-V4.1-Flash-1017。[思考配置说明](https://api-docs.deepseek.com/guides/thinking_mode/)。',
        '已只读核对本地配置模型为deepseek-flash、端点为api.deepseek.com，凭据存在；未输出凭据、未改.env。评测超时120秒仅对隔离客户端生效；原配置30秒不变。',
        '4096输出预留可能同时容纳思考与正式结果，导致截断；保留同样配置以观察真实风险，不在运行中临时加输出。Judge独立关闭思考以控制结构化评分成本，生成四组思考配置一致。',
        '**US$10是应用侧保守预留停止线，不是供应商账单硬封顶。**字节估算不是精确token，失败可能无usage、价格可变、实扣账单不可取得；未知值保留，实扣不伪造。只准官方冻结端点/价格，不自动充值、换服务、换模型或扩额。','',
        '## 评分与下一步','',
        '固定规则见[RUBRIC.md](RUBRIC.md)，机器配置见[PROTOCOL.json](PROTOCOL.json)，全部必要条件及完整证据见[GOLD.json](GOLD.json)。审核通过后绑定这些文件和原文哈希，不用候选反向补Gold。',
        '自动Judge96次是总保护而非评分完成保证；细分后若超额，未评分记uncertain/unscorable并报告覆盖率，不能默认为正确，也不请求人工逐条补判。',
        '已实现离线计划、独立暂停SQLite、Gold与HTTP实际请求台账/保护模块；真实执行器和Judge编排将在批准后接入并先做D01冒烟。阶段1不把这些尚未真实验证的接入能力说成已验收。',
        '受控验证：14项新增检查、34项原有V1/V2测试均通过；最新记录见[validation.json](validation.json)，依赖版本见[environment.json](environment.json)。这些不证明外部模型/语义质量。',
        '离线model_identity为无凭据占位端点，不能直接恢复离线SQLite发请求。批准后用冻结原文与预算在独立live.sqlite3重建计划，绑定审核中的真实端点指纹，核对窗口/路由与离线诊断一致；保留离线库作为证据。',
        '正式数据库、资料、学习记录、默认算法及服务均未修改。未提交或推送Git。','']
    (directory/'GOLD_REVIEW.md').write_text('\n'.join(out),encoding='utf-8')
    print('Review packet ready:',directory/'GOLD_REVIEW.md')


if __name__=='__main__':render()
