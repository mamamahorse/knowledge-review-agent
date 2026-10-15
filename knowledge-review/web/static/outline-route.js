/* Routes advise; enrollment and FSRS remain explicit, separate operations. */
const routeRequests = new Map();
const routeAllowances = new Map();
const routeEdgeDrafts = new Map();
const routeDisclosures = new Map();
const routeKinds = { prerequisite: '前置依赖', helpful_first: '建议先学', uncertain: '关系尚不确定' };
const routeStates = { queued: '等待生成', running: '正在比较', paused: '已暂停', failed: '生成失败，可继续', completed: '可审核' };

function routeDetails(key, open = false) {
  const detail = makeElement('details', 'learning-details');
  detail.open = routeDisclosures.has(key) ? routeDisclosures.get(key) : open;
  detail.addEventListener('toggle', () => routeDisclosures.set(key, detail.open));
  return detail;
}

function routeButton(text, action, disabled = false) {
  const button = makeElement('button', 'practice-action secondary', text);
  button.type = 'button'; button.disabled = disabled;
  button.addEventListener('click', async () => {
    button.disabled = true;
    try { await action(); }
    catch (error) { outlineError(error); }
    finally { button.disabled = disabled; }
  });
  return button;
}

function renderOutlineRoutes(job, panel) {
  if (!job.routes) return;
  const section = makeElement('section', 'learning-item');
  section.append(makeElement('h3', '', '前置关系与学习顺序建议'));
  section.append(makeElement('p', 'practice-hint', '请先审核知识清单。默认比较全部可用目标，也可勾选部分目标；待核对和待确认重复项需先处理，已确认重复项只用代表。建议可以修改或跳过，所有学习入口仍可自由使用。'));
  const allowance = makeElement('input');
  allowance.type = 'number'; allowance.min = '1'; allowance.max = '200';
  allowance.value = routeAllowances.get(job.id) || '24';
  allowance.setAttribute('aria-label', '路线本次最多模型请求次数');
  allowance.addEventListener('input', () => routeAllowances.set(job.id, allowance.value));
  const label = makeElement('label', 'practice-hint', '本次最多模型请求次数 ');
  label.append(allowance); section.append(label);
  const callLimit = () => {
    const n = Number(allowance.value);
    if (!Number.isInteger(n) || n < 1 || n > 200) throw new Error('请求次数请填写 1–200 的整数。');
    return n;
  };
  section.append(routeButton('生成学习路线建议', async () => {
    const forms = [...panel.querySelectorAll('form[data-outline-candidate]')];
    const selected = forms.filter(f => f.elements.namedItem('selected').checked && !f.elements.namedItem('selected').disabled);
    const ids = selected.length ? selected.map(f => f.dataset.outlineCandidate) : job.routes.eligible_ids;
    if (!ids.length || ids.some(id => !job.routes.eligible_ids.includes(id))) throw new Error('请先审核待核对或待确认重复项，再选择可用目标。');
    for (const form of forms.filter(f => ids.includes(f.dataset.outlineCandidate))) {
      await saveOutlineCandidate(form, job.candidates.find(c => c.id === form.dataset.outlineCandidate));
    }
    const key = JSON.stringify([job.id, ids.map(id => [id, job.candidates.find(c => c.id === id).revision]), responseLanguage()]);
    if (!routeRequests.has(key)) routeRequests.set(key, crypto.randomUUID());
    await jsonRequest(`/api/outlines/${job.id}/routes`, { request_id: routeRequests.get(key), candidate_ids: ids,
      response_language: responseLanguage(), call_limit: callLimit() });
    routeRequests.delete(key);
    await loadOutline(job.id);
  }, job.stale || job.status !== 'completed' || job.processing || outlineJoining));
  const excluded = Object.entries(job.routes.excluded);
  if (excluded.length) {
    const detail = makeElement('details', 'learning-details');
    detail.append(makeElement('summary', '', `暂不纳入路线的候选 · ${excluded.length}`));
    excluded.forEach(([id, reason]) => detail.append(makeElement('p', 'practice-hint', `${job.candidates.find(c => c.id === id)?.learning.target.title || id}：${reason}`)));
    section.append(detail);
  }
  job.routes.runs.forEach((route, index) => {
    const row = routeDetails(`${route.id}:run`, index === 0); row.className = 'learning-item';
    row.append(makeElement('summary', '', `${localDate(route.created_at)} · ${routeStates[route.status]} · ${route.nodes.length} 个目标${route.stale ? ' · 旧版本，仅供查看' : ''}`));
    row.append(makeElement('p', 'practice-state', `已判断 ${route.cursor}/${route.total_pairs} 个目标对 · 模型请求 ${route.calls_used}/${route.allowance}`));
    if (route.error) row.append(makeElement('p', 'request-status error', route.error));
    const disabled = route.stale || job.stale || route.status !== 'completed' || outlineJoining;
    if (['queued', 'running'].includes(route.status)) row.append(routeButton('暂停路线生成', async () => {
      await jsonRequest(`/api/outline-routes/${route.id}/pause`, {}); await loadOutline(job.id);
    }));
    if (['paused', 'failed'].includes(route.status)) row.append(routeButton('增加额度并继续路线', async () => {
      await jsonRequest(`/api/outline-routes/${route.id}/continue`, { call_limit: callLimit() }); await loadOutline(job.id);
    }, route.stale || job.stale || job.processing || outlineJoining));
    const names = Object.fromEntries(route.nodes.map(n => [n.id, n.learning.target.title]));
    const relations = routeDetails(`${route.id}:relations`);
    relations.append(makeElement('summary', '', `审核关系与理由 · ${route.edges.length} 条`));
    if (!route.edges.length) relations.append(makeElement('p', 'practice-hint', '尚无关系建议。可以查看判断回执，或在完成后手动添加。未提出关系不保证两目标没有依赖。'));
    route.edges.forEach(edge => renderRouteEdge(job, route, edge, names, relations, disabled));
    if (route.nodes.length > 1) {
      const form = makeElement('form', 'outline-edit');
      form.append(makeElement('h4', '', '手动添加关系'));
      const before = choice(names, route.order[0], '手动关系的先学目标');
      const after = choice(names, route.order[1], '手动关系的后学目标');
      const kind = choice(routeKinds, 'helpful_first', '手动关系类型');
      form.append(before, after, kind);
      const reason = outlineField(form, 'reason', '添加理由（作为你的建议保存）', '', true, 350);
      form.append(routeButton('添加待审核关系', async () => {
        await jsonRequest(`/api/outline-routes/${route.id}/edges`, { revision: route.revision, before_id: before.value,
          after_id: after.value, kind: kind.value, reason: reason.value }); await loadOutline(job.id);
      }, disabled));
      form.addEventListener('submit', e => e.preventDefault());
      [before, after, kind, reason].forEach(x => { x.disabled = disabled; });
      relations.append(form);
    }
    row.append(relations);
    renderRouteOrder(job, route, names, row, disabled);
    if (route.recommendation) renderRouteRecommendation(job, route, names, row);
    const receipts = routeDetails(`${route.id}:receipts`);
    receipts.append(makeElement('summary', '', '目标对判断回执、待补充话题与预算'));
    route.pages.forEach(page => {
      const p = makeElement('div', 'outline-chapter');
      p.append(makeElement('p', 'practice-hint', `${page.status} · ${page.inputs.length} 个目标对`));
      const d = page.diagnostic;
      p.append(makeElement('p', 'practice-hint', `完整输入估计 ${d.estimated_input_tokens}/${d.input_limit}；预期输出估计 ${d.output_estimate}，输出预留 ${d.output_reserved}。${d.capacity_source}。计数为保守估计。`));
      if (page.error) p.append(makeElement('p', 'request-status error', page.error));
      const pairs = Object.fromEntries(page.inputs.map(x => [x.index, x]));
      (page.result?.comparisons || []).forEach(c => p.append(makeElement('p', '', `${names[pairs[c.pair_index].a]} / ${names[pairs[c.pair_index].b]}：${c.relation === 'none' ? '未提出关系' : c.relation === 'uncertain' ? '尚不确定' : '提出顺序建议'} · ${c.reason}`)));
      (page.result?.missing_topics || []).forEach(m => p.append(makeElement('p', 'practice-hint', `待核实的外部话题：${m.topic} → ${names[m.after_id]}。${m.reason}（未自动创建目标或加入计划）`)));
      if (page.result?.note) p.append(makeElement('p', '', page.result.note));
      receipts.append(p);
    });
    row.append(receipts); section.append(row);
  });
  panel.append(section);
}

