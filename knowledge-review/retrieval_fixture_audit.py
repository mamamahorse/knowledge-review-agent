"""Bind a complete authored section ledger to actual prepared chunk identities.

This projects explicit semantic judgments; it never fills missing grades with
zero and never reads rankings. A changed fixture needs a new audit/manifest.
"""
from hashlib import sha256

from retrieval_benchmark_data import covering_bundles, initial_review, validate_review


def bind_section_audit(manifest, sections, corrections=None):
    corrections = corrections or {}
    if not set(corrections) <= {q['id'] for q in manifest['suite']['queries']}:
        raise ValueError('初审修正包含未知问题。')
    cases = []
    for query in manifest['suite']['queries']:
        allowed = {key:value for key,value in sections.items() if value['file'] in query['scope']}
        ledger = query['section_judgments']
        if len(ledger) != len(allowed) or {j['section_id'] for j in ledger} != set(allowed):
            raise ValueError('逐章节初审必须完整且不重复，不能把未审章节自动当0。')
        correction = corrections.get(query['id'])
        if correction:
            if not correction['reason'].strip() or not correction['grades'] or not set(correction['grades']) <= set(allowed):
                raise ValueError('等级修正需理由与许可章节身份。')
            ledger = [{**j,'grade':correction['grades'].get(j['section_id'],j['grade']),
                       'reason':correction['reason'] if j['section_id'] in correction['grades'] else j['reason']} for j in ledger]
        grades, notes = {}, {}
        chunks = [c for c in manifest['chunks'] if c['file'] in query['scope']]
        for judgment in ledger:
            if type(judgment['grade']) is not int or judgment['grade'] not in (0,1,2) or not judgment['reason'].strip():
                raise ValueError('逐章节等级和判定理由不合法。')
            section = allowed[judgment['section_id']]
            source = next(s for s in manifest['sources'] if s['file'] == section['file'])
            original = '\n'.join(source['text'].splitlines()[section['line_start']-1:section['line_end']])
            if original != section['meaning']:
                raise ValueError('章节台账与实际原文不一致，必须重新初审。')
            for bundle in covering_bundles(chunks, section):
                for identity in bundle:
                    grades[identity] = max(grades.get(identity,0), judgment['grade'])
                    notes.setdefault(identity, []).append(judgment['reason'])
        if set(grades) != {c['id'] for c in chunks}:
            raise ValueError('台账未判定全部实际片段，需补审标题或新分块。')
        review = initial_review(manifest, query)
        reason = correction['reason'] if correction else query['audit']['reason']
        review.update(scope_checked=True, note=reason, issue_resolution=reason,
                      judgments=[{'chunk_id':c['id'],'grade':grades[c['id']], 'note':'\n'.join(notes[c['id']])} for c in chunks])
        validate_review(manifest, query, {**review,'status':'approved'})
        cases.append({'query_id':query['id'],
                      'decision':'needs_user' if query['audit']['status']=='disputed' else 'confident',
                      'reason':reason, 'review':review,
                      'focus_chunk_ids':[c['id'] for c in chunks if grades[c['id']] > 0]})
    return {'schema':1,'version':'expanded-v1-section-audit-20261013',
            'method':'Agent撰写并逐节审核公开合成协议，显式列出每题许可章节的等级与语义理由；本程序仅绑定实际预处理身份，未读取任何被测排名。等级2为直接支持所问命题，1为部分支持，0为条件/操作不支持。合成自审不等于外部独立gold。',
            'benchmark_fingerprint':manifest['fingerprint'],
            'source_hashes':{s['file']:sha256(s['text'].encode()).hexdigest() for s in manifest['sources']},
            'section_ledger':sections,'semantic_corrections':corrections,'cases':cases}
