# Knowledge Review 技术决策审计

2026-10-24后续：默认应用导入副作用及网页PDF VLM准入已修复，参见[修复与验证](../audit-fixes-20261024.md)。本文代码行号/快照继续对应10月23日基线；预算建议新增[成熟项目调研](../context-budget-research.md)，优先复用模型适配与tokenizer，不以新增语义评测集作为计数替换的前置条件。

审计日期：2026-10-23（Asia/Shanghai）。对象是**当前工作区**，不是仅审计 Git HEAD。HEAD 为 `61be7fbc1aff46e621f1318409be9e1dd5bdfa31`；审计开始已有业务源码修改及未跟踪的流式输出文件。基线文件哈希、符号位置及初始状态见 [source-snapshot.json](evidence/source-snapshot.json)。本次只新增审计与学习材料，没有修改业务实现。

## 1. 如何使用本审计

阅读顺序：本文件 → [源码学习地图](SOURCE_CODE_STUDY_MAP.md) → [首批答案 Q35–Q46](INTERVIEW_BATCH_01_RETRIEVAL.md)。题单索引和后续专题见 [学习入口](README.md)。不要把本文的“今天如何判断”背成自己当初的选型经历。

证据标签贯穿全文：

| 标签 | 可以说什么 | 不能据此推导什么 |
| --- | --- | --- |
| **[实现] 已实现且有代码证据** | 当前函数执行了什么、拒绝什么 | 模型质量已证明、部署已验收 |
| **[测试] 已测试但未验证真实效果** | 受控输入下的契约与故障路径；本轮执行结果另列 | 真实 E5、VLM、生成模型在真实资料上有效 |
| **[实验] 已有真实实验结果** | 指定日期、语料、模型、冻结标签上的记录 | 本轮重跑、统计显著、生产用户收益 |
| **[分析] 根据工程原理做出的分析** | 可解释的机制、风险、适用条件 | 开发者当时就是这样想的 |
| **[建议] 计划或建议实现** | 下一步可检验的改进 | 当前已实现 |
| **[未知] 无法确认** | 未找到充分证据、未检查范围 | 用合理推测补齐事实 |

**历史证据分两级。** Git 能确认某机制不晚于某提交存在；设计记录能确认文档写过什么理由。它们都不能确认代码由谁独立思考完成。初始提交 `df36eaa` 已包含 Agent 和 embedding；`ae20da7` 为学习闭环，`d86372e` 批量收录记忆/混合检索，`61be7fb` 包含网关适配。提交粒度较粗，不能把提交日期当作每项设计的首次提出日期，也不能把提交作者身份等同于个人原创证明。

审计没有提取或展示私人笔记、聊天原话或密钥内容；没有重跑云端生成、真实模型 A/B/C 或独立 C++ Gateway。注意既有测试导入web_app会触发默认应用/数据库结构初始化，因此不能声称默认数据库文件完全未被触及；隔离边界见[验证记录](VALIDATION.md)。题单中的网站面经说明作为题目背景，未独立核验，不视作招聘方承诺。

## 2. 先纠正文档时效与面试风险

| 旧资料或容易误读的说法 | 当前核验结果 |
| --- | --- |
| 三份 `docs/project-context` 文档基于 10-21 干净工作区 | 本轮是 HEAD 加已有未提交改动，不能沿用它们的源码行号/版本声明 |
| `ARCHITECTURE.md`“未实现逐 token”、前端普通 POST JSON；`CALL_CHAINS.md` F04/F16、ADR-014 类似 | **已过期**。网页新提问/失败重试走 POST SSE；`run_agent → answer_question → request_streamed → collect_answer` 增量解析正文。SDK 数据块不保证一个块等于一个 token。见 [answer_stream.py:L116 — collect_answer](<E:/agentic project/knowledge-review/answer_stream.py:116>)、[web_app.py:L1080 — stream_turn](<E:/agentic project/knowledge-review/web_app.py:1080>)、[web_app.py:L1100 — stream_retry_turn](<E:/agentic project/knowledge-review/web_app.py:1100>)、[web/static/app.js:L1 — 证据](<E:/agentic project/knowledge-review/web/static/app.js:1>)、[web/static/chat-stream.js:L1 — 证据](<E:/agentic project/knowledge-review/web/static/chat-stream.js:1>) |
| 题单 J 节“不是逐 Token SSE 流式输出” | 应改为“已有回答正文增量预览，完整结构/引用/保存后才是正式答案”。Q112–Q114 仍有效 |
| `expanded-v1/audit.md` 末段“正式保留 A，B 未默认上线” | 是接入前历史；当前浏览器默认 B，API/CLI 默认 A；[knowledge_tools.py:L92 — KnowledgeTools.__init__](<E:/agentic project/knowledge-review/knowledge_tools.py:92>) 与 [docs/production-hybrid-search.md:L1 — 证据](<E:/agentic project/knowledge-review/docs/production-hybrid-search.md:1>) 优先 |
| 早期 AGENTS“无归档/删除、只做一期记忆” | 已过期；[conversation_store.py:L137 — ConversationStore.archive](<E:/agentic project/knowledge-review/conversation_store.py:137>) / [conversation_store.py:L154 — ConversationStore.delete](<E:/agentic project/knowledge-review/conversation_store.py:154>) 与跨聊天工具已存在 |
| SQLite 向量数据库、三路生产召回、MCP Agent | 实际是 SQLite 向量缓存＋内存精确排序；文档生产是 dense 或 dense+BM25，两路；C 是离线重排。工具是内部 Function Calling，没有因命名为 tool 就成为 MCP |
| 有合法引用就没有幻觉；取消就不再收费；一个请求一次模型调用 | 均不成立：范围合法不等于语义蕴含；远端停算未知；Agent、最终生成、摘要及协议探测都可能增加 HTTP 调用 |

下列 16 项是本轮主审计范围。学习调度、清单归组与路线作为架构旁支在学习地图说明，不宣称完成整个应用的安全审计或所有业务分支验收。

## D01. Dense + BM25 + 等权 RRF

1. **当前实现 [实现]**：[knowledge_tools.py:L150 — KnowledgeTools._search_notes](<E:/agentic project/knowledge-review/knowledge_tools.py:150>) 先编码查询，首次搜索才准备向量；dense 模式取请求 top_k，hybrid 模式取 dense≤50、BM25≤50，融合≤50，再返回 top_k（默认3、最大10）。[hybrid_retrieval.py:L42 — HybridConfig](<E:/agentic project/knowledge-review/hybrid_retrieval.py:42>) 固定默认 `k1=1.2,b=.75,rrf_k=60`；[hybrid_retrieval.py:L72 — BM25Index](<E:/agentic project/knowledge-review/hybrid_retrieval.py:72>) 只在当前许可 prepared chunks 建倒排；[hybrid_retrieval.py:L110 — reciprocal_rank_fusion](<E:/agentic project/knowledge-review/hybrid_retrieval.py:110>) 按 chunk_id 合并贡献、同分按输入顺序。[retrieval.py:L25 — rank_chunks](<E:/agentic project/knowledge-review/retrieval.py:25>) 是全扫描余弦与完整排序，不是 ANN。
2. **解决的问题 [分析]**：语义近似不一定保留代码标识符、数字条件和稀有术语；词项匹配又漏同义改述。融合希望利用互补信号，并避免直接相加不同尺度的原分。
3. **历史依据**：[docs/production-hybrid-search.md:L1 — 证据](<E:/agentic project/knowledge-review/docs/production-hybrid-search.md:1>) 明确记录 B 接入的固定集质量/成本理由；[docs/retrieval-benchmark-design.md:L1 — 证据](<E:/agentic project/knowledge-review/docs/retrieval-benchmark-design.md:1>) 记录 A/B/C 对照。[未知] 未找到 `50/10/60/1.2/.75` 在本项目网格搜索选优的证据；不能说这些是实测最优参数。
4. **原理 [分析]**：`BM25(q,d)=Σ IDF(t)·f(t,d)(k1+1)/(f(t,d)+k1(1-b+b|d|/avgdl))`，求和遍历去重查询词；`IDF=ln(1+(N-df+.5)/(df+.5))`。本项目长度是词法特征数，不是原文字数。`RRF(d)=Σ路 1/(60+rank路(d))`，排名从1开始，缺路贡献0。TF 饱和、IDF 抑制常见词、长度归一化抑制长块偏置；RRF 只消费名次。
5. **合理替代 [分析]**：纯 dense（现有 A）；分数归一化后加权或学习排序（未实现）。纯 BM25 也可作为无本地模型的独立基线，但当前生产没有自动此降级。
6. **Trade-off [分析]**：

   | 方案 | 质量 | 性能/成本 | 复杂度/维护 |
   | --- | --- | --- | --- |
   | 当前 B | 固定集改善，可能压低单路独有证据 | 加词项索引与排序；无额外云调用 | 算法透明，scope 变化需重建统计 |
   | A | 同义查询可用，精确词可能漏 | 少一条词法路；仍付 embedding 成本 | 最简单、已有兼容路径 |
   | 归一化加权/学习排序 | 能利用分差/学习任务偏好，需新证据 | 推理可很轻，标注调参与训练有成本 | 分布漂移、校准与版本维护更多 |

