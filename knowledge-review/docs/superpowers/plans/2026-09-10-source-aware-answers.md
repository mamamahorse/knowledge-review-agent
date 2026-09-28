# 阶段 A：多资料与回答依据 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 命令行同时读取多份 Markdown，默认允许标注模型补充，并在逐段引用中校验资料版本和行号。

**Architecture:** `sources.py` 管理不可变的本次资料快照；`answers.py` 定义输出结构、校验和呈现；`model_client.py` 只负责请求及解析；`main.py` 组织入口。所有资料仍完整发送，不在本阶段引入检索或 Agent 循环。

**Tech Stack:** Python 标准库、现有 OpenAI 兼容客户端、Pydantic、unittest。

工作目录：`E:\agentic project\knowledge-review`。现有目录没有 Git 仓库，直接在用户当前学习项目中开发，不创建工作树或初始化仓库。

## Task 1：资料快照

- [x] 在 `test_sources.py` 写用临时 Markdown 验证内容、路径、顺序和版本变化的测试，运行并确认失败。
- [x] 新增 `sources.py`：`load_sources(paths: list[Path]) -> list[Source]`、`format_sources(sources) -> str`；资料快照包括 `source_id, path, text`。
- [x] ID 计算使用 `sha256((str(resolved_path) + '\0' + text).encode('utf-8')).hexdigest()`；同一文件同一内容不随输入顺序变化，路径或内容变化时 ID 变化。重复路径去重，拒绝空文件、非 Markdown 和缺失文件。
- [x] 输入文本每行标记 `[L数字]`，文档头单独给出 ID；运行测试确认通过。

## Task 2：逐段回答契约

- [x] 在 `test_answers.py` 验证三种区块、严格模式、未知来源、越界/倒置范围、空文本、覆盖状态矛盾，确认新功能测试失败。
- [x] 新增 `answers.py`：`Citation(source_id, line_start, line_end)`、`AnswerBlock(kind, text, citations)`、`NoteAnswer(coverage, coverage_note, blocks)`；所有对象 Pydantic strict 且 extra forbid。
- [x] `kind` 为 `grounded / inference / supplement`，`coverage` 为 `sufficient / partial / none`。`validate_answer(result, sources, strict=False)` 验证引用范围及状态；模型补充不带资料引用，资料和推导区块必须有引用，缺少资料时说明缺口，严格模式禁止补充。
- [x] `render_answer(result, sources)` 显示中文类型标签、资料覆盖情况、逐段来源文件/版本/行号。无资料严格回答允许 blocks 为空并显示缺口；其他情况不能为空。
- [x] 运行纯本地验证确认通过。范围检查不能证明语义正确，不宣称可以检测所有伪引用。

## Task 3：接入请求与入口

- [x] 更新 `test_model_client.py`，使用真实 SDK 加模拟 HTTP 响应，检查请求包含多份资料、新结构和模式要求，确认旧实现失败。
- [x] `answer_question(sources, question, strict=False) -> NoteAnswer` 接收快照，要求只在 citations 字段引用，私人事实（如项目上线日期）不能根据模型知识编造；拒答、空响应、截断及结构错误返回清楚异常。
- [x] `main.py` 使用 `note` 的 `nargs='*'` 和 `--strict`；空参数仍预览现有示例；任一文件失败先退出，不发送模型请求。输出使用统一 renderer。
- [x] 命令行子进程测试覆盖默认/多文件预览、缺失文件、空白问题及严格选项。
- [x] 运行 `.\.venv\Scripts\python.exe -m unittest discover -v`，所有检查通过。

## Task 4：真实验证和学习说明

- [x] 使用当前模型验证资料内问题、资料外通用知识、同一问题严格模式；失败时先保留诊断，不因额度问题无限重试。
- [x] 更新 README，解释数据流、引用版本边界、完整资料输入的限制及两种运行模式；附两文件命令。
- [x] 检查变更与阶段 A 范围一致，记录结果。现阶段资料快照只在本次运行内存中，不宣称重启后可查看旧版本。

完成标准：默认允许清楚标识的模型补充，严格模式可拒绝资料外回答，多资料引用可核对文件和原始行；不修改用户笔记、密钥和模型选择。

## 执行记录

- 本地 `unittest discover -v`：14 项测试通过，含引用矛盾的多个子案例。
- 真实模型：资料内问答、模型补充、严格模式资料不足、多资料问答四个场景均正常退出；人工核对多资料引用对应原文。
- 新增 `examples/reading-goals.md` 用于多资料演示；原有用户示例未修改。
- Windows 受限运行环境下，`tempfile` 创建的目录权限导致测试文件访问失败。通过最小文件读写实验确认普通继承权限目录可用，测试改用 `test_support.py`，并清理本次生成的工作区临时目录。
- 在覆盖说明里发现未校验的行号标记，先新增失败案例，再补校验，完整回归通过。
- 当前没有 Git 仓库，未创建提交、分支或 PR；没有新增依赖，也没有更改模型配置。
- 模型正文有一次输出字面量 `\n`，当前按原样呈现，不在程序中全局替换，避免破坏代码示例里的转义符。回答排版可后续单独改进。
- 独立代码审查未发现阻塞性正确性问题。低优先级测试补充项：CLI 提问时严格参数的转发、服务拒答/空响应分支、相同内容不同路径的身份断言；当前实现已处理这些行为，严格模式也已通过真实命令验证。

