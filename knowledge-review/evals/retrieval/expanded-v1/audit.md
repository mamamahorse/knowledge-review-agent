# 100 条扩展集：范围与初审记录

**最新状态：用户已确认两条建议，100条于2026-10-13冻结，98条Agent自动采用、2条用户确认、0待处理。冻结身份 `6c215fa76fc64742af175be84c9f8a06`。下文创建时待审及未冻结运行描述保留为历史记录；最新结果见文末。suite名称中的draft是不可变初稿名称，不表示当前标签仍未冻结。**

2026-10-13 建立独立版本 `retrieval-expanded-v1-100-draft`，不覆盖旧 20 条及其冻结。资料都是自行撰写的公开合成协议，**不是生产功能列表，也不是外部产品规范**。没有读取私人笔记、聊天或学习记录，未调用生成模型准备标签。

共有 10 份 Markdown、80 个实际 E5 预处理片段、100 条查询。开发集 20 条/2 个领域问题族；测试集 80 条/8 个问题族。80 条单事实查询是 40 个规则的术语/条件问法及语义改述；另有 12 条多必要证据题、8 条无答案题（含 3 条权限范围题）。同一规则的改述、组合与同领域无答案题不跨分区。查询数不是独立样本数；它仍是有限领域的合成压力集，不代表所有真实笔记。

冻结前可运行排名和延迟，不给未确认标签计算正式质量。B/C 参数沿用旧试运行，不根据本测试标签或新排名调参。已有试运行中的 string_view 退化案例留在旧报告，不移入新测试集冒充独立新题。

## 可复核的标注来源

1. `build_fixture.py` 中逐条撰写原文、问题及直接证据；没有读取 A/B/C 排名。
2. `section-audit.json` 保留 80 个章节命题的完整原文及行号。`suite.json` 内每题的 `section_judgments` 显式判定全部许可章节，并给出语义理由；不是把未判断默认为 0。
3. 少数互相支持的邻近规则标 1，直接充分支持所问问题标 2；多事实题每段只回答一个子问题，修正为 1，保留各自必要证据组，不把命中一段算全答。0 级按对象、条件、操作核对，不仅凭关键词不匹配。
4. `retrieval_fixture_audit.py` 只把这份已审台账映射到真正的预处理片段。原文不符、章节遗漏/重复、片段遗漏会报错，不负责自行判断语义。
5. `agent-review.json` 是等级修正后的完整初审，绑定实际 manifest、全部资料哈希、逐片段等级及理由；首次版本保留为 `agent-review-initial.json`，初标台账也保留。`grade-corrections.json` 明确记录12条组合题的修正，理由来自等级定义，而非新排名。数据库以CAS追加Agent修订，不覆盖原版、用户编辑、待处理项或旧冻结；98 条自动采用、2 条待用户决定。人工来源与 Agent 来源分开保留，不称全部人工 gold。

当前 benchmark：`ecd182209e2340afbd9defa36de97b7d`。打开 `/retrieval-review?benchmark=ecd182209e2340afbd9defa36de97b7d`，默认只显示两条分歧：

- `cache-4-q2`：cache_hit 计数段 2 级，TTL 撤销段建议 0 级；争议是失效背景是否构成部分支持。TTL 原文在 `cache.md` L9–L11，可用“全部片段”查看；页面当前焦点为必要计数证据。
- `release-4-q2`：evidence_revision 段建议 2 级；它直接说明不同能力证据不可替代、修订保留原回答。争议是这种证据资格说明是否足以支持口语化的“也会了吗”。

本文件记录创建时状态；最新用户决定及冻结以审核数据库中的追加版本为准，不能据此覆盖后续编辑。

## 重现方式

在应用目录使用既有 `.venv`：

```powershell
.\.venv\Scripts\python.exe quality_retrieval.py validate --suite evals/retrieval/expanded-v1/suite.json
.\.venv\Scripts\python.exe quality_retrieval.py prepare --suite evals/retrieval/expanded-v1/suite.json
.\.venv\Scripts\python.exe quality_retrieval.py baseline --benchmark ecd182209e2340afbd9defa36de97b7d --suite evals/retrieval/expanded-v1/suite.json
.\.venv\Scripts\python.exe quality_retrieval.py hybrid --benchmark ecd182209e2340afbd9defa36de97b7d --suite evals/retrieval/expanded-v1/suite.json
.\.venv\Scripts\python.exe quality_retrieval.py rerank --b-run <新B运行JSON>
.\.venv\Scripts\python.exe quality_retrieval.py compare --a-run <新A运行JSON> --b-run <新B运行JSON> --c-run <新C运行JSON> --freeze <这版冻结身份>
```

不得把旧 20 条的 freeze 传给新版本。首次在不同绝对路径/模型指纹下准备时，已有初审不匹配会保留待审，不偷偷套用旧身份。重新核对当前原文台账后，可运行 `bind-section-audit --benchmark <新身份> --sections evals/retrieval/expanded-v1/section-audit.json --corrections evals/retrieval/expanded-v1/grade-corrections.json --output <不存在的新初审JSON>`，再 `adopt-audit --benchmark <新身份> --audit <新初审JSON>`。绑定只投影显式语义判定及修正，不自动发布或冻结；重跑 prepare 不覆盖用户编辑。原文/分块规则变化导致额外片段时，先补审，不能强行映射。对已自动采用的确定错误，`adopt-audit --expected-agent-revisions <查询到预期版本的JSON映射>` 才可显式追加修正，过期版本、用户决定/草稿或待处理项一律拒绝，默认导入仍只初始化。

`build_fixture.py` 是维护资料的可复核生成器，正常评测不运行它；编辑或重建 fixture 后须作为新版本重新准备和初审，不能改已冻结快照。

