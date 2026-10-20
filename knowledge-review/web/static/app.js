const state = { documents: [], selectedIds: new Set(), current: null, focus: null, view: "rendered", practiceId: null, practice: null,
  conversationId: null, conversations: [], turns: [], chatEpoch: 0, conversationsEpoch: 0, chatBusy: false,
  collections: [], collectionId: "root", catalogEpoch: 0, documentEpoch: 0 };
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

function restoreRetrievalMode() {
  try {
    const saved = localStorage.getItem('knowledge-review.retrieval-mode');
    if (saved === 'dense' || saved === 'hybrid') $('retrieval-mode').value = saved;
  } catch { }
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
  const epoch = ++state.catalogEpoch;
  const docs = await request(`/api/documents?collection_id=${encodeURIComponent(state.collectionId)}`);
  if (epoch !== state.catalogEpoch) return;
  state.documents = docs;
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
  $("practice-selection").textContent = state.current ? `当前文档：${state.current.name} · 整篇（选段可选）` : "打开文档，或选择一个知识库集合开始练习。";
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
  const epoch = ++state.documentEpoch;
  try {
    const doc = await request(`/api/documents/${encodeURIComponent(id)}`);
    if (epoch !== state.documentEpoch) return null;
    state.current = doc;
    clearFocus();
    $("document-title").textContent = doc.name;
    $("document-meta").textContent = doc.format === 'pdf'
      ? `${doc.page_count} 页 · ${doc.line_count} 行文本视图 · ${doc.status === 'partial' ? '有待核对内容' : '解析完成'} · 版本 ${doc.source_id.slice(0, 12)}`
      : `${doc.line_count} 行 · 版本 ${doc.source_id.slice(0, 12)}`;
    $('view-source').textContent = doc.format === 'pdf' ? '提取文本' : '原文';
    $('reparse-document').hidden = doc.format !== 'pdf';
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

function citationLabel(citation) {
  const pages = [...new Set((citation.locations || []).map((loc) => loc.page).filter(Boolean))];
  return pages.length ? `${citation.name} · 第 ${pages.join('、')} 页 ↗` : `${citation.name} · L${citation.line_start}–L${citation.line_end} ↗`;
}

async function showCitation(citation) {
  try {
    const current = await request(`/api/documents/${citation.document_id}`);
    if (current.source_id !== citation.source_id) throw new Error("资料已经替换");
  } catch {
    const dialog = makeElement("dialog", "history-dialog");
    dialog.append(makeElement("h3", "", `${citation.name} · 历史版本 L${citation.line_start}–L${citation.line_end}`),
      makeElement("p", "", "当前资料已替换或移出。这里展示当时保存的引用快照，不能用新文件同一行冒充旧依据。"),
      makeElement("pre", "", citation.excerpt));
    const close = makeElement("button", "", "关闭"); close.addEventListener("click", () => dialog.close());
    dialog.append(close); dialog.addEventListener("close", () => dialog.remove());
    document.body.append(dialog); dialog.showModal(); return;
  }
  const doc = await openDocument(citation.document_id, citation.source_id);
  if (!doc || doc.source_id !== citation.source_id) return;
  if (doc.format === 'pdf' && doc.assets) {
    const page = (citation.locations || []).find((loc) => loc.page)?.page;
    const block = (doc.blocks || []).find((b) => b.line_start <= citation.line_end && b.line_end >= citation.line_start && b.asset_ids.length);
    const assetId = citation.asset_id || block?.asset_ids[0] || (page ? `page-${page}` : null);
    const asset = doc.assets.find((a) => a.asset_id === assetId);
    if (asset) {
      const dialog = document.createElement('dialog'); dialog.className = 'citation-snapshot';
      const img = document.createElement('img'); img.src = asset.url; img.alt = citationLabel(citation); img.style.maxWidth = '100%';
      const close = makeElement('button', '', '关闭'); close.addEventListener('click', () => dialog.close());
      dialog.append(makeElement('h3', '', citationLabel(citation)), makeElement('pre', '', citation.excerpt), img, close);
      dialog.addEventListener('close', () => dialog.remove()); document.body.append(dialog); dialog.showModal(); return;
    }
  }
  setView("source");
  document.querySelectorAll(".citation-highlight").forEach((node) => node.classList.remove("citation-highlight"));
  const lines = [...$("source-note").querySelectorAll(".source-line")];
  for (let number = citation.line_start; number <= citation.line_end; number++) {
    lines[number - 1]?.classList.add("citation-highlight");
  }
  lines[citation.line_start - 1]?.scrollIntoView({ behavior: "smooth", block: "center" });
}

function renderAnswer(result, container = $("answer-result")) {
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
      const button = makeElement("button", "citation", citationLabel(citation));
      button.type = "button";
      button.append(makeElement("small", "", citation.excerpt));
      button.addEventListener("click", () => showCitation(citation));
      section.append(button);
    }
    for (const reference of (result.history_references || []).filter((r) => r.block_index === result.blocks.indexOf(block))) {
      const button = makeElement("button", "citation history-reference", `关联历史 · ${reference.title} · 第 ${reference.seq} 轮 ↗`);
      button.type = "button";
      button.append(makeElement("small", "", `${reference.role === "user" ? "用户原话" : "当时助手的解释"} · 不作为文档依据`));
      button.addEventListener("click", () => showHistoryReference(reference));
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
  if (result.retrieval) {
    const mode = result.retrieval.mode === 'hybrid' ? '融合查找' : '原语义查找';
    const searches = result.retrieval.searches || [];
    details.append(makeElement('p', '', `${mode} · 实际搜索 ${searches.length} 次`));
    for (const trace of searches) {
      details.append(makeElement('p', '', trace.status === 'ok'
        ? `许可片段 ${trace.permitted_chunks} · 返回 ${trace.returned.length} · ${trace.total_ms.toFixed(1)} ms`
        : '搜索失败，未自动改用另一种方式。'));
      const diagnostic = makeElement('details', '');
      diagnostic.append(makeElement('summary', '', '查看排名依据与实际可见范围'));
      diagnostic.append(makeElement('pre', '', JSON.stringify({ search:trace, final_visible_ranges:result.retrieval.final_visible_ranges }, null, 2)));
      details.append(diagnostic);
    }
  }
  container.append(details);
}

async function askQuestion(event) {
  event.preventDefault();
  const focus = state.focus;
  const question = $("question").value;
  const start = Number($("line-start").value);
  const end = Number($("line-end").value);
  if (!state.current) return setStatus("request-status", "请先打开一份文档。", true);
  if (focus && (!Number.isInteger(start) || !Number.isInteger(end) || start < 1 || end < start || end > state.current.line_count)) {
    return setStatus("request-status", "选段必须在当前原文范围内。", true);
  }
  if (!question.trim()) return setStatus("request-status", "请输入问题。", true);
  if (!state.conversationId || state.chatBusy) return;
  const documentIds = [...state.selectedIds];
  if (!documentIds.includes(state.current.id)) documentIds.push(state.current.id);
  if (focus) showFocus(start, end);
  const pending = readPending(state.conversationId);
  const payload = pending || {
    request_id: crypto.randomUUID(), document_ids: documentIds, focus_document_id: state.current.id,
    focus_source_id: state.current.source_id, ...(focus ? { line_start: start, line_end: end } : {}),
    question, strict: $("strict-mode").checked, response_language: responseLanguage(),
    retrieval_mode: $('retrieval-mode').value,
  };
  await sendChatSubmission(state.conversationId, payload);
}

function pendingKey(id) { return `knowledge-review.pending.${id}`; }
function readPending(id) {
  try { return JSON.parse(localStorage.getItem(pendingKey(id)) || "null"); } catch { return null; }
}

async function refreshConversations() {
  const epoch = ++state.conversationsEpoch;
  const chats = await request(`/api/conversations?archived=${$("show-archived").checked}`);
  if (epoch !== state.conversationsEpoch) return;
  state.conversations = chats;
  const list = $("conversation-list");
  list.replaceChildren();
  for (const item of state.conversations) {
    if (!item.title.toLowerCase().includes($("conversation-filter").value.toLowerCase())) continue;
    const option = makeElement("option", "", item.title);
    option.value = item.id;
    list.append(option);
  }
  list.value = state.conversationId || "";
}

async function showHistoryReference(reference) {
  try {
    const original = await request(`/api/history/${reference.message_id}?start_offset=${reference.start_offset}&end_offset=${reference.end_offset}&message_revision=${reference.message_revision}`);
    const dialog = document.createElement("dialog");
    dialog.className = "history-dialog";
    dialog.dataset.conversationId = original.conversation_id;
    dialog.append(makeElement("h3", "", `${original.title} · 第 ${original.seq} 轮`));
    dialog.append(makeElement("p", "", `${original.role === "user" ? "用户原话" : "当时助手的解释"} · 讨论记录，不是文档依据`));
    dialog.append(makeElement("pre", "chat-original", original.content));
    const open = makeElement("button", "", "打开原聊天"), close = makeElement("button", "", "关闭");
    close.addEventListener("click", () => dialog.close());
    open.addEventListener("click", async () => {
      dialog.close();
      $("show-archived").checked = Boolean((await request(`/api/conversations/${original.conversation_id}/messages?limit=1`)).conversation.archived);
      $("conversation-filter").value = "";
      await refreshConversations();
      await openConversation(original.conversation_id);
      document.getElementById(`message-${original.message_id}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    dialog.append(open, close);
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    document.body.append(dialog);
    dialog.showModal();
  } catch (error) { setStatus("request-status", error.message, true); }
}

function renderChat(data) {
  state.turns = data.turns;
  $("chat-title").textContent = data.conversation.title;
  $("archive-conversation").textContent = data.conversation.archived ? "恢复到列表" : "归档";
  const container = $("answer-result");
  container.replaceChildren();
  for (const turn of data.turns) {
    const card = makeElement("article", "chat-turn");
    if (!turn.memory_valid) card.append(makeElement("p", "coverage-note", "这轮回复关联的历史来源已删除；记录保留，但不再参与记忆。"));
    for (const message of turn.messages) {
      const node = makeElement("section", `chat-message ${message.role}`);
      node.id = `message-${message.id}`;
      node.append(makeElement("strong", "", message.role === "user" ? "你" : "助手"));
      if (message.role === "assistant" && message.result) {
        const answer = makeElement("div", "answer-result");
        renderAnswer(message.result, answer);
        node.append(answer);
      } else node.append(makeElement("p", "chat-original", message.content));
      card.append(node);
    }
    if (turn.status === "failed") {
      card.append(makeElement("p", "error", turn.error || "生成失败，输入已保存。"));
      const retry = makeElement("button", "chat-retry", "重试这条输入");
      retry.type = "button";
      const identity = state.conversationId;
      retry.addEventListener("click", () => retryChatTurn(identity, turn.id));
      const skip = makeElement("button", "chat-retry", "跳过，保留原话");
      skip.type = "button";
      skip.addEventListener("click", async () => {
        try {
          await request(`/api/conversations/${identity}/turns/${turn.id}/skip`, { method: "POST" });
          if (state.conversationId === identity) await loadChat(identity);
        } catch (error) { setStatus("request-status", error.message, true); }
      });
      card.append(retry, skip);
    } else if (turn.status === "running") card.append(makeElement("p", "", "输入已保存，正在生成……"));
    else if (turn.status === "cancelled") card.append(makeElement("p", "coverage-note", "此输入已跳过，原话保留，不计入完成对话。"));
    container.append(card);
  }
  $("chat-summary").hidden = !data.summary;
  const summary = $("summary-content");
  summary.replaceChildren();
  if (data.summary) {
    summary.append(makeElement("p", "", `版本 ${data.summary.version} · 覆盖至第 ${data.summary.covers_through_seq} 轮；近期原话另行保留。`));
    const labels = { user_goal: "用户目标", user_decision: "用户决定", user_question: "用户疑问", assistant_explanation: "助手解释", example: "重要例子" };
    for (const item of data.summary.payload.items) {
      const row = makeElement("p", "", `${labels[item.kind]}：${item.text}`);
      for (const id of item.evidence_message_ids) {
        const sourceTurn = data.turns.find((turn) => turn.messages.some((message) => message.id === id));
        const link = makeElement("button", "summary-evidence", sourceTurn ? `第 ${sourceTurn.seq} 轮原话` : "查看原话");
        link.type = "button";
        link.addEventListener("click", () => document.getElementById(`message-${id}`)?.scrollIntoView({ behavior: "smooth", block: "center" }));
        row.append(link);
      }
      summary.append(row);
    }
  }
  $("ask-button").disabled = state.chatBusy || data.turns.some((t) => t.status === "failed" || t.status === "running");
}

async function loadChat(id) {
  const epoch = ++state.chatEpoch;
  let after = 0, all = [], data;
  do {
    data = await request(`/api/conversations/${id}/messages?after_seq=${after}`);
    all.push(...data.turns);
    after = data.next_cursor;
  } while (after !== null);
  if (epoch !== state.chatEpoch || id !== state.conversationId) return;
  const pending = readPending(id);
  if (pending && all.some((turn) => turn.request_id === pending.request_id)) {
    try { localStorage.removeItem(pendingKey(id)); } catch { }
  }
  renderChat({ ...data, turns: all });
  if (typeof refreshChatPracticeJobs === 'function') refreshChatPracticeJobs().catch(() => {});
  const naming = all.some((t) => t.status === "completed") && ["pending", "generating"].includes(data.conversation.title_state);
  if (naming || state.conversations.find((c) => c.id === id)?.title !== data.conversation.title) await refreshConversations();
  if (all.some((t) => t.status === "running") || naming) {
    setTimeout(() => {
      if (state.conversationId === id && state.chatEpoch === epoch) loadChat(id).catch((error) => setStatus("request-status", error.message, true));
    }, 2500);
  }
}

async function openConversation(id) {
  state.conversationId = id;
  try { localStorage.setItem("knowledge-review.conversation", id); } catch { }
  $("conversation-list").value = id;
  $("rename-form").hidden = true;
  $("question").value = readPending(id)?.question || "";
  clearFocus();
  setStatus("request-status", readPending(id) ? "上次提交尚未确认，点击发送会使用相同标识恢复。" : "聊天记录保存在本机，可连续追问。");
  await loadChat(id);
}

async function newConversation() {
  const created = await request("/api/conversations", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: "新聊天" }),
  });
  $("show-archived").checked = false;
  $("conversation-filter").value = "";
  await refreshConversations();
  await openConversation(created.id);
}

async function initializeChat() {
  await refreshConversations();
  let saved;
  try { saved = localStorage.getItem("knowledge-review.conversation"); } catch { }
  if (saved && !state.conversations.some((c) => c.id === saved)) {
    try {
      const metadata = await request(`/api/conversations/${saved}`);
      if (metadata.archived) {
        $("show-archived").checked = true;
        await refreshConversations();
      }
    } catch { /* A deleted selection falls back to an available chat. */ }
  }
  const current = state.conversations.find((c) => c.id === saved) || state.conversations[0];
  if (current) await openConversation(current.id);
  else await newConversation();
}

async function sendChatSubmission(id, payload, turnId = null) {
  if (state.chatBusy) return;
  state.chatBusy = true;
  $("ask-button").disabled = true;
  if (!turnId) {
    try { localStorage.setItem(pendingKey(id), JSON.stringify(payload)); } catch { }
  }
  setStatus("request-status", "正在保存输入、整理聊天背景并生成回答，必要时会查阅资料……");
  setTimeout(() => {
    if (state.chatBusy && state.conversationId === id) loadChat(id).catch(() => {});
  }, 700);
  try {
    const turn = await request(`/api/conversations/${id}/turns${turnId ? `/${turnId}/retry` : ""}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    try { localStorage.removeItem(pendingKey(id)); } catch { }
    if (state.conversationId === id) {
      $("question").value = "";
      setStatus("request-status", turn.status === "completed" ? "回复已保存，可继续追问或切换聊天。" : (turn.error || "输入已保存，可重试。"), turn.status !== "completed");
      await loadChat(id);
    }
    await refreshConversations();
  } catch (error) {
    if (state.conversationId === id) setStatus("request-status", `${error.message} 输入保留；重发使用相同标识。`, true);
  } finally {
    state.chatBusy = false;
    if (state.conversationId) {
      try { await loadChat(state.conversationId); } catch { $("ask-button").disabled = false; }
    }
  }
}

async function retryChatTurn(id, turnId) {
  await sendChatSubmission(id, { response_language: responseLanguage(), retrieval_mode:$('retrieval-mode').value }, turnId);
}

async function importFiles(event) {
  const files = [...event.target.files];
  if (!files.length) return;
  const data = new FormData();
  files.forEach((file) => data.append("files", file));
  setStatus("import-status", `正在导入 ${files.length} 份笔记……`);
  try {
    const targetCollection = state.collectionId;
    const imported = await request(`/api/documents?collection_id=${encodeURIComponent(targetCollection)}`, { method: "POST", body: data });
    await refreshCatalog();
    if (state.collectionId === targetCollection) await openDocument(imported[0].id);
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
  const button = makeElement("button", "citation", citationLabel(citation));
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
  if (session.basis) {
    detail.append(makeElement("p", "practice-state", session.basis.revoked ?
      "聊天来源已撤销：本题仅保留历史记录，不能继续评分、复习或加入计划。真实作答与复习日期保留。" :
      session.basis.kind === "discussion_model" ? "讨论题 · 参考要点由模型拟定，历史解释不代表文档依据。请审核后自行决定是否加入计划。" : "文档题 · 讨论提供出题角度，知识依据限于所示原文。"));
    (session.basis.references || []).forEach((ref) => {
      const button = makeElement("button", "citation history-reference", `出题缘由 · ${ref.title} · 第 ${ref.seq} 轮`);
      button.append(makeElement("small", "", ref.practice_reason || "讨论角度"));
      button.addEventListener("click", () => showHistoryReference(ref)); detail.append(button);
    });
  }
  session.questions.forEach((item, index) => {
    const current = index === session.questions.length - 1;
    const card = makeElement("article", `practice-question${current ? " current" : ""}`);
    card.append(makeElement("h4", "", `第 ${item.position} 题 · ${item.objective}`));
    card.append(makeElement("p", "prompt", item.question));
    if (item.proposed_target && !item.unit_id) {
      const target = item.proposed_target;
      card.append(makeElement("p", "practice-state", `拟加入目标：${target.knowledge_name} / ${target.title} · ${capabilities[target.capability] || target.capability}`),
        makeElement("p", "practice-state", `考察范围：${target.scope}。可先查看参考要点，再决定是否加入。`));
    }
    item.citations.forEach((citation) => card.append(practiceCitation(citation)));
    if (session.basis?.revoked) {
      if (item.answer_text !== null) card.append(makeElement("p", "practice-response", item.answer_text));
      if (item.feedback) card.append(makeElement("p", "practice-feedback", item.feedback));
      detail.append(card); return;
    }

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
  if ($("practice-scope").value === "discussion") {
    const button = $("practice-start-button"); button.disabled = true;
    try {
      const strict = $("discussion-strict").checked;
      const payload = {request_id: crypto.randomUUID().replaceAll("-", ""), objective: $("practice-objective").value.trim(),
        response_language: responseLanguage(), collection_id: $("practice-collection").value, strict};
      if (strict) {
        if (!state.current) throw new Error("严格资料模式需要先打开文档。");
        Object.assign(payload, {document_ids: [...new Set([...state.selectedIds, state.current.id])],
          focus_document_id: state.current.id, focus_source_id: state.current.source_id});
      }
      setStatus("practice-status", "正在整理讨论背景并提出临时题……");
      await submitChatPractice(payload);
    } catch (error) { setStatus("practice-status", error.message, true); await refreshChatPracticeJobs().catch(() => {}); }
    finally { button.disabled = false; }
    return;
  }
  const collectionMode = $("practice-scope").value === "collection";
  if (!collectionMode && !state.current) {
    return setStatus("practice-status", "请打开一份文档，或选择知识库集合。", true);
  }
  const start = !collectionMode && state.focus ? Number($("line-start").value) : null;
  const end = !collectionMode && state.focus ? Number($("line-end").value) : null;
  if (start !== null && (!Number.isInteger(start) || !Number.isInteger(end) || start < 1 || end < start || end > state.current.line_count)) {
    return setStatus("practice-status", "选段必须在原文行号范围内。", true);
  }
  const button = $("practice-start-button");
  button.disabled = true;
  setStatus("practice-status", "正在根据原文生成题目与评分要点……");
  try {
    const documentIds = [...state.selectedIds];
    if (!collectionMode && !documentIds.includes(state.current.id)) documentIds.push(state.current.id);
    const payload = { objective: $("practice-objective").value.trim(), response_language: responseLanguage() };
    if (!collectionMode) Object.assign(payload, {
      document_ids: documentIds, focus_document_id: state.current.id,
      focus_source_id: state.current.source_id, line_start: start, line_end: end,
    });
    const session = await request(collectionMode ? `/api/collections/${$("practice-collection").value}/practice` : "/api/practice", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
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
restoreRetrievalMode();
$("response-language").addEventListener("change", () => {
  try { localStorage.setItem("knowledge-review.response-language", responseLanguage()); } catch { }
});
$('retrieval-mode').addEventListener('change', () => {
  try { localStorage.setItem('knowledge-review.retrieval-mode', $('retrieval-mode').value); } catch { }
});
$("view-source").addEventListener("click", () => setView("source"));
$("reader-scroll").addEventListener("mouseup", captureSelection);
$("reader-scroll").addEventListener("keyup", captureSelection);
$("question-form").addEventListener("submit", askQuestion);
$("conversation-list").addEventListener("change", (event) => openConversation(event.target.value).catch((error) => setStatus("request-status", error.message, true)));
$("conversation-filter").addEventListener("input", () => refreshConversations().catch((error) => setStatus("request-status", error.message, true)));
$("show-archived").addEventListener("change", async () => {
  try {
    await refreshConversations();
    if (state.conversations.length) await openConversation(state.conversations[0].id);
    else { $("conversation-list").value = ""; setStatus("request-status", "此列表暂无聊天；当前聊天仍可继续使用。"); }
  } catch (error) { setStatus("request-status", error.message, true); }
});
$("archive-conversation").addEventListener("click", async () => {
  if (!state.conversationId) return;
  try {
    const chat = await request(`/api/conversations/${state.conversationId}/messages?limit=1`);
    await request(`/api/conversations/${state.conversationId}/${chat.conversation.archived ? "restore" : "archive"}`, { method: "POST" });
    await refreshConversations();
    await loadChat(state.conversationId);
  } catch (error) { setStatus("request-status", error.message, true); }
});
async function leaveDeletedChat(id) {
  try { localStorage.removeItem(pendingKey(id)); } catch { }
  document.querySelectorAll(".history-dialog").forEach((dialog) => { if (dialog.dataset.conversationId === id) dialog.close(); });
  if (state.conversationId !== id) {
    await refreshConversations();
    if (state.conversationId) await loadChat(state.conversationId);
    return;
  }
  state.chatEpoch++;
  state.conversationId = null;
  state.turns = [];
  $("question").value = "";
  $("answer-result").replaceChildren();
  $("show-archived").checked = false;
  $("conversation-filter").value = "";
  await initializeChat();
}
$("delete-conversation").addEventListener("click", async () => {
  const id = state.conversationId;
  if (!id || !confirm("删除这个聊天及其原话、摘要和检索缓存？关联的派生回复将停止参与记忆。此操作不可恢复。")) return;
  try {
    await request(`/api/conversations/${id}`, { method: "DELETE" });
    try { localStorage.setItem("knowledge-review.deleted", id); } catch { }
    await leaveDeletedChat(id);
    setStatus("request-status", "聊天已删除，原话及派生记忆已撤销。");
  } catch (error) { setStatus("request-status", error.message, true); }
});
window.addEventListener("storage", (event) => {
  if (event.key === "knowledge-review.deleted" && event.newValue) leaveDeletedChat(event.newValue).catch(() => {});
});
$("new-conversation").addEventListener("click", () => newConversation().catch((error) => setStatus("request-status", error.message, true)));
$("rename-conversation").addEventListener("click", () => {
  $("conversation-title").value = state.conversations.find((c) => c.id === state.conversationId)?.title || "";
  $("rename-form").hidden = false;
  $("conversation-title").focus();
});
$("rename-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await request(`/api/conversations/${state.conversationId}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: $("conversation-title").value }) });
    $("rename-form").hidden = true;
    await refreshConversations();
    await loadChat(state.conversationId);
  } catch (error) { setStatus("request-status", error.message, true); }
});
$("clear-focus").addEventListener("click", clearFocus);
$("import-files").addEventListener("change", importFiles);
$("mode-ask").addEventListener("click", () => switchMode("ask"));
$("mode-practice").addEventListener("click", () => switchMode("practice"));
$("practice-start-form").addEventListener("submit", startPractice);
refreshPracticeList().catch((error) => setStatus("practice-status", error.message, true));
initializeChat().catch((error) => setStatus("request-status", error.message, true));
