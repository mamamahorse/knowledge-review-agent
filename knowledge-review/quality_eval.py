"""Bounded, isolated evaluation of existing services, never the web app's global DB.

Reference labels enter comparisons AFTER generation. Smoke responses are deliberately
controlled fixtures: they verify plumbing, and do not measure model semantics.
"""

import argparse
import json
import re
import time
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import model_client
import practice_model
from context import citation_is_visible
from evaluation_cases import CORPUS, load_suite, remap_citations, sources_for
from evaluation_report import grade_check, relation_checks, review_run, write_report
from generation_budget import GenerationBudget
from learning_store import LearningStore
from learning_types import LearningDraft
from outline_context import DocumentBrief, plan_outline, request_diagnostic
from outline_group_store import get_run, rows
from outline_grouping import GroupDraft, GroupProposal, run_grouping, start_grouping
from outline_service import (check_request, excerpt, public_outline, run_outline,
                             scope_fingerprint, section_manifest, validate_learning)
from outline_store import OutlineStore
from practice_model import QuestionDraft, SemanticGradeDraft, validate_question
from practice_service import grade_pending_answer

OUTPUT_ROOT = Path(__file__).resolve().parent / '.data' / 'evaluations'
REAL_REQUEST = practice_model._request_structured
STAGES = ('extraction', 'grouping', 'grading')


class EvaluationCallLimit(RuntimeError):
    pass


def safe_error(exc):
    # Provider messages can contain URLs, credentials or request content.
    return f'{type(exc).__name__}：未完成；详情请核对阶段状态和预算，未导出服务错误原文。'


def controlled_response(case, aliases, fixture_ids, example, messages, schema):
    if schema is SemanticGradeDraft:
        assessment = example.expected_assessments[0]
        return schema(covered_indices=example.expected_covered,
            missing_indices=[i for i in range(1, len(example.question.rubric_points) + 1) if i not in example.expected_covered],
            answer_quotes=[example.answer[:80]], feedback='受控接口样例，不代表真实模型判断。',
            next_action='finish', teaching_note='', follow_up=None,
            overall_assessment=assessment, suggested_rating='Good' if assessment == 'meets_standard' else 'Again',
            rating_reason='受控返回，只验证数据链路。', target_fit=example.expected_target_fits[0], dimension_observations=[])
    data = json.loads(messages[1]['content'])
    if issubclass(schema, GroupDraft):
        suggestions = []
        present = {c['id'] for c in data['candidates']}
        for relation in case.relations:
            if not relation.expected:
                continue
            kind = relation.expected[0]
            ids = [fixture_ids[a] for a in relation.pair]
            if not set(ids) <= present:
                continue
            suggestions.append(GroupProposal(kind=kind, candidate_ids=ids,
                name='受控关系', reason=relation.reason, canonical_id=ids[0] if kind == 'duplicate_targets' else None))
        receipt = {'checked_indices': [p['index'] for p in data['comparisons']], 'deferred_indices': []} if 'comparisons' in data else {}
        return schema(suggestions=suggestions[:4], note='受控返回，不代表归组质量。', **receipt)
    if schema is DocumentBrief and 'overviews' in data:
        points = [p for overview in data['overviews'] for p in overview['points']]
        return schema(points=points[:6], unresolved=[])
    # Use only the source ranges actually supplied to this request.
    text = data.get('sources', '')
    chunks = []
    for sid, start, end in re.findall(r'Source ID: ([^\n]+).*?Visible lines: L(\d+)-L(\d+)', text, re.S):
        source = next(s for s in aliases.values() if s.source_id == sid)
        chunks.append(excerpt(source, int(start), int(end)))
    if schema is DocumentBrief:
        citations = [{'source_id': c.source_id, 'line_start': c.line_start, 'line_end': min(c.line_start + 1, c.line_end)} for c in chunks[:1]]
        return schema(points=[{'text': '受控概述，仅验证原文指针传递。', 'citations': citations}] if citations else [], unresolved=[])
    # Outline requests call this field "sources" too, including the full-document path.
    proposals = [LearningDraft.model_validate(remap_citations(d.model_dump(), aliases)) for d in case.candidate_fixtures.values()]
    visible = [d for d in proposals if all(citation_is_visible(c.source_id, c.line_start, c.line_end, chunks) for c in d.knowledge.citations)]
    limit = 6 if schema.__name__ == 'FullOutlineDraft' else 4
    return schema(proposals=[d.model_dump() for d in visible[:limit]], note='受控提取，不代表知识完整性。')


