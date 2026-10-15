/* Directed investigation is a separate suggestion until the user adopts it. */
const sourceReviewRequests = new Map();

function renderOutlineReviews(job, group, form, callLimit) {
  const reviews = (job.grouping.reviews || []).filter(r => r.group_id === group.id);
  const origin = (job.grouping.reviews || []).find(r => r.applied_group_id === group.id && r.group_id !== group.id);
  if (origin) form.append(makeElement('p', 'practice-hint', '此归组来自你采用的定向复查；原建议和补读依据保留在原建议下。'));
  if (!reviews.length && group.proposal.kind !== 'needs_review') return;
  const section = makeElement('section');
  section.append(makeElement('h4', '', '定向原文复查'));
  section.append(makeElement('p', 'practice-hint', '在这份清单的资料快照中定位或补读，最多两轮、每轮两项。与整合共用请求额度；原候选和原建议保留，结论须你采用。'));
  const active = reviews.some(r => ['queued', 'running', 'paused'].includes(r.status) && !r.stale);
  const button = (text, target, payload, disabled = false) => {
    const b = makeElement('button', 'practice-action secondary', text);
    b.type = 'button'; b.disabled = disabled;
    b.addEventListener('click', async () => {
      b.disabled = true;
      try {
        await jsonRequest(target, typeof payload === 'function' ? payload() : payload);
        if (target.endsWith('/source-reviews')) {
          for (const key of sourceReviewRequests.keys()) if (key.startsWith(`${group.id}:`)) sourceReviewRequests.delete(key);
        }
        await loadOutline(job.id);
      } catch (error) { outlineError(error); b.disabled = false; }
    });
    return b;
  };
  if (group.status === 'proposed' && group.proposal.kind === 'needs_review' && !active) {
    section.append(button('定向补读原文', `/api/outline-groups/${group.id}/source-reviews`, () => {
      const key = `${group.id}:${group.revision}:${responseLanguage()}`;
      if (!sourceReviewRequests.has(key)) sourceReviewRequests.set(key, crypto.randomUUID());
      return { request_id: sourceReviewRequests.get(key), response_language: responseLanguage() };
    }, job.stale || job.processing || outlineJoining));
  }
  const status = { queued: '等待复查', running: '正在复查', paused: '已暂停', failed: '失败，可继续', completed: '复查完成', stale: '旧版本复查，记录保留' };
  const outcomes = { same_knowledge: '同一知识点，保留不同目标', duplicate_targets: '重复目标，保留代表',
    distinct: '条件不同，保留独立', unresolved: '证据不足，仍待核对', revise_candidates: '需先修订候选' };
  reviews.forEach(review => {
    const row = makeElement('div', 'learning-item');
    row.append(makeElement('p', 'practice-state', `${status[review.status]} · 补读 ${review.rounds}/2${review.applied_group_id ? ' · 已采用' : review.stale || job.stale ? ' · 已失效，记录保留' : ''}`));
    if (review.error) row.append(makeElement('p', 'request-status error', review.error));
    const details = document.createElement('details');
    details.append(makeElement('summary', '', '查看补读过程、实际依据与预算'));
    review.steps.forEach(step => {
      details.append(makeElement('p', 'practice-hint', `${step.status}${step.action ? ` · ${step.action.issue}` : ''}`));
      if (step.action?.action === 'read' && step.action.reason) details.append(makeElement('p', '', step.action.reason));
      if (step.diagnostic) details.append(makeElement('p', 'practice-hint', `完整输入估计 ${step.diagnostic.estimated_input_tokens}/${step.diagnostic.input_limit}；输出预留 ${step.diagnostic.output_reserved}。`));
      (step.observation?.lookups || []).forEach(lookup => {
        details.append(makeElement('p', '', `${lookup.request.kind === 'locate' ? `定位「${lookup.request.query}」` : `读取 ${lookup.request.line_start}–${lookup.request.line_end} 行`} · ${lookup.note || ''}`));
        (lookup.matches || []).forEach(p => details.append(makeElement('p', 'practice-hint', `导航：${p.source_id} · ${p.line_start} 行（尚不构成原文依据）`)));
      });
      step.citations.forEach(c => details.append(practiceCitation(c)));
      if (step.error) details.append(makeElement('p', 'request-status error', step.error));
    });
    row.append(details);
    const result = review.result;
    if (result) {
      row.append(makeElement('p', '', `${outcomes[result.outcome]} · ${result.name || result.issue}`));
      row.append(makeElement('p', '', result.reason));
      review.citations.forEach(c => row.append(practiceCitation(c)));
      (result.clarifications || []).forEach(c => {
        const candidate = job.candidates.find(x => x.id === c.candidate_id);
        row.append(makeElement('p', '', `修订建议 · ${candidate?.learning.target.title || c.candidate_id}：${c.suggestion}`));
      });
      if (result.outcome === 'revise_candidates') row.append(makeElement('p', 'practice-hint', '请在原候选中审核并修改目标表述；原引用不能在这里替换。修改后重新归组，旧复查失效。'));
    }
    if (['queued', 'running'].includes(review.status)) row.append(button('暂停复查', `/api/outline-source-reviews/${review.id}/pause`, {}));
    if (['paused', 'failed'].includes(review.status)) row.append(button('增加额度并继续复查', `/api/outline-source-reviews/${review.id}/continue`, () => ({ call_limit: callLimit() }), review.stale || job.stale || job.processing || outlineJoining));
    if (review.status === 'completed' && !review.applied_group_id && ['same_knowledge', 'duplicate_targets', 'distinct'].includes(result?.outcome)) {
      let representative;
      if (result.outcome === 'duplicate_targets') {
        const choices = Object.fromEntries(Object.keys(group.members).map(id => [id, job.candidates.find(c => c.id === id).learning.target.title]));
        representative = choice(choices, result.canonical_id, '复查建议保留的代表目标');
        representative.disabled = review.stale || job.stale || outlineJoining;
        row.append(representative);
      }
      row.append(button(result.outcome === 'distinct' ? '采用复查：保留独立目标' : '采用复查归组结论', `/api/outline-source-reviews/${review.id}/apply`, () => ({
        group_revision: review.group_revision, canonical_id: representative?.value || null,
        unit_id: result.outcome === 'distinct' ? null : form.elements.namedItem('unit_id').value || null,
      }), review.stale || job.stale || outlineJoining));
    }
    section.append(row);
  });
  form.append(section);
}
