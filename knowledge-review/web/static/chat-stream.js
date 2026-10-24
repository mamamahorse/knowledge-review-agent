/* Decode UTF-8 and SSE independently of network chunks; a saved result is authoritative. */
async function consumeChatStream(response, onEvent) {
  if (!response.ok) {
    let body = {}; try { body = await response.json(); } catch { }
    throw new Error(typeof body.detail === 'string' ? body.detail : '请求未完成，请稍后再试。');
  }
  if (!response.headers.get('content-type')?.toLowerCase().startsWith('text/event-stream') || !response.body) {
    throw new Error('服务器没有返回可读取的回答流。');
  }
  const reader = response.body.getReader(), decoder = new TextDecoder('utf-8', {fatal: true});
  const limit = 4 * 1024 * 1024 + 16384;
  let buffer = '', event = '', data = [], eventSize = 0, turn = null, done = false, doneStatus = null;
  function line(value) {
    if (value === '') {
      if (data.length) {
        if (done) throw new Error('终态后收到额外回答事件。');
        const payload = JSON.parse(data.join('\n'));
        if (event === 'result') turn = payload.turn;
        if (event === 'done') { done = true; doneStatus = payload.status; }
        onEvent(event, payload);
      }
      event = ''; data = []; eventSize = 0;
    } else if (value.startsWith('event:')) event = value.slice(6).trim();
    else if (value.startsWith('data:')) data.push(value.slice(5).replace(/^ /, ''));
  }
  try {
    while (true) {
      const next = await reader.read();
      buffer += decoder.decode(next.value, {stream: !next.done});
      let end;
      while ((end = buffer.indexOf('\n')) !== -1) {
        eventSize += end + 1;
        if (eventSize > limit) throw new Error('回答事件超过显示限制。');
        line(buffer.slice(0, end).replace(/\r$/, ''));
        buffer = buffer.slice(end + 1);
      }
      if (buffer.length + eventSize > limit) throw new Error('回答事件超过显示限制。');
      if (next.done) break;
    }
    if (buffer.length || data.length || !done) throw new Error('连接中断，请读取聊天记录确认保存状态。');
    if (doneStatus !== 'completed' || turn?.status !== 'completed') throw new Error('生成未完成，输入已保存，可以重试。');
    return turn;
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
if (typeof module !== 'undefined') module.exports = {consumeChatStream};
