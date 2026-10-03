const state = { documents: [], selectedIds: new Set(), current: null, focus: null, view: "rendered", practiceId: null, practice: null };
const $ = (id) => document.getElementById(id);

function restoreResponseLanguage() {
  try {
    const saved = localStorage.getItem("knowledge-review.response-language");
    if (saved === "zh" || saved === "en") $("response-language").value = saved;
  } catch { /* The selector still works when browser storage is unavailable. */ }
}

function responseLanguage() {
  return $("response-language").value;
}

function setStatus(id, message, isError = false) {
  const node = $(id);
  node.textContent = message;
  node.classList.toggle("error", isError);
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  let body;
  try { body = await response.json(); } catch { body = {}; }
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "请求未完成，请稍后再试。");
  return body;
}

function renderCatalog() {
  const list = $("document-list");
  list.replaceChildren();
  for (const doc of state.documents) {
    const row = document.createElement("div");
    row.className = "document-row" + (state.current?.id === doc.id ? " active" : "");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.current?.id === doc.id || state.selectedIds.has(doc.id);
    checkbox.disabled = state.current?.id === doc.id;
    checkbox.setAttribute("aria-label", `本次使用 ${doc.name}`);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.selectedIds.add(doc.id);
      else state.selectedIds.delete(doc.id);
    });
    const name = document.createElement("button");
    name.className = "document-name";
    name.type = "button";
    name.textContent = doc.name;
    name.title = doc.name;
    name.addEventListener("click", () => openDocument(doc.id));
    const icon = document.createElement("span");
    icon.className = "document-icon";
    icon.textContent = "↗";
    row.append(checkbox, name, icon);
    list.append(row);
  }
}

async function refreshCatalog() {
  state.documents = await request("/api/documents");
  renderCatalog();
  if (!state.current && state.documents.length) await openDocument(state.documents[0].id);
}

function clearFocus() {
  state.focus = null;
  $("selection-card").classList.add("empty");
  $("selection-range").textContent = state.current ? `${state.current.name} · 整篇文档` : "打开一份文档即可提问";
  $("selection-preview").textContent = "模型按需要查阅当前文档。选中原文可添加提问焦点，也可勾选其他资料一起使用。";
  $("line-controls").hidden = true;
  $("clear-focus").hidden = true;
  $("practice-selection").textContent = "先在阅读区选中一段原文。";
}

function showFocus(start, end) {
  if (!state.current) return;
  state.focus = { documentId: state.current.id, sourceId: state.current.source_id, start, end };
  $("selection-card").classList.remove("empty");
  $("selection-range").textContent = `${state.current.name} · L${start}–L${end}`;
  $("selection-preview").textContent = state.current.lines.slice(start - 1, end).join("\n").slice(0, 400);
  $("line-start").value = start;
  $("line-end").value = end;
  $("line-start").max = state.current.line_count;
  $("line-end").max = state.current.line_count;
  $("line-controls").hidden = false;
  $("clear-focus").hidden = false;
  $("practice-selection").textContent = `当前选段：${state.current.name} · L${start}–L${end}`;
}

function renderSourceLines(lines) {
  const container = $("source-note");
  container.replaceChildren();
  const fragment = document.createDocumentFragment();
  lines.forEach((text, index) => {
    const row = document.createElement("div");
    row.className = "source-line";
    row.dataset.lineStart = String(index + 1);
    row.dataset.lineEnd = String(index + 1);
    const number = document.createElement("span");
    number.className = "line-number";
    number.textContent = String(index + 1);
    const content = document.createElement("span");
    content.className = "line-content";
    content.textContent = text || " ";
    row.append(number, content);
    fragment.append(row);
  });
  container.append(fragment);
}

async function openDocument(id, expectedSourceId = null) {
  try {
    const doc = await request(`/api/documents/${encodeURIComponent(id)}`);
    state.current = doc;
    clearFocus();
    $("document-title").textContent = doc.name;
    $("document-meta").textContent = `${doc.line_count} 行 · 版本 ${doc.source_id.slice(0, 12)}`;
    $("rendered-note").innerHTML = doc.html;
    renderSourceLines(doc.lines);
    $("reader-empty").hidden = true;
    renderCatalog();
    $("reader-scroll").scrollTop = 0;
    if (expectedSourceId && doc.source_id !== expectedSourceId) {
      setStatus("request-status", "这份笔记已修改，引用属于旧版本。请查看回答中的原文摘录。", true);
    }
    return doc;
  } catch (error) {
    setStatus("request-status", error.message, true);
    return null;
  }
}

