# Knowledge Review 开发约定

2026-10-18 Gateway M2.2：网关已接入的7个JSON模型入口通过@streams.bounded与SSE共用实际工作槽，不改变原业务请求/成功响应或数据库；KNOWLEDGE_REVIEW_MODEL_WORKERS范围1–32，独立Python默认4、Compose默认2。池满503及Retry-After，提交前准入，真实Future完成才释放；HTTP超时或取消等待不提前释放。JSON原有BackgroundTasks标题、读取触发标题及后台202作业未纳入，完整模型取消仍未实现；前面SSE专用槽的描述属于旧阶段。原SSE12项、新增共享槽7项、相关原有80项回归通过；真实Gateway/Redis/Agent/可控本地模型8项通过，无外部模型或质量验收。执行python -m unittest -v test_agent_sse test_agent_admission验证；详情见docs/gateway-sse.md与独立Gateway项目docs/ADMISSION_CONTROL.md及evidence/m22-*。本次只做最小HTTP适配，保留原数据和配置，无自动提交/推送。


2026-10-16：新增最小 Gateway SSE 适配，复用原 execute_turn/run_agent 和 SQLite 提交流程；GET /healthz 不调用模型，POST /api/conversations/{identity}/turns/stream 提供真实执行进度和保存后的最终结果。原 JSON API 保持。SSE 每进程最多4个实际同步工作，客户端断连不提前释放槽位；不是逐 token 或完整业务取消。12项新增、80项相关原有测试和10项可控 HTTP 链路检查通过，无外部模型调用，不代表模型质量或原项目全量回归。详细调用链、原因、运行方法和边界见 docs/gateway-sse.md。容器最终验证见独立 Gateway 仓库的 README/evidence，本项目不包含 C++ 实现；无自动提交/推送。


2026-10-15 后续用户明确要求：视觉模型可配置，未配置默认复用原聊天模型，不能因缺少 VLM_MODEL 就禁用。pdf_vision.vision_settings 按字段 VLM_* 非空优先、LLM_* 兜底；明确不支持图片时按连接/模型/凭据指纹缓存，401/429/超时/无效图片/结构化协议错误不误判。导入兜底仍由 PDF_VLM_ENABLED 控制；普通文字功能不受视觉拒绝影响。deepseek-flash 已完成真实合成图片及完整 Agent→最终视觉引用验证（4.914s/11.841s 单样例），不是复杂文档质量评测。表格支持邻近表题/单位/脚注原文依赖与 read_table 行分页/已提取覆盖标记，不把 top-k 行当整表；新可选上下文字段为空时不改变旧快照序列化身份。下段“VLM_MODEL配置后才”已被本段替代。

2026-10-15 PDF 第一版见 docs/pdf-document-design.md。Document/ContentBlock/Asset 在 document_model.py；Source.document 统一承载结构，Markdown 文本和旧 source_id 保持。pdf_parser.py 默认 PyMuPDF，Docling 为显式可选依赖/离线模型，尚无真实 Docling 模型验收；PDF 来源绑定原文件及解析快照，不在阅读时重跑。网页接入导入/页面资源/替换/重新解析，library_store 增加可空 source_json/snapshot_json 列，旧行保留。表格文本子块与真实表头分开引用，Embedding、工具与最终预算均需保持依赖，不把非连续范围伪装为连续原文。VLM_MODEL 配置后才提供 inspect_visual；PDF_VLM_ENABLED=1 才导入转写，最多3页，失败保留 partial。视觉观察独立进入最终请求及存档，asset_id 只有成功查看才可引用；纯图片导航标签不能作为原文出题。测试仅用合成 PDF、隔离目录与受控服务，不能称真实 VLM 质量通过。视觉向量检索/通用超宽与跨页表格/整表精确计算仍后续；新开发不自动提交或推送。

2026-10-15：用户明确授权本批整理并提交、推送到既有origin/learning-loop-mvp。只纳入knowledge-review的非忽略应用文件，根三份未跟踪设计保留且不纳入；密钥、个人数据、模型与运行产物不上传。本次授权不意味着以后自动提交/推送。上传边界与公开实验报告见docs/git-management.md及evals/retrieval/expanded-v1/results-20261013.md；原固定语料末尾空行不能为格式检查改写。

2026-10-13 最新用户授权：后续检索评测相关性/条件/证据与争议由Agent自行判定、留理由、冻结，无需逐项请示；不改称人工gold，不用被测排名追改test标签。无法可靠判定明确excluded，用户原有决定/草稿和旧冻结不可覆盖；自主评审不等于自动加入学习计划。B已接入网页search_notes：网页默认hybrid、可切dense并保存偏好，API/CLI/KnowledgeTools默认dense兼容旧客户端及A基线。未搜不加载E5/BM25、每路50等权RRF再返回≤10、只有许可prepared chunks统计/原文引用，C仍离线。旧mode缺字段比较时补dense、JSON类型保持、原存档不改，失败重试可换mode而不新建轮次/学习记录。成功/失败存程序检索诊断，不存隐藏思维链；Agent/最终预算范围分存。真实最终模型曾误称前面搜索不可用，已加completed_actions程序记录进完整预算，40相关测试/真实样例复核；失败报告保留。详情见docs/production-hybrid-search.md；下文“正式保持A/须用户核对”均历史阶段。

本批最终验证：189相关回归通过，最后细节修改再跑25项重叠定向测试，不相加、不称全量。真实正式B100/100前十及RRF分数与实验一致；模型失败与复测分别保留，不合并答案成功率。公开隔离网页实际保存/引用/诊断与偏好恢复通过；首次搜索约8.36秒包含加载/索引，暖B约19ms不是正式端到端。正式8767最终代码已重启、HTTP200，隔离8770已停止；旧行/资料/根三设计/冻结保持，三个SQLite完整性ok/外键0。详见development-log最新节与production-hybrid-preservation-20261013.json。

