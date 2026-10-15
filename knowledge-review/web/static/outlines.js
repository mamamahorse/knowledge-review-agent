const outlineStates = { queued: "等待生成", running: "正在分析", paused: "已暂停", failed: "未全部完成，可继续", completed: "分析完成" };
const chapterStates = { pending: "待分析", partial: "部分分析", failed: "分析失败", completed: "已分析" };
let outlineJob = null;
let outlineEpoch = 0;
let outlineTimer = null;
let outlineJoining = false;
let outlineRequestId = null;
let outlineDrafts = new Map();
let outlineGroupDrafts = new Map();
let outlineGroupRequest = null;
const outlineGroupAllowances = new Map();

function outlineError(error) { setStatus("outline-status", error.message, true); }

async function refreshOutlines(preferred = null) {
  const epoch = ++outlineEpoch;
  clearTimeout(outlineTimer);
  const collection = $("learning-collection").value || "root";
  const rows = await request(`/api/collections/${collection}/outlines`);
  if (epoch !== outlineEpoch) return;
  let saved;
  try { saved = localStorage.getItem(`knowledge-review.outline.${collection}`); } catch { }
  const selected = preferred || (outlineJob?.collection_id === collection ? outlineJob.id : saved);
  $("outline-list").replaceChildren();
  rows.forEach((row) => {
    const option = makeElement("option", "", `${localDate(row.created_at)} · ${row.stale ? "资料范围已变化 · 仅供查看" : outlineStates[row.status]}${row.objective ? ` · ${row.objective}` : ""}`);
    option.value = row.id;
    $("outline-list").append(option);
  });
  if (!rows.length) {
    outlineJob = null;
    $("outline-detail").replaceChildren(makeElement("p", "practice-empty", "还没有清单。生成后可先审核，再选择加入计划。"));
    return;
  }
  $("outline-list").value = rows.some((r) => r.id === selected) ? selected : rows[0].id;
  await loadOutline($("outline-list").value, epoch);
}

async function loadOutline(id, epoch = outlineEpoch) {
  const job = await request(`/api/outlines/${id}`);
  if (epoch !== outlineEpoch || job.collection_id !== $("learning-collection").value) return;
  if (outlineJob?.id !== id) { outlineDrafts = new Map(); outlineGroupDrafts = new Map(); }
  outlineJob = job;
  try { localStorage.setItem(`knowledge-review.outline.${job.collection_id}`, id); } catch { }
  if (!outlineJoining) renderOutline(job);
  clearTimeout(outlineTimer);
  if (job.status === "running" || job.status === "queued" || job.processing || job.candidates.some((c) => c.status === "enrolling")) {
    outlineTimer = setTimeout(() => loadOutline(id, epoch).catch(outlineError), 2500);
  }
}

function outlineField(form, key, label, value, multiline = false, maxLength = 2000) {
  const wrapper = makeElement("label", "outline-field", label);
  const input = makeElement(multiline ? "textarea" : "input");
  input.name = key;
  input.value = value;
  input.maxLength = maxLength;
  if (multiline) input.rows = 3;
  wrapper.append(input);
  form.append(wrapper);
  return input;
}

function editedLearning(form, candidate) {
  const learning = structuredClone(candidate.learning);
  learning.knowledge.name = form.elements.namedItem("name").value;
  learning.knowledge.statement = form.elements.namedItem("statement").value;
  ["title", "description", "scope", "capability"].forEach((key) => { learning.target[key] = form.elements.namedItem(key).value; });
  learning.target.criteria = form.elements.namedItem("criteria").value.split("\n").map((s) => s.trim()).filter(Boolean);
  return learning;
}

function captureOutlineEdits() {
  document.querySelectorAll("form[data-outline-candidate]").forEach((form) => {
    const id = form.dataset.outlineCandidate;
    const candidate = outlineJob?.candidates.find((c) => c.id === id);
    if (candidate) outlineDrafts.set(id, { learning: editedLearning(form, candidate),
      selected: form.elements.namedItem("selected").checked, unit: form.elements.namedItem("unit").value,
      open: form.querySelector("details").open });
  });
  document.querySelectorAll('form[data-outline-group]').forEach((form) => {
    outlineGroupDrafts.set(form.dataset.outlineGroup, {
      revision: Number(form.dataset.revision), name: form.elements.namedItem('name').value,
      reason: form.elements.namedItem('reason').value, canonical_id: form.elements.namedItem('canonical_id')?.value || null,
      unit_id: form.elements.namedItem('unit_id').value || null,
    });
  });
}

