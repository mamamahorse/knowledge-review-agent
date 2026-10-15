"""Read current effective primary-answer evidence; no mastery score or mutations."""
import json
from contextlib import closing
from datetime import datetime, timezone


def recommend_next(store, route, order, accepted):
    evidence={}; skipped=set(route['skipped'])
    with closing(store.practice._connect()) as db:
        db.execute('BEGIN')  # One consistent read snapshot, no write transaction.
        for identity in order:
            c=db.execute('SELECT item_id FROM outline_candidates WHERE id=?',(identity,)).fetchone()
            item=db.execute('SELECT * FROM review_items WHERE id=?',(c['item_id'],)).fetchone() if c and c['item_id'] else None
            facts={'candidate_id':identity,'item_id':c['item_id'] if c else None,'state':'unseen',
                'evidence':None,'pending_session_id':None,'due':False,'paused':bool(item and item['status']=='paused')}
            if item:
                pending=db.execute("SELECT id FROM practice_sessions WHERE review_item_id=? AND review_mode!='follow_up' AND status IN ('waiting_for_answer','grading') ORDER BY created_at DESC LIMIT 1",(item['id'],)).fetchone()
                facts['pending_session_id']=pending['id'] if pending else None
                # Follow-ups and other capabilities are not proof for this fixed target.
                latest=db.execute("""SELECT q.id,q.answer_text,q.assistance,q.submitted_at,q.effective_evaluation_id,
                    q.revision,e.origin,e.grade_json FROM practice_questions q
                    JOIN practice_sessions s ON s.id=q.session_id
                    JOIN evaluation_revisions e ON e.id=q.effective_evaluation_id
                    WHERE q.target_id=? AND q.position=1
                    AND q.answer_text IS NOT NULL ORDER BY q.submitted_at DESC,q.rowid DESC LIMIT 1""",(item['target_id'],)).fetchone()
                if latest:
                    grade=json.loads(latest['grade_json'])
                    valid=latest['assistance']=='none_recorded' and grade.get('target_fit')=='within_scope' and grade.get('overall_assessment') in ('meets_standard','developing','needs_review')
                    facts['state']=('supported' if grade['overall_assessment']=='meets_standard' else 'gap') if valid else 'insufficient'
                    facts['evidence']={k:latest[k] for k in ('id','answer_text','assistance','submitted_at','effective_evaluation_id','revision','origin')} | {
                        'assessment':grade.get('overall_assessment'),'target_fit':grade.get('target_fit'),'feedback':grade['feedback']}
                scheduled=db.execute('SELECT 1 FROM review_events WHERE item_id=? AND eligible=1 LIMIT 1',(item['id'],)).fetchone()
                facts['due']=bool(scheduled and datetime.fromisoformat(item['next_review_at'])<=datetime.now(timezone.utc))
            evidence[identity]=facts
    active=[i for i in order if i not in skipped and not evidence[i]['paused']]
    def result(identity,kind,reason):
        return {'candidate_id':identity,'kind':kind,'reason':reason,'item_id':evidence[identity]['item_id'] if identity else None,
            'pending_session_id':evidence[identity]['pending_session_id'] if identity else None,'facts':list(evidence.values()),
            'policy':'pending → scheduled due → current qualitative gap → route exploration; advisory only',
            'note':'本题达到标准不等于全面掌握；未测试、使用提示或范围不确定也不等于不会。所有目标都可自由选择。'}
    pending=next((i for i in active if evidence[i]['pending_session_id']),None)
    if pending:return result(pending,'continue','有未完成或待评分的真实作答，建议先继续，避免重复创建练习。')
    due=next((i for i in active if evidence[i]['due']),None)
    if due:return result(due,'review','已有有效复习事件，FSRS 建议日期已到；这是复习时间建议，不是掌握判断。')
    gap=next((i for i in active if evidence[i]['state']=='gap'),None)
    if gap:return result(gap,'address_gap','当前有效、独立且范围匹配的主要作答显示理解缺口，建议先处理这个目标。')
    next_id=next((i for i in active if evidence[i]['state']!='supported'),None)
    if next_id:
        prerequisites=[e['before_id'] for e in accepted if e['after_id']==next_id and e['kind']=='prerequisite' and e['before_id'] in active]
        earlier=next((i for i in order if i in prerequisites and evidence[i]['state']!='supported'),None)
        if earlier:return result(earlier,'check_prerequisite','你接受的前置关系指向此目标，但尚无当前独立达标证据，建议先核对；后续目标仍可直接学习。')
        labels={'unseen':'尚无主要作答证据，可沿建议顺序先尝试。','insufficient':'已有作答使用了提示、范围不匹配或评估不确定，建议独立核对；未判定为不会。'}
        return result(next_id,'explore',labels[evidence[next_id]['state']])
    return result(None,'choose_freely','当前未跳过且未暂停的目标近期本题均已有独立达标证据，或暂无候选；可自由挑选挑战、恢复跳过目标或等待复习。')