function setView(view) {
  state.view = view;
  $("rendered-note").hidden = view !== "rendered";
  $("source-note").hidden = view !== "source";
  $("view-rendered").classList.toggle("active", view === "rendered");
  $("view-source").classList.toggle("active", view === "source");
}

function captureSelection() {
  if (!state.current) return;
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || !selection.rangeCount) return;
  const range = selection.getRangeAt(0);
  const reader = state.view === "source" ? $("source-note") : $("rendered-note");
  if (!reader.contains(range.startContainer) || !reader.contains(range.endContainer)) return;
  const matched = [...reader.querySelectorAll("[data-line-start][data-line-end]")]
    .filter((element) => range.intersectsNode(element));
  const leaves = matched.filter((element) => !matched.some(
    (other) => other !== element && element.contains(other)
  ));
  if (!leaves.length) return;
  const start = Math.min(...leaves.map((element) => Number(element.dataset.lineStart)));
  const end = Math.max(...leaves.map((element) => Number(element.dataset.lineEnd)));
  if (start > 0 && end >= start) showFocus(start, end);
}

function makeElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

async function showCitation(citation) {
  const doc = await openDocument(citation.document_id, citation.source_id);
  if (!doc || doc.source_id !== citation.source_id) return;
  setView("source");
  document.querySelectorAll(".citation-highlight").forEach((node) => node.classList.remove("citation-highlight"));
  const lines = [...$("source-note").querySelectorAll(".source-line")];
  for (let number = citation.line_start; number <= citation.line_end; number++) {
    lines[number - 1]?.classList.add("citation-highlight");
  }
  lines[citation.line_start - 1]?.scrollIntoView({ behavior: "smooth", block: "center" });
}

function renderAnswer(result) {
  const container = $("answer-result");
  container.replaceChildren();
  const heading = makeElement("div", "result-heading");
  heading.append(makeElement("h3", "", "回答"));
  const coverage = { sufficient: "依据充分", partial: "部分依据", none: "资料不足" };
  heading.append(makeElement("span", "coverage-badge", coverage[result.coverage] || result.coverage));
  container.append(heading);
  if (result.coverage_note) container.append(makeElement("p", "coverage-note", result.coverage_note));
  const kinds = { grounded: "资料依据", inference: "分析与推导", supplement: "模型补充 · 未外部核验" };
  for (const block of result.blocks) {
    const section = makeElement("section", `answer-block ${block.kind}`);
    section.append(makeElement("span", "block-kind", kinds[block.kind] || block.kind));
    section.append(makeElement("p", "answer-text", block.text));
    for (const citation of block.citations) {
      const button = makeElement("button", "citation", `${citation.name} · L${citation.line_start}–L${citation.line_end} ↗`);
      button.type = "button";
      button.append(makeElement("small", "", citation.excerpt));
      button.addEventListener("click", () => showCitation(citation));
      section.append(button);
    }
    container.append(section);
  }
  const details = makeElement("details", "step-list");
  details.append(makeElement("summary", "", `执行步骤 · ${result.steps.length} 次工具调用 · ${result.stop_reason}`));
  const list = makeElement("ol", "");
  result.steps.forEach((step) => {
    const item = makeElement("li", "", `${step.name}${step.error ? ` · 错误：${step.error}` : " · 成功"}`);
    item.append(makeElement("pre", "", JSON.stringify(step.arguments, null, 2)));
    list.append(item);
  });
  details.append(list);
  container.append(details);
}

