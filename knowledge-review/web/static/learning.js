const capabilities = { recall: "回忆", explain: "解释", apply: "应用", distinguish: "辨析" };
const assessments = { meets_standard: "达到本题标准", developing: "理解尚有缺口", needs_review: "需要复习", uncertain: "暂无法判断" };
const reviewRatings = { Again: "Again · 未能完成回忆", Hard: "Hard · 正确但吃力", Good: "Good · 正常完成", Easy: "Easy · 轻松完成" };
let learningGroups = [];

function choice(options, value, label) {
  const select = makeElement("select", "learning-select");
  select.setAttribute("aria-label", label);
  Object.entries(options).forEach(([key, text]) => {
    const option = makeElement("option", "", text);
    option.value = key;
    select.append(option);
  });
  select.value = value;
  return select;
}

function jsonRequest(url, body, method = "POST") {
  return request(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

async function learningAction(button, action, statusId = "practice-status") {
  button.disabled = true;
  setStatus(statusId, "正在处理……");
  try {
    await action();
    await refreshLearning();
    setStatus(statusId, "已保存。");
  } catch (error) {
    setStatus(statusId, error.message, true);
  } finally { button.disabled = false; }
}

function localDate(date) {
  return new Date(date).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

async function showLearningSession(session) {
  switchMode("practice");
  state.practiceId = session.id;
  state.practice = session;
  renderPractice(session);
  await refreshPracticeList();
}

async function refreshLearning() {
  learningGroups = await request("/api/learning");
  const list = $("learning-list");
  list.replaceChildren();
  if (!learningGroups.length) list.append(makeElement("p", "practice-empty", "还没有学习计划。可以在任意一道练习中选择“加入学习计划”。"));
  learningGroups.forEach((group) => {
    const section = makeElement("section", "knowledge-group");
    section.append(makeElement("h3", "", group.name));
    const knowledge = makeElement("details", "learning-details");
    knowledge.append(makeElement("summary", "", "知识说明"), makeElement("p", "practice-feedback", group.statement));
    section.append(knowledge);
    group.items.forEach((item) => {
      const card = makeElement("article", "learning-item");
      card.append(makeElement("span", "practice-rating", `${capabilities[item.capability]} · ${item.status === "paused" ? "已暂停" : item.due ? "可以复习了" : "已安排"}`));
      card.append(makeElement("h4", "", item.title));
      card.append(makeElement("p", "practice-state", item.evidence.length ? `最近一次：${assessments[item.evidence[0].assessment] || "历史评分"}${item.evidence[0].assistance !== "none_recorded" ? " · 使用了提示/资料" : ""}` : "尚无作答证据"));
      card.append(makeElement("p", "practice-state", `复习建议：${localDate(item.next_review_at)}`));
      const actions = makeElement("div", "learning-actions");
      const review = makeElement("button", "practice-action", item.pending_session_id ? "继续未完成的作答" : "同目标变式");
      review.type = "button";
      review.disabled = item.status === "paused";
      review.addEventListener("click", () => learningAction(review, async () => {
        await showLearningSession(await jsonRequest(`/api/learning/${item.id}/review`, { mode: "variant", response_language: responseLanguage() }));
      }, "learning-status"));
      const original = makeElement("button", "practice-action secondary", "重做原题");
      original.type = "button";
      original.disabled = item.status === "paused" || Boolean(item.pending_session_id);
      if (item.pending_session_id) original.title = "先完成或结束当前题，再选择原题。";
      original.addEventListener("click", () => learningAction(original, async () => {
        await showLearningSession(await jsonRequest(`/api/learning/${item.id}/review`, { mode: "original", response_language: responseLanguage() }));
      }, "learning-status"));
      const pause = makeElement("button", "practice-action secondary", item.status === "paused" ? "恢复复习" : "暂停");
      pause.type = "button";
      pause.addEventListener("click", () => learningAction(pause, () => jsonRequest(`/api/learning/${item.id}`, { status: item.status === "paused" ? "active" : "paused" }, "PATCH"), "learning-status"));
      actions.append(review, original, pause);
      card.append(actions);
      const evidence = makeElement("details", "learning-details");
      evidence.append(makeElement("summary", "", `近期学习证据 · ${item.recent_evidence_count} 次`));
      evidence.append(makeElement("p", "practice-feedback", item.description));
      evidence.append(makeElement("p", "practice-state", `考察范围：${item.scope}`));
      if (!item.evidence.length) evidence.append(makeElement("p", "practice-empty", "尚无作答证据。未测试不等于没有掌握。"));
      item.evidence.forEach((entry) => {
        evidence.append(makeElement("p", "practice-state", `${localDate(entry.submitted_at)} · ${assessments[entry.assessment] || "历史评分"} · ${entry.origin === "user" ? "用户更正" : "模型评价"}${entry.assistance !== "none_recorded" ? " · 使用了提示/资料" : ""}`));
        evidence.append(makeElement("p", "practice-feedback", entry.feedback));
        entry.dimension_observations.forEach((observation) => evidence.append(makeElement("p", "practice-feedback", `${capabilities[observation.capability]}观察：${observation.observation}`)));
      });
      card.append(evidence);
      section.append(card);
    });
    const add = makeElement("details", "learning-details");
    add.append(makeElement("summary", "", "新增一个考察目标"));
    const dimension = choice(capabilities, "apply", "新目标的能力维度");
    const generate = makeElement("button", "practice-action secondary", "生成并加入计划");
    generate.type = "button";
    generate.addEventListener("click", () => learningAction(generate, async () => {
      await showLearningSession(await jsonRequest(`/api/learning/${group.items[0].id}/dimensions`, { capability: dimension.value, response_language: responseLanguage() }));
    }, "learning-status"));
    add.append(dimension, generate);
    section.append(add);
    list.append(section);
  });
}

function appendEnrollmentControl(card, session, item) {
  if (item.target_title) {
    card.append(makeElement("p", "practice-state", `学习计划：${item.unit_name} / ${capabilities[item.capability]} / ${item.target_title}`));
    return;
  }
  const group = choice({ "": "作为新的知识点", ...Object.fromEntries(learningGroups.map((g) => [g.id, g.name])) }, "", "加入计划的知识点归组");
  const button = makeElement("button", "practice-action secondary", "加入学习计划");
  button.type = "button";
  button.addEventListener("click", () => learningAction(button, async () => {
    const result = await jsonRequest(`/api/practice/${session.id}/enroll`, { question_id: item.id, unit_id: group.value || null, response_language: responseLanguage() });
    state.practice = result.session;
    renderPractice(result.session);
  }));
  const row = makeElement("div", "learning-enroll");
  row.append(group, button);
  card.append(row);
}

function appendReferenceControl(card, session, item) {
  const button = makeElement("button", "practice-action secondary", "查看参考要点");
  button.type = "button";
  if (item.assistance === "reference_seen") card.append(makeElement("p", "practice-state", "已查看参考要点，本次作答作为学习证据，不更新复习日期。"));
  button.addEventListener("click", () => learningAction(button, async () => {
    const result = await request(`/api/practice/${session.id}/reference/${item.id}`, { method: "POST" });
    const list = makeElement("ol", "rubric-list");
    result.rubric_points.forEach((point) => list.append(makeElement("li", "", point)));
    card.append(list, makeElement("p", "practice-state", "已记录查看参考，本次不会改变复习日期。"));
  }));
  card.append(button);
}

function appendGradeControls(card, session, item) {
  if (item.rating === "uncertain") card.append(makeElement("p", "practice-state", "本次评价存在不确定性，不按答错处理。"));
  if (item.rating_reason) card.append(makeElement("p", "practice-feedback", `复习档位依据：${item.rating_reason}`));
  item.dimension_observations.forEach((observation) => {
    card.append(makeElement("p", "practice-feedback", `${capabilities[observation.capability]}观察：${observation.observation}`));
  });
  const event = item.review_event;
  if (event) {
    if (event.eligible) {
      const rating = choice({ "": "暂未建议档位", ...reviewRatings }, event.effective_rating || "", "复习档位");
      const save = makeElement("button", "practice-action secondary", "修改复习档位");
      save.type = "button";
      save.addEventListener("click", () => learningAction(save, async () => {
        if (!rating.value) throw new Error("请选择一个复习档位。");
        await jsonRequest(`/api/review-events/${event.id}`, { rating: rating.value, expected_revision: item.revision }, "PATCH");
        await openPractice(session.id);
      }));
      card.append(rating, save);
      card.append(makeElement("p", "practice-state", `当前采用：${reviewRatings[event.effective_rating] || "暂无档位，日期未推进"} · ${event.rating_origin === "user" ? "你的选择" : "模型建议"}`));
    } else card.append(makeElement("p", "practice-state", event.exclusion_reason));
  } else if (item.suggested_rating) card.append(makeElement("p", "practice-state", `建议档位：${reviewRatings[item.suggested_rating]} · 本次仅保存学习证据`));

  const editor = makeElement("details", "learning-details");
  editor.append(makeElement("summary", "", "评分有误？当场修改"));
  const assessment = choice(assessments, assessments[item.rating] ? item.rating : "uncertain", "整体判断");
  const fit = choice({ within_scope: "在目标范围内", outside_scope: "题目超出范围", uncertain: "范围尚不确定" }, item.target_fit, "题目与目标的关系");
  const suggested = choice({ "": "暂无建议", ...reviewRatings }, item.suggested_rating || "", "更正后的建议档位");
  editor.append(makeElement("label", "practice-label", "整体判断、目标范围、建议档位"), assessment, fit, suggested);
  const checks = item.rubric_points.map((point, index) => {
    const label = makeElement("label", "practice-label");
    const checkbox = makeElement("input", "");
    checkbox.type = "checkbox";
    checkbox.checked = item.covered_indices.includes(index + 1);
    label.append(checkbox, document.createTextNode(` 已体现：${point}`));
    editor.append(label);
    return checkbox;
  });
  const feedback = makeElement("textarea", "practice-correction");
  feedback.value = item.feedback;
  feedback.setAttribute("aria-label", "更正后的评语");
  const reason = makeElement("textarea", "practice-correction");
  reason.placeholder = "说明为什么修改（保留原始作答和原评分）";
  reason.setAttribute("aria-label", "修改评分的理由");
  const save = makeElement("button", "practice-action secondary", "保存新评分");
  save.type = "button";
  save.addEventListener("click", () => learningAction(save, async () => {
    const covered = checks.flatMap((check, index) => check.checked ? [index + 1] : []);
    const missing = checks.flatMap((check, index) => check.checked ? [] : [index + 1]);
    const result = await jsonRequest(`/api/practice/${session.id}/evaluation`, {
      question_id: item.id, expected_revision: item.revision, overall_assessment: assessment.value,
      covered_indices: covered, missing_indices: missing, feedback: feedback.value,
      suggested_rating: suggested.value || null, target_fit: fit.value, reason: reason.value,
    });
    state.practice = result;
    renderPractice(result);
  }));
  editor.append(makeElement("label", "practice-label", "评语与修改理由"), feedback, reason, save);
  card.append(editor);
  const history = makeElement("details", "learning-details");
  history.append(makeElement("summary", "", `评分记录 · ${item.evaluation_history.length} 个版本`));
  item.evaluation_history.forEach((entry) => {
    history.append(makeElement("p", "practice-state", `第 ${entry.revision} 版 · ${entry.origin === "user" ? "用户更正" : entry.origin === "legacy" ? "历史评分" : "模型评价"} · ${localDate(entry.created_at)}`));
    history.append(makeElement("p", "practice-feedback", entry.grade.feedback));
    if (entry.reason) history.append(makeElement("p", "practice-feedback", `修改理由：${entry.reason}`));
  });
  card.append(history);
  if (item.candidate_available && session.status === "finished" && item.position === session.questions.length) {
    const next = makeElement("button", "practice-action", "继续一题（可选）");
    next.type = "button";
    next.addEventListener("click", () => learningAction(next, async () => {
      await showLearningSession(await request(`/api/practice/${session.id}/continue`, { method: "POST" }));
    }));
    card.append(next);
  }
}

$("mode-learning").addEventListener("click", () => { switchMode("learning"); refreshLearning().catch((error) => setStatus("learning-status", error.message, true)); });
refreshLearning().catch((error) => setStatus("learning-status", error.message, true));