7. **证据及局限**：[test_hybrid_retrieval.py:L40 — test_bm25_known_term_frequency_math_and_no_zero_fill](<E:/agentic project/knowledge-review/test_hybrid_retrieval.py:40>) 手算验证；[test_production_retrieval.py:L69 — test_production_top10_matches_independent_frozen_protocol_b](<E:/agentic project/knowledge-review/test_production_retrieval.py:69>) 用替身向量查接线。[实验] 固定72可答测试题 Recall@10 A=.87500、B=.91667；不是最终答案准确率。数据来源和局限见 §3。
8. **今天评审 [分析]**：保留 A/B 开关及 scope 内统计是合理的小规模方案；不能据合成集把 B 宣传成通用最优。上线判断需要真实查询分布与端到端成本。
9. **未验证假设/小实验 [建议]**：在新公开中文笔记集按问题族划分 dev/test，加入标识符、否定条件、跨段题；只在 dev 比较候选20/50/100及不同 k，冻结后报告完整证据、误召回、首次与暖耗时。
10. **深入追问**：为何同一个词在不同 scope 分数不同？为什么两路各50不是最终100？RRF 是否会让两路一般结果超过单路第一名？详见首批 Q35–Q46。

## D02. 同 B 候选池的离线 Cross-Encoder 重排

1. **当前实现 [实现]**：[rerank_model.py:L21 — RerankConfig](<E:/agentic project/knowledge-review/rerank_model.py:21>) 使用 `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`，固定 revision `1427fd652930e4ba29e8149678df786c240d8825`；本地 CPU、logit 排序。[rerank_retrieval.py:L9 — validate_hybrid_pool](<E:/agentic project/knowledge-review/rerank_retrieval.py:9>) 绑定 B 运行/manifest；[rerank_retrieval.py:L42 — document_windows](<E:/agentic project/knowledge-review/rerank_retrieval.py:42>) 保留完整 query 和连续原文窗；[rerank_retrieval.py:L77 — rerank_pool](<E:/agentic project/knowledge-review/rerank_retrieval.py:77>) 每块取最大窗口分，同分保持 B 顺序；默认≤16窗/块、≤1024对/查询，超限失败。生产工具拒绝 rerank。
2. **解决的问题 [分析]**：召回把可能相关材料找齐后，需要更细的 query-document 交互来排序；重排不能补回池外证据。
3. **历史依据**：[docs/retrieval-benchmark-design.md:L1 — 证据](<E:/agentic project/knowledge-review/docs/retrieval-benchmark-design.md:1>)、[evals/retrieval/expanded-v1/audit.md:L1 — 证据](<E:/agentic project/knowledge-review/evals/retrieval/expanded-v1/audit.md:1>) 记录同池和保留离线的实验约束；Git `d86372e` 可确认实现已收录。[未知] 为什么最初选该模型而非其他 reranker，没有系统模型选型对照证据。
4. **原理 [分析]**：Bi-Encoder 分别编码后点积，可离线缓存文档；Cross-Encoder 对 `[query,document]` 联合编码，交互更细，但每对都要推理。当前 `max(window logits)` 偏向“有一段匹配”，没有直接优化多个必要证据的联合覆盖。
5. **替代 [分析]**：直接保留 B 排序；按问题分解/证据多样性重排（MMR、分组约束），后者尚未实现。
6. **Trade-off [分析]**：B 无追加模型成本、维护少，精排可能不足；当前 C 增加模型内存、CPU与窗口管理，单段质量可能提升但多证据退化；覆盖导向重排可减少冗余，却引入子问题错误、相似度/多样性权重与新评测负担，不保证收益。
7. **证据及局限**：[test_rerank_retrieval.py:L1 — 证据](<E:/agentic project/knowledge-review/test_rerank_retrieval.py:1>) 覆盖同池、超长、失败分母与排序；[实验] 测试暖 C P50=1112.44ms，是**纯重排**。8条多证据题前三完整证据 B5/8、C0/8；模型内部原因是分析，不能由排名直接确认。长窗真实试验覆盖有限。
8. **今天评审 [分析]**：暂留离线符合现有证据；不应为使用“高级技术”直接上线。若用户问答延迟预算更紧，这一成本尤其需要质量收益支撑。
9. **小实验 [建议]**：先用新 dev 集比较原 B、C、C+简单文档/证据配额；固定候选与生成模型，记录每必要组在池内/前K/最终输入中的位置，再评答案。已有 test 失败可做诊断，不再当独立调参验收集。
10. **追问**：max pooling 为何可能偏好长块？候选包含全部证据为何最终仍答错？P50(B)+P50(C) 是否等于实测 P50(B+C)？答：分位数不能一般直接相加，现报告相加样本也仅为估计链路。

## D03. 结构分块、软字符上限与表格依赖

1. **当前实现 [实现]**：[chunking.py:L37 — chunk_source](<E:/agentic project/knowledge-review/chunking.py:37>) Markdown outer blocks/标题层级切分，默认 max_chars=1200、min_chars=100；大单块允许超软上限，小块贪心相邻合并，无通用固定 overlap。PDF 走 [chunking.py:L146 — document_chunks](<E:/agentic project/knowledge-review/chunking.py:146>)；[chunking.py:L193 — with_table_context](<E:/agentic project/knowledge-review/chunking.py:193>) 将真实表头/表注等作为独立范围；[embeddings.py:L98 — LocalEmbedder.prepare_chunks](<E:/agentic project/knowledge-review/embeddings.py:98>) 再按 token 预算细分真实行。
2. **问题 [分析]**：固定字符切割破坏代码、表格和句子；纯大块又稀释检索且超模型容量。结构身份与检索窗口需要分离。
3. **历史依据**：[docs/document-rag-and-token-budget.md:L32 — 证据](<E:/agentic project/knowledge-review/docs/document-rag-and-token-budget.md:32>) 明确解释保留完整行是引用契约要求；[docs/pdf-document-design.md:L29 — 证据](<E:/agentic project/knowledge-review/docs/pdf-document-design.md:29>) 记录表格依赖。[未知] 1200/100 的效果最优性、早期选择过程没有消融证明。
4. **原理 [分析]**：使用 Markdown token.map 的零起始半开位置转换成原文1起始闭区间；哈希绑定版本/算法/行范围。分块是信息单元与召回粒度的取舍，不只是字符串长度控制。表头与正文不连续时使用区间集合，不能伪造中间原文可见。
5. **替代 [分析]**：固定 token 窗口＋overlap；父子块检索（小块召回、大块补读）或语义边界切分。
6. **Trade-off [分析]**：当前方案结构可解释、无需额外模型，但超长块还需二次切分；固定窗实现简单、边界覆盖好，却重复编码/占预算且需字符位置引用；父子块有助上下文完整，增加索引映射和补读预算；语义切分增加编码成本与阈值维护。
7. **证据**：[test_chunking.py:L12 — test_ranges_cover_original_nonblank_lines_once](<E:/agentic project/knowledge-review/test_chunking.py:12>)、[test_chunking.py:L23 — test_code_table_list_and_quote_stay_whole](<E:/agentic project/knowledge-review/test_chunking.py:23>)；[test_embeddings.py:L67 — test_long_code_block_splits_into_exact_original_line_ranges](<E:/agentic project/knowledge-review/test_embeddings.py:67>) 说明“结构阶段不拆”不等于“最终检索永不拆”。[测试] 覆盖/范围正确不代表最优召回；未找到分块参数对照实验。
8. **今天评审 [分析]**：在可定位原文的学习应用里保留结构契约；不要仅调 overlap 而忽略引用与表头依赖。
9. **小实验 [建议]**：20份公开笔记，标注至少10条跨边界/表头依赖题；对结构方案、256-token/32-overlap、父子块记录完整证据覆盖、重复率、最终预算消耗。重建 manifest，不能沿用旧 chunk 标签。
10. **追问**：为什么 max_chars 不是硬上限？小块合并能跨标题吗？跨块问题应先增加 overlap 还是调用 read_source？答：依据是否缺连续上下文及预算决定，当前已有精确补读工具。

