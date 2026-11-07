/* Explicit enrichment only: opening a document or searching never calls vision. */
(() => {
  let scope = null, busy = false;
  const output = () => $('visual-descriptions-result');
  const error = (e) => output().replaceChildren(makeElement('p', 'error', e.message));
  function render(value) {
    const names = {queued:'等待执行',running:'正在补全',completed:'选定范围处理完毕',partial:'部分完成，可再次提交复用成功结果',failed:'执行失败',interrupted:'已中断'};
    output().replaceChildren(makeElement('p','',`${names[value.status] || value.status} · ${value.completed || 0}/${value.total || 0} 页／图片 · 复用 ${value.reused || 0} · 实际 HTTP ${value.requests?.length || 0} 次`));
    if (value.error) output().append(makeElement('p','error',value.error));
    for (const unit of value.units || []) {
      const row = makeElement('details','');
      row.append(makeElement('summary','',`${unit.page ? '第 '+unit.page+' 页' : unit.asset_id} · ${unit.status} · ${unit.objects.length} 个对象`));
      for (const obj of unit.objects) row.append(makeElement('p','',`${obj.label}：${obj.description || ''}`));
      if (unit.error) row.append(makeElement('p','error',`本页失败：${unit.error}，成功描述已保留。`));
      for (const note of [...new Set(unit.unresolved || [])]) row.append(makeElement('p','practice-hint',note));
      output().append(row);
    }
  }
  $('visual-descriptions-load').onclick = async () => {
    if (busy) return;
    try {
      if (!state.current) throw new Error('请先打开一份资料。');
      const current = state.current;
      const value = await request(`/api/documents/${current.id}/visual-descriptions`);
      if (state.current?.id !== current.id) return;
      scope = {...value, documentId:current.id};
      const select = $('visual-descriptions-targets'); select.replaceChildren();
      for (const target of value.targets) {
        const option = makeElement('option','',target.label); option.value = target.asset_id; select.append(option);
      }
      if (select.options.length) select.options[0].selected = true;
      output().replaceChildren(makeElement('p','',value.targets.length ? '请选择范围，点击补全才会调用模型。' : '当前资料没有可查看的页面或已导入图片。'));
    } catch (e) { error(e); }
  };
  $('visual-descriptions-all').onclick = () => {
    for (const option of $('visual-descriptions-targets').options) option.selected = true;
  };
  $('visual-descriptions-form').onsubmit = async (event) => {
    event.preventDefault(); if (busy) return;
    if (!scope || state.current?.id !== scope.documentId || state.current?.source_id !== scope.source_id) return error(new Error('请重新加载当前资料范围。'));
    const asset_ids = [...$('visual-descriptions-targets').selectedOptions].map(o => o.value);
    if (!asset_ids.length) return error(new Error('请选择至少一页或一张图片。'));
    busy = true; $('visual-descriptions-start').disabled = true;
    const request_id = crypto.randomUUID().replaceAll('-','');
    const documentId = scope.documentId; let polling = false, observing = true;
    const timer = setInterval(async () => {
      if (polling) return; polling = true;
      try { const value = await request(`/api/visual-descriptions/${request_id}`); if (observing && state.current?.id === documentId) render(value); }
      catch { /* Request may still be entering admission. */ }
      finally { polling = false; }
    },1000);
    try {
      const value = await jsonRequest(`/api/documents/${documentId}/visual-descriptions`, {
        source_id:scope.source_id,request_id,asset_ids,max_requests:Number($('visual-descriptions-limit').value)});
      observing = false;
      if (state.current?.id === documentId) render(value);
    } catch (e) { if (state.current?.id === documentId) error(e); }
    finally { observing = false; clearInterval(timer); busy = false; $('visual-descriptions-start').disabled = false; }
  };
})();
