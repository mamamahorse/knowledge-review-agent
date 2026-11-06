# LearnTrace

[English](README.md) | [简体中文](README.zh-CN.md)

A personal learning agent that turns Markdown notes and PDFs into source-grounded conversations, learning objectives, and practice with spaced repetition.

## Key features

- **Ask your documents:** an agent chooses when to search or read, with BM25 + dense retrieval and traceable citations.
- **Read PDFs visually:** extract text and tables, inspect pages, and zoom into regions with an optional vision-capable model.
- **Learn through practice:** generate learning objectives and questions, receive feedback, and schedule reviews with FSRS.
- **Continue where you left off:** persist conversations, document snapshots, practice records, and resumable outline tasks in SQLite.
- **Controlled execution:** stream answers over SSE, enforce request budgets and concurrency limits, and support cancellation.

Built with Python, FastAPI, SQLite, Sentence Transformers, and an OpenAI-compatible model API. A separate C++ gateway is optional.

## Get started

Clone the repository and configure your model:

```sh
git clone https://github.com/mamamahorse/knowledge-review-agent.git
cd knowledge-review-agent/knowledge-review
cp .env.example .env
```

Edit `.env`: set `LLM_API_KEY`, `LLM_MODEL`, and your provider's `LLM_BASE_URL`. PDF image inspection requires a vision-capable endpoint; optional `VLM_*` settings override the chat connection.

### Docker

Requires Docker with Compose. Build the image, download the local retrieval model once, then start:

```sh
docker compose build
docker compose run --rm app python main.py --download-model
docker compose up -d
```

Open **http://localhost:8767**. Import a document, ask a question, or generate a learning outline.

Data and retrieval models persist in the `knowledge-data` and `knowledge-models` Compose volumes. `docker compose down` keeps them. The initial build/model download needs internet access; generation uses your configured provider.

### Local Python

Requires Python 3.12 or 3.13:

```sh
python -m venv .venv
# Activate: source .venv/bin/activate
# PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-local.txt
python main.py --download-model
python -m uvicorn web_app:app --host 127.0.0.1 --port 8767
```

Local data is stored in `.data/`, and downloaded models in `.models/`.

## Project status

Actively developed. The experimental single-document path now connects autonomous reading, formal objectives, automatic review, and selected-goal practice; it has controlled tests, not live-model quality validation. V1/V2 defaults are unchanged; the older whole-outline review draft retains known test failures. Large-file and multimodal improvements continue. Docker Compose is valid; image build/startup remain unverified. The default port is localhost only.

## Documentation

- [Architecture](knowledge-review/docs/project-context/ARCHITECTURE.md) · [Call chains](knowledge-review/docs/project-context/CALL_CHAINS.md)
- [Code study handbook](knowledge-review/docs/project-learning-handbook.md)
- [Autonomous learning flow and limits](knowledge-review/docs/learning-planner.md)
- [Development log](knowledge-review/docs/development-log.md) · [Historical README](knowledge-review/README.legacy.md)