## 结果如何使用

冻结后同一 Trace 可重新计算 BEIR，无需重跑模型。对照报告自动记录候选缺证据、前三缺必要证据、运行失败/缺失和 nDCG@5 退化，保存各系统前三等级、原文行号及缺失组。失败类型会重叠，不能相加当错误率；无答案的相似片段返回不等于错误拒答或幻觉。

仍未完成：代表性真实公开笔记集、更多独立问题族、按问题族配对的不确定性区间、最终生成答案评测、实测整条问答延迟。是否生产升级要看质量、失败模式和耗时一起判断；本版本建立不自动改变正式 Agent 的检索工具。

## 本机真实运行记录（2026-10-13）

A/B/C各100/100请求成功，每题首次调用另记及3次暖请求，CPU 8线程。C使用固定模型与完整保存的B池，未重新检索。当前没有新冻结，质量为null；成功调用不等于语义正确。

| 方案 | 测试集暖P50 ms | P95 ms |
| --- | ---: | ---: |
| A | 18.37 | 22.73 |
| B | 18.71 | 23.08 |
| C | 1112.44 | 1366.15 |

C时间仅纯重排，B+C是估计，不是正式问答端到端实测。新质量尚未确定，不能依据排名变化数宣称改善。

运行索引：`.data/evaluations/expanded-experiment-d1bb7e5e6bb7426fb109bf539c6b11d3.json`。

## 冻结后的质量结果与接入决定

2026-10-13用户明确确认两条原建议并授权冻结。使用ReviewStore.save的预期revision追加用户决定，freeze再核对所有当前版本；没有改98条自动初审等级、原初标、旧20条冻结或A/B/C原始Trace。冻结包含两个完整初审快照，保留98自动+2用户的真实来源，不描述成100条专家人工gold。

冻结：`6c215fa76fc64742af175be84c9f8a06`；标签指纹：`5798ba454a37bcbb978e9790e91bf33aea0c842e0b1396d6e49cbd8b13179a9a`。

BEIR 2.2.0/pytrec-eval-terrier基于已有真实本地排名计算；对compare/export入口设置模型加载失败守卫，确认没有重新编码/检索/重排。原运行中quality=null是创建时事实，原文件不改写，最新冻结成绩由新对照报告给出。

以下仅72条可回答测试题；8条无答案单列，20条开发题不混入。测试有8领域问题族，问题改述相互关联；没有配对置信区间，不能称统计显著或真实答案正确率。

| 指标 | A 原向量 | B 向量+BM25+RRF | C 同B池重排 |
| --- | ---: | ---: | ---: |
| Recall@10 | 0.87500 | 0.91667 | 0.91667 |
| nDCG@5 | 0.73702 | 0.80860 | 0.80037 |
| MRR@10 | 0.74919 | 0.82515 | 0.82818 |
| Hit@3 | 0.80556 | 0.88889 | 0.91667 |
| CompleteEvidenceHit@3 | 0.72222 | 0.83333 | 0.79167 |
| CandidateRecall@50 | 0.97917 | 1.00000 | 1.00000 |

B比A的Recall@10提高4.17个百分点，nDCG@5提高0.07159；C比B的Recall@10不变，nDCG@5降低0.00824。C的Hit@3提高2.78个百分点，但完整必要证据@3降低4.17个百分点：至少找到一段与找到所有必要事实是不同目标。

C救回@3：4/8=50%；退化@3：2/64=3.125%；B池无从救回0/72。失败0，成功不能排除排序退化。另有11条test的C-B nDCG@5退化、4条B-A退化；这些事件可重叠，不可相加当总错误率。

分类观察：32条术语/条件题C nDCG@5=1，32条语义改述C nDCG@5=.66301优于B .62434；但8条多证据题C nDCG@5=.55126低于B .82619，前三完整证据B5/8、C0/8。这是当前候选池及模型的观测结果，不推断模型内部原因；不能根据这些已知test案例调参再称同一独立测试。

具体失败可复查：
- `budget-4-q2`：摘要失败/版本发布问题的直接证据budget.md L17–L19，B排第3，C排第7，前三被其他写事务/快照规则占据。
- `release-multi-2-3`：两必要事实release.md L9–L11与L13–L15在B排1/3，C排1/19；至少一段命中不代表完整证据命中。
- `release-4-q2`：用户确认的证据修订段release.md L17–L19由B第1降到C第2；原文充分支持等级不因排名降低而修改。

测试分区28条诊断样例，dev8条；类型可重叠。完整JSON保存每条原文范围、等级、缺组及各方案指标。8无答案仅记录相似片段返回，没有评估生成拒答，不能宣布拒答正确率。

接入决定：现阶段正式问答保留A。B的当前质量与本机成本较有利，优先作为后续可选择、可回退的试用方案，并继续验证真实公开笔记；当前没有默认上线B。C纯重排P50/P95约1112.44/1366.15ms，额外耗时明显且整体排序/多证据质量未胜B，继续留在独立评测，不默认上线。该决定建立在此固定合成集上，不能泛化到所有Reranker或真实资料。

后续优先补充更多独立问题族/真实公开笔记并做完整问答链路验证；考虑按问题族配对区间、最终答案及权限拒答能力、实测问答端到端延迟。任何新策略使用开发/新版本集，不反改本次冻结标签。

[完整冻结对照](../../../.data/evaluations/retrieval-comparison-303339665061499da918c318faf34a04.md)；BEIR导出目录 `.data/evaluations/retrieval-frozen-6c215fa76fc64742af175be84c9f8a06-7b50f5d8`，含80资料片段、92可回答query、100标签与8负例元数据。
