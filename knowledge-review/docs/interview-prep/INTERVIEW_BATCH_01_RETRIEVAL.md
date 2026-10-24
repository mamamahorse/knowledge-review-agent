# 首批面试答案：Dense、BM25、RRF 与范围内检索

范围：原题 Q35–Q46。Q47–Q48 是查询改写与多样性扩展，留到后续专题，避免把尚未实现的方案混进当前链路。本批在 [技术决策审计](TECHNICAL_DECISION_AUDIT.md) 完成后编写；基线为2026-10-23工作区，证据等级与运行结果见审计和 [验证记录](VALIDATION.md)。

使用方式：先看概念与代码，再闭卷口述，最后做学习缺口练习。口述版约30–90秒，描述当前项目及能解释的依据，不代写未经证实的个人经历。历史实验统一引用 [冻结结果副本](../../evals/retrieval/expanded-v1/results-20261013.md)：72条可答test、8条负例另列、合成问题族、98 Agent标签＋2用户确认；不是本轮真实模型重跑。

## Q35

### 1. 面试问题

Q35. ★ 为什么单纯使用向量检索不能满足所有检索需求？举两个具体的失败例子。

### 2. 核心概念和原理

Dense检索学习文本到向量的映射，用接近程度近似语义相关性。它不是符号表、数据库谓词执行器或逻辑蕴含判定器。语义很相近的两段文字可能只差一个参数名、版本号或否定条件，但答案完全相反。

两个**说明机制的假设例子，不是项目已记录的真实失败**：

- 问“`request_id`与`attempt`分别负责什么”，资料中同时有request_id、job_id、attempt和revision。向量可能把几段“防止重复/过期”的解释排得接近，却漏掉精确标识符所在段。
- 问“失败后是否能发布、必须满足哪两个条件”，单段写“成功可发布”，另一段写“版本变化后即使成功也拒绝”。前者语义近，但缺少例外条件；Top-K若只有一段就不足。

这不意味着BM25一定能解决：如果问法完全改述原词，BM25也可能没有命中；如果相关条件分散，任一单段高分也不能代表完整答案。

### 3. 项目真实实现

[实现] [retrieval.py:L25 — rank_chunks](<E:/agentic project/knowledge-review/retrieval.py:25>) 对全部当前Chunk算余弦；[knowledge_tools.py:L150 — KnowledgeTools._search_notes](<E:/agentic project/knowledge-review/knowledge_tools.py:150>) 支持dense/hybrid；后者增加BM25并做RRF。无相似度阈值自动证明“可回答”；少于请求数时只返回已有材料。

[实验] 历史报告中，`budget-1-q2`的A存在candidate_evidence_incomplete_at50，B/C仍有前三证据不完整；`release-multi-2-3`的A/C有前三完整证据失败记录，B在该题前三保留了两条必要事实。它们能证明某次运行缺证据，**不能仅据结果确认根因就是模型不理解数字或否定**。具体根因还要看实际片段、scope、分数和裁剪。

### 4. 设计权衡

当前hybrid利用词法互补，成本主要是当前scope内索引/词项排序，不增加云端模型调用；但可能引入“字面相似却无关”的干扰。纯dense更简单、改述通常更自然；精确字段查询/词法过滤适合明确实体但容易漏同义词；query rewrite可增强语义表述，却可能改坏技术符号，项目文档检索没有已上线的独立改写流水线。

### 5. 面试口述版

项目最初已有向量检索，现在网页默认是向量加BM25再用RRF融合。纯向量的局限是，语义接近不等于精确条件一致，技术资料里的标识符、数值和否定条件尤其容易成为关键差异。另一个问题是多证据问题，找到一段相关材料并不代表找齐所有前提。项目的历史固定集里，B比A的完整证据覆盖更好，但我不会把它说成所有资料上的保证。我现在会先分清是候选根本没召回、排序挤出，还是最终预算裁掉，再判断是否需要词法路、补读或新的检索策略。

### 6. 深入追问及参考答案

- **“Dense不能精确匹配”是否过度概括？** 是。Dense可能学会术语关系；局限是没有精确匹配/逻辑正确性的保证。应该用实际错误集验证，不能预设它必然失败。
- **加BM25以后还缺证据怎么办？** 检查许可范围和解析；确认相关片段是否在两路候选及融合池；如果跨块条件缺失，补读相邻原文或分解问题；若候选齐但前K缺，审查融合和多样性，不能直接换生成prompt掩盖问题。

### 7. 学习缺口

亲手打开一个报告失败案例，把“问题→必要事实→实际chunk→分路排名→最终可见”列成表。不能只记住“Dense语义、BM25关键词”就认为掌握了错误诊断；也不能把本批举的假设例子说成自己的真实实验。

## Q36

### 1. 面试问题

Q36. ★ BM25 的基本原理是什么？TF、IDF、文档长度归一化分别有什么作用？

### 2. 核心概念和原理

本项目实现的BM25为：

\[
score(q,d)=\sum_{t\in unique(tokens(q))}\ln\left(1+\frac{N-df(t)+0.5}{df(t)+0.5}\right)
\frac{f(t,d)(k_1+1)}{f(t,d)+k_1(1-b+b|d|/\overline{|d|})}.
\]

