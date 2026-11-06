"""Local Markdown reading and source-aware question workspace."""

import logging
import re
import json
from contextlib import closing
from dataclasses import dataclass, replace
from html import escape
from pathlib import Path
from threading import Lock
from uuid import uuid4
from typing import Literal
from hashlib import sha256

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, BackgroundTasks
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field

from agent_cancel import GenerationCancelled, checkpoint
from agent_stream import StreamRunner
from agent_loop import document_focus, run_agent
from embedding_config import select_embedding_config
from knowledge_tools import KnowledgeTools, knowledge_tool_definitions
from practice_service import grade_pending_answer, start_practice
from practice_store import DEFAULT_PRACTICE_DB, PracticeStore, public_session, archived_sources
from learning_store import LearningStore
from learning_types import Assessment, Capability, ReviewRating, LearningDraft
from learning_service import enroll_question, start_review, add_dimension, correct_grade
from response_preferences import ResponseLanguage
from sources import Source, load_sources, source_from_document
from source_reader import StoredSource, read_lines, line_count, snapshot as source_snapshot, open_snapshot
from learning_metrics import summary as metrics_summary
from document_model import Document as ParsedDocument, snapshot_metadata, citation_metadata
from dotenv import load_dotenv
from conversation_store import ConversationStore, ConversationBusy
from conversation_context import build_context
from conversation_memory import ConversationMemory, MemoryAnswer, SourceMemoryAnswer, memory_tool_definitions
from generation_budget import GenerationBudget, BudgetError
from answers import NoteAnswer, SourceOnlyAnswer, Citation
from context import format_context
from structured_output import structured_protocols
from library_store import LibraryStore
from outline_store import OutlineStore, OutlineBusy
from task_routing import TaskRouter, capabilities
from outline_service import (scope_fingerprint, run_outline, public_outline,
                             candidate_context, edit_candidate, enroll_candidate, require_current)

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
    revision: int = 1
    text_override: str | None = None
    source_override: dict | None = None


class DocumentCatalog:
    def __init__(self, notes_dir: Path, library=None):
        self.notes_dir = notes_dir
        self.library = library

    def managed(self, doc):
        if self.library is None:
            return doc
        row = self.library.document_state(doc.document_id)
        if row['removed']:
            raise HTTPException(410, detail='这份资料已移出知识库；旧练习仍保留出题时的版本依据。')
        return Document(doc.document_id, doc.path, doc.name, row['revision'], row['text_override'],
                        json.loads(row['source_json']) if row.get('source_json') else None)

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
                files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in ('.md', '.pdf')]
                if len(files) == 1 and files[0].is_file():
                    uploads.append(Document(f"upload-{folder.name}", files[0], files[0].name))
        result = []
        for doc in examples + uploads:
            try:
                result.append(self.managed(doc))
            except HTTPException as exc:
                if exc.status_code != 410:
                    raise
        return result

    def get(self, document_id: str) -> Document:
        match = UPLOAD_ID.fullmatch(document_id)
        if match:
            folder = self.notes_dir / match.group(1)
            files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in ('.md', '.pdf')] if folder.is_dir() else []
            if len(files) == 1 and files[0].is_file():
                return self.managed(Document(document_id, files[0], files[0].name))
        elif document_id.startswith("example-"):
            stem = document_id.removeprefix("example-")
            if re.fullmatch(r"[A-Za-z0-9_-]+", stem):
                path = ROOT / "examples" / f"{stem}.md"
                if path.is_file():
                    return self.managed(Document(document_id, path, path.name))
        raise HTTPException(status_code=404, detail="找不到这份笔记。")

    @staticmethod
    def prepare_import(filename: str, content: bytes) -> tuple[str, str | bytes]:
        original = filename.replace("\\", "/").split("/")[-1]
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", original).strip(" .")
        if Path(safe_name).suffix.lower() not in ('.md', '.pdf') or not safe_name or len(safe_name) > 120:
            raise ValueError("请选择文件名不超过 120 个字符的 Markdown (.md) 或 PDF (.pdf) 文件。")
        if safe_name.lower().endswith('.pdf'):
            from pdf_parser import MAX_PDF_BYTES
            if not content.startswith(b'%PDF-') or len(content) > MAX_PDF_BYTES:
                raise ValueError('请选择有效且不超过 20 MB 的 PDF。')
            return safe_name, content
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValueError("单份笔记不能超过 2 MB。")
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("笔记必须使用 UTF-8 编码。") from exc
        if not text.strip():
            raise ValueError("不能导入空笔记。")
        return safe_name, text

    def import_note(self, safe_name: str, text: str | bytes, parsed=None, images=None) -> Document:
        folder_id = uuid4().hex
        folder = self.notes_dir / folder_id
        folder.mkdir(parents=True)
        path = folder / safe_name
        if isinstance(text, bytes):
            path.write_bytes(text)
            (folder / 'document.json').write_text(json.dumps(parsed.to_dict(), ensure_ascii=False), encoding='utf-8')
        else:
            if images:
                from markdown_images import import_images
                import_images(path,text,images)
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

    # Old API clients without this field keep A; the web selector sends B.
    retrieval_mode: Literal['dense', 'hybrid'] = 'dense'


class ConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(default="新聊天", min_length=1, max_length=120)


class TurnRequest(AskRequest):
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")


class RetryTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    response_language: ResponseLanguage | None = None
    retrieval_mode: Literal['dense', 'hybrid'] | None = None
    clarification: dict | None = None
    save_only: bool = False


class ClarificationReply(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    card_id: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=1)
    response_id: str = Field(min_length=1, max_length=100)
    option_id: str | None = Field(default=None, max_length=100)
    text: str = Field(default='', max_length=4000)


class ChatPracticeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    request_id: str = Field(min_length=1, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$')
    objective: str = Field(default='', max_length=300)
    response_language: ResponseLanguage = 'zh'
    collection_id: str = 'root'
    strict: bool = False
    document_ids: list[str] = Field(default_factory=list, max_length=MAX_SCOPE)
    focus_document_id: str | None = None
    focus_source_id: str | None = None


class PracticeStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    document_ids: list[str] = Field(min_length=1, max_length=MAX_SCOPE)
    focus_document_id: str
    focus_source_id: str
    line_start: int | None = None
    line_end: int | None = None
    objective: str = Field(default="", max_length=300)
    response_language: ResponseLanguage = "zh"


class CollectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=100)
    parent_id: str = "root"


class CollectionAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    collection_id: str


class CollectionPracticeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    objective: str = Field(default="", max_length=300)
    response_language: ResponseLanguage = "zh"


class OutlineRequest(CollectionPracticeRequest):
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    algorithm: Literal['v1', 'v2'] = 'v1'
    context_tokens: int | None = Field(default=None, ge=8192, le=1048576)


class PlanningRequest(CollectionPracticeRequest):
    request_id: str = Field(min_length=1,max_length=100,pattern=r'^[a-zA-Z0-9_-]+$')
    source_id: str = Field(min_length=1)
    max_requests: int = Field(default=16,ge=1,le=128)
    formalize: bool = True
    visual_coverage: bool = False


class PlanningResumeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    additional_calls: int = Field(default=0,ge=0,le=128)


class PlanningExerciseRequest(PlanningResumeRequest):
    goal_id: str = Field(min_length=1, max_length=200)
    batch_index: int | None = Field(default=None,ge=1)


class PlanningNextBatchRequest(PlanningResumeRequest):
    batch_index: int = Field(ge=2)


class OutlineEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    learning: LearningDraft
    revision: int = Field(ge=1)


class OutlineEnrollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: int = Field(ge=1)
    collection_id: str = "root"
    unit_id: str | None = None
    response_language: ResponseLanguage = "zh"


class OutlineGroupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    response_language: ResponseLanguage = "zh"
    candidate_ids: list[str] | None = None
    call_limit: int = Field(default=24, ge=1, le=200)


class OutlineGroupContinueRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    call_limit: int = Field(default=24, ge=1, le=200)


class OutlineGroupReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: int = Field(ge=1)
    decision: Literal['proposed', 'accepted', 'rejected']
    name: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=600)
    canonical_id: str | None = None
    unit_id: str | None = None


class OutlineSourceReviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    request_id: str = Field(min_length=1, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$')
    response_language: ResponseLanguage = 'zh'


class OutlineSourceReviewApplyRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    group_revision: int = Field(ge=1)
    canonical_id: str | None = None
    unit_id: str | None = None


class OutlineRouteRequest(OutlineGroupRequest):
    candidate_ids: list[str] | None = Field(default=None, min_length=1)


class OutlineRouteEdgeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    revision: int = Field(ge=1)
    before_id: str
    after_id: str
    kind: Literal['prerequisite','helpful_first','uncertain']
    reason: str = Field(min_length=1,max_length=350)
    decision: Literal['proposed','accepted','rejected']


class OutlineRouteOrderRequest(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    revision: int = Field(ge=1)
    order: list[str] | None
    skipped: list[str]


class OutlineRouteAddRequest(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    revision: int = Field(ge=1)
    before_id: str
    after_id: str
    kind: Literal['prerequisite','helpful_first','uncertain']
    reason: str = Field(min_length=1,max_length=350)


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
    collection_id: str = "root"


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
    if document.path.suffix.lower() == '.pdf':
        try:
            data = document.source_override or json.loads((document.path.parent / 'document.json').read_text(encoding='utf-8'))
            source = open_snapshot(data) if data.get('reader') else source_from_document(document.path, ParsedDocument.from_dict(data))
            from pdf_parser import raw_path
            raw_path(source)
            return source
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(422, detail='PDF 原件或解析快照无法读取，请重新导入。') from exc
    if document.text_override is not None:
        path = document.path.resolve()
        text = document.text_override
        from markdown_images import bind_source
        return bind_source(path,text,sha256((str(path) + '\0' + text).encode('utf-8')).hexdigest())
    try:
        return load_sources([document.path])[0]
    except (OSError, UnicodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"无法读取 {document.name}：{exc}") from exc


def create_app(notes_dir: Path = DEFAULT_NOTES_DIR, practice_db: Path | None = None,
               conversation_db: Path | None = None) -> FastAPI:
    streams = StreamRunner()
    app = FastAPI(title="Knowledge Review", docs_url=None, redoc_url=None, lifespan=streams.lifespan)
    app.state.stream_runner = streams

    @app.get("/internal/metrics")
    def worker_metrics():
        return streams.snapshot()

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}
    from retrieval_review_app import review_router
    app.include_router(review_router())
    catalog = DocumentCatalog(notes_dir)
    practice_store = LearningStore(practice_db or (
        DEFAULT_PRACTICE_DB if notes_dir == DEFAULT_NOTES_DIR else notes_dir.parent / "practice.sqlite3"
    ))
    conversations = ConversationStore(conversation_db or notes_dir.parent / "conversations.sqlite3")
    library = LibraryStore(practice_store)
    catalog.library = library
    practice_store.conversation_path = conversations.path
    conversations.practice_path = practice_store.path
    outlines = OutlineStore(practice_store)
    app.state.conversation_store = conversations
    app.state.practice_store = practice_store
    app.state.library_store = library
    app.state.outline_store = outlines
    from learning_planner import PlanningStore, run_planning
    planning = PlanningStore(practice_store.path)
    app.state.planning_store = planning
    templates = Jinja2Templates(directory=ROOT / "web" / "templates")
    app.mount("/static", StaticFiles(directory=ROOT / "web" / "static"), name="static")

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return templates.TemplateResponse(request, "index.html", {
            'task_capabilities': capabilities(),
            "asset_version": str(max(p.stat().st_mtime_ns for p in (ROOT / "web" / "static").iterdir() if p.is_file())),
        })

    @app.get("/api/documents")
    def documents(collection_id: str = "root"):
        allowed = set(learning_action(lambda: library.descendants(collection_id)))
        memberships = library.document_memberships()
        return [{"id": doc.document_id, "name": doc.name, "collection_id": memberships.get(doc.document_id, "root")}
                for doc in catalog.list() if memberships.get(doc.document_id, "root") in allowed]

    @app.get("/api/collections")
    def collections():
        return library.list()

    @app.post("/api/collections", status_code=201)
    def create_collection(payload: CollectionRequest):
        return learning_action(lambda: library.create(payload.name, payload.parent_id))

    @app.patch("/api/collections/{identity}")
    def rename_collection(identity: str, payload: CollectionRequest):
        return learning_action(lambda: library.rename(identity, payload.name))

    @app.post('/api/collections/{identity}/move')
    def move_collection(identity: str, payload: CollectionAssignment):
        return learning_action(lambda: library.move(identity, payload.collection_id))

    @app.delete('/api/collections/{identity}')
    def remove_collection(identity: str):
        return learning_action(lambda: library.remove_collection(identity))

    @app.patch("/api/documents/{identity}/collection")
    def move_document(identity: str, payload: CollectionAssignment):
        catalog.get(identity)
        return learning_action(lambda: (library.assign_documents([identity], payload.collection_id), {"saved": True})[1])

    @app.patch("/api/learning/{identity}/collection")
    def move_plan_item(identity: str, payload: CollectionAssignment):
        return learning_action(lambda: (library.move_review(identity, payload.collection_id), {"saved": True})[1])

    @app.post("/api/collections/{identity}/practice", status_code=201)
    @streams.bounded
    def collection_practice(identity: str, payload: CollectionPracticeRequest):
        docs = documents(identity)
        if not docs:
            raise HTTPException(status_code=400, detail="这个集合及其子集合中还没有 Markdown 文件，请先导入资料。")
        scope_docs = [catalog.get(doc['id']) for doc in docs]
        sources = [_source_for(doc) for doc in scope_docs]
        by_source = {source.source_id: doc["id"] for source, doc in zip(sources, docs)}
        load_dotenv(ROOT / ".env")
        return learning_action(lambda: start_practice(practice_store, sources, by_source, sources[0].source_id,
            None, None, payload.objective, response_language=payload.response_language, collection_id=identity,
            before_request=lambda: guard_documents(scope_docs, sources),
            document_dependencies=[{'id': d.document_id, 'revision': d.revision} for d in scope_docs]))

    def collection_snapshots(identity):
        allowed = set(library.descendants(identity))
        memberships = library.document_memberships()
        snapshots = []
        for doc in catalog.list():
            if memberships.get(doc.document_id, 'root') in allowed:
                source = _source_for(doc)
                if isinstance(source,StoredSource):
                    raise ValueError('此资料使用按页存档，请在阅读区使用自主学习清单；旧集合清单尚不支持该格式。')
                snapshots.append({'document_id': doc.document_id, 'source_id': source.source_id,
                                  'path': str(source.path), 'name': doc.name, 'text': source.text, **snapshot_metadata(source)})
        return snapshots

    def outline_action(action):
        try:
            return action()
        except KeyError as exc:
            raise HTTPException(404, detail=str(exc).strip("'")) from exc
        except (ValueError, OutlineBusy) as exc:
            raise HTTPException(409, detail=str(exc)) from exc
        except (OSError, RuntimeError) as exc:
            raise HTTPException(502, detail='清单处理失败，已有内容保留，可以重试。') from exc

    @app.get('/api/collections/{identity}/outlines')
    def outline_list(identity: str):
        def action():
            library.get(identity)
            result = outlines.list(identity)
            fingerprint = scope_fingerprint(collection_snapshots(identity))
            for row in result:
                row['stale'] = row.pop('scope_fingerprint') != fingerprint
                if row['status'] in ('queued', 'running'):
                    row['status'] = public_outline(outlines, row['id'], collection_snapshots)['status']
            return result
        return outline_action(action)

    def planning_current(job):
        document_id=job['inputs'].get('document_id')
        if not document_id:
            raise HTTPException(409,'此任务没有网页资料绑定。')
        if job['inputs']['snapshot'].get('reader'):
            # Read revision/access without fetching the full parsed document JSON.
            with closing(practice_store._connect()) as db:
                row=db.execute('SELECT revision,removed FROM document_states WHERE document_id=?',
                               (document_id,)).fetchone()
            source=archived_sources([job['inputs']['snapshot']])[0]
            from source_reader import file_digest
            from pdf_parser import raw_path
            physical=raw_path(source) if source.document and source.document.format=='pdf' else source.path
            if row is None or row['removed'] or row['revision']!=job['inputs']['document_revision'] or (
                    not source.file_hash or file_digest(physical)!=source.file_hash):
                raise HTTPException(409,'资料版本已变化，请按当前资料创建规划。')
            return source
        document=catalog.get(document_id)
        source=_source_for(document)
        if source.source_id != job['inputs']['snapshot']['source_id'] or (
                job['inputs'].get('document_revision') is not None and
                document.revision != job['inputs']['document_revision']):
            raise HTTPException(409,'资料版本已变化，请按当前资料创建规划。')
        return source

    def batch_job(job, index=None):
        if not job.get('batches'):
            if index is not None:
                raise ValueError('此任务没有分批。')
            return job
        index=index if index is not None else job.get('working_batch') or job['batch_cursor']+1
        if not 1<=index<=len(job['batches']):
            raise ValueError('批次编号无效。')
        child=job['batches'][index-1]['job_id']
        if not child:
            raise ValueError('此批次尚未开始。')
        return planning.get(child)

    def root_job(identity):
        job=planning.get(identity)
        if job['inputs'].get('parent_id'):
            raise ValueError('请从父任务操作批次，以保留共享额度及取消保护。')
        return job

    def public_planning(job, batch_index=None):
        if job.get('batches'):
            from learning_batches import batch_summary
            summaries=batch_summary(planning,job)
            index=batch_index if batch_index is not None else job.get('working_batch') or job['batch_cursor']+1
            if not 1<=index<=len(job['batches']):
                raise ValueError('批次编号无效。')
            selected=batch_job(job,index) if job['batches'][index-1]['job_id'] else job
            result=public_planning(selected) if selected is not job else public_single_planning(job)
            return {**result,'id':job['id'],'status':job['status'],'error':job['error'],
                'metrics':metrics_summary(planning,job['id']),
                'calls_reserved':job['calls_reserved'],'call_limit':job['call_limit'],
                'batches':summaries,'batch_index':index,'batch_status':selected['status'],
                'batch_cursor':job['batch_cursor']+1,'working_batch':job.get('working_batch'),
                'batch_range':[job['batches'][index-1]['line_start'],job['batches'][index-1]['line_end']],
                'completed_batches':sum(s['generation_complete'] for s in summaries),
                'can_next':job['status']=='waiting_next'}
        if batch_index is not None:
            raise ValueError('此任务没有分批。')
        return public_single_planning(job)

    def public_single_planning(job):
        try:
            planning_current(job)
            stale=False
        except (HTTPException,OSError,ValueError):
            stale=True
        source=archived_sources([job['inputs']['snapshot']])[0]
        evidence={identity:{**{k:v for k,v in item.items() if k!='receipt'}, 'citation':{
            **item['citation'], 'document_id':job['inputs']['document_id'], 'name':source.path.name,
            'excerpt':item.get('text',item.get('observation','')),
            **citation_metadata(source,Citation.model_validate(item['citation']))}}
            for identity,item in job['evidence'].items()}
        formal=job.get('formal',{})
        revisions=[dict(stage=e['stage'],note=e['note'],changes=e['changes'])
            for e in job['events'] if e.get('stage') in ('refine','review') and e['action']=='save']
        return {key:job[key] for key in ('id','status','items','dispositions','units',
                'calls_reserved','call_limit','error')} | dict(stale=stale,
                stage=job.get('stage','planning'), formal=formal, revisions=revisions,
                metrics=metrics_summary(planning,job['id']), runtime=job.get('runtime'),
                visual_coverage=job.get('visual_coverage'),visual_coverage_enabled=job['inputs'].get('visual_coverage',False),
                parse_status=source.document.status if source.document else None,
                parse_issues=list(source.document.issues) if source.document else [],
                questions={key:dict(session_id=value['session_id']) for key,value in job.get('questions',{}).items()},
                evidence=evidence,document_id=job['inputs']['document_id'],source_id=job['inputs']['snapshot']['source_id'])

    @app.get('/api/documents/{identity}/learning-plans')
    def list_planning(identity:str):
        catalog.get(identity)
        return [public_planning(job) for job in planning.list_for(identity)]

    @app.post('/api/documents/{identity}/learning-plans',status_code=202)
    @streams.detached('planning',resume=False)
    def create_planning(identity:str,payload:PlanningRequest,background:BackgroundTasks):
        document=catalog.get(identity)
        source=_source_for(document)
        if source.source_id != payload.source_id:
            raise HTTPException(409,'资料版本已变化，请刷新。')
        def action():
            job_id=planning.create(source,payload.objective or '整理值得掌握的能力及必要条件',
                payload.request_id,language=payload.response_language,max_requests=payload.max_requests,
                document_id=identity,document_revision=document.revision,formalize=payload.formalize,visual_coverage=payload.visual_coverage)
            job=planning.get(job_id)
            if job['status']=='queued':
                background.add_task(run_planning,planning,job_id,lambda:planning_current(planning.get(job_id)))
            return {'id':job_id}
        return outline_action(action)

    @app.get('/api/learning-plans/{identity}')
    def read_planning(identity:str,batch_index:int | None=None):
        return outline_action(lambda:public_planning(root_job(identity),batch_index))

    @app.post('/api/learning-plans/{identity}/next-batch',status_code=202)
    @streams.detached('planning',resume=True)
    def next_planning_batch(identity:str,payload:PlanningNextBatchRequest,background:BackgroundTasks):
        def action():
            job=root_job(identity)
            planning_current(job)
            if not job.get('batches') or payload.batch_index>len(job['batches']):
                raise ValueError('批次编号无效。')
            if payload.batch_index>job['batch_cursor']+1:
                if job['status']!='waiting_next' or payload.batch_index!=job['batch_cursor']+2:
                    raise ValueError('请先完成当前批次。')
                background.add_task(run_planning,planning,identity,
                    lambda:planning_current(planning.get(identity)),next_batch=payload.batch_index,
                    additional_calls=payload.additional_calls)
            return {'id':identity}
        return outline_action(action)

    @app.post('/api/learning-plans/{identity}/resume',status_code=202)
    @streams.detached('planning',resume=True)
    def resume_planning(identity:str,payload:PlanningResumeRequest,background:BackgroundTasks):
        def action():
            job=root_job(identity)
            planning_current(job)
            if job['status'] not in ('completed','waiting_next'):
                background.add_task(run_planning,planning,identity,
                    lambda:planning_current(planning.get(identity)),additional_calls=payload.additional_calls)
            return {'id':identity}
        return outline_action(action)

    @app.post('/api/learning-plans/{identity}/pause')
    def pause_planning(identity:str):
        def action():
            root_job(identity)
            planning.pause(identity)
            streams.cancel_job('planning',identity)
            return {'id':identity}
        return outline_action(action)

    @app.post('/api/learning-plans/{identity}/formalize',status_code=202)
    @streams.detached('planning',resume=True)
    def formalize_planning(identity:str,payload:PlanningResumeRequest,background:BackgroundTasks):
        def action():
            job=root_job(identity)
            planning_current(job)
            if job.get('batches'):
                selected=batch_job(job)
                if selected['status']!='completed' or selected.get('stage')!='planning':
                    raise ValueError('仅能将当前已完成的轻量规划转为正式清单。')
            if job.get('stage','planning')=='planning' and job['status']!='completed':
                raise ValueError('请先完成轻量规划。')
            if job.get('stage')!='ready':
                background.add_task(run_planning,planning,identity,
                    lambda:planning_current(planning.get(identity)),additional_calls=payload.additional_calls,formalize=True)
            return {'id':identity}
        return outline_action(action)

    @app.post('/api/learning-plans/{identity}/questions',status_code=202)
    @streams.detached('planning',resume=True)
    def planning_question(identity:str,payload:PlanningExerciseRequest,background:BackgroundTasks):
        def action():
            job=root_job(identity)
            planning_current(job)
            selected=batch_job(job,payload.batch_index)
            if payload.goal_id not in selected.get('formal',{}).get('goals',{}):
                raise ValueError('正式目标不存在。')
            if not selected.get('questions',{}).get(payload.goal_id):
                if selected.get('stage')!='ready' or selected['status']!='completed':
                    raise ValueError('请先完成本批复查；中断的出题请用继续按钮恢复。')
                if job.get('batches') and job['status'] not in ('waiting_next','completed'):
                    raise ValueError('请先完成或恢复当前操作，再选择目标出题。')
                background.add_task(run_planning,planning,identity,
                    lambda:planning_current(planning.get(identity)),additional_calls=payload.additional_calls,
                    question_goal_id=payload.goal_id,question_batch=payload.batch_index)
            return {'id':identity}
        return outline_action(action)

    @app.post('/api/collections/{identity}/outlines', status_code=202)
    @streams.detached("outline", resume=False)
    def create_outline(identity: str, payload: OutlineRequest, background: BackgroundTasks):
        def action():
            snapshots = collection_snapshots(identity)
            if not snapshots:
                raise ValueError('这个集合及子集合还没有 Markdown 资料，请先导入。')
            from practice_store import archived_sources
            load_dotenv(ROOT / '.env')
            if payload.algorithm == 'v2':
                from outline_v2 import plan_direct
                sections, batches, plan = plan_direct(archived_sources(snapshots), snapshots,
                    payload.objective, payload.response_language, payload.context_tokens)
            else:
                if payload.context_tokens is not None:
                    raise ValueError('V1继续使用原环境预算；context_tokens仅用于V2实验任务。')
                from outline_context import plan_outline
                sections, batches, plan = plan_outline(archived_sources(snapshots), snapshots, payload.objective, payload.response_language)
            job_id = outlines.create(identity, payload.request_id, payload.objective, payload.response_language,
                scope_fingerprint(snapshots), snapshots, sections, batches, plan)
            if outlines.get(job_id)['status'] == 'queued':
                background.add_task(run_outline, outlines, job_id, collection_snapshots)
            return {'id': job_id}
        return outline_action(action)

    @app.get('/api/outlines/{identity}')
    def read_outline(identity: str):
        return outline_action(lambda: public_outline(outlines, identity, collection_snapshots))

    @app.post('/api/outlines/{identity}/resume', status_code=202)
    @streams.detached("outline", resume=True)
    def resume_outline(identity: str, background: BackgroundTasks):
        def action():
            with outlines.lock(identity):
                job = outlines.get(identity)
                require_current(job, collection_snapshots)
                outlines.resume(identity)
                # Reserved before returning: another job cannot take this collection.
                with practice_store.transaction() as db:
                    db.execute("UPDATE learning_outlines SET status='queued' WHERE id=?", (identity,))
            background.add_task(run_outline, outlines, identity, collection_snapshots)
            return {'id': identity}
        return outline_action(action)

    @app.post('/api/outlines/{identity}/pause')
    def pause_outline(identity: str):
        def action():
            from outline_final_store import latest, pause
            review=latest(outlines,identity)
            if review:
                pause(outlines,review['id']); streams.cancel_job('final_review',review['id'])
            outlines.pause(identity); streams.cancel_job('outline', identity)
            return {'id':identity}
        return outline_action(action)

    @app.post('/api/outline-final-reviews/{identity}/resume',status_code=202)
    @streams.detached('final_review',resume=True)
    def resume_final_outline(identity: str, payload: OutlineGroupContinueRequest, background: BackgroundTasks):
        def action():
            from outline_final_store import resume
            from outline_final_review import run_final_review
            resume(outlines,identity,collection_snapshots,payload.call_limit)
            background.add_task(run_final_review,outlines,identity,collection_snapshots)
            return {'id':identity}
        return outline_action(action)

    @app.post('/api/outline-final-reviews/{identity}/pause')
    def pause_final_outline(identity: str):
        def action():
            from outline_final_store import record,pause
            record(outlines,identity); pause(outlines,identity); streams.cancel_job('final_review',identity)
            return {'id':identity}
        return outline_action(action)

    @app.patch('/api/outline-candidates/{identity}')
    def update_outline_candidate(identity: str, payload: OutlineEditRequest):
        return outline_action(lambda: {'revision': edit_candidate(outlines, identity, payload.learning, payload.revision)})

    @app.post('/api/outlines/{identity}/grouping', status_code=202)
    @streams.detached("grouping", resume=False)
    def group_outline(identity: str, payload: OutlineGroupRequest, background: BackgroundTasks):
        def action():
            from outline_grouping import start_grouping
            from outline_review import run_grouping_with_reviews
            from outline_group_store import get_run
            load_dotenv(ROOT / '.env')
            run_id = start_grouping(outlines, identity, payload.request_id, payload.response_language,
                                    payload.candidate_ids, collection_snapshots, payload.call_limit)
            if get_run(outlines, run_id)['status'] == 'queued':
                background.add_task(run_grouping_with_reviews, outlines, run_id, collection_snapshots)
            return {'id': run_id}
        return outline_action(action)

    @app.post('/api/outline-group-runs/{identity}/retry', status_code=202)
    @streams.detached("grouping", resume=True)
    def retry_outline_grouping(identity: str, background: BackgroundTasks, payload: OutlineGroupContinueRequest):
        def action():
            from outline_grouping import retry_grouping
            from outline_review import run_grouping_with_reviews
            retry_grouping(outlines, identity, collection_snapshots, payload.call_limit)
            background.add_task(run_grouping_with_reviews, outlines, identity, collection_snapshots)
            return {'id': identity}
        return outline_action(action)

    @app.post('/api/outline-group-runs/{identity}/pause')
    def pause_outline_grouping(identity: str):
        def action():
            from outline_group_store import get_run
            from outline_group_steps import pause_grouping
            get_run(outlines, identity)
            pause_grouping(outlines, identity)
            streams.cancel_job('grouping', identity)
            return {'id': identity}
        return outline_action(action)

    @app.patch('/api/outline-groups/{identity}')
    def update_outline_group(identity: str, payload: OutlineGroupReviewRequest):
        def action():
            from outline_group_store import review_group
            with closing(practice_store._connect()) as db:
                row = db.execute('SELECT outline_id FROM outline_groups WHERE id=?', (identity,)).fetchone()
            if not row:
                raise KeyError('归组建议不存在。')
            if payload.decision != 'rejected':
                require_current(outlines.get(row['outline_id']), collection_snapshots)
            return {'revision': review_group(outlines, identity, payload.revision, payload.decision,
                payload.name, payload.reason, payload.canonical_id, payload.unit_id)}
        return outline_action(action)

    @app.post('/api/outline-groups/{identity}/source-reviews', status_code=202)
    @streams.detached("review", resume=False)
    def source_review(identity: str, payload: OutlineSourceReviewRequest, background: BackgroundTasks):
        def action():
            from outline_review import start_source_review, run_source_review
            from outline_review_store import get_review
            load_dotenv(ROOT / '.env')
            review_id = start_source_review(outlines, identity, payload.request_id, payload.response_language, collection_snapshots)
            if get_review(outlines, review_id)['status'] == 'queued':
                background.add_task(run_source_review, outlines, review_id, collection_snapshots)
            return {'id': review_id}
        return outline_action(action)

    @app.post('/api/outline-source-reviews/{identity}/continue', status_code=202)
    @streams.detached("review", resume=True)
    def continue_review(identity: str, payload: OutlineGroupContinueRequest, background: BackgroundTasks):
        def action():
            from outline_review import continue_source_review, run_source_review
            continue_source_review(outlines, identity, collection_snapshots, payload.call_limit)
            background.add_task(run_source_review, outlines, identity, collection_snapshots)
            return {'id': identity}
        return outline_action(action)

    @app.post('/api/outline-source-reviews/{identity}/pause')
    def pause_source_review(identity: str):
        from outline_review_store import pause_review
        return outline_action(lambda: (pause_review(outlines, identity), streams.cancel_job('review', identity), {'id': identity})[2])

    @app.post('/api/outline-source-reviews/{identity}/apply')
    def apply_review(identity: str, payload: OutlineSourceReviewApplyRequest):
        from outline_review import apply_source_review
        return outline_action(lambda: {'group_id': apply_source_review(outlines, identity, payload.group_revision,
            collection_snapshots, payload.canonical_id, payload.unit_id)})

    @app.post('/api/outlines/{identity}/routes',status_code=202)
    @streams.detached("route", resume=False)
    def generate_route(identity: str,payload: OutlineRouteRequest,background: BackgroundTasks):
        def action():
            from outline_routes import start_route,run_route
            from outline_route_store import route_record
            load_dotenv(ROOT/'.env')
            key=start_route(outlines,identity,payload.request_id,payload.response_language,payload.candidate_ids,collection_snapshots,payload.call_limit)
            if route_record(outlines,key)['status']=='queued':background.add_task(run_route,outlines,key,collection_snapshots)
            return {'id':key}
        return outline_action(action)

    @app.post('/api/outline-routes/{identity}/continue',status_code=202)
    @streams.detached("route", resume=True)
    def retry_route(identity: str,payload: OutlineGroupContinueRequest,background: BackgroundTasks):
        def action():
            from outline_routes import continue_route,run_route
            continue_route(outlines,identity,collection_snapshots,payload.call_limit)
            background.add_task(run_route,outlines,identity,collection_snapshots)
            return {'id':identity}
        return outline_action(action)

    @app.post('/api/outline-routes/{identity}/pause')
    def stop_route(identity: str):
        from outline_route_store import pause_route
        return outline_action(lambda:(pause_route(outlines,identity),streams.cancel_job('route',identity),{'id':identity})[2])

    @app.patch('/api/outline-route-edges/{identity}')
    def review_route_edge(identity: str,payload: OutlineRouteEdgeRequest):
        from outline_routes import edit_edge
        return outline_action(lambda:{'revision':edit_edge(outlines,identity,payload.revision,payload.before_id,payload.after_id,
            payload.kind,payload.reason,payload.decision,collection_snapshots)})

    @app.patch('/api/outline-routes/{identity}/order')
    def change_route_order(identity: str,payload: OutlineRouteOrderRequest):
        from outline_routes import save_order
        return outline_action(lambda:{'revision':save_order(outlines,identity,payload.revision,payload.order,payload.skipped,collection_snapshots)})

    @app.post('/api/outline-routes/{identity}/edges',status_code=201)
    def add_route_edge(identity: str,payload: OutlineRouteAddRequest):
        from outline_routes import add_edge
        return outline_action(lambda:{'id':add_edge(outlines,identity,payload.revision,payload.before_id,payload.after_id,payload.kind,payload.reason,collection_snapshots)})

    @app.post('/api/outline-candidates/{identity}/enroll')
    @streams.bounded
    def enroll_outline_candidate(identity: str, payload: OutlineEnrollRequest):
        return outline_action(lambda: enroll_candidate(outlines, identity, payload.revision, payload.collection_id,
            payload.unit_id, payload.response_language, collection_snapshots))

    @app.get("/api/documents/{document_id}")
    def read_document(document_id: str,page:int=1,line_start:int | None=None):
        doc = catalog.get(document_id)
        source = _source_for(doc)
        if isinstance(source,StoredSource):
            return read_stored_document(doc,source,page,line_start)
        result = {
            "id": doc.document_id,
            "name": doc.name,
            "source_id": source.source_id,
            "revision": doc.revision,
            "managed": document_id.startswith('upload-'),
            "line_count": len(source.text.splitlines()),
            "lines": source.text.splitlines(),
            "html": render_markdown(source.text),
        }
        structure = source.document
        result.update(format=structure.format, blocks=[b.__dict__ for b in structure.blocks] if structure.format == 'pdf' else [],
                      page_count=structure.page_count, status=structure.status, issues=list(structure.issues))
        if structure.format=='markdown' and structure.assets:
            from html import unescape
            from document_model import markdown_document
            original=markdown_document(source.text); mapping={}
            current={b.block_id:b for b in structure.blocks}
            for b in original.blocks:
                for before,after in zip(b.asset_ids,current[b.block_id].asset_ids): mapping[before]=after
            uris={a.uri:mapping.get(a.asset_id,a.asset_id) for a in original.assets}
            result['assets']=[dict(asset_id=a.asset_id,url=f'/api/documents/{document_id}/assets/{a.asset_id}?source_id={source.source_id}')
                              for a in structure.assets if a.asset_id.startswith('mdimg-')]
            def image_html(match):
                identity=uris.get(unescape(match[1]),'')
                if not identity.startswith('mdimg-'): return '<span>［图片未导入］</span>'
                return f'<img loading="lazy" alt="资料图片" src="/api/documents/{document_id}/assets/{identity}?source_id={source.source_id}">'
            result['html']=re.sub(r'<img\b[^>]*src="([^"]*)"[^>]*>',image_html,result['html'])
        # Dataclass locations must be serialized explicitly rather than exposing paths.
        if structure.format == 'pdf':
            from pdf_vision import vision_status
            result['vision'] = vision_status()
            result['blocks'] = structure.to_dict()['blocks']
            base = f'/api/documents/{document_id}/assets/'
            result['assets'] = [{**a, 'url': base + a['asset_id'] + '?source_id=' + source.source_id}
                                for a in structure.to_dict()['assets'] if a['page']]
            labels = {'garbled_text': '提取文字存在乱码', 'reading_order_needs_review': '多栏阅读顺序需要核对',
                      'visual_content_needs_review': '页面主要为图片，尚无可靠文字', 'table_structure_needs_review': '表格结构需要核对',
                      'table_detection_failed': '表格提取未完成', 'docling_failed': '结构解析未完成，已保留基础解析结果',
                      'vlm_failed': '视觉转写未完成', 'vlm_unresolved': '视觉转写仍有无法辨认的内容',
                      'vlm_unsupported': '当前模型服务不支持图片，已保留常规解析结果；可单独配置视觉模型',
                      'vlm_no_reliable_text': '视觉转写没有取得可靠文字', 'vlm_page_budget_exhausted': '已达到本次视觉转写页数上限'}
            if structure.issues:
                result['html'] = '<aside class="pdf-quality"><strong>解析提示</strong><ul>' + ''.join(
                    f'<li>第 {int(i["page"])} 页：{escape(labels.get(i["code"], "内容需要核对"))}</li>'
                    for i in structure.issues) + '</ul></aside>' + result['html']
            result['html'] += '<section class="pdf-originals"><h3>原始页面</h3>' + ''.join(
                f'<details><summary>第 {a["page"]} 页原图</summary><img loading="lazy" alt="第 {a["page"]} 页原图" src="{escape(a["url"])}"></details>'
                for a in result['assets'] if a['kind'] == 'page') + '</section>'
        return result

    def read_stored_document(doc,source,page,start):
        from dataclasses import asdict
        structure=source.document
        try:
            if start is not None: page=structure.page_at(start)
            left,right=structure.page(page)
            start=left if start is None else start
            if not left<=start<=right: raise ValueError('读取位置不在当前页。')
            end=min(right,start+199)
            text=read_lines(source,start,end)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc
        base=f'/api/documents/{doc.document_id}/assets/'
        assets=[{**asdict(a),'url':base+a.asset_id+'?source_id='+source.source_id} for a in structure.read_assets(page)]
        html=render_markdown(text)
        html=re.sub(r'data-line-(start|end)="(\d+)"',lambda m:f'data-line-{m[1]}="{int(m[2])+start-1}"',html)
        if structure.status=='partial':
            html='<aside class="pdf-quality">资料包含待核对的解析内容；请结合原始页面查看，完整问题记录见学习清单的解析提示。</aside>'+html
        html+='<section class="pdf-originals"><h3>原始页面</h3>'+''.join(
            f'<details><summary>第 {a["page"]} 页原图</summary><img loading="lazy" alt="原始页面" src="{escape(a["url"])}"></details>'
            for a in assets if a['kind']=='page')+'</section>'
        return dict(id=doc.document_id,name=doc.name,source_id=source.source_id,revision=doc.revision,
            managed=doc.document_id.startswith('upload-'),line_count=source.line_count,lines=text.splitlines(),html=html,
            format='pdf',blocks=[asdict(b) for b in structure.read_blocks(start,end)],page_count=structure.page_count,
            status=structure.status,issues=[i for i in structure.issues if i.get('page')==page],assets=assets,
            paged=True,page=page,window_start=start,window_end=end,
            previous_query=(f'line_start={max(left,start-200)}' if start>left else f'page={page-1}' if page>1 else None),
            next_query=(f'line_start={end+1}' if end<right else f'page={page+1}' if page<structure.page_count else None))

    @app.get('/api/documents/{document_id}/assets/{asset_id}')
    def read_asset(document_id: str, asset_id: str, source_id: str):
        source = _source_for(catalog.get(document_id))
        if source.source_id != source_id:
            raise HTTPException(409, detail='资源属于旧资料版本，请查看当时的引用快照。')
        try:
            from pdf_parser import render_asset
            if source.document.format=='markdown':
                from markdown_images import render
                return Response(render(source,asset_id)[0],media_type='image/png',headers={'Cache-Control':'private, no-store'})
            return Response(render_asset(source, asset_id), media_type='image/png', headers={'Cache-Control': 'private, no-store'})
        except (OSError, ValueError) as exc:
            raise HTTPException(404, detail='当前版本没有这个视觉资源。') from exc

    @app.post("/api/documents", status_code=201)
    async def import_documents(request: Request, files: list[UploadFile] = File(...), collection_id: str = "root",import_id:str | None=None,images:list[UploadFile]=File(default=[])):
        learning_action(lambda: library.get(collection_id))
        if not 1 <= len(files) <= MAX_SCOPE:
            raise HTTPException(status_code=400, detail=f"每次最多导入 {MAX_SCOPE} 份笔记。")
        from pdf_parse_cache import write_record
        progress_path=None
        progress_state=dict(status='running',file_index=0,total_files=len(files),phase='uploading')
        progress_lock=Lock()
        if import_id is not None:
            if not re.fullmatch('[0-9a-f]{32}',import_id): raise HTTPException(400,'导入进度标识无效。')
            progress_path=notes_dir.parent/'pdf-import-progress'/(import_id+'.json')
            if progress_path.exists(): raise HTTPException(409,'此导入进度标识已使用，请新建导入。')
            write_record(progress_path,progress_state)
        def report(value):
            with progress_lock:
                if progress_path and progress_state['status']=='running':
                    progress_state.update(value); write_record(progress_path,progress_state)
        prepared = []
        image_data={}
        if images:
            from markdown_images import relative_name,checked_image,MAX_BYTES
            if len(files)!=1 or not (files[0].filename or '').lower().endswith('.md') or len(images)>20:
                raise HTTPException(400,'附图导入请一次选择一份Markdown及最多20张图片。')
            try:
                for image in images:
                    name=relative_name(image.filename or '')
                    if name in image_data: raise ValueError('图片相对路径重复。')
                    body=await image.read(MAX_BYTES+1); checked_image(body); image_data[name]=body
                    if sum(map(len,image_data.values()))>32*1024*1024: raise ValueError('附图总量不能超过32MB。')
            except (ValueError,OSError) as exc:
                if progress_path:
                    with progress_lock:
                        progress_state.update(status='interrupted'); write_record(progress_path,progress_state)
                raise HTTPException(400,str(exc)) from exc
        staged=[]
        imported = []
        published=False
        try:
            for index,file in enumerate(files,1):
                report(dict(file_index=index,phase='uploading',completed_pages=0,total_pages=None))
                if (file.filename or '').lower().endswith('.pdf'):
                    folder_id=uuid4().hex
                    folder=notes_dir.parent/'pdf-import-staging'/folder_id; folder.mkdir(parents=True)
                    staged.append(folder)
                    safe_name=DocumentCatalog.prepare_import(file.filename,b'%PDF-')[0]
                    path=folder/safe_name
                    await save_upload(file,path)
                    final_path=notes_dir/folder_id/safe_name
                    parsed=await parse_stored_pdf(path,final_path,safe_name,request,progress=report)
                    record=source_snapshot(parsed) if isinstance(parsed,StoredSource) else parsed.to_dict()
                    (folder/'document.json').write_text(json.dumps(record,ensure_ascii=False),encoding='utf-8')
                    prepared.append((folder,folder_id,safe_name))
                else:
                    safe_name,body=catalog.prepare_import(file.filename or '',await file.read(MAX_UPLOAD_BYTES+1))
                    prepared.append((None,None,(safe_name,body)))
            for folder,folder_id,value in prepared:
                if folder:
                    notes_dir.mkdir(parents=True,exist_ok=True); folder.replace(notes_dir/folder_id)
                    doc=Document('upload-'+folder_id,notes_dir/folder_id/value,value)
                else: doc=catalog.import_note(*value,images=image_data)
                imported.append(dict(id=doc.document_id,name=doc.name,collection_id=collection_id))
            library.assign_documents([doc['id'] for doc in imported],collection_id)
            published=True
            if progress_path:
                with progress_lock:
                    progress_state.update(status='completed',phase='published'); write_record(progress_path,progress_state)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc
        finally:
            if progress_path and not published:
                with progress_lock:
                    progress_state.update(status='interrupted'); write_record(progress_path,progress_state)
            import shutil
            staging_root=(notes_dir.parent/'pdf-import-staging').resolve()
            for folder in staged:
                # A disconnected request may leave a real parser still exiting.
                # Keep its raw input/checkpoints; never delete under that worker.
                if published and folder.exists() and folder.resolve().is_relative_to(staging_root): shutil.rmtree(folder)
        return imported

    @app.get('/api/document-imports/{identity}')
    def import_progress(identity:str):
        if not re.fullmatch('[0-9a-f]{32}',identity): raise HTTPException(404,'导入进度不存在。')
        path=notes_dir.parent/'pdf-import-progress'/(identity+'.json')
        if not path.is_file(): raise HTTPException(404,'导入进度不存在。')
        return json.loads(path.read_text(encoding='utf-8'))

    async def save_upload(upload,path):
        from pdf_parser import MAX_PDF_BYTES
        size=0
        with path.open('wb') as target:
            while content:=await upload.read(65536):
                size+=len(content)
                if size>MAX_PDF_BYTES: raise ValueError('PDF不能超过20MB。')
                target.write(content)
        with path.open('rb') as check:
            if check.read(5)!=b'%PDF-': raise ValueError('请选择有效PDF。')

    async def parse_stored_pdf(path,identity_path,raw_file,request,*,force=False,progress=None):
        from pdf_parser import profile
        from pdf_archive import parse_pdf_stored
        config=await run_in_threadpool(profile)
        if config['engine']=='docling':
            # Optional legacy converter explicitly retains its full-memory contract.
            return await parse_document_pdf(await run_in_threadpool(path.read_bytes),raw_file,request,force=force)
        def parse():
            result=parse_pdf_stored(path,notes_dir.parent/'pdf-source-archives',config=config,force=force,
                                    identity_path=identity_path,raw_file=raw_file,progress=progress)
            checkpoint()
            return result
        # Parsing consumes CPU/disk even without VLM. Hold the existing bounded
        # worker until it actually exits, with disconnect cancellation too.
        return await streams.run(parse,request)

    @app.put('/api/documents/{identity}')
    async def replace_document(identity: str, expected_source_id: str, request: Request, file: UploadFile = File(...)):
        doc = catalog.get(identity)
        if not identity.startswith('upload-'):
            raise HTTPException(400, detail='示例文档只供阅读，请导入自己的文件再替换。')
        if doc.path.suffix.lower() == '.pdf' and _source_for(doc).source_id != expected_source_id:
            raise HTTPException(409, detail='PDF 版本已变化，请刷新后再替换。')
        if doc.path.suffix.lower()=='.pdf':
            safe_name=catalog.prepare_import(file.filename or '',b'%PDF-')[0]
            if not safe_name.lower().endswith('.pdf'): raise HTTPException(400,'替换文件需保持PDF格式。')
            version_dir=doc.path.parent/'versions'; version_dir.mkdir(exist_ok=True)
            pending=version_dir/(uuid4().hex+'.tmp')
            try:
                await save_upload(file,pending)
                from source_reader import file_digest
                version=version_dir/(file_digest(pending)+'.pdf')
                if version.exists(): pending.unlink()
                else: pending.replace(version)
                parsed=await parse_stored_pdf(version,doc.path,'versions/'+version.name,request)
                return learning_action(lambda:library.change_document(identity,_source_for(doc),expected_source_id,
                    expected_revision=doc.revision,document=parsed))
            except ValueError as exc: raise HTTPException(400,str(exc)) from exc
            finally: pending.unlink(missing_ok=True)
        try:
            from pdf_parser import MAX_PDF_BYTES
            safe_name, text = catalog.prepare_import(file.filename or '', await file.read(MAX_PDF_BYTES + 1))
            if Path(safe_name).suffix.lower() != doc.path.suffix.lower():
                raise ValueError('替换文件需保持原资料格式；不同格式请另行导入。')
            parsed = None
            if isinstance(text, bytes):
                parsed = await parse_document_pdf(text, safe_name, request)
                version_dir = doc.path.parent / 'versions'
                version_dir.mkdir(exist_ok=True)
                version = version_dir / (parsed.raw_hash + '.pdf')
                if not version.exists():
                    version.write_bytes(text)
                parsed = replace(parsed, raw_file='versions/' + version.name)
                text = None
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
        return learning_action(lambda: library.change_document(identity, _source_for(doc), expected_source_id,
                               expected_revision=doc.revision, text=text, document=parsed))

    @app.delete('/api/documents/{identity}')
    def remove_document(identity: str, expected_source_id: str):
        doc = catalog.get(identity)
        if not identity.startswith('upload-'):
            raise HTTPException(400, detail='示例文档不能移除。')
        return learning_action(lambda: library.change_document(identity, _source_for(doc), expected_source_id,
                               expected_revision=doc.revision, remove=True))

    @app.post('/api/documents/{identity}/reparse')
    async def reparse_document(identity: str, expected_source_id: str, request: Request):
        doc = catalog.get(identity)
        source = _source_for(doc)
        if doc.path.suffix.lower() != '.pdf' or source.source_id != expected_source_id:
            raise HTTPException(409, detail='仅能重新解析当前版本的 PDF，请先刷新资料。')
        from pdf_parser import raw_path
        try:
            parsed = await parse_stored_pdf(raw_path(source),doc.path,source.document.raw_file,request,force=True)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, detail=str(exc)) from exc
        return learning_action(lambda: library.change_document(identity, source, expected_source_id,
                               expected_revision=doc.revision, document=parsed))

    async def parse_document_pdf(data, raw_file, request, *, force=False):
        from pdf_parser import parse_pdf, profile
        config = await run_in_threadpool(profile)

        def parse():
            result = parse_pdf(data, raw_file=raw_file, config=config,
                               checkpoint_dir=notes_dir.parent / 'pdf-parse-pages', force=force)
            # Local parsing is cooperative too: cancellation must prevent publish
            # even when it arrives after the last remote request has completed.
            checkpoint()
            return result

        if config.get('vlm'):
            # Admit the complete VLM-enabled parse before any page requests.
            # Reusing run preserves disconnect cancellation and real-work release.
            return await streams.run(parse, request)
        return await run_in_threadpool(parse)

    def resolve_scope(document_ids, focus_document_id, focus_source_id, line_start, line_end):
        if len(set(document_ids)) != len(document_ids):
            raise HTTPException(status_code=400, detail="资料范围不能包含重复笔记。")
        if focus_document_id not in document_ids:
            raise HTTPException(status_code=400, detail="当前笔记必须包含在资料范围内。")
        documents_by_id = {doc_id: catalog.get(doc_id) for doc_id in document_ids}
        try:
            sources = [_source_for(doc) for doc in documents_by_id.values()]
        except (OSError, UnicodeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"无法读取资料：{exc}") from exc
        sources_by_path = {source.path: source for source in sources}
        focus_doc = documents_by_id[focus_document_id]
        focus_source = sources_by_path[focus_doc.path.resolve()]
        if focus_source.source_id != focus_source_id:
            raise HTTPException(status_code=409, detail="笔记内容已变化，请重新打开这份笔记。")
        if (line_start is None) != (line_end is None):
            raise HTTPException(status_code=400, detail="选段起止行需同时提供，或同时省略以询问整篇文档。")
        if line_start is not None and not 1 <= line_start <= line_end <= line_count(focus_source):
            raise HTTPException(status_code=400, detail="选段行号不在原文范围内。")
        return sources, documents_by_id, focus_source

    def generate_answer(payload: AskRequest, conversation_context=None, budget=None, resolved_scope=None, diagnostics=None, event_sink=None, selected_task=None):
        if not payload.question.strip():
            raise HTTPException(status_code=400, detail="请输入问题。")
        sources, documents_by_id, focus_source = resolved_scope or resolve_scope(
            payload.document_ids, payload.focus_document_id, payload.focus_source_id,
            payload.line_start, payload.line_end,
        )
        sources_by_path = {source.path: source for source in sources}
        try:
            if selected_task and selected_task not in ('explain', 'recall_discussion'):
                control = TaskRouter().execute('select_task', {'task_id': selected_task})
                guard_documents(list(documents_by_id.values()), sources)
                return {'control': control}, conversation_context.snapshot() if conversation_context else {}
            tools = KnowledgeTools(sources, select_embedding_config("e5-small"), retrieval_mode=payload.retrieval_mode,
                                   reading_range=(payload.line_start,payload.line_end) if isinstance(focus_source,StoredSource) and payload.line_start else None,
                                   index_path=practice_store.path.parent/'embeddings.sqlite3',object_db=practice_store.path)
            if diagnostics is not None:
                diagnostics['retrieval'] = {'mode':payload.retrieval_mode,'searches':tools.retrieval_trace}
            focus = (document_focus(focus_source, payload.line_start, payload.line_end)
                     if payload.line_start is not None else None)
            extra = ({"conversation_context": conversation_context, "budget": budget}
                     if conversation_context is not None else {})
            if conversation_context is not None:
                extra["memory_tools"] = ConversationMemory(conversations, conversation_context.memory_dependencies[0]["conversation_id"],
                                                           conversation_context.memory_dependencies)
                if selected_task is None:
                    extra['task_router'] = TaskRouter()
            if event_sink is not None:
                extra["event_sink"] = event_sink
            run = run_agent(sources, payload.question, tools, strict=payload.strict, focus=focus,
                            response_language=payload.response_language,
                            before_request=lambda: guard_documents(list(documents_by_id.values()), sources), **extra)
            guard_documents(list(documents_by_id.values()), sources)
        except BudgetError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (OSError, RuntimeError) as exc:
            # Provider exceptions can echo request data. Do not log chat contents.
            LOGGER.error("Question run failed (%s)", type(exc).__name__)
            raise HTTPException(status_code=502, detail="问答未完成，请检查终端中的模型配置或错误信息。")

        if getattr(run, 'control', None):
            return {'control': run.control}, run.context_snapshot

        doc_id_by_source = {
            sources_by_path[doc.path.resolve()].source_id: doc_id
            for doc_id, doc in documents_by_id.items()
        }
        source_by_id = {source.source_id: source for source in sources}
        result = {
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
                    **citation_metadata(source_by_id[citation.source_id], citation),
                    "excerpt": read_lines(source_by_id[citation.source_id],citation.line_start,citation.line_end),
                } for citation in block.citations],
            } for block in run.answer.blocks],
            "steps": [{"name": step.name, "arguments": step.arguments, "error": step.error}
                      for step in run.steps],
            "stop_reason": run.stop_reason,
            "focus_context": [focus.chunk.line_start, focus.chunk.line_end] if focus else None,
            "history_references": list(run.history_references),
            "retrieval": {'mode':payload.retrieval_mode, 'searches':tools.retrieval_trace,
                          'final_visible_ranges':[entry['visible_ranges'] for entry in
                              (run.context_snapshot or {}).get('generation_requests', [])
                              if entry.get('phase')=='final']},
        }
        return result, run.context_snapshot

    @app.post("/api/ask")
    @streams.bounded
    def ask(payload: AskRequest):
        return generate_answer(payload)[0]

    def conversation_action(action):
        try:
            return action()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConversationBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def public_turn(turn):
        return {k: turn[k] for k in ("id", "seq", "request_id", "status", "error", "messages", "context", "memory_valid", "control")}

    @app.get('/api/task-capabilities')
    def task_capabilities():
        return capabilities()

    @app.post('/api/conversations/{identity}/turns/{turn_id}/clarification')
    def answer_clarification(identity: str, turn_id: str, payload: ClarificationReply):
        def action():
            try:
                conversations.answer_clarification(identity, turn_id, payload.card_id, payload.revision,
                    payload.response_id, payload.option_id, payload.text)
            except ConversationBusy:
                raise
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            return public_turn(conversations.turn(turn_id))
        return conversation_action(action)

    @app.get("/api/conversations")
    def list_conversations(archived: bool = False):
        return conversations.list(archived=archived)

    @app.post("/api/conversations/{identity}/archive")
    def archive_conversation(identity: str):
        return conversation_action(lambda: conversations.archive(identity, True))

    @app.post("/api/conversations/{identity}/restore")
    def restore_conversation(identity: str):
        return conversation_action(lambda: conversations.archive(identity, False))

    @app.delete("/api/conversations/{identity}")
    def delete_conversation(identity: str):
        conversation_action(lambda: conversations.delete(identity))
        return {"deleted": True}

    @app.get("/api/history/{message_id}")
    def history_reference(message_id: str, start_offset: int, end_offset: int, message_revision: int = 1):
        memory = ConversationMemory(conversations, None)
        try:
            message = memory.message(message_id)
            if message_revision != 1 or not 0 <= start_offset < end_offset <= len(message["content"]):
                raise HTTPException(status_code=400, detail="历史引用范围或版本不合法。")
            return memory.span(message, start_offset, end_offset)
        except ValueError as exc:
            raise HTTPException(status_code=410, detail="关联原话已删除或不再可用于记忆。") from exc

    @app.post("/api/conversations", status_code=201)
    def create_conversation(payload: ConversationRequest):
        if not payload.title.strip():
            raise HTTPException(status_code=400, detail="聊天标题不能为空。")
        return conversations.create(payload.title.strip())

    @app.get("/api/conversations/{identity}")
    def read_conversation_metadata(identity: str):
        return conversation_action(lambda: conversations.get_conversation(identity))

    @app.patch("/api/conversations/{identity}")
    def rename_conversation(identity: str, payload: ConversationRequest):
        if not payload.title.strip():
            raise HTTPException(status_code=400, detail="聊天标题不能为空。")
        return conversation_action(lambda: conversations.rename(identity, payload.title.strip()))

    @app.get("/api/conversations/{identity}/messages")
    def conversation_messages(identity: str, background_tasks: BackgroundTasks, after_seq: int = 0, limit: int = 100):
        if after_seq < 0 or not 1 <= limit <= 200:
            raise HTTPException(status_code=400, detail="历史分页参数不合法。")
        def action():
            try:
                with conversations.generation_lock(identity):
                    conversations.recover(identity)
            except ConversationBusy:
                pass  # A live generation is displayed as running; do not reset it.
            turns = conversations.turns(identity, after=after_seq, limit=limit)
            chat = conversations.get_conversation(identity)
            if chat["title_state"] in ("pending", "generating", "failed"):
                completed = next((turn for turn in turns if turn["status"] == "completed"), None)
                if completed:
                    background_tasks.add_task(streams.title, conversations, identity, completed["request"].get("response_language", "zh"))
            return {"conversation": conversations.get_conversation(identity),
                    "turns": [public_turn(turn) for turn in turns],
                    "next_cursor": turns[-1]["seq"] if len(turns) == limit else None,
                    "summary": conversations.summary(identity)}
        return conversation_action(action)

    @app.get("/api/conversations/{identity}/summary")
    def conversation_summary(identity: str):
        return conversation_action(lambda: conversations.summary(identity))

    def execute_turn(identity, request_id, request_data, retry_language=None, retry_retrieval_mode=None, event_sink=None):
        with conversations.generation_lock(identity):
            conversations.recover(identity)
            turn = conversations.begin(identity, request_id, request_data)
            if event_sink is not None:
                event_sink("meta", {"request_id": request_id, "turn_id": turn["id"]})
            if turn["status"] == "completed" or (turn['status'] == 'waiting_user' and not turn['control'].get('answer')):
                return public_turn(turn)
            diagnostics = {}
            try:
                checkpoint()
                effective = dict(request_data)
                # Requests stored before mode selection used dense retrieval.
                effective.setdefault('retrieval_mode', 'dense')
                if retry_language is not None:
                    effective["response_language"] = retry_language
                if retry_retrieval_mode is not None:
                    effective['retrieval_mode'] = retry_retrieval_mode
                payload = AskRequest.model_validate(effective)
                selection = (turn.get('control') or {}).get('answer')
                selected_task = selection.get('option_id') if selection else None
                if selection:
                    from task_routing import TASKS
                    clarification = selection['text'] or TASKS[selected_task][0]
                    payload.question += '\n\n用户对任务的明确补充：' + clarification
                diagnostics['retrieval'] = {'mode':payload.retrieval_mode,'searches':[]}
                load_dotenv(ROOT / ".env")
                budget = GenerationBudget.from_env()
                scope = resolve_scope(payload.document_ids, payload.focus_document_id, payload.focus_source_id,
                                      payload.line_start, payload.line_end)
                sources, _, focus_source = scope
                focus = document_focus(focus_source, payload.line_start, payload.line_end) if payload.line_start is not None else None
                required_data = {
                    "question": payload.question, "response_language": payload.response_language,
                    "materials": [{"id": s.source_id, "name": s.path.name, "lines": line_count(s)} for s in sources],
                    "focus": format_context([focus.chunk], sources) if focus else None,
                    "agent_tools": knowledge_tool_definitions() + memory_tool_definitions(),
                    "final_protocols": structured_protocols(SourceMemoryAnswer if payload.strict else MemoryAnswer),
                }
                background = build_context(conversations, identity, payload.question, payload.response_language, budget,
                                           required_data=required_data)
                if event_sink is not None:
                    event_sink("progress", {"phase": "context", "status": "completed"})
                result, snapshot = generate_answer(payload, background, budget, scope, diagnostics,
                                                   **({"event_sink": event_sink} if event_sink is not None else {}),
                                                   **({'selected_task': selected_task} if selected_task else {}))
                snapshot = snapshot or background.snapshot()
                snapshot['document_dependencies'] = [{'id': d.document_id, 'revision': d.revision} for d in scope[1].values()]
                checkpoint()
                if 'control' in result:
                    control = result['control']
                    if control['kind'] == 'clarification':
                        conversations.wait_for_user(turn, control, snapshot)
                        return public_turn(conversations.turn(turn['id']))
                    result = dict(coverage='none', coverage_note='已识别任务，请在任务入口确认资料范围及具体对象。',
                        blocks=[], steps=[], stop_reason='task_selected', task_action={**control,
                            'document_ids': payload.document_ids, 'objective': payload.question,
                            'focus_document_id': payload.focus_document_id, 'focus_source_id': payload.focus_source_id})
                conversations.finish(turn["id"], turn["attempt"], result, snapshot or background.snapshot())
            except GenerationCancelled:
                conversations.fail(turn["id"], turn["attempt"], "生成已取消，原始输入已保存，可以重试。", diagnostics)
            except HTTPException as exc:
                conversations.fail(turn["id"], turn["attempt"], str(exc.detail), diagnostics)
            except Exception as failure:
                error = "生成未完成，原始输入已保存，可以重试。"
                # Budget failures have safe, actionable messages; transport failures do not.
                if isinstance(failure, (BudgetError, ConversationBusy)):
                    error = str(failure)
                else:
                    LOGGER.error("Conversation generation failed (%s)", type(failure).__name__)
                conversations.fail(turn["id"], turn["attempt"], error, diagnostics)
            return public_turn(conversations.turn(turn["id"]))

    @app.post("/api/conversations/{identity}/turns/stream")
    async def stream_turn(identity: str, payload: TurnRequest, request: Request):
        if not payload.question.strip():
            raise HTTPException(status_code=400, detail="请输入问题。")

        def action(event_sink):
            # Preflight happens in the worker before meta/HTTP 200. execute_turn
            # retains its own permission checks and generation lock for races.
            resolve_scope(payload.document_ids, payload.focus_document_id, payload.focus_source_id,
                          payload.line_start, payload.line_end)
            def notify(event, data):
                if event == "meta":
                    data = {**data, "trace_id": request.headers.get("X-Request-ID", "")[:128]}
                event_sink(event, data)
            return conversation_action(lambda: execute_turn(
                identity, payload.request_id, payload.model_dump(exclude={"request_id"}), event_sink=notify))

        return await streams.response(action, request=request,
            after_released=lambda: streams.title(conversations, identity, payload.response_language))

    @app.post("/api/conversations/{identity}/turns/{turn_id}/retry/stream")
    async def stream_retry_turn(identity: str, turn_id: str, payload: RetryTurnRequest, request: Request):
        def action(event_sink):
            def retry():
                turn = conversations.turn(turn_id)
                if turn['conversation_id'] != identity:
                    raise KeyError('轮次不属于当前聊天。')
                def notify(event, data):
                    if event == 'meta':
                        data = {**data, 'trace_id': request.headers.get('X-Request-ID', '')[:128]}
                    event_sink(event, data)
                return execute_turn(identity, turn['request_id'], turn['request'],
                                    payload.response_language, payload.retrieval_mode, event_sink=notify)
            return conversation_action(retry)
        return await streams.response(action, request=request,
            after_released=lambda: streams.title(conversations, identity,
                payload.response_language or conversations.turn(turn_id)['request'].get('response_language', 'zh')))

    @app.post("/api/conversations/{identity}/turns")
    @streams.bounded
    def submit_turn(identity: str, payload: TurnRequest, background_tasks: BackgroundTasks):
        if not payload.question.strip():
            raise HTTPException(status_code=400, detail="请输入问题。")
        result = conversation_action(lambda: execute_turn(identity, payload.request_id, payload.model_dump(exclude={"request_id"})))
        if result["status"] == "completed":
            background_tasks.add_task(streams.title, conversations, identity, payload.response_language)
        return result

    @app.post("/api/conversations/{identity}/turns/{turn_id}/retry")
    @streams.bounded
    def retry_turn(identity: str, turn_id: str, payload: RetryTurnRequest, background_tasks: BackgroundTasks):
        if payload.clarification is not None:
            try:
                reply = ClarificationReply.model_validate(payload.clarification)
            except ValueError as exc:
                raise HTTPException(400, '澄清回答格式不合法。') from exc
            saved = answer_clarification(identity, turn_id, reply)
            if payload.save_only:
                return saved
        elif payload.save_only:
            raise HTTPException(400, '保存澄清需要提供回答。')
        def action():
            turn = conversations.turn(turn_id)
            if turn["conversation_id"] != identity:
                raise KeyError("轮次不属于当前聊天。")
            return execute_turn(identity, turn["request_id"], turn["request"], payload.response_language, payload.retrieval_mode)
        result = conversation_action(action)
        if result["status"] == "completed":
            language = payload.response_language or conversations.turn(turn_id)["request"].get("response_language", "zh")
            background_tasks.add_task(streams.title, conversations, identity, language)
        return result

    @app.post("/api/conversations/{identity}/turns/{turn_id}/skip")
    def skip_turn(identity: str, turn_id: str):
        def action():
            with conversations.generation_lock(identity):
                conversations.recover(identity)
                conversations.skip(identity, turn_id)
            return public_turn(conversations.turn(turn_id))
        return conversation_action(action)

    @app.get("/api/practice")
    def list_practice():
        return practice_store.list()

    def guard_documents(docs, sources):
        for doc, source in zip(docs, sources, strict=True):
            current = catalog.get(doc.document_id)
            if current.revision != doc.revision or _source_for(current).source_id != source.source_id:
                raise ValueError('资料已替换或移除，请重新打开文档后生成。')

    @app.get('/api/conversations/{identity}/practice')
    def chat_practice_jobs(identity: str):
        def action():
            conversations.get_conversation(identity)
            with closing(practice_store._connect()) as db:
                return [dict(r) for r in db.execute('SELECT id,request_id,status,error,session_id,request_json FROM chat_practice_jobs WHERE conversation_id=? ORDER BY rowid DESC', (identity,))]
        return learning_action(action)

    @app.post('/api/conversations/{identity}/practice')
    @streams.bounded
    def discussion_practice(identity: str, payload: ChatPracticeRequest):
        from chat_practice import start
        def action():
            library.get(payload.collection_id)
            sources, docs = [], {}
            if payload.strict:
                if not payload.focus_document_id or not payload.focus_source_id or not payload.document_ids:
                    raise ValueError('严格资料模式需打开当前文档。')
                sources, docs, _ = resolve_scope(payload.document_ids, payload.focus_document_id, payload.focus_source_id, None, None)
            by_source = {s.source_id: d.document_id for d, s in zip(docs.values(), sources)}
            load_dotenv(ROOT / '.env')
            return start(practice_store, conversations, identity, payload.request_id,
                payload.model_dump(exclude={'request_id'}), sources=sources, document_ids=by_source,
                document_dependencies=[{'id': d.document_id, 'revision': d.revision} for d in docs.values()],
                source_guard=lambda: guard_documents(list(docs.values()), sources))
        return learning_action(action)

    @app.get("/api/practice/{session_id}")
    def read_practice(session_id: str):
        try:
            return public_session(practice_store.get(session_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/practice", status_code=201)
    @streams.bounded
    def create_practice(payload: PracticeStartRequest):
        load_dotenv(ROOT / ".env")
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
                collection_id=library.document_memberships().get(payload.focus_document_id, "root"),
                before_request=lambda: guard_documents(list(documents_by_id.values()), sources),
                document_dependencies=[{'id': d.document_id, 'revision': d.revision} for d in documents_by_id.values()],
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (OSError, RuntimeError):
            LOGGER.exception("Practice question generation failed")
            raise HTTPException(status_code=502, detail="出题未完成，请检查终端中的模型配置或错误信息。")

    @app.post("/api/practice/{session_id}/answers")
    @streams.bounded
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
    @streams.bounded
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
    def learning_plan(collection_id: str = "root"):
        return learning_action(lambda: practice_store.list_plan(collection_id))

    @app.post("/api/practice/{session_id}/enroll")
    def enroll(session_id: str, payload: EnrollmentRequest):
        def action():
            item_id = enroll_question(practice_store, session_id, payload.question_id,
                unit_id=payload.unit_id, response_language=payload.response_language, collection_id=payload.collection_id)
            return {"item_id": item_id, "session": public_session(practice_store.get(session_id))}
        return learning_action(action)

    @app.post("/api/learning/{item_id}/review")
    @streams.bounded
    def review(item_id: str, payload: ReviewRequest):
        return learning_action(lambda: start_review(practice_store, item_id,
            mode=payload.mode, response_language=payload.response_language))

    @app.post("/api/learning/{item_id}/dimensions")
    @streams.bounded
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


_default_app_lock = Lock()


def __getattr__(name):
    """Keep uvicorn web_app:app compatible without opening stores on import."""
    if name != 'app':
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    with _default_app_lock:
        if 'app' not in globals():
            globals()['app'] = create_app()
        return globals()['app']


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web_app:app", host="127.0.0.1", port=8765)