2026-10-13 用户通过异步问题明确确认两条扩样建议并授权冻结；当前新freeze6c215fa76fc64742af175be84c9f8a06，98auto+2user、0pending。两条按CAS追加user确认，原12auto等级修正/原初标/旧20freeze保留。BEIR复算已存A/B/C，报告303339665061499da918c318faf34a04，72可答test+8negative/20dev分列；不是100独立命题或全人工gold。B Recall10=0.916667/nDCG5=0.808604、C Recall10同B/nDCG5=0.800366，完整证据3 B=.833333/C=.791667；8组合题B5/8、C0/8。C救回4/8、退化2/64，纯重排P50约1112ms，暂无区间/答案评测。决定正式保持A，后续优先B可选试用，C保留离线不默认上线，不调测试标签或切模型来追分。已有原始运行quality_status仍记创建时未冻结，最新质量读新冻结对照；勿重写原Trace。docs/audit最新节记录结论与方法；下面2pending为历史快照。

扩样收尾：真实A/B/C各100/100，运行索引expanded-experiment-d1bb7e5e6bb7426fb109bf539c6b11d3、对照9ca3af80c0464880b1b7dbe74ed48791（未冻结quality=null）。最终80检索+44业务回归，7Python语法；不称全量124或真实语义通过。组合题片段只答子问题，12条初标2确定修正为1；agent-review-initial原版/最新agent-review/grade-corrections保留。`bind-section-audit --corrections`只投影显式修正，`adopt-audit --expected-agent-revisions`在短事务内CAS追加auto/originagent，用户草稿/决定/待处理及旧版本/冻结不可覆盖；默认prepare仍只初始化。当前98auto/2pending/0freeze，需用户核对后本版freeze重评已存Trace，无需重跑模型。正式接入尚待质量与耗时依据；小样本合成100不当100独立能力点。源/数据库已有行及三根设计保持，保护记录expanded-preservation-final。

2026-10-13 新100条扩样见 `evals/retrieval/expanded-v1/audit.md`：10公开合成协议/80实际片段、100查询但只有40规则/10领域问题族，dev20(2族)/test80(8族)，12多证据/8无答案(3权限负例)。不当生产实现列表或外部gold。`--suite`显式选择validate/prepare/A/B的资料与同目录初审，默认仍旧20条；完整section_judgments显式语义等级由binder绑定实际片段，原文/台账缺漏失败，禁止盲填0。98自动+2待用户是创建时快照，最新状态读数据库；旧20freeze7283e52f0de54e80897be850bd37bb01不能套新manifest，初审/用户编辑/冻结追加不覆盖。冻结compare自动存failure_inventory(真实前三原文范围/等级、缺组、缺失执行、候选缺证据、nDCG退化)，无freeze不作质量诊断，负例返回不等于生成拒答错误。正式搜索及其他业务未改，配对区间、真实公开语料/答案评测、生产升级后续。开发记录需写实际问题/方法/理由。

2026-10-13 C收尾：补质量说明/核心导读/手册最新C路线，恢复8767并页面确认freeze7283e52f0de54e80897be850bd37bb01。报告Markdown零分母显示不适用、JSON仍null；未传freeze的报告说明“未绑定”而非断言数据库未冻结。审核页nofurther分歧且已有freeze时显示版本记录提示，JS加v=20261013以解决旧资产缓存，不写标签。2项报告展示相关测试、5Python语法、JS语法/页面脚本错误0通过；94项为此前实际测试，不称本次重跑。复算报告81290c018a2c4b9da60c66fc7fdef545与旧指标一致，不重新模型推理；学习/聊天/标签/根设计保护核对通过。本次没有扩100或生产接入，保持旧冻结与所有实验。

2026-10-12 C同池重排已实现，仅独立评测。用户明确授权冻结20条，当前freeze=`7283e52f0de54e80897be850bd37bb01`、16 auto+4 user，旧标注/旧Trace保留。`rerank_model.py` 固定mmarco-mMiniLMv2-L12-H384-v1 commit1427fd652930e4ba29e8149678df786c240d8825；显式download-reranker、本地普通目录避免Windows临时symlink权限问题，常规rerank不下载，safetensors/trust_remote_code=False、CPUfloat32logits非概率。容量按配置/tokenizer/位置容量最小值；完整问题+连续原文字符窗口，禁止truncation，窗口覆盖/实际范围/分数保存。`rerank_retrieval.py` 绑定完整B快照/digest，只重排原池，失败保留不降级成功。每片段max窗口logit/同分B序，最多16窗口/1024对，超预算失败。compare支持--c-run；候选20/50以B原池BEIR计，排序指标按C，救回/退化保存分母，0分母null，失败不退出。94项相关测试、20项新C工程测试；真实试运行输入958候选均无需分窗，长输入路径为受控验证。C暖延迟纯重排，B+C之和仅估计端到端，不称实测。正式模型/问答/学习业务未改，约100扩样、配对区间和生产升级后续；旧C未实现/未冻结为历史状态。维护开发记录实际问题/方法/理由。

2026-10-12 文档Hybrid B已实现，仅独立评测，不替换KnowledgeTools/网页工具。`hybrid_retrieval.py` 正IDF BM25(k1=1.2,b=.75)，NFKC/casefold、完整代码词及组成词、Han单/双字；不是语言分词。按当前许可prepared chunks建统计/倒排，不引用库中旧/越权资料；原文不改，无词命中不补零分。等权RRF(k=60)按1起始排名，两路最多50去重后最多50，缺路贡献0，同分按manifest顺序。`quality_retrieval.py hybrid/compare` 保存各路原分/名次/贡献、实际原文和分阶段耗时；每暖请求一次E5编码，显式失败不降级成功。`retrieval_comparison.py` 不加载模型，未冻结质量null，冻结后同标签BEIR配对点差；暂无区间或C。74项相关测试通过、A/B真实各20/20成功；A与旧排名一致，B向量路与A一致。参数未按测试标签调优；正式问答仍按需原向量，其他旧日期B未实现是历史记录。见retrieval-benchmark-design/development-log最新节。