## D04. 本地 E5、超长输入完整窗口与向量池化

1. **当前实现 [实现]**：[embedding_config.py:L28 — select_embedding_config](<E:/agentic project/knowledge-review/embedding_config.py:28>) 默认 multilingual-e5-small、`query: `/`passage: `前缀；[embeddings.py:L25 — LocalEmbedder.__init__](<E:/agentic project/knowledge-review/embeddings.py:25>) CPU、默认离线、显式下载；容量读取模型 max_seq_length。[embeddings.py:L136 — LocalEmbedder._windows](<E:/agentic project/knowledge-review/embeddings.py:136>) 保留全部字符，[embeddings.py:L156 — LocalEmbedder._encode_complete](<E:/agentic project/knowledge-review/embeddings.py:156>) 按窗口原文字符数加权平均后归一化，长 query 同样处理；不是句向量拼接。
2. **问题 [分析]**：编码器默认截断可能让尾部关键事实从向量消失，而引用仍指完整行；频繁重编码与远程服务依赖也有成本。
3. **历史依据**：[docs/document-rag-and-token-budget.md:L32 — 证据](<E:/agentic project/knowledge-review/docs/document-rag-and-token-budget.md:32>) 明写“整行引用”和主题混合的风险。[未知] 未见 E5 对其他模型/在线 API 的系统胜出证据；本地选择可以解释隐私/部署取舍，不能说当时已证明效果最好。
4. **原理 [分析]**：`v=normalize(Σ len正文窗_i·normalize(encode(prefix+窗_i)))`。所有字符被处理只证明覆盖；均值会混合不同主题，跨窗否定/条件关系可能失真。单位向量余弦等于点积，平方欧氏距离为 `2−2cos`。
5. **替代 [分析]**：长上下文 embedding 模型；多向量检索（每窗独立向量或 late interaction）；在线 embedding API 也是部署替代。
6. **Trade-off [分析]**：当前一块一个向量、接口简单，但丢窗间细节且 CPU 冷加载明显；长模型改动较少但权重/内存/供应兼容成本更高；多向量保留局部匹配却增大存储、候选去重与引用映射；在线服务省本地维护但增加网络、付费与资料传输边界。
7. **证据**：[test_embeddings.py:L50 — test_long_single_line_and_query_keep_all_text_in_bounded_windows](<E:/agentic project/knowledge-review/test_embeddings.py:50>) 使用受控 tokenizer/vector；历史文档有真实本地4窗/384维覆盖记录，不是召回消融。[未知] 本轮未验证真实模型权重或长窗语义质量。
8. **今天评审 [分析]**：保留“不静默截断”的正确性约束；把池化视作兜底，针对长行查询做效果评测后再决定是否换多向量。
9. **小实验 [建议]**：同一事实分别放行首/中/尾，并加入否定条件；比较当前池化、独立窗最大相似度、长模型。记录 token 数、覆盖、相似度排序及答案证据，不以向量非零当成功。
10. **追问**：按字符加权为何不是 token 加权？前缀算权重吗？二分前缀长度对 tokenizer 是否严格单调？当前最终 fits 复查与连续前进能保证输出窗合规/覆盖，不保证每窗长度全局最优。

## D05. SQLite 派生向量缓存与精确内存检索

1. **当前实现 [实现]**：[embeddings.py:L50 — LocalEmbedder.cache_profile](<E:/agentic project/knowledge-review/embeddings.py:50>) 指纹覆盖实际权重、tokenizer、运行库、前缀与容量；[vector_index.py:L36 — get_document_vectors](<E:/agentic project/knowledge-review/vector_index.py:36>) profile 再纳入分块策略/维度，行含 source_id、范围、实际输入 hash、vector JSON/checksum。命中还检有限值/维度；损坏行重算，计算后短事务成批写。只按当前 chunks 查询，旧库行不会自动入候选。
2. **问题 [分析]**：相同模型名可以对应不同权重/配置；资料、标题或前缀改变会让旧向量失效。缓存目的主要是节省文档编码，不是实现 ANN。
3. **历史依据**：`df36eaa` 已有本地编码；`d86372e` 和相关设计记录确认当前完整窗口/缓存版本。[未知] 没有依据证明曾正式比较 Milvus、FAISS、Chroma 后淘汰它们。
4. **原理 [分析]**：缓存正确性是“同函数身份＋同实际输入⇒可复用”。计算量：dense 每次逐向量归一化/点积约 O(Nd)，完整排序 O(N log N)，空间 O(Nd)，不是 O(log N)。profile 每模型实例计算一次，不是进程全局一次。
5. **替代 [分析]**：无持久缓存；向量化矩阵/FAISS 精确或 ANN 索引＋独立元数据；服务化向量库。
6. **Trade-off [分析]**：SQLite 部署简单、事务明确，但 JSON向量/逐行查询/全扫描及每实例加载有限制；无缓存最易维护却重复计算贵；矩阵/ANN 提升大规模查询吞吐，但需删除更新/版本/过滤一致性，ANN还有召回损失；外部服务增运维、网络和备份成本。
7. **证据**：[test_vector_index.py:L59 — test_weight_settings_dimension_and_input_changes_do_not_reuse](<E:/agentic project/knowledge-review/test_vector_index.py:59>)、[test_vector_index.py:L83 — test_failed_rebuild_leaves_previous_data_intact](<E:/agentic project/knowledge-review/test_vector_index.py:83>)、[test_vector_index.py:L113 — test_database_write_failure_rolls_back_partial_rebuild](<E:/agentic project/knowledge-review/test_vector_index.py:113>)。测试证明失效/事务契约；历史暖耗时不证明跨请求冷加载消失。
8. **今天评审 [分析]**：小范围个人库可保留；先测编码/加载/指纹/扫描的分项占比，再决定优化。不要无数据地把数据库迁移当首要优化。
9. **小实验 [建议]**：100/1000/10000 synthetic chunks，分别测新实例首次、持久缓存命中、同实例暖查询；记录 RSS、模型加载、hash、SQL、余弦/排序时间。测试全局只读模型复用时另查并发安全和配置隔离。
10. **追问**：为何 checksum 不等于可信来源？为什么 `with connection` 不会关闭连接？为何仅用 chunk_id 或维度作缓存键不够？

## D06. 版本化 PDF 解析、可选 OCR 与按需 VLM

