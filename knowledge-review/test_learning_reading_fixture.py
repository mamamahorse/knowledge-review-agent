"""Submission-only fixtures: replace reading decisions, not evidence validators.

These business tests do not measure the native protocol or finish_reading HTTP.
Actual protocol/counting/recovery is covered by test_learning_runtime.
"""
def prepared_reading(reader,state,client,model,budget,definitions,make_messages,reserve,persist,execute,guard,**kwargs):
    guard()
    if not state['evidence']:
        source=state['inputs']['snapshot']['source_id']
        reserve()
        execute('read_source',dict(source_id=source,line_start=1,line_end=max(u['line_end'] for u in state['units'])))
    state.setdefault('runtime',{}).update(phase='submission',stage=state['stage'])
    persist()
