/* Experimental single-document planning. Existing request/session handling is reused. */
(() => {
  let epoch = 0, timer = null, pending = null, selectedBatch = null;
  const output = () => $('learning-planner-result');
  function error(err) { output().replaceChildren(makeElement('p', 'error', err.message)); }
  function render(value) {
    const box = output(); box.replaceChildren();
    const labels = { queued: '等待执行', running: '正在生成', completed: '全部完成', waiting_next: '本批完成，等待继续', paused: '已暂停', failed: '执行失败' };
    const stages = { batches: '分批生成', planning: '自主阅读与轻量规划', refine: '正式目标精化', review: '本批清单复查', ready: '本批正式清单已就绪', question: '选定目标出题' };
    box.append(makeElement('p', '', `${stages[value.stage] || ''} · ${labels[value.status] || value.status} · 已预留 ${value.calls_reserved}/${value.call_limit} 次请求额度`));
    if (value.batches) {
      box.append(makeElement('p', 'practice-hint', `当前文件过大，采用分批生成。已完成 ${value.completed_batches}/${value.batches.length} 批；完成本批后点击继续，不会自动运行下一批。`));
      const select = document.createElement('select'); select.setAttribute('aria-label', '查看生成批次');
      for (const batch of value.batches) {
        if (!batch.job_id) continue;
        const option = document.createElement('option'); option.value = batch.index;
        option.textContent = `第 ${batch.index} 批 · L${batch.line_start}–L${batch.line_end} · ${labels[batch.status] || batch.status} · ${batch.goal_count} 个目标`;
        option.selected = batch.index === value.batch_index; select.append(option);
      }
      select.onchange = () => { selectedBatch = Number(select.value); load(value.id, ++epoch).catch(error); };
      box.append(select, makeElement('p', 'practice-hint', `当前展示第 ${value.batch_index} 批，原文 L${value.batch_range[0]}–L${value.batch_range[1]}。各批独立复查，未做跨批去重。`));
      if (value.working_batch) box.append(makeElement('p', '', `后台正在处理第 ${value.working_batch} 批。`));
    }
    if (pending?.id === value.id) box.append(makeElement('p', 'practice-hint', '已提交，等待后台更新。'));
    box.append(makeElement('p', 'practice-hint', `已有处理回执 ${Object.keys(value.dispositions).length}/${value.units.length} 个单元。处理进度不代表知识无遗漏或已掌握。`));
    if (value.stale) box.append(makeElement('p', 'error', '资料版本或范围已变化；此规划保留供查看，请按当前版本新建。'));
    if (value.error) box.append(makeElement('p', 'error', `任务未完成：${value.error}`));
    if (value.runtime) {
      box.append(makeElement('p', 'practice-hint', value.runtime.phase === 'submission' ? '正在整理并校验学习产物' : '正在按需阅读资料'));
      const correction=value.runtime.correction;
      if (correction) {
        box.append(makeElement('p', 'practice-hint', `模型纠错已使用 ${correction.attempts}/2 次机会。`));
        for (const item of correction.errors || [correction]) box.append(makeElement('p', 'practice-hint', `工具 ${item.tool || '决策'} (${item.call_id || '无调用ID'})：${item.error.code} · ${item.error.message}`));
      }
      const transport=value.runtime.transport;
      if (transport) box.append(makeElement('p', 'practice-hint', `网络重试已使用 ${transport.attempts}/2 次；${transport.error_type || '服务连接异常'}。模型纠错次数不因此增加。`));
    }
    if (value.metrics?.coverage_status === 'observed_attempts_only') {
      const m=value.metrics, input=m.tokens.input_tokens, output=m.tokens.output_tokens;
      box.append(makeElement('p','practice-hint',`HTTP尝试 ${m.http_attempts} 次（失败 ${m.failed_http_attempts} 次）；已观测输入/输出 ${input.observed_total}/${output.observed_total} Token，未知用量调用 ${input.unknown_calls}/${output.unknown_calls}；累计执行 ${(m.active_ms/1000).toFixed(1)} 秒。费用尚未核价。`));
    } else if (value.metrics) box.append(makeElement('p','practice-hint','尚无运行遥测；未执行或历史消耗未知，不能据此认为消耗为零。'));
    if (value.visual_coverage) {
      const ledger = value.visual_coverage, targets = Object.values(ledger.targets), objects = Object.values(ledger.objects);
      const visualLabels = { uninspected:'尚未检查', reviewed:'已检查', needs_detail:'需要细看', failed:'检查失败', discovered:'已发现，待处理', read:'已读取', excluded:'已排除', unresolved:'尚未解决' };
      const objectLabels = { figure:'图示', chart:'图表', table:'表格', formula:'公式', icon:'图标', other:'其他对象' };
      const detail = document.createElement('details');
      detail.append(makeElement('summary', '', `视觉检查 ${targets.filter(t => t.status === 'reviewed').length}/${targets.length} · 发现对象 ${objects.length} · 已交代 ${objects.filter(o => ['read','excluded'].includes(o.status)).length}`));
      detail.append(makeElement('p', 'practice-hint', '检查记录不保证没有漏检；发现描述不是正式证据。'));
      for (const target of targets) detail.append(makeElement('p', '', `${target.page ? `第 ${target.page} 页` : '资料图片'}：${visualLabels[target.status] || target.status} ${target.note || ''}`));
      for (const object of objects) detail.append(makeElement('p', '', `${objectLabels[object.kind] || '对象'} · ${object.label}：${visualLabels[object.status] || object.status} · ${object.uncertainty || object.reason || ''}`));
      box.append(detail);
    } else if (!value.visual_coverage_enabled) box.append(makeElement('p', 'practice-hint', '本任务未要求逐页视觉检查，文字处理完成不代表图表与公式已全面检查。'));
    if (value.parse_issues.length) {
      const issues = document.createElement('details'); issues.append(makeElement('summary', '', `原文解析问题（${value.parse_status}）`));
      issues.append(makeElement('pre', '', JSON.stringify(value.parse_issues, null, 2))); box.append(issues);
    }
    if (['queued', 'running'].includes(value.status)) {
      const pause = makeElement('button', '', '暂停'); pause.type = 'button';
      pause.onclick = () => request(`/api/learning-plans/${value.id}/pause`, { method: 'POST' }).then(() => load(value.id, epoch)).catch(error);
      box.append(pause);
    } else if (value.status !== 'completed' && !value.stale) {
      const extra = document.createElement('input'); extra.type = 'number'; extra.min = '0'; extra.max = '128'; extra.value = '0';
      const label = makeElement('label', '', '额外请求额度（默认不增加）'); label.append(extra);
      const resume = makeElement('button', '', value.can_next ? '继续生成下一批' : '继续当前任务'); resume.type = 'button';
      resume.disabled = pending?.id === value.id;
      resume.onclick = () => {
        if (value.can_next) selectedBatch = null;
        submitOperation(value, value.can_next ? 'next-batch' : 'resume', {
          additional_calls: Number(extra.value), ...(value.can_next ? {batch_index: value.batch_cursor + 1} : {})
        }).catch(error);
      };
      box.append(label, resume);
    }
    if (value.stage === 'planning' && (value.batch_status || value.status) === 'completed' &&
        (!value.batches || value.batch_index === value.batch_cursor) && !value.stale) {
      const formalize = makeElement('button', '', '生成正式清单并自动复查'); formalize.type = 'button';
      formalize.onclick = () => submitOperation(value, 'formalize', {}).catch(error);
      box.append(formalize);
    }
    const goals = Object.values(value.formal?.goals || {});
    if (goals.length) {
      box.append(makeElement('h3', '', ['ready','question'].includes(value.stage) ? '正式学习目标' : '已保存的目标（复查尚未完成）'));
      if (value.formal.missing_context) box.append(makeElement('p', 'practice-hint', `外部背景缺口：${value.formal.missing_context}`));
      if (value.formal.omission_risk) box.append(makeElement('p', 'error', '模型仍报告已读范围可能遗漏目标。'));
      for (const goal of goals) {
        const card = makeElement('section', 'outline-candidate');
        const learning = goal.learning;
        card.append(makeElement('h4', '', learning.target.title), makeElement('p', '', learning.knowledge.statement),
          makeElement('p', '', `能力：${learning.target.capability} · 范围：${learning.target.scope}`),
          makeElement('p', '', learning.target.description));
        const criteria = document.createElement('ol');
        for (const criterion of learning.target.criteria) criteria.append(makeElement('li', '', criterion));
        card.append(criteria, makeElement('p', 'practice-hint', goal.reason));
        appendEvidence(card, learning.knowledge.evidence_ids, value);
        const session = value.questions[goal.id];
        if (session) {
          const open = makeElement('button', '', '打开首次练习'); open.type = 'button';
          open.onclick = () => request(`/api/practice/${session.session_id}`).then(showLearningSession).catch(error);
          card.append(open);
        } else if (value.stage === 'ready' && (value.batch_status || value.status) === 'completed' &&
                   ['completed','waiting_next'].includes(value.status) && !value.stale) {
          const generate = makeElement('button', '', '按此目标出题'); generate.type = 'button';
          generate.disabled = pending?.id === value.id;
          generate.onclick = () => submitOperation(value, 'questions', { goal_id: goal.id,
            ...(value.batches ? {batch_index: value.batch_index} : {}) }).catch(error);
          card.append(generate);
        }
        box.append(card);
      }
    }
    if (value.revisions.length) {
      const history = document.createElement('details'); history.append(makeElement('summary', '', '目标修订与复查记录'));
      for (const revision of value.revisions) {
        history.append(makeElement('p', '', `${stages[revision.stage]}：${revision.note}`));
        for (const r of revision.changes.reviews) history.append(makeElement('p', '', `${r.goal_id} · ${r.decision} → ${r.replacement_ids.join('、') || '移除'}：${r.reason}`));
        for (const r of revision.changes.retirements) history.append(makeElement('p', '', `${r.goal_id} → ${r.replacement_ids.join('、') || '移除'}：${r.reason}`));
      }
      box.append(history);
    }
    const abilities = document.createElement('details'); abilities.append(makeElement('summary', '', '轻量能力规划（不是最终考核标准）'));
    for (const item of value.items) {
      const section = makeElement('section', 'outline-candidate');
      section.append(makeElement('h4', '', item.ability));
      for (const condition of item.conditions) section.append(makeElement('p', '', condition));
      if (item.dependencies.length) section.append(makeElement('p', '', `关联能力：${item.dependencies.join('、')}`));
      if (item.uncertainty) section.append(makeElement('p', 'practice-hint', item.uncertainty));
      const resolution = value.formal?.abilities?.[item.id];
      if (resolution) section.append(makeElement('p', 'practice-hint', `正式目标去向：${resolution.status} · ${resolution.goal_ids.join('、') || '无单列目标'} · ${resolution.reason}`));
      appendEvidence(section, item.evidence_ids, value);
      abilities.append(section);
    }
    box.append(abilities);
  }
  function appendEvidence(section, ids, value) {
      for (const id of ids) {
        const evidence = value.evidence[id]; if (!evidence) continue;
        const detail = document.createElement('details');
        detail.append(makeElement('summary', '', evidence.kind === 'visual_observation' ? '视觉观察与依据' : '原文依据'));
        detail.append(makeElement('pre', '', evidence.text || evidence.observation || ''));
        if (evidence.limitations) detail.append(makeElement('p', '', evidence.limitations));
        const view = makeElement('button', '', '查看原文位置'); view.type = 'button';
        view.onclick = () => showCitation(evidence.citation).catch(error);
        detail.append(view); section.append(detail);
      }
  }
  async function submitOperation(value, action, body) {
    if (pending?.id === value.id) return;
    pending = { id: value.id, action, goalId: body.goal_id, calls: value.calls_reserved, until: Date.now() + 30000 };
    try {
      await request(`/api/learning-plans/${value.id}/${action}`, { method: 'POST', body: JSON.stringify(body) });
      await load(value.id, ++epoch);
    } catch (err) { pending = null; throw err; }
  }
  async function load(id, version) {
    const result = await request(`/api/learning-plans/${id}${selectedBatch ? `?batch_index=${selectedBatch}` : ''}`);
    if (version !== epoch || result.document_id !== state.current?.id) return;
    if (pending?.id === id && (Date.now() > pending.until || result.calls_reserved > pending.calls ||
        (pending.action === 'formalize' && result.stage !== 'planning') ||
        (pending.action === 'questions' && (result.stage === 'question' || result.questions[pending.goalId])))) pending = null;
    render(result); clearTimeout(timer);
    if (['queued', 'running'].includes(result.status) || pending?.id === id) timer = setTimeout(() => load(id, version).catch(error), 2500);
  }
  async function refresh() {
    clearTimeout(timer); pending = null; selectedBatch = null; const version = ++epoch, documentId = state.current?.id;
    output().replaceChildren(); $('learning-planner-history').replaceChildren();
    if (!documentId) return;
    const rows = await request(`/api/documents/${documentId}/learning-plans`);
    if (version !== epoch || documentId !== state.current?.id) return;
    for (const row of rows) {
      const option = document.createElement('option'); option.value = row.id; option.textContent = `${row.id.slice(0, 8)} · ${row.status}`;
      $('learning-planner-history').append(option);
    }
    if (rows.length) await load(rows[0].id, version);
  }
  document.addEventListener('learning-document-changed', () => refresh().catch(error));
  $('learning-planner-refresh').onclick = () => refresh().catch(error);
  $('learning-planner-history').onchange = () => { selectedBatch = null; load($('learning-planner-history').value, ++epoch).catch(error); };
  $('learning-planner-form').onsubmit = async event => {
    event.preventDefault(); if (!state.current) return;
    const source = state.current, button = event.submitter; if (button) button.disabled = true;
    const payload = { source_id: source.source_id, objective: $('learning-planner-objective').value,
      max_requests: Number($('learning-planner-limit').value), response_language: responseLanguage(), formalize: true,
      visual_coverage: $('learning-planner-visual').checked };
    const key = `learntrace.planning-submit.${source.id}`;
    try {
      let pending = null;
      try { pending = JSON.parse(localStorage.getItem(key) || 'null'); } catch { localStorage.removeItem(key); }
      if (!pending || JSON.stringify(pending.payload) !== JSON.stringify(payload)) {
        pending = { request_id: crypto.randomUUID(), payload }; localStorage.setItem(key, JSON.stringify(pending));
      }
      const accepted = await request(`/api/documents/${source.id}/learning-plans`, { method: 'POST',
        body: JSON.stringify({ ...payload, request_id: pending.request_id }) });
      localStorage.removeItem(key);
      if (state.current?.id === source.id) { selectedBatch = null; await load(accepted.id, ++epoch); }
    } catch (err) { error(err); } finally { if (button) button.disabled = false; }
  };
})();