1. **当前实现 [实现]**：[pdf_parser.py:L94 — parse_pdf](<E:/agentic project/knowledge-review/pdf_parser.py:94>) 默认 PyMuPDF，提取文本/表格/图像位置，保存 issues；Docling 为显式可选且要求本地模型，失败记 effective_engine 并保留候选。[pdf_parser.py:L236 — docling_pages](<E:/agentic project/knowledge-review/pdf_parser.py:236>) 的 OCR 由配置开启。导入 VLM 仅开启后处理最多3个异常页；[pdf_vision.py:L163 — inspect_visual](<E:/agentic project/knowledge-review/pdf_vision.py:163>) 是 Agent 按需原图观察。[document_model.py:L47 — Document](<E:/agentic project/knowledge-review/document_model.py:47>)、[sources.py:L57 — source_from_document](<E:/agentic project/knowledge-review/sources.py:57>) 绑定解析快照/版本，阅读不自动重解析。
2. **问题 [分析]**：PDF 阅读顺序、表格结构与扫描文字不等价于普通纯文本。需要让缺失/转写不确定性和物理页位置可追溯。
3. **历史依据**：[docs/pdf-document-design.md:L1 — 证据](<E:/agentic project/knowledge-review/docs/pdf-document-design.md:1>) 记录统一 Document/ContentBlock/Asset 与解析阶段计划；其后实施段更新 VLM 按字段回落聊天配置。[未知] 无通用 OCR/VLM 引擎质量排名支撑最初选型。
4. **原理 [分析]**：OCR 从像素识字；VLM 可结合图表解释，但解释不是逐字原文。抽取视图行号和 PDF 物理页/bbox 是两种坐标；解析配置/产物变化需要新来源身份。当前 ready/partial 为程序质量标记，不是识别准确率。
5. **替代 [分析]**：所有页一律 OCR/视觉转写；文本优先并用独立高质量版面解析/OCR流水线；托管文档解析服务。
6. **Trade-off [分析]**：当前按需降低常规文档成本、保留原图，但路由启发式可能漏异常；全页视觉覆盖广但慢/贵且可幻觉；专门版面模型较可控但依赖大、部署复杂；托管省本地维护却有计费、上传和供应商锁定。
7. **证据**：[test_pdf_documents.py:L1 — 证据](<E:/agentic project/knowledge-review/test_pdf_documents.py:1>) 使用真实合成 PDF 和受控视觉返回；[test_table_reading.py:L1 — 证据](<E:/agentic project/knowledge-review/test_table_reading.py:1>) 验表头与分页。历史设计记录有单个真实视觉样例；未见复杂跨页/超宽表格、真实 Docling 的全面验收，不可写“完整多模态 RAG 已证明有效”。
8. **今天评审 [分析]**：保留版本/partial/按需观察，接受明确能力边界；在需要精确整表统计前，必须加结构完整性与计算工具，不能让 Top-K 片段承担全表最大值/求和。
9. **小实验 [建议]**：10页公开测试包覆盖扫描、双栏、跨页表格、单位脚注，人工逐项核验文本/单元格/阅读顺序；分别报告覆盖与错误，不将渲染成功算提取成功。
10. **追问**：VLM失败为什么不影响普通文字路径？导入成功为何仍可能 partial？图片导航标签能否作为原文依据？当前引用校验会拒绝仅导航块的文字引用。

## D07. 有界工具 Agent 与独立最终回答

1. **当前实现 [实现]**：[agent_loop.py:L84 — run_agent](<E:/agentic project/knowledge-review/agent_loop.py:84>) `tool_choice=auto`，默认4、可设1–6次实际工具预算，允许零调用。`json.loads` 后由 [knowledge_tools.py:L239 — KnowledgeTools.execute](<E:/agentic project/knowledge-review/knowledge_tools.py:239>) 严格校验白名单、参数与范围，返回 `output` 和 `visible_chunks`。ValueError/TypeError 回传模型并计预算；网络/运行错误可终止。最终始终走 [model_client.py:L44 — answer_question](<E:/agentic project/knowledge-review/model_client.py:44>)，传同一会话背景与程序 completed_actions。
2. **问题 [分析]**：允许模型按缺口搜索/补读，同时让执行权限、终止与正式答案契约掌握在程序中。
3. **历史依据**：初始 `df36eaa` 已有两阶段代码；[docs/document-rag-and-token-budget.md:L1 — 证据](<E:/agentic project/knowledge-review/docs/document-rag-and-token-budget.md:1>) 记录取消强制首次搜索；[docs/production-hybrid-search.md:L1 — 证据](<E:/agentic project/knowledge-review/docs/production-hybrid-search.md:1>) 明确记录最终阶段误称未执行搜索及 completed_actions 修正。[未知] 不能仅凭这些记录归属个人原创。
4. **原理 [分析]**：模型发工具名称＋JSON参数，Python执行真实函数，按 tool_call_id 返回结果，再让模型决策。不是模型自己执行代码，也不是 MCP。一次模型响应可包含多个调用；超预算调用回错误且不执行，不能简单把“工具上限”说成“HTTP上限”。最终阶段文本由证据重新生成，前阶段自由文本不直接发布。
5. **替代 [分析]**：固定一次 RAG（现 CLI 支持）；单循环内通过结果工具结束；LangGraph等持久工作流为更复杂流程的候选，未在当前问答采用。
6. **Trade-off [分析]**：当前适合按需、多次补读，工程边界清楚但增加一次最终生成与状态传递成本；固定RAG低延迟、好测但可能多搜/少搜；同循环输出省阶段切换却需同样实现发布校验；框架提供编排能力但无法自动解决项目的引用、版本、幂等问题。
7. **证据**：[test_agent_loop.py:L203 — test_general_answer_does_not_load_embedding_model_or_search](<E:/agentic project/knowledge-review/test_agent_loop.py:203>)、[test_conversations.py:L87 — test_both_model_phases_receive_same_chat_background_without_search](<E:/agentic project/knowledge-review/test_conversations.py:87>)、[test_production_retrieval.py:L140 — test_hybrid_topk_trimmed_to_actual_visible_lines_in_both_requests](<E:/agentic project/knowledge-review/test_production_retrieval.py:140>)。[实验] completed_actions 的历史单样例复测不能证明所有执行描述正确。
8. **今天评审 [分析]**：保留有界与统一发布入口；需评估额外阶段是否值得。工具动作记录是程序事实，不是事实证据，也不是隐藏思维链。
9. **小实验 [建议]**：30个新问题含寒暄、焦点足够、必须搜索、必须补读，比较固定RAG与Agent，记录工具选择/遗漏、HTTP次数、成本、最终正确性，错误阶段分开。
10. **追问**：零工具路径如何回答？为什么工具错误也扣预算？工具 Schema 校验能防资料里的恶意指令吗？答：只能限制执行边界，不能证明语义层免疫 prompt injection。

## D08. 完整生成预算与引用同步裁剪

