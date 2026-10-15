"""Versioned public retrieval fixtures and explicit human review, isolated from app data."""

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from chunking import chunk_source, CHUNK_ALGORITHM
from sources import load_sources

ROOT = Path(__file__).resolve().parent
SUITE = ROOT / 'evals/retrieval/suite.json'
DEFAULT_REVIEW_DB = ROOT / '.data/retrieval-benchmark/review.sqlite3'


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def load_suite(path=SUITE):
    suite = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    names = suite['sources']
    if len(names) != len(set(names)) or any(not re.fullmatch(r'[a-z0-9-]+\.md', n) for n in names):
        raise ValueError('评测资料名称重复或不合法。')
    sources = load_sources([Path(path).parent / 'corpus' / n for n in names])
    by_name = {s.path.name: s for s in sources}
    ids, families = set(), {}
    for query in suite['queries']:
        if query['id'] in ids or not query['text'].strip() or query['split'] not in ('dev', 'test'):
            raise ValueError('查询身份、内容或分区不合法。')
        ids.add(query['id'])
        if families.setdefault(query['family'], query['split']) != query['split']:
            raise ValueError('同一问题族不能跨开发/测试分区。')
        if not query['scope'] or not set(query['scope']) <= set(names):
            raise ValueError('查询资料范围不合法。')
        if type(query['answerable']) is not bool or query['answerable'] != bool(query['evidence_groups']):
            raise ValueError('可回答性和必要证据组不一致。')
        groups = query['evidence_groups']
        if len({g['id'] for g in groups}) != len(groups):
            raise ValueError('证据组身份重复。')
        for entry in [*query['annotations'], *(e for g in groups for e in g['evidence'])]:
            if entry['file'] not in query['scope']:
                raise ValueError('标注扩大了查询资料权限。')
            lines = by_name[entry['file']].text.splitlines()
            if not 1 <= entry['line_start'] <= entry['line_end'] <= len(lines):
                raise ValueError('证据行号越界。')
            if 'grade' in entry and (type(entry['grade']) is not int or entry['grade'] not in (0, 1, 2)):
                raise ValueError('相关性等级不合法。')
    return suite, sources


def make_manifest(suite, sources, embedder):
    # The exact production preprocessor defines evaluation units, not raw chunks.
    chunks = embedder.prepare_chunks([c for s in sources for c in chunk_source(s)])
    names = {s.source_id: s.path.name for s in sources}
    manifest = {'schema': 1, 'suite': suite, 'chunking': {'algorithm': CHUNK_ALGORITHM, 'max_chars': 1200, 'min_chars': 100},
        'embedding': embedder.cache_profile(),
        'sources': [{'file': s.path.name, 'source_id': s.source_id, 'text': s.text,
                     'text_hash': sha256(s.text.encode()).hexdigest()} for s in sources],
        'chunks': [{'id': c.chunk_id, 'source_id': c.source_id, 'file': names[c.source_id],
                    'line_start': c.line_start, 'line_end': c.line_end, 'text': c.text,
                    'headings': c.section_paths or (c.heading_path,)} for c in chunks]}
    manifest['fingerprint'] = digest(manifest)
    return manifest


def covering_bundles(chunks, evidence):
    """Line coverage only suggests identities; human approval supplies semantics."""
    eligible = [c for c in chunks if c['file'] == evidence['file'] and
                c['line_start'] <= evidence['line_end'] and c['line_end'] >= evidence['line_start']]
    single = [[c['id']] for c in eligible if c['line_start'] <= evidence['line_start'] and c['line_end'] >= evidence['line_end']]
    if single:
        return single
    covered = {line for c in eligible for line in range(max(c['line_start'], evidence['line_start']), min(c['line_end'], evidence['line_end']) + 1)}
    if covered != set(range(evidence['line_start'], evidence['line_end'] + 1)):
        raise ValueError('预处理后片段没有完整覆盖初标证据。')
    return [[c['id'] for c in eligible]]


