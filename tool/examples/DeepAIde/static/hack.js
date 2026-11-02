/* DeepCoder 对拍任务窗口 */
(function () {
  'use strict';

  const list = document.getElementById('hack-list');
  const summary = document.getElementById('hack-summary');
  const cards = {}; // taskId -> {card, nameEl, statusEl, body, stopBtn, restartBtn}

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

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function truncate(s, max) {
    s = String(s == null ? '' : s);
    if (s.length <= max) return s;
    return s.slice(0, max) + '\n…（已截断，共 ' + s.length + ' 字符）';
  }

  function getCard(taskId, s) {
    if (cards[taskId]) return cards[taskId];
    const card = el('div', 'hack-task');
    card.dataset.task = taskId;
    const head = el('div', 'hack-task-head');
    const nameEl = el('span', 'hack-task-name', '…');
    const statusEl = el('span', 'hack-task-status', '…');
    const saveBtn = el('button', 'btn mini hidden', '存为样例');
    const restartBtn = el('button', 'btn mini hidden', '重启');
    const stopBtn = el('button', 'btn mini hidden', '停止');
    saveBtn.addEventListener('click', (e) => { e.stopPropagation(); saveAsSample(taskId); });
    restartBtn.addEventListener('click', (e) => { e.stopPropagation(); restartHack(taskId); });
    stopBtn.addEventListener('click', (e) => { e.stopPropagation(); stopHack(taskId); });
    head.appendChild(nameEl);
    head.appendChild(statusEl);
    head.appendChild(saveBtn);
    head.appendChild(restartBtn);
    head.appendChild(stopBtn);
    head.addEventListener('click', () => card.classList.toggle('open'));
    const body = el('div', 'hack-task-body');
    card.appendChild(head);
    card.appendChild(body);
    list.appendChild(card);
    cards[taskId] = { card: card, nameEl: nameEl, statusEl: statusEl, body: body, saveBtn: saveBtn, stopBtn: stopBtn, restartBtn: restartBtn };
    return cards[taskId];
  }

  function render(taskId, s) {
    const c = getCard(taskId, s);
    c.nameEl.textContent = (s.main ? s.main + ' ⇄ ' + s.std : taskId);
    c.saveBtn.classList.add('hidden');
    if (s.status === 'running') {
      c.statusEl.textContent = `对拍进行中… 已完成 ${s.round} 轮`;
      c.statusEl.className = 'hack-task-status running';
      c.stopBtn.classList.remove('hidden');
      c.restartBtn.classList.add('hidden');
      c.body.textContent = s.spj_warn ? ('⚠ ' + s.spj_warn) : '';
    } else if (s.status === 'done') {
      c.statusEl.textContent = `✔ 全部通过（${s.round} 轮），未发现反例`;
      c.statusEl.className = 'hack-task-status ok';
      c.stopBtn.classList.add('hidden');
      c.restartBtn.classList.remove('hidden');
      c.body.textContent = s.spj_warn ? ('⚠ ' + s.spj_warn) : '';
    } else if (s.status === 'found') {
      c.statusEl.textContent = `✘ 第 ${s.round} 轮发现反例`;
      c.statusEl.className = 'hack-task-status fail';
      c.stopBtn.classList.add('hidden');
      c.restartBtn.classList.remove('hidden');
      c.saveBtn.classList.remove('hidden');
      c.body.textContent = '--- 输入 ---\n' + truncate(s.input, 10000) +
        '\n\n--- 期望输出 ---\n' + truncate(s.answer, 10000) +
        '\n\n--- 实际输出 ---\n' + truncate(s.output, 10000) +
        '\n\n反例已保存到 .build/hack_' + taskId + '/';
      c.card.classList.add('open');
    } else if (s.status === 'error') {
      c.statusEl.textContent = '错误';
      c.statusEl.className = 'hack-task-status fail';
      c.stopBtn.classList.add('hidden');
      c.restartBtn.classList.remove('hidden');
      c.body.textContent = s.message || '';
      c.card.classList.add('open');
    } else if (s.status === 'stopped') {
      c.statusEl.textContent = `已停止（${s.round} 轮）`;
      c.statusEl.className = 'hack-task-status stopped';
      c.stopBtn.classList.add('hidden');
      c.restartBtn.classList.remove('hidden');
    }
  }

  async function saveAsSample(taskId) {
    const r = await api('POST', '/api/hack_to_sample', { task: taskId });
    if (!r.ok) { alert(r.error || '保存失败'); return; }
    alert('反例已保存为 ' + r.in + ' / ' + r.out + '。\n回到主窗口点「↻ 刷新」即可在样例列表中看到。');
  }

  async function stopHack(taskId) {
    await api('POST', '/api/hack_stop', { task: taskId });
  }

  async function restartHack(taskId) {
    const r = await api('GET', '/api/hack_status?task=' + encodeURIComponent(taskId));
    if (r.error || !r.main) { alert('无法读取任务信息'); return; }
    const res = await api('POST', '/api/hack', {
      main: r.main, std: r.std, gen: r.gen,
      rounds: r.rounds || 0, io: r.io || 'stdio', name: r.name || '',
      time_limit: r.time_limit || 2000, memory_limit: r.memory_limit || 256,
    });
    if (!res.ok) { alert(res.error || '重启失败'); return; }
    delete cards[taskId];
    const old = document.querySelector('.hack-task[data-task="' + taskId + '"]');
    if (old) old.remove();
  }

  async function refresh() {
    const r = await api('GET', '/api/hack_list');
    if (r.error) { summary.textContent = '连接错误：' + r.error; return; }
    const tasks = r.tasks || {};
    const ids = Object.keys(tasks);
    let running = 0;
    for (const id of ids) if (tasks[id].status === 'running') running++;
    summary.textContent = ids.length
      ? `共 ${ids.length} 个任务，${running} 个运行中`
      : '暂无任务';
    // 清理已不在服务器上的卡片（被服务端裁剪）
    for (const id of Object.keys(cards)) {
      if (!tasks[id]) {
        const card = document.querySelector('.hack-task[data-task="' + id + '"]');
        if (card) card.remove();
        delete cards[id];
      }
    }
    const sorted = ids.sort((a, b) => parseInt(a) - parseInt(b));
    for (const id of sorted) render(id, tasks[id]);
  }

  setInterval(refresh, 1000);
  refresh();
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') window.close();
  });
})();