function renderOutline(job) {
  const active = document.activeElement;
  const activeForm = active?.closest("form[data-outline-candidate], form[data-outline-group]");
  const focus = activeForm ? { id: activeForm.dataset.outlineCandidate || activeForm.dataset.outlineGroup,
    group: Boolean(activeForm.dataset.outlineGroup), name: active.name,
    start: active.selectionStart, end: active.selectionEnd } : null;
  captureOutlineEdits();
  const panel = $("outline-detail");
  const previousDestination = panel.querySelector("select[data-outline-destination]")?.value;
  panel.replaceChildren();
  const listOption = [...$("outline-list").options].find((option) => option.value === job.id);
  if (listOption) listOption.textContent = `${localDate(job.created_at)} · ${outlineStates[job.status]}${job.objective ? ` · ${job.objective}` : ""}`;
  panel.append(makeElement("p", "practice-state", `${outlineStates[job.status]} · ${job.coverage.filter((s) => s.status === "completed").length}/${job.coverage.length} 个章节已分析 · ${job.candidates.length} 个候选目标`));
  if (job.reading_plan) {
    const reading = makeElement("details", "learning-details");
    reading.append(makeElement("summary", "", "阅读方式与整体背景"));
    job.reading_plan.documents.forEach((doc) => {
      const name = job.coverage.find((s) => s.source_id === doc.source_id)?.name || "文档";
      reading.append(makeElement("p", "practice-state", `${name} · ${doc.mode === "full" ? "全文阅读" : "分层阅读"} · ${doc.reason}`));
    });
    const labels = { excerpt: "片段概述", chapter: "章节归并", document: "文档概览" };
    const steps = job.context_steps || [];
    reading.append(makeElement("p", "practice-hint", `整体背景整理 ${steps.filter((s) => s.status === "completed").length}/${steps.length} 步完成。概述帮助理解，知识依据仍来自原文；章节分析进度不代表掌握程度。`));
    steps.forEach((step) => {
      const item = makeElement("details", "learning-details");
      item.append(makeElement("summary", "", `${labels[step.kind] || "背景整理"} · ${step.status === "superseded" ? "旧步骤已替代" : chapterStates[step.status] || "正在整理"}${step.error ? ` · ${step.error}` : ""}`));
      (step.result?.points || []).forEach((point) => item.append(makeElement("p", "", point.text)));
      (step.result?.unresolved || []).forEach((issue) => item.append(makeElement("p", "practice-state", `待核对：${issue}`)));
      reading.append(item);
    });
    panel.append(reading);
  }
  if (job.stale) panel.append(makeElement("p", "request-status error", "资料或集合范围已经变化。清单、整合结果和学习路线均为旧范围，仅供查看；请根据当前资料重新生成。已有题目与复习日期保留。"));
  const controls = makeElement("div", "learning-actions");
  const pause = makeElement("button", "practice-action secondary", "暂停分析");
  pause.type = "button";
  pause.disabled = !["queued", "running"].includes(job.status);
  pause.addEventListener("click", async () => {
    try { await jsonRequest(`/api/outlines/${job.id}/pause`, {}); await loadOutline(job.id); }
    catch (error) { outlineError(error); }
  });
  const resume = makeElement("button", "practice-action", "继续未完成的章节");
  resume.type = "button";
  resume.disabled = job.stale || job.processing || ["running", "queued", "completed"].includes(job.status);
  resume.addEventListener("click", async () => {
    resume.disabled = true;
    try { await jsonRequest(`/api/outlines/${job.id}/resume`, {}); await loadOutline(job.id); }
    catch (error) { outlineError(error); resume.disabled = false; }
  });
  controls.append(pause, resume);
  panel.append(controls);
  const coverage = makeElement("details", "learning-details");
  coverage.open = true;
  coverage.append(makeElement("summary", "", "章节覆盖 · 分析范围与计划收录"));
  job.coverage.forEach((section) => {
    const row = makeElement("div", "outline-chapter");
    row.append(makeElement("p", "", `${section.name} / ${section.heading_path.join(" › ")}`));
    row.append(makeElement("p", "practice-state", `${section.has_content ? chapterStates[section.status] : "无正文，无需生成目标"} · L${section.line_start}–L${section.line_end} · ${section.analyzed_lines}/${section.total_lines} 行 · 候选 ${section.proposed_targets} · 已加入 ${section.enrolled_targets}`));
    const detail = makeElement("details");
    detail.append(makeElement("summary", "", "查看分段分析记录"));
    section.ranges.forEach((range) => detail.append(makeElement("p", "practice-state", `L${range.line_start}–L${range.line_end} · ${chapterStates[range.status] || "正在分析"}${range.error ? ` · ${range.error}` : range.note ? ` · ${range.note}` : ""}`)));
    row.append(detail);
    coverage.append(row);
  });
  panel.append(coverage);
  renderOutlineGrouping(job, panel);
  renderOutlineRoutes(job, panel);
  const selection = makeElement("div", "learning-actions");
  const destination = choice(collectionOptions(), previousDestination || job.collection_id, "将所选目标加入哪个集合");
  destination.dataset.outlineDestination = "true";
  const selectAll = makeElement("button", "practice-action secondary", "勾选待审核目标");
  selectAll.type = "button";
  selectAll.disabled = job.stale || outlineJoining;
  selectAll.addEventListener("click", () => panel.querySelectorAll('input[name="selected"]:not(:disabled)').forEach((input) => { input.checked = true; }));
  const enroll = makeElement("button", "practice-action", "将勾选目标加入计划");
  enroll.type = "button";
  enroll.disabled = job.stale || outlineJoining;
  enroll.addEventListener("click", () => enrollOutlineSelection(job, destination.value, enroll));
  selection.append(makeElement("span", "", "加入到"), destination, selectAll, enroll);
  panel.append(selection);
  panel.append(makeElement("p", "practice-hint", "加入时会按审核后的目标生成首道练习。未勾选目标只保存在清单中，不产生复习或掌握记录。"));
  job.candidates.forEach((candidate, index) => {
    const card = makeElement("article", "learning-item outline-candidate");
    const form = makeElement("form", "outline-edit");
    form.dataset.outlineCandidate = candidate.id;
    const saved = outlineDrafts.get(candidate.id);
    const learning = candidate.status === "proposed" && saved ? saved.learning : candidate.learning;
    const selectedLabel = makeElement("label", "outline-check");
    const selected = makeElement("input");
    selected.type = "checkbox";
    selected.name = "selected";
    selected.checked = candidate.status === "proposed" && Boolean(saved?.selected);
    selected.disabled = candidate.status !== "proposed" || candidate.suppressed || job.stale || outlineJoining;
    selectedLabel.append(selected, makeElement("span", "", `${index + 1}. ${learning.knowledge.name} · ${candidate.status === "enrolled" ? "已加入计划" : candidate.status === "enrolling" ? "正在加入" : candidate.suppressed ? "重复记录，加入代表目标即可" : "待审核"}`));
    form.append(selectedLabel);
    const editor = makeElement("details", "learning-details");
    editor.open = Boolean(saved?.open);
    editor.append(makeElement("summary", "", `审核与修改 · ${capabilities[learning.target.capability]} · ${learning.target.title}`));
    form.append(editor);
    outlineField(editor, "name", "知识点名称", learning.knowledge.name, false, 160);
    outlineField(editor, "statement", "知识说明", learning.knowledge.statement, true);
    outlineField(editor, "title", "学习目标", learning.target.title, false, 200);
    const capability = choice(capabilities, learning.target.capability, "目标能力");
    capability.name = "capability";
    const capabilityLabel = makeElement("label", "outline-field", "能力维度");
    capabilityLabel.append(capability);
    editor.append(capabilityLabel);
    outlineField(editor, "description", "需要能做到什么", learning.target.description, true);
    outlineField(editor, "scope", "考察范围", learning.target.scope, true);
    outlineField(editor, "criteria", "判断标准（每行一条，最多四条）", learning.target.criteria.join("\n"), true, 8000);
    const units = choice({ "": "创建新知识点", ...Object.fromEntries(learningGroups.map((g) => [g.id, g.name])) }, saved?.unit || "", "知识点归组");
    units.name = "unit";
    const unitLabel = makeElement("label", "outline-field", "加入计划时归入");
    unitLabel.append(units);
    form.append(unitLabel);
    if (candidate.group_id) {
      const confirmed = job.grouping.suggestions.find(g => g.id === candidate.group_id);
      units.querySelector('option[value=""]').textContent = '按已确认归组统一分配';
      units.value = confirmed?.unit_id || '';
      form.append(makeElement('p', 'practice-hint', '使用已确认归组的统一知识点。修改目标会使相关归组失效，请保存修改、重新审核后再加入。'));
    }
    const save = makeElement("button", "practice-action secondary", "保存审核修改");
    save.type = "submit";
    editor.append(save);
    form.querySelectorAll("input,textarea,select,button").forEach((input) => { if (input !== selected) input.disabled = candidate.status !== "proposed" || outlineJoining; });
    if (candidate.group_id) units.disabled = true;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      save.disabled = true;
      try { await saveOutlineCandidate(form, candidate); await loadOutline(job.id); setStatus("outline-status", "审核修改已保存。"); }
      catch (error) { outlineError(error); save.disabled = false; }
    });
    card.append(form);
    candidate.citations.forEach((citation) => card.append(practiceCitation(citation)));
    if (candidate.error) card.append(makeElement("p", "request-status error", candidate.error));
    if (candidate.status === "enrolled") {
      const practice = makeElement("button", "practice-action", "打开首次练习");
      practice.type = "button";
      practice.addEventListener("click", async () => {
        try { await showLearningSession(await request(`/api/practice/${candidate.session_id}`)); }
        catch (error) { outlineError(error); }
      });
      card.append(practice);
    }
    panel.append(card);
  });
  if (focus) {
    const input = panel.querySelector(`form[data-outline-${focus.group ? 'group' : 'candidate'}="${focus.id}"]`)?.elements.namedItem(focus.name);
    if (input && !input.disabled) {
      input.focus({ preventScroll: true });
      if (focus.start !== null && typeof input.setSelectionRange === "function") input.setSelectionRange(focus.start, focus.end);
    }
  }
}