def initial_review(manifest, query):
    chunks = [c for c in manifest['chunks'] if c['file'] in query['scope']]
    grades = {c['id']: None for c in chunks}
    notes = {}
    for annotation in query['audit'].get('suggested_annotations', query['annotations']):
        for bundle in covering_bundles(chunks, annotation):
            for identity in bundle:
                grades[identity] = max(grades[identity] or 0, annotation['grade'])
                notes[identity] = annotation['note']
    groups = []
    for group in query['evidence_groups']:
        # Pilot evidence groups each describe one independent fact.
        if len(group['evidence']) != 1:
            raise ValueError('每个初标证据组须有一个原文范围；复杂替代组合在审核中编辑。')
        groups.append({'id': group['id'], 'label': group['label'], 'bundles': covering_bundles(chunks, group['evidence'][0])})
    return {'answerable': query['answerable'], 'status': 'pending', 'scope_checked': False, 'note': '',
            'judgments': [{'chunk_id': identity, 'grade': grade, 'note': notes.get(identity, '')} for identity, grade in grades.items()],
            'groups': groups, 'issue_resolution': ''}


def validate_review(manifest, query, review):
    if review['status'] not in ('pending', 'approved', 'excluded', 'auto_accepted') or type(review['answerable']) is not bool or type(review['scope_checked']) is not bool:
        raise ValueError('审核状态或确认标志不合法。')
    if review.get('origin', 'user') not in ('user', 'agent'):
        raise ValueError('标注决定来源不合法。')
    if review['status'] == 'auto_accepted' and (review.get('origin') != 'agent' or not review.get('audit_id')):
        raise ValueError('自动采用必须绑定完整 Agent 初审记录，不能冒充用户批准。')
    chunks = {c['id'] for c in manifest['chunks'] if c['file'] in query['scope']}
    judgments = review['judgments']
    if len({j['chunk_id'] for j in judgments}) != len(judgments) or {j['chunk_id'] for j in judgments} != chunks:
        raise ValueError('审核必须保留全部范围内片段身份，不能扩大范围或遗漏。')
    if any(j['grade'] is not None and (type(j['grade']) is not int or j['grade'] not in (0, 1, 2)) for j in judgments):
        raise ValueError('相关性等级必须为未判断或 0/1/2。')
    grades = {j['chunk_id']: j['grade'] for j in judgments}
    groups = review['groups']
    if len({g['id'] for g in groups}) != len(groups):
        raise ValueError('证据组身份重复。')
    for group in groups:
        if not group['label'].strip() or not group['bundles'] or any(not b or len(b) != len(set(b)) or not set(b) <= chunks for b in group['bundles']):
            raise ValueError('证据组需包含合法且非空的替代片段组合。')
    if review['status'] == 'excluded' and not review['note'].strip():
        raise ValueError('排除样例必须记录原因。')
    if review['status'] in ('approved', 'auto_accepted'):
        if not review['scope_checked'] or any(j['grade'] is None for j in judgments):
            raise ValueError('请核对整个许可范围并判定所有片段；未判断不能自动算无关。')
        if query['audit']['status'] in ('error', 'disputed') and not review['issue_resolution'].strip():
            raise ValueError('请记录初审错误或争议的处理理由。')
        positives = {identity for identity, grade in grades.items() if grade and grade > 0}
        if review['answerable'] != bool(positives) or review['answerable'] != bool(groups):
            raise ValueError('可回答性、相关片段和必要证据组需一致；不足信息说明不算具体答案证据。')
        if any(not set(bundle) <= positives for group in groups for bundle in group['bundles']):
            raise ValueError('必要证据组中的片段必须标为相关。')