B交付实际HTTP核对：v2标签16 auto_accepted+4 approved，用户已经确认四条争议，冻结0；本轮标注/审核/冻结库哈希与启动前一致。下文4 pending为此前审核收尾的历史状态。用户冻结后可直接score/compare已有A/B Trace，无需重跑编码。

2026-10-12 检索例外审核收尾：38项相关测试通过（30检索+8网页），prepare仅匹配manifest复用完整初审，重复操作保留用户决定；混合来源导出与BEIR评分通过。独立合成页面验证采用后自动下一条、冻结/刷新恢复，正式v2仍16 auto_accepted+4 pending、冻结0。正式8767已恢复；正式学习/聊天记录与根设计文档保持。当前Windows测试沿用temporary_notes和普通mkdir，TemporaryDirectory子目录曾拒绝访问；勿据此改业务权限。详细问题/方法/理由见development-log最新节。此前335全量是历史运行，B/C尚未实现。

2026-10-11 用户最新调整：检索标注只需用户处理 Agent 不确定的样例。`evals/retrieval/agent-review.json` 为逐份阅读整个许可原文的完整初审，绑定 manifest、资料哈希及逐片段明确等级，不能仅凭 checked 标志把 null 当0。`adopt_agent_audit` 只初始化未编辑记录，16条确定项标 auto_accepted/origin=agent，4条分歧保留pending；确定错误直接修正留痕。页面默认只列pending、只展分歧原文，直接采用建议免逐片段确认/填写理由；改决定须理由。用户保存始终 origin=user，不能伪造auto状态/初审指针；旧修改/旧冻结不覆盖。冻结保存初审快照，BEIR/导出支持混合标签，报告label_provenance；不能说所有标签是人工gold。前文“全部须人工批准/20条全pending”是首次交付规则，已由用户此次授权替代。

2026-10-11 检索评测第一步见 `docs/retrieval-benchmark-design.md`：只做 A 原向量、B 向量+BM25+RRF、C 同 B 候选+Rerank 三组。当前仅 A、20 条/8 份合成公开资料、初审/审核/冻结与真实 BEIR 接线已实现，B/C 未实现。`retrieval_benchmark_data.py` 的不可变 manifest 绑定实际 prepare_chunks、E5 指纹、scope；审核 null 不是 0，批准须全范围检查，错误/争议须理由，append revision 与冻结版本不得覆盖。独立 review/vectors SQLite 不读正式用户原话、不写学习记录，模型不参与 gold 批准。`quality_retrieval.py` 调原 KnowledgeTools 并核对额外候选前十，不能为评测强制网页 RAG 或放宽生产 MAX_SEARCH_RESULTS。BEIR 只计算冻结标签；生产同分顺序用序位得分保留，原始分数在 Trace，缺失/失败不退出质量分母，无答案单列。试运行/受控测试不能当正式改善；后续保留同资料/标签/权限/设备协议并报告救回、退化分母。

2026-10-11 学习与面试入口为 `docs/project-learning-handbook.md`，第一课为 `docs/learning-01-question-flow.md`。讲解按用户操作串起入口、上下文、模型调用、校验、事务及失败恢复，配合成数据实验；功能和技术栈须核对代码。当前链路都有手册阅读路线，后续逐课细讲。更新学习材料时区分模型语义与程序保证、历史测试记录与本次运行，不打印私人原话或密钥。

2026-10-10 第二、三批已实现，最新状态见 `docs/library-and-chat-practice.md`。集合移动检查循环；删除集合原子迁移内容到父级，旧清单保留但撤销范围。导入正文替换保 document_id/名称/归属，当前正文和版本资格在 practice.sqlite3，原始文件保持；“移出资料库”撤销查阅资格，保留旧题依据，不是磁盘删除。Catalog 所有网页链路使用当前版本，生成前及保存事务复查；旧引用显示快照，不能跳新版本同一行。清单、整合、路线过期只供查看。

`chat_practice.py` 持久幂等任务/同聊天 OS 锁，共用 build_context、相关历史召回与完整双协议预算。DiscussionQuestion/DiscussionKnowledge 的文档引用为空，文档题原契约不放松；严格出题仅当前实际原文。默认讨论题参考标准属模型解释，先审核/选择才加入，复用真实作答、语义评分、追问和 FSRS。`practice_provenance.py` 存讨论依赖及原话范围；两个既有磁盘 SQLite 默认 DELETE 日志，ATTACH 短事务原子撤销/发布，不持网络写锁。删除聊天清引用/诊断，撤销在途任务及所有相关练习使用资格；保存真实答案和调度，变式/参考/加入/评分/新维度/相关能力历史不能绕过。旧文档快照仍可用于明确旧版本练习，与聊天删除规则不同。

`test_management_chat_practice.py` 20 项受控工程测试；`quality_chat_practice.py` 默认 smoke、显式 live，独立合成数据且无自动加入。315 项全量测试通过，最新扁平 Proposal 接口后 72 项相关回归通过；2026-10-11 收尾 80 个 Python 语法解析、六个 JS 语法及依赖检查通过，正式 8767 已启动。失败报告与单个真实成功样例分别保留，不代表全面教学质量。全局画像、纠正关系、后台摘要、多题批量审核、物理文件清除/恢复和版本浏览仍后续；后文相冲突“未实现”属于此前阶段。