def fixed_outline(store, case, sources, snapshots, aliases, language):
    """Create reviewed-shape fixtures without running extraction; grouping is isolated."""
    sections = section_manifest(sources, snapshots)
    batches = [{'section_id': next(s['id'] for s in sections if s['source_id'] == source.source_id),
                'source_id': source.source_id, 'line_start': 1, 'line_end': len(source.text.splitlines())} for source in sources]
    identity = store.create('root', 'group-fixtures', case.objective, language,
                            scope_fingerprint(snapshots), snapshots, sections, batches)
    drafts = [(name, LearningDraft.model_validate(remap_citations(d.model_dump(), aliases)))
              for name, d in case.candidate_fixtures.items()]
    store.start(identity)
    names = []
    while (batch := store.claim_batch(identity)) is not None:
        source = next(s for s in sources if s.source_id == batch['source_id'])
        visible = [excerpt(s, 1, len(s.text.splitlines())) for s in sources]
        selected = [(name, d) for name, d in drafts if d.knowledge.citations[0].source_id == source.source_id]
        for _, d in selected:
            validate_learning(d, visible)
        names.extend(name for name, _ in selected)
        ranges = [{'source_id': c.source_id, 'line_start': c.line_start, 'line_end': c.line_end} for c in visible]
        store.finish_batch(batch, [d for _, d in selected], '固定输入，仅供归组评测。', visible=ranges)
    store.settle(identity)
    candidates = store.get(identity)['candidates']
    return identity, dict(zip(names, [c['id'] for c in candidates]))


def extraction(store, case, sources, snapshots, current, language, budget):
    sections, batches, plan = plan_outline(sources, snapshots, case.objective, language, budget)
    identity = store.create('root', 'extract', case.objective, language,
                            scope_fingerprint(snapshots), snapshots, sections, batches, plan=plan)
    run_outline(store, identity, current)
    result = public_outline(store, identity, current)
    result['analyzed_lines'] = sum(c['analyzed_lines'] for c in result['coverage'])
    result['total_lines'] = sum(c['total_lines'] for c in result['coverage'])
    # Original service has already validated against each persisted actual visible range.
    result['citation_check'] = 'validated_by_existing_service_against_actual_visible_ranges'
    if result['status'] != 'completed':
        result['error'] = '提取未完成；已完成的候选和概述保留在隔离数据库中，不计算完整语义指标。'
    return result


def grouping(store, case, sources, snapshots, aliases, current, language, fixture_ids):
    identity, ids = fixed_outline(store, case, sources, snapshots, aliases, language)
    fixture_ids.update(ids)
    # The explicit outer evaluation limit is authoritative; avoid an unrelated
    # product pause before that limit. No automatic retry of failed generations.
    run_id = start_grouping(store, identity, 'compare', language, list(ids.values()), current, call_limit=200)
    run_grouping(store, run_id, current)
    result = get_run(store, run_id)
    suggestions = rows(store, identity)[1]
    from outline_grouping import public_grouping
    integration = next(r for r in public_grouping(store, identity)['runs'] if r['id'] == run_id)
    return {'status': result['status'], 'error': result['error'], 'suggestions': suggestions,
            'integration': integration,
            'checks': relation_checks([r.model_dump() for r in case.relations], suggestions, {v: k for k, v in ids.items()}, result['status'] == 'completed')}