async function saveOutlineCandidate(form, candidate) {
  const learning = editedLearning(form, candidate);
  if (JSON.stringify(learning) !== JSON.stringify(candidate.learning)) {
    const result = await jsonRequest(`/api/outline-candidates/${candidate.id}`, { learning, revision: candidate.revision }, "PATCH");
    candidate.revision = result.revision;
    candidate.learning = learning;
  }
}

async function enrollOutlineSelection(job, destination, button) {
  const forms = [...document.querySelectorAll("form[data-outline-candidate]")].filter((f) => f.elements.namedItem("selected").checked && !f.elements.namedItem("selected").disabled);
  if (!forms.length) return outlineError(new Error("请先审核并勾选要加入计划的目标。"));
  outlineJoining = true;
  button.disabled = true;
  forms.forEach((form) => form.querySelectorAll("input,textarea,select,button").forEach((input) => { input.disabled = true; }));
  clearTimeout(outlineTimer);
  const epoch = outlineEpoch;
  let count = 0;
  let failure = null;
  try {
    for (const form of forms) {
      const candidate = job.candidates.find((c) => c.id === form.dataset.outlineCandidate);
      if (candidate.group_id && JSON.stringify(editedLearning(form, candidate)) !== JSON.stringify(candidate.learning)) {
        throw new Error('已归组目标还有未保存修改。请先保存并重新审核归组，再加入计划。');
      }
      setStatus("outline-status", `正在加入第 ${count + 1}/${forms.length} 个目标，生成首次练习……`);
      await saveOutlineCandidate(form, candidate);
      await jsonRequest(`/api/outline-candidates/${candidate.id}/enroll`, { revision: candidate.revision,
        collection_id: destination, unit_id: form.elements.namedItem("unit").value || null, response_language: responseLanguage() });
      count += 1;
    }
  } catch (error) { failure = error; }
  finally {
    outlineJoining = false;
    await refreshLearning();
    if (epoch === outlineEpoch) {
      await loadOutline(job.id);
      setStatus("outline-status", `已加入 ${count} 个目标。${failure ? `其余未加入：${failure.message}` : "未勾选的目标仍保存在待审核清单。"}`, Boolean(failure));
    }
  }
}