`N`是当前scope的prepared chunks数；`df`是含词的块数；`f`为块内词频；长度是词法特征数；`k1=1.2,b=.75`。

- TF增加可提高得分，但分母让收益饱和，不能靠无限重复某词无限涨分。
- IDF让罕见词更有区分力。这里使用正IDF变体，频繁词仍可有正贡献。
- 长度归一化比较实际长度与平均长度，减少长块仅因内容多而更易命中的优势。
- `b=0`关闭长度归一化；`b=1`采用完整长度比例。对已有词项，`k1=0`使TF部分退化为1。

手算对应项目测试：N=2，rare只在A出现，A有2次rare，b=0。IDF=ln2，TF项=`2×2.2/(2+1.2)=1.375`，得分约`.953077`。查询写两次rare也只计一次查询词贡献。

### 3. 项目真实实现

[hybrid_retrieval.py:L74 — BM25Index.__init__](<E:/agentic project/knowledge-review/hybrid_retrieval.py:74>) 用Counter统计块TF，postings存`(chunk序号,frequency)`；[hybrid_retrieval.py:L88 — BM25Index.search](<E:/agentic project/knowledge-review/hybrid_retrieval.py:88>) 遍历去重查询词与对应posting，`log1p`计算IDF，累计得分后排序；无交集返回空，不补零分片段。检索字段是retrieval_context（如表头）＋原文，不单独按文件名加权。

[test_hybrid_retrieval.py:L40 — test_bm25_known_term_frequency_math_and_no_zero_fill](<E:/agentic project/knowledge-review/test_hybrid_retrieval.py:40>) 验证上述公式；[test_hybrid_retrieval.py:L49 — test_bm25_length_normalization_common_terms_and_empty_documents](<E:/agentic project/knowledge-review/test_hybrid_retrieval.py:49>) 验长度与空块。这是确定性公式验证，不是中文实际效果证明。

### 4. 设计权衡

BM25无需训练、可解释，适合技术术语但不理解否定或同义。简单TF-IDF更容易入门，却没有相同的TF饱和/长度控制；学习稀疏检索可扩展语义词项，但引入模型、训练分布与索引维护。当前k1/b未有本项目最优性实验证据，不能声称采用它们是调优结果。

### 5. 面试口述版

BM25可以理解为带词频饱和和长度归一化的词项打分。TF说明词在片段里有多突出，IDF说明它在当前资料范围里是否稀有，长度项避免长片段天然占便宜。项目使用正IDF版本，k1是1.2、b是0.75，查询词先去重，再通过倒排表累计分数。中文使用单字和双字特征，所以这里的长度不是汉字数。代码有手算测试，但没有证明这些参数在我们的真实查询上最优；我会把公式正确和效果有效分开说明。

### 6. 深入追问及参考答案

- **scope增减文档为什么原片段分数也变？** N、df、平均长度都改变；即使片段正文没变，词项统计也不同。缓存BM25必须绑定scope身份，不能仅看query。
- **`log1p(x)`和`log(1+x)`为什么数学相同却写前者？** 前者在x很小时有更好的浮点数值稳定性；在这里还直接对应正IDF公式。不是一种不同的相关性算法。

### 7. 学习缺口

手算三文档、两查询词的结果，分别令b=0/.75/1，解释排序变化；把TF从1加到2、10、100观察饱和。能背公式却说不出N/df统计范围，面试仍容易暴露缺口。

## Q37

### 1. 面试问题

Q37. BM25 和向量检索各自擅长哪些查询？对中文术语、代码符号和数字有什么区别？

### 2. 核心概念和原理

Dense把同义改述投影到相近向量，BM25依赖可匹配特征。这里是倾向，不是绝对分工：术语也可能有优质embedding，中文双字也可能造成误匹配。

| 查询类型 | Dense可能的优势/风险 | 本项目BM25可能的优势/风险 |
| --- | --- | --- |
| “何时不能写回”对应“版本冲突拒绝发布” | 可能识别改述 | 没有共享词时易漏 |
| `std::string_view`、`request_id` | 可能把近似符号混淆 | 完整符号＋组成词保留精确信号，也可能因共有id误召回 |
| `ACK=120` | 数值含义受上下文影响 | 能匹配ack/120，但不执行等式或范围比较 |
| “缓存过期” | 可关联失效/TTL | 单字/双字找到缓存/过期，不能证明两者在句中构成条件关系 |

以上是机制分析，不能代替实际对照结果。

### 3. 项目真实实现

[hybrid_retrieval.py:L20 — lexical_tokens](<E:/agentic project/knowledge-review/hybrid_retrieval.py:20>) 先NFKC与casefold，识别代码词及组成部分，对Han连续串输出单字和相邻双字。[test_hybrid_retrieval.py:L31 — test_chinese_phrase_code_identifiers_and_nfkc](<E:/agentic project/knowledge-review/test_hybrid_retrieval.py:31>) 覆盖全角ACK、120、std::string_view、user_id、C++/C#。[embeddings.py:L17 — passage_text](<E:/agentic project/knowledge-review/embeddings.py:17>) 在embedding输入中加章节/表格上下文；两路检索字段并非逐字完全相同，BM25没有一般的章节标题前缀增强，不能笼统声称“同一字符串喂两路”。