2026-10-10 记忆扩展第一批已实现，见 `docs/conversation-memory-phase2.md`。`conversation_memory.py` 独立历史工具、关键词/E5/RRF、原话字符范围与 MemoryReference；只有实际搜索加载 embedding，零调用不补检索。`ContextBundle` 两阶段共用，最终历史可见范围单独预算裁剪。SourceOnlyAnswer 内部契约保留，历史不能提升文档覆盖/知识权威。SQLite 增 archived/memory_revision/memory_valid、依赖表、隔离历史向量和最小删除身份；删除原子清理、沿依赖撤销派生助手回复与摘要，保留其他聊天记录但禁用派生记忆。调用前及发布事务复查，兼容降级也复查，不持网络写锁。归档继续参与、无年龄截止；练习与 FSRS 不受聊天管理影响。本文后面的“跨会话/归档删除未实现”是旧阶段记录。聊天出题、资料管理和全局画像仍后续；语义阈值/全历史扫描未校准，结构合法不等于语义可靠。

2026-10-10 最新范围：前置关系与学习顺序建议已实现，详见 `docs/collection-learning-outline.md` 最新实现节。`outline_routes.py`、`outline_route_model.py`、`outline_route_store.py` 为完成并经用户核对的清单保存独立可审核路线；默认比较全部可用目标对，排除待核对/待确认重复和非代表重复项，按完整双协议输入及明确输出估计分批，默认每次 24 请求。失败/额度暂停可续跑；候选、归组决定、资料版本及 attempt 撤销迟到结果，网络不持写事务。原模型关系和用户编辑分存，修改后标 user 且当前不保留原文授权；接受检查 DAG，用户仍能修改顺序、跳过关系/目标、自由学习。后文“路线尚未实现”是此前范围记录。

`route_recommendation.py` 仅建议：读固定 target_id 的最近首题及当前 effective_evaluation_id，不将追问/同知识其他能力当作达标证据；会话变为追问不能抹去首题。无提示记录、范围匹配、可判断才作为当前本题达标/缺口；未测试未知，提示/不确定证据不足。优先未完成主要作答、真实 eligible 事件的 FSRS 到期、定性缺口、路线探索/前置核对；不计算掌握概率、不锁关、不自动加入、不因跳过改 FSRS。未审核关系仅顺序预览，推荐用已接受关系或用户顺序。最坏目标对平方级、外部缺失话题待核实、跨清单统一路线和开放式研究仍后续。`test_outline_routes.py` 为受控测试，`quality_route.py` 默认 smoke/显式 live，用固定合成候选和独立数据库，不能称作真实提取或全面教学质量。

## 先理解目标

这是用户通过实际开发学习工程与 Agent 技术的项目。用户能写 Python/C++ 小程序，但没有独立完成项目的经验。用中文沟通，解释设计取舍、数据流、为什么修改以及修改后行为；不要出基础语法小测，也不要一次堆上大量框架或功能。

产品范围以 `docs/product-design-v1.md` 为准，运行方法与验证记录见 `README.md`。DeepTutor 是参考项目，本机位置为 `D:\deeptutor`，不是本项目；不要未经请求修改参考项目。

会话记忆设计见 `docs/conversation-memory-design.md`，实际第一阶段见第 0 节及 `docs/conversation-memory-phase1.md`：已实现持久聊天、两阶段共享背景、预算触发摘要、当前会话早期原话补回、失败重试和并发保护。跨会话召回、历史引用、归档/删除失效链路、聊天出题尚未实现。用户确认默认考虑完整当前聊天，后续默认召回全部可用本地历史，以相关性为主、时间为辅；归档只收起列表并继续参与记忆，删除撤销原话及派生记忆的引用资格。临时题目需选择才加入计划。继续开发时以真实代码为准。

持续学习设计演进见 `docs/mastery-path-design.md` 和 `docs/knowledge-question-model-design.md`，实际第一版与数据库说明见 `docs/learning-loop-implementation.md`。现有练习主动加入计划、按目标生成变式/重做原题、FSRS 日期、评分与档位修订已实现。路线仅建议，不锁关、不强制追问；跨会话记忆和自动整库学习仍为后续工作。

用户确认解释、应用等是同一知识点的掌握维度，分别安排复习且界面按知识点归组；模型负责结合目标、资料和真实回答进行有依据的语义评分，程序验证依据、身份、结构与状态。rubric 是参考要求，不以固定关键词、评分点计数或任意加减分公式替代模型判断；现已删除覆盖组合推导等级及强制追问规则。未测试不等于不会，不生成任意 mastery 百分比。

第一版面向本地单用户、技术 Markdown 笔记，已有阅读问答—练习—保存证据—主动加入—计划复习。以后扩展整库目标与路线、聊天记忆、PDF/EPUB 和 Obsidian。不要把路线图当成已实现功能。

## 协作方式

2026-10-08 已建立质量评测第一步：`quality_eval.py` 默认离线校验，smoke 验证真实业务服务接线，显式 live 才请求当前模型。`evals/` 仅用合成资料，参考标注是待人工确认的草案；运行使用 `.data/evaluations/` 独立数据库，不导入网页全局应用、不自动加入计划。提取、固定候选归组与固定答案评分分别记录；部分运行、未审核及 smoke 不显示语义质量比率。操作见 `docs/quality-evaluation.md`，五条核心链路见 `docs/core-code-walkthrough.md`。继续开发需同步 `docs/development-log.md`，记录实际问题、方法、选择理由及验证，不能倒推虚构此前经历或把程序校验称为语义正确。

2026-10-04 新增实际能力：聊天首次成功回复后后台自动命名，手动标题优先；练习默认整篇文档、选段可选，网页练习不再受 read_source 的 200 行边界限制；知识库为含子集合的 collection。根 `root` 为总览，旧文件和旧复习项默认归根。详细实现与边界见 `docs/collections-and-practice.md`。

