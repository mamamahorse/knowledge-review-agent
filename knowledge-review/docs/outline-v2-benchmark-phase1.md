# V1/V2 学习清单对照：阶段1交付说明

2026-10-26，算法基线 `4f06786`，分支 `codex/outline-extraction-v2`。
本轮仅离线准备和受控测试，没有真实提取/Judge请求，没有改默认算法或正式记录。

## 审核入口与范围

正式审核材料：`.data/evaluations/outline-v2-20261026-review/GOLD_REVIEW.md`。

- 六份资料及哈希、来源/许可：`corpus.json`、`corpus/raw/`、`corpus/canonical/`。
- 43个Agent初标关键Gold：`GOLD.json`及`review/D01-GOLD.md`…`D06-GOLD.md`。尚未人工批准，不是全知识枚举。
- 18个真实规划器产生的计划：`planning.json`、`PLANNING.md`、独立且全部暂停的`offline-plans.sqlite3`。
- 评分规则：`RUBRIC.md`；调用、费用和模型配置：`PROTOCOL.json`。
- PDF原文、Document快照及页图保留，包含公式/不等号丢失的解析缺陷。

`outline-v2-prep-20261026`与`review/offline-iterations/initial/`是早期资料选择/表格结构诊断，不属于另一次真实实验，不与最终18任务合并计数。最终D01固定为API说明小节正文与Note；D06修正合成Markdown表格的空行，保留2个实际表格。均发生在Gold批准前。

## 已核对的真实算法

|行为|当前代码依据|对实验的含义|
|---|---|---|
|V1全文候选最多6，局部最多4|`outline_context.FullOutlineDraft`、`outline_service.py:43`|这是schema契约，不是保证产出数|
|全文条件同时检查输入、预期数量和输出|`outline_context.py:81 plan_outline`|`expected=max(外层正文块数,ceil(UTF8字节/500),1)`，需≤6且expected×950≤4096；增大输入不会取消输出启发式|
|V1分层初始窗口≤80行并检查完整协议|`outline_context.py:109`之后的窗口循环|32k/128k的相同初始批次是实际规划现象，非评测脚本写死|
|每个窗口先概述，然后章/文归并，最后提取|`outline_context.py:181 reduce_steps`、`:227 run_adaptive`|4路归并是最多4项而非永远4项；单项也调用一次。实际概述太大时会更多调用/失败|
|V2每次最多8项|`outline_v2.py:22 MAX_PROPOSALS`|达到8不代表已经遗漏，只是有限拆批触发器|
|结构原文直接打包|`outline_v2.py:155 plan_direct`|使用ContentBlock、表格上下文和完整请求预算，无固定80行上限|
|V2计划保存预算及身份|`outline_v2.py:32 direct_budget`、`:155 plan_direct`、`:227 run_direct`|保存generation_budget后恢复执行；离线占位身份不应用来发真实请求，live需绑定审核端点|
|动态拆分|`outline_v2.py:245–292`的运行分支|请求预算、输出截断、8项饱和、coverage_risk分别保留；最大深度4，内部调用上限200，外层实验上限优先|
|引用的存在及可见范围|`outline_service.py:146 validate_learning`、`outline_v2.validate_direct`|程序不能证明语义正确；V2至少一处引用须属于主范围，不能只引用辅助上下文|
|父批与尝试历史|`outline_store.py:16 direct_diagnostic`、`:300 reserve_direct_call`、`:308`之后的拆批/失败保存|superseded父批不形成正式候选，但真实usage不能丢；attempts与顶层最新副本不可双计|
|SDK结构化兼容|`structured_output.py:42 request_structured`、`:110 request_tool_result`|原生schema不可用才回退submit_result；forced tool不可用可再auto，每次HTTP都计数|
|V2已有usage诊断，V1默认没有同等粒度|`practice_model.py:93 _request_structured`|telemetry非空时关闭SDK隐式重试；评测另加HTTP实际尝试台账覆盖V1/失败/截断，不能把逻辑函数调用数当实际调用数|
|保守预算|`generation_budget.py:37 estimate`、`:42 usage`|UTF8字节+协议常量，不更换tokenizer；32k输入上限26624、128k输入上限124928|

未将32k/128k解释为服务硬上限；本地模型配置只读核对为`deepseek-flash`与官方端点。

## 规划结论：上限与目的存在冲突

