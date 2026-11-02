/* DeepCoder 交互模式独立窗口 */
(function () {
  'use strict';

  const params = new URLSearchParams(location.search);
  const session = params.get('session') || '';
  const out = document.getElementById('inter-out');
  const input = document.getElementById('inter-input');
  const statusEl = document.getElementById('inter-status');
  const sendBtn = document.getElementById('inter-send');
  const killBtn = document.getElementById('inter-kill');
  let ended = false;

  async function api(method, url, body) {
    const opt = { method: method, headers: {} };
    if (body !== undefined) {
      opt.headers['Content-Type'] = 'application/json';
      opt.body = JSON.stringify(body);
    }
    let r;
    try {
      r = await fetch(url, opt);
    } catch (e) {
      return { error: '网络错误：' + e.message };
    }
    try {
      return await r.json();
    } catch (e) {
      return { error: '服务器响应异常（HTTP ' + r.status + '）' };
    }
  }

  function addText(t) {
    out.textContent += t;
    out.scrollTop = out.scrollHeight;
  }

  function finish(statusText) {
    if (ended) return;
    ended = true;
    statusEl.textContent = statusText;
    input.disabled = true;
    sendBtn.disabled = true;
    killBtn.disabled = true;
  }

  async function poll() {
    const r = await api('POST', '/api/interactive_poll', { session: session });
    if (r.error) { finish('连接错误：' + r.error); return; }
    if (r.status === 'starting') {
      statusEl.textContent = '编译中…';
      if (!ended) setTimeout(poll, 200);
      return;
    }
    if (r.status === 'ce') {
      out.textContent += (r.log || '（无编译输出）') + '\n';
      out.scrollTop = out.scrollHeight;
      finish('编译失败');
      return;
    }
    if (r.out) addText(r.out);
    if (r.err) addText(r.err);
    if (r.status === 'exited') {
      addText('\n[进程结束，退出码 ' + r.exit_code + ']\n');
      finish('进程已结束');
      return;
    }
    if (r.status === 'not_found') { finish('会话不存在'); return; }
    if (r.status === 'running' && statusEl.textContent !== '运行中') statusEl.textContent = '运行中';
    if (!ended) setTimeout(poll, 200);
  }

  async function send() {
    const text = input.value;
    if (text === '' || ended) return;
    addText('> ' + text + '\n');
    await api('POST', '/api/interactive_input', { session: session, text: text + '\n' });
    input.value = '';
    input.focus();
  }

  async function kill() {
    if (ended) return;
    await api('POST', '/api/interactive_kill', { session: session });
    finish('已手动结束');
    addText('\n[已手动结束]\n');
  }

  if (!session) {
    finish('缺少会话参数');
    return;
  }
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') window.close();
  });
  // 关闭窗口 → 立即通知服务器杀掉进程
  window.addEventListener('beforeunload', () => {
    if (ended) return;
    try {
      fetch('/api/interactive_kill', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session: session }),
        keepalive: true,
      });
    } catch (e) { /* ignore */ }
  });
  sendBtn.addEventListener('click', send);
  killBtn.addEventListener('click', kill);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') send();
  });
  input.focus();
  poll();
})();
