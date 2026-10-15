"""Isolated synthetic chat-practice evaluation; live requires an explicit flag."""

import argparse
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from chat_practice import start, DiscussionProposal
from conversation_memory import ConversationMemory
from conversation_store import ConversationStore
from generation_budget import GenerationBudget
from learning_store import LearningStore
from practice_model import DiscussionQuestion


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('smoke', 'live'), default='smoke')
    parser.add_argument('--output-tokens', type=int, help='Evaluation-only reserve; does not change app configuration.')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    directory = root / '.data' / 'evaluations' / f'chat-practice-{args.mode}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:6]}'
    directory.mkdir(parents=True)
    conversations = ConversationStore(directory / 'conversations.sqlite3')
    practice = LearningStore(directory / 'practice.sqlite3')
    practice.conversation_path = conversations.path
    conversations.practice_path = practice.path
    identity = conversations.create('合成 TCP 数字例子')['id']
    turn = conversations.begin(identity, 'seed', {'question': '我们讨论 SEQ=100，LEN=20，ACK=120；决定先理解下一期待字节，不加入学习计划。'})
    conversations.finish(turn['id'], turn['attempt'], {'coverage': 'none', 'coverage_note': '合成讨论',
        'blocks': [{'kind': 'supplement', 'text': 'ACK=120 是接收方下一期待的字节序号。', 'citations': []}]}, {})
    payload = {'objective': '请用刚才的例子考考我，把长度改成30，并要求解释 ACK 的含义。', 'response_language': 'zh', 'strict': False, 'collection_id': 'root'}
    if args.mode == 'live':
        from dotenv import load_dotenv
        load_dotenv(root / '.env')
    budget = GenerationBudget.from_env()
    if args.output_tokens:
        budget = replace(budget, output_tokens=args.output_tokens)
    report = {'mode': args.mode, 'synthetic_histories': True, 'semantic_quality_rate': None,
              'automatic_enrollment': False, 'budget': budget.diagnostic(0)}
    def controlled(messages, schema, *, before_request):
        before_request()
        data = json.loads(messages[1]['content'])
        ref = data['discussion_reference_ranges'][0]
        draft = DiscussionQuestion.model_validate({'objective': '解释累计确认', 'question': 'SEQ=100、LEN=30 时 ACK 是多少？解释为什么。',
            'rubric_points': ['计算 ACK=130', '说明 ACK 是下一期待字节序号'], 'citations': [],
            'criterion_indices': [1, 2], 'learning': {'knowledge': {'name': 'TCP 累计确认',
                'statement': '本题沿讨论考察下一期待字节，参考解释需审核。', 'content_tags': ['TCP'], 'citations': []},
                'target': {'title': '解释 ACK', 'capability': 'explain', 'description': '结合数据长度解释累计确认',
                    'scope': '不包含 SYN/FIN 的数据段', 'criteria': ['计算下一期待位置', '解释确认含义']}}})
        return DiscussionProposal(**draft.model_dump(), reason='受控讨论角度，未证明真实出题质量。',
            history_references=[{k: ref[k] for k in ('conversation_id', 'message_id', 'message_revision', 'start_offset', 'end_offset')} | {'block_index': 0}])
    with patch('chat_practice.GenerationBudget.from_env', return_value=budget):
        if args.mode == 'smoke':
            with patch.object(ConversationMemory, 'search', return_value=[]):
                result = start(practice, conversations, identity, 'exercise', payload, generator=controlled)
        else:
            result = start(practice, conversations, identity, 'exercise', payload)
    report['generation'] = result
    report['review_items_created'] = sum(len(g['items']) for g in practice.list_plan())
    if result['session']:
        session = result['session']
        conversations.delete(identity)
        report['deletion'] = {'basis_revoked': bool(practice.get(session['id'])['basis']['revoked']),
                              'references_cleared': not practice.get(session['id'])['basis']['references']}
    (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(directory / 'report.json'), 'mode': args.mode, 'status': result['status'],
                      'review_items_created': report['review_items_created']}, ensure_ascii=True))
    return 0 if result['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
