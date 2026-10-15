"""Exception-focused label review; keep Agent adoption and human decisions distinct."""

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from retrieval_benchmark_data import ROOT, DEFAULT_REVIEW_DB, ReviewStore


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Judgment(Contract):
    chunk_id: str = Field(min_length=1, max_length=100)
    grade: StrictInt | None = None
    note: str = Field(default='', max_length=3000)


class EvidenceGroup(Contract):
    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=500)
    bundles: list[list[str]] = Field(max_length=100)


class Review(Contract):
    answerable: StrictBool
    status: Literal['pending', 'approved', 'excluded']
    scope_checked: StrictBool
    note: str = Field(max_length=5000)
    judgments: list[Judgment] = Field(max_length=10000)
    groups: list[EvidenceGroup] = Field(max_length=100)
    issue_resolution: str = Field(max_length=5000)
    origin: Literal['user', 'agent'] = 'user'
    audit_id: str | None = Field(default=None, max_length=100)


class SaveReview(Contract):
    expected_revision: int = Field(ge=0)
    payload: Review


class Freeze(Contract):
    reviewer: str = Field(min_length=1, max_length=100)
    note: str = Field(min_length=1, max_length=5000)
    expected_revisions: dict[str, StrictInt]
    confirmed: Literal[True]


def review_router(path: Path = DEFAULT_REVIEW_DB):
    router = APIRouter()
    # Lazy initialization: importing the application does not create an eval DB.
    def store():
        return ReviewStore(path)

    def call(operation):
        try:
            return operation()
        except KeyError as exc:
            raise HTTPException(404, detail=str(exc).strip("'")) from exc
        except ValueError as exc:
            raise HTTPException(409, detail=str(exc)) from exc

    @router.get('/retrieval-review', include_in_schema=False)
    def page():
        return FileResponse(ROOT / 'web/templates/retrieval-review.html')

    @router.get('/api/retrieval-benchmark')
    def benchmarks():
        return {'benchmarks': store().list()}

    @router.get('/api/retrieval-benchmark/{identity}')
    def overview(identity: str):
        def operation():
            current = store()
            manifest = current.manifest(identity)
            return {'id': identity, 'name': manifest['suite']['revision'], 'fingerprint': manifest['fingerprint'],
                    'chunk_count': len(manifest['chunks']), 'source_count': len(manifest['sources']),
                    'queries': [{**q, 'review': current.review(identity, q['id'])} for q in manifest['suite']['queries']],
                    'freezes': current.freezes(identity)}
        return call(operation)

    @router.get('/api/retrieval-benchmark/{identity}/queries/{query_id}')
    def detail(identity: str, query_id: str):
        def operation():
            current = store()
            manifest = current.manifest(identity)
            review = current.review(identity, query_id)
            query = current.query(manifest, query_id)
            audit_case = current.agent_audit_case(identity, query_id, review['payload'].get('audit_id'))
            return {'query': query, 'review': review,
                    'agent_audit': audit_case,
                    'sources': [s for s in manifest['sources'] if s['file'] in query['scope']],
                    'chunks': [c for c in manifest['chunks'] if c['file'] in query['scope']]}
        return call(operation)

    @router.put('/api/retrieval-benchmark/{identity}/queries/{query_id}')
    def save(identity: str, query_id: str, request: SaveReview):
        return call(lambda: store().save(identity, query_id, request.payload.model_dump(), request.expected_revision))

    @router.post('/api/retrieval-benchmark/{identity}/freeze')
    def freeze(identity: str, request: Freeze):
        result = call(lambda: store().freeze(identity, request.reviewer, request.note, request.expected_revisions))
        return {'id': result['id'], 'fingerprint': result['fingerprint'], 'notice': '已冻结。后续编辑不会改动这个版本；修订后请冻结新版本并重评所有方案。'}

    return router


def create_review_app(path=DEFAULT_REVIEW_DB):
    app = FastAPI(title='检索标注审核', docs_url=None, redoc_url=None)
    app.mount('/static', StaticFiles(directory=ROOT / 'web/static'), name='static')
    app.include_router(review_router(Path(path)))
    return app


app = create_review_app()
