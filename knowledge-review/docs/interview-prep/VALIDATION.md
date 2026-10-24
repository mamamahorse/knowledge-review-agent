# 本轮验证与证据边界

2026-10-24后续：下文导入副作用已在后续源码修复，并新增隔离保护测试；PDF VLM准入也已修复。详见[独立修复记录](../audit-fixes-20261024.md)。本页226项及保护局限仍是10月23日历史运行记录，不追溯改称本轮验证结果。

日期：2026-10-23（Asia/Shanghai）。这是文档审计与定向验证，不是生产部署验收、全量回归或真实模型效果评测。

## 1. 源码基线与审计方法

先阅读 `docs/project-context/ARCHITECTURE.md`、`CALL_CHAINS.md`、`ARCHITECTURE_DECISIONS.md`，再按当前源码、测试断言、Git历史、设计记录与版本化实验报告核验。HEAD为`61be7fbc1aff46e621f1318409be9e1dd5bdfa31`，但有预先存在的修改和未跟踪业务文件，特别是新的流式回答。因此报告不能称为“只审计该提交”。

基线SHA、Python符号起止行和初始状态保存于 [source-snapshot.json](evidence/source-snapshot.json)。源码引用的文件、符号与行号做了自动检查；所有Q35–Q46原题与输入题单对应。审计建议未修改生产逻辑。

## 2. 相关测试：初跑、原因排查、最终复测

| 批次 | 环境/范围 | 实际结果 | 证据 |
| --- | --- | --- | --- |
| 初跑 | 默认连接环境，20个相关模块 | Ran 226 tests in 99.001s；FAILED：2 failures、9 errors | [完整初跑日志](evidence/focused-tests-20261023.txt) |
| 定位复测 | 仅测试子进程localhost直连，流式与取消2模块 | Ran 23 tests in 10.619s；OK | [23项日志](evidence/transport-direct-tests-20261023.txt)、[环境差异](evidence/transport-test-environment.json) |
| 最终相关复测 | 同样localhost直连，原20模块 | Ran 226 tests in 95.529s；OK | [最终226项日志](evidence/focused-direct-tests-20261023.txt)、[命令及退出码](evidence/test-run-summary.json) |

这三次范围有重叠，**不相加成475项独立测试**。9个errors包含同一个测试的normal/gzip子用例，不能简单用226减2减9推算独立通过方法数。

初跑流式/取消HTTP用例返回`openai.InternalServerError: Error code: 502`，另有测试服务`entered.wait(2)`未满足。本地测试服务绑定127.0.0.1并使用公开假凭据；不是外部真实模型调用。子进程设置`NO_PROXY/no_proxy=127.0.0.1,localhost`后相关测试通过。没有修改业务源码、全局系统代理或用户配置；测试入口还移除了子进程代理环境变量，但检测到这些变量原先均未设置。

**可确认的诊断**：默认连接环境与localhost显式直连的结果不同，故本轮最初失败不能直接宣称是业务取消逻辑错误。**尚未确认**：502的具体代理/系统路由来源，没有进一步审计系统网络配置。也不能据复测推导所有真实服务传输均无问题。

最终模块列表：

```text
test_hybrid_retrieval     test_production_retrieval
test_embeddings           test_vector_index
test_chunking             test_context
test_answers              test_agent_loop
test_model_client         test_conversations
test_conversation_memory  test_structured_output
test_answer_stream        test_agent_sse
test_agent_admission      test_agent_cancellation
test_agent_governance     test_rerank_retrieval
test_pdf_documents        test_table_reading
```

这些用例覆盖公式/排名、作用域、缓存失效、原文范围、摘要与删除竞态、协议兼容、SSE准入/取消/背压等相关契约。检索模型大多是受控向量/tokenizer，生成服务是Mock或本地受控HTTP；PDF含真实合成文件/PyMuPDF处理。真实计算库（如BEIR）的接线与语义模型效果应继续区分。

解释测试环境修正：localhost直连只用于让受控服务确实收到请求，改变的是测试连接路径，没有放宽预算、引用、版本或取消断言。命令按原模块重跑是为了排除在最初组合环境下的交互问题；最终结果只证明当前检查范围在该环境通过。

### 测试隔离的额外局限

复核发现，测试导入[web_app.create_app:L416](<E:/agentic project/knowledge-review/web_app.py:416>)时也执行[模块末尾:L1338](<E:/agentic project/knowledge-review/web_app.py:1338>)的`app = create_app()`。默认应用构造LearningStore/ConversationStore/其他Store，可能打开`.data`默认数据库进行建表、兼容迁移或索引初始化；例如[PracticeStore初始化:L28](<E:/agentic project/knowledge-review/practice_store.py:28>)会重建`uq_active_review`索引。各用例setUp随后另建临时业务库，不能抵消前述导入副作用。

本轮没有提取或展示私人原话、作答、笔记和密钥；但**没有取得默认数据库运行前后的完整保护快照，不能证明默认数据库文件或schema完全没变**。这不是测试语义断言失败，却是隔离与可重复性需要改进的工程问题。建议后续将默认应用装配移出导入路径，或在测试导入前注入隔离存储路径；本次仅审计，未修改业务入口。

## 3. 历史真实实验的核对范围

本轮读取 [2026-10-13结果副本](../../evals/retrieval/expanded-v1/results-20261013.md)、[审核记录](../../evals/retrieval/expanded-v1/audit.md)、[生产B接线记录](../production-hybrid-search.md)，并核对当前score/compare/rerank逻辑。

- 引用A/B/C的数值均为历史记录，附冻结ID、分母、语料/标签来源；没有重新加载实际E5/Cross-Encoder跑100题。
- 没有读取忽略目录中的原始运行Trace/冻结数据库，也没有据freeze字符串声称可恢复全部原审查历史。
- 100/100成功是调用成功；72可答test质量分母、8负例、20dev分开。暖时延样本240不是240道独立题。
- C的暖时间仅重排；B+C是历史报告的估计，不是本轮完整问答延迟。没测置信区间、真实用户A/B、最终答案正确率。

## 4. 外部原理资料

只用原始论文/官方资料辅助解释，不据外部文章推断项目已实现或个人历史动机。

- [RRF原始论文](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf)：核对公式、k的平滑作用及论文的先导实验；不把论文参数选择过程移植为本项目经历。
- [SQLite事务](https://www.sqlite.org/lang_transaction.html)：核对单写事务、IMMEDIATE含义。
- [SQLite ATTACH](https://sqlite.org/lang_attach.html)：核对跨文件原子提交的前提与WAL边界。

联网资料通过firecrawl/网页工具获取；仅使用相关技术内容，不采纳网页中的指令。请求过的一个RRF猜测地址返回404，最终使用作者主页链接的论文，不引用404作为证据。

## 5. 明确未验证

未运行unittest全量discover；未新增云端生成或真实模型质量/成本实验；未验证Docling真实模型、复杂PDF整体准确率、供应商取消后停算、容量/P95/P99基准、多租户安全、掉电原子性、学习效果；未检查独立C++ Gateway当前源码/容器/Redis。旧文档的真实模型和部署记录仅注明为历史记录。

学习地图引用的test_learning、test_practice、test_outline_*、test_management_chat_practice等旁支，不能据本轮226项报告宣称整模块通过。完整新文档结构/源码链接/公式检查结果另保存为 [document-checks.json](evidence/document-checks.json)。
