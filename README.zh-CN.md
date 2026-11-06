# LearnTrace

[English](README.md) | [简体中文](README.zh-CN.md)

一个面向个人资料学习的 AI Agent：将 Markdown 笔记和 PDF 转化为有来源依据的问答、学习目标和练习，并通过间隔复习持续巩固。

## 核心功能

- **资料问答**：Agent 按需搜索、读取原文，支持 BM25＋向量混合检索和可追溯引用。
- **PDF 视觉阅读**：提取文本与表格，使用支持视觉的模型查看整页、放大局部区域。
- **练习与复习**：生成学习目标和题目，提供作答反馈，使用 FSRS 安排复习。
- **持久化与恢复**：使用 SQLite 保存会话、文档快照、练习记录和可恢复的清单任务。
- **执行治理**：SSE 流式回答、请求预算、并发准入和取消支持。

技术栈：Python、FastAPI、SQLite、Sentence Transformers、OpenAI 兼容模型 API。可选搭配独立 C++ 网关。

## 快速开始

克隆仓库并配置模型：

```sh
git clone https://github.com/mamamahorse/knowledge-review-agent.git
cd knowledge-review-agent/knowledge-review
cp .env.example .env
```

编辑 `.env`，填写 `LLM_API_KEY`、`LLM_MODEL` 和供应商的 `LLM_BASE_URL`。PDF 看图需要支持视觉的端点；可用 `VLM_*` 单独配置视觉模型，留空则复用聊天连接。

### Docker 部署

安装 Docker 和 Compose 后，构建镜像，首次下载本地检索模型，再启动服务：

```sh
docker compose build
docker compose run --rm app python main.py --download-model
docker compose up -d
```

打开 **http://localhost:8767**，导入资料，即可问答或生成学习清单。

资料与模型分别保存在 Compose 的 `knowledge-data`、`knowledge-models` 卷中，`docker compose down` 会保留数据。首次构建和模型下载需要联网，生成请求使用你配置的模型服务。

### 本地 Python 运行

需要 Python 3.12 或 3.13：

```sh
python -m venv .venv
# 激活环境：source .venv/bin/activate
# PowerShell：.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-local.txt
python main.py --download-model
python -m uvicorn web_app:app --host 127.0.0.1 --port 8767
```

本地资料和记录保存在 `.data/`，模型保存在 `.models/`。

## 项目状态

持续开发中。单文档实验路径已串联自主阅读、正式目标、自动复查及选目标出题，通过受控检查，尚未验证真实模型质量。V1/V2 默认入口保持；旧整体复查草稿仍有已知测试失败。大文件与多模态改造仍在继续。Docker Compose 配置有效，镜像构建与启动尚未验证。默认端口仅绑定本机。

## 文档

- [架构](knowledge-review/docs/project-context/ARCHITECTURE.md) · [调用链](knowledge-review/docs/project-context/CALL_CHAINS.md)
- [源码学习手册](knowledge-review/docs/project-learning-handbook.md)
- [自主学习链路与限制](knowledge-review/docs/learning-planner.md)
- [开发记录](knowledge-review/docs/development-log.md) · [历史 README](knowledge-review/README.legacy.md)