### 4. 设计权衡

当前词法方案无需词典，技术混合文本维护轻；但单/双字使特征多、存在噪声、不能做真正中文词义消歧。词典分词可生成更像词的单元，却要维护专业词典和代码例外；字符n-gram简单稳健但索引增长；纯dense维护少一个词法器，但丢失可解释的符号匹配通道。是否更好必须按查询类别分报。

### 5. 面试口述版

我会按查询类型理解两路互补。问法和资料措辞差得比较大时，Dense可能有优势；技术标识符、数字、短术语要求精确匹配时，BM25能提供很直接的词法信号。项目不是用通用中文分词器，而是做NFKC、大小写归一化、代码词及组成词、中文单字和双字。它能匹配ACK和120，但不能理解ACK是否等于120，更不能靠数字命中证明条件正确。因此两路的分数都只是排序依据，仍需要实际原文和最终证据校验。

### 6. 深入追问及参考答案

- **C++和C会不会混淆？** 当前规则可保留完整c++同时产生c组成特征；这保留精确通道也引入共有特征，效果取决于其他词/df，不能保证完全区分。
- **数值匹配能处理“大于120”吗？** 不行，BM25只匹配词项，不执行数值谓词。如果场景需要精确数值过滤，应新增结构化字段/查询或计算工具，并验证原文抽取正确。

### 7. 学习缺口

亲手运行lexical_tokens输出：`user_id`、`userId`、`超时重试`、`ＡＣＫ=120`。注意casefold后camelCase没有按大小写边界切分的额外逻辑。设计一组同义查询和符号差一字符查询，分别查两路，不凭直觉归因。

## Q38

### 1. 面试问题

Q38. ★ 你的项目如何实现 Dense + BM25 混合检索？完整描述候选生成和融合流程。

### 2. 核心概念和原理

混合检索至少有三个步骤：各路生成候选、统一身份并融合、按最终需求截断。这里必须分清三种K：每路候选50、融合候选50、模型工具返回默认3/最大10。返回更多结果也要付生成上下文成本。

### 3. 项目真实实现

从 [knowledge_tools.py:L92 — KnowledgeTools.__init__](<E:/agentic project/knowledge-review/knowledge_tools.py:92>) 到 [knowledge_tools.py:L150 — KnowledgeTools._search_notes](<E:/agentic project/knowledge-review/knowledge_tools.py:150>)：

1. 构造时绑定当前Source快照并结构分块，不加载E5/BM25。
2. 实际搜索先验证query/top_k，惰性取得embedder，计算query向量。
3. 首次搜索调用prepare_chunks，获取持久缓存或新编码的文档向量；同工具实例后续搜索复用_vectors。
4. hybrid时dense取50；用**同一prepared chunks范围**建立BM25，返回≤50且只含词法命中项。
5. RRF检查身份/原文/有限分数，合并双路贡献，按总分及scope顺序排序截到50。
6. fused[:top_k]返回SearchResult；execute补必要表头等原文，形成output与visible_chunks。
7. Agent和最终生成预算可再缩小可见范围，引用据最终范围校验。生产C没有接在这里。

[test_production_retrieval.py:L45 — test_lazy_read_then_search_reuses_exact_instance_bm25](<E:/agentic project/knowledge-review/test_production_retrieval.py:45>) 验惰性/实例复用；[test_production_retrieval.py:L69 — test_production_top10_matches_independent_frozen_protocol_b](<E:/agentic project/knowledge-review/test_production_retrieval.py:69>) 验生产与独立B协议排序一致，使用受控embedding。

### 4. 设计权衡

当前同scope融合让权限与统计一致，trace易诊断；每请求新工具实例会重建BM25且可能重载模型。预建全局索引可降低冷开销，但必须重新解决scope统计、删除版本、权限过滤与缓存隔离；多个独立服务可各自扩缩，却增加网络/一致性维护。单次固定RAG更可预测，但缺少按需补读能力。

### 5. 面试口述版

项目的混合检索从KnowledgeTools.search_notes进入。它只绑定本次许可的Source，真正搜索时才加载embedding。第一次准备当前块和向量，后续同实例只算新query向量。hybrid取Dense前50和BM25最多50，按chunk_id合并，用等权RRF得到最多50，再返回工具请求的前K，默认3、上限10。BM25统计也只在同一许可范围建立。返回之后还要经过Agent和最终回答的预算裁剪，所以检索候选并不自动成为可引用证据。C是离线对照，没有接入这条生产路径。

### 6. 深入追问及参考答案

- **为什么先编码query再取文档缓存？** 当前实现用query向量长度作为缓存维度校验，同时一次搜索必需query向量；这不是所有系统唯一正确顺序。可预先读取模型维度，但需保证身份一致。
- **两路之一失败会发生什么？** 文档生产检索显式记录失败并抛出，不自动冒充另一策略成功。BM25“合法空结果”不是失败，此时RRF自然保留dense顺序。

