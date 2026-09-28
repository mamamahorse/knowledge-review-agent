# Knowledge Review

基于个人技术资料的学习与复习项目。第一版范围见 [产品设计](docs/product-design-v1.md)。

当前完成阶段 A：多份 Markdown 问答、版本化来源标识，以及资料依据 / 分析推导 / 模型补充的区分。
阶段 B 已完成 Markdown 分块，以及本地 embedding 与语义检索预览。
文档向量现在自动保存在 SQLite 中，同一资料版本和配置可跨运行复用。
`--question` 使用全文问答，增加 `--rag` 则先固定检索一次，增加 `--agent` 则由模型在次数上限内选择搜索、读原文或结束；`--search` 只运行本地检索。尚未实现网页、练习与学习记录数据库。

开发 Agent 请先阅读 [AGENTS.md](AGENTS.md)，[agent.md](agent.md) 是同一份约定的入口。产品需求仍以产品设计文档为准。

## 1. 运行

在 Windows PowerShell 中进入项目目录：

```powershell
Set-Location 'E:\agentic project\knowledge-review'
```

使用已有 `.venv`。首次搭建环境时执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

如果还没有 `.env`，将 `.env.example` 复制为 `.env`；已有配置直接保留。

```dotenv
LLM_API_KEY=你自己的密钥
LLM_MODEL=你有可用额度的模型标识
LLM_BASE_URL=服务商提供的兼容接口地址
```

当前调用的是 OpenAI 兼容接口。使用 Gemini 官方接口时，地址为 `https://generativelanguage.googleapis.com/v1beta/openai/`。模型可用性和免费额度按账号实际配置确认。
配置只放在本地 `.env`，不要写入示例或源码。

### 本地预览

```powershell
.\.venv\Scripts\python.exe main.py
.\.venv\Scripts\python.exe main.py .\examples\project-notes.md .\examples\reading-goals.md
```

不传问题时只读取并显示文件，不发送模型请求。空参数使用默认示例；多个路径按顺序加载，相同路径只读取一次。
任一文件不存在、不是 Markdown、编码错误或内容为空时先退出，不会发送其他文件的内容。

### 默认问答：资料优先，允许补充

分块预览用以下命令，不需要模型密钥：

```powershell
.\.venv\Scripts\python.exe main.py .\examples\chunking-demo.md --chunks --chunk-size 120
```

`--chunk-size` 默认 1200，单位是字符，不是 token。它是软上限：代码块、表格、列表、引用块和单个长段落保持完整，章节标题与首个内容块也尽量放在一起；超过上限会显示 `exceeds soft limit`。
先在章节边界产生候选片段，再按 `--chunk-min` 合并过小的相邻片段。默认最小目标为 100 字符，设置为 0 则保留原来的章节边界；`--chunk-min` 不能大于 `--chunk-size`。
`--chunks` 不能与 `--question` 同用。分块用于 `--search` 和 `--question --rag`，普通 `--question` 保留全文问答，便于比较。

### 提问

```powershell
.\.venv\Scripts\python.exe main.py --question "这个项目的第一个版本做什么？"
.\.venv\Scripts\python.exe main.py --question "什么是向量检索？用一个简短例子解释。"
.\.venv\Scripts\python.exe main.py .\examples\project-notes.md .\examples\reading-goals.md --question "结合两份笔记，说说我的项目目标和阅读学习方式。"
```

资料没有涉及的通用知识可以显示为模型补充，但这不等于外部核验。
项目具体上线日期等私人事实不能由模型知识猜出；缺少这些信息时仍应说明无法确定。
以上普通问答命令会发送所选笔记全文；加上 `--rag` 则只发送命中片段及其来源、章节信息。两者的生成阶段都需要云端调用。

### 严格模式：只依据资料

```powershell
.\.venv\Scripts\python.exe main.py --strict --question "什么是向量检索？"
```

严格模式不允许模型补充。资料不足时显示缺口说明；允许基于资料本身的推导，但不能依靠外部假设补答案。
两种模式使用相同的来源验证规则，不会因为允许模型补充而允许伪造引用。

## 2. 本次的数据流

```text
命令行中的文件路径与问题
  → load_sources：读取本次资料快照
  → format_sources：组织文档标识和带行号的全文
  → answer_question：发送请求、解析结构
  → validate_answer：检查依据、来源和行号
  → render_answer：显示各段类型及对应来源
```