def grading(store, example, sources, snapshots, aliases, language):
    question = QuestionDraft.model_validate(remap_citations(example.question.model_dump(), aliases))
    visible = [excerpt(s, 1, len(s.text.splitlines())) for s in sources]
    validate_question(question, sources, visible)
    session = store.create(question.objective, sources[0].source_id, 1, len(sources[0].text.splitlines()),
        snapshots, [{'source_id': c.source_id, 'line_start': c.line_start, 'line_end': c.line_end} for c in visible],
        question, response_language=language)
    store.submit_answer(session, example.answer)
    result = {'id': example.id, 'answer': example.answer, 'session_id': session, 'status': 'failed'}
    try:
        grade_pending_answer(store, session, response_language=language)
        draft = store.get(session)['questions'][0]['grade']
        result.update(status='completed', result=draft.model_dump(), checks=grade_check(example.model_dump(), draft.model_dump()))
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        result['error'] = safe_error(exc)
    return result


def run_suite(mode='smoke', case_ids=None, stage='all', language='zh', max_calls=128, output_root=OUTPUT_ROOT):
    if mode not in ('smoke', 'live') or stage not in ('all', *STAGES) or max_calls < 1:
        raise ValueError('评测模式、阶段或调用上限不合法。')
    suite, digest = load_suite()
    if case_ids and not set(case_ids) <= {c.id for c in suite.cases}:
        raise ValueError('未知评测 case ID。')
    cases = [c for c in suite.cases if not case_ids or c.id in case_ids]
    directory = output_root / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{mode}-{uuid4().hex[:8]}"
    directory.mkdir(parents=True, exist_ok=False)
    code_files = ('quality_eval.py', 'evaluation_cases.py', 'evaluation_report.py', 'outline_service.py',
                  'outline_context.py', 'outline_grouping.py', 'outline_group_plan.py', 'outline_group_steps.py',
                  'outline_integration.py', 'practice_model.py', 'structured_output.py', 'generation_budget.py')
    report = {'mode': mode, 'language': language, 'suite_revision': suite.revision, 'suite_fingerprint': digest,
        'reference_status': suite.reference_status, 'model': 'controlled-fixture', 'max_calls': max_calls,
        'code_sha256': {n: sha256((CORPUS.parent.parent / n).read_bytes()).hexdigest() for n in code_files},
        'calls': [], 'cases': [], 'call_limit_reached': False}
    state = {'case': None, 'stage': None, 'example': None, 'aliases': {}, 'fixture_ids': {}}
    budget = GenerationBudget()
    with ExitStack() as stack:
        if mode == 'smoke':
            for name in ('model_client.model_settings', 'outline_service.model_settings'):
                stack.enter_context(patch(name, return_value=('controlled-fixture', {})))
            stack.enter_context(patch.object(GenerationBudget, 'from_env', return_value=budget))
        else:
            report['model'], _ = model_client.model_settings()
            budget = GenerationBudget.from_env()

        def request(messages, schema):
            if len(report['calls']) >= max_calls:
                report['call_limit_reached'] = True
                raise EvaluationCallLimit('达到本次逻辑任务调用上限。')
            call = {'case': state['case'].id, 'stage': state['stage'], 'example': getattr(state['example'], 'id', None),
                    'schema': schema.__name__, 'status': 'running', 'seconds': 0.0}
            report['calls'].append(call)
            started = time.perf_counter()
            try:
                check_request(budget, messages, schema)
                call['diagnostic'] = request_diagnostic(budget, messages, schema)
                # These requests contain only versioned synthetic data, no reference labels in live mode.
                call['messages'] = messages
                if mode == 'smoke':
                    value = controlled_response(state['case'], state['aliases'], state['fixture_ids'], state['example'], messages, schema)
                else:
                    value = REAL_REQUEST(messages, schema)
                call['status'] = 'returned'  # Publication validation still belongs to the service.
                return value
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                call.update(status='failed', error=safe_error(exc), error_type=type(exc).__name__,
                            cause_type=type(exc.__cause__).__name__ if exc.__cause__ else None)
                raise
            finally:
                call['seconds'] = round(time.perf_counter() - started, 3)
                (directory / 'calls.json').write_text(json.dumps(report['calls'], ensure_ascii=False, indent=2), encoding='utf-8')

        for name in ('practice_model._request_structured', 'outline_service._request_structured', 'outline_grouping._request_structured'):
            stack.enter_context(patch(name, side_effect=request))
        for case in cases:
            sources, snapshots, aliases = sources_for(case, CORPUS.parent)
            current = lambda collection, saved=snapshots: saved
            store = LearningStore(directory / f'{case.id}.sqlite3')
            outlines = OutlineStore(store)
            state.update(case=case, aliases=aliases, fixture_ids={}, example=None)
            output = {'id': case.id, 'title': case.title, 'files': case.files,
                'reference_points': [p.model_dump() for p in case.reference_points], 'review_checks': case.review_checks,
                'extraction': {'status': 'not_run'}, 'grouping': {'status': 'not_run'}, 'grading': []}
            report['cases'].append(output)
            for phase in STAGES:
                if stage not in ('all', phase):
                    continue
                state.update(stage=phase, example=None)
                write_report(directory, report)
                if phase == 'grading':
                    for example in case.grades:
                        state['example'] = example
                        if len(report['calls']) >= max_calls:
                            report['call_limit_reached'] = True
                            output['grading'].append({'id': example.id, 'answer': example.answer, 'status': 'not_run', 'error': '达到调用上限。'})
                        else:
                            output['grading'].append(grading(store, example, sources, snapshots, aliases, language))
                        write_report(directory, report)
                    continue
                if len(report['calls']) >= max_calls:
                    report['call_limit_reached'] = True
                    output[phase]['error'] = '达到调用上限；未开始本阶段。'
                    continue
                try:
                    if phase == 'extraction':
                        output[phase] = extraction(outlines, case, sources, snapshots, current, language, budget)
                        if report['call_limit_reached'] and output[phase]['status'] != 'completed':
                            output[phase]['error'] = '达到调用上限；部分概述/候选保留，不能视为完整提取。'
                    else:
                        output[phase] = grouping(outlines, case, sources, snapshots, aliases, current, language, state['fixture_ids'])
                except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                    output[phase] = {'status': 'failed', 'error': safe_error(exc)}
            with closing(store._connect()) as db:
                output['learning_records'] = {name: db.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                                              for name in ('review_items', 'review_events')}
            write_report(directory, report)
    report['completed'] = all(c[p]['status'] == 'completed' for c in report['cases'] for p in ('extraction', 'grouping') if stage in ('all', p)) and all(
        g['status'] == 'completed' for c in report['cases'] for g in c['grading'])
    write_report(directory, report)
    return directory, report


