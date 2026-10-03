"""Local Markdown reading and source-aware question workspace."""

import logging
import re
from dataclasses import dataclass
from html import escape
from pathlib import Path
from uuid import uuid4
from typing import Literal

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field

from agent_loop import document_focus, run_agent
from embedding_config import select_embedding_config
from knowledge_tools import KnowledgeTools
from practice_service import grade_pending_answer, start_practice
from practice_store import DEFAULT_PRACTICE_DB, PracticeStore, public_session
from learning_store import LearningStore
from learning_types import Assessment, Capability, ReviewRating
from learning_service import enroll_question, start_review, add_dimension, correct_grade
from response_preferences import ResponseLanguage
from sources import Source, load_sources

ROOT = Path(__file__).resolve().parent
DEFAULT_NOTES_DIR = ROOT / ".data" / "notes"
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_SCOPE = 20
UPLOAD_ID = re.compile(r"upload-([0-9a-f]{32})\Z")
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Document:
    document_id: str
    path: Path
    name: str


class DocumentCatalog:
    def __init__(self, notes_dir: Path):
        self.notes_dir = notes_dir

    def list(self) -> list[Document]:
        examples = [
            Document(f"example-{path.stem}", path, path.name)
            for path in sorted((ROOT / "examples").glob("*.md"))
        ]
        uploads = []
        if self.notes_dir.exists():
            for folder in sorted(self.notes_dir.iterdir()):
                if not folder.is_dir() or not re.fullmatch(r"[0-9a-f]{32}", folder.name):
                    continue
                files = list(folder.glob("*.md"))
                if len(files) == 1 and files[0].is_file():
                    uploads.append(Document(f"upload-{folder.name}", files[0], files[0].name))
        return examples + uploads

    def get(self, document_id: str) -> Document:
        match = UPLOAD_ID.fullmatch(document_id)
        if match:
            folder = self.notes_dir / match.group(1)
            files = list(folder.glob("*.md")) if folder.is_dir() else []
            if len(files) == 1 and files[0].is_file():
                return Document(document_id, files[0], files[0].name)
        elif document_id.startswith("example-"):
            stem = document_id.removeprefix("example-")
            if re.fullmatch(r"[A-Za-z0-9_-]+", stem):
                path = ROOT / "examples" / f"{stem}.md"
                if path.is_file():
                    return Document(document_id, path, path.name)
        raise HTTPException(status_code=404, detail="找不到这份笔记。")

    @staticmethod
    def prepare_import(filename: str, content: bytes) -> tuple[str, str]:
        original = filename.replace("\\", "/").split("/")[-1]
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", original).strip(" .")
        if not safe_name.lower().endswith(".md") or not safe_name or len(safe_name) > 120:
            raise ValueError("请选择文件名不超过 120 个字符的 Markdown (.md) 文件。")
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValueError("单份笔记不能超过 2 MB。")
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("笔记必须使用 UTF-8 编码。") from exc
        if not text.strip():
            raise ValueError("不能导入空笔记。")
        return safe_name, text

    def import_note(self, safe_name: str, text: str) -> Document:
        folder_id = uuid4().hex
        folder = self.notes_dir / folder_id
        folder.mkdir(parents=True)
        path = folder / safe_name
        path.write_text(text, encoding="utf-8")
        return Document(f"upload-{folder_id}", path, safe_name)


def render_markdown(text: str) -> str:
    """Annotate rendered blocks with the original inclusive line range."""
    md = MarkdownIt("commonmark", {"html": False})
    md.enable("table")
    tokens = md.parse(text)
    for token in tokens:
        if token.map and token.type in {"heading_open", "paragraph_open", "list_item_open", "table_open", "hr"}:
            token.attrSet("data-line-start", str(token.map[0] + 1))
            token.attrSet("data-line-end", str(token.map[1]))

    def code_block(renderer, tokens, index, options, env):
        token = tokens[index]
        start, end = token.map
        language = token.info.strip().split(" ", 1)[0] if token.type == "fence" else ""
        label = f'<span class="code-language">{escape(language)}</span>' if language else ""
        return (
            f'<div class="code-block" data-line-start="{start + 1}" data-line-end="{end}">'
            f'{label}<pre><code>{escape(token.content)}</code></pre></div>\n'
        )

    md.add_render_rule("fence", code_block)
    md.add_render_rule("code_block", code_block)
    return md.renderer.render(tokens, md.options, {})


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    document_ids: list[str] = Field(min_length=1, max_length=MAX_SCOPE)
    focus_document_id: str
    focus_source_id: str
    line_start: int | None = None
    line_end: int | None = None
    question: str = Field(min_length=1, max_length=4000)
    strict: bool = False
    response_language: ResponseLanguage = "zh"


class PracticeStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    document_ids: list[str] = Field(min_length=1, max_length=MAX_SCOPE)
    focus_document_id: str
    focus_source_id: str
    line_start: int
    line_end: int
    objective: str = Field(default="", max_length=300)
    response_language: ResponseLanguage = "zh"


class PracticeAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    answer: str = Field(min_length=1, max_length=10_000)
    response_language: ResponseLanguage | None = None
    assistance: Literal["none_recorded", "hint_used", "reference_seen"] = "none_recorded"


class EnrollmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question_id: str
    unit_id: str | None = None
    response_language: ResponseLanguage = "zh"


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["variant", "original"] = "variant"
    response_language: ResponseLanguage = "zh"


class DimensionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    capability: Capability
    response_language: ResponseLanguage = "zh"


class RatingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    rating: ReviewRating
    expected_revision: int = Field(ge=1)


class StatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["active", "paused"]


class GradeCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question_id: str
    expected_revision: int = Field(ge=1)
    overall_assessment: Assessment
    covered_indices: list[int]
    missing_indices: list[int]
    feedback: str = Field(min_length=1, max_length=4000)
    suggested_rating: ReviewRating | None
    target_fit: Literal["within_scope", "outside_scope", "uncertain"]
    reason: str = Field(min_length=1, max_length=4000)


class PracticeGradeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    response_language: ResponseLanguage | None = None


class CorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    question_id: str
    correction: str = Field(min_length=1, max_length=4000)


def _source_for(document: Document) -> Source:
    try:
        return load_sources([document.path])[0]
    except (OSError, UnicodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"无法读取 {document.name}：{exc}") from exc


