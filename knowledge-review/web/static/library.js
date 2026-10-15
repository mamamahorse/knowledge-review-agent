function collectionPath(id) {
  const names = [];
  let node = state.collections.find((c) => c.id === id);
  while (node) {
    names.unshift(node.name);
    node = state.collections.find((c) => c.id === node.parent_id);
  }
  return names.join(" / ") || "总知识库";
}

function collectionOptions() {
  return { root: "总知识库", ...Object.fromEntries(state.collections.map((c) => [c.id, collectionPath(c.id)])) };
}

function fillCollections(select, selected = "root") {
  select.replaceChildren();
  Object.entries(collectionOptions()).forEach(([id, name]) => {
    const option = makeElement("option", "", name);
    option.value = id;
    select.append(option);
  });
  select.value = state.collections.some((c) => c.id === selected) ? selected : "root";
}

async function refreshCollections() {
  state.collections = await request("/api/collections");
  const tree = $("collection-tree");
  tree.replaceChildren();
  function append(parentId, depth) {
    state.collections.filter((c) => c.parent_id === parentId).forEach((c) => {
      const button = makeElement("button", "collection-node" + (c.id === state.collectionId ? " active" : ""), c.name);
      button.type = "button";
      button.style.paddingLeft = `${10 + depth * 14}px`;
      button.setAttribute("aria-pressed", String(c.id === state.collectionId));
      button.title = collectionPath(c.id);
      button.addEventListener("click", () => openCollection(c.id).catch(libraryError));
      tree.append(button);
      append(c.id, depth + 1);
    });
  }
  append(null, 0);
  ["collection-parent", "collection-destination", "document-destination", "learning-collection", "practice-collection"].forEach((id) => {
    fillCollections($(id), $(id).value || state.collectionId);
  });
  document.querySelectorAll("select[data-collection-selector]").forEach((select) => fillCollections(select, select.value || select.dataset.collectionSelector));
  $("collection-description").textContent = `${collectionPath(state.collectionId)} · 显示此集合及子集合的文件。新导入文件放入此处。`;
  $("collection-rename-controls").hidden = state.collectionId === "root";
  $("collection-rename").value = state.collections.find((c) => c.id === state.collectionId)?.name || "";
}

function libraryError(error) { setStatus("library-status", error.message, true); }

async function openCollection(id) {
  state.collectionId = id;
  ++state.documentEpoch;
  state.current = null;
  state.selectedIds.clear();
  $("rendered-note").replaceChildren();
  $("source-note").replaceChildren();
  $("document-title").textContent = "选择一份笔记";
  $("document-meta").textContent = "打开文档后可以提问或练习";
  $("reader-empty").hidden = false;
  clearFocus();
  try { localStorage.setItem("knowledge-review.collection", id); } catch { }
  await refreshCollections();
  if (state.collectionId !== id) return;
  ["collection-parent", "document-destination", "learning-collection", "practice-collection"].forEach((key) => { $(key).value = id; });
  await refreshCatalog();
  if (state.collectionId !== id) return;
  await refreshLearning();
  await refreshOutlines();
}

$("collection-create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    const created = await jsonRequest("/api/collections", { name: $("collection-name").value, parent_id: $("collection-parent").value });
    $("collection-name").value = "";
    await openCollection(created.id);
    setStatus("library-status", "集合已创建，可以用右上角的 ＋ 导入 Markdown 文件。");
  } catch (error) { libraryError(error); }
  finally { button.disabled = false; }
});

$("collection-rename-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await jsonRequest(`/api/collections/${state.collectionId}`, { name: $("collection-rename").value }, "PATCH");
    await refreshCollections();
    await refreshLearning();
    setStatus("library-status", "集合名称已保存。");
  } catch (error) { libraryError(error); }
});

$("move-document").addEventListener("click", async () => {
  if (!state.current) return libraryError(new Error("请先打开要移动的文件。"));
  try {
    await jsonRequest(`/api/documents/${state.current.id}/collection`, { collection_id: $("document-destination").value }, "PATCH");
    await openCollection(state.collectionId);
    setStatus("library-status", "文件归属已更新，已有题目和复习日期保留。");
  } catch (error) { libraryError(error); }
});

$("practice-clear-focus").addEventListener("click", clearFocus);
$("practice-scope").addEventListener("change", () => {
  const collection = $("practice-scope").value === "collection";
  const discussion = $("practice-scope").value === "discussion";
  $("practice-collection").hidden = !(collection || discussion);
  $("practice-selection").hidden = collection || discussion;
  $("practice-clear-focus").hidden = collection || discussion;
  $("discussion-source-controls").hidden = !discussion;
  if (discussion) refreshChatPracticeJobs().catch(libraryError);
});