| 文件 | 职责 | 为什么拆开 |
| --- | --- | --- |
| main.py | 参数和执行流程 | 后续网页可复用其他模块 |
| sources.py | 文件读取、内容快照、来源标识、行号 | 以后检索和阅读器使用同一套来源 |
| chunking.py | 按 Markdown 结构分块、展示原文范围 | 为检索准备片段，避免切断代码和丢失引用位置 |
| embeddings.py | 本地模型加载、输入前缀、token 长度检查、向量化 | 把模型运行与排序逻辑分开，超长输入不静默截断 |
| embedding_config.py | 模型预设、自定义模型配置、可选 token 上限 | 切换模型时同时切换前缀约定，长度从模型自身读取 |
| retrieval.py | 余弦相似度排序、返回带来源的片段 | 当前资料量小时可直接计算，无需先引入向量数据库 |
| vector_index.py | SQLite 文档向量缓存、失效检查、事务写入 | 重复提问复用文档向量，改动资料或模型时避免误用旧数据 |
| knowledge_tools.py | `search_notes`、`read_source` 及严格参数调度 | 给未来 Agent 一个受程序控制的知识库接口 |
| agent_loop.py | 模型选择工具、执行、错误回传与调用预算 | 把模型决策和程序边界接成可追踪的循环 |
| context.py | 校验片段与资料快照一致、格式化上下文、检查可引用范围 | 阻止引用真实存在但没有发送给模型的行 |
| answers.py | 回答结构、业务校验、文字显示 | 校验无需联网，可单独验证 |
| model_client.py | 模型配置、请求、解析和服务异常 | 模型连接变化不必重写文件处理 |
| test_sources.py / test_answers.py | 来源与回答规则测试 | 覆盖内容变化、多资料、错误引用和模式约束 |
| test_model_client.py / test_cli.py | SDK 与入口测试 | 模拟 HTTP，不消耗模型额度；命令行测试禁用密钥 |
| test_support.py | 创建并清理测试文件夹 | 使用继承权限的目录，适应 Windows 测试环境 |

`read_note()` 和 `add_line_numbers()` 从原来的 main.py 移到了 sources.py，核心行为保留：UTF-8 读取、空行计数、原文件不被修改。

## 3. 为什么改变回答结构

旧结构只有一个回答和一组行号，无法清楚表达同一次回答中哪些内容依据资料、哪些属于补充。
新结构把整次资料覆盖情况与逐段依据分开：

```python
class Citation(StrictModel):
    source_id: str
    line_start: int
    line_end: int

class AnswerBlock(StrictModel):
    kind: Literal["grounded", "inference", "supplement"]
    text: str
    citations: list[Citation]

class NoteAnswer(StrictModel):
    coverage: Literal["sufficient", "partial", "none"]
    coverage_note: str
    blocks: list[AnswerBlock]
```

`StrictModel` 是项目里定义的 Pydantic 基类，开启严格类型检查并拒绝额外字段。
`coverage` 描述资料能否支撑本次问题，不代表模型置信度。`coverage_note` 说明缺少的资料。
`blocks` 按段标明依据，引用跟随具体段落，不把整份答案共用一个模糊的来源列表。
`line_start` 和 `line_end` 是包含两端的原文行号范围。

| 类型 | 作用 | 程序检查 |
| --- | --- | --- |
| grounded | 忠实转述资料 | 必须有有效引用 |
| inference | 基于资料前提分析 | 必须引用前提；提示词要求说明假设 |
| supplement | 资料外模型知识 | 不得附资料引用，严格模式拒绝此类型 |

有依据的覆盖状态必须有资料或推导区块。覆盖为 none 时不允许这两种区块；可给模型补充，也可只说明缺口。
该边界由结构检查约束，但模型对类型和覆盖情况的判断仍可能错误，不能只信标签。

## 4. 来源标识为何包含内容

单独的 `[L9]` 无法区分两个文件，也无法区分修改前后的第 9 行。
本次实现用文件的绝对路径和读取到的内容计算 SHA-256，得到 `source_id`。
同一文件内容不变时，即使输入顺序变化，ID 也不变；修改内容或移动文件后 ID 变化。
ID 表示“这个位置的这个内容版本”，不是永久不变的笔记身份，后续数据库会单独维护文档身份。

模型引用完整 ID；终端为方便阅读只展示其前 12 位，同时展示完整文件路径和行号范围。
两个相同文件名、不同目录的资料仍能区分。路径没有完整发送给模型，模型只收到文件名、ID 和内容。