function renderRouteEdge(job, route, edge, names, panel, disabled) {
  const form = makeElement('form', 'learning-item outline-edit');
  const draft = routeEdgeDrafts.get(edge.id);
  const d = draft?.revision === edge.revision ? draft : edge.decision;
  form.append(makeElement('p', 'practice-state', ({ proposed: '待审核', accepted: '已接受', rejected: '已跳过' })[edge.status]));
  form.append(makeElement('p', 'practice-hint', `${({ source: '含原文依据的模型建议', pedagogical: '教学顺序建议', user: '用户修改或添加的建议' })[edge.decision.basis]} · ${edge.decision.certainty === 'clear' ? '模型认为较明确' : '仍需核对'}`));
  const before = choice(names, d.before_id, '关系的先学目标');
  const after = choice(names, d.after_id, '关系的后学目标');
  const kind = choice(routeKinds, d.kind, '关系类型');
  form.append(before, makeElement('span', '', ' → '), after, kind);
  const reason = outlineField(form, 'reason', '关系理由', d.reason, true, 350);
  const capture = () => routeEdgeDrafts.set(edge.id, { revision: edge.revision, before_id: before.value, after_id: after.value, kind: kind.value, reason: reason.value });
  [before, after, kind, reason].forEach(x => { x.disabled = disabled; x.addEventListener('input', capture); });
  edge.citations.forEach(c => form.append(practiceCitation(c)));
  if (edge.model) {
    const original = routeDetails(`${edge.id}:original`);
    original.append(makeElement('summary', '', '原模型建议与依据（修改后仍保留）'),
      makeElement('p', '', `${names[edge.model.before_id]} → ${names[edge.model.after_id]} · ${routeKinds[edge.model.kind]} · ${edge.model.reason}`));
    edge.original_citations.forEach(c => original.append(practiceCitation(c))); form.append(original);
  }
  [['保存修改', 'proposed'], ['接受关系', 'accepted'], ['跳过关系', 'rejected']].forEach(([text, decision]) => {
    form.append(routeButton(text, async () => {
      await jsonRequest(`/api/outline-route-edges/${edge.id}`, { revision: edge.revision, before_id: before.value,
        after_id: after.value, kind: kind.value, reason: reason.value, decision }, 'PATCH');
      routeEdgeDrafts.delete(edge.id); await loadOutline(job.id);
    }, disabled));
  });
  form.addEventListener('submit', e => e.preventDefault()); panel.append(form);
}