### 7. 学习缺口

闭卷画出两次同实例搜索和一次新请求，标明哪些步骤复用、哪些重跑。进一步指出持久缓存命中仍可能需要加载模型/算指纹；不能把暖单实例测试直接外推到网页首次请求。

## Q39

### 1. 面试问题

Q39. ★ 什么是 RRF（Reciprocal Rank Fusion）？写出公式，并用两个排名列表手算一次融合结果。

### 2. 核心概念和原理

\[
RRF(d)=\sum_{r\in\{Dense,BM25\},\ d\in r}\frac{1}{k+rank_r(d)},\quad k=60.
\]

两列表：Dense `[A,B,C]`，BM25 `[B,D,A]`。排名从1起，缺路贡献0：

| Chunk | Dense贡献 | BM25贡献 | 总分 |
| --- | ---: | ---: | ---: |
| A | 1/61 | 1/63 | .032266458 |
| B | 1/62 | 1/61 | .032522475 |
| C | 1/63 | 0 | .015873016 |
| D | 0 | 1/62 | .016129032 |

最终B>A>D>C。B在两路都靠前，所以即使Dense把A排第一，B仍胜出。这个结果由公式算出，不是语义标签证明B更正确。

### 3. 项目真实实现

[hybrid_retrieval.py:L110 — reciprocal_rank_fusion](<E:/agentic project/knowledge-review/hybrid_retrieval.py:110>) 用`enumerate(hits,1)`、contributions[chunk_id][branch]保存rank/raw_score/rrf_contribution，按总分排序。[test_hybrid_retrieval.py:L64 — test_rrf_uses_ranks_deduplicates_and_records_both_contributions](<E:/agentic project/knowledge-review/test_hybrid_retrieval.py:64>) 使用dense .99/.01和BM25 50000/1，验证只使用名次，分数尺度不会直接影响融合。

### 4. 设计权衡

RRF无需分数校准/训练，解释简单；代价是丢掉分差，dense第一名比第二名只高.001还是高.8，名次贡献一样。归一化分数融合保留间隔但需要校准；学习排序可加入特征但需要标签与防过拟合。RRF也不建模证据之间是否互补。

### 5. 面试口述版

RRF是按名次做融合，项目公式是每路贡献一除以60加该路排名，再把同一chunk的贡献相加，缺路记零。比如Dense是A、B、C，BM25是B、D、A，B的总分是一除以62加一除以61，比A的一除以61加一除以63略高，所以最终B第一。它解决了余弦和BM25原分尺度不同的问题，但会丢掉原分差距，也不保证多证据覆盖。项目同时保存各路原分和贡献，是为了能定位某段为什么上升或下降。

### 6. 深入追问及参考答案

- **一段被两个高度相关的检索器都命中，算不算双重计票？** 算两个路的支持。RRF不校正检索器相关性；如果加大量相似检索路，可能放大偏好，需要路权重/多样性评测。
- **所有总分相同如何稳定？** 当前按原chunks列表序号作为tie-break；可复现但不是语义更好的排序依据。不能把稳定性当质量提升。

### 7. 学习缺口

把表里BM25列表改成`[C,D]`重新手算；再让Dense第一名原分翻倍，验证RRF不变。能计算但不理解“丢分差”仍不够，需要给出它可能害排序的反例。

## Q40

### 1. 面试问题

Q40. 为什么使用 RRF，而不是直接对 Dense 相似度和 BM25 分数加权求和？

### 2. 核心概念和原理

余弦受归一化约束在[-1,1]；本项目BM25为正值累计且受query词数、词频、语料统计影响，没有相同量纲。直接`α·cos+(1−α)·bm25`中的α不等于“语义占α比例”：BM25值域若大得多会主导排序。

可以先做min-max/z-score等校准，但每query候选的极值、异常值或分布变化会影响归一化。RRF绕开分数尺度，用名次交换这份信息；它不是唯一合理方案。

### 3. 项目真实实现

[hybrid_retrieval.py:L110 — reciprocal_rank_fusion](<E:/agentic project/knowledge-review/hybrid_retrieval.py:110>) 保留raw_score作诊断，但总分只sum分路rrf_contribution；[knowledge_tools.py:L44 — SearchResult.as_dict](<E:/agentic project/knowledge-review/knowledge_tools.py:44>) 用score_kind区分rrf/cosine。原始RRF论文也讨论了不依赖任意分数尺度的特点，但论文结论不能证明本项目最优。[原论文](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf)

### 4. 设计权衡

RRF免校准、少参数、对分数单调变换不敏感；归一化线性融合保留置信间隔，可用dev调权重；学习排序还能引入长度、词项、来源等特征，但引入训练数据/部署/漂移维护。没有本项目三方案控制实验，因此只能说RRF是可解释起点。

### 5. 面试口述版

我现在能解释RRF的主要依据是两路原分不可直接比较。Dense是余弦，BM25是受词项和scope统计影响的累计分，直接加权可能只是让尺度大的那路主导。项目因此按排名融合，并保留原分方便诊断。这个设计减少了校准工作，但会丢掉分差，所以我不会说它天然优于所有加权方案。如果要进一步评审，我会在独立开发集上比较RRF、归一化加权和简单学习排序，再用新冻结测试集验收；现有记录没有证明当时做过这组对照。