资料快照暂时只保存在本次运行内存中，尚未存档旧版本，也未实现重启后查看历史引用。

## 5. 三层约束各做什么

1. 提示词：要求模型标明推导假设，不编造私人事实，笔记中的文字不应当改变系统规则。
2. 结构化输出：向服务提交 Pydantic 结构，并将返回 JSON 解析为对象。
3. 业务校验：检查非空内容、覆盖状态、模式权限、来源版本和行号范围。

结构化调用沿用 Gemini 官方兼容接口的 `parse(..., response_format=NoteAnswer)` 用法：
https://ai.google.dev/gemini-api/docs/openai#structured-output

部分 OpenAI 兼容服务会在某些模型或时段拒绝 `response_format`。遇到明确的“response_format unavailable/unsupported”错误时，程序会自动重试一次普通 Chat Completions 请求，并要求模型只返回同一份 JSON 契约；随后仍由 Pydantic 和项目的来源、行号、严格模式校验决定是否接受。其他 API 错误不会被重试或伪装成结构化回答。

校验通过不代表解释一定正确、引用一定支持结论、文档指令一定不会影响模型。
程序不联网核验，资料与模型知识冲突仍需要人判断。

## 6. 验证与本次学习重点

运行不联网的自动检查：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
```

真实问答用上面的三类命令验证：资料中存在答案、资料外通用问题、严格模式中的相同问题。
关注模型是否选择了正确的回答区块，以及引用回查后是否真的支持对应结论。

本次重点：让资料成为有身份的数据对象，让回答成为有明确依据的区块，而不只是把几份文件拼成一大段字符串。
检索已接入回答流程，并约束模型只引用实际收到的原文范围；文档向量也已持久化。下一步将检索与读取能力封装为 Agent 工具。

## 7. 分块为何这样设计

新增的本地数据流是 `load_sources → chunk_source → format_chunks`。
`Source` 代表整份文件的内容版本，`Chunk` 代表该版本中的一段连续原文；以后 embedding 是这段文字的向量表示，不替代原文。

- `source_id` 将片段关联到资料快照；`chunk_id` 由来源版本、算法版本和原文范围计算，同一片段重复运行时身份稳定。
- `section_paths` 保存片段涉及的全部章节路径，`heading_path` 保存它们的共同祖先。合并兄弟小节后不会冒充只属于其中一个小节；原文行号仍能进一步定位具体标题。
- `line_start` / `line_end` 是从 1 开始、包含两端的原文行号。显示时延续原文行号，避免每个片段都从 L1 开始。
- `text` 从原文按行截取，保留 Markdown。只清除片段边缘的空白行，不改写正文。

这里使用 markdown-it-py 识别外层 Markdown 块与行范围，而不是只查找以 `#` 开头的行，因此代码里的注释不会被当成章节。外层列表、引用和表格作为整体处理。解析器未单独输出的链接定义也通过连续原文范围保留下来。
标题文本保留 Markdown 行内语法；暂不专门识别 Obsidian 链接或 YAML 元数据。

软上限用于先验证结构切分。现在 embedding 会检查 token 数，超长时明确报错而不是静默截断。可先降低 `--chunk-size`；单个超长段落或代码块仍需进一步拆分。当前没有自动拆分超长块。

## 8. 本地 embedding 与检索

