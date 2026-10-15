# 正式问答的融合查找（2026-10-13）

已将实验 B 的同一算法接入网页问答。网页“资料查找”默认选“融合查找（推荐）”，可随时选择“原语义查找”；偏好在本浏览器保存，新提交捕获当前值。CLI、旧 API 和 `KnowledgeTools` 默认仍为 dense，既有 A 基线不会因网页上线而变成 B。C 暂未接入，API 只接受 dense/hybrid。

采用 B 的依据来自固定100条实验：72条可回答test中 Recall@10 A=.875/B=.916667，nDCG@5 A=.737015/B=.808604，前三完整证据A=.722222/B=.833333。仅8领域问题族、合成混合来源标签，无显著性或最终答案正确率结论。用户授权继续开发后采用可回退的网页方案，不根据已知测试失败修改 gold 或调参数。C多证据退化与额外成本的判断保留。

## 一次消息怎样流转

1. 网页把选择保存为 `retrieval_mode`，连同完整问题、资料身份、语言和提交身份发送；原输入仍先入 SQLite。
2. 后端解析当前版本与权限，构造同一 `KnowledgeTools`。构造与 `read_source` 不加载 embedding、不建 BM25；模型可以零工具调用或只读原文。
3. 只有实际调用 `search_notes` 才加载现有本地 E5、准备真实片段和向量。B取向量最多50、BM25最多50，以原参数等权RRF融合最多50，再按工具的top_k返回最多10。A保留原余弦排序。
4. BM25的统计与倒排只基于本次允许的预处理片段；同一工具实例内复用，跨请求重新构造。索引缓存从本应用数据库所在目录取得，正式仍用 `.data/embeddings.sqlite3`，隔离测试使用自身目录。
5. Agent对含分路分数的完整工具结果检查生成预算；若裁剪，引用资格随实际原文缩小。最终阶段再次检查自身Schema与材料预算，SourceOnlyAnswer和引用校验不变。原文不因词法归一化而改写，内部50候选不自动获得引用资格。
6. 最终请求另带已完成动作的简短程序记录（名称、成功/失败），说明证据获取已经执行；不传隐藏思维链、不把动作当知识证据。成功答复和上下文保存诊断；失败也保存本次模式与已发生的检索诊断，原输入及失败状态保留。

两阶段仍共享聊天背景。此处补动作记录的原因是真实样例发现最终模型不知道前阶段已搜过，误称搜索工具不可用；只传原文不能解决这类执行状态误解。动作记录进入完整请求预算，语言仍由现有偏好控制。

## 为什么这样修改代码

| 代码 | 变化与原因 |
| --- | --- |
| `knowledge_tools.py` | 接受程序配置的mode，共用现有BM25/RRF实现；保留懒加载、10条工具上限、原文与scope。记录分路原分/名次/RRF贡献及耗时，不把不同单位原分直接相加 |
| `web_app.py` / 网页模板 / `app.js` | 提交、记录、显示选择；失败轮次可换方式重试，完成轮次重放不再生成。旧消息没有新字段仍可展示 |
| `conversation_store.py` | 旧请求缺mode视为dense，仅在比较时补默认，不改原存档；比较仍按JSON保留布尔/数值类型区别。同一ID换新输入仍拒绝，失败诊断按attempt更新，不新建消息 |
| `agent_loop.py` / `model_client.py` | 存实际搜索/可见范围；最终阶段纳入程序动作记录与预算，不再仅收到原文却误解前一阶段状态 |
| `retrieval_benchmark_data.py` / `quality_retrieval.py` | Agent可显式CAS追加自己的未决项判定或排除，并用CLI冻结。完整原文初审、理由及版本仍需校验，不把pending盲改为成功，不覆盖用户修订/旧冻结 |
| `quality_production_retrieval.py` | 用正式工具核对已保存B的前十与RRF分数；显式live才请求当前生成服务，全部使用公开合成资料和隔离索引 |

## 自主评审规则

按用户本次授权，后续检索测试集的相关性、条件、证据范围和争议由Agent判定并留理由，不逐项请示。确定判断标Agent来源；确实不能可靠判断的样例明确排除，不能伪装成负例或成功。软件不自动推断语义等级：Agent须先提供完整审查文件，再由程序校验发布。

`adopt-audit --resolve-agent-disputes --expected-agent-revisions <JSON>` 只能追加到预期版本的Agent记录；已有用户决定/草稿仍受保护。`freeze --benchmark <身份> --note <判断及版本说明>` 可自主冻结全部已判定的版本。旧20及100条冻结、原2条人工确认仍保持历史来源，不追溯改称Agent决定。学习目标审核、用户主动加入计划和真实作答评分流程没有因此自动化。

## 运行与验证

正式服务已在 `http://127.0.0.1:8767/` 重启生效。手动运行仍使用项目已有venv：

```powershell
.\.venv\Scripts\python.exe -m uvicorn web_app:app --host 127.0.0.1 --port 8767
.\.venv\Scripts\python.exe -m unittest test_production_retrieval
.\.venv\Scripts\python.exe quality_production_retrieval.py --b-run .data/evaluations/retrieval-hybrid-e0116c7ffdec445b96ae2cce27937e79.json
.\.venv\Scripts\python.exe quality_production_retrieval.py --b-run .data/evaluations/retrieval-hybrid-e0116c7ffdec445b96ae2cce27937e79.json --live-agent
```

`--agent-only --cases greeting missing` 只复测指定真实样例，另存结果，不覆盖失败报告。该工具不读取私人笔记、聊天或学习记录，不自动加入计划。

真实正式工具100/100查询的前十及RRF分数与保存B一致，报告 `production-hybrid-af0f27791d084a99b08273e463579660.json`。这是排名接线核对，不是新的独立质量提升。首轮模型样例发现最终阶段错误描述工具可用性，修正后supported样例成功（`291e7e461658493680a00d2b51a78730`）；该次greeting/missing失败原样保留。定向复测两项成功（`902fa29771ef437ea402eccac8419b22`），不合并成虚假的一次全成功或语义成功率。Agent逐样例审查记录为 `production-hybrid-agent-review-20261013.json`。

独立公开资料网页测试完成真实提交/保存/引用/Trace展示，并实际切换A、刷新保持偏好。测试数据在 `.data/evaluations/production-hybrid-ui-20261013`，不写正式聊天和学习。旧输入重发、换模式重试幂等、预算裁剪引用、零工具调用和自动评审版本保护由受控测试验证；不能把这些测试当真实语义效果。

本批189项相关回归通过；最后细节修改后的25项定向复测与前者重叠，不相加，不称全量。两个JavaScript及10个修改Python语法通过。正式服务按最终代码重启，HTTP200及不支持的rerank模式422已核对；隔离测试服务已停止。保护报告 `production-hybrid-preservation-20261013.json` 确认旧数据库行、根设计文件、旧资料和冻结指纹保持，三个SQLite完整性ok、外键错误0。问题、解决方法及选择理由详见 `development-log.md` 最新节。

## 当前限制

BM25在每个请求的新工具实例内重建，模型沿用现有每工具懒加载机制，尚无跨请求模型池。公开网页样例的第一次搜索约8.36秒，含E5加载、准备向量与索引；实验B约19ms为成功暖查询，不能当正式首次问答或端到端耗时。网页诊断区分别显示排名返回与Agent/最终实际可见范围，分数不是相关概率。

当前生成服务仍可能请求失败或返回不合约输出；保留失败和原输入，支持重试，不放松结构与引用校验来追成功率。更多真实公开笔记、更多独立问题族、最终答案/拒答评测、配对区间、跨请求性能优化和C多证据策略仍后续。