### 6. 深入追问及参考答案

- **把BM25乘100，RRF是否变化？** 只要路内排序不变且分数仍有限，贡献不变；直接分数融合通常会变化。
- **RRF也有参数，为什么说少校准？** k控制名次衰减形状、还可有路权重；它免的是原分尺度校准，不是免除所有超参数评审。当前等权也可能不适合所有查询分布。

### 7. 学习缺口

构造3条数据，比较原分直接加权、min-max加权与RRF；加入一个BM25极端值，解释三者变化。不要仅复述“分数不可比”，要知道哪些归一化可选以及为什么仍需验证。

## Q41

### 1. 面试问题

Q41. RRF 中的常数 `k=60` 有什么作用？为什么不是 1 或 100？

### 2. 核心概念和原理

k是平滑名次优势的常数，**不是Top-K返回条数**。同一路第一名和第十名的贡献比为`(k+10)/(k+1)`：

| k | 第1贡献 | 第10贡献 | 第1/第10 |
| --- | ---: | ---: | ---: |
| 1 | .5 | .090909 | 5.5 |
| 60 | .016393 | .014286 | 1.147541 |
| 100 | .009901 | .009091 | 1.089109 |

k越小越强调头部；越大前后名次差别越弱，双路共同出现相对更有优势。形式上大k时`1/(k+r)≈1/k−r/k²`，出现路数的影响更突出。不能由此断言某个k的质量一定更高。

### 3. 项目真实实现

[hybrid_retrieval.py:L42 — HybridConfig](<E:/agentic project/knowledge-review/hybrid_retrieval.py:42>) 默认rrf_k=60；[knowledge_tools.py:L150 — KnowledgeTools._search_notes](<E:/agentic project/knowledge-review/knowledge_tools.py:150>) 构造默认配置，未根据当前query或标签动态调k。原论文把60作为先导实验后固定的值；那是**论文作者的实验**，不是本项目作者的选型经历。[原论文](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf)

[未知] 未找到本项目比较k=1/60/100并选优的历史产物。源码存在参数校验，不代表产品提供用户调k入口。

### 4. 设计权衡

固定60维护简单、实验可复现；较小k可能保护某一路第一名但也放大离群错排；较大k更强调多路共识但可能压低单路独有证据。按query类型自适应k/权重是候选方案，增加分类错误与调参复杂度，当前未实现。

### 5. 面试口述版

RRF里的60是排名平滑常数，不是取60条。k小会强化第一名对后面结果的优势，k大则让同一路不同名次更接近、双路共同命中的影响更明显。比如k为1时第一名贡献是第十名的5.5倍，k为60时约1.15倍。项目沿用了60作为默认值，但我没有找到项目自身调参证明它比1或100更好。论文里的先导实验不能算成我的实验，所以更准确的说法是：我能解释它的作用，当前最优性仍需要独立开发集验证。

### 6. 深入追问及参考答案

- **k趋于无穷会怎样？** 各贡献趋小但排序主要受出现路数影响，在相同出现路数内再比较名次细微差别。极端有限精度下还要防止差异被舍入，不应选无穷值；源码拒绝非有限参数。
- **能直接在现有100条test上试k选最高吗？** 可作为诊断探索，但此后该集已参与调参，不能再称独立测试。要用dev选参数，另冻结未使用test或新问题族确认。

### 7. 学习缺口

手画两路榜：相关片段只在Dense第1，干扰片段两路均第20，比较k=1/60/100排序。这个例子能具体说明“共识”可能牺牲独有证据。

## Q42

### 1. 面试问题

Q42. ★ 为什么项目的 Hybrid 检索先取 Dense Top-50、BM25 Top-50，最终只返回最多 10 条？

### 2. 核心概念和原理

召回池和最终上下文服务不同目标。候选池希望保留足够多可能相关材料；最终给模型的片段要控制冗余、Token成本、噪声和注意力负担。池越大并不自动使最终回答更好，特别是固定前K时可能引入新的高排干扰。

### 3. 项目真实实现

[hybrid_retrieval.py:L42 — HybridConfig](<E:/agentic project/knowledge-review/hybrid_retrieval.py:42>) branch_m=50/candidate_m=50；[knowledge_tools.py:L128 — KnowledgeTools.search_notes](<E:/agentic project/knowledge-review/knowledge_tools.py:128>) top_k默认3、范围1–10。双路最多100个不同身份先参与融合，然后截成50；SearchResult最多10。Agent预算和最终生成预算还可缩小实际片段，所以“返回10”不是保证模型最终见到10个完整块。

[test_hybrid_retrieval.py:L89 — test_invalid_parameters_and_real_candidate_limits](<E:/agentic project/knowledge-review/test_hybrid_retrieval.py:89>) 检查候选限制；[test_production_retrieval.py:L140 — test_hybrid_topk_trimmed_to_actual_visible_lines_in_both_requests](<E:/agentic project/knowledge-review/test_production_retrieval.py:140>) 检查两阶段裁剪。[未知] 50/10不是经本项目系统性能/质量最优搜索证明的结论。