1. **当前实现 [实现]**：[generation_budget.py:L16 — GenerationBudget](<E:/agentic project/knowledge-review/generation_budget.py:16>) 默认32768容量/4096输出/2048余量，为应用默认而非供应商保证。输入估算为 UTF-8 序列化字节＋协议开销；包含 messages、工具、结果、Schema。[generation_budget.py:L73 — fit_chunks](<E:/agentic project/knowledge-review/generation_budget.py:73>) 按完整原文行裁前缀或丢块，atomic/带依赖块不能随意截。显式焦点为必需块，超限失败；[model_client.py:L44 — answer_question](<E:/agentic project/knowledge-review/model_client.py:44>) 更新最终 chunks 后再校验引用。
2. **问题 [分析]**：只算正文会漏工具、历史、Schema成本；裁掉正文却保留旧引用权限会授权未见内容。
3. **历史依据**：[docs/document-rag-and-token-budget.md:L74 — 证据](<E:/agentic project/knowledge-review/docs/document-rag-and-token-budget.md:74>) 记载完整请求与两阶段预算方案。[未知] 默认容量与余量没有本轮供应商容量/极端输入校准证据。
4. **原理 [分析]**：`输入≤容量−输出预留−安全余量`。这是保守工程估算，不是 token 精确计算，更不是所有模型的数学上界。按完整行缩减保持行号语义；必需证据不足选择失败而非无声损失。
5. **替代 [分析]**：供应商精确 tokenizer/计数 API；超长时摘要替换证据；更大上下文模型。
6. **Trade-off [分析]**：字节估算无供应商依赖但中文可能过度裁剪；精确计数利用率高却绑定模型协议版本；摘要省预算但失去原文引用资格且会漏条件；长上下文减裁剪但成本/时延/注意力质量需实测。
7. **证据**：[test_conversations.py:L333 — test_schema_itself_and_result_tool_fallback_are_counted](<E:/agentic project/knowledge-review/test_conversations.py:333>)、[test_conversations.py:L350 — test_explicit_selection_and_current_question_are_not_silently_cut](<E:/agentic project/knowledge-review/test_conversations.py:350>)、[test_context.py:L1 — 证据](<E:/agentic project/knowledge-review/test_context.py:1>)。只验证程序计算与权限同步，不证明所有真实服务永不超限。
8. **今天评审 [分析]**：可保留保守默认；应把容量来源和实际 usage 作为诊断。尤其不能用 E5 的 tokenizer 计算生成模型容量。
9. **小实验 [建议]**：公开中文、英文、代码、表格各10例，对比保守估计与供应商返回 usage；记录裁剪损失/超限率，采用新配置后重测完整请求而非只测文本。
10. **追问**：为什么焦点不能悄悄截短？`chunks=None` 与 `chunks=[]` 有何区别？为什么 Agent 阶段见过的行在最终阶段仍可能无权引用？

## D09. 版本身份与“实际可见”引用校验

1. **当前实现 [实现]**：[context.py:L8 — validate_chunks](<E:/agentic project/knowledge-review/context.py:8>) 逐字比对来源快照及范围；[context.py:L55 — citation_is_visible](<E:/agentic project/knowledge-review/context.py:55>) 检查引用闭区间被已发送范围的并集无空洞覆盖。[answers.py:L50 — validate_answer](<E:/agentic project/knowledge-review/answers.py:50>) 校验 grounded/inference 引文、supplement 无引文、strict禁止 supplement、coverage一致性、视觉观察授权。历史字符引用另有契约。
2. **问题 [分析]**：存在于数据库的文本不等于发给模型；旧版本同一行也不等于新内容；合法文件名不足以防伪引用。
3. **历史依据**：[docs/pdf-document-design.md:L37 — 证据](<E:/agentic project/knowledge-review/docs/pdf-document-design.md:37>)、[docs/document-rag-and-token-budget.md:L46 — 证据](<E:/agentic project/knowledge-review/docs/document-rag-and-token-budget.md:46>) 明确表达原文/权限边界；初始代码已有来源校验。[未知] 没有证明该设计消除了全部幻觉。
4. **原理 [分析]**：对区间排序后维护 cursor，遇到 gap拒绝，覆盖到 end则接受。检验的是身份、范围和文本一致性；不检查“所引句子是否蕴含答案”。`inference` 允许推导但仍需可见前提。
5. **替代 [分析]**：只校验 source_id/行存在；新增逐主张语义蕴含校验/人工审核。二者分别弱化工程边界或补充语义层。
6. **Trade-off [分析]**：当前确定性、低成本、可复现但无法判断语义真伪；只验存在实现最轻却放行不可见/跨gap引文；语义校验可查不支持主张但增加模型成本、误判和评测维护，不能替代确定性范围校验。
7. **证据**：[test_context.py:L1 — 证据](<E:/agentic project/knowledge-review/test_context.py:1>)、[test_answers.py:L1 — 证据](<E:/agentic project/knowledge-review/test_answers.py:1>)、[test_table_reading.py:L1 — 证据](<E:/agentic project/knowledge-review/test_table_reading.py:1>)；[测试] 无跨gap等用例通过不等于 Faithfulness 已测。未见广泛逐主张人工 gold 的最终答案评测。
8. **今天评审 [分析]**：这是必须保留的不变量；效果宣称收窄为“阻止部分结构性伪引用”。
9. **小实验 [建议]**：造两类样例：合法行但含无关/相反事实；范围伪造但内容相关。分别测结构拒绝率、人工语义支持率，避免混成单一“引用准确率”。
10. **追问**：相邻区间可以联合引用吗？表头与正文之间未发的行能引用吗？合法引用和可证结论之间缺哪一步？

## D10. Structured Outputs、受限回退与增量预览

1. **当前实现 [实现]**：[structured_output.py:L42 — request_structured](<E:/agentic project/knowledge-review/structured_output.py:42>) 原生 parse优先，明确400不支持 response_format才回退单个 submit_result；forced tool_choice不支持才auto，成功后缓存端点/模型能力。[structured_output.py:L68 — request_streamed](<E:/agentic project/knowledge-review/structured_output.py:68>) 在尚未发正文时允许回退；[answer_stream.py:L116 — collect_answer](<E:/agentic project/knowledge-review/answer_stream.py:116>) 增量解析 `blocks[].text`，最后整段 JSON 经 Pydantic，随后引用/业务校验及保存。预览不是正式发布。
2. **问题 [分析]**：兼容服务能力不一；自由文本难保证字段；等待最终结构完成影响体验。必须同时维持格式约束和增量展示。
3. **历史依据**：[docs/WEB_AND_TOKEN_STREAM.md:L1 — 证据](<E:/agentic project/knowledge-review/docs/WEB_AND_TOKEN_STREAM.md:1>) 记录最新改动目标及验证；旧 ADR-014未更新。[未知] 本轮未验独立Gateway容器/网页部署，也未重跑历史真实供应商样例。
4. **原理 [分析]**：Schema限制语法/类型，Pydantic检查实际结果，业务校验约束证据/状态；三者不等价。流式小状态机跨JSON字符串转义/分片提取text，最终以完整对象为准；SDK chunk与token不一一对应。submit_result是提交结果的协议，不是让模型再执行一次业务工具。
5. **替代 [分析]**：仅最终JSON；自由文本流后补结构/引文；固定只支持一个原生Schema供应商。
6. **Trade-off [分析]**：当前兼容/体验较好但协议分支和解析维护复杂，用户可能短暂看见最终校验不通过的预览；最终JSON最简单但等待感强；自由流快但语义/引用后验对齐困难；单供应商减少回退复杂度却降低可移植性。
7. **证据**：[test_structured_output.py:L1 — 证据](<E:/agentic project/knowledge-review/test_structured_output.py:1>)、[test_answer_stream.py:L1 — 证据](<E:/agentic project/knowledge-review/test_answer_stream.py:1>)、[test_agent_sse.py:L1 — 证据](<E:/agentic project/knowledge-review/test_agent_sse.py:1>)。本轮运行情况见 [验证记录](VALIDATION.md)，不能忽略本地HTTP失败。[实验] 10-22文档有单条真实样例的delta/时延记录，本轮未复验，不作为性能基准。
8. **今天评审 [分析]**：保留预览与已保存结果区分、失败清预览、回读状态机制；优先核验流式错误和取消组合，不能拿早到正文代替正式成功率。
9. **小实验 [建议]**：受控服务分别在Unicode转义、工具名、正文中途断流；最终给非法引用；观察预览清除、无半结果写库、同request_id恢复。再测首字时延与完整保存时延。
10. **追问**：为什么已发正文后不重试另一协议？为什么一个逻辑生成可能多次HTTP？预览含错误引用时程序能否撤回用户已读到的内容？答：能清显示，无法保证用户未看到，属于产品信任边界。

## D11. 近期原话、压力摘要与历史召回