集合及文件关联保存在 practice.sqlite3，文件使用 document_id 管理归属，不用版本性的 source_id。一个文件/复习项当前只有一个直接集合，父集合显示后代。复习项归属与文件归属独立，移动不重建题目、不改变 FSRS。题目仍主动选择才加入，可选择根或任意集合。集合出题是按该范围生成临时题；没有集合删除/重排/迁移接口。

集合学习清单已实现，见 `docs/collection-learning-outline.md`。根据集合及后代完整快照建立不重叠章节清单，按完整生成预算分窗口顺序分析；保存实际行范围、失败和候选，失败可继续未完成段。标题无正文可程序标记，无模型知识推断。审核保留固定原文证据与 revision，勾选后按固定目标生成首题，题目/目标/计划/候选标记原子提交。范围 document_id/source_id 指纹改变时旧清单只供查看，未加入候选需重新生成。OS 锁与 attempt 防重复和迟到发布；网络期间不占写锁。覆盖记录是分析进度与目标收录，不是全库语义完整性或掌握概率。跨章归组已补阶段 B1，前置关系与完整学习路线仍为后续，不提供清单删除入口。

无选段练习使用结构分块、文件交替与章节分散、可选目标字面匹配；按完整请求 Schema 预算取原文并预留评分空间，保存真实可见范围。不要称为语义召回或全库覆盖。显式选段必须保留且校验版本，必要内容超预算时报错。聊天按需工具行为不变，选择集合不会自动扩大聊天资料权限。

2026-10-04 用户确认的完整规划见 `docs/collection-learning-outline.md` 的“大文件分层整理后续设计”。2026-10-05 阶段 A 已实现，新清单通过 `outline_context.py` 按输入/预期输出预算选择全文或分层；Markdown 结构窗口 → 带原文指针的概述 → 分层章节/文档归并 → 共享背景提取，按证据顺序补入真实原文。背景使窗口缩小时原子保存完整尾段；公开诊断与实际可见范围持久保存，审核/加入按此校验。模式是应用启发式，概述是有损结果；旧清单仍走旧流程。概述步骤使用 practice.sqlite3、OS 锁、attempt 及依赖结果哈希，失败复用有效结果，不持网络期间写锁。阶段 A 当时未包含候选整合；阶段 B1 见下一段，自主复查、前置关系与学习顺序仍未实现。默认使用现有技术栈，框架与多 Agent 按实际需要评估；分别报告受控测试与真实模型。当前 8765 被 Anki 占用，开发服务用 8767，不终止占用程序。

2026-10-07 阶段 B1 已实现：`outline_grouping.py`/`outline_group_store.py` 从候选及实际证据产生可审核的同知识/重复目标/待核对建议；保存原模型建议、用户编辑、成员版本和完整预算。接受不创建学习记录，选择加入时在原事务内延迟绑定统一 unit；不同目标各自 FSRS，重复只允许代表加入，原候选保留。候选修改使建议失效，首题发布复核归组 revision；撤销不重写已有项。

2026-10-09 补齐大候选集自动分层整合：`outline_group_plan.py` 按完整输入、明确标注的输出估计及 Schema 容量选择直接/分层；稳定章节树分配全部候选对，包括未归组及远处节点。上层卡片保留完整目标、条件与标准，只省原文；关联必须补回固定证据核验，卡片不授予引用权限。`ComparisonPage` 的已处理/待处理身份由代码验证，槽位满或明确输出截断缩小工作继续；不将没有返回建议当成保证没有关系。`outline_group_steps.py` 保存逐请求 attempt、回执、预算和可见范围；默认每次 24 请求，额度用完暂停，用户继续增加额度，完成步骤复用。`outline_integration.py` 仅以组内每对同类型原文依据组装大组，不用连通关系直接合并；不覆盖原候选。完成全部计划工作才发布最终审核建议，暂停使迟到结果失效，模型调用不占写事务。旧 plan=NULL 任务保持旧协议。最坏候选对工作量平方级，回执完成不保证语义完整；自主原文调查、前置关系、学习顺序与跨清单去重仍未实现。七组合成评测中新增大候选集，smoke 默认上限 128、live 仍为 8，真实调用必须显式 live。

- 用户要求继续开发时，完成一个可运行、可解释、可验证的步骤，结合当前进度选择下一步。
- 给出代码或交付改动时，解释为什么这么做、改变了什么、如何运行、验证了什么。
- 修改前检查实际文件与现有测试；文档可能落后于代码。保留用户的笔记、配置和其他改动。
- 优先小模块和明确的数据对象。当前无需引入 Web 框架、多 Agent 框架或向量数据库来完成简单改动。
- 不要因为切换开发 Agent 就重写项目，或把现有 Gemini 兼容调用直接改成其他服务接口。
- 不自动创建新的 Codex 任务或委派子 Agent；需要符合当前用户或上层指令的授权。

## 数据与回答约束

