/* The API key is exchanged for an opaque HttpOnly cookie, never persisted by JS. */
(() => {
  let enabled = false, activeLogin = null, ready = null, authVersion = 0;

  // One consumer can stop waiting without cancelling other requests' login.
  function waitFor(promise, signal) {
    if (!signal) return promise;
    signal.throwIfAborted();
    return new Promise((resolve, reject) => {
      const cleanup = () => signal.removeEventListener('abort', abort);
      const abort = () => { cleanup(); reject(signal.reason); };
      signal.addEventListener('abort', abort, {once: true});
      Promise.resolve(promise).then(
        value => { cleanup(); resolve(value); },
        error => { cleanup(); reject(error); },
      );
    });
  }

  function gatewayError(response, body) {
    const messages = {
      RATE_LIMITED: '请求过于频繁，请稍后再试。',
      GATEWAY_OVERLOADED: '当前生成任务较多，请稍后再试。',
      REDIS_UNAVAILABLE: '请求检查服务暂时不可用，请稍后再试。',
      GATEWAY_TIMEOUT: '等待服务响应超时，请读取聊天记录确认是否已保存。',
      UPSTREAM_CONNECT_TIMEOUT: '暂时无法连接学习服务，请稍后再试。',
      UPSTREAM_ERROR: '学习服务连接中断，请读取聊天记录确认是否已保存。',
      BODY_TOO_LARGE: '提交内容过大，请缩小后再试。',
    };
    const code = body.error?.code;
    if (!Object.hasOwn(messages, code)) return null;
    const retry = response.headers.get('Retry-After');
    const seconds = retry && /^\d+$/.test(retry) ? Number(retry) : 0;
    const hint = ['RATE_LIMITED', 'GATEWAY_OVERLOADED'].includes(code)
      && Number.isSafeInteger(seconds) && seconds > 0 ? `建议 ${seconds} 秒后重试。` : '';
    return new Error(messages[code] + hint);
  }

  async function errorBody(response) {
    return !response.ok && enabled ? await response.clone().json().catch(() => ({})) : {};
  }
  async function login() {
    if (activeLogin) return activeLogin;
    activeLogin = new Promise((resolve, reject) => {
      const dialog = document.createElement('dialog');
      dialog.className = 'gateway-login';
      const heading = document.createElement('h2'); heading.textContent = '连接学习工作台';
      const form = document.createElement('form');
      const label = document.createElement('label'); label.textContent = 'Gateway API Key';
      const input = document.createElement('input'); input.type = 'password'; input.required = true;
      input.autocomplete = 'off'; input.maxLength = 512; input.setAttribute('aria-label', 'Gateway API Key');
      label.append(input);
      const button = document.createElement('button'); button.textContent = '连接';
      const cancel = document.createElement('button'); cancel.type = 'button'; cancel.textContent = '取消连接';
      const message = document.createElement('p'); message.setAttribute('role', 'status');
      form.append(label, button, cancel, message); dialog.append(heading, form); document.body.append(dialog);
      const cancelLogin = () => {
        input.value = ''; dialog.close(); dialog.remove();
        reject(new DOMException('连接已取消，可以重新连接。', 'AbortError'));
      };
      cancel.addEventListener('click', cancelLogin);
      dialog.addEventListener('cancel', event => { event.preventDefault(); cancelLogin(); });
      form.addEventListener('submit', async event => {
        event.preventDefault(); button.disabled = true; cancel.disabled = true; message.textContent = '';
        try {
          const response = await fetch('/gateway/session', {method: 'POST', headers: {'X-API-Key': input.value}, cache: 'no-store'});
          input.value = '';
          if (!response.ok) throw new Error('连接未成功，请检查 API Key。');
          authVersion += 1;
          dialog.close(); dialog.remove(); resolve();
        } catch (error) { message.textContent = error.message; }
        finally { button.disabled = false; cancel.disabled = false; }
      });
      dialog.showModal(); input.focus();
    }).finally(() => { activeLogin = null; });
    return activeLogin;
  }
  async function checkSession() {
    const response = await fetch('/gateway/session', {cache: 'no-store'});
    if (response.status === 404) return; // The independent Python app stays usable.
    enabled = true;
    if (response.status === 401) await login();
    else if (!response.ok) throw new Error('Gateway 暂时不可用。');
  }
  function ensureReady() {
    if (!ready) {
      const pending = checkSession();
      ready = pending;
      // A failed probe must not poison every later operation until reload.
      pending.catch(() => { if (ready === pending) ready = null; });
    }
    return ready;
  }
  window.gatewayFetch = async (url, options = {}) => {
    options.signal?.throwIfAborted();
    await waitFor(activeLogin || ensureReady(), options.signal);
    const version = authVersion;
    let response = await fetch(url, options);
    let body = await errorBody(response);
    // A gateway 401 is rejected before any upstream operation; retry only this case.
    if (enabled && response.status === 401 && body.error?.code === 'INVALID_API_KEY') {
      // A delayed 401 may belong to the session another request already replaced.
      if (version === authVersion) await waitFor(login(), options.signal);
      options.signal?.throwIfAborted();
      response = await fetch(url, options);
      body = await errorBody(response);
    }
    const error = enabled && !response.ok ? gatewayError(response, body) : null;
    if (error) throw error;
    return response;
  };
  ensureReady().catch(error => { console.error(error.message); });
})();