完整矩阵资料为D01（小API）、D03（完整连续教材章节）、D06（重复/跨章压力），其余B/D。
真实规划下界：18任务合计**423个逻辑调用**，未计协议回退、模型输出引起的额外拆分。
7个V1分层任务的下界已超过每任务16次，且概述阶段本身就超过16。
逐任务按16截停再求和也达158，生成全局128会进一步使后序任务无法完成。

因此默认16/128只能回答低调用预算下的可交付性，不能得到全部大资料的完整语义质量对照。
审核材料同时给出未选中的备选：每任务128、生成512、Judge256、全局768、US$40保守预留保护；无缓存满预算估计约US$27.53。
这不是运行时自动扩额；必须由用户在唯一Gold审核时明确选择。任何上限下都保留失败和未知，不保证全部任务成功。

D05/V2初始9个待提取批次外，有2个图片导航行已被规划器标为未处理；PDF不是干净无损解析输入。
全文V2估计约278k远大于约6.6KB文本，反映结构块序列化开销，不能只看PDF页数或原文字节判断请求规模。

## 新增文件为什么这样划分

|文件|作用及原因|对正式项目的改变|
|---|---|---|
|`outline_benchmark_prepare.py`|固定公开网页缓存/二进制、解析PDF、原创合成资料、保存哈希；保留原件以定位解析错误|仅写隔离评测目录；OCR/VLM显式关闭，不调用提取模型|
|`outline_benchmark.py`|复用真实V1/V2规划器，用临时预算作用域产生18任务及独立SQLite；不重写算法|只读正式模块；`ExitStack`退出恢复所有patch，不写`.env`|
|`outline_benchmark_gold.py`|明确编写目标/必要条件及证据；单独生成审核材料|Gold不传入生成提示，避免泄露答案|
|`outline_benchmark_review.py`|评分规范、费用保护、争议与可读审核包|不会批准Gold，不会调用真实服务|
|`outline_benchmark_guard.py`|按实际HTTP尝试写开始/结束台账；审批摘要检查、请求上限和保守费用预留|不构造真实客户端；仅供隔离执行器使用；无需改业务存储|
|`test_outline_benchmark.py`|受控HTTP、真实资料哈希/Gold锚点、矩阵/预算恢复、结构表格及核心哈希验证|不将测试通过当真实模型质量证明|

这里`@contextmanager`将临时配置的生命周期绑定到`with`；`ExitStack`集中撤销patch，即使异常也恢复，避免预算泄漏到其他任务。
`@dataclass(frozen=True)`让调用保护配置不可随意原地修改。HTTP台账是事实来源，模型诊断是解释来源，两者通过调用ID关联，不把复制的诊断视作新消费。
台账开始记录flush/fsync后再发请求；进程中断留下只有start的请求，按已消费/usage未知处理，不重新发送来“补成功”。

## 验证与下一阶段边界

- 14项新增受控检查通过（最新耗时见隔离目录validation.json）：含未批准阻止HTTP、修改Gold/原文字节使审批失效、失败/协议回退均计次、台账恢复、独立Judge上限、未知usage不退还预留、缓存不重复计费、43个Gold全部范围校验、18任务及V2预算持久化、D01全文路由。
- 34项原有`test_outline_context`及`test_outline_v2`通过，耗时41.303秒。不是全项目回归、不是外部服务验收。
- 初次新增测试因Windows私有Temp目录权限失败；测试目录改用隔离工作区继承权限后通过，没有放宽生产权限。
- 9个核心算法/协议文件SHA-256与规划时一致；核心算法文件未修改。正式默认和学习记录未改变。

真实执行器/Judge编排尚未做外部验收，用户批准后才接入并做D01四组冒烟。评测客户端须统一显式thinking high、max_retries=0、timeout=120秒，V1临时预算作用域，V2逐任务保存预算；完整实际payload再次检查，含思考控制字段开销。端点/模型改变或认证/容量异常立即停止。
Judge固定规则、匿名ID、逐条件证据校验，自动判定未知不算正确。PDF解析冲突/过时教材文本单列，父子诊断不泄露身份。详细约束以审核包RUBRIC为准。

用户唯一审核尚未完成，所以此时没有`gold-approved.json`。不得自动创建此文件或先执行真实提取。批准后绑定原文/Gold/配置/规范/计划哈希，才进入后续自动步骤。

离线复核命令（不发模型请求）：

```powershell
.\.venv\Scripts\python.exe outline_benchmark.py validate
.\.venv\Scripts\python.exe -m unittest -v test_outline_benchmark
```

重新获取/规划需要使用新的`--directory`，已存在离线SQLite不覆盖；批准后的Gold/资料拒绝重新生成。