安装额外依赖，然后首次下载模型：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-local.txt
.\.venv\Scripts\python.exe main.py --download-model
```

默认使用 `intfloat/multilingual-e5-small`，在 CPU 上运行，输出 384 维向量。也支持通过下文的模型配置接口选择其他本地 SentenceTransformer 模型。依赖和模型首次需要联网下载，模型保存在项目 `.models/`，默认模型权重约 470 MB，运行库另占空间。下载后普通 `--search` 仅加载本地模型文件，不调用对话模型，不需要 `.env` 密钥。下载阶段如果访问 Hugging Face 失败，需检查终端网络或代理；程序不会自行更改系统代理。

运行检索预览：

```powershell
.\.venv\Scripts\python.exe main.py .\examples\retrieval-demo.md --search "两个线程各拿着一个资源等对方释放，为什么会一直卡住？" --top-k 2
.\.venv\Scripts\python.exe main.py .\examples\project-notes.md .\examples\reading-goals.md --search "我希望如何练习和复习？" --top-k 3
```

数据流：

```text
Markdown → Source → Chunk → 章节路径 + 原文 → 文档向量
问题 → 问题向量
问题向量与每个文档向量 → 余弦相似度 → 排序取前 K 个 → 显示原文与行号
```

默认 E5 模型要求文档加 `passage: ` 前缀、问题加 `query: ` 前缀，中英文都一样。章节路径给独立片段补充语境，例如“操作系统 > 死锁”；不会把它写回 `Chunk.text`，引用仍然对应原文。E5 small 的配置上限为 512 token，程序检查时把前缀、章节和特殊 token 都计算在内。其他模型使用各自的配置与 tokenizer。依据见 [E5 官方模型说明](https://huggingface.co/intfloat/multilingual-e5-small)。

`rank_chunks()` 将向量归一化再计算点积，也就是余弦相似度。它比较向量方向，因此较长向量不会仅凭数值更大占优势。返回分数用于当前候选的相对排序，不是“答案正确的概率”。即使所有资料都不相关，前 K 项仍会存在，不能把检索命中当成资料足以回答。

搜索会读取并分块本次所选资料，然后复用 SQLite 中匹配的文档向量，只计算缺失项；每次仍重新计算问题向量。修改一份文件会使该文件的所有片段进入新来源版本，其他文件的向量可继续复用。尚未实现知识库目录管理、混合检索和重排序。

### 标题很多时怎么办

现在先按 Markdown 块和章节形成候选片段，再从左到右合并：前一个结果或当前片段小于 `min_chars`，且合并后的连续原文不超过 `max_chars`，就合并两者。结尾的小片段也可以向前合并。两个都已达到最小目标的片段保留边界。

例如相邻片段长 30、40、120 字符，`min_chars=100`、`max_chars=300` 时，会合成一个约 190 字符的片段（还要计入原文空行）。如果最大值为 150，前两个可以合并，但不能再并入第三个，最终允许保留一个小于 100 的片段。

最小值是软目标：不丢弃短文，不填充文字，不为凑长度跨文件合并，也不因最小值切断完整代码块。原文仍按连续行范围截取，所有涉及的章节保存在 `section_paths`。这一版算法标识更新为 `markdown-blocks-v2`，旧片段 ID 不复用。

```powershell
.\.venv\Scripts\python.exe main.py .\examples\chunking-demo.md --chunks --chunk-min 100 --chunk-size 400
.\.venv\Scripts\python.exe main.py .\examples\chunking-demo.md --chunks --chunk-min 0 --chunk-size 400
```

上面两条分别开启和关闭合并，可以直观看到片段边界变化。字符合并不保证满足 token 上限：中文、英文和代码的 token 数不同，最终仍由所选模型的 tokenizer 检查。超长时给出原文范围，不静默截断。自动按 token 重切超长内容尚未实现。

先用检索预览观察哪些问题找错、哪些命中缺少上下文，再决定策略。较大的片段也可能混入多个主题，因此不能仅凭大小判断检索好坏。

### 选择 embedding 模型

`EmbeddingConfig` 集中保存模型名称或本地路径、问题前缀、资料前缀、可选 token 上限。`LocalEmbedder` 接收该配置，检索排序模块不依赖某个具体模型。

| 预设 | 模型 | 输入约定 | 配置中的常见上限 |
| --- | --- | --- | --- |
| `e5-small`（默认） | `intfloat/multilingual-e5-small` | `query: ` / `passage: ` | 512 token |
| `multilingual-minilm` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | 无前缀 | 128 token |

第二个模型的用法及上限见 [MiniLM 官方模型说明](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2)。表中的数值用于说明，程序不靠这张表硬编码上限，而是读取实际加载模型的 `max_seq_length`。不同模型的向量维度也由实际输出决定。

```powershell
# 使用已下载的默认模型，把本次上限主动降低到 256 token
.\.venv\Scripts\python.exe main.py .\examples\retrieval-demo.md --search "线程为何互相等待？" --embedding-model e5-small --embedding-max-tokens 256 --chunk-min 100 --chunk-size 200

