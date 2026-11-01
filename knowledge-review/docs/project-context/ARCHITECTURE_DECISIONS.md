# Knowledge Review 架构决策索引

> 更新日期：2026-10-26。源码基线`4f06786`，分支`codex/outline-extraction-v2`；任务识别阶段`e947e6b`。路径相对应用根目录knowledge-review。旧项按当前源码修订，新增决策注明本次讨论与实现依据；不能倒推为最初开发者动机。本次为文档静态核对，前一实现阶段266项相关受控回归通过，无真实V1/V2质量实验。

**摘要**：17项决策涵盖证据、恢复、固定目标、长输入、执行治理，以及新加入的人类澄清和可选V2。精确顺序见[CALL_CHAINS.md](CALL_CHAINS.md)，职责见[ARCHITECTURE.md](ARCHITECTURE.md)。已实现机制与已证明语义收益分开。

<a id="adr-index"></a>
## 目录与证据规则

| 决策 | 状态 | 主要影响 |
| --- | --- | --- |
| [ADR-001 模块化单体与有限工具 Agent](#adr-001) | 当前实现体现的设计 | 运行边界、模块耦合 |
| [ADR-002 版本化原文与实际可见证据](#adr-002) | 已确认的决策 | 引用、快照、PDF/表格 |
| [ADR-003 按需范围检索与生产 A/B、离线 C](#adr-003) | 已确认的决策 | 召回、性能、证据范围 |
| [ADR-004 离线向量与完整窗口、派生缓存](#adr-004) | 当前实现体现的设计 | 可复用性、长输入完整性 |
| [ADR-005 工具决策与最终结构化回答分离](#adr-005) | 已确认的决策 | 问答可控性、可见执行记录 |
| [ADR-006 原生结构化输出与受限协议降级](#adr-006) | 已确认的决策 | 供应商兼容、发布校验 |
| [ADR-007 完整请求预算与失败关闭](#adr-007) | 已确认的决策 | 长上下文、必需证据保护 |
| [ADR-008 SQLite 状态归属、快照与跨库事务](#adr-008) | 已确认的决策 | 持久化、删除一致性 |
| [ADR-009 幂等、文件锁及发布时版本检查](#adr-009) | 当前实现体现的设计 | 并发、失败重试、恢复 |
| [ADR-010 固定目标与可重放 FSRS 事件](#adr-010) | 已确认的决策 | 评分修订、调度一致性 |
| [ADR-011 用户确认的学习组织与有限推荐](#adr-011) | 已确认的决策 | 用户控制、建议边界 |
| [ADR-012 分层背景、候选对回执与原文复查](#adr-012) | 已确认的决策 | 大范围生成、恢复、合并可靠性 |
| [ADR-013 历史独立来源及删除撤销依赖](#adr-013) | 已确认的决策 | 记忆可信度、讨论练习有效性 |
| [ADR-014 共享有界池、事件 SSE 与协作取消](#adr-014) | 已确认的决策 | 资源治理、断连、后台执行 |
| [ADR-015 隔离评测与冻结审核证据](#adr-015) | 已确认的决策 | 结果可比性、质量结论边界 |
| [ADR-016 有限任务识别与持久澄清](#adr-016) | 已实现，意图质量待验证 | 任务边界、人类等待、业务入口 |
| [ADR-017 V2直接提取与逐任务预算](#adr-017) | 可选实验实现，V1仍默认 | 结构窗口、输出压力、对照与成本 |

“已确认”表示源码机制和现有记录有明确对应，不表示所有可能动机都有历史证据；“当前实现体现”只确认机制。每项**设计依据**区分记录明确说明的理由与尚未确认的动机；**备选方案及权衡**若无原始方案记录，一律标作分析性比较。被测试覆盖表示存在相应契约，不等于本次测试已通过。新增历史 ADR 时应保留证据和时间，不将推测改写为开发者原话。

<a id="adr-001"></a>
## ADR-001：模块化单体与有限工具 Agent

- **状态**：当前实现体现的设计。
- **背景与问题**：一个个人资料应用同时需要同步浏览、模型交互、学习状态和持久后台作业，且有不经网页的 CLI/评测调用。
- **当前方案**：FastAPI 装配 Python 模块；浏览器使用模板及共享全局状态的原生脚本。服务/Store 可由网页、CLI 或评测直接调用。普通问答使用 `run_agent` 的有限工具循环，最终回答单独生成；清单、归组、路线等使用各自持久工作流。逻辑分层不等于严格无环依赖，存在延迟导入和对具体 Store 的耦合。
- **设计依据**：`create_app`、服务接口和 CLI 直接调用证明运行结构；`AGENTS.md` 的当前业务边界说明有限工具执行。未找到“为什么采用单体而不拆服务”的独立历史决策，部署简化只能视为待验证工程解释。
- **备选方案及权衡**：**分析性比较**：拆分服务可独立扩缩模型执行，但需远程状态协议和跨服务一致性；引入通用 Agent 框架可减少工具协议代码，但不能自动替代当前的证据、状态发布和恢复约束。没有证据表明这些是当时实际候选。
- **代价与限制**：`web_app.py` 承担大量协调，模块边界主要靠约定；UI 脚本有加载顺序和共享状态耦合。工作池只是单进程资源限制，不能使系统成为分布式服务。
- **源码证据**：[web_app.py](../../web_app.py)：`create_app`、`execute_turn`；[main.py](../../main.py)：`main`；[agent_loop.py](../../agent_loop.py)：`run_agent`；[index.html](../../web/templates/index.html)；[AGENTS.md](../../AGENTS.md)；`test_agent_loop.py`、`test_web_app.py`。
- **相关模块与调用链**：[M01/M05/M09/M12](ARCHITECTURE.md#module-map)；[F03 CLI](CALL_CHAINS.md#f03)、[F04 问答](CALL_CHAINS.md#f04)、[F11 清单](CALL_CHAINS.md#f11)。

<a id="adr-002"></a>
## ADR-002：版本化原文与实际可见证据

- **状态**：已确认的决策。
- **背景与问题**：文件修改、PDF 解析差异、表格分块及预算裁剪都会改变“模型究竟看过什么”，仅验证文件名或存在行号不足以约束引用。
- **当前方案**：管理身份用稳定 `document_id`，内容身份用 `Source.source_id`；Markdown 绑定路径和文本，结构化 PDF 进一步绑定解析产物。`Chunk` 记录连续实际原文，表头/标题/注释等单列 `context_ranges`，裁剪后重算覆盖。最终校验依据实际送入请求的文本和范围；历史引用、视觉观察与文档引用分开。导入问题和部分解析显式保存；表格提取行数不等于整份 PDF 表格完整性。
- **设计依据**：`context.validate_chunks`、`answers.validate_answer`、PDF/表格测试直接体现“可见范围授权”。[统一文档与表格设计](../pdf-document-design.md)记录结构/视觉来源边界；[资料管理记录](../library-and-chat-practice.md)说明稳定身份与替换版本的关系。具体哈希组成由实现确认，不将哈希本身解释成语义正确性证明。
- **备选方案及权衡**：**分析性比较**：仅按路径/当前全文引用更简单，但无法区分历史练习与替换后资料；把非连续表头混入正文可减少字段，却会制造虚假连续行范围；把视觉观察当逐字原文可统一接口，但会掩盖转写不确定性。
- **代价与限制**：版本更新导致向量缓存和当前作用域失效；老练习需保留快照。引用范围合法不证明结论受其支持；PDF 跨页表格、视觉识别与 OCR 仍可能缺失或误读。
- **源码证据**：[sources.py](../../sources.py)：`Source`、`source_from_document`；[document_model.py](../../document_model.py)：`Document`；[chunking.py](../../chunking.py)；[context.py](../../context.py)：`validate_chunks`；[answers.py](../../answers.py)：`validate_answer`；[table_reading.py](../../table_reading.py)；[pdf_parser.py](../../pdf_parser.py)；`test_sources.py`、`test_context.py`、`test_pdf_documents.py`、`test_table_reading.py`。
- **相关模块与调用链**：[M02/M03/M05、核心实体](ARCHITECTURE.md#data-model)；[F01 导入](CALL_CHAINS.md#f01)、[F02 版本管理](CALL_CHAINS.md#f02)、[F05 证据工具](CALL_CHAINS.md#f05)。

<a id="adr-003"></a>
## ADR-003：按需范围检索与生产 A/B、离线 C

- **状态**：已确认的决策。
- **背景与问题**：全文请求和无条件检索都有成本；纯 dense 可能漏掉标识符、数字和中文短术语；重排又增加延迟和依赖。
- **当前方案**：Agent 自行选择是否检索，未调用工具不加载 embedding。`KnowledgeTools` 只检索显式许可资料：dense 或 hybrid；hybrid 合并 dense50 与同范围 BM25≤50，等权 RRF 得到≤50，再返回请求的前≤10。浏览器默认 hybrid，API/CLI 默认 dense。离线 C 在固定 B 候选池上运行本地交叉编码器，生产参数拒绝 rerank。检索失败显式报错，没有生产静默降级为另一检索算法。
- **设计依据**：[production-hybrid-search.md](../production-hybrid-search.md)明确记录 B 的接入及质量/耗时证据边界；[检索评测设计](../retrieval-benchmark-design.md)的 C 节记录固定池实验。源码确认作用域和惰性加载。历史实验支持当时固定集上的比较，不能扩写成任意私人资料上 B 必然更好。
- **备选方案及权衡**：已有 A/B/C 是真实实验选项；A 依赖少、路径短，B 额外计算词项统计和融合，C 增加本地模型/窗口及成对计算成本。将 C 上生产仍需单独评估，现有 C 产物不是该决策已经作出的证据。
- **代价与限制**：BM25 统计随当前许可范围变化；Han 单/双字和代码词规则不是通用中文分词器。RRF 无学习权重；工具上限与候选上限可能限制多证据问题。Agent 不搜也合法，但未获得资料证据时不能假装已检索。
- **源码证据**：[knowledge_tools.py](../../knowledge_tools.py)：`KnowledgeTools.search_notes`；[hybrid_retrieval.py](../../hybrid_retrieval.py)；[retrieval.py](../../retrieval.py)；[app.js](../../web/static/app.js)；[rerank_retrieval.py](../../rerank_retrieval.py)；`test_production_retrieval.py`、`test_hybrid_retrieval.py`、`test_agent_loop.py`。
- **相关模块与调用链**：[M04/M05/M13](ARCHITECTURE.md#module-map)；[F04 问答](CALL_CHAINS.md#f04)、[F05 检索工具](CALL_CHAINS.md#f05)、[F17 检索实验](CALL_CHAINS.md#f17)。

<a id="adr-004"></a>
## ADR-004：离线向量与完整窗口、派生缓存

- **状态**：当前实现体现的设计。
- **背景与问题**：本地模型可能截断长行/长查询；模型、分词器或配置变化后复用旧向量可能形成不可见错误；跨请求重算又有成本。
- **当前方案**：默认 CPU、`local_files_only`、不信任远程代码；下载显式执行。按原行准备块，单长行保留原引用身份，使用完整字符窗口编码并聚合归一化；查询同样不静默截断。profile 包含实际权重/分词器/运行库和窗口设置。SQLite 缓存校验输入哈希、profile、向量维数/有限性/校验和，损坏作为 miss，先计算再短事务写入。旧缓存不参与越范围召回。
- **设计依据**：`LocalEmbedder`、`get_document_vectors` 与 `test_embeddings.py`/`test_vector_index.py` 确认完整覆盖和缓存隔离契约；[文档问答与预算](../document-rag-and-token-budget.md)的超长内容章节说明完整行引用与窗口聚合理由。未找到涵盖所有 profile 字段的原始 ADR，不能断言每个字段的历史选择理由。
- **备选方案及权衡**：**分析性比较**：云 embedding 可免本地安装，但引入资料上传、服务和计费边界；简单截断更快却丢弃尾部；仅模型名作缓存键无法区分实际权重变化。窗口聚合保证处理覆盖，不保证语义召回最佳。
- **代价与限制**：模型加载/指纹与长窗口编码消耗 CPU/内存；当前没有跨请求共享模型服务或全局模型对象池。缓存可积累旧版本，不是历史资料访问接口；向量统计上的相似度也不等于证据蕴含。
- **源码证据**：[embeddings.py](../../embeddings.py)：`LocalEmbedder`；[embedding_config.py](../../embedding_config.py)；[vector_index.py](../../vector_index.py)：`get_document_vectors`、`IndexResult`；[conversation_memory.py](../../conversation_memory.py) 的独立历史向量缓存；`test_embeddings.py`、`test_vector_index.py`、`test_knowledge_tools.py`。
- **相关模块与调用链**：[M04、存储基础设施](ARCHITECTURE.md#infrastructure)；[F03 本地 CLI](CALL_CHAINS.md#f03)、[F05 检索](CALL_CHAINS.md#f05)、[F06 历史](CALL_CHAINS.md#f06)。

<a id="adr-005"></a>
## ADR-005：工具决策与最终结构化回答分离

- **状态**：已确认的决策。
- **背景与问题**：工具选择需要多轮交互，面向用户的结果又需要稳定分区、可见证据校验，且模型可能忘记先前工具已经执行。
- **当前方案**：`run_agent` 最多进行显式配置的有限工具调用；其自然语言输出不直接发布。`answer_question` 根据实际收集并裁剪后的文档、历史、视觉观察独立生成最终结构化答案；`completed_actions` 是程序生成的简短执行事实，不是文档证据或隐藏推理。严格模式使用只含资料来源的 Schema，普通模式区分资料依据、推导、补充。
- **控制分支补充**：上述两阶段适用于普通问答。持久聊天的ask_question/select_task可以返回AgentRun.control，跳过最终答案模型；澄清不伪装成已完成的资料回答。见ADR-016。
- **设计依据**：[文档问答设计](../document-rag-and-token-budget.md)和 `agent_loop`/`model_client` 说明工具和最终结果分工；[production-hybrid-search.md](../production-hybrid-search.md)记录真实模型在最终阶段误称“未搜索”的现象及执行记录补充。没有证据证明分两阶段本身就能提高所有回答的正确率。
- **备选方案及权衡**：**分析性比较**：直接发布工具阶段文本可以少一次请求，但需另解输出契约与可见证据校验；单一无限循环更灵活，却失去明确调用预算和发布入口。当前工具循环可能零调用，也可能因失败耗尽上限。
- **代价与限制**：增加模型请求/协议预算和阶段间信息组装；执行摘要不能替代真实引用。最终阶段仍可能语义错误；结构检查只能拒绝部分非法输出。历史记忆参与不改变严格资料引用规则。
- **源码证据**：[agent_loop.py](../../agent_loop.py)：`run_agent`；[model_client.py](../../model_client.py)：`answer_question`；[answers.py](../../answers.py)：`SourceOnlyAnswer`、`NoteAnswer`；`test_agent_loop.py`、`test_model_client.py`、`test_production_retrieval.py`。
- **相关模块与调用链**：[M05/M06](ARCHITECTURE.md#module-map)；[F04 双阶段问答](CALL_CHAINS.md#f04)、[F05 工具](CALL_CHAINS.md#f05)。

<a id="adr-006"></a>
## ADR-006：原生结构化输出与受限协议降级

- **状态**：已确认的决策。
- **背景与问题**：兼容 Chat Completions 的服务对原生 response format、强制 tool choice 和图片能力支持并不一致；不能把权限/网络错误误判为能力缺失。
- **当前方案**：优先 SDK 原生 parse；只有明确的 response-format 不支持响应才降级到单个 `submit_result` 工具，强制选择不支持时才改 auto，仍要求唯一合格结果。缓存端点/模型的成功协议偏好；每次真实尝试都执行预算/状态守卫，返回后仍进行 Pydantic 和业务验证。视觉能力是独立的进程缓存，鉴权、限流、超时等不写成“不支持图片”。
- **设计依据**：[AGENTS.md](../../AGENTS.md) 的结构化任务兼容说明、[PDF 实施记录](../pdf-document-design.md)及对应协议测试记录兼容行为；源码限制了降级条件。不能据一次 400 就认为同供应商全部模型都不支持某能力。
- **备选方案及权衡**：已实现原生/函数两条路径；**分析性比较**：统一自由文本 JSON 提取可能适用更广服务，但放宽了响应约束；对所有异常自动降级容易掩盖真正的凭据/配额故障。
- **代价与限制**：一次逻辑生成可能产生多次 HTTP 请求；能力缓存随进程重置，并非持久供应商配置。协议成功不等于语义或发布条件成功，也不代表服务端一定严格执行 Schema。
- **源码证据**：[structured_output.py](../../structured_output.py)：`request_structured`；[pdf_vision.py](../../pdf_vision.py)；[generation_budget.py](../../generation_budget.py)；`test_structured_output.py`、`test_pdf_vision_settings.py`、`test_model_client.py`。
- **相关模块与调用链**：[M03/M06](ARCHITECTURE.md#module-map)；[F04 问答](CALL_CHAINS.md#f04)、[F05 视觉](CALL_CHAINS.md#f05)、[F16 取消](CALL_CHAINS.md#f16)。

<a id="adr-007"></a>
## ADR-007：完整请求预算与失败关闭

- **状态**：已确认的决策。
- **背景与问题**：只计算原文忽略提示、工具及展开 Schema 会低估输入；随意截断可能删除选区、引文表头、学习目标或待评分答案。
- **当前方案**：`GenerationBudget` 以完整消息、协议 Schema/工具、输出预留及安全余量估算；原生和函数两种协议都做检查。可选原文按完整行/受保护结构裁剪，明确必需内容容不下则失败，不偷偷丢弃。会话按完整轮次摘要；清单/归组/路线采用可恢复分页、拆分和调用额度。估计器采用保守 UTF-8 字节等规则，不宣称是目标模型 tokenizer 的精确计数。
- **V2补充（2026-10-28更新）**：全局默认上下文/输出/余量为131072/12288/2048，显式环境值优先，不是供应商上限。V2按任务保存预算，恢复及最终SDK检查使用同一保存值；模型硬容量、产品策略和输出预留不同。仍未替换保守计数器，不能声称精确使用128k真实token。历史32k/4096实验配置不改写。见ADR-017。
- **设计依据**：[文档问答与预算](../document-rag-and-token-budget.md)和[会话第一阶段](../conversation-memory-phase1.md)记录完整请求预算目标；`test_context.py`、`test_model_client.py`、清单相关测试确认必需证据保护和协议体积检查。各阈值是当前配置/启发式，容量默认值不是服务端真实能力探测。
- **备选方案及权衡**：**分析性比较**：仅截断原文实现简单，但无法保护业务输入；每个远程模型精确 tokenizer 可提高利用率，却需要版本映射且不覆盖供应商隐藏开销；完全依赖服务报超限会在状态处理后才发现失败。
- **代价与限制**：保守估计可能过早拒绝或多拆分；生成输出仍可能截断。明确截断错误才触发部分流程缩页，不能把所有模型错误当成自动重试依据；进度覆盖不证明语义完整。
- **源码证据**：[generation_budget.py](../../generation_budget.py)：`GenerationBudget`、`fit_chunks`；[context.py](../../context.py)：`validate_chunks`；[conversation_context.py](../../conversation_context.py)：`build_context`；[outline_context.py](../../outline_context.py)；[outline_group_steps.py](../../outline_group_steps.py)；[outline_routes.py](../../outline_routes.py)；`test_context.py`、`test_outline_context.py`、`test_outline_routes.py`。
- **相关模块与调用链**：[M06/M08/M09/M10](ARCHITECTURE.md#module-map)；[F06 会话压缩](CALL_CHAINS.md#f06)、[F11 清单](CALL_CHAINS.md#f11)、[F12 归组](CALL_CHAINS.md#f12)、[F15 路线](CALL_CHAINS.md#f15)。

<a id="adr-008"></a>
## ADR-008：SQLite 状态归属、快照与跨库事务

- **状态**：已确认的决策。
- **背景与问题**：同一次业务行为可能影响作答、学习目标、复习事件和生成来源；资料替换或聊天删除后，必须能解释旧记录并阻止失效派生物继续使用。
- **当前方案**：学习/练习/目录覆盖/清单在学习库；聊天/依赖/历史向量在会话库；文档向量和检索审核另库。业务生成保存输入快照及来源。通过已配置路径的 SQLite `ATTACH`，聊天删除与派生练习撤销、会话完成时学习资料检查可纳入当前事务。Markdown 替换使用数据库 override；PDF 文件与 SQLite 并不是一个通用事务。Store 采用手写 SQL、启动建表及兼容迁移，不是 ORM 迁移系统。
- **设计依据**：[library-and-chat-practice.md](../library-and-chat-practice.md)明确说明 override 避免磁盘/数据库半提交、跨库撤销和来源追踪；[conversation-memory-phase2.md](../conversation-memory-phase2.md)说明历史向量随会话事务清理。源码确认默认连接和 ATTACH，不应未经部署检查就更改为 WAL 并沿用跨文件原子性假设。
- **备选方案及权衡**：**分析性比较**：单库减少跨库配置但改变隔离/生命周期；外部数据库与对象存储便于服务化，却需不同事务和版本机制；覆盖原始文件简单，但增加旧来源重建和中途失败风险。
- **代价与限制**：SQLite 写入串行、连接/文件锁约束；文件产物与数据库故障可能留下孤立资源。数据库快照支持业务解释，不提供完整备份、迁移回滚或用户历史版本浏览。删除不等于远端供应商清除、物理安全擦除或全目录垃圾回收。
- **源码证据**：[practice_store.py](../../practice_store.py)、[learning_store.py](../../learning_store.py)、[library_store.py](../../library_store.py)、[conversation_store.py](../../conversation_store.py)、[practice_provenance.py](../../practice_provenance.py)、[web_app.py](../../web_app.py)：`create_app`；`test_library.py`、`test_management_chat_practice.py`、`test_outline_integration.py`。
- **相关模块与调用链**：[存储与基础设施](ARCHITECTURE.md#infrastructure)；[F02 管理](CALL_CHAINS.md#f02)、[F07 删除](CALL_CHAINS.md#f07)、[F10 讨论练习](CALL_CHAINS.md#f10)、[F14 原子加入](CALL_CHAINS.md#f14)。

<a id="adr-009"></a>
## ADR-009：幂等、文件锁及发布时版本检查

- **状态**：当前实现体现的设计。
- **背景与问题**：重复点击、客户端重试、模型调用期间资料修改和进程中断可能产生重复结果或把过期生成写入当前状态。
- **当前方案**：请求 ID、唯一索引和状态记录承担各流程幂等；OS 文件锁保护聊天生成、清单作业及关键加入操作。模型调用通常在写事务外，发布前校验 attempt、对象 revision、scope/source 或 memory revision；事务内只提交仍有效结果。持久作业保留步骤/回执，暂停撤销 attempt，继续从既有状态恢复。并非所有端点都具有相同锁/检查组合，细节需查对应 Store。
- **设计依据**：会话/清单/归组/加入测试包含重复请求、并发修改、过期发布、回滚及恢复契约；代码充分确认机制。未发现统一记载所有锁设计动机的历史 ADR；降低长事务锁占用是机制产生的工程效果，不能当成已知原始动机。
- **备选方案及权衡**：**分析性比较**：把模型调用包在数据库写事务中更直观但锁占用长；仅客户端禁用按钮挡不住重放；队列系统可分离执行，但同样需要幂等和发布版本检查。文件锁不是分布式租约。
- **代价与限制**：恢复逻辑和状态机复杂；进程退出需要显式恢复，不能承诺 exactly-once 模型计费。个别当前来源检查位于事务外，不能笼统宣称所有文件/数据库状态有同一个原子快照。已完成结果可能在断连前已持久化。
- **源码证据**：[conversation_store.py](../../conversation_store.py)：`generation_lock`、`begin`、`finish`；[outline_store.py](../../outline_store.py)；[outline_group_store.py](../../outline_group_store.py)；[outline_integration.py](../../outline_integration.py)；[agent_work.py](../../agent_work.py)：`job_boundary`；`test_conversations.py`、`test_outlines.py`、`test_outline_integration.py`、`test_agent_governance.py`。
- **相关模块与调用链**：[核心状态](ARCHITECTURE.md#data-model)；[F04 会话发布](CALL_CHAINS.md#f04)、[F11–F14 持久作业](CALL_CHAINS.md#f11)、[F16 治理](CALL_CHAINS.md#f16)。

<a id="adr-010"></a>
## ADR-010：固定目标与可重放 FSRS 事件

- **状态**：已确认的决策。
- **背景与问题**：单次题目、同一能力的变式、追问和新的学习维度并不等价；评分修订或用户改 Rating 不能简单再追加一次复习。
- **当前方案**：`KnowledgeUnit → LearningTarget → LearningQuestion` 分离知识、固定能力和题面。变式复用目标，新维度新建目标；真实答案先保存再评分。只有合格的首题事件、明确评分、无记录辅助等条件满足才映射 FSRS Rating，追问不自动改变调度。评估修订追加版本、选有效版本，用户 Rating 可保留；按真实发生时间重放有 Rating 的事件。FSRS 版本固定 `6.3.2`、禁用 fuzz，参数版本检查后重算。
- **设计依据**：[知识与题目模型](../knowledge-question-model-design.md)及[学习闭环实现](../learning-loop-implementation.md)记录能力/题面与证据边界；`learning_state`/`review_scheduler` 和测试明确重放、辅助条件、追问排除。不得从 FSRS 接入推导出“已实现可靠掌握度估计”。
- **备选方案及权衡**：**分析性比较**：每题独立卡片更简单，但同一能力变式状态碎片化；把追问/看到答案后的成功也计复习会混淆测量条件；只修改最新 card 而不重放不能一致处理历史评分修订。
- **代价与限制**：需保存不可变题目目标、真实时间、辅助信息和评估修订；语义评分仍可能错误。调度资格是当前工程规则，不是其教育学有效性已验证；加入旧练习不伪造历史复习事件。
- **源码证据**：[learning_types.py](../../learning_types.py)、[learning_store.py](../../learning_store.py)、[learning_state.py](../../learning_state.py)、[learning_service.py](../../learning_service.py)、[review_scheduler.py](../../review_scheduler.py)；[practice_service.py](../../practice_service.py)；`test_learning.py`、`test_practice.py`。
- **相关模块与调用链**：[M07、学习实体](ARCHITECTURE.md#data-model)；[F08 练习评分](CALL_CHAINS.md#f08)、[F09 目标/调度](CALL_CHAINS.md#f09)。

<a id="adr-011"></a>
## ADR-011：用户确认的学习组织与有限推荐

- **状态**：已确认的决策。
- **背景与问题**：自动生成的清单、能力合并和前置关系可能有误，不能因为生成成功就改变用户的学习计划或复习状态。
- **当前方案**：候选、归组、路线建议分别保存；用户审核/接受与真正加入计划是不同状态迁移。重复能力组绑定代表项和知识单元，跨章主题不直接折叠成同一能力；待原文核对项不会直接成为可加入依据。路线只接受无环关系或完整用户顺序。推荐综合固定目标有效作答、待完成练习、FSRS 到期、缺口与路线，给理由，不自动作答/加入/更改调度。
- **设计依据**：[collection-learning-outline.md](../collection-learning-outline.md)及[mastery-path-design.md](../mastery-path-design.md)开头的用户确认/实施补充明确建议与主动加入边界；后者较早规划和外部项目分析不视为本项目实现。`selection_view`、`ordered_nodes` 和 `recommend_next` 体现当前筛选。推荐采用的优先级是实现规则，未见真实用户研究证明它最优。
- **备选方案及权衡**：**分析性比较**：生成后自动加入省交互，但会放大错误合并和无效任务；严格前置闸门能限制路径，却可能阻止用户探索。当前展示建议，未建概率掌握模型或自动课程规划器。
- **代价与限制**：用户审核和重复确认增加操作；未加入项没有正式复习状态。接受路线仍不证明教育上的必要前置；用户边编辑会清除模型证据标签、标作 user，避免伪装成模型原始判断。
- **源码证据**：[outline_service.py](../../outline_service.py)、[outline_group_store.py](../../outline_group_store.py)、[outline_integration.py](../../outline_integration.py)、[outline_routes.py](../../outline_routes.py)、[outline_route_store.py](../../outline_route_store.py)、[route_recommendation.py](../../route_recommendation.py)；`test_outline_groups.py`、`test_outline_routes.py`、`test_outline_integration.py`。
- **相关模块与调用链**：[M09/M10](ARCHITECTURE.md#module-map)；[F12 归组](CALL_CHAINS.md#f12)、[F14 加入](CALL_CHAINS.md#f14)、[F15 路线推荐](CALL_CHAINS.md#f15)。

<a id="adr-012"></a>
## ADR-012：分层背景、候选对回执与原文复查

- **状态**：已确认的决策。
- **背景与问题**：长资料可能面临输入、输出或应用预算压力；能否直接处理取决于实际模型和任务，不能以32k回退值证明1M服务必须分层。候选关系比较另有跨章覆盖及非传递合并问题。
- **当前方案**：V1清单仍默认采用full/hierarchical路由，分层leaf brief→章节/文档reduce→带背景回原文提取。V2已实现原文直接提取但仅显式启用，见ADR-017。候选归组的outline_group_plan是另一条链：全候选对计划/卡片假设→原文逐对核验→checked/deferred回执；合组要求全部候选对正向证明，不凭传递连通。不确定关系进入needs_review，有限原文复查最多两轮、每轮两次定位/补读。
- **设计依据**：[collection-learning-outline.md](../collection-learning-outline.md)记录背景不足、跨章卡和有限复查演进；`proposals_from_proofs`、计划/步骤/复查测试是精确契约。卡片只能提出假设、原文复核和非传递合并是明确边界。分层阈值/页大小来自实现启发式，不宣称理论最优。
- **备选方案及权衡**：已有小输入直接/大输入分层路径、旧单次 GroupDraft 和新版持久计划；**分析性比较**：单请求成本低但受预算及输出覆盖限制；全部原文全候选对直接比较更直接，但体积与调用量增长；并查集式传递合并不能表达逐对同能力证明。
- **代价与限制**：候选对最坏平方增长；背景压缩/路由可能漏语义信息，即使分配回执完整也不保证正确。新旧计划并存增加恢复复杂度；未绑定清晰关系的 unresolved/revise 结论不能自动应用。明确截断可缩页，其他错误不会无限自动重试。
- **源码证据**：[outline_context.py](../../outline_context.py)、[outline_service.py](../../outline_service.py)、[outline_group_plan.py](../../outline_group_plan.py)、[outline_group_steps.py](../../outline_group_steps.py)、[outline_grouping.py](../../outline_grouping.py)、[outline_review.py](../../outline_review.py)；`test_outline_context.py`、`test_outline_groups.py`、`test_outline_review.py`。
- **相关模块与调用链**：[M09/M10](ARCHITECTURE.md#module-map)；[F11 清单](CALL_CHAINS.md#f11)、[F12 整合](CALL_CHAINS.md#f12)、[F13 原文复查](CALL_CHAINS.md#f13)、[F15 路线分页](CALL_CHAINS.md#f15)。

<a id="adr-013"></a>
## ADR-013：历史独立来源及删除撤销依赖

- **状态**：已确认的决策。
- **背景与问题**：助手旧结论不应变成正式资料证据；跨聊天引用、摘要和从讨论生成的练习又可能继续依赖已删除内容。
- **当前方案**：原始消息不可被摘要覆盖；完整轮次压缩与显式历史搜索/读取分开。历史引用使用消息 ID/revision/原话字符范围，和文档 Citation 不互换；允许归档聊天检索，排除失效助手证据。助手保守继承依赖；删除沿图撤销派生记忆、清摘要/历史向量并增加 memory revision。讨论练习使用独立无资料引文的 Schema，保存可核验原话 basis；删除来源通过 ATTACH 事务撤销 basis/jobs，真实答案与调度历史保留但禁止继续派生操作。
- **设计依据**：[conversation-memory-phase2.md](../conversation-memory-phase2.md)、[library-and-chat-practice.md](../library-and-chat-practice.md)明确原话/摘要、历史/资料、向量同事务清理和来源撤销边界。保守依赖是明确机制，不能把它宣传成精确语义依赖追踪。
- **备选方案及权衡**：**分析性比较**：只删除当前聊天较简单，但残留派生物继续被当有效来源；追踪精确句级因果可减少误撤销，却需无法由当前结构校验保证的语义判断；以摘要替代原话降低空间但失去可复核的来源跨度。
- **代价与限制**：保守撤销可能影响很多后续答案，其他聊天的失效助手文本仍可显示但不再合格召回；历史搜索可能扫描全部合格消息并计算向量，阈值未校准。取消/本地删除不保证远端遗忘；来源撤销不篡改真实学习行为历史。
- **源码证据**：[conversation_context.py](../../conversation_context.py)、[conversation_memory.py](../../conversation_memory.py)、[conversation_store.py](../../conversation_store.py)：`delete`；[chat_practice.py](../../chat_practice.py)、[practice_provenance.py](../../practice_provenance.py)；`test_conversation_memory.py`、`test_conversations.py`、`test_management_chat_practice.py`。
- **相关模块与调用链**：[M08/M11](ARCHITECTURE.md#module-map)；[F06 上下文](CALL_CHAINS.md#f06)、[F07 删除](CALL_CHAINS.md#f07)、[F10 讨论练习](CALL_CHAINS.md#f10)。

<a id="adr-014"></a>
## ADR-014：共享有界池、事件 SSE 与协作取消

- **状态**：已确认的决策。
- **背景与问题**：HTTP 断连、后台任务和标题生成都可能继续占用同步模型线程；仅发出“取消”或 HTTP 返回并不代表工作真的退出。
- **当前方案**：11个bounded请求入口、两个聊天SSE入口、8个后台提交/继续入口及标题共享StreamRunner；Web端VLM启用的PDF整次解析也经streams.run准入。信号量先预留、满503、无执行器积压；后台202后仍持槽，标题忙则跳过。SSE包含meta/progress/delta/result/clarification/error/done，正文delta是未完成预览，正式结果需结构/引用/存储成功。ContextVar取消令牌关闭模型HTTP并等待清理，Future真退出才释放；业务暂停同时撤销attempt。人类等待保存后结束执行，不长期占槽。
- **设计依据**：[gateway-sse.md](../gateway-sse.md)末段及四组执行治理测试确认最新覆盖、入场顺序、标题独立槽和真实退出后释放。文件早期“尚未治理后台/标题/断连”段落为历史状态，不能用于描述当前实现。
- **备选方案及权衡**：**分析性比较**：无界 executor 队列隐藏超载；分开无上限标题/后台池仍可超过总容量；仅停止 SSE 发送无法终止模型 I/O；强杀线程不能安全维持 Python/SQLite 状态。分布式队列是另一部署方案，当前未实现。
- **代价与限制**：不是跨进程全部模型流量上限；CLI、独立评测及直接服务调用须逐项核对。CPU/文件/数据库/DNS阻塞不可强杀，远端停算未知。VLM启用时本地解析阶段也占模型槽，是现有最小复用策略的代价。SSE无事件重放，预览不能当正式证据；上限/指标为单进程，无内置租户鉴权。精确范围见F16。
- **源码证据**：[agent_stream.py](../../agent_stream.py)：`StreamRunner`；[agent_cancel.py](../../agent_cancel.py)：`CancellableTransport`；[agent_work.py](../../agent_work.py)：`job_boundary`；[web_app.py](../../web_app.py)；`test_agent_sse.py`、`test_agent_admission.py`、`test_agent_cancellation.py`、`test_agent_governance.py`。
- **相关模块与调用链**：[M01/M12、运行边界](ARCHITECTURE.md#runtime)；[F16 入场/SSE/取消](CALL_CHAINS.md#f16)、[F06 标题](CALL_CHAINS.md#f06)、[F11 后台任务](CALL_CHAINS.md#f11)。

<a id="adr-015"></a>
## ADR-015：隔离评测与冻结审核证据

- **状态**：已确认的决策。
- **背景与问题**：流程契约通过不等于模型语义质量；复用变化中的标签或给不同检索方案不同语料/候选池会使比较失真；评测不能污染真实学习数据。
- **当前方案**：合成版本化 suite、独立输出和数据库；validate/smoke/live 明确区分，live 显式开启且不把参考答案/关系标签塞入生成请求。检索 manifest 绑定实际 prepared chunks/profile/来源/范围；审核追加 revisions，user 与 agent 来源分开、空标签不当0。freeze 生成不可变标签与审查快照；score 复用保存 trace 和同一 freeze，实际调用 BEIR。B 使用同一范围，C 绑定完整 B digest/候选池，不重新召回；缺失/失败保留质量分母，分区/负例/排除单列，无 freeze 不报质量数值。
- **设计依据**：[检索评测设计](../retrieval-benchmark-design.md)的 A/B/C 章节、[质量评测说明](../quality-evaluation.md)及 `evals/` 协议记录实际实验约束；`test_evaluation` 明确 smoke 不产语义质量率、live 不泄漏标准答案。标签可混合 Agent 初审与用户审核，不能称为外部人工 gold。
- **备选方案及权衡**：真实 A/B/C 对照是已存在方案；**分析性比较**：每次 score 都重跑模型会混入模型/时延变化；直接修改旧标签/旧报告节省文件但失去版本可追溯；只统计成功案例会高估有效性。
- **代价与限制**：冻结审核成本高，固定合成分布不能外推所有私人资料；C 的已有池纯重排时延与 B 相加只是端到端估计。质量审核本身仍依赖语义判断；专项输入与真实提取端到端覆盖要分开。本次文档分析没有重跑任何历史实验或回归测试。
- **源码证据**：[quality_eval.py](../../quality_eval.py)、[evaluation_cases.py](../../evaluation_cases.py)、[evaluation_report.py](../../evaluation_report.py)、[retrieval_benchmark_data.py](../../retrieval_benchmark_data.py)、[retrieval_benchmark_metrics.py](../../retrieval_benchmark_metrics.py)、[retrieval_comparison.py](../../retrieval_comparison.py)、[rerank_retrieval.py](../../rerank_retrieval.py)；`test_evaluation.py`、`test_retrieval_benchmark.py`、`test_retrieval_expansion.py`、`test_rerank_retrieval.py`。
- **相关模块与调用链**：[M13、验证边界](ARCHITECTURE.md#evidence)；[F17 检索评测](CALL_CHAINS.md#f17)、[F18 学习/记忆评测](CALL_CHAINS.md#f18)。

<a id="adr-016"></a>
## ADR-016：有限任务识别与持久澄清

- **状态**：2026-10-26已实现，真实模型任务识别质量尚未验证。
- **背景与问题**：“帮我整理/学一下”可能指问答、清单或练习；猜错交付物会造成不必要的大任务。用户可能刷新或长时间后才回答，不能阻塞工作线程等待。
- **当前方案**：TaskRouter注册有限任务和后端控制的标签；持久聊天首次Agent决策按需ask_question或select_task，不增加必经分类模型。ask_question以结构化卡片保存waiting_user/id/revision，用户答复单独保存，原request_json不覆盖。成功控制动作独占响应并停止最终模型；等待释放真实工作，答复后重新准入、核对版本。业务任务当前返回入口卡片，在原界面核对对象和集合后执行。
- **可考证依据**：本轮用户明确提出不确定时显示功能选择器，并授权先实现该方案。此前设计和本次实现见[方案](../task-routing-and-clarification-proposal.md)、[实现](../task-routing-implementation.md)，不倒造更早的选型经历。
- **替代及权衡**：固定分类器多一次必经调用；纯关键词难处理跨轮语义；每次展示全目录增加操作负担。当前模型按需判断仍可能误判，需中文意图用例及无必要澄清率评测。同步阻塞等待虽代码短，却占线程、连接且无法刷新恢复。
- **边界**：目前为2–4个任务选项及自由文本，不是任意对象选择器或多阶段任务引擎；复杂对象在原界面选。完整大资料总结标为不可执行，清单选择不自动加入计划。同卡重复答复幂等、冲突拒绝，最多三轮。旧Gateway网页使用retry兼容保存路径，短暂准入；独立clarification API不调用模型但不在旧网关白名单。
- **证据**：[task_routing.py](../../task_routing.py)、[agent_loop.py](../../agent_loop.py)、[conversation_store.py](../../conversation_store.py)、`web_app.execute_turn/answer_clarification/retry_turn`、`test_task_routing.py`、`test_chat_stream.cjs`；[F19](CALL_CHAINS.md#f19)。

<a id="adr-017"></a>
## ADR-017：可选V2原文直接提取与逐任务预算

- **状态**：2026-10-26实验分支已实现，V1仍默认；尚无真实模型收益结论。
- **背景与问题**：V1的32k回退、80行和预计目标数阈值可能在长上下文服务上过早触发概述树；额外概述/归并是否改善提取质量没有当前对照证明。
- **当前方案**：ContentBlock结构单元、真实表头/脚注依赖及完整请求预算贪心打包，原文直接生成LearningProposal；无必经概述树。V2预算默认131072，可由网页/API/环境修改；输出预留默认12288，安全余量2048，采用现有保守计数。计划保存预算、模型/端点身份，实际SDK与恢复使用同一预算。主/辅范围分离，所有引用可见且每候选有主要范围锚点。
- **可考证依据**：本轮用户明确提出直接提取V2，要求保留旧算法、独立分支尝试约128k并在对照后决定默认。实现见[outline-v2-implementation.md](../outline-v2-implementation.md)；不能把这项新取舍说成项目最初的动机。
- **输出与恢复**：2026-10-28按用户要求将V2八项目标和通用四条criteria改为软阈值，模型整理等价内容、区分独立能力，不得删除必要条件；九个目标或五条标准可以保留。数量只记录与提示；原文内omission_risk、截断或请求预算不足触发有限拆分。此前2026-10-27已区分遗漏与missing_context，外部缺失仍只记录。父批superseded并保存诊断，子批完整接替，父输出不发布；主范围并集计覆盖。深度4/额度200仍是安全边界。新策略direct-structure-v3-soft-targets/Schema摘要防止旧任务静默换协议，完成候选不覆盖；下游出题同步允许超过四项，但必须覆盖全部目标条件。当前仅有受控证据，不证明语义质量改善。
- **替代及权衡**：一次全文调用简单且保留全局上下文，输入/输出/成本合适时应作为基线；V1背景树可能帮助远距离依赖，但增加前置调用并可能压缩条件；固定行数对格式敏感；仅Top-K无法承担遍历覆盖。V2减少固定前置摘要步骤，不保证总token、耗时或召回更好，拆批和更多候选还可能增加成本/归组量。
- **证据及限制**：18项V2受控检查和相关266项回归支持契约，不证明语义收益。usage缺失为null，attempts保留重试成本，不能用估计补真实消耗。超长单行、复杂表格和图片仍有显式未处理项；未实现字符偏移切句、自动跨章补读或专门全书总结。[四组实验](../outline-v2-experiment-plan.md)待用户确认，参考覆盖/证据/实际成本共同决定是否换默认。
- **源码**：[outline_v2.py](../../outline_v2.py)：`plan_direct/run_direct/validate_direct/covered_lines`；[outline_store.py](../../outline_store.py)：`split_direct_batch/reserve_direct_call`；[practice_model.py](../../practice_model.py)、[structured_output.py](../../structured_output.py)、`test_outline_v2.py`；[F11](CALL_CHAINS.md#f11)。

## 2026-10-28 补充决策：概述归并选择证据 ID

- **依据**：本轮 D04-B 失败诊断发现模型把不连续指针归并成包含不可见空隙的连续范围；用户明确建议由模型选择已有 ID、程序映射坐标。这是此次修复的动机，不作为最初开发动机。
- **实现**：`outline_context.available_evidence/evidence_id` 按来源版本、原范围及可选 asset_id 生成稳定身份；`MergeDocumentBrief` 只返回 evidence_ids，`resolve_merge` 限定本次白名单并恢复原 Citation。`citation_is_visible` 和来源/依赖版本检查保留。存储仍为 DocumentBrief，首次原文概述保持精确选行。
- **权衡**：比提示词约束或事后裁剪错误行号更明确；内容派生 ID 比顺序编号更稳定但更长，实际 Token/质量收益未验证。合法 ID 不保证语义支撑。原每点两条依据和六点上限保持，不能以修复坐标为由声称覆盖问题已解决。
- **验证边界**：本轮只做离线检查；历史实验和 Gold 保留，后续实测需新的源码运行版本。详见 [实现与测试边界](../outline-evidence-ids.md)。

## 2026-10-28补充：清单实验取消自动Judge，导出完整独立评价材料

依据是实际旧Judge频繁结构失败、引用契约错误和语义自相矛盾，用户明确要求降低评测工程复杂度。新入口复用现有生成、Pydantic、引用验证、持久化及台账；只移除该实验的自动评分调度，before_http禁止judge，不删除旧代码或结果。最新源码重新运行六份精简资料的12个128k/12k任务，旧32k/4096候选不混入新比较。原Gold和RUBRIC字节不修改。

两个匿名交付文件完整保存正式目标、原文/Gold/必要条件/引用/版本哈希、未处理范围和客观指标，PDF原始二进制内嵌JSON；方法和原ID映射单独保存在内部审计目录。评价正文不带旧分数。相较继续修复杂Judge包协议，这减少自动调用和门槛，但独立评价仍可能误判，必须逐条件给依据、保留uncertain，不以程序合法率或候选数替代语义质量。没有实现新的大评测框架或作默认切换。见[实现边界](../outline-generation-only.md)。

## 后续核验入口

修改前优先检查对应源码和测试，再检查上述设计记录的时间。当前未核验真实模型行为、本地模型/供应商能力、部署拓扑、独立 Gateway、断连后供应商停算、教育效果或真实用户分布质量。若后续取得原始选型讨论，应补入相关项的“设计依据”；若只是新的分析，保留“分析性比较”标签，不把它改写成历史事实。