def main(argv=None):
    parser = argparse.ArgumentParser(description='合成资料质量评测；默认只校验样例，不调用模型。')
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('validate')
    run = sub.add_parser('run')
    run.add_argument('--mode', choices=('smoke', 'live'), default='smoke')
    run.add_argument('--case', action='append', dest='cases')
    run.add_argument('--stage', choices=('all', *STAGES), default='all')
    run.add_argument('--language', choices=('zh', 'en'), default='zh')
    run.add_argument('--max-calls', type=int)
    review = sub.add_parser('review')
    review.add_argument('directory', type=Path)
    review.add_argument('review_file', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command in (None, 'validate'):
            suite, _ = load_suite()
            print(f'样例校验通过：{len(suite.cases)} 组资料，{sum(len(c.grades) for c in suite.cases)} 组评分；参考标注待人工确认。')
            return 0
        if args.command == 'review':
            review_run(args.directory, args.review_file)
            print(f'审核结果：{args.directory / "reviewed.md"}')
            return 0
        directory, report = run_suite(args.mode, args.cases, args.stage, args.language,
                                      args.max_calls if args.max_calls is not None else (8 if args.mode == 'live' else 128))
        print(f'运行记录：{directory / "report.md"}；任务调用 {len(report["calls"])}/{report["max_calls"]}；完成：{report["completed"]}')
        return 0 if report['completed'] else 2
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        print(safe_error(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