1. **当前实现 [实现]**：[conversation_context.py:L193 — build_context](<E:/agentic project/knowledge-review/conversation_context.py:193>) 历史预算默认不超过输入预算45%，结合必要数据再收紧；短对话用全部完成原话，超限至少保留最近两轮，增量摘要；[conversation_context.py:L144 — recall_originals](<E:/agentic project/knowledge-review/conversation_context.py:144>) 为当前聊天早期原话字面召回。另 [conversation_memory.py:L175 — ConversationMemory.search](<E:/agentic project/knowledge-review/conversation_memory.py:175>) 跨聊天按800字符/640步长分片，字面＋E5阈值0.78经RRF，最多5消息并补同轮邻居，精确字符引用；模型失败可明确降级关键词。
2. **问题 [分析]**：全历史超过容量；只保最近几轮忘记早期事实；仅摘要容易损失细节；助手旧解释不能变成文档事实。
3. **历史依据**：[docs/conversation-memory-phase1.md:L1 — 证据](<E:/agentic project/knowledge-review/docs/conversation-memory-phase1.md:1>)、[docs/conversation-memory-phase2.md:L1 — 证据](<E:/agentic project/knowledge-review/docs/conversation-memory-phase2.md:1>) 记录分期功能/边界。[未知] 0.78、45%、800/640的用户质量校准未确认；不能把当前聊天字面召回称为向量召回。
4. **原理 [分析]**：ContextBundle共享会话背景，原话为不可变事实记录，摘要是派生物；摘要保留证据消息ID/说话者，发布检查版本和前缀指纹。跨聊天可检归档、排除失效助手消息；文档引用和历史引用权限分离。
5. **替代 [分析]**：固定滑动窗口；全部历史向量检索或纯摘要；超长上下文直接发送全历史。
6. **Trade-off [分析]**：当前连续性/可追溯性较好但同步摘要增加延迟和失败点；窗口便宜确定却丢早期事实；纯向量容易漏精确上下文、纯摘要会压缩失真；全历史实现直观但成本/容量/注意力受限。完整扫描历史的当前实现规模受限。
7. **证据**：[test_conversations.py:L218 — test_long_chat_summary_recent_turns_coverage_evidence_and_originals](<E:/agentic project/knowledge-review/test_conversations.py:218>)、[test_conversation_memory.py:L102 — test_keyword_fallback_and_zero_unrelated_results](<E:/agentic project/knowledge-review/test_conversation_memory.py:102>)、[test_conversation_memory.py:L110 — test_semantic_recall_without_literal_match_uses_injected_model](<E:/agentic project/knowledge-review/test_conversation_memory.py:110>)。最后一个用注入模型，不是实际E5效果证据。
8. **今天评审 [分析]**：保留原话和独立历史引用；摘要/召回策略仍属启发式，需测“用户事实、偏好、助手解释”混淆和纠正覆盖。
9. **小实验 [建议]**：构建20轮公开合成讨论，植入偏好更新、同名不同对象、早期否定与过期结论；分别只近期/近期+摘要/再加召回，盲评事实保留、归属和错误继承。
10. **追问**：摘要失败为何有时还能继续、有时必须失败？摘要有证据ID为何仍不保证忠实？历史工具降级为什么不等于生产文档检索也自动降级？

## D12. 来源撤销与保守依赖传播

1. **当前实现 [实现]**：[conversation_store.py:L154 — ConversationStore.delete](<E:/agentic project/knowledge-review/conversation_store.py:154>) 在短事务内遍历 memory_dependencies，失效派生助手记录、清摘要/向量、递增 memory_revision，再删本聊天并留tombstone；[practice_provenance.py:L38 — revoke](<E:/agentic project/knowledge-review/practice_provenance.py:38>) 撤销依赖讨论练习资格。[conversation_store.py:L289 — ConversationStore.finish](<E:/agentic project/knowledge-review/conversation_store.py:289>) 保守继承依赖；[library_store.py:L150 — LibraryStore.change_document](<E:/agentic project/knowledge-review/library_store.py:150>) 文档替换保稳定document_id、改revision/来源身份并保旧版本。
2. **问题 [分析]**：删除原始聊天但保留派生摘要/向量会让其复活；原文替换后新行不能冒充旧题依据；撤销来源不应改写真实作答史。
3. **历史依据**：[docs/conversation-memory-phase2.md:L1 — 证据](<E:/agentic project/knowledge-review/docs/conversation-memory-phase2.md:1>)、[docs/library-and-chat-practice.md:L1 — 证据](<E:/agentic project/knowledge-review/docs/library-and-chat-practice.md:1>) 明确讨论传播、跨库撤销及旧文档快照政策。[未知] 不能把保守聊天级依赖称为精确语义因果图。
4. **原理 [分析]**：依赖图可达性传播，用visited避免重复/环；“内容记录仍显示”和“可继续作为有效记忆/练习来源”是不同状态。消息revision与字符范围绑定，删除并发时发布守卫使迟到任务无效。
5. **替代 [分析]**：只删原记录；精确主张级依赖；事件日志＋异步索引撤销。
6. **Trade-off [分析]**：当前同步/保守易保证不复活，但可能过度撤销、图大时写锁长；只删原记录简单却残留派生物；精确依赖减少误撤销但标注/推理不可靠；异步撤销扩展性好却需处理延迟窗口和请求时资格校验。
7. **证据**：[test_conversation_memory.py:L167 — test_indirect_derived_replies_summaries_vectors_revoked_raw_user_preserved](<E:/agentic project/knowledge-review/test_conversation_memory.py:167>)、[test_conversation_memory.py:L213 — test_embedding_deletion_race_does_not_restore_vectors](<E:/agentic project/knowledge-review/test_conversation_memory.py:213>)；[test_management_chat_practice.py:L1 — 证据](<E:/agentic project/knowledge-review/test_management_chat_practice.py:1>) 为关联学习路径测试，本轮未纳入该模块运行。没有磁盘安全擦除或远端删除证明。
8. **今天评审 [分析]**：在单用户小规模应用中保守传播合理；新增派生物必须注册依赖与撤销规则。政策差异要说清：旧文档快照可继续解释旧练习，已删除聊天来源不继续派生。
9. **小实验 [建议]**：A→B→C依赖链，在B生成/编码时删除A，核验C旧记录可见但不能召回、晚到写入拒绝、练习/FSRS真实记录保留；测高扇出撤销锁时长。
10. **追问**：为什么保留其他聊天的助手文本？怎样处理循环依赖？为什么逻辑删除不等于物理擦除？

## D13. SQLite 短事务、ATTACH 与资源生命周期