def create_app(notes_dir: Path = DEFAULT_NOTES_DIR, practice_db: Path | None = None) -> FastAPI:
    app = FastAPI(title="Knowledge Review", docs_url=None, redoc_url=None)
    catalog = DocumentCatalog(notes_dir)
    practice_store = LearningStore(practice_db or (
        DEFAULT_PRACTICE_DB if notes_dir == DEFAULT_NOTES_DIR else notes_dir.parent / "practice.sqlite3"
    ))
    templates = Jinja2Templates(directory=ROOT / "web" / "templates")
    app.mount("/static", StaticFiles(directory=ROOT / "web" / "static"), name="static")

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return templates.TemplateResponse(request, "index.html")

    @app.get("/api/documents")
    def documents():
        return [{"id": doc.document_id, "name": doc.name} for doc in catalog.list()]

    @app.get("/api/documents/{document_id}")
    def read_document(document_id: str):
        doc = catalog.get(document_id)
        source = _source_for(doc)
        return {
            "id": doc.document_id,
            "name": doc.name,
            "source_id": source.source_id,
            "line_count": len(source.text.splitlines()),
            "lines": source.text.splitlines(),
            "html": render_markdown(source.text),
        }

    @app.post("/api/documents", status_code=201)
    async def import_documents(files: list[UploadFile] = File(...)):
        if not 1 <= len(files) <= MAX_SCOPE:
            raise HTTPException(status_code=400, detail=f"每次最多导入 {MAX_SCOPE} 份笔记。")
        prepared = []
        for file in files:
            content = await file.read(MAX_UPLOAD_BYTES + 1)
            try:
                prepared.append(catalog.prepare_import(file.filename or "", content))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        imported = []
        for safe_name, text in prepared:
            doc = catalog.import_note(safe_name, text)
            imported.append({"id": doc.document_id, "name": doc.name})
        return imported

    def resolve_scope(document_ids, focus_document_id, focus_source_id, line_start, line_end):
        if len(set(document_ids)) != len(document_ids):
            raise HTTPException(status_code=400, detail="资料范围不能包含重复笔记。")
        if focus_document_id not in document_ids:
            raise HTTPException(status_code=400, detail="当前笔记必须包含在资料范围内。")
        documents_by_id = {doc_id: catalog.get(doc_id) for doc_id in document_ids}
        try:
            sources = load_sources([doc.path for doc in documents_by_id.values()])
        except (OSError, UnicodeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"无法读取资料：{exc}") from exc
        sources_by_path = {source.path: source for source in sources}
        focus_doc = documents_by_id[focus_document_id]
        focus_source = sources_by_path[focus_doc.path.resolve()]
        if focus_source.source_id != focus_source_id:
            raise HTTPException(status_code=409, detail="笔记内容已变化，请重新打开这份笔记。")
        if (line_start is None) != (line_end is None):
            raise HTTPException(status_code=400, detail="选段起止行需同时提供，或同时省略以询问整篇文档。")
        if line_start is not None and not 1 <= line_start <= line_end <= len(focus_source.text.splitlines()):
            raise HTTPException(status_code=400, detail="选段行号不在原文范围内。")
        return sources, documents_by_id, focus_source

    @app.post("/api/ask")
    def ask(payload: AskRequest):
        if not payload.question.strip():
            raise HTTPException(status_code=400, detail="请输入问题。")
        sources, documents_by_id, focus_source = resolve_scope(
            payload.document_ids, payload.focus_document_id, payload.focus_source_id,
            payload.line_start, payload.line_end,
        )
        sources_by_path = {source.path: source for source in sources}
        try:
            tools = KnowledgeTools(sources, select_embedding_config("e5-small"))
            focus = (document_focus(focus_source, payload.line_start, payload.line_end)
                     if payload.line_start is not None else None)
            run = run_agent(sources, payload.question, tools, strict=payload.strict, focus=focus,
                            response_language=payload.response_language)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (OSError, RuntimeError):
            LOGGER.exception("Question run failed")
            raise HTTPException(status_code=502, detail="问答未完成，请检查终端中的模型配置或错误信息。")

        doc_id_by_source = {
            sources_by_path[doc.path.resolve()].source_id: doc_id
            for doc_id, doc in documents_by_id.items()
        }
        source_by_id = {source.source_id: source for source in sources}
        return {
            "coverage": run.answer.coverage,
            "coverage_note": run.answer.coverage_note,
            "blocks": [{
                "kind": block.kind,
                "text": block.text,
                "citations": [{
                    "document_id": doc_id_by_source[citation.source_id],
                    "source_id": citation.source_id,
                    "name": source_by_id[citation.source_id].path.name,
                    "line_start": citation.line_start,
                    "line_end": citation.line_end,
                    "excerpt": "\n".join(source_by_id[citation.source_id].text.splitlines()[
                        citation.line_start - 1:citation.line_end
                    ]),
                } for citation in block.citations],
            } for block in run.answer.blocks],
            "steps": [{"name": step.name, "arguments": step.arguments, "error": step.error}
                      for step in run.steps],
            "stop_reason": run.stop_reason,
            "focus_context": [focus.chunk.line_start, focus.chunk.line_end] if focus else None,
        }

    @app.get("/api/practice")
    def list_practice():
        return practice_store.list()

    @app.get("/api/practice/{session_id}")
    def read_practice(session_id: str):
        try:
            return public_session(practice_store.get(session_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/practice", status_code=201)
    def create_practice(payload: PracticeStartRequest):
        sources, documents_by_id, focus_source = resolve_scope(
            payload.document_ids, payload.focus_document_id, payload.focus_source_id,
            payload.line_start, payload.line_end,
        )
        by_source = {
            source.source_id: doc_id
            for doc_id, doc in documents_by_id.items()
            for source in sources if source.path == doc.path.resolve()
        }
        try:
            return start_practice(
                practice_store, sources, by_source, focus_source.source_id,
                payload.line_start, payload.line_end, payload.objective,
                response_language=payload.response_language,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (OSError, RuntimeError):
            LOGGER.exception("Practice question generation failed")
            raise HTTPException(status_code=502, detail="出题未完成，请检查终端中的模型配置或错误信息。")

    @app.post("/api/practice/{session_id}/answers")
    def submit_practice_answer(session_id: str, payload: PracticeAnswerRequest):
        try:
            practice_store.submit_answer(session_id, payload.answer,
                                         response_language=payload.response_language, assistance=payload.assistance)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            return grade_pending_answer(practice_store, session_id)
        except (OSError, RuntimeError, ValueError):
            LOGGER.exception("Practice grading failed after answer was saved")
            raise HTTPException(status_code=502, detail="回答已保存，评分未完成。请在练习记录中重试评分。")

    @app.post("/api/practice/{session_id}/grade")
    def retry_practice_grade(session_id: str, payload: PracticeGradeRequest | None = None):
        try:
            return grade_pending_answer(practice_store, session_id,
                                        response_language=payload.response_language if payload else None)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            record = practice_store.get(session_id)
            if record["status"] != "grading":
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            LOGGER.exception("Practice grade validation failed")
            raise HTTPException(status_code=502, detail="评分未完成，原始答案仍已保存。") from exc
        except (OSError, RuntimeError):
            LOGGER.exception("Practice grading failed")
            raise HTTPException(status_code=502, detail="评分未完成，原始答案仍已保存。")

    @app.post("/api/practice/{session_id}/finish")
    def finish_practice(session_id: str):
        try:
            practice_store.finish(session_id)
            return public_session(practice_store.get(session_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/practice/{session_id}/corrections")
    def correct_practice_feedback(session_id: str, payload: CorrectionRequest):
        try:
            practice_store.save_correction(session_id, payload.question_id, payload.correction)
            return public_session(practice_store.get(session_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def learning_action(action):
        try:
            return action()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (OSError, RuntimeError):
            LOGGER.exception("Learning action failed")
            raise HTTPException(status_code=502, detail="操作未完成，已保存的题目和作答仍在，可稍后重试。")

    @app.get("/api/learning")
    def learning_plan():
        return practice_store.list_plan()

    @app.post("/api/practice/{session_id}/enroll")
    def enroll(session_id: str, payload: EnrollmentRequest):
        def action():
            item_id = enroll_question(practice_store, session_id, payload.question_id,
                unit_id=payload.unit_id, response_language=payload.response_language)
            return {"item_id": item_id, "session": public_session(practice_store.get(session_id))}
        return learning_action(action)

    @app.post("/api/learning/{item_id}/review")
    def review(item_id: str, payload: ReviewRequest):
        return learning_action(lambda: start_review(practice_store, item_id,
            mode=payload.mode, response_language=payload.response_language))

    @app.post("/api/learning/{item_id}/dimensions")
    def dimension(item_id: str, payload: DimensionRequest):
        return learning_action(lambda: add_dimension(practice_store, item_id, payload.capability,
            response_language=payload.response_language))

    @app.patch("/api/learning/{item_id}")
    def learning_status(item_id: str, payload: StatusRequest):
        return learning_action(lambda: (practice_store.set_status(item_id, payload.status), {"status": payload.status})[1])

    @app.patch("/api/review-events/{event_id}")
    def rating(event_id: str, payload: RatingRequest):
        return learning_action(lambda: (practice_store.set_rating(event_id, payload.rating, payload.expected_revision), {"saved": True})[1])

    @app.post("/api/practice/{session_id}/evaluation")
    def evaluation(session_id: str, payload: GradeCorrectionRequest):
        return learning_action(lambda: correct_grade(practice_store, session_id, payload.question_id,
            payload.model_dump(exclude={"question_id", "expected_revision", "reason"}), payload.reason, payload.expected_revision))

    @app.post("/api/practice/{session_id}/continue")
    def continue_practice(session_id: str):
        def action():
            practice_store.continue_follow_up(session_id)
            return public_session(practice_store.get(session_id))
        return learning_action(action)

    @app.post("/api/practice/{session_id}/reference/{question_id}")
    def reference(session_id: str, question_id: str):
        return learning_action(lambda: {"rubric_points": practice_store.reveal_reference(session_id, question_id)})

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web_app:app", host="127.0.0.1", port=8765)
