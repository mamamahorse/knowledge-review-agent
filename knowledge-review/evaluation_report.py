"""Separate mechanical checks, draft-label agreement, and explicit semantic review."""

import json
from hashlib import sha256
from itertools import combinations


def fingerprint(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def relation_checks(reference, groups, aliases, completed=True):
    predictions = {}
    for group in groups:
        proposal = group['proposal']
        members = [aliases[c] for c in proposal['candidate_ids']]
        for pair in combinations(sorted(members), 2):
            predictions.setdefault(pair, set()).add(proposal['kind'])
    annotated = {tuple(sorted(r['pair'])) for r in reference}
    checks = []
    for r in reference:
        actual = predictions.get(tuple(sorted(r['pair'])), set())
        violation = sorted(actual.intersection(r['forbidden']))
        found = bool(actual.intersection(r['expected'])) if r['expected'] else True
        checks.append({**r, 'actual': sorted(actual), 'forbidden_found': violation,
                       'expected_found': found if completed else None,
                       'agrees_with_draft': found and not violation if completed else None})
    return {'completed': completed, 'labelled_pairs': checks,
            'unlabelled_predicted_pairs': [list(pair) for pair in predictions if pair not in annotated],
            'note': '按候选身份和建议类型比较草案标注；未标注关系不自动算错。'}


def grade_check(reference, grade):
    return {'id': reference['id'], 'reference': reference,
            'assessment_agrees_with_draft': grade['overall_assessment'] in reference['expected_assessments'],
            'target_fit_agrees_with_draft': grade['target_fit'] in reference['expected_target_fits'],
            'rubric_indices_agree_with_draft': sorted(grade['covered_indices']) == sorted(reference['expected_covered']),
            'note': '整体判断和逐标准分类分别比较；不按标准计数重新计算模型等级。'}


def review_template(report):
    cases = []
    for case in report['cases']:
        extraction = case.get('extraction', {})
        if extraction.get('status') == 'not_run':
            continue
        cases.append({'id': case['id'],
            'points': [{'id': point['id'], 'decision': 'unreviewed', 'candidate_ids': [], 'note': ''}
                       for point in case['reference_points']],
            'candidates': [{'id': c['id'], 'acceptable': None, 'conditions_correct': None,
                            'evidence_supports_claim': None, 'note': ''} for c in extraction.get('candidates', [])],
            'duplicate_sets': [], 'note': ''})
    return {'artifact_fingerprint': fingerprint(report), 'reviewer': '', 'cases': cases}


def apply_review(report, review):
    if review.get('artifact_fingerprint') != fingerprint(report):
        raise ValueError('审核文件与本次结果指纹不一致，不能套用旧审核。')
    if not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip():
        raise ValueError('请填写审核人或明确的审核方式，例如 human 或 controlled-test。')
    template = review_template(report)
    expected_cases = {c['id']: c for c in template['cases']}
    actual_cases = review.get('cases', [])
    if len(actual_cases) != len(expected_cases) or {c['id'] for c in actual_cases} != expected_cases.keys():
        raise ValueError('审核 case 身份重复、遗漏或未知。')
    findings = []
    for case in actual_cases:
        expected = expected_cases[case['id']]
        output = next(c for c in report['cases'] if c['id'] == case['id'])
        candidate_ids = {c['id'] for c in expected['candidates']}
        for field in ('points', 'candidates'):
            ids = [r['id'] for r in case[field]]
            if len(ids) != len(set(ids)) or set(ids) != {r['id'] for r in expected[field]}:
                raise ValueError('审核参考点或候选 ID 重复、遗漏或未知。')
        matched, point_reviewed = 0, 0
        for point in case['points']:
            decision = point['decision']
            members = point['candidate_ids']
            if decision not in ('unreviewed', 'matched', 'missing') or len(set(members)) != len(members) or not set(members) <= candidate_ids:
                raise ValueError('参考点审核决定或匹配候选无效。')
            if (decision == 'matched' and not members) or (decision != 'matched' and members):
                raise ValueError('只有 matched 参考点可以且必须关联候选。')
            matched += decision == 'matched'
            point_reviewed += decision != 'unreviewed'
        acceptable, candidate_reviewed = 0, 0
        for candidate in case['candidates']:
            flags = [candidate[k] for k in ('acceptable', 'conditions_correct', 'evidence_supports_claim')]
            if any(x is not None and type(x) is not bool for x in flags):
                raise ValueError('候选审核字段只能是 true、false 或 null。')
            acceptable += candidate['acceptable'] is True
            candidate_reviewed += candidate['acceptable'] is not None
        seen = set()
        duplicate_members = set()
        for members in case['duplicate_sets']:
            key = tuple(sorted(members))
            if len(set(members)) < 2 or len(set(members)) != len(members) or not set(members) <= candidate_ids or key in seen or duplicate_members.intersection(members):
                raise ValueError('重复候选集合含未知、重复或重叠身份。')
            seen.add(key)
            duplicate_members.update(members)
        completed = output.get('extraction', {}).get('status') == 'completed'
        measurable = completed and report['mode'] == 'live'
        findings.append({'id': case['id'], 'pipeline_completed': completed, 'matched_points': matched,
            'reviewed_points': point_reviewed, 'reference_points': len(case['points']),
            'reviewed_candidates': candidate_reviewed, 'candidates': len(candidate_ids),
            'reference_recall': matched / len(case['points']) if measurable and point_reviewed == len(case['points']) else None,
            'candidate_precision': acceptable / len(candidate_ids) if measurable and candidate_ids and candidate_reviewed == len(candidate_ids) else None,
            'duplicate_extra_candidates': sum(len(s) - 1 for s in case['duplicate_sets']),
            'condition_errors': sum(c['conditions_correct'] is False for c in case['candidates']),
            'unsupported_claims': sum(c['evidence_supports_claim'] is False for c in case['candidates']),
            'review': case})
    return {'mode': report['mode'], 'reference_status': report['reference_status'],
            'reviewer': review['reviewer'], 'cases': findings,
            'note': '仅反映本次样本与审核；smoke 结果没有模型语义质量含义。不完整提取/审核不计算完整召回率。'}


def markdown_report(report):
    lines = ['# Knowledge Review 质量评测运行记录', '',
             f"- 模式：`{report['mode']}`；语言：`{report['language']}`",
             f"- 样例版本：`{report['suite_revision']}`；参考状态：`{report['reference_status']}`",
             f"- 模型：`{report['model']}`；模型任务调用：{len(report['calls'])}/{report['max_calls']}",
             '- 兼容探测可能产生额外 HTTP 请求；这里统计逻辑任务调用。服务实际 token/账单用量未获取。',
             '- 参考标注是开发草案，尚待人工确认。程序通过不等于语义正确；没有自动评测总分。', '',
             '| 样例 | 提取 | 归组 | 评分已完成 | 候选数 |', '| --- | --- | --- | --- | --- |']
    for case in report['cases']:
        extraction = case['extraction']
        grouping = case['grouping']
        grades = case['grading']
        lines.append(f"| {case['id']} | {extraction['status']} | {grouping['status']} | {sum(g['status'] == 'completed' for g in grades)}/{len(grades)} | {len(extraction.get('candidates', []))} |")
    for case in report['cases']:
        lines.extend(['', f"## {case['title']}", '', f"资料：{', '.join(case['files'])}", '', '### 人工参考与复核事项', ''])
        for point in case['reference_points']:
            refs = ', '.join(f"{c['file']} L{c['line_start']}–L{c['line_end']}" for c in point['evidence'])
            lines.append(f"- `{point['id']}`：{point['statement']}（{refs}）")
        for check in case['review_checks']:
            lines.append(f'- 复核：{check}')
        extraction = case['extraction']
        lines.extend(['', '### 实际提取结果', ''])
        if extraction.get('error'):
            lines.append(extraction['error'])
        lines.append(f"原文行处理进度：{extraction.get('analyzed_lines', 0)}/{extraction.get('total_lines', 0)}，不是知识召回率。")
        for candidate in extraction.get('candidates', []):
            learning = candidate['learning']
            lines.extend(['', f"- `{candidate['id']}`：{learning['knowledge']['name']}",
                          f"  - 知识说明：{learning['knowledge']['statement']}",
                          f"  - 目标：{learning['target']['capability']} / {learning['target']['title']}",
                          f"  - 条件/范围：{learning['target']['scope']}"])
            for criterion in learning['target']['criteria']:
                lines.append(f'  - 判断标准：{criterion}')
            for cite in candidate['citations']:
                lines.append(f"  - {cite['name']} L{cite['line_start']}–L{cite['line_end']}：{cite['excerpt']}")
        grouping = case['grouping']
        lines.extend(['', '### 固定候选归组', '', '本阶段使用人工准备的固定候选，单独评估归组；不是把提取结果自动映射为参考身份。'])
        if grouping.get('error'):
            lines.append(grouping['error'])
        integration = grouping.get('integration')
        if integration and integration.get('plan'):
            plan, progress = integration['plan'], integration['progress']
            lines.append(f"- 路由：{plan.get('mode')}；{plan.get('reason')}；任务请求 {integration['calls_used']}/{integration['allowance']}。")
            lines.append(f"- 比较进度 {progress['handled_pairs']}/{progress['total_pairs']}；原文核验 {progress['verified_pairs']}，待核验 {progress['pending_verification']}；不是语义完整性。")
            for step in integration['steps']:
                d = step.get('diagnostic') or {}
                lines.append(f"- 步骤 `{step['id'][:12]}`：{step['kind']} / {step['scope']} / {step['status']}；完整输入估计 {d.get('estimated_input_tokens')}/{d.get('input_limit')}。")
        for check in grouping.get('checks', {}).get('labelled_pairs', []):
            lines.append(f"- {' / '.join(check['pair'])}：实际 {check['actual']}；草案一致 {check['agrees_with_draft']}；禁止关系 {check['forbidden_found']}。{check['reason']}")
        for g in grouping.get('suggestions', []):
            lines.append(f"- 模型建议：{g['proposal']['kind']} / {g['proposal']['name']}；{g['proposal']['reason']}")
        lines.extend(['', '### 固定题目与真实评分接口', ''])
        for grade in case['grading']:
            lines.append(f"- `{grade['id']}`：{grade['status']}；输入答案：{grade['answer']}")
            if grade.get('result'):
                result = grade['result']
                lines.append(f"  - 判断：{result['overall_assessment']}；目标范围：{result['target_fit']}；逐标准覆盖：{result['covered_indices']}。")
                lines.append(f"  - 反馈：{result['feedback']}")
                lines.append(f"  - 与草案比较：{json.dumps(grade['checks'], ensure_ascii=False)}")
            elif grade.get('error'):
                lines.append(f"  - {grade['error']}")
    lines.extend(['', '## 调用与预算', '', '| 阶段 | 契约 | 状态 | 秒 | 输入估计/上限 |', '| --- | --- | --- | --- | --- |'])
    for call in report['calls']:
        d = call.get('diagnostic', {})
        lines.append(f"| {call['case']}/{call['stage']} | {call['schema']} | {call['status']} | {call['seconds']:.2f} | {d.get('estimated_input_tokens', '?')}/{d.get('input_limit', '?')} |")
    lines.extend(['', '完整数据见 `results.json`；用 `review-template.json` 关联参考点和实际候选。',
                  '未审核时不计算知识召回率、候选精确率或语义引用正确率。', ''])
    return '\n'.join(lines)


def write_report(directory, report):
    (directory / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (directory / 'report.md').write_text(markdown_report(report), encoding='utf-8')
    (directory / 'review-template.json').write_text(json.dumps(review_template(report), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def review_run(directory, review_path):
    report = json.loads((directory / 'results.json').read_text(encoding='utf-8'))
    review = json.loads(review_path.read_text(encoding='utf-8'))
    reviewed = apply_review(report, review)
    (directory / 'reviewed.json').write_text(json.dumps(reviewed, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    lines = ['# 语义审核记录', '', f"模式：{reviewed['mode']}；审核方式：{reviewed['reviewer']}", '', reviewed['note'], '']
    for case in reviewed['cases']:
        lines.extend([f"## {case['id']}", '',
            f"参考点已审核 {case['reviewed_points']}/{case['reference_points']}，匹配 {case['matched_points']}；候选已审核 {case['reviewed_candidates']}/{case['candidates']}。",
            f"参考点召回率：{case['reference_recall']}；候选精确率：{case['candidate_precision']}。未完成时 None 表示尚不计算。",
            f"条件错误：{case['condition_errors']}；缺少语义依据：{case['unsupported_claims']}；重复额外候选：{case['duplicate_extra_candidates']}。", ''])
    (directory / 'reviewed.md').write_text('\n'.join(lines), encoding='utf-8')
    return reviewed