# 切换前先下载所选模型；此命令会额外下载 MiniLM
.\.venv\Scripts\python.exe main.py --download-model --embedding-model multilingual-minilm
.\.venv\Scripts\python.exe main.py .\examples\retrieval-demo.md --search "线程为何互相等待？" --embedding-model multilingual-minilm --chunk-min 0 --chunk-size 100
```

不传 `--embedding-max-tokens` 就使用所选模型配置的上限；传入时只能降低，不能借此扩大模型支持长度。检查与实际编码使用同一个有效上限。每次搜索用同一实例为问题与资料编码，避免混用不同模型的向量。

其他兼容 SentenceTransformer 的模型可直接传入模型名称或本地文件夹，但必须显式指定其输入前缀。例如已了解该模型不需要前缀时，使用 `--query-prefix= --passage-prefix=`。需要特殊 pooling、非文本输入或自定义远程代码的模型不保证兼容此接口。

### 验证重点

`test_embeddings.py` 检查前缀、章节上下文、本地加载开关与超长拒绝；`test_retrieval.py` 用已知向量验证排序、维度与非法向量；`test_cli.py` 检查检索入口不会调用生成模型。这些自动测试不下载模型，也不调用云端服务。真实检索效果需要另外用本地模型检查。

多模型配置与最小长度合并更新后，38 项自动测试通过，包含跨小节合并、短尾段、合并上限、代码块完整性、不同模型配置的长度读取和前缀切换。真实 E5 以 256 token 上限、100/200 字符分块完成检索，命中包含死锁解释的片段；同一份资料按 100/400 合并时产生 258 token 输入，被正确拒绝。MiniLM 预设已验证配置与调用传递，尚未下载并进行真实推理验证。

2026-09-12 初版检索验证（尚未开启最小长度合并，可用 `--chunk-min 0` 复现其切法）：30 项自动测试通过，依赖检查通过。真实本地模型在禁止 socket 网络连接的条件下，以下 4 个小样例均将预期小节排在第 1 位：

| 问题 | 预期小节 |
| --- | --- |
| Why do two threads wait forever for each other? | 并发中的死锁 |
| 如何减少数据库查询时扫描的数据？ | 数据库索引 |
| 为什么读到的数据还是修改前的旧值？ | 缓存与过期 |
| 修改代码后怎么确认原先功能没有坏？ | 单元测试 |

这只证明示例上的基本检索可用，不代表真实知识库质量。中文死锁问题的第 1 项得分约 0.9035，但不相关的“缓存与过期”仍能排第 2、得分约 0.8568。这说明高相似度分数不保证相关，后续问答必须检查检索内容是否足够支持答案。

## 9. 检索问答：把找到的资料交给模型

```powershell
.\.venv\Scripts\python.exe main.py .\examples\retrieval-demo.md --question "两个线程为什么会互相等待？资料提到了什么避免方法？" --rag --strict --chunk-min 100 --chunk-size 200 --top-k 1
```

`--rag` 必须与 `--question` 一起使用。程序先显示命中片段，再使用已有 `.env` 配置调用对话模型。默认允许明确标注的模型知识补充，加 `--strict` 时仅根据命中资料回答。`--top-k` 控制送给模型的片段数量；embedding 模型、长度与分块选项与 `--search` 相同。

```text
Source → 分块 → 本地 embedding → 相似度排序 → top-k Chunk
  → format_context：只组织这些片段及原文行号
  → answer_question：发送片段和问题、解析 NoteAnswer
  → validate_answer：检查回答类型与本次可引用范围
  → render_answer：显示答案与来源