### 4. 设计权衡

扩大候选至100可能增加覆盖，也增加分路排序/融合和离线重排成本；若只dense路取更多且仍全排序，主要比较集变化而非把计算从近似变精确。工具Top-K随预算自适应可提高利用率，但必须同步引用范围且避免质量分布不稳定；固定小K成本可控，却容易漏多证据。父子块补读可保连续语义，但增加工具调用与Token。

### 5. 面试口述版

项目把候选数和模型返回数分开：hybrid两路各最多50，融合后也最多50，工具默认返回3、上限10。候选较宽是为了给融合保留选择空间，返回较小是为了限制上下文成本和噪声；但这是工程解释，不是说50和10已经被证明最优。实际模型看到的还可能更少，因为工具和最终生成分别检查完整预算。对多证据问题，我会看完整证据在50池里是否齐、前K是否齐、最后是否被裁掉，再决定扩大哪一层，而不是一律增大Top-K。

### 6. 深入追问及参考答案

- **返回10条能否跨过200行读取上限？** 200是read_source单次显式读取边界，不是所有检索上下文的统一总行数上限。搜索有自己的条数和生成预算，不能混用两个限制。
- **Top-K从3增到10后答案反而差怎么解释？** 可能加入冲突/重复或过长材料，导致关键证据被后续预算裁剪、注意力稀释；先检查实际发送内容再评生成，不直接归因模型退化。

### 7. 学习缺口

对同一查询保存候选50、返回3/10、Agent可见、最终可见四个列表，算每层的完整证据覆盖和字节预算。只有真正看过这四层，才能避免把检索参数和上下文容量混淆。

## Q43

### 1. 面试问题

Q43. 同一个 Chunk 同时被两路召回，如何去重？如果没有去重会发生什么？

### 2. 核心概念和原理

同一证据被两路找到，应保留两路支持信号但只占一个结果位置。去重单位是稳定证据身份，而非“文本看起来一样”。同文本可能来自不同版本/不同来源，有不同引用资格；语义近重复是另一个问题。

### 3. 项目真实实现

[hybrid_retrieval.py:L110 — reciprocal_rank_fusion](<E:/agentic project/knowledge-review/hybrid_retrieval.py:110>) 建by_id和scope_order，contributions按chunk_id聚合各路rank/score。双路同一chunk合并分数；**同一路重复chunk视为上游错误，直接拒绝**；同ID对应不同Chunk、范围外或非有限分数也拒绝。[test_hybrid_retrieval.py:L64 — test_rrf_uses_ranks_deduplicates_and_records_both_contributions](<E:/agentic project/knowledge-review/test_hybrid_retrieval.py:64>) 与 [test_hybrid_retrieval.py:L81 — test_rrf_rejects_alien_duplicate_conflicting_and_nonfinite_candidates](<E:/agentic project/knowledge-review/test_hybrid_retrieval.py:81>)分别证明。

没有做全库语义近重复折叠，也没有默认文档配额。不同chunk即使高度相似，仍可能占满Top-K。

### 4. 设计权衡

身份去重确定、便宜、保版本语义；文本hash去重减少重复正文但可能抹去来源差异；相似度/MMR减少语义冗余却可能删掉外表相似但条件不同的必要片段。合并分数又保留两条结果会同时重复计票和浪费上下文，应明确结果唯一性不变量。

### 5. 面试口述版

项目按chunk_id合并两路结果，同一个chunk的Dense和BM25贡献会相加，但最终只占一个结果位置。这里有个细节：同一路重复候选不是静默去重，而是认为上游有错误并拒绝；同ID原文不同和越范围候选也会拒绝。不去重会让一个片段占多个Top-K位置，减少多证据覆盖并浪费Token。当前只做身份去重，没有语义近重复消除，所以不同但相似的块仍可能挤占结果，需要另评文档配额或多样性重排，不能把它们说成已实现。

### 6. 深入追问及参考答案

- **为什么不用Source ID去重？** Source是一份来源版本，里面多个chunk可能各自是必要证据。按Source去重会把多段证据强行删成一段。
- **两个不同来源内容完全一样能否合并？** 可以设计展示层聚合并保留所有来源身份，但要明确引用选择和版本；不能直接复用一个source_id冒充另一份资料。当前未实现这种聚合。

### 7. 学习缺口

构造同ID同内容、同ID不同内容、不同ID同内容三组输入，观察哪些合并、哪些拒绝、哪些保留。解释这种区别如何影响缓存、版本和引用，而不只说“用set”。

## Q44

### 1. 面试问题

Q44. ★ 如果 Dense 找到了关键证据，但 RRF 后证据掉出了 Top-K，如何分析问题？

### 2. 核心概念和原理

RRF优化多路排名支持，不直接优化答案所需证据集合。以k=60举一个**构造例子**：正确证据E只在Dense第一，得分1/61≈.016393；干扰D在两路都第20，得分2/80=.025，D会超过E。共识不等于正确。