function renderRouteOrder(job, route, names, row, disabled) {
  row.append(makeElement('h4', '', '建议顺序（可自由调整）'));
  row.append(makeElement('p', 'practice-hint', ({ preview: '当前为模型待审核关系的预览；下一目标只使用已接受关系。', accepted: '按已接受关系排序；不要求先通过前一目标。', user: '当前使用你调整的顺序。' })[route.order_mode]));
  if (route.cycle.length) row.append(makeElement('p', 'request-status error', '建议关系存在循环，暂用原清单顺序。请审核冲突关系；学习入口仍可自由使用。'));
  if (route.order_conflicts.length) row.append(makeElement('p', 'practice-hint', '你调整的顺序与部分已接受关系不同，仍按你的顺序显示。'));
  const save = async (order, skipped) => {
    await jsonRequest(`/api/outline-routes/${route.id}/order`, { revision: route.revision, order, skipped }, 'PATCH');
    await loadOutline(job.id);
  };
  const list = makeElement('ol', 'rubric-list');
  route.order.forEach((id, index) => {
    const item = makeElement('li', 'outline-chapter');
    item.append(makeElement('p', '', `${names[id]}${route.skipped.includes(id) ? ' · 已暂时跳过' : ''}`));
    const controls = makeElement('div', 'learning-actions');
    [-1, 1].forEach(direction => controls.append(routeButton(direction < 0 ? '上移目标' : '下移目标', async () => {
      const order = [...route.order]; [order[index], order[index + direction]] = [order[index + direction], order[index]];
      await save(order, route.skipped);
    }, disabled || index + direction < 0 || index + direction >= route.order.length)));
    controls.append(routeButton(route.skipped.includes(id) ? '恢复此目标' : '暂时跳过此目标', () => save(route.order_mode === 'user' ? route.order : null,
      route.skipped.includes(id) ? route.skipped.filter(x => x !== id) : [...route.skipped, id]), disabled));
    item.append(controls); list.append(item);
  });
  row.append(list, routeButton('恢复关系建议顺序', () => save(null, route.skipped), disabled));
}

function renderRouteRecommendation(job, route, names, row) {
  const rec = route.recommendation;
  row.append(makeElement('h4', '', rec.candidate_id ? `下一目标建议：${names[rec.candidate_id]}` : '下一目标：自由选择'));
  row.append(makeElement('p', '', rec.reason), makeElement('p', 'practice-hint', rec.note));
  row.append(routeButton('刷新作答证据与推荐', () => loadOutline(job.id)));
  if (rec.candidate_id) row.append(routeButton(rec.item_id ? '开始或继续此目标练习' : '定位此目标，审核后选择加入', async () => {
    if (rec.pending_session_id) await showLearningSession(await request(`/api/practice/${rec.pending_session_id}`));
    else if (rec.item_id) await showLearningSession(await jsonRequest(`/api/learning/${rec.item_id}/review`, { mode: 'variant', response_language: responseLanguage() }));
    else {
      const form = document.querySelector(`form[data-outline-candidate="${rec.candidate_id}"]`);
      form?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      const detail = form?.querySelector('details'); if (detail) detail.open = true;
    }
  }, outlineJoining));
  const facts = routeDetails(`${route.id}:facts`);
  facts.append(makeElement('summary', '', '推荐所用的当前有效作答证据'));
  rec.facts.forEach(f => {
    facts.append(makeElement('p', 'practice-state', `${names[f.candidate_id]} · ${({ unseen: '未测试', gap: '当前本题有缺口', supported: '当前本题达到标准', insufficient: '证据不足' })[f.state]}${f.paused ? ' · 已暂停复习' : ''}${f.due ? ' · 建议复习日已到' : ''}`));
    if (f.evidence) {
      facts.append(makeElement('p', 'practice-feedback', `${localDate(f.evidence.submitted_at)} · 有效评分版本 ${f.evidence.revision} · ${assessments[f.evidence.assessment] || '暂无法判断'} · ${f.evidence.assistance === 'none_recorded' ? '无提示记录' : '使用了提示或资料'}`),
        makeElement('p', 'practice-feedback', f.evidence.answer_text), makeElement('p', 'practice-feedback', f.evidence.feedback));
    }
  });
  row.append(facts);
}
