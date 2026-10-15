/* Review original public evidence, never a retrieval system's rankings. */
const $ = id => document.getElementById(id);
let overview = null, current = null, edited = null, revision = 0, dirty = false, epoch = 0;
function el(tag, text, cls) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (cls) node.className = cls; return node; }
function notice(text) { $('message').textContent = text; }
async function api(path, options) {
  const response = await fetch('/api/retrieval-benchmark' + path, options);
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '输入格式不符合要求。');
  return body;
}
function label(chunk) { return `${chunk.file} · L${chunk.line_start}–L${chunk.line_end}`; }
function markDirty() { dirty = true; }
function discard() { return !dirty || confirm('尚有未保存的审核修改。离开并放弃这些修改？'); }
function refreshList() {
  $('query-list').replaceChildren();
  const pending = overview.queries.filter(q => q.review.payload.status === 'pending');
  const automatic = overview.queries.filter(q => q.review.payload.status === 'auto_accepted').length;
  const confirmed = overview.queries.filter(q => q.review.payload.status === 'approved').length;
  $('progress').textContent = `仅 ${pending.length} 条待处理 · ${automatic} 条自动采用 · ${confirmed} 条用户确认 · 共 ${overview.queries.length} 条`;
  const visible = $('show-all').checked ? overview.queries : pending;
  for (const query of visible) {
    const state = {pending:'待复核', approved:'用户已确认', excluded:'已排除',auto_accepted:'自动采用'}[query.review.payload.status];
    const button = el('button', `${state} · ${query.text}`, current?.query.id === query.id ? 'active' : '');
    button.type = 'button'; button.onclick = () => { if(discard()) openQuery(query.id).catch(e=>notice(e.message)); };
    $('query-list').append(button);
  }
  if (!visible.length) $('query-list').append(el('p',overview.freezes.length?'没有待处理分歧；已有冻结版本，可展开查看。':'没有需要你处理的分歧，可冻结当前版本。','muted'));
  $('freeze-history').replaceChildren();
  for (const frozen of overview.freezes) $('freeze-history').append(el('p', `冻结版本：${frozen.id}\n${frozen.created_at}`, 'muted'));
}
async function openBenchmark(identity) {
  const token = ++epoch;
  const data = await api('/' + encodeURIComponent(identity));
  if (token !== epoch) return;
  overview = data; current = null; dirty = false; refreshList();
  const first = overview.queries.find(q=>q.review.payload.status === 'pending');
  if(first) await openQuery(first.id); else {$('detail').hidden=true;notice(overview.freezes.length?`没有待处理分歧。已保存 ${overview.freezes.length} 个冻结版本，可展开查看记录；修改后可冻结新版本。`:'没有待处理分歧。可以冻结，或勾选查看全部样例。');}
}
async function openQuery(identity) {
  const token = ++epoch;
  const data = await api(`/${overview.id}/queries/${encodeURIComponent(identity)}`);
  if (token !== epoch) return;
  current = data; revision = data.review.revision; edited = structuredClone(data.review.payload); dirty = false;
  $('detail').hidden = false;
  $('question').textContent = data.query.text;
  $('metadata').textContent = `${data.query.split === 'dev' ? '开发集' : '测试集'} · ${data.query.category} · 许可资料：${data.query.scope.join('、')} · 审核修订 ${revision}`;
  $('audit-status').textContent = edited.status === 'auto_accepted' ? 'Agent 评审已采用，无需确认' : edited.status === 'approved' ? '用户已确认，可继续修改' : edited.status === 'excluded' ? '已明确排除，理由保留' : '待 Agent 复核，争议理由保留';
  $('audit-reason').textContent = data.agent_audit?.reason || data.query.audit.reason;
  $('audit-coverage').textContent = data.agent_audit ? 'Agent 已检查本题整个许可范围并记录理由。你可选择查看、修改或排除，无需逐条确认。' : '这份旧草案尚没有完整 Agent 初审，未判断仍需补齐。';
  $('draft').textContent = JSON.stringify({annotations:data.query.annotations,evidence_groups:data.query.evidence_groups,suggested_changes:data.query.audit.suggested_changes || null},null,2);
  $('answerable').checked = edited.answerable;
  $('scope-checked').checked = edited.scope_checked;
  $('resolution').value = edited.issue_resolution;
  $('review-note').value = edited.origin === 'agent' ? '' : edited.note;
  $('chunk-filter').value = data.agent_audit ? 'focus' : 'all';
  $('advanced-edit').open = false;
  renderEvidenceSummary();
  $('source-text').replaceChildren();
  for (const source of data.sources) {
    const details = el('details'); details.append(el('summary',source.file));
    details.append(el('pre',source.text.split('\n').map((line,i)=>`L${i+1}  ${line}`).join('\n'),'original'));
    $('source-text').append(details);
  }
  renderChunks(); renderGroups(); refreshList();
  notice(edited.status==='pending'?'此项保留待 Agent 复核；也可手动修改并留理由。':'本条无需重复确认；修改后会保留你的决定来源。');
}
function renderEvidenceSummary() {
  $('evidence-summary').replaceChildren();
  $('evidence-summary').append(el('p',edited.answerable?'建议：许可资料可回答。':'建议：许可资料不足，不标成具体答案证据。'));
  for(const group of edited.groups){
    const bundles=group.bundles.map(bundle=>bundle.map(id=>label(current.chunks.find(c=>c.id===id))).join(' + ')).join(' 或 ');
    $('evidence-summary').append(el('p',`${group.label}：${bundles}`,'muted'));
  }
}
function renderChunks() {
  const filter = $('chunk-filter').value; $('chunks').replaceChildren();
  for (const chunk of current.chunks) {
    const judgment = edited.judgments.find(j=>j.chunk_id === chunk.id);
    if (filter === 'focus' && !(current.agent_audit?.focus_chunk_ids || []).includes(chunk.id) || filter === 'positive' && !(judgment.grade > 0) || filter === 'unjudged' && judgment.grade !== null) continue;
    const card = el('article',undefined,'chunk'); card.append(el('strong',label(chunk)));
    const select = el('select'); select.setAttribute('aria-label',label(chunk)+' 相关性');
    for (const [value,text] of [['','未判断'],['0','0 无关 / 条件错误'],['1','1 部分支持'],['2','2 充分支持']]) {
      const option = el('option',text); option.value=value; select.append(option);
    }
    select.value = judgment.grade === null ? '' : String(judgment.grade);
    select.onchange = () => { judgment.grade=select.value === '' ? null : Number(select.value); markDirty(); };
    card.append(select,el('pre',chunk.text));
    const note = el('textarea'); note.value=judgment.note; note.maxLength=3000; note.setAttribute('aria-label',label(chunk)+' 判断理由');
    note.oninput=()=>{judgment.note=note.value;markDirty();}; card.append(note); $('chunks').append(card);
  }
}
function renderGroups() {
  $('groups').replaceChildren();
  edited.groups.forEach((group,gi)=>{
    const card=el('div',undefined,'group'), title=el('input'); title.value=group.label;title.maxLength=500;title.setAttribute('aria-label','必要事实名称');
    title.oninput=()=>{group.label=title.value;markDirty();}; card.append(title);
    const remove=el('button','删除组');remove.onclick=()=>{edited.groups.splice(gi,1);markDirty();renderGroups();};card.append(remove);
    group.bundles.forEach((bundle,bi)=>{
      const block=el('div',undefined,'bundle');block.append(el('p',`替代组合 ${bi+1}：选中的片段需要同时出现`,'muted'));
      const select=el('select');select.multiple=true;select.size=Math.min(7,current.chunks.length); select.setAttribute('aria-label',`${group.label} 替代组合 ${bi+1}`);
      for(const chunk of current.chunks){const option=el('option',label(chunk)+' '+chunk.text.slice(0,55).replaceAll('\n',' '));option.value=chunk.id;option.selected=bundle.includes(chunk.id);select.append(option);}
      select.onchange=()=>{group.bundles[bi]=[...select.selectedOptions].map(o=>o.value);markDirty();};block.append(select);
      const button=el('button','删除组合');button.onclick=()=>{group.bundles.splice(bi,1);markDirty();renderGroups();};block.append(button);card.append(block);
    });
    const add=el('button','添加可替代组合');add.onclick=()=>{group.bundles.push([]);markDirty();renderGroups();};card.append(add);$('groups').append(card);
  });
}
async function save(status) {
  if (!current) return;
  edited.answerable=$('answerable').checked;edited.scope_checked=$('scope-checked').checked;
  edited.issue_resolution=$('resolution').value;edited.note=$('review-note').value;
  if(status==='approved' && !edited.issue_resolution.trim() && current.agent_audit) {
    const proposed=current.agent_audit.review;
    const grades=payload=>payload.judgments.map(j=>[j.chunk_id,j.grade]);
    if(edited.answerable!==proposed.answerable || JSON.stringify(grades(edited))!==JSON.stringify(grades(proposed)) || JSON.stringify(edited.groups)!==JSON.stringify(proposed.groups)){
      $('advanced-edit').open=true;throw new Error('你修改了建议，请填写处理理由后保存。');
    }
    edited.issue_resolution='用户采用 Agent 建议：'+current.agent_audit.reason;
  }
  if(status==='excluded' && !edited.note.trim()){$('advanced-edit').open=true;throw new Error('请填写排除此样例的原因。');}
  const payload=structuredClone(edited);payload.status=status;
  const token=epoch, queryId=current.query.id;
  const saved=await api(`/${overview.id}/queries/${queryId}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:revision,payload})});
  if(token !== epoch) return;
  revision=saved.revision;edited=saved.payload;dirty=false;
  overview.queries.find(q=>q.id===queryId).review=saved;refreshList();
  $('metadata').textContent=`${current.query.split} · ${current.query.category} · 审核修订 ${revision}`;
  $('resolution').value=edited.issue_resolution;
  $('audit-status').textContent=status==='approved'?'用户已确认，可继续修改':'已保存当前决定';
  notice(status==='approved'?'已采用建议并记录为用户确认，可继续处理其他分歧。':status==='excluded'?'已记录排除理由。':'待处理修改已保存。');
  if(status!=='pending' && !$('show-all').checked){
    const next=overview.queries.find(q=>q.review.payload.status==='pending');
    if(next){await openQuery(next.id);notice('上一条已处理，继续查看下一条分歧。');}
    else {$('detail').hidden=true;notice('分歧已全部处理。可以冻结标注版本，无需确认自动采用的样例。');}
  }
}
async function locked(action) {
  const controls = [...document.querySelectorAll('button,input,select,textarea')];
  const states = controls.map(node=>node.disabled);
  controls.forEach(node=>node.disabled=true);
  try { return await action(); }
  finally { controls.forEach((node,i)=>node.disabled=states[i]); }
}
for (const id of ['answerable','scope-checked','resolution','review-note']) $(id).addEventListener('input',markDirty);
$('chunk-filter').onchange=renderChunks;
$('show-all').onchange=refreshList;
$('mark-rest').onclick=()=>{if(confirm('请先核对整个许可范围。确认将其余所有未判断片段标为无关？')){edited.judgments.forEach(j=>{if(j.grade===null)j.grade=0;});markDirty();renderChunks();}};
$('add-group').onclick=()=>{edited.groups.push({id:crypto.randomUUID(),label:'新的必要事实',bundles:[[]]});markDirty();renderGroups();};
for(const [id,status] of [['save-draft','pending'],['approve','approved'],['exclude','excluded']]) $(id).onclick=()=>locked(()=>save(status)).catch(e=>notice(e.message));
$('benchmark').onchange=()=>{if(discard())openBenchmark($('benchmark').value).catch(e=>notice(e.message));else $('benchmark').value=overview.id;};
$('freeze-form').onsubmit=async event=>{event.preventDefault();if(dirty){notice('请先保存当前审核修改，再冻结。');return;}
  try{const result=await api(`/${overview.id}/freeze`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reviewer:$('reviewer').value,note:$('freeze-note').value,confirmed:$('freeze-confirm').checked,expected_revisions:Object.fromEntries(overview.queries.map(q=>[q.id,q.review.revision]))})});
    const updated=await api('/'+overview.id);overview.freezes=updated.freezes;refreshList();$('freeze-confirm').checked=false;notice(`冻结成功：${result.id}\n${result.notice}`);
  }catch(error){notice(error.message);}
};
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
(async()=>{try{const data=await api('');if(!data.benchmarks.length){notice('尚未准备样例。请按方案文档执行 prepare，然后刷新。');return;}
  $('reviewer').value='本机用户';$('freeze-note').value='Agent 完整初审 + 用户处理分歧；分别保留自动采用和用户确认来源。';
  for(const benchmark of data.benchmarks){const option=el('option',benchmark.name+' · '+benchmark.id.slice(0,8));option.value=benchmark.id;$('benchmark').append(option);}
  const requested=new URL(location.href).searchParams.get('benchmark');if(data.benchmarks.some(b=>b.id===requested))$('benchmark').value=requested;
  await openBenchmark($('benchmark').value);
}catch(error){notice(error.message);}})();