### 3. 项目真实实现

[hybrid_retrieval.py:L110 — reciprocal_rank_fusion](<E:/agentic project/knowledge-review/hybrid_retrieval.py:110>) 保存分路贡献，[knowledge_tools.py:L128 — KnowledgeTools.search_notes](<E:/agentic project/knowledge-review/knowledge_tools.py:128>) 保存返回身份/范围/score与状态；[agent_loop.py:L84 — run_agent](<E:/agentic project/knowledge-review/agent_loop.py:84>) 保存agent_visible_ranges，[model_client.py:L44 — answer_question](<E:/agentic project/knowledge-review/model_client.py:44>) 保存final visible_ranges。离线比较的失败清单由 [retrieval_comparison.py:L10 — compare_runs](<E:/agentic project/knowledge-review/retrieval_comparison.py:10>) 关联指标和缺组。

排查顺序：确认E是当前版本/许可范围内且原文确实支持 → 看Dense前50位置 → 看BM25是否命中/位置 → 手算双方贡献 → 查融合前50/前K → 查预算是否再裁。只要E在Dense50但不在最终K，不能笼统记为embedding未召回；如果只看到用户答案没有E，也不能直接断言是RRF丢掉。

### 4. 设计权衡

保护单路Top-N配额能保独有证据，但会硬塞单路噪声；加权RRF可提高某路影响，但需要dev验证且可能损害另一类查询；MMR/证据覆盖重排可减少冗余，却要额外相似度/子问题假设。单纯扩大K最省改动，但Token、噪声和下游裁剪会抵消收益。

### 5. 面试口述版

我会先看完整链路，而不是马上改权重。先确认关键证据属于本次scope，再查它在Dense50、BM2550和融合池的位置，用保存的贡献手算为什么被挤下去。RRF确实可能让双路中等名次超过单路第一名，例如k为60时，双路第20名得.025，高于单路第一的约.0164。然后还要看工具返回和最终预算有没有裁掉它。确认是融合问题后，才在开发集试单路配额、权重或多样性；当前项目没有证据支持某一种必然最好。

### 6. 深入追问及参考答案

- **把E强制插第一行不就解决了吗？** 这等于使用已知gold修补单个test，无法泛化。应该定义可执行、不依赖测试标签的规则，再在未使用样本验证。
- **怎样证明是排序问题不是解析问题？** 同一版本原文中必要事实存在，prepared chunk准确包含它，且召回候选已有该chunk；这些条件满足后再定位融合/前K。需要保存实际文本与范围，不只看ID。

### 7. 学习缺口

在纸上造4个候选实现上述反例，再用项目RRF函数确认；把K加大后检查完整证据是否真正进入最终预算。完成后可以说“我验证过这个反例”，完成前只能说“这是机制分析”。

## Q45

### 1. 面试问题

Q45. BM25 的倒排索引原理是什么？你项目中中文分词与匹配规则有哪些限制？

### 2. 核心概念和原理

倒排索引从词项指向包含它的文档，而非每次遍历所有正文找词。本项目“文档”在索引层实际是prepared chunk。posting记录chunk序号与词频，长度数组用于BM25归一化。若查询词集合为Q，打分工作主要遍历`Σ|postings(t)|`，随后对命中H个块排序O(HlogH)；常见词可触及全部块，不能无条件说O(logN)。

### 3. 项目真实实现

[hybrid_retrieval.py:L74 — BM25Index.__init__](<E:/agentic project/knowledge-review/hybrid_retrieval.py:74>) 用`defaultdict(list)`、`Counter`；[hybrid_retrieval.py:L20 — lexical_tokens](<E:/agentic project/knowledge-review/hybrid_retrieval.py:20>) 使用NFKC/casefold、正则代码词和Han单/双字，未引入jieba、词典分词、词干化或停用词表。同一个Han字符串长度m通常产生m个单字＋m−1个双字特征，重复字符还影响TF。

[test_hybrid_retrieval.py:L57 — test_bm25_chinese_technical_query_matches_original_evidence](<E:/agentic project/knowledge-review/test_hybrid_retrieval.py:57>) 证明一个受控中文术语案例能命中，不能外推全中文语料效果。索引是内存、scope绑定、实例内复用，不是持久搜索引擎服务。

### 4. 设计权衡

当前规则无外部词典、对混合技术符号友好，但边界粗、同义词弱、单字噪声大；词典分词更紧凑但需处理专业新词；成熟搜索引擎提供持久倒排/压缩/查询功能，却增服务、schema与权限维护。把Han双字称为“中文语义分词”会夸大能力。

### 5. 面试口述版

倒排索引把词映射到包含它的片段和词频，查询时只遍历命中的posting，再用片段长度计算BM25。项目实现很轻量，用Python字典和列表存当前scope的索引；中文不是jieba分词，而是单字加双字，代码则保留完整符号和组成词。优点是不依赖专业词典、规则容易追踪，限制是噪声较多、同义表达和条件关系仍不理解。复杂度主要由命中posting总量和结果排序决定，常见词仍可能扫到全部块；它不是已经具备大规模搜索引擎性能的实现。

### 6. 深入追问及参考答案