```

为什么还要新增 `context.py`：来源验证只保证“这个文件确实有这一行”，检索问答还需要保证“这一行确实发送给了模型”。例如只发送 L2-L3 和 L5，引用 L2-L5 应被拒绝，因为 L4 没有发送；相邻片段完整覆盖一个范围时则允许引用该范围。

`answer_question(..., chunks=None)` 明确表示全文；`chunks=[]` 表示检索结果为空，不会自动退回全文。生成请求之前检查片段正文与本次 `Source` 快照是否一致，拒绝旧版本、错误行号或改写后的片段冒充原文。全部 Source 留在本地用于验证，未命中正文不会因此被加入请求。

检索不到足够依据时，提示词要求说明“本次检索片段不足”，不能宣称整份知识库都没有答案。相似度分数不作为证据发送，模型仍需判断片段是否相关。范围校验只能检查来源边界，不能证明引用在语义上支持结论。

这一步是固定的 RAG 工作流：检索一次、生成一次。模型尚不能主动改写查询、继续检索或读取相邻内容，因此还不是完整 Agent 工具循环。文档向量已持久化，embedding 超长输入仍会报错；不会因检索失败自动改用全文发送。

自动检查覆盖真实 SDK 的模拟 HTTP 请求：未命中正文不发送、原文行号不重编、越界与跨空缺引用拒绝、空检索不回退、严格模式不允许补充、检索失败不调用生成模型。

本步骤验证：47 项自动测试通过。使用已下载的本地 E5 与已有云端生成配置，运行上面的严格模式命令成功：检索发送 L1-L9，答案说明互相等待与统一资源获取顺序，并引用 L5-L5。回答主体为中文，模型仍在覆盖说明字段输出了英文；引用范围校验不会检测这种语言一致性问题。此真实案例验证了基本链路，不代表对所有问题的事实正确性或检索质量保证。

## 10. 保存与复用文档向量

搜索和 RAG 默认把文档向量保存在项目的 `.data/embeddings.sqlite3`，无需安装数据库服务。SQLite 负责存储；相似度仍由 `retrieval.py` 对本次选中文件的片段逐一计算，这不是近似最近邻索引或独立向量数据库服务。

连续运行两次同一命令：

```powershell
.\.venv\Scripts\python.exe main.py .\examples\retrieval-demo.md --search "两个线程为何互相等待？" --chunk-min 100 --chunk-size 200 --top-k 1
```

首次没有对应缓存时显示 `Index: reused 0, computed 2 document vectors.`；再次运行显示 `Index: reused 2, computed 0 document vectors.`。换一个问题也可以复用这些文档向量，因为问题向量与文档向量分别计算。

```text
本次文件 → 来源版本 → 分块 → 查找对应配置下的向量
                              ├─ 匹配且完整：复用
                              └─ 缺失或损坏：计算 → 事务保存