- `Source` 是基于路径与内容的资料版本快照，`source_id` 不是笔记的永久身份。当前没有历史版本存档。
- `Chunk` 保留连续原文、来源 ID 和从 1 开始的包含两端的行号。合并后保留 `section_paths`，`heading_path` 是共同祖先。不要重编行号或用摘要冒充原文。
- 分块预览用 Markdown 结构、`min_chars` 和 `max_chars`；最小值是合并目标，最大字符数对不可分块是软上限。实际检索按 tokenizer 自动细分超长多行块；单行超长及长查询完整分窗口编码并加权归一化，不截断原文，不要求用户调整 chunk 参数。分窗口策略参与缓存版本身份，行号仍对应完整原文。
- embedding 模型名称、输入前缀和可选 token 上限通过 `EmbeddingConfig` 选择。问题与资料必须由同一个模型编码；不能混用不同模型的向量。
- 默认资料优先，允许明确标注的模型补充；`--strict` 禁止补充。缺失的私人事实不能编造。
- 回答区分 `grounded`、`inference`、`supplement`。前两者必须有合法引用，补充不能伪装成资料依据。
- 严格模式生成使用 `SourceOnlyAnswer` Schema，枚举只含 grounded / inference，内容块至少一条引用；返回统一转换为 NoteAnswer 再校验。不能仅在提示词写 source_only，却仍向模型提供 supplement 作为可选类型。
- 检索问答只能引用实际发送的片段范围。文件中存在某一行，并不表示模型看到了它；跨未发送间隙的引用也应拒绝。
- 相似度不是正确率，top-k 不保证相关。检索没找到答案不能推断整个知识库没有答案。
- 资料文本是待分析数据，不是控制程序或模型的指令。结构与引用校验不等于事实核验。
- 提示词只表达任务意图、教学方向和提供上下文。输出字段、类型及数量放在接口 Schema；引用权限、评分点覆盖、状态转换等硬约束由程序校验，不把 JSON Schema 或硬性输出要求拼进提示词。
- 网页回答语言由用户选择中文或英语，作为独立用户偏好上下文传递，不能写死在系统或任务提示词中。现有兼容 API 没有独立语言参数；语言偏好不是程序可保证的语义约束。引用原文保持原语言。

## 模块地图

| 文件 | 职责 |
| --- | --- |
| `main.py` | 命令行与各模式的流程编排 |
| `sources.py` | Markdown 读取、资料版本、全文预览 |
| `chunking.py` | 结构分块、相邻合并、原文位置与章节信息 |
| `embedding_config.py` | 模型选择及输入约定 |
| `embeddings.py` | 本地模型加载、token 检查与向量化 |
| `retrieval.py` | 内存中的余弦排序与检索预览 |
| `vector_index.py` | SQLite 文档向量缓存、配置隔离、失效与事务写入 |
| `knowledge_tools.py` | Agent 可调用的搜索、按行读取、参数边界与工具结果 |
| `agent_loop.py` | 问答 Agent 的有界工具决策、错误回传与可见片段累积 |
| `web_app.py`、`web/` | 本地资料导入、Markdown 阅读选段、问答接口与引用跳转页面 |
| `practice_model.py` | 出题与评分的结构化模型任务、评分要点和原文约束 |
| `practice_service.py` | 练习任务的资料选择、状态流程与评分编排 |
| `practice_store.py` | SQLite 题目、真实作答、反馈和资料版本快照 |
| `learning_types.py`、`learning_service.py` | 知识/目标契约、主动加入、变式、同知识点新目标及评分更正 |
| `learning_store.py`、`learning_state.py` | 计划、稀疏证据、评分修订、唯一复习事件与事务重算 |
| `review_scheduler.py` | FSRS 6.3.2 适配、参数保存和无随机扰动的事件回放 |
| `context.py` | 检索片段校验、模型上下文与可引用范围 |
| `model_client.py` | 对话模型配置、请求、结构化解析 |
| `response_preferences.py` | 中文/英语回答偏好的校验和独立上下文 |
| `conversation_store.py` | SQLite 聊天、不可变消息、幂等轮次、会话锁、摘要 CAS 发布 |
| `conversation_title.py` | 独立后台标题生成、有限摘录、任务发布与手动标题优先 |
| `library_store.py`、`web/static/library.js` | 知识库与子集合、文件归属、计划集合筛选及原数据迁移 |
| `outline_service.py`、`outline_store.py`、`web/static/outlines.js` | 持久可恢复的章节分析、审核清单、完整请求预算及用户选择后的首题/计划原子保存 |
| `outline_grouping.py`、`outline_group_store.py` | 候选证据比较、持久建议/审核、预算和版本检查、统一知识点延迟绑定及重复代表限制 |
| `outline_group_plan.py`、`outline_group_steps.py`、`outline_integration.py` | 直接/分层路由、完整比较回执、有界步骤和额度、跨节点卡片筛选、原文核验及大组证据组装 |
| `outline_context.py` | 新清单的全文/分层预算路由、结构窗口、持久概述树、共享背景及实际原文补回 |
| `conversation_context.py` | 共用 ContextBundle、预算压力摘要、完整近期轮次与字面匹配早期原话 |
| `generation_budget.py` | 全生成请求的保守预算、输出预留与完整原文行裁剪；不使用 E5 tokenizer |
| `structured_output.py` | 原生结构化输出与工具结果提交的接口兼容、解析和进程内能力缓存 |
| `answers.py` | 回答契约、依据与引用验证、显示 |

当前使用 Python/FastAPI 网页、本地 SentenceTransformer embedding、SQLite 保存文档向量及学习记录、OpenAI SDK 的 Chat Completions 兼容接口连接对话模型。已有问答工具循环，以及可恢复的出题—作答—语义评分—主动加入计划—FSRS 复习闭环。