- **可不可以把postings持久化？** 可以，但需将tokenizer版本、Chunk版本、scope统计与删除更新绑定。当前只持久化embedding，不存在自动复用全局BM25索引的实现。
- **为什么查询词去重、文档词却保留TF？** 当前定义让query重复不额外强调该词，文档重复进入饱和TF体现主题强度。这是实现选择，不是BM25所有变体必须如此。

### 7. 学习缺口

手写三段中文/代码的posting表和lengths，计算df而不是TF总和；理解“同一文档词出现10次，df只加1”。再比较是否删除单字特征会影响短词、长词与误召回，使用新dev案例验证。

## Q46

### 1. 面试问题

Q46. 如何做到检索范围过滤？为什么要在检索阶段限制可用文档，而不是回答后再过滤？

### 2. 核心概念和原理

检索范围既约束证据资格，也影响排序。先全库查Top-K再过滤可能只剩很少结果；越范围内容可能已进入模型、缓存诊断或统计中，最终删答案无法撤回这种暴露。预过滤让候选、统计和引用在同一范围内，不过在ANN系统里实现高效过滤有额外取舍。

### 3. 项目真实实现

[web_app.py:L845 — resolve_scope](<E:/agentic project/knowledge-review/web_app.py:845>) 根据目录ID解析当前资料，[web_app.py:L1154 — guard_documents](<E:/agentic project/knowledge-review/web_app.py:1154>)及发布事务核对版本；[knowledge_tools.py:L92 — KnowledgeTools.__init__](<E:/agentic project/knowledge-review/knowledge_tools.py:92>) 绑定本次Source，缓存仅查询当前chunks；[hybrid_retrieval.py:L72 — BM25Index](<E:/agentic project/knowledge-review/hybrid_retrieval.py:72>)只用这些prepared chunks算N/df/长度。[knowledge_tools.py:L206 — KnowledgeTools.read_source](<E:/agentic project/knowledge-review/knowledge_tools.py:206>)要求精确source_id和合法闭区间，不能用路径/ID前缀。最终 [answers.py:L50 — validate_answer](<E:/agentic project/knowledge-review/answers.py:50>) 再核实际可见资格。

[test_production_retrieval.py:L55 — test_original_text_topk_scope_and_score_units](<E:/agentic project/knowledge-review/test_production_retrieval.py:55>) 检查换source后旧版本不可读；[test_vector_index.py:L48 — test_changed_document_only_recomputes_that_document_and_omitted_files_stay_out](<E:/agentic project/knowledge-review/test_vector_index.py:48>) 检查旧缓存不回流候选。这些是单用户文档范围边界，**不是多租户ACL系统**；独立Gateway鉴权也不能替代资料级授权。

### 4. 设计权衡

当前按scope重建内存候选统计，范围清楚、易测，但重复准备成本高。全局索引＋严格预过滤可扩展，但要让所有路、缓存、诊断、原文读取同步权限；检索后过滤简单，却有Top-K被无权文档占满和信息暴露风险。多租户分库隔离强但资源/迁移成本高，均需重新设计而非沿用单用户scope改名。

### 5. 面试口述版

项目的工具在构造时绑定当前许可的Source快照，Dense候选和BM25统计都来自这个范围，SQLite里残留的旧向量不会自动变成可检索资料。读取原文还要求精确source_id和合法行号，最终引用再按实际发送范围校验。之所以前置限制，是因为全库先查再过滤不仅可能丢掉本来应召回的许可结果，还可能已经把不该用的材料发给模型。不过当前这是个人应用内的文档scope，不是多用户租户授权；扩到企业场景还要加服务端身份、ACL、缓存隔离和删除一致性。

### 6. 深入追问及参考答案

- **scope合法但文档在生成过程中被替换呢？** 检索使用不可变快照防中途内容混杂，发布前再查当前revision/removed；过期结果被拒。快照保证本次输入一致，版本守卫决定是否还允许发布，两者都需要。
- **为什么还要最终引用检查？** 许可范围只说明可以去查，不说明已经发送了全部内容。候选、返回与预算裁剪层层收窄，最终引用必须属于最后实际可见原文。

### 7. 学习缺口

准备公开A/B两份资料，先把B缓存，再只传A搜索，确认B不会出现；模型返回前替换A，确认发布失败保留输入。然后设计一个多租户反例，说明仅靠浏览器传document_id为什么不足以授权。

## 本批闭卷验收

1. 画出Dense/BM25/RRF/返回/预算/引用六层；说出每一层数量上限和身份。
2. 手算一次BM25、一次RRF，区分IDF、rrf_k和top_k。
3. 从源码指出惰性加载、scope统计、两路去重、异常不降级、最后权限收窄的位置。
4. 解释一个B优于A的历史指标及其分母；同时讲出一个B/C不保证更好的反例。
5. 用“当前实现／我能解释的原理／我亲自做过的验证”三列记录。最后一列只填写自己真正完成的实验与证据路径。

如果第1–3项还不能独立完成，优先回到源码和受控实验；暂时不要增加一串未实现的HyDE、图检索或框架名称来丰富项目介绍。