function renderOutlineGrouping(job, panel) {
  const section = makeElement('section', 'learning-item');
  section.append(makeElement('h3', '', '跨章节知识归组建议'));
  section.append(makeElement('p', 'practice-hint', '默认整理全部未归组候选，也可勾选部分目标。按预算自动选择直接或分层比较；跨批关联补回原文核验。建议需你确认，不会自动加入计划。'));
  const allowance = document.createElement('input');
  allowance.type = 'number'; allowance.min = '1'; allowance.max = '200';
  allowance.value = outlineGroupAllowances.get(job.id) || '24';
  allowance.addEventListener('input', () => outlineGroupAllowances.set(job.id, allowance.value));
  allowance.setAttribute('aria-label', '本次最多模型请求次数');
  const allowanceLabel = makeElement('label', 'practice-hint', '本次最多模型请求次数 ');
  allowanceLabel.append(allowance); section.append(allowanceLabel);
  const callLimit = () => {
    const n = Number(allowance.value);
    if (!Number.isInteger(n) || n < 1 || n > 200) throw new Error('模型请求次数请填写 1–200 的整数。');
    return n;
  };
  const generate = makeElement('button', 'practice-action secondary', '生成归组建议');
  generate.type = 'button';
  generate.disabled = job.status !== 'completed' || job.stale || job.processing || outlineJoining;
  generate.addEventListener('click', async () => {
    generate.disabled = true;
    try {
      const forms = [...document.querySelectorAll('form[data-outline-candidate]')];
      const selected = forms.filter(f => f.elements.namedItem('selected').checked && !f.elements.namedItem('selected').disabled);
      const eligible = job.candidates.filter(c => c.status === 'proposed' && !c.group_id);
      const ids = selected.length ? selected.map(f => f.dataset.outlineCandidate) : eligible.map(c => c.id);
      if (ids.length < 2) throw new Error('至少需要两个未归组、未加入计划的候选。');
      if (!ids.every(id => eligible.some(c => c.id === id))) throw new Error('已确认归组的目标无需重复整理，请取消它们的勾选。');
      for (const form of forms.filter(f => ids.includes(f.dataset.outlineCandidate))) {
        await saveOutlineCandidate(form, job.candidates.find(c => c.id === form.dataset.outlineCandidate));
      }
      const key = JSON.stringify([job.id, ids.map(id => [id, job.candidates.find(c => c.id === id).revision]), responseLanguage()]);
      if (outlineGroupRequest?.key !== key) outlineGroupRequest = { key, id: crypto.randomUUID() };
      await jsonRequest(`/api/outlines/${job.id}/grouping`, {
        request_id: outlineGroupRequest.id, candidate_ids: ids, response_language: responseLanguage(),
        call_limit: callLimit(),
      });
      outlineGroupRequest = null;
      await loadOutline(job.id);
      setStatus('outline-status', '归组任务已保存，可在完成后审核；原始候选保留。');
    } catch (error) { outlineError(error); generate.disabled = false; }
  });
  section.append(generate);
  const grouping = job.grouping || { runs: [], suggestions: [] };
  const labels = { proposed: '待审核', accepted: '已接受', rejected: '保留独立', stale: '已失效，原始记录保留' };
  const kinds = { same_knowledge: '同一知识点，保留不同目标', duplicate_targets: '重复目标，保留代表', needs_review: '条件或证据待核对' };
  grouping.runs.forEach(run => {
    const row = makeElement('div');
    row.append(makeElement('p', 'practice-state', `${localDate(run.created_at)} · 比较 ${run.candidate_ids.length} 个候选 · ${{ queued: '等待比较', running: '正在比较', completed: '比较完成', failed: '比较失败，可重试', paused: '已暂停，可继续' }[run.status]}${run.note ? ` · ${run.note}` : ''}`));
    if (run.plan) {
      const p = run.progress;
      const mode = { direct: '直接比较', hierarchical: '分层比较' }[run.plan.mode] || '正在准备';
      row.append(makeElement('p', 'practice-hint', `${mode} · ${run.plan.reason || '正在准备预算与步骤'} · 请求 ${run.calls_used}/${run.allowance}。`));
      row.append(makeElement('p', 'practice-hint', `比较进度 ${p.handled_pairs}/${p.total_pairs}；涉及候选 ${p.handled_candidates}/${p.total_candidates}；原文核验 ${p.verified_pairs}，待核验关联 ${p.pending_verification}。模型回执不代表所有语义关系已发现。`));
      const details = document.createElement('details');
      details.append(makeElement('summary', '', `查看 ${run.steps.length} 个步骤、结果与依据`));
      (run.steps || []).forEach(step => {
        const names = { evidence: '局部原文比较', route: '跨批卡片筛选', verify: '补回原文核验' };
        details.append(makeElement('p', 'practice-hint', `${names[step.kind] || step.kind} · ${step.scope} · ${step.status} · ${step.inputs.length} 对比较${step.error ? ` · ${step.error}` : ''}`));
        if (step.diagnostic) details.append(makeElement('p', 'practice-hint', `完整输入估计 ${step.diagnostic.estimated_input_tokens}/${step.diagnostic.input_limit}，输出预留 ${step.diagnostic.output_reserved}。`));
        if (step.result) {
          details.append(makeElement('p', '', step.result.note));
          step.result.suggestions.forEach(g => details.append(makeElement('p', '', `${kinds[g.kind]} · ${g.name} · ${g.reason}`)));
        }
      });
      row.append(details);
    }
    if (run.diagnostic) row.append(makeElement('p', 'practice-hint', `完整请求保守估计 ${run.diagnostic.estimated_input_tokens} / ${run.diagnostic.input_limit}；输出预留 ${run.diagnostic.output_reserved}。比较范围不代表语义完整性。`));
    if (run.error) row.append(makeElement('p', 'request-status error', run.error));
    if (run.status === 'running' || run.status === 'queued') {
      const pause = makeElement('button', 'practice-action secondary', '暂停整合');
      pause.type = 'button';
      pause.addEventListener('click', async () => {
        pause.disabled = true;
        try { await jsonRequest(`/api/outline-group-runs/${run.id}/pause`, {}); await loadOutline(job.id); }
        catch (error) { outlineError(error); pause.disabled = false; }
      });
      row.append(pause);
    }
    if (run.status === 'failed' || run.status === 'paused') {
      const retry = makeElement('button', 'practice-action secondary', '继续未完成比较');
      retry.type = 'button'; retry.disabled = job.stale || job.processing || outlineJoining;
      retry.addEventListener('click', async () => {
        retry.disabled = true;
        try { await jsonRequest(`/api/outline-group-runs/${run.id}/retry`, { call_limit: callLimit() }); await loadOutline(job.id); }
        catch (error) { outlineError(error); retry.disabled = false; }
      });
      row.append(retry);
    }
    section.append(row);
  });
  grouping.suggestions.forEach(group => {
    const form = makeElement('form', 'learning-item');
    form.dataset.outlineGroup = group.id; form.dataset.revision = group.revision;
    const saved = outlineGroupDrafts.get(group.id);
    const draft = group.status === 'proposed' && saved?.revision === group.revision ? saved : group.proposal;
    const superseded = (grouping.reviews || []).some(r => r.group_id === group.id && r.applied_group_id && r.applied_group_id !== group.id);
    form.append(makeElement('p', 'practice-state', `${kinds[group.proposal.kind]} · ${superseded ? '原建议保留，已采用复查结论' : labels[group.status]}`));
    if (group.support?.length) form.append(makeElement('p', 'practice-hint', `由 ${group.support.length} 个原文核验步骤支持；原模型结果在上方步骤中保留。重叠建议是备选分组，不能同时接受。`));
    const names = Object.fromEntries(Object.keys(group.members).map(id => {
      const index = job.candidates.findIndex(c => c.id === id);
      const c = job.candidates[index];
      return [id, `${index + 1}. ${c.learning.target.title} · ${capabilities[c.learning.target.capability]}`];
    }));
    Object.entries(names).forEach(([id, text]) => {
      const link = makeElement('button', 'practice-action secondary', `${text} · 查看原始候选与证据`);
      link.type = 'button';
      link.addEventListener('click', () => panel.querySelector(`form[data-outline-candidate="${id}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'center' }));
      form.append(link);
    });
    outlineField(form, 'name', '归组名称', draft.name, false, 160);
    outlineField(form, 'reason', '归组说明与条件', draft.reason, true, 600);
    if (group.proposal.kind === 'duplicate_targets') {
      const representative = choice(names, draft.canonical_id, '保留哪个代表目标');
      representative.name = 'canonical_id'; form.append(representative);
    }
    const units = choice({ '': '首次加入时创建统一知识点', ...Object.fromEntries(learningGroups.map(g => [g.id, g.name])) }, group.status === 'proposed' && saved?.revision === group.revision ? saved.unit_id || '' : group.unit_id || '', '归入已有知识点（可选）');
    units.name = 'unit_id'; form.append(units);
    form.querySelectorAll('input,textarea,select').forEach(input => { input.disabled = group.status !== 'proposed' || job.stale || outlineJoining; });
    const actions = makeElement('div', 'learning-actions');
    [['proposed', '保存建议修改'], ['accepted', '接受建议'], ['rejected', group.status === 'accepted' ? '撤销归组，已有计划不变' : '保留独立目标']].forEach(([decision, text]) => {
      if (group.status !== 'proposed' && !(group.status === 'accepted' && decision === 'rejected')) return;
      if (decision === 'accepted' && group.proposal.kind === 'needs_review') return;
      const button = makeElement('button', 'practice-action secondary', text);
      button.type = 'button'; button.disabled = outlineJoining || (job.stale && decision !== 'rejected');
      button.addEventListener('click', async () => {
        actions.querySelectorAll('button').forEach(b => { b.disabled = true; });
        try {
          await jsonRequest(`/api/outline-groups/${group.id}`, {
            revision: group.revision, decision, name: form.elements.namedItem('name').value,
            reason: form.elements.namedItem('reason').value,
            canonical_id: form.elements.namedItem('canonical_id')?.value || null,
            unit_id: form.elements.namedItem('unit_id').value || null,
          }, 'PATCH');
          outlineGroupDrafts.delete(group.id);
          await loadOutline(job.id);
          setStatus('outline-status', decision === 'accepted' ? '归组已确认。请继续勾选目标加入计划，各目标仍独立复习。' : '审核决定已保存，原始候选与已有计划保留。');
        } catch (error) { outlineError(error); actions.querySelectorAll('button').forEach(b => { b.disabled = false; }); }
      });
      actions.append(button);
    });
    form.append(actions);
    renderOutlineReviews(job, group, form, callLimit);
    form.addEventListener('submit', event => event.preventDefault());
    section.append(form);
  });
  panel.append(section);
}

$("outline-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("outline-create").disabled = true;
  setStatus("outline-status", "正在准备章节并启动分析……");
  const key = JSON.stringify([$("learning-collection").value, $("outline-objective").value, responseLanguage()]);
  if (outlineRequestId?.key !== key) outlineRequestId = { key, id: crypto.randomUUID() };
  try {
    const result = await jsonRequest(`/api/collections/${$("learning-collection").value}/outlines`, {
      request_id: outlineRequestId.id, objective: $("outline-objective").value, response_language: responseLanguage() });
    outlineRequestId = null;
    await refreshOutlines(result.id);
    setStatus("outline-status", "分析已启动，可查看章节进度，也可暂停后继续。");
  } catch (error) { outlineError(error); }
  finally { $("outline-create").disabled = false; }
});
$("outline-list").addEventListener("change", () => {
  ++outlineEpoch;
  clearTimeout(outlineTimer);
  loadOutline($("outline-list").value).catch(outlineError);
});
$("learning-collection").addEventListener("change", () => refreshOutlines().catch(outlineError));
