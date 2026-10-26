"""Typed user tasks and non-blocking clarification; document text grants no actions."""

from typing import Literal
from pydantic import Field
from answers import StrictModel

TASKS = {
    'explain': ('解释概念 / 回答问题', '讲解概念、选段或代码，按需查阅资料。', 'answer'),
    'extract_outline': ('生成学习清单', '提取知识点和可考察目标，审核后再选择加入计划。', 'outline'),
    'practice': ('出题练习', '从当前资料或讨论生成练习题。', 'practice'),
    'assess_answer': ('评价作答 / 查看讲解', '打开已有练习，提交回答或查看反馈。', 'practice'),
    'organize_outline': ('整合学习清单', '对已有候选进行归组、重复检查和来源复查。', 'outline'),
    'plan_route': ('规划学习顺序', '根据已有候选建议前置关系和学习顺序。', 'outline'),
    'review_learning': ('复习已有目标', '查看已加入计划的项目并开始复习。', 'learning'),
    'recall_discussion': ('回顾以前的讨论', '搜索有效历史原话，延续已有讨论。', 'answer'),
    'enroll_selected': ('加入复习计划', '打开清单并明确选择要加入的目标。', 'outline'),
}


def capabilities():
    return [dict(id=k, label=v[0], description=v[1], destination=v[2], available=True)
            for k, v in TASKS.items()] + [dict(id='summarize_materials', label='整理 / 总结整份资料',
                description='遍历大资料的专门报告流程尚未接通；局部总结可在问答中提出。', available=False)]


class Clarification(StrictModel):
    question: str = Field(min_length=1, max_length=300)
    reason_code: Literal['task_ambiguous', 'scope_missing', 'object_missing', 'sequence_ambiguous']
    option_ids: list[str] = Field(min_length=2, max_length=4)


class TaskSelection(StrictModel):
    task_id: str


class TaskRouter:
    def definitions(self):
        schemas = []
        for name, model, description in (
            ('ask_question', Clarification, 'Ask only when important task ambiguity remains. '
             'Returns a persistent selector and suspends this turn without a final answer. '
             'Choose 2-4 distinct task IDs from the capability catalog. Do not ask again for an explicit task.'),
            ('select_task', TaskSelection, 'Select a clearly requested registered task. '
             'Non-answer tasks open their existing workflow and retain scope/review controls. '
             'Ordinary explanation continues with reading tools instead.')):
            schema = model.model_json_schema()
            target = schema['properties']['option_ids' if name == 'ask_question' else 'task_id']
            (target['items'] if name == 'ask_question' else target)['enum'] = list(TASKS)
            schemas.append(dict(name=name, description=description, parameters=schema))
        return schemas

    def execute(self, name, arguments):
        if name == 'ask_question':
            data = Clarification.model_validate(arguments).model_dump()
            ids = data.pop('option_ids')
            if len(set(ids)) != len(ids) or any(k not in TASKS for k in ids):
                raise ValueError('澄清选项必须是不同的可用任务。')
            return dict(kind='clarification', **data, options=[dict(id=k, label=TASKS[k][0],
                description=TASKS[k][1]) for k in ids])
        task = TaskSelection.model_validate(arguments).task_id
        if task not in TASKS:
            raise ValueError('这个任务尚不可用。')
        return dict(kind='task', task_id=task, label=TASKS[task][0],
                    description=TASKS[task][1], destination=TASKS[task][2])