网页由 `web_app.py` 提供，只监听 `127.0.0.1:8765`。导入文件保存在 `.data/notes/`，资料范围由页面上的文档 ID 解析，不能接受浏览器提交任意路径。问答默认基于当前整篇文档，用户勾选其他文档可扩大范围；选段可选且可取消，网页不会自动加入所有资料或曾打开过的资料。当前文档与可选选段都需验证 `source_id`；阅读视图按 Markdown 块定位原文行，原文视图逐行定位。网页通过 `document_focus()` 处理可选选段，通过 `run_agent()` 和 tool_choice=auto 让模型根据问题及可见证据选择搜索、读取或结束；用户明确要求 RAG 按需调用，已删除每请求首次搜索分支，不得重新作为 middleware 加入。目录仅是导航元数据；已有焦点足够时可直接回答，一般对话可以不查阅资料。只有实际搜索才加载 embedding 或准备向量，零工具调用后也不自动补检索。页面状态只报告真实调用。网页选段不限于 read_source 的 200 行工具边界，网页练习也使用可选 document_focus，CLI 保留原有 focus_on_lines。引用仍只能指向实际发送原文，严格模式缺证据可返回资料不足。技术说明见 `docs/document-rag-and-token-budget.md`。网页已通过独立 conversations.sqlite3 保存完整问答及摘要；恢复和预算边界见 docs/conversation-memory-phase1.md。

练习和计划保存在 `.data/practice.sqlite3`。题目先保存；提交时原答案和支持情况先保存，再评分。失败保留 `grading` 可重试，不覆盖答案或重复累计。追问仅为候选，用户点击才创建待答题，最多三题；追问使用简单 ExerciseDraft，不要求新长期目标。参考引用必须在实际可见资料中，评分逐项分类并引用真实答案子串；整体等级由模型语义判断。旧资料快照保留，不能用新文件同一行冒充旧依据。

ReviewItem 由用户选择创建。知识点下各目标独立调度，变式共用固定目标、范围和标准映射；同知识点其他维度的作答可作语义参考，但不会批量累计复习。一次主要作答最多产生一个 ReviewEvent；不确定、超范围、有提示、教学追问只留证据。建议档位默认采用，用户可改。内容更正创建 EvaluationRevision，以原时间和原参数重放有效事件；FSRS 6.3.2 关闭 fuzz，不能将版本更新直接混入旧状态。目标变式共用 FSRS 是适配假设，未做学习效果校准。

回答语言选项共用于问答、出题、反馈和追问。浏览器记住最近选择；练习保存最近一次生成时的语言，提交/重试可以更换，旧内容不会自动翻译。旧数据库增加 `response_language` 字段并默认中文，不删除已有题目和作答。评分、讲解与追问由同一个模型结果返回。

结构化任务优先使用 `response_format`；服务明确不支持时，以函数 Schema 提交 `submit_result`，不执行工具或为收取结果再开模型轮次。思考模式若明确不支持指定 `tool_choice`，只降级为 `auto`，仍要求收到一个可校验的结果工具调用，否则报错。成功后按服务地址与模型缓存能力；首次能力探测可能有被拒绝的请求，不能宣称每个任务从第一次起总是只有一次 HTTP 请求。兼容模式不等于服务端严格保证 Schema，最终仍由 Pydantic 和业务校验验证。

`--question` 保留全文问答；`--question --rag` 为固定的一次本地检索与云端生成；`--question --agent` 让模型在预算内选择 `search_notes`、`read_source` 或结束，二者互斥。`--focus NOTE START END` 只与 `--question --agent` 同用：焦点文件自动加入本次资料，原文选段及少量邻近行在模型首次决策前可见，并进入最终引用范围；选中行与上下文范围分别标明。`--search` 只预览本地检索。各搜索路径均通过 `KnowledgeTools.search_notes()`，自动复用 `.data/embeddings.sqlite3` 的文档向量，支持 `--rebuild-index` 和 `--no-cache`。Agent 最终回答仍走 `answer_question()` 的结构化解析及引用校验，只能引用焦点上下文与成功工具返回的片段；调用失败计入预算且错误回传模型。命令行问答循环没有持久化记录；练习状态由网页的独立学习业务服务维护，不要称为完整学习 Agent。

缓存不能只用模型名称或维度作为身份：需包含已加载权重与 tokenizer 指纹、运行库版本、前缀、有效 token 上限、分块配置、来源版本和实际片段输入。当前指纹每个模型实例计算一次；实例加载后按只读推理使用。不同配置的向量隔离保存。只搜索本次提供的资料，不能把数据库里遗留的旧片段重新加入候选。旧向量暂不清理，但不等于原文历史存档。数据库写入使用事务，SQLite 连接必须关闭（事务上下文不等于关闭连接）。

知识工具在构造时绑定本次不可变 `Source` 快照。`search_notes` 最多返回 10 项；`read_source` 只接受精确完整的当前 `source_id` 和包含两端的合法行号，每次最多 200 行，不能接受路径或 ID 前缀。工具 `execute()` 拒绝未知及额外参数，并返回两部分：可序列化的 `output` 给模型，`visible_chunks` 给运行层累积引用权限。一个 `KnowledgeTools` 实例第一次搜索准备文档向量，后续改写查询只计算问题向量。

## 环境与验证

会话第一阶段新增 `.data/conversations.sqlite3`，原话不被摘要替换，不保存隐藏思维链。浏览器使用持久会话接口，`/api/ask` 和 CLI 保留一次问答。`request_id` 与轮次角色唯一约束防重复；同聊天通过 OS 文件锁串行生成，锁文件在数据库旁，不长期持有 SQLite 写锁。失败轮次须重试或跳过再提交新问题，跳过保留原话，不加入完成历史。模型调用后通过 attempt 核对发布，摘要还核对版本及前缀指纹。

两个模型阶段传递同一 `ContextBundle`；当前语言、资料快照和可选焦点由程序维护，历史不得扩大资料权限。只在历史预算压力下调用摘要，至少保留最近两个完整已完成轮次；早期原话按英文/数字词和中文双字匹配补回。不要把字面召回称为语义检索，也不要把历史助手解释当作文档依据。现不提供归档、删除或原话编辑入口。