问题 → 问题向量 → 与本次片段排序 → 预览或 RAG
```

两个控制选项都只用于搜索或 RAG，不能同时使用：

- `--rebuild-index`：重新计算当前资料、当前配置的文档向量，成功后替换相应记录。不会删除其他文件或配置的缓存。
- `--no-cache`：不读取、不写入数据库，本次直接计算所有文档向量。适用于比较结果或排查缓存问题。

为什么不只保存一个向量列表：必须知道每个向量对应什么资料、什么片段、什么模型。同维度的两个模型产生的向量也不能混用。

`profiles` 表保存配置，`vectors` 表按配置 ID 和片段 ID 保存向量、来源版本、行号、输入摘要与校验和。配置 ID 包含模型配置、有效 token 上限、实际权重与 tokenizer 的 SHA-256 指纹、运行库版本、向量维度，以及分块算法版本和最小/最大字符数。片段实际输入也单独计算摘要，包含章节上下文和资料前缀。

因此模型权重、分词器、输入约定、长度或分块配置变化时不会混用向量；文件内容变化时会重算该文件的全部片段，未改文件可以复用。不再选择某个文件时，数据库里它的旧向量不会进入本次检索。数据库暂不清理历史缓存，也不保存旧版原文，不能用它恢复历史引用或自动发现知识库文件。

单行向量校验和错误、维度错误、NaN 或零向量会当作缓存未命中重算；整个数据库无法读取时明确报错，可用 `--no-cache` 继续本地搜索。重算完成且向量有效之后才开始一次事务写入，失败不会留下半次更新。

每次运行仍需加载模型、计算问题向量，以及扫描权重生成模型指纹；缓存节省的是文档 embedding 推理，不保证短小示例的总启动时间明显缩短。指纹每个模型实例计算一次，当前模型实例按只读推理使用。自定义慢速 tokenizer 暂不支持持久化指纹，可使用 `--no-cache`。

`.data/` 已加入忽略列表，普通预览不创建数据库。使用 Python 标准库 SQLite，无额外依赖。

2026-09-16 验证：完整套件 55 项通过；补充数据库中途写入失败回滚用例后，18 项索引与入口专项检查通过，依赖检查通过。真实 E5 在两次独立进程中分别报告“复用 0、计算 2”和“复用 2、计算 0”，检索第一名及分数一致（约 0.8951）。测试还覆盖修改单份文件、配置隔离、输入次序变化、损坏行重算、重建失败保留旧数据、跳过缓存和 RAG 参数传递。本步骤没有再次调用云端生成模型。

## 11. Agent 可调用的知识工具

固定 RAG 原来把分块、模型加载、缓存和排序直接组织在 `main.py`。现在这些步骤由 `KnowledgeTools` 统一提供，命令行的 `--search` 与 `--rag` 也调用同一实现，避免未来 Agent 和现有命令出现两套搜索行为。

第一项工具是：

```python
search_notes(query: str, top_k: int = 3)
```

它只搜索构造 `KnowledgeTools` 时提供的 `Source` 快照，`top_k` 限制为 1 到 10。结果包含原文、完整来源 ID、文件名、章节和原始行号；相似度只用于排序，并附带“不代表答案置信度”的提示。一个工具实例第一次搜索准备文档向量，模型以后改写查询再次搜索时，只需生成新的问题向量，文档向量直接从当前内存复用。

第二项工具是：

```python
read_source(source_id: str, line_start: int, line_end: int)
```

它用于搜索后读取更准确或更完整的上下文。只接受当前加载资料版本的完整 `source_id`，不能使用文件路径、简称或旧版本 ID；行号包含两端，并且每次最多读取 200 行。即使磁盘文件随后发生变化，本次工具仍读取构造时的不可变快照，防止一次 Agent 运行中的来源悄悄改变。

工具调用统一经过：

```python
execution = tools.execute("search_notes", {"query": "死锁如何避免？", "top_k": 3})
```

`execute()` 拒绝未知工具、缺少参数和额外参数。返回值分为：

- `output`：只含 JSON 可序列化数据，之后作为工具结果发回模型；不会暴露向量。
- `visible_chunks`：程序内部保存的原文片段，用来累计“模型实际看过哪些行”，最终回答只能引用这些范围。

`knowledge_tool_definitions()` 提供与实际参数边界一致的、服务商无关的 JSON Schema。下一步接模型工具调用时，由模型客户端把它包装成 Gemini/OpenAI 兼容接口需要的格式；工具实现本身不依赖某个对话 API。

本步骤没有实现模型决策循环。目前模型还不会主动选择 `search_notes`、判断结果不足后调用 `read_source`，也不会自行改写查询。完成的是工具和权限边界，使下一步 Agent 循环只负责编排，不再重复实现检索。

2026-09-22 验证：完整套件 63 项通过，依赖检查通过。新增测试覆盖同一实例的重复搜索、工具输出不泄露向量、完整来源 ID、快照稳定性、读取范围与 200 行限制、额外参数拒绝及工具 Schema。真实本地 E5 通过现有 `--search` 入口命中死锁片段，复用 2 个缓存向量、计算 0 个文档向量；没有调用云端生成模型。

## 12. 有界问答 Agent 循环

```powershell
.\.venv\Scripts\python.exe main.py .\examples\project-notes.md --question "第一个版本为什么先处理 Markdown？" --agent
.\.venv\Scripts\python.exe main.py .\examples\project-notes.md --question "笔记里如何定义第一版？" --agent --strict --max-tool-calls 4
```

`--agent` 与固定 `--rag` 互斥，默认最多执行 4 次知识工具调用，可用 `--max-tool-calls` 设置为 1–6。模型每轮看到问题、当前资料的文件名与来源 ID，以及此前工具结果；它可以调用 `search_notes`、`read_source`、改写查询再搜索，或直接结束取证。程序执行工具并把结果或参数错误交回下一轮模型。达到预算后停止工具阶段，终端显示实际工具步骤、错误摘要、停止原因和已见片段数。

结束取证后，现有结构化回答通道只收到成功工具返回过的原文片段，并再次校验引用是否落在可见行号内。资料目录只用于导航，不自动获得引用权限；工具调用失败也不会把原文加入可引用范围。若模型无需调用工具，严格模式只能说明资料证据不足；默认模式仍可给明确标注的通用知识补充。预算限制的是工具调用次数，最终结构化回答另需一次云端模型请求；搜索仍使用本地 embedding 与 SQLite 向量缓存。

这一步完成的是问答任务的工具决策循环，不包含出题、收取真实作答、保存学习状态或复习。也尚未实现跨运行的 Agent 记录持久化；当前步骤只在终端显示。自动测试使用模拟 HTTP，不消耗云端额度。

2026-09-24 验证：完整套件 68 项通过，依赖检查通过。使用现有云端配置执行 `--agent --strict --max-tool-calls 2` 提问“这个项目的第一个版本做什么？”；模型自主调用 `read_source` 读取示例笔记 L1–L16，随后结束取证，结构化答案引用 L9–L10。此案例确认兼容工具调用链路可用，不代表各种问题下的工具选择和回答质量已经得到全面评估。