$("move-collection").addEventListener("click", async () => {
  try {
    await jsonRequest(`/api/collections/${state.collectionId}/move`, {collection_id: $("collection-destination").value});
    await openCollection(state.collectionId);
    setStatus("library-status", "集合及子集合已移动；旧清单会按新的资料范围检查。");
  } catch (error) { libraryError(error); }
});
$("remove-collection").addEventListener("click", async () => {
  if (!confirm("删除这个集合？文件、子集合和学习计划移到上一级，已有作答和复习日期保留。旧清单保留为过期记录。")) return;
  try {
    const result = await request(`/api/collections/${state.collectionId}`, {method: "DELETE"});
    await openCollection(result.parent_id);
    setStatus("library-status", "集合已删除，内容移到上一级。");
  } catch (error) { libraryError(error); }
});
$("replace-document").addEventListener("change", async (event) => {
  const file = event.target.files[0], current = state.current;
  event.target.value = "";
  if (!file || !current) return libraryError(new Error("请先打开要替换的文件。"));
  const body = new FormData(); body.append("file", file);
  try {
    await request(`/api/documents/${current.id}?expected_source_id=${current.source_id}`, {method: "PUT", body});
    await openCollection(state.collectionId); await openDocument(current.id);
    setStatus("library-status", "正文已替换。旧练习保留原版本依据；过期清单需重新生成。");
  } catch (error) { libraryError(error); }
});
$("remove-document").addEventListener("click", async () => {
  const current = state.current;
  if (!current) return libraryError(new Error("请先打开要移出的文件。"));
  if (!confirm(`将 ${current.name} 移出资料库？今后不再查阅它；本机原始导入文件、旧题依据和真实作答保留。`)) return;
  try {
    await request(`/api/documents/${current.id}?expected_source_id=${current.source_id}`, {method: "DELETE"});
    await openCollection(state.collectionId);
    setStatus("library-status", "资料已移出，旧引用显示历史快照，不能跳到其他版本。");
  } catch (error) { libraryError(error); }
});
$("discussion-practice").addEventListener("click", () => {
  $("practice-scope").value = "discussion";
  $("practice-scope").dispatchEvent(new Event("change"));
  switchMode("practice");
  setStatus("practice-status", "将结合当前讨论与相关历史提出一道临时题。讨论题的参考要点属于模型解释，请先审核，再选择是否加入计划。");
});

async function submitChatPractice(payload) {
  const chat = state.conversationId;
  const result = await jsonRequest(`/api/conversations/${chat}/practice`, payload);
  if (chat !== state.conversationId) return;
  if (result.session) await showLearningSession(result.session);
  await refreshChatPracticeJobs();
  setStatus("practice-status", result.status === "completed" ? "临时题已保存，出题依据可查看；尚未加入学习计划。" : result.error || "出题未完成，可以重试。", result.status !== "completed");
}

async function refreshChatPracticeJobs() {
  const chat = state.conversationId;
  if (!chat) return;
  const rows = await request(`/api/conversations/${chat}/practice`);
  if (chat !== state.conversationId) return;
  const list = $("chat-practice-jobs"); list.replaceChildren();
  rows.filter((r) => r.status !== "completed").forEach((row) => {
    const card = makeElement("div", "practice-state", row.error || "出题曾被中断，可以重试。");
    if (row.status !== "revoked") {
      const retry = makeElement("button", "practice-action secondary", "重试讨论出题");
      retry.type = "button";
      retry.addEventListener("click", async () => {
        retry.disabled = true;
        try { await submitChatPractice({...JSON.parse(row.request_json), request_id: row.request_id}); }
        catch (error) { setStatus("practice-status", error.message, true); }
        finally { retry.disabled = false; }
      });
      card.append(retry);
    }
    list.append(card);
  });
}
$("collection-practice").addEventListener("click", () => {
  $("practice-scope").value = "collection";
  $("practice-collection").value = $("learning-collection").value;
  $("practice-scope").dispatchEvent(new Event("change"));
  switchMode("practice");
  setStatus("practice-status", "将从所选集合及其子集合的资料出题。填写目标后点击生成；题目需另行选择加入计划。");
});
$("learning-collection").addEventListener("change", () => refreshLearning().catch((e) => setStatus("learning-status", e.message, true)));

async function initializeLibrary() {
  await refreshCollections();
  let saved;
  try { saved = localStorage.getItem("knowledge-review.collection"); } catch { }
  await openCollection(state.collections.some((c) => c.id === saved) ? saved : "root");
}
document.addEventListener("DOMContentLoaded", () => initializeLibrary().catch(libraryError), { once: true });