1. **当前实现 [实现]**：[conversation_store.py:L76 — ConversationStore.connection](<E:/agentic project/knowledge-review/conversation_store.py:76>) 每次连接启用外键、可ATTACH学习库，事务结束finally关闭；begin/finish/delete 用 `BEGIN IMMEDIATE`。[vector_index.py:L36 — get_document_vectors](<E:/agentic project/knowledge-review/vector_index.py:36>) 先编码后写缓存。业务模型调用通常在短写事务外；同聊天OS锁可以跨模型调用。文档原件文件与SQLite不是统一事务。
2. **问题 [分析]**：需要多表/跨库状态一致，又不能让慢网络请求长时间占写锁；文件/模型/数据库有各自生命周期。
3. **历史依据**：[docs/library-and-chat-practice.md:L1 — 证据](<E:/agentic project/knowledge-review/docs/library-and-chat-practice.md:1>) 说明默认DELETE日志和ATTACH撤销；[docs/project-context/ARCHITECTURE_DECISIONS.md:L1 — 证据](<E:/agentic project/knowledge-review/docs/project-context/ARCHITECTURE_DECISIONS.md:1>) 记录边界。[未知] 本轮没有检查真实运行数据库PRAGMA，也未做掉电实验；不可泛称部署一定满足跨文件原子条件。
4. **原理 [分析]**：SQLite同一数据库同时最多一个写事务；IMMEDIATE尽早取得写事务资格，竞争时可能BUSY，不能提高并发写数量。官方注明ATTACH跨文件提交原子性要求主库不是`:memory:`且非WAL等条件，不能随意切WAL后沿用原假设。参见 [事务](https://www.sqlite.org/lang_transaction.html)、[ATTACH](https://sqlite.org/lang_attach.html)。
5. **替代 [分析]**：合并业务库；PostgreSQL＋对象存储；模型调用放进长写事务（可讨论但不建议）。
6. **Trade-off [分析]**：当前本地部署/备份简单、短事务清楚，但写串行/跨文件配置维护成本存在；单库事务更简单但生命周期耦合；外部DB支持服务化并发但运维/迁移/鉴权成本更高，文件仍需发布协议；长事务减少部分应用竞态却把网络延迟变成写锁阻塞。
7. **证据**：[test_conversations.py:L270 — test_summary_requests_include_schema_budget_and_release_database_write_lock](<E:/agentic project/knowledge-review/test_conversations.py:270>)；缓存写失败回滚测试。受控事务测试不证明崩溃/掉电/远程文件系统保证；跨库删除受控测试另在management模块。
8. **今天评审 [分析]**：先维持已验证日志/连接约束，再做迁移。对文件落盘＋DB发布明确承认孤立文件风险，必要时加恢复清理而非宣称全流程原子。
9. **小实验 [建议]**：两个进程分别写同库，比较短事务与夹模拟网络等待时的BUSY/等待；另用一次性测试库在ATTACH更新中注入异常，确认两边回滚。掉电测试需独立环境，不能从异常回滚推导掉电结论。
10. **追问**：IMMEDIATE与EXCLUSIVE有什么区别？事务上下文为何不自动close？为什么持OS锁但不持SQLite写锁？

## D14. request_id、attempt、revision 与失败恢复

1. **当前实现 [实现]**：[conversation_store.py:L238 — ConversationStore.begin](<E:/agentic project/knowledge-review/conversation_store.py:238>) 按聊天/request_id唯一，核对原请求，completed直接重放、failed重试复用轮次，赋新attempt；[conversation_store.py:L89 — ConversationStore.generation_lock](<E:/agentic project/knowledge-review/conversation_store.py:89>) OS非阻塞锁；[conversation_store.py:L289 — ConversationStore.finish](<E:/agentic project/knowledge-review/conversation_store.py:289>) 检查running/attempt和文档/记忆版本后原子存助手结果。[conversation_store.py:L231 — ConversationStore.recover](<E:/agentic project/knowledge-review/conversation_store.py:231>) 把遗弃running置failed，[conversation_store.py:L322 — ConversationStore.skip](<E:/agentic project/knowledge-review/conversation_store.py:322>) 将failed置cancelled。
2. **问题 [分析]**：重复点击/断线重试、两次相同输入、模型运行期间资料变化、旧任务迟到返回是不同问题，不能用单个标志解决。
3. **历史依据**：[docs/conversation-memory-phase1.md:L1 — 证据](<E:/agentic project/knowledge-review/docs/conversation-memory-phase1.md:1>) 记录幂等与恢复；相关测试明确旧请求缺mode补dense的兼容。[未知] 原始完整锁选型讨论不足，文件锁的部署简化是工程分析。
4. **原理 [分析]**：request_id标识一次逻辑提交，不按问题字符串去重；attempt标识当前执行拥有的发布权；revision标识依赖内容版本；锁限制同聊天活跃执行；UNIQUE是数据库最后防线。`UPDATE ... WHERE attempt=?` 的rowcount是CAS式检查。可保证受条件保护的不重复发布，不保证供应商exactly-once计费。
5. **替代 [分析]**：仅前端禁按钮；单长事务；持久队列＋租约/fencing token。
6. **Trade-off [分析]**：当前低运维且边界明确，但状态恢复复杂、OS锁不是跨机器租约；前端防重仅改善体验；长事务占锁严重；队列可分离接收/执行、扩展多机，却仍需幂等键/发布版本与可观测恢复。
7. **证据**：[test_conversations.py:L118 — test_failure_duplicate_retry_and_completed_replay_save_once_without_learning](<E:/agentic project/knowledge-review/test_conversations.py:118>)、[test_conversations.py:L138 — test_interrupt_recovery_and_skip_keep_input](<E:/agentic project/knowledge-review/test_conversations.py:138>)、[test_conversations.py:L152 — test_same_conversation_concurrency_rejected_and_read_does_not_reset_live_turn](<E:/agentic project/knowledge-review/test_conversations.py:152>)。真实进程崩溃/计费幂等未在本轮验证。
8. **今天评审 [分析]**：保留三种身份与短事务发布；部署扩到多进程/多机时重新界定锁、槽、数据路径，不能把本机文件锁包装成分布式一致性。
9. **小实验 [建议]**：受控模型在返回前挂起：重复同ID、同ID改输入、删除/替换来源、新attempt完成后旧attempt返回；检查消息数、状态、计费调用数分开记录。
10. **追问**：同一个问题提交两个不同request_id是否应生成两次？失败为何阻塞下一轮？HTTP超时后怎样判断是否已保存？

## D15. 有界工作池、SSE背压与协作取消

1. **当前实现 [实现]**：[agent_stream.py:L216 — StreamRunner._reserve](<E:/agentic project/knowledge-review/agent_stream.py:216>) 非阻塞信号量，满503/Retry-After，默认workers4、范围1–32；指定JSON/SSE、后台/标题共池，未覆盖所有调用路径。[agent_stream.py:L276 — StreamRunner._submit](<E:/agentic project/knowledge-review/agent_stream.py:276>) 真实Future完成才释放；标题最多1且多worker留余量。[agent_stream.py:L75 — EventBridge.emit](<E:/agentic project/knowledge-review/agent_stream.py:75>) delta队列满等待形成背压，普通progress满可失败，终态单独保留。[agent_cancel.py:L53 — CancellableBody](<E:/agentic project/knowledge-review/agent_cancel.py:53>) 同步接口包异步HTTP，检查取消并等待清理；Token作用域内SDK重试关闭。
2. **问题 [分析]**：FastAPI事件循环不能被慢同步网络阻塞；无界任务积压耗内存；断连停止发送并不等于线程/连接退出，提前还槽会失去上限。
3. **历史依据**：[docs/gateway-sse.md:L1 — 证据](<E:/agentic project/knowledge-review/docs/gateway-sse.md:1>) 有10-16/18/19/20演进，[docs/WEB_AND_TOKEN_STREAM.md:L1 — 证据](<E:/agentic project/knowledge-review/docs/WEB_AND_TOKEN_STREAM.md:1>) 补最新delta。[未知] workers4等不是容量压测选优；独立Gateway的Redis/鉴权/队列不是本项目源码事实。
4. **原理 [分析]**：准入数量与实际工作生命周期绑定；请求取消、worker观察取消、HTTP资源清理、业务发布互相独立。ContextVar传递当前Token，threading.Event做协作标志；GenerationCancelled继承BaseException避免被普通业务 `except Exception` 吞掉。CPU/文件/SQLite/DNS阻塞不强杀；供应商停算未知。
5. **替代 [分析]**：全异步业务；有界持久队列＋独立worker；无界线程池排队（风险基线）。
6. **Trade-off [分析]**：当前复用同步服务、上限明确，但有两套异步/同步桥和单进程上限；全异步减少线程等待却需改全链、CPU仍可能阻塞；持久队列提升恢复/多机能力但增加状态/重试/部署复杂度；无界队列把拒绝推迟成高尾延迟与内存风险。模型网络I/O与本地embedding CPU必须分开测。
7. **证据**：[test_agent_admission.py:L1 — 证据](<E:/agentic project/knowledge-review/test_agent_admission.py:1>)、[test_agent_governance.py:L1 — 证据](<E:/agentic project/knowledge-review/test_agent_governance.py:1>)、[test_agent_cancellation.py:L1 — 证据](<E:/agentic project/knowledge-review/test_agent_cancellation.py:1>)、[test_answer_stream.py:L1 — 证据](<E:/agentic project/knowledge-review/test_answer_stream.py:1>)；本轮有HTTP类初跑失败，见验证记录。受控本地HTTP即使通过也不证明远端计费停止或高并发容量。
8. **今天评审 [分析]**：保留真实Future退出后还槽，优先清楚列出治理覆盖；在并发量未测前不扩workers或声称支持高并发。
9. **小实验 [建议]**：公开本地慢服务下2槽提交3请求、慢消费者、断流、取消后清理延迟，记录held_slots与真实活动连接；CPU慢任务验证取消不强杀且槽不提前释放。之后再测P95/P99/拒绝率。
10. **追问**：async def里调用同步SDK为什么仍阻塞？Future.cancel为何不等于杀线程？后台202返回为什么不能释放槽？为什么POST SSE用fetch解析而不是原生只发GET的EventSource？

## D16. 隔离评测、标签冻结与完整证据指标

1. **当前实现 [实现]**：[quality_retrieval.py:L62 — run_baseline](<E:/agentic project/knowledge-review/quality_retrieval.py:62>) / [quality_retrieval.py:L120 — run_hybrid](<E:/agentic project/knowledge-review/quality_retrieval.py:120>) 保存同语料/分块/范围排名；[retrieval_benchmark_data.py:L305 — ReviewStore.freeze](<E:/agentic project/knowledge-review/retrieval_benchmark_data.py:305>) 冻结标签版本；[retrieval_benchmark_metrics.py:L22 — score_run](<E:/agentic project/knowledge-review/retrieval_benchmark_metrics.py:22>) 调BEIR/trec，使用保存名次的递减分数保持同分顺序，真实分数另存。C候选指标从B input_pool计算；失败/缺失留分母，负例/排除分列；未freeze不给质量数。
2. **问题 [分析]**：运行成功、检索命中、完整证据、答案正确、学习效果不是同一指标。边运行边改标签、不同候选池、只统计成功会使比较失真。
3. **历史依据**：[docs/retrieval-benchmark-design.md:L1 — 证据](<E:/agentic project/knowledge-review/docs/retrieval-benchmark-design.md:1>) 与 [evals/retrieval/expanded-v1/results-20261013.md:L1 — 证据](<E:/agentic project/knowledge-review/evals/retrieval/expanded-v1/results-20261013.md:1>) 有运行/冻结身份与边界；[docs/quality-evaluation.md:L1 — 证据](<E:/agentic project/knowledge-review/docs/quality-evaluation.md:1>) 区分smoke/live。[未知] 原始本机Trace本轮未读取，仓库副本不是完全可重建冻结库。
4. **原理 [分析]**：Recall@K为正相关块找回比例，Hit为任一命中；完整证据要求**每个group至少一个bundle全部在前K**。MRR看第一个正例；nDCG关注分级相关排序，本实现报告注明BEIR/trec线性gain，不应机械套教程中的指数gain。按查询均值与按问题族均值也不同。
5. **替代 [分析]**：只做单元/手工演示；只用LLM-as-a-Judge端到端评分；真实用户随机A/B（当前未实现）。
6. **Trade-off [分析]**：当前可追踪失效阶段、复算不重新跑模型，但审核成本高且自建合成分布偏窄；手工演示便宜却无稳定分母；LLM判分覆盖快但有同源偏差/方差/成本；线上A/B最贴近用户却要分桶、样本量、回滚、隐私与统计设计。
7. **证据**：[test_hybrid_retrieval.py:L216 — test_frozen_comparison_real_beir_and_missing_query_zero](<E:/agentic project/knowledge-review/test_hybrid_retrieval.py:216>) 使用受控排名＋真实BEIR；[test_rerank_retrieval.py:L1 — 证据](<E:/agentic project/knowledge-review/test_rerank_retrieval.py:1>) 检查候选指标不可偷换。[实验] 具体数字见§3，不能说是本轮测量。
8. **今天评审 [分析]**：保留冻结和失败清单；优先扩独立问题族/真实公开语料与最终答案链路，而不是继续追已知test分数。
9. **小实验 [建议]**：新问题族冻结后，固定生成配置，依次检查解析→候选50→前K→最终可见→答案逐主张；配对记录质量/成本。区间按问题族重采样，不假设100个改述完全独立。
10. **追问**：Recall提升为何答案会降？负例如何测拒答？为何C重排后的Top20不能作为候选召回增益？标签冻结能消除标注偏差吗？答：只能固定比较依据，不能保证标签正确。

## 3. 已有实验可以讲到什么程度

唯一在这里引用完整指标表的历史产物：[evals/retrieval/expanded-v1/results-20261013.md:L1 — 证据](<E:/agentic project/knowledge-review/evals/retrieval/expanded-v1/results-20261013.md:1>)。报告ID `303339665061499da918c318faf34a04`；freeze `6c215fa76fc64742af175be84c9f8a06`。本轮逐项核对该版本化副本与指标代码，**未重跑真实E5/Cross-Encoder，未复建原始SQLite冻结历史**。

| 条件/指标 | A | B | C |
| --- | ---: | ---: | ---: |
| 可回答test题数 | 72 | 72 | 72 |
| Recall@10 | .87500 | .91667 | .91667 |
| nDCG@5 | .73702 | .80860 | .80037 |
| Hit@3 | .80556 | .88889 | .91667 |
| CompleteEvidenceHit@3 | .72222 | .83333 | .79167 |
| 候选Recall@50（C沿用B输入池） | .97917 | 1.00000 | 1.00000 |
| test暖P50 ms | 18.37 | 18.71 | 1112.44（仅重排） |
| test暖P95 ms | 22.73 | 23.08 | 1366.15（仅重排） |

100查询来自40条基础规则、10个领域问题族；dev20/2族、test80/8族，其中72可答/8负例；标签98 Agent采用＋2用户确认。暖样本test240（每题3次）；质量分母72，不是240。多证据子集8题，前三完整覆盖B5/8、C0/8；“C救回4/8、退化2/64”是另一个**任一正例命中**口径，不得与完整证据混用。

**结论边界**：B−A Recall@10约+4.17个百分点；C−B完整证据@3约−4.17个百分点。没有配对置信区间，没有最终答案正确率/真实用户留存/学习收益结果。全部调用成功不代表排序有效；报告中“排名变化100%”也不是改善100%。运行参数、设备、模型/语料指纹改变后必须新报，不复制旧数字。

本机原始Trace与冻结库不随Git克隆恢复。版本化副本可证明“项目保存了这些历史实验结论”，更强的可重复性声明需要恢复原始产物或依协议新跑。`audit.md`中B未上线描述为历史，当前实现以D01为准。

## 4. 重新评审时的优先顺序

1. **先保护已能确定的边界**：实际原文/版本/引用、幂等发布、取消后实际还槽。它们可用故障注入验收。
2. **再补质量证据**：真实公开笔记、多证据、否定条件、最终答案逐主张评测。当前最大的证据缺口不是“框架不够多”。
3. **再测性能再优化**：跨请求模型加载、权重指纹、向量扫描、同步摘要、远程模型时间分开测；暖18ms不代表一次网页问答18ms。
4. **规模变化后再迁移架构**：多用户权限、ANN、外部DB、分布式任务都属于新增系统约束，不能把当前单用户scope/文件锁/线程池直接改名当作已具备。

补充工程发现：[实现] [web_app.py:L1338](<E:/agentic project/knowledge-review/web_app.py:1338>) 模块末尾执行 `app = create_app()`，导入会构造默认Store；[PracticeStore.__init__:L28](<E:/agentic project/knowledge-review/practice_store.py:28>)还有建表/索引初始化。这使测试模块即使在setUp中另建临时库，也可能先触及默认库结构。[建议] 后续改为应用工厂入口或在导入前显式隔离默认路径，并加导入不触及正式数据目录的验证；本轮仅记录，未修改实现。

本次运行的通过、失败、复测及未执行范围统一见 [VALIDATION.md](VALIDATION.md)。阅读这些材料之后，还应亲手走一次学习地图中的可观察练习，再将真实完成的工作追加到自己的面试经历中；不要把“本次Agent审计”直接说成“我当年独立实现”。