生成预算通过 `LLM_CONTEXT_TOKENS`、`LLM_OUTPUT_TOKENS`、`LLM_SAFETY_TOKENS` 配置；未设置时采用 32768/4096/2048 的应用保守上限，不能声称这是当前模型的真实容量。计数为 UTF-8 字节及协议开销的保守估计，所有实际请求包括工具、结果、SDK 展开的 Schema 都检查，输出用 max_tokens 限制。显式焦点不静默裁剪，补充资料只按完整原文行缩小且同步引用权限。结构化兼容探测与回退也检查预算。

在项目目录使用 PowerShell 和现有虚拟环境：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe main.py --help
```

基础依赖为 `requirements.txt`，本地 embedding 额外依赖为 `requirements-local.txt`。模型缓存为 `.models/`，普通搜索只读取本地模型；首次下载使用 `--download-model`，不得把普通预览变成隐式下载。

测试使用标准库 unittest。API 测试通过 HTTP 模拟，不消耗云端额度；模型配置与排序使用可控数据测试。修改引用边界、来源、分块、模型参数或流程时添加针对性测试，不只验证实现细节。真实检索样例与模拟测试要分别报告，不能把模拟调用称为真实模型验证。

Windows 沙箱中 `tempfile` 创建的目录曾出现权限问题。测试文件使用 `test_support.temporary_notes()` 的继承权限目录；不要为绕过环境问题修改业务语义。依赖安装遇到权限错误时，根据工具权限流程处理。

## 配置与交接

密钥只在本地 `.env` 或环境变量中，示例配置只能放占位符。不要读取并展示 `.env` 内容，不要在输出、源码或文档中写密钥。离线测试不需要密钥。真实云端调用应符合当前任务授权，并明确区分本地检索与云端生成。

功能完成后同步 README 中的命令、当前能力和实际验证结果。有架构或范围变化时更新产品文档和本文件。交付时指出仍未实现的必要部分，避免后续 Agent 把计划当成代码。

### 2026-10-09 新增：有限定向原文复查

以本段及 collection-learning-outline.md 最新实现章节为当前状态；前文同日分层整合的“自主原文调查尚未实现”是当时记录。新模块 outline_review.py / outline_review_model.py / outline_review_store.py 和 web/static/outline-review.js 为 needs_review 保存独立任务、最多两轮每轮两项读取/字面定位、实际原文权限与完整双协议预算。新归组后台自动考虑，旧建议有手动入口；共用父整合逻辑调用额度，失败不无限自重试，完成补读可恢复。两轮后 Schema 仅允许结论；导航不授予引用权限，按完整 Markdown 块/200 行/预算限制实际补读。

原候选/原建议不覆盖，新结论另存；补读动作的说明及暂时未解决状态不是最终决定。未解决允许带非绑定修订建议，但不能直接采用；正向结论不得夹带必须修正候选的前提。明确采用后新增接受分组或保留独立，仍不创建学习记录，加入沿用旧原子事务和独立 FSRS。候选/建议 revision、资料范围、attempt 与父 OS 锁维护并发；网络不持写事务，暂停/修改立即撤销迟到发布。没有删除入口和自动换引用入口；需扩大候选证据时重新生成。

专项测试 test_outline_review.py；quality_source_review.py 默认 smoke、显式 live，合成固定候选与原提取评测分开。保留失败报告；输入保守估计合规不保证服务实际 token 或输出完整。真实样例有输出截断和与参考草案不同的保守修订结论，不能当作成功率或全面语义正确性。继续维护开发记录中的实际问题/方法/理由，不引入额外框架或改变模型服务。


2026-10-19 M2.3.2：网关已接入的JSON/SSE支持请求范围内的模型HTTP取消。复用原同步业务函数，agent_cancel.py以协作式标志和异步httpx传输关闭模型I/O；execute_turn沿用failed保存取消说明，完成后再释放实际工作槽。已提交结果不回滚；CPU/文件/SQLite/DNS阻塞不强杀，供应商远端计算是否停止未经验证。范围内SDK自动重试关闭；范围外CLI和后台任务仍用原传输。后台202/独立标题治理、Python实际任务指标、完整真实embedding/RAG部署仍未完成。调用链、取消边界和复现见docs/gateway-sse.md及独立Gateway仓库docs/CANCELLATION.md；测试结果以Gateway对应日期evidence为准，无自动提交/推送。


2026-10-20 M2.3.3：8个清单/归组/来源核查/路线的后台提交与继续入口，和自动标题，接入原共享模型工作池。先预留槽再创建记录，202返回不释放实际名额；提交者断连/Gateway重启不取消已接受工作。原pause入口先使attempt失效再请求取消模型HTTP，真实Future结束（含连接清理）才释放。嵌套自动核查复用父工作槽与Token；已完成结果保留，原暂停/继续/幂等契约及SQLite结构不变。

标题最多1个；workers>1时至少保留一个未占槽，单worker只在空闲启动且不抢占。忙时跳过无等待队列，失败用已有title_started_at冷却30秒。增加内部GET /internal/metrics，只在进程内聚合request/background/title；Gateway通过受Key保护的GET /agent-metrics固定代理读取。/metrics仍是Gateway本地请求观察，两者不能混同。Agent端口不要直接对不可信网络开放。

相关回归255项（含新增治理16项）、普通/ASan同批103项、真实受控链路与容器各同12组治理检查通过；工具/源码同步最终状态以独立Gateway的evidence/m233-verification-20261020.json为准。原Python不是全量discover，本轮没有外部模型/回答质量/embedding-RAG验收。进程重启计数重置，中断工作沿用原恢复后显式继续，不自动重放。CPU/文件/SQLite/DNS等阻塞不强杀，供应商远端停算未验证，分布式并发/V3仍未实现。详细改动理由、字段、接口和命令见Gateway仓库docs/JOB_GOVERNANCE.md；继续维护其docs/DEVELOPMENT_LOG.md。未自动提交或推送。