class ReviewStore:
    def __init__(self, path=DEFAULT_REVIEW_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS benchmarks(id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, manifest_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reviews(benchmark_id TEXT NOT NULL, query_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    payload_json TEXT NOT NULL, reviewed_at TEXT NOT NULL, PRIMARY KEY(benchmark_id,query_id,revision));
                CREATE TABLE IF NOT EXISTS freezes(id TEXT PRIMARY KEY, benchmark_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    payload_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(benchmark_id,fingerprint));
                CREATE TABLE IF NOT EXISTS agent_audits(id TEXT PRIMARY KEY, benchmark_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
            ''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def create(self, manifest):
        if manifest['fingerprint'] != digest({k: v for k, v in manifest.items() if k != 'fingerprint'}):
            raise ValueError('标注集指纹不一致。')
        with closing(self.connect()) as db, db:
            identity = uuid4().hex
            db.execute('INSERT OR IGNORE INTO benchmarks VALUES (?,?,?)', (identity, manifest['fingerprint'], json.dumps(manifest, ensure_ascii=False)))
            return db.execute('SELECT id FROM benchmarks WHERE fingerprint=?', (manifest['fingerprint'],)).fetchone()[0]

    def list(self):
        with closing(self.connect()) as db:
            rows = db.execute('SELECT id,manifest_json FROM benchmarks ORDER BY rowid DESC').fetchall()
        return [{'id': r['id'], 'name': json.loads(r['manifest_json'])['suite']['revision']} for r in rows]

    def manifest(self, identity):
        with closing(self.connect()) as db:
            row = db.execute('SELECT manifest_json FROM benchmarks WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise KeyError('找不到这个检索标注集，请先准备样例。')
        return json.loads(row[0])

    @staticmethod
    def query(manifest, query_id):
        return next((q for q in manifest['suite']['queries'] if q['id'] == query_id), None)

    def review(self, identity, query_id):
        manifest = self.manifest(identity)
        query = self.query(manifest, query_id)
        if query is None:
            raise KeyError('找不到查询。')
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM reviews WHERE benchmark_id=? AND query_id=? ORDER BY revision DESC LIMIT 1', (identity, query_id)).fetchone()
        return {'revision': row['revision'] if row else 0, 'payload': json.loads(row['payload_json']) if row else initial_review(manifest, query)}

    def save(self, identity, query_id, payload, expected_revision):
        manifest = self.manifest(identity)
        query = self.query(manifest, query_id)
        if query is None:
            raise KeyError('找不到查询。')
        payload = {**payload, 'origin': 'user'}
        if payload['status'] == 'auto_accepted':
            raise ValueError('用户保存请使用批准、待处理或排除；自动采用仅由完整初审导入。')
        validate_review(manifest, query, payload)
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                revision = db.execute('SELECT COALESCE(MAX(revision),0) FROM reviews WHERE benchmark_id=? AND query_id=?', (identity, query_id)).fetchone()[0]
                if revision != expected_revision:
                    raise ValueError('审核已被其他页面更新，请刷新后再保存。')
                previous = db.execute('SELECT payload_json FROM reviews WHERE benchmark_id=? AND query_id=? AND revision=?',
                                      (identity, query_id, revision)).fetchone()
                # User changes inherit the actual audit pointer, not a client claim.
                payload.pop('audit_id', None)
                if previous and json.loads(previous[0]).get('audit_id'):
                    payload['audit_id'] = json.loads(previous[0])['audit_id']
                db.execute('INSERT INTO reviews VALUES (?,?,?,?,?)', (identity, query_id, revision+1, json.dumps(payload, ensure_ascii=False), now()))
                db.commit()
            except Exception:
                db.rollback()
                raise
        # Return this committed revision, even if another editor writes next.
        return {'revision': revision + 1, 'payload': payload}

    def adopt_agent_audit(self, identity, audit, expected_agent_revisions=None, *, resolve_agent_disputes=False):
        """Apply a complete explicit source audit, never infer unjudged grades as zero.

        Normally only untouched queries are initialized. Explicit corrections can
        append to auto-accepted Agent rows using CAS. Explicitly authorized Agent
        dispute resolution also appends to its own pending rows; user rows and
        all previous revisions/freezes remain unchanged.
        """
        manifest = self.manifest(identity)
        if audit['benchmark_fingerprint'] != manifest['fingerprint']:
            raise ValueError('初审与资料/分块/问题版本不一致。')
        if not audit.get('version', '').strip() or not audit.get('method', '').strip() or audit.get('source_hashes') != {
                s['file']: s.get('text_hash', sha256(s['text'].encode()).hexdigest()) for s in manifest['sources']}:
            raise ValueError('初审须绑定完整资料哈希、方法和版本。')
        cases = audit['cases']
        if len(cases) != len(manifest['suite']['queries']) or {c['query_id'] for c in cases} != {q['id'] for q in manifest['suite']['queries']}:
            raise ValueError('初审必须逐条覆盖整个查询集。')
        corrections = expected_agent_revisions or {}
        if not set(corrections) <= {q['id'] for q in manifest['suite']['queries']} or any(
                type(v) is not int or v < 1 for v in corrections.values()):
            raise ValueError('初审修正须指定合法查询及预期版本。')
        audit_id, prepared = digest(audit), []
        for case in cases:
            query = self.query(manifest, case['query_id'])
            if case['decision'] not in ('confident', 'needs_user', 'excluded') or not case['reason'].strip():
                raise ValueError('初审必须记录确定/待用户处理及理由。')
            payload = {**case['review'], 'status': {'confident':'auto_accepted','needs_user':'pending','excluded':'excluded'}[case['decision']],
                       'origin': 'agent', 'audit_id': audit_id}
            if case['decision']=='excluded':
                payload['note'] = case['reason'] + (' '+payload['note'] if payload['note'].strip() else '')
            if not payload['scope_checked'] or any(j['grade'] is None for j in payload['judgments']):
                raise ValueError('初审须明确判定整个许可范围；不能自动把 null 改成 0。')
            validate_review(manifest, query, payload)
            # Pending disputes still need a coherent, fully checked proposal.
            proposal = {**payload, 'status': 'approved', 'issue_resolution': payload['issue_resolution'] or case['reason']}
            if case['decision']!='excluded':
                validate_review(manifest, query, proposal)
            allowed = {j['chunk_id'] for j in payload['judgments']}
            if not set(case['focus_chunk_ids']) <= allowed:
                raise ValueError('争议焦点不能扩大许可范围。')
            prepared.append((case['query_id'], payload))
        result = {'auto_accepted': 0, 'needs_user': 0, 'excluded':0, 'preserved_existing': 0, 'corrected_agent':0, 'audit_id': audit_id}
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            for query_id, revision in corrections.items():
                row = db.execute('SELECT revision,payload_json FROM reviews WHERE benchmark_id=? AND query_id=? ORDER BY revision DESC LIMIT 1',
                                 (identity,query_id)).fetchone()
                payload = json.loads(row['payload_json']) if row else {}
                decision = next(c['decision'] for c in cases if c['query_id']==query_id)
                resolvable = resolve_agent_disputes and payload.get('status')=='pending' and decision in ('confident','excluded')
                if not row or row['revision'] != revision or payload.get('origin') != 'agent' or (payload['status'] != 'auto_accepted' and not resolvable):
                    raise ValueError('只能追加修正预期版本的自动初审；用户决定、草稿或待处理分歧须保留。')
            db.execute('INSERT OR IGNORE INTO agent_audits VALUES (?,?,?,?)', (audit_id, identity, json.dumps(audit, ensure_ascii=False), now()))
            for query_id, payload in prepared:
                if query_id in corrections:
                    db.execute('INSERT INTO reviews VALUES (?,?,?,?,?)', (identity,query_id,corrections[query_id]+1,json.dumps(payload,ensure_ascii=False),now()))
                    result['corrected_agent'] += 1
                    continue
                if db.execute('SELECT 1 FROM reviews WHERE benchmark_id=? AND query_id=?', (identity, query_id)).fetchone():
                    result['preserved_existing'] += 1
                    continue
                db.execute('INSERT INTO reviews VALUES (?,?,?,?,?)', (identity, query_id, 1, json.dumps(payload, ensure_ascii=False), now()))
                result[{'auto_accepted':'auto_accepted','pending':'needs_user','excluded':'excluded'}[payload['status']]] += 1
        return result

    def agent_audit(self, identity, audit_id):
        if not audit_id:
            return None
        with closing(self.connect()) as db:
            row = db.execute('SELECT payload_json FROM agent_audits WHERE id=? AND benchmark_id=?', (audit_id, identity)).fetchone()
        if row is None:
            raise ValueError('初审记录缺失，不能使用该标注。')
        audit = json.loads(row[0])
        if digest(audit) != audit_id:
            raise ValueError('初审记录指纹不一致。')
        return audit

    def agent_audit_case(self, identity, query_id, audit_id):
        audit = self.agent_audit(identity, audit_id)
        return next((c for c in audit['cases'] if c['query_id'] == query_id), None) if audit else None

    def freeze(self, identity, reviewer, note, expected_revisions):
        if not reviewer.strip() or not note.strip():
            raise ValueError('冻结须填写审核人和版本说明。')
        manifest = self.manifest(identity)
        if set(expected_revisions) != {q['id'] for q in manifest['suite']['queries']}:
            raise ValueError('冻结须提供整个标注集的审核版本。')
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                cases = []
                for query in manifest['suite']['queries']:
                    row = db.execute('SELECT * FROM reviews WHERE benchmark_id=? AND query_id=? ORDER BY revision DESC LIMIT 1', (identity, query['id'])).fetchone()
                    if row is None or row['revision'] != expected_revisions.get(query['id']):
                        raise ValueError('存在未审核或已变化的样例，请刷新。')
                    payload = json.loads(row['payload_json'])
                    validate_review(manifest, query, payload)
                    if payload['status'] not in ('approved', 'excluded', 'auto_accepted'):
                        raise ValueError('请审核或明确排除每条样例，待处理标注不能冻结。')
                    if payload.get('audit_id'):
                        self.agent_audit_case(identity, query['id'], payload['audit_id'])
                    cases.append({'query_id': query['id'], 'revision': row['revision'], **payload})
                if not any(c['status'] in ('approved', 'auto_accepted') and self.query(manifest, c['query_id'])['split'] == 'test' and c['answerable'] for c in cases):
                    raise ValueError('至少需要一条可回答的已审核测试查询。')
                payload = {'benchmark_id': identity, 'benchmark_fingerprint': manifest['fingerprint'],
                           'reviewer': reviewer.strip(), 'note': note.strip(), 'cases': cases}
                audit_ids = {c['audit_id'] for c in cases if c.get('audit_id')}
                if audit_ids:
                    payload['agent_audits'] = {audit_id: self.agent_audit(identity, audit_id) for audit_id in sorted(audit_ids)}
                fingerprint = digest(payload)
                frozen_id = uuid4().hex
                db.execute('INSERT OR IGNORE INTO freezes VALUES (?,?,?,?,?)', (frozen_id, identity, fingerprint, json.dumps(payload, ensure_ascii=False), now()))
                row = db.execute('SELECT id FROM freezes WHERE benchmark_id=? AND fingerprint=?', (identity, fingerprint)).fetchone()
                db.commit()
                return self.frozen(row[0])
            except Exception:
                db.rollback()
                raise

    def frozen(self, identity):
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM freezes WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise KeyError('找不到冻结版本。')
        return {'id': row['id'], 'fingerprint': row['fingerprint'], **json.loads(row['payload_json'])}

    def freezes(self, identity):
        with closing(self.connect()) as db:
            return [dict(r) for r in db.execute('SELECT id,fingerprint,created_at FROM freezes WHERE benchmark_id=? ORDER BY rowid DESC', (identity,))]