async function askQuestion(event) {
  event.preventDefault();
  const focus = state.focus;
  const question = $("question").value.trim();
  const start = Number($("line-start").value);
  const end = Number($("line-end").value);
  if (!state.current) return setStatus("request-status", "请先打开一份文档。", true);
  if (focus && (!Number.isInteger(start) || !Number.isInteger(end) || start < 1 || end < start || end > state.current.line_count)) {
    return setStatus("request-status", "选段必须在当前原文范围内。", true);
  }
  if (!question) return setStatus("request-status", "请输入问题。", true);
  const documentIds = [...state.selectedIds];
  if (!documentIds.includes(state.current.id)) documentIds.push(state.current.id);
  if (focus) showFocus(start, end);
  const button = $("ask-button");
  button.disabled = true;
  setStatus("request-status", "正在分析问题并生成回答，必要时会查阅资料……");
  try {
    const result = await request("/api/ask", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        document_ids: documentIds, focus_document_id: state.current.id,
        focus_source_id: state.current.source_id,
        ...(focus ? { line_start: start, line_end: end } : {}),
        question, strict: $("strict-mode").checked,
        response_language: responseLanguage(),
      }),
    });
    const searched = result.steps.some((step) => step.name === "search_notes" && !step.error);
    const read = result.steps.some((step) => step.name === "read_source" && !step.error);
    const message = searched ? "已检索相关原文，回答依据可点击查看。"
      : read ? "已查阅原文，回答依据可点击查看。"
        : result.focus_context ? "已根据选段上下文生成回答，本次未调用资料工具。"
          : "本次未调用资料工具。";
    setStatus("request-status", `完成。${message}`);
    renderAnswer(result);
  } catch (error) {
    setStatus("request-status", error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function importFiles(event) {
  const files = [...event.target.files];
  if (!files.length) return;
  const data = new FormData();
  files.forEach((file) => data.append("files", file));
  setStatus("import-status", `正在导入 ${files.length} 份笔记……`);
  try {
    const imported = await request("/api/documents", { method: "POST", body: data });
    await refreshCatalog();
    await openDocument(imported[0].id);
    setStatus("import-status", `已导入 ${imported.length} 份笔记。`);
  } catch (error) {
    setStatus("import-status", error.message, true);
  } finally {
    event.target.value = "";
  }
}

function switchMode(mode) {
  $("ask-view").hidden = mode !== "ask";
  $("practice-view").hidden = mode !== "practice";
  $("mode-ask").classList.toggle("active", mode === "ask");
  $("mode-practice").classList.toggle("active", mode === "practice");
  $("learning-view").hidden = mode !== "learning";
  $("mode-learning").classList.toggle("active", mode === "learning");
}

function renderPracticeList(items) {
  const list = $("practice-list");
  list.replaceChildren();
  if (!items.length) {
    list.append(makeElement("p", "practice-empty", "还没有练习记录。"));
    return;
  }
  const statuses = { waiting_for_answer: "等待作答", grading: "待评分", finished: "已结束" };
  items.forEach((item) => {
    const button = makeElement("button", item.id === state.practiceId ? "active" : "");
    button.type = "button";
    button.append(makeElement("strong", "", item.objective));
    button.append(makeElement("small", "", `${statuses[item.status]} · 第 ${item.position} 题`));
    button.addEventListener("click", () => openPractice(item.id));
    list.append(button);
  });
}

async function refreshPracticeList() {
  const items = await request("/api/practice");
  renderPracticeList(items);
  if (!state.practiceId && items.length) {
    const active = items.find((item) => item.status !== "finished") || items[0];
    await openPractice(active.id);
  }
}

function practiceCitation(citation) {
  const button = makeElement("button", "citation", `${citation.name} · L${citation.line_start}–L${citation.line_end} ↗`);
  button.type = "button";
  button.title = citation.excerpt;
  button.append(makeElement("small", "", citation.excerpt));
  button.addEventListener("click", () => showCitation(citation));
  return button;
}

function renderPractice(session) {
  const detail = $("practice-detail");
  detail.replaceChildren();
  detail.append(makeElement("h3", "practice-title", session.objective));
  const statuses = { waiting_for_answer: "等待你的真实作答", grading: "答案已保存，等待评分", finished: "本轮练习已结束" };
  detail.append(makeElement("p", "practice-state", statuses[session.status]));
  session.questions.forEach((item, index) => {
    const current = index === session.questions.length - 1;
    const card = makeElement("article", `practice-question${current ? " current" : ""}`);
    card.append(makeElement("h4", "", `第 ${item.position} 题 · ${item.objective}`));
    card.append(makeElement("p", "prompt", item.question));
    item.citations.forEach((citation) => card.append(practiceCitation(citation)));

    if (item.answer_text !== null) {
      card.append(makeElement("span", "practice-label", "你的回答"));
      card.append(makeElement("p", "practice-response", item.answer_text));
      if (item.feedback !== null) {
        const ratings = { needs_review: "需要复习", developing: "理解尚有缺口", meets_standard: "达到本题标准", uncertain: "暂无法判断" };
        card.append(makeElement("span", "practice-rating", ratings[item.rating] || item.rating));
        if (item.answer_quotes?.length) {
          card.append(makeElement("span", "practice-label", "评分引用的作答原话"));
          item.answer_quotes.forEach((quote) => card.append(makeElement("p", "practice-answer-quote", `“${quote}”`)));
        }
        card.append(makeElement("p", "practice-feedback", item.feedback));
        if (item.rubric_points) {
          const heading = makeElement("span", "practice-label", "本题评分要点");
          const list = makeElement("ol", "rubric-list");
          item.rubric_points.forEach((point, pointIndex) => {
            const number = pointIndex + 1;
            const kind = item.covered_indices.includes(number) ? "covered" : "missing";
            list.append(makeElement("li", kind, `${kind === "covered" ? "已覆盖" : "待补充"}：${point}`));
          });
          card.append(heading, list);
        }
        if (item.teaching_note) {
          card.append(makeElement("span", "practice-label", "补充讲解"));
          card.append(makeElement("p", "practice-teaching", item.teaching_note));
        }
        if (item.correction_note) card.append(makeElement("p", "practice-correction-note", `你的更正：${item.correction_note}`));
        appendGradeControls(card, session, item);
      } else if (current && session.status === "grading") {
        card.append(makeElement("p", "practice-state", "评分尚未完成，原始答案已经保存。"));
        const retry = makeElement("button", "practice-action secondary", "重试评分");
        retry.type = "button";
        retry.addEventListener("click", () => retryPracticeGrade(retry));
        card.append(retry);
      }
    } else if (current && session.status === "waiting_for_answer") {
      const label = makeElement("label", "practice-label", "写下你的回答，再查看反馈");
      const answer = makeElement("textarea", "practice-answer");
      answer.placeholder = "用自己的话解释，并说明关键机制或适用条件……";
      answer.maxLength = 10000;
      label.append(answer);
      const submit = makeElement("button", "practice-action", "提交回答");
      submit.type = "button";
      const hint = makeElement("input", "");
      hint.type = "checkbox";
      const hintLabel = makeElement("label", "practice-label");
      hintLabel.append(hint, document.createTextNode(" 本次使用了提示或查看了资料"));
      submit.addEventListener("click", () => submitPracticeAnswer(answer.value, submit, hint.checked ? "hint_used" : "none_recorded"));
      card.append(hintLabel);
      card.append(label, submit);
      appendReferenceControl(card, session, item);
    }
    appendEnrollmentControl(card, session, item);
    detail.append(card);
  });
  if (session.status === "waiting_for_answer") {
    const finish = makeElement("button", "practice-finish", "结束本轮练习");
    finish.type = "button";
    finish.addEventListener("click", () => finishPractice(finish));
    detail.append(finish);
  }
}

async function openPractice(id) {
  try {
    const session = await request(`/api/practice/${encodeURIComponent(id)}`);
    state.practiceId = id;
    state.practice = session;
    renderPractice(session);
    renderPracticeList(await request("/api/practice"));
  } catch (error) {
    setStatus("practice-status", error.message, true);
  }
}

async function startPractice(event) {
  event.preventDefault();
  if (!state.focus || !state.current) {
    return setStatus("practice-status", "请先在阅读区选择一段原文。", true);
  }
  const start = Number($("line-start").value);
  const end = Number($("line-end").value);
  if (!Number.isInteger(start) || !Number.isInteger(end) || start < 1 || end < start || end > state.current.line_count || end - start + 1 > 200) {
    return setStatus("practice-status", "选段必须是原文范围内连续的 1–200 行。", true);
  }
  const button = $("practice-start-button");
  button.disabled = true;
  setStatus("practice-status", "正在根据原文生成题目与评分要点……");
  try {
    const documentIds = [...state.selectedIds];
    if (!documentIds.includes(state.focus.documentId)) documentIds.push(state.focus.documentId);
    const session = await request("/api/practice", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        document_ids: documentIds, focus_document_id: state.focus.documentId,
        focus_source_id: state.focus.sourceId, line_start: start, line_end: end,
        objective: $("practice-objective").value.trim(),
        response_language: responseLanguage(),
      }),
    });
    state.practiceId = session.id;
    state.practice = session;
    renderPractice(session);
    await refreshPracticeList();
    setStatus("practice-status", "题目与评分要点已保存。可以现在作答，也可以稍后继续。 ");
  } catch (error) {
    setStatus("practice-status", error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function submitPracticeAnswer(answer, button, assistance = "none_recorded") {
  if (!answer.trim()) return setStatus("practice-status", "请先填写你的回答。", true);
  button.disabled = true;
  setStatus("practice-status", "正在保存答案并生成反馈……");
  try {
    const session = await request(`/api/practice/${state.practiceId}/answers`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answer, response_language: responseLanguage(), assistance }),
    });
    state.practice = session;
    renderPractice(session);
    await refreshPracticeList();
    await refreshLearning();
    setStatus("practice-status", session.status === "finished" ? "反馈已保存，本轮结束。" : "反馈已保存，下一题等待你作答。 ");
  } catch (error) {
    await openPractice(state.practiceId);
    setStatus("practice-status", error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function retryPracticeGrade(button) {
  button.disabled = true;
  setStatus("practice-status", "正在根据已保存的回答重试评分……");
  try {
    const session = await request(`/api/practice/${state.practiceId}/grade`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ response_language: responseLanguage() }),
    });
    state.practice = session;
    renderPractice(session);
    await refreshPracticeList();
    setStatus("practice-status", "评分已保存。 ");
    await refreshLearning();
  } catch (error) {
    setStatus("practice-status", error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function finishPractice(button) {
  button.disabled = true;
  try {
    const session = await request(`/api/practice/${state.practiceId}/finish`, { method: "POST" });
    state.practice = session;
    renderPractice(session);
    await refreshPracticeList();
    setStatus("practice-status", "本轮练习已结束，记录已保存。 ");
  } catch (error) {
    setStatus("practice-status", error.message, true);
    button.disabled = false;
  }
}

async function savePracticeCorrection(questionId, correction, button) {
  if (!correction.trim()) return setStatus("practice-status", "请填写更正内容。", true);
  button.disabled = true;
  try {
    const session = await request(`/api/practice/${state.practiceId}/corrections`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question_id: questionId, correction: correction.trim() }),
    });
    state.practice = session;
    renderPractice(session);
    setStatus("practice-status", "你的更正已保存。 ");
  } catch (error) {
    setStatus("practice-status", error.message, true);
    button.disabled = false;
  }
}

$("view-rendered").addEventListener("click", () => setView("rendered"));
restoreResponseLanguage();
$("response-language").addEventListener("change", () => {
  try { localStorage.setItem("knowledge-review.response-language", responseLanguage()); } catch { }
});
$("view-source").addEventListener("click", () => setView("source"));
$("reader-scroll").addEventListener("mouseup", captureSelection);
$("reader-scroll").addEventListener("keyup", captureSelection);
$("question-form").addEventListener("submit", askQuestion);
$("clear-focus").addEventListener("click", clearFocus);
$("import-files").addEventListener("change", importFiles);
$("mode-ask").addEventListener("click", () => switchMode("ask"));
$("mode-practice").addEventListener("click", () => switchMode("practice"));
$("practice-start-form").addEventListener("submit", startPractice);
refreshCatalog().catch((error) => setStatus("request-status", error.message, true));
refreshPracticeList().catch((error) => setStatus("practice-status", error.message, true));
