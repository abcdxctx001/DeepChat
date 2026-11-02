/* DeepCoder 前端逻辑 */
(function () {
  'use strict';

  // ---------- 状态 ----------
  const state = {
    editor: null,
    currentPath: null,
    currentProblem: null,
    config: {},         // 当前题目配置 {io, time_limit, memory_limit}
    tree: [],
    samples: [],        // 当前题目样例 {label, in, out}
    selected: new Set(), // 多选的文件路径
    fontSize: parseInt(localStorage.getItem('deepcoder.fontSize')) || 14,
    editorSettings: { theme: 'material-darker' },
    compile: { compiler: 'g++', flags: '-O2 -std=c++14 -Wl,--stack=2147483647 -static',
               python: '', javac: '', java: '' },
    dirty: false,
    mdMode: 'edit',     // Markdown 三态：'edit' 只编辑 | 'split' 边编辑边渲染 | 'render' 只显示渲染
    // 分屏：右侧面板（独立编辑器与当前文件/题目配置；右侧栏归属见 sidepanelPane）
    right: { editor: null, currentPath: null, dirty: false, mdMode: 'edit',
             currentProblem: null, config: { io: 'file', time_limit: 2000, memory_limit: 256 }, samples: [] },
    active: 'left',     // 'left' | 'right'：点击文件时打开到哪个面板
  };

  function activePane() { return state.active === 'right' ? state.right : state; }
  function activeEditor() { return activePane().editor; }
  function activePath() { return activePane().currentPath; }
  function isSplit() { return !$('#pane-right').classList.contains('hidden'); }

  // ---------- API ----------
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

  // ---------- 工具 ----------
  function $(sel) { return document.querySelector(sel); }
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function parentDir(path) {
    const parts = path.split('/');
    parts.pop();
    return parts.join('/');
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function truncate(s, max) {
    s = String(s == null ? '' : s);
    if (s.length <= max) return s;
    return s.slice(0, max) + '\n…（已截断，共 ' + s.length + ' 字符）';
  }
  function modeForPath(path) {
    const l = path.toLowerCase();
    if (l.endsWith('.cpp') || l.endsWith('.c') || l.endsWith('.h') || l.endsWith('.hpp')) return 'text/x-c++src';
    if (l.endsWith('.py')) return 'text/x-python';
    if (l.endsWith('.java')) return 'text/x-java';
    if (l.endsWith('.md')) return 'text/x-markdown';
    return 'text/plain';
  }
  function isCodeExt(path) {
    const l = path.toLowerCase();
    return ['.cpp', '.c', '.py', '.java'].some(e => l.endsWith(e));
  }
  function isRunnable(path) {
    const l = path.toLowerCase();
    return isCodeExt(path) || l.endsWith('.h') || l.endsWith('.hpp');
  }
  function compileLangFor(path) {
    const l = (path || '').toLowerCase();
    if (l.endsWith('.py')) return 'python';
    if (l.endsWith('.java')) return 'java';
    return 'cpp';
  }
  // 各语言默认 IO 模式：C/C++ 模板含 freopen → file；Python/Java 无文件读写 → stdio
  function defaultIoFor(path) {
    const l = (path || '').toLowerCase();
    if (l.endsWith('.cpp') || l.endsWith('.c')) return 'file';
    return 'stdio';
  }
  // 右侧栏归属面板：
  // - 未分屏：活动面板（打开代码文件显示右侧栏，非代码文件隐藏）
  // - 分屏中恰好一侧是代码：固定代码文件那一侧（不随活动面板切换）
  // - 分屏中两侧都是代码：跟随活动面板
  // - 分屏中两侧都不是代码：返回 null（右侧栏隐藏）
  function sidepanelPane() {
    if (isSplit() && state.right.currentPath) {
      const lCode = isCodeExt(state.currentPath || '');
      const rCode = isCodeExt(state.right.currentPath || '');
      if (lCode && !rCode) return state;
      if (!lCode && rCode) return state.right;
      if (lCode && rCode) return activePane();
      return null;
    }
    return activePane();
  }

  // 右侧栏统一刷新：显隐 + 编译设置语言区 + 题目配置 + 样例，全部基于 sidepanelPane()
  function refreshSidepanel() {
    const sp = sidepanelPane();
    const show = isSplit() ? !!sp : (!activePane().currentPath || isCodeExt(activePane().currentPath));
    $('#sidepanel').classList.toggle('hidden', !show);
    if (!show) return;
    const pane = sp || activePane();
    const lang = compileLangFor(pane.currentPath);
    updateCompilePanel(lang);
    $('#field-spj').classList.toggle('hidden', lang !== 'cpp'); // SPJ 仅 C++ 有效
    updateConfigPanel(pane.config, pane);
    if (pane.currentProblem) {
      const cached = problemCache[pane.currentProblem];
      renderSamples(cached ? cached.samples : (pane.samples || []), pane);
    } else {
      renderSamples([], pane);
    }
  }
  function iconForPath(name) {
    const l = name.toLowerCase();
    if (l.endsWith('.cpp') || l.endsWith('.c') || l.endsWith('.h') || l.endsWith('.hpp')) return '⚙';
    if (l.endsWith('.py')) return '🐍';
    if (l.endsWith('.java')) return '☕';
    if (l.endsWith('.pdf')) return '📕';
    return '📄';
  }

  // ---------- 文件树 ----------
  function renderTree() {
    const root = $('#file-tree');
    root.innerHTML = '';
    const walk = (nodes) => {
      const out = [];
      for (const n of nodes) {
        if (n.type === 'dir') {
          const item = el('div', 'tree-item dir');
          item.dataset.path = n.path;
          item.appendChild(el('span', 'icon', '📁'));
          item.appendChild(el('span', 'name', n.name));
          const children = el('div', 'tree-children');
          children.dataset.path = n.path;
          walk(n.children).forEach(c => children.appendChild(c));
          item.addEventListener('click', (e) => {
            if (e.ctrlKey || e.metaKey) {
              e.stopPropagation();
              toggleSelect(n.path, item);
            } else {
              e.stopPropagation();
              children.style.display = children.style.display === 'none' ? '' : 'none';
            }
          });
          out.push(item, children);
        } else {
          const item = el('div', 'tree-item file');
          const isCpp = /\.(cpp|c|h|hpp)$/i.test(n.name);
          item.dataset.path = n.path;
          item.appendChild(el('span', 'icon' + (isCpp ? ' cpp' : ''), iconForPath(n.name)));;
          item.appendChild(el('span', 'name', n.name));
          item.addEventListener('click', (e) => {
            if (e.ctrlKey || e.metaKey) {
              e.stopPropagation();
              toggleSelect(n.path, item);
            } else {
              openFile(n.path);
              clearSelection();
            }
          });
          out.push(item);
        }
      }
      return out;
    };
    walk(state.tree).forEach(n => root.appendChild(n));
    refreshSelectionHighlight();
  }

  function refreshSelectionHighlight() {
    document.querySelectorAll('.tree-item').forEach(t =>
      t.classList.toggle('selected', state.selected.has(t.dataset.path)));
  }

  function toggleSelect(path, item) {
    if (state.selected.has(path)) state.selected.delete(path);
    else state.selected.add(path);
    refreshSelectionHighlight();
  }

  function clearSelection() {
    state.selected.clear();
    refreshSelectionHighlight();
  }

  async function loadTree() {
    const r = await api('GET', '/api/tree');
    state.tree = r.tree || [];
    renderTree();
  }

  // ---------- 右键菜单 ----------
  function showContextMenu(e, node) {
    e.preventDefault();
    const isRoot = node.type === 'dir' && !node.path;
    if (!isRoot) {
      if (!state.selected.has(node.path)) {
        state.selected.clear();
        state.selected.add(node.path);
        refreshSelectionHighlight();
      }
    }
    const old = document.querySelector('.ctx-menu');
    if (old) old.remove();
    const menu = el('div', 'ctx-menu');
    menu.style.cssText = `position:fixed;left:${e.clientX}px;top:${e.clientY}px;`;
    const add = (label, fn) => {
      const it = el('div', 'ctx-item', label);
      it.addEventListener('click', () => { menu.remove(); fn(); });
      menu.appendChild(it);
    };
    const dirPath = node.type === 'dir' ? node.path : parentDir(node.path);
    add('新建文件', () => promptNewFile(dirPath));
    add('新建文件夹', () => promptNewDir(dirPath));
    add('上传文件', () => promptUpload(dirPath));
    if (node.type === 'file') {
      add('在右侧打开', () => openInPane(node.path, 'right'));
      add('下载', () => downloadFile(node.path));
    }
    if (isSplit()) {
      add('关闭分屏', () => disableSplit());
    }
    if (!isRoot) {
      add('重命名', () => promptRename(node));
      const n = state.selected.size;
      add(n > 1 ? `删除选中 (${n})` : '删除', () => doDeleteSelected());
    }
    document.body.appendChild(menu);
    document.addEventListener('click', () => menu.remove(), { once: true });
  }

  // ---------- 编辑器 ----------
  function createCodeMirror(host, isRight) {
    const cm = CodeMirror(host, {
      value: '',
      mode: 'text/x-c++src',
      theme: 'dracula',
      lineNumbers: true,
      indentUnit: 4,
      indentWithTabs: true,
      tabSize: 4,
      lineWrapping: false,
      styleActiveLine: true,
      matchBrackets: true,
      autoCloseBrackets: true,
      extraKeys: {
        'F9': () => { compileOnly(); return false; },
        'F10': () => { startInteractive(); return false; },
        'F11': () => { runAllSamples(); return false; },
      },
    });
    applyEditorFont();
    applyEditorSettings();
    setupBracketMatching(cm);
    // Ctrl + 滚轮调节字号：capture 阶段拦截，避免调字号时文件滚动
    const scroller = cm.getScrollerElement();
    scroller.addEventListener('wheel', onEditorWheel, { passive: false, capture: true });
    cm.getWrapperElement().addEventListener('wheel', onEditorWheel, { passive: false });
    // 代码补全：自动触发 + Ctrl+Space 手动触发 + 弹层键盘导航
    cm.on('keydown', (c, e) => { handleEditorKeydown(c, e); });
    cm.on('blur', () => { hideCompletionBox(); cancelGhost(cm); });
    cm.on('scroll', () => { if (completer.visible) hideCompletionBox(); });
    cm.on('cursorActivity', (c) => {
      if (ghostState.suppress) return;
      if (ghostState.active) cancelGhost(c);
      resetGhostTimer(c, isRight ? state.right : state);
    });
    cm.on('focus', (c) => { resetGhostTimer(c, isRight ? state.right : state); });
    cm.on('change', (c, change) => {
      if (suppressChange) return;   // 程序化赋值（setValue 等）：不处理
      // ghost 自身操作（插入/取消）：不标记 dirty、不重置计时
      if (change.origin === '+aiGhost' || change.origin === '+aiGhostCancel' || ghostState.suppress) return;
      // 用户输入：取消 ghost 并重新 5 秒计时
      if (ghostState.active) cancelGhost(c);
      resetGhostTimer(c, isRight ? state.right : state);
      const pane = isRight ? state.right : state;
      if (!pane.dirty) {
        pane.dirty = true;
        if (pane === activePane()) $('#dirty-mark').classList.remove('hidden');
      }
      const ins = change && change.text ? change.text.join('') : '';
      if (ins.includes('{')) {
        setTimeout(() => maybeSemicolonAfterOpenBrace(c), 0);
      }
      if (ins.includes('}')) {
        setTimeout(() => maybeAddSemicolon(c), 0);
      }
      // 补全仅对代码文件（md/txt 等不触发，避免打标点时整文件上传请求）
      if (isCodeExt(pane.currentPath || '')) {
        if (completionTriggerForChange(c, ins)) {
          setTimeout(() => triggerCompletion(true), 0);
        } else if (completer.visible && /[A-Za-z0-9_]$/.test(ins)) {
          scheduleAutoComplete();
        }
      } else if (completer.visible) {
        hideCompletionBox();
      }
      if (pane.mdMode === 'split') scheduleMdPreview(pane);
    });
    return cm;
  }

  function initEditor() {
    loadEditorSettings();
    state.editor = createCodeMirror($('#editor'), false);
    state.right.editor = createCodeMirror($('#editor-right'), true);
    // 点击面板切换活动面板：pointerdown（按下即切），拖选文本/点在分隔条附近也能切换，
    // 避免 click 因 mouseup 落在面板外而不触发、导致"要点两下"的观感
    $('#pane-left').addEventListener('pointerdown', (e) => { if (e.button === 0) setActivePane('left'); });
    $('#pane-right').addEventListener('pointerdown', (e) => { if (e.button === 0) setActivePane('right'); });
  }

  function onEditorWheel(e) {
    if (!e.ctrlKey) return;
    e.preventDefault();
    e.stopPropagation();
    setFontSize(state.fontSize + (e.deltaY < 0 ? 1 : -1));
  }

  function setActivePane(which) {
    if (state.active === which) return;
    state.active = which;
    // 选中框：高亮活动面板
    $('#pane-left').classList.toggle('active-pane', which === 'left');
    $('#pane-right').classList.toggle('active-pane', which === 'right');
    updateFileStatus();
    refreshTreeActive();
    // 右侧栏统一按 sidepanelPane() 刷新（分屏中若恰好一侧是代码，侧栏固定跟随代码侧，
    // 活动面板切换不改变侧栏归属）；缓存缺失时后台加载，加载完成后自动刷新 UI
    const pane = activePane();
    if (pane.currentPath && pane.currentProblem && !problemCache[pane.currentProblem]) {
      loadProblem(pane.currentPath, null, true, pane);
    }
    refreshSidepanel();
  }

  function initActivePaneVisual() {
    $('#pane-left').classList.add('active-pane');
  }

  // ---------- 分屏控制 ----------
  function enableSplit() {
    $('#split-divider').classList.remove('hidden');
    $('#pane-right').classList.remove('hidden');
    const w = parseInt(localStorage.getItem('deepcoder.split.left')) || 0;
    if (w > 100) $('#pane-left').style.flex = '0 0 ' + w + 'px';
    if (state.right.editor) state.right.editor.refresh();
    if (state.editor) state.editor.refresh();
  }

  function disableSplit() {
    $('#split-divider').classList.add('hidden');
    $('#pane-right').classList.add('hidden');
    $('#pane-right').classList.remove('md-split');
    $('#pane-left').style.flex = '';
    state.right.currentPath = null;
    state.right.dirty = false;
    state.right.mdMode = 'edit';
    cancelMdPreview(state.right);
    $('#btn-md-preview-r').textContent = '预览';
    $('#md-toolbar-right').classList.add('hidden');
    $('#pane-right').classList.remove('md-render');
    if (state.active === 'right') setActivePane('left');
    refreshTreeActive();
    updateFileStatus();
    if (state.editor) state.editor.refresh();
  }

  function setupSplitDivider() {
    const divider = $('#split-divider');
    let dragging = null;
    let raf = null, lastEv = null;
    divider.addEventListener('mousedown', (e) => {
      dragging = { startX: e.clientX, startW: $('#pane-left').getBoundingClientRect().width };
      document.body.classList.add('resizing');
      e.preventDefault();
    });
    window.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      lastEv = e;
      if (raf) return;                       // 每帧最多重排一次（mousemove 频率远高于帧率）
      raf = requestAnimationFrame(() => {
        raf = null;
        if (!dragging || !lastEv) return;
        const w = Math.min(window.innerWidth * 0.85, Math.max(160, dragging.startW + (lastEv.clientX - dragging.startX)));
        $('#pane-left').style.flex = '0 0 ' + w + 'px';
        if (state.editor) state.editor.refresh();
        if (state.right.editor) state.right.editor.refresh();
      });
    });
    window.addEventListener('mouseup', () => {
      if (!dragging) return;
      if (raf) { cancelAnimationFrame(raf); raf = null; }
      if (lastEv) {                        // 兜底应用最终宽度（若 rAF 尚未执行）
        const w = Math.min(window.innerWidth * 0.85, Math.max(160, dragging.startW + (lastEv.clientX - dragging.startX)));
        $('#pane-left').style.flex = '0 0 ' + w + 'px';
      }
      try {
        localStorage.setItem('deepcoder.split.left', String(Math.round($('#pane-left').getBoundingClientRect().width)));
      } catch (err) { /* ignore */ }
      dragging = null;
      lastEv = null;
      document.body.classList.remove('resizing');
      if (state.editor) state.editor.refresh();
      if (state.right.editor) state.right.editor.refresh();
    });
  }

function applyEditorFont() {
    document.documentElement.style.setProperty('--editor-font-size', state.fontSize + 'px');
    if (state.editor) state.editor.refresh();
    if (state.right.editor) state.right.editor.refresh();
  }

  // 程序化 setValue 时抑制 change 事件（避免打开文件被误标为未保存）
  let suppressChange = false;
  function setEditorValue(editor, text) {
    editor = editor || state.editor;
    cancelGhost(editor);            // 切换/加载文件：取消 AI ghost
    clearTimeout(ghostState.timer); ghostState.timer = null;
    suppressChange = true;
    try {
      editor.setValue(text);
    } finally {
      suppressChange = false;
    }
  }

  // ---------- 编辑器设置（主题） ----------
  function loadEditorSettings() {
    try {
      const s = JSON.parse(localStorage.getItem('deepcoder.editor') || 'null') || {};
      state.editorSettings = { theme: s.theme || 'material-darker' };
    } catch (e) {
      state.editorSettings = { theme: 'material-darker' };
    }
  }

  function applyEditorSettings() {
    const s = state.editorSettings;
    if (state.editor) {
      state.editor.setOption('theme', s.theme);
    }
    if (state.right.editor) {
      state.right.editor.setOption('theme', s.theme);
    }
    const sel = $('#cfg-theme');
    if (sel) sel.value = s.theme;
  }

  function saveEditorSettings() {
    state.editorSettings = { theme: $('#cfg-theme').value };
    try {
      localStorage.setItem('deepcoder.editor', JSON.stringify(state.editorSettings));
    } catch (e) { /* ignore */ }
    applyEditorSettings();
  }

  function setFontSize(n) {
    state.fontSize = Math.max(8, Math.min(48, n));
    localStorage.setItem('deepcoder.fontSize', String(state.fontSize));
    applyEditorFont();
  }

  // ---------- 括号匹配高亮（vendored CodeMirror 不含 matchBrackets，自行实现） ----------
  function setupBracketMatching(cm) {
    const PAIRS = { '(': ')', ')': '(', '[': ']', ']': '[', '{': '}', '}': '{' };
    const OPEN = '([{';
    const marks = [];
    let lastKey = null;   // 上次扫描的 "行:列:括号:文档版本"
    let rafId = null;

    function clearMarks() {
      while (marks.length) { marks.pop().clear(); }
    }

    function findMatch(startLine, startCol, isOpen, bracket, target) {
      let depth = 0;
      let first = true;
      let lineNo = startLine;
      // 正向扫描时携带 tokenizer 状态，跨行注释/字符串也能正确跳过
      let st = null;
      if (isOpen) {
        try {
          st = startLine > 0 ? cm.getStateAfter(startLine - 1) : null;
        } catch (e) { st = null; }
      }
      while (lineNo >= 0 && lineNo < cm.lineCount()) {
        const text = cm.getLine(lineNo);
        const skip = new Array(text.length).fill(false);
        try {
          const tokens = cm.getLineTokens(lineNo, isOpen ? st : true);
          for (const t of tokens) {
            if (/string|comment/i.test(t.type || '')) {
              for (let i = t.start; i < t.end; i++) skip[i] = true;
            }
          }
          if (isOpen && tokens.length) st = tokens[tokens.length - 1].state;
        } catch (e) { /* 忽略 tokenizer 异常 */ }
        let j = first ? (isOpen ? startCol + 1 : startCol - 1) : (isOpen ? 0 : text.length - 1);
        for (; isOpen ? j < text.length : j >= 0; j += (isOpen ? 1 : -1)) {
          if (skip[j]) continue;
          const c = text[j];
          if (c === bracket) depth++;
          else if (c === target) {
            if (depth === 0) return { line: lineNo, ch: j };
            depth--;
          }
        }
        first = false;
        lineNo += (isOpen ? 1 : -1);
      }
      return null;
    }

    function highlight() {
      try {
        const gen = cm.changeGeneration();
        const cur = cm.getCursor();
        const line = cm.getLine(cur.line);
        if (!line) { clearMarks(); return; }
        // 先做 O(1) 的括号相邻检测，命中才进行全文扫描
        let idx = -1;
        let bracket = null;
        for (const cand of [[cur.ch, line[cur.ch]], [cur.ch - 1, line[cur.ch - 1]]]) {
          if (cand[1] && PAIRS[cand[1]]) { idx = cand[0]; bracket = cand[1]; break; }
        }
        const key = cur.line + ':' + idx + ':' + bracket + ':' + gen;
        if (key === lastKey) return;   // 位置与文档都没变，跳过
        lastKey = key;
        clearMarks();
        if (bracket === null) return;
        if (cm.getValue().length > 500000) return; // 超大文件跳过
        const isOpen = OPEN.indexOf(bracket) >= 0;
        const match = findMatch(cur.line, idx, isOpen, bracket, PAIRS[bracket]);
        marks.push(cm.markText(
          { line: cur.line, ch: idx }, { line: cur.line, ch: idx + 1 },
          { className: match ? 'CodeMirror-matchingbracket' : 'CodeMirror-nonmatchingbracket' }
        ));
        if (match) {
          marks.push(cm.markText(
            { line: match.line, ch: match.ch }, { line: match.line, ch: match.ch + 1 },
            { className: 'CodeMirror-matchingbracket' }
          ));
        }
      } catch (e) { /* ignore */ }
    }

    function schedule() {
      if (rafId) return;
      rafId = requestAnimationFrame(() => { rafId = null; highlight(); });
    }

    cm.on('cursorActivity', schedule);
    cm.on('changes', schedule);
  }

  function updateFileStatus() {
    const pane = activePane();
    $('#cur-file').textContent = pane.currentPath || '（未打开文件）';
    $('#dirty-mark').classList.toggle('hidden', !pane.dirty);
  }

  // 文件树高亮：两个面板各自标记当前打开的文件；活动面板的文件额外加选中框样式
  function refreshTreeActive() {
    const actPath = activePath();
    document.querySelectorAll('.tree-item.file').forEach(t => {
      const p = t.dataset.path;
      const act = (state.currentPath && state.currentPath === p) ||
                  (state.right.currentPath && state.right.currentPath === p);
      t.classList.toggle('active', act);
      t.classList.toggle('active-pane-file', !!actPath && actPath === p);
    });
  }

  const BINARY_VIEW_EXT = ['.pdf', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp'];
  function viewableBinary(path) {
    const l = path.toLowerCase();
    return BINARY_VIEW_EXT.some(e => l.endsWith(e));
  }

  // ---------- 单文件打开（切换前静默自动保存） ----------
  function saveBeforeSwitch(pane) {
    pane = pane || activePane();
    if (pane.dirty && pane.currentPath && !viewableBinary(pane.currentPath)) {
      api('PUT', '/api/file', { path: pane.currentPath, content: pane.editor.getValue() });
      pane.dirty = false;
    }
  }

  async function savePane(pane) {
    if (!pane.currentPath || viewableBinary(pane.currentPath)) return;
    if (pane.dirty) {
      const cm = pane.editor;
      cancelGhost(cm);            // 保存前取消 AI ghost，避免把灰色提示文本写入文件
      const r = await api('PUT', '/api/file', { path: pane.currentPath, content: cm.getValue() });
      if (!r.error) pane.dirty = false;
    }
    updateFileStatus();
  }

  // Ctrl+S：保存活动面板
  function saveCurrent() { return savePane(activePane()); }

  function showBinaryViewer(path, pane) {
    const isRight = pane === state.right;
    pane.currentPath = path;
    pane.dirty = false;
    pane.currentProblem = null;
    pane.config = { io: defaultIoFor(path), time_limit: 2000, memory_limit: 256 };
    refreshSidepanel();  // 非代码文件（PDF/图片等）：无论打开到哪个面板都按分屏规则刷新侧栏
    updateFileStatus();
    refreshTreeActive();
    pane.mdMode = 'edit';
    cancelMdPreview(pane);
    const editorEl = isRight ? $('#editor-right') : $('#editor');
    const container = isRight ? $('#binary-viewer-right') : $('#binary-viewer');
    const paneEl = isRight ? $('#pane-right') : $('#pane-left');
    paneEl.classList.remove('md-split');
    paneEl.classList.remove('md-render');
    (isRight ? $('#btn-md-preview-r') : $('#btn-md-preview')).textContent = '预览';
    (isRight ? $('#md-toolbar-right') : $('#md-toolbar')).classList.add('hidden');
    editorEl.classList.add('hidden');
    container.classList.remove('hidden');
    const isPdf = path.toLowerCase().endsWith('.pdf');
    container.classList.toggle('pdf', isPdf);
    if (isPdf) {
      const frame = isRight ? $('#pdf-frame-right') : $('#pdf-frame');
      frame.classList.remove('hidden');
      if (frame.dataset.pdf !== path) {
        frame.src = '/api/raw?path=' + encodeURIComponent(path);
        frame.dataset.pdf = path;
      }
    } else {
      (isRight ? $('#pdf-frame-right') : $('#pdf-frame')).classList.add('hidden');
      loadImageFor(path, isRight ? $('#img-view-right') : $('#img-view'),
                   isRight ? $('#zoom-label-r') : $('#zoom-label'));
    }
  }

  function showCodeFile(path, r, pane) {
    const isRight = pane === state.right;
    const editor = pane.editor;
    const editorEl = isRight ? $('#editor-right') : $('#editor');
    const binEl = isRight ? $('#binary-viewer-right') : $('#binary-viewer');
    editorEl.classList.remove('hidden');
    binEl.classList.add('hidden');
    pane.currentPath = path;
    setEditorValue(editor, r.content);
    editor.refresh();
    pane.dirty = false;
    updateFileStatus();
    refreshTreeActive();
    pane.mdMode = 'edit';
    cancelMdPreview(pane);
    const paneEl = isRight ? $('#pane-right') : $('#pane-left');
    paneEl.classList.remove('md-split');
    paneEl.classList.remove('md-render');
    (isRight ? $('#btn-md-preview-r') : $('#btn-md-preview')).textContent = '预览';
    const isMd = path.toLowerCase().endsWith('.md');
    editor.setOption('mode', modeForPath(path));
    (isRight ? $('#md-toolbar-right') : $('#md-toolbar')).classList.toggle('hidden', !isMd);
    const base = path.split('/').pop();
    const isCode = isCodeExt(path);
    if (isCode && !base.startsWith('.default.')) {
      pane.currentProblem = base.replace(/\.[^.]+$/, '');
      loadProblem(path, r, false, pane);
    } else {
      pane.currentProblem = null;
      pane.config = { io: defaultIoFor(path), time_limit: 2000, memory_limit: 256 };
    }
    // 无论打开到哪个面板都刷新侧栏：分屏中另一侧文件变化可能改变侧栏归属
    // （如恰好一侧代码 → 两侧都是/都不是代码）
    refreshSidepanel();
  }

  function openInPane(path, paneId, silent) {
    const pane = paneId === 'right' ? state.right : state;
    // 目标面板已打开该文件：若活动面板是别的面板则聚焦目标面板（不做重复打开）
    if (pane.currentPath === path) {
      if (state.active !== paneId) setActivePane(paneId);
      return;
    }
    // 同一文件不允许在两个面板同时打开：聚焦已打开的面板
    const other = pane === state.right ? state : state.right;
    if (other.currentPath === path) {
      setActivePane(pane === state.right ? 'left' : 'right');
      return;
    }
    if (paneId === 'right' && !isSplit()) enableSplit();
    saveBeforeSwitch(pane);
    if (viewableBinary(path)) {
      showBinaryViewer(path, pane);
      return;
    }
    return api('GET', '/api/file?path=' + encodeURIComponent(path)).then(r => {
      if (r.error) { if (!silent) alert(r.error); return; }
      showCodeFile(path, r, pane);
    });
  }

  async function openFile(path, silent) {
    return openInPane(path, state.active, silent);
  }

  function clearRightPane() {
    state.right.currentPath = null;
    state.right.currentProblem = null;
    state.right.dirty = false;
    state.right.mdMode = 'edit';
    cancelMdPreview(state.right);
    $('#pane-right').classList.remove('md-split');
    $('#pane-right').classList.remove('md-render');
    $('#btn-md-preview-r').textContent = '预览';
    $('#md-toolbar-right').classList.add('hidden');
    setEditorValue(state.right.editor, '');
    state.right.editor.setOption('mode', 'text/plain');
    refreshTreeActive();
    updateFileStatus();
    refreshSidepanel();
  }

  function clearEditorArea() {
    state.currentPath = null;
    state.currentProblem = null;
    state.dirty = false;
    state.mdMode = 'edit';
    cancelMdPreview(state);
    $('#pane-left').classList.remove('md-split');
    $('#pane-left').classList.remove('md-render');
    $('#btn-md-preview').textContent = '预览';
    setEditorValue(state.editor, '');
    state.editor.setOption('mode', 'text/plain');
    $('#md-toolbar').classList.add('hidden');
    $('#binary-viewer').classList.add('hidden');
    $('#editor').classList.remove('hidden');
    updateFileStatus();
    refreshSidepanel();
  }

  // ---------- 图片查看器（居中 + 自由缩放/拖拽） ----------
  const imgViewer = { scale: 1, tx: 0, ty: 0, dragging: false };

  function setupImageViewer() {
    const viewport = $('#img-viewport');
    const imgEl = $('#img-view');
    const label = $('#zoom-label');

    function apply() {
      imgEl.style.left = imgViewer.tx + 'px';
      imgEl.style.top = imgViewer.ty + 'px';
      imgEl.style.width = (imgEl.naturalWidth * imgViewer.scale) + 'px';
      imgEl.style.height = (imgEl.naturalHeight * imgViewer.scale) + 'px';
      label.textContent = Math.round(imgViewer.scale * 100) + '%';
    }

    function fit() {
      const iw = imgEl.naturalWidth, ih = imgEl.naturalHeight;
      if (!iw || !ih) return;
      imgViewer.scale = Math.min(viewport.clientWidth / iw, viewport.clientHeight / ih);
      imgViewer.tx = (viewport.clientWidth - iw * imgViewer.scale) / 2;
      imgViewer.ty = (viewport.clientHeight - ih * imgViewer.scale) / 2;
      apply();
    }

    function reset() {
      const iw = imgEl.naturalWidth, ih = imgEl.naturalHeight;
      if (!iw || !ih) return;
      imgViewer.scale = 1;
      imgViewer.tx = (viewport.clientWidth - iw) / 2;
      imgViewer.ty = (viewport.clientHeight - ih) / 2;
      apply();
    }

    function zoomAt(cx, cy, factor) {
      const ns = Math.min(20, Math.max(0.05, imgViewer.scale * factor));
      const r = ns / imgViewer.scale;
      imgViewer.tx = cx - (cx - imgViewer.tx) * r;
      imgViewer.ty = cy - (cy - imgViewer.ty) * r;
      imgViewer.scale = ns;
      apply();
    }

    viewport.addEventListener('wheel', (e) => {
      if (!imgEl.naturalWidth) return;
      e.preventDefault();
      const rect = viewport.getBoundingClientRect();
      zoomAt(e.clientX - rect.left, e.clientY - rect.top, e.deltaY < 0 ? 1.15 : 1 / 1.15);
    }, { passive: false });

    viewport.addEventListener('mousedown', (e) => {
      if (e.button !== 0 || !imgEl.naturalWidth) return;
      imgViewer.dragging = { x: e.clientX, y: e.clientY, tx: imgViewer.tx, ty: imgViewer.ty };
      viewport.classList.add('grabbing');
      e.preventDefault();
    });
    window.addEventListener('mousemove', (e) => {
      if (!imgViewer.dragging) return;
      imgViewer.tx = imgViewer.dragging.tx + (e.clientX - imgViewer.dragging.x);
      imgViewer.ty = imgViewer.dragging.ty + (e.clientY - imgViewer.dragging.y);
      apply();
    });
    window.addEventListener('mouseup', () => {
      imgViewer.dragging = false;
      viewport.classList.remove('grabbing');
    });

    $('#btn-zoom-in').addEventListener('click', () => {
      const rect = viewport.getBoundingClientRect();
      zoomAt(rect.width / 2, rect.height / 2, 1.25);
    });
    $('#btn-zoom-out').addEventListener('click', () => {
      const rect = viewport.getBoundingClientRect();
      zoomAt(rect.width / 2, rect.height / 2, 1 / 1.25);
    });
    $('#btn-zoom-fit').addEventListener('click', fit);
    $('#btn-zoom-reset').addEventListener('click', reset);

    imgEl.addEventListener('load', fit);
    imgEl.addEventListener('error', () => { label.textContent = '加载失败'; });
    viewerFitFn = fit;
    return { fit: fit, reset: reset };
  }

  // 右侧面板图片查看器（独立缩放状态）
  function setupRightImageViewer() {
    const viewport = $('#img-viewport-right');
    const imgEl = $('#img-view-right');
    const label = $('#zoom-label-r');
    const st = { scale: 1, tx: 0, ty: 0, dragging: false };

    function apply() {
      imgEl.style.left = st.tx + 'px';
      imgEl.style.top = st.ty + 'px';
      imgEl.style.width = (imgEl.naturalWidth * st.scale) + 'px';
      imgEl.style.height = (imgEl.naturalHeight * st.scale) + 'px';
      label.textContent = Math.round(st.scale * 100) + '%';
    }
    function fit() {
      const iw = imgEl.naturalWidth, ih = imgEl.naturalHeight;
      if (!iw || !ih) return;
      st.scale = Math.min(viewport.clientWidth / iw, viewport.clientHeight / ih);
      st.tx = (viewport.clientWidth - iw * st.scale) / 2;
      st.ty = (viewport.clientHeight - ih * st.scale) / 2;
      apply();
    }
    function reset() {
      const iw = imgEl.naturalWidth, ih = imgEl.naturalHeight;
      if (!iw || !ih) return;
      st.scale = 1;
      st.tx = (viewport.clientWidth - iw) / 2;
      st.ty = (viewport.clientHeight - ih) / 2;
      apply();
    }
    function zoomAt(cx, cy, factor) {
      const ns = Math.min(20, Math.max(0.05, st.scale * factor));
      const r = ns / st.scale;
      st.tx = cx - (cx - st.tx) * r;
      st.ty = cy - (cy - st.ty) * r;
      st.scale = ns;
      apply();
    }
    viewport.addEventListener('wheel', (e) => {
      if (!imgEl.naturalWidth) return;
      e.preventDefault();
      const rect = viewport.getBoundingClientRect();
      zoomAt(e.clientX - rect.left, e.clientY - rect.top, e.deltaY < 0 ? 1.15 : 1 / 1.15);
    }, { passive: false });
    viewport.addEventListener('mousedown', (e) => {
      if (e.button !== 0 || !imgEl.naturalWidth) return;
      st.dragging = { x: e.clientX, y: e.clientY, tx: st.tx, ty: st.ty };
      viewport.classList.add('grabbing');
      e.preventDefault();
    });
    window.addEventListener('mousemove', (e) => {
      if (!st.dragging) return;
      st.tx = st.dragging.tx + (e.clientX - st.dragging.x);
      st.ty = st.dragging.ty + (e.clientY - st.dragging.y);
      apply();
    });
    window.addEventListener('mouseup', () => {
      st.dragging = false;
      viewport.classList.remove('grabbing');
    });
    $('#btn-zoom-in-r').addEventListener('click', () => {
      const rect = viewport.getBoundingClientRect();
      zoomAt(rect.width / 2, rect.height / 2, 1.25);
    });
    $('#btn-zoom-out-r').addEventListener('click', () => {
      const rect = viewport.getBoundingClientRect();
      zoomAt(rect.width / 2, rect.height / 2, 1 / 1.25);
    });
    $('#btn-zoom-fit-r').addEventListener('click', fit);
    $('#btn-zoom-reset-r').addEventListener('click', reset);
    imgEl.addEventListener('load', fit);
    imgEl.addEventListener('error', () => { label.textContent = '加载失败'; });
  }

  // 图片缓存：Blob 对象 URL，切换标签不再重复下载（LRU 上限 20）
  const imgCache = new Map(); // path -> objectURL
  let viewerFitFn = null;

  function loadImageFor(path, img, labelEl) {
    img.dataset.path = path;
    const cached = imgCache.get(path);
    if (cached) {
      imgCache.delete(path);
      imgCache.set(path, cached); // 刷新 LRU 顺序
      img.src = cached;
      if (img.complete && viewerFitFn) viewerFitFn();
      return;
    }
    fetch('/api/raw?path=' + encodeURIComponent(path))
      .then(r => { if (!r.ok) throw new Error(String(r.status)); return r.blob(); })
      .then(blob => {
        if (img.dataset.path !== path) return; // 期间已切到别的图
        const url = URL.createObjectURL(blob);
        imgCache.set(path, url);
        while (imgCache.size > 10) {
          const k = imgCache.keys().next().value;
          URL.revokeObjectURL(imgCache.get(k));
          imgCache.delete(k);
        }
        img.src = url;
      })
      .catch(() => { if (labelEl) labelEl.textContent = '加载失败'; });
  }

  // ---------- Markdown 预览 ----------
  const mdRenderer = window.markdownit({ html: false, breaks: true });
  function renderMarkdown(text) {
    text = text || (state.editor ? state.editor.getValue() : '');
    const md = mdRenderer;
    const math = [];
    let processed = text.replace(/\$\$([\s\S]+?)\$\$/g, (m, f) => {
      math.push({ formula: f, display: true });
      return '@@MATH' + (math.length - 1) + '@@';
    });
    processed = processed.replace(/\$([^\s$][^\$\n]*?)\$/g, (m, f) => {
      math.push({ formula: f, display: false });
      return '@@MATH' + (math.length - 1) + '@@';
    });
    let html = md.render(processed);
    html = html.replace(/@@MATH(\d+)@@/g, (m, id) => {
      const b = math[parseInt(id)];
      try {
        return katex.renderToString(b.formula, { displayMode: b.display, throwOnError: false });
      } catch (e) {
        return '<code>' + b.formula + '</code>';
      }
    });
    return html;
  }

  // 更新指定面板的 md 预览（保留滚动位置，避免打字时跳回顶部）
  function updateMdPreview(pane) {
    pane = pane || state;
    const isRight = pane === state.right;
    const previewEl = isRight ? $('#md-preview-right') : $('#md-preview');
    const st = previewEl.scrollTop;
    previewEl.innerHTML = renderMarkdown(pane.editor ? pane.editor.getValue() : '');
    previewEl.scrollTop = st;
  }

  // 防抖：输入时最多每 150ms 渲染一次预览（避免每次按键全量重渲染导致卡顿）；
  // 打开预览时由 applyMdMode 立即渲染
  function scheduleMdPreview(pane) {
    if (pane._mdTimer) clearTimeout(pane._mdTimer);
    pane._mdTimer = setTimeout(() => {
      pane._mdTimer = null;
      if (pane.mdMode === 'split') updateMdPreview(pane);
    }, 150);
  }

  function cancelMdPreview(pane) {
    if (pane._mdTimer) { clearTimeout(pane._mdTimer); pane._mdTimer = null; }
  }

  // 代码区/预览区分隔比例（预览区左侧边界占面板宽度的百分比，15~85）
  const MD_SPLIT_KEY = { left: 'deepcoder.md.left', right: 'deepcoder.md.right' };
  function applyMdSplit(paneId) {
    let pct = parseFloat(localStorage.getItem(MD_SPLIT_KEY[paneId]));
    if (!(pct >= 15 && pct <= 85)) pct = 50;
    (paneId === 'left' ? $('#pane-left') : $('#pane-right')).style.setProperty('--md-split', pct + '%');
    return pct;
  }

  // Markdown 三态：'edit' 只编辑 | 'split' 边编辑边渲染 | 'render' 只显示渲染结果。
  // 按钮循环切换：编辑态显示「预览」→ 分屏态显示「只读」→ 只读态显示「编辑」
  const MD_MODES = ['edit', 'split', 'render'];
  const MD_NEXT_BTN = { edit: '预览', split: '只读', render: '编辑' };
  function applyMdMode(pane, paneId, mode) {
    const isRight = paneId === 'right';
    const paneEl = isRight ? $('#pane-right') : $('#pane-left');
    const btn = isRight ? $('#btn-md-preview-r') : $('#btn-md-preview');
    pane.mdMode = mode;
    paneEl.classList.toggle('md-split', mode === 'split');
    paneEl.classList.toggle('md-render', mode === 'render');
    btn.textContent = MD_NEXT_BTN[mode];
    if (mode === 'split') {
      applyMdSplit(paneId);
      updateMdPreview(pane);
    } else if (mode === 'render') {
      updateMdPreview(pane);
    } else {
      cancelMdPreview(pane);
    }
    // 类切换会改变编辑器尺寸/可见性，延迟一帧再刷新，避免 display:none 期间 refresh 计算错位
    if (pane.editor) setTimeout(() => pane.editor.refresh(), 0);
  }
  function cycleMdModeFor(paneId) {
    const pane = paneId === 'right' ? state.right : state;
    const cur = MD_MODES.includes(pane.mdMode) ? pane.mdMode : 'edit';
    const next = MD_MODES[(MD_MODES.indexOf(cur) + 1) % MD_MODES.length];
    applyMdMode(pane, paneId, next);
  }

  // 拖动代码区/预览区分隔条（每个面板独立，比例持久化；rAF 节流避免每事件重排）
  function setupMdDivider() {
    [
      ['left', '#md-divider', '#pane-left', () => state.editor],
      ['right', '#md-divider-right', '#pane-right', () => state.right.editor]
    ].forEach(([paneId, sel, paneSel, editorOf]) => {
      const divider = $(sel);
      let dragging = null;
      let raf = null, lastEv = null;
      divider.addEventListener('mousedown', (e) => {
        const paneEl = $(paneSel);
        const w = paneEl.getBoundingClientRect().width;
        if (!w) return;
        dragging = { startX: e.clientX, lastX: e.clientX, startW: w, startPct: applyMdSplit(paneId) };
        document.body.classList.add('resizing');
        e.preventDefault();
      });
      window.addEventListener('mousemove', (e) => {
        if (!dragging) return;
        lastEv = e;
        if (raf) return;                     // 每帧最多重排一次
        raf = requestAnimationFrame(() => {
          raf = null;
          if (!dragging || !lastEv) return;
          dragging.lastX = lastEv.clientX;
          const pct = Math.min(85, Math.max(15, dragging.startPct + (lastEv.clientX - dragging.startX) / dragging.startW * 100));
          $(paneSel).style.setProperty('--md-split', pct + '%');
          const ed = editorOf();
          if (ed) ed.refresh();
        });
      });
      window.addEventListener('mouseup', () => {
        if (!dragging) return;
        if (raf) { cancelAnimationFrame(raf); raf = null; }
        if (lastEv) dragging.lastX = lastEv.clientX;
        const pct = Math.min(85, Math.max(15, dragging.startPct + (dragging.lastX - dragging.startX) / dragging.startW * 100));
        $(paneSel).style.setProperty('--md-split', pct + '%');   // 兜底应用最终位置
        try { localStorage.setItem(MD_SPLIT_KEY[paneId], String(Math.round(pct))); } catch (err) { /* ignore */ }
        dragging = null;
        lastEv = null;
        document.body.classList.remove('resizing');
        const ed = editorOf();
        if (ed) ed.refresh();
      });
    });
  }

  // ---------- 题目配置（缓存，避免切标签反复请求） ----------
  const problemCache = {}; // 题目名 -> {config, samples}

  async function loadProblem(path, r, force, pane) {
    pane = pane || activePane();
    const key = pane.currentProblem;
    const isAct = pane === sidepanelPane();
    if (!force && key && problemCache[key]) {
      pane.config = problemCache[key].config;
      if (isAct) { updateConfigPanel(); renderSamples(problemCache[key].samples, pane); }
      return;
    }
    if (!r) r = await api('GET', '/api/file?path=' + encodeURIComponent(path));
    // 默认配置按语言：C/C++ 默认 file（模板含 freopen），Python/Java 默认 stdio；已保存的题目配置优先
    pane.config = Object.assign({ io: defaultIoFor(path), time_limit: 2000, memory_limit: 256 }, r.config || {});
    const samples = r.samples || [];
    pane.samples = samples;
    if (key) problemCache[key] = { config: pane.config, samples: samples };
    if (isAct) { updateConfigPanel(); renderSamples(samples, pane); }
  }

  function updateConfigPanel(config, pane) {
    pane = pane || sidepanelPane() || activePane();
    config = config || pane.config || {};
    $('#io-select').value = config.io || defaultIoFor(pane.currentPath);
    $('#cfg-time').value = config.time_limit || 2000;
    $('#cfg-mem').value = config.memory_limit || 256;
    $('#cfg-spj').value = config.spj || '';
  }

  async function saveConfig(pane) {
    // 事件监听器会把 Event 对象作为首参传入，需过滤；
    // 无参调用默认保存到侧栏归属面板（分屏中侧栏可能固定显示非活动面板的配置）
    if (!pane || pane instanceof Event) pane = sidepanelPane() || activePane();
    if (!pane.currentProblem) return;
    pane.config = {
      io: $('#io-select').value,
      time_limit: parseInt($('#cfg-time').value) || 2000,
      memory_limit: parseInt($('#cfg-mem').value) || 256,
      spj: $('#cfg-spj').value.trim(),
    };
    const cached = problemCache[pane.currentProblem];
    if (cached) cached.config = pane.config;
    await api('POST', '/api/config', { name: pane.currentProblem, patch: pane.config });
  }

  // ---------- 编译配置 ----------
  async function loadCompileConfig() {
    const r = await api('GET', '/api/config');
    if (r.error || !r.config) return;
    if (r.config.compile) Object.assign(state.compile, r.config.compile);
    updateCompilePanel();
  }

  function updateCompilePanel(lang) {
    lang = lang || 'cpp';
    $('#cfg-compiler').value = state.compile.compiler || '';
    $('#cfg-flags').value = state.compile.flags || '';
    $('#cfg-python').value = state.compile.python || '';
    $('#cfg-javac').value = state.compile.javac || '';
    $('#cfg-java').value = state.compile.java || '';
    $('#cc-cpp').classList.toggle('hidden', lang !== 'cpp');
    $('#cc-python').classList.toggle('hidden', lang !== 'python');
    $('#cc-java').classList.toggle('hidden', lang !== 'java');
  }

  async function saveCompileConfig() {
    state.compile = {
      compiler: $('#cfg-compiler').value.trim(),
      flags: $('#cfg-flags').value.trim(),
      python: $('#cfg-python').value.trim(),
      javac: $('#cfg-javac').value.trim(),
      java: $('#cfg-java').value.trim(),
    };
    await api('POST', '/api/config', { name: '__compile__', patch: state.compile });
  }

  // ---------- 样例面板 ----------
  const BIG_SAMPLE = 200 * 1024;  // 超过 200KB 视为大样例，不加载内容到 UI

  function fmtSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1024 / 1024).toFixed(1) + ' MB';
  }

  // 样例渲染串行化 + 仅渲染侧栏归属面板：防止切换面板时并发渲染，
  // 旧的（左侧面板）样例在 await 返回后被追加到新（右侧面板）列表后面
  let samplesRenderChain = Promise.resolve();
  function renderSamples(samples, pane) {
    pane = pane || activePane();
    const run = async () => {
      if (pane !== sidepanelPane()) return;   // 侧栏已归属别的面板（或隐藏）：本次渲染作废
      const box = $('#samples');
      box.innerHTML = '';
      pane.samples = [];
      if (!pane.currentProblem) {
        box.appendChild(el('div', 'hint', '打开代码文件后显示样例'));
        return;
      }
      const manual = getManualSamples(pane);
      if (samples.length === 0 && manual.length === 0) {
        box.appendChild(el('div', 'hint', '还没有样例，点「＋ 样例」或「导入」添加，或把 .in/.out 文件拖入左侧文件树'));
        return;
      }
      // 并行构建，避免样例多时串行请求拖慢
      const built = await Promise.all(samples.map(s => buildSample(s, pane)));
      if (pane !== sidepanelPane()) return;   // await 期间又切走：丢弃，不 append（防样例混叠）
      for (const w of built) box.appendChild(w);
      for (const m of manual) box.appendChild(buildManualSample(m, pane));
    };
    samplesRenderChain = samplesRenderChain.then(run);
    return samplesRenderChain;
  }

  function getManualSamples(pane) {
    pane = pane || activePane();
    try {
      const arr = JSON.parse(localStorage.getItem('deepcoder.samples.' + pane.currentProblem) || '[]');
      return Array.isArray(arr) ? arr : [];
    } catch (e) { return []; }
  }

  function setManualSamples(arr, pane) {
    pane = pane || activePane();
    localStorage.setItem('deepcoder.samples.' + pane.currentProblem, JSON.stringify(arr));
  }

  function buildSample(s, pane) {
    pane = pane || activePane();
    const dir = parentDir(pane.currentPath);
    const base = (dir ? dir + '/' : '') + (s.subdir ? s.subdir + '/' : '');
    const inPath = base + s.in;
    const outPath = base + s.out;

    const wrap = el('div', 'sample');
    const head = el('div', 'sample-head');
    head.appendChild(el('span', 'name', s.label));
    const sizeEl = el('span', 'size', '…');
    head.appendChild(sizeEl);
    const verdict = el('span', 'verdict', '');
    head.appendChild(verdict);

    const entry = { type: 'file', label: s.label, in: s.in, out: s.out, subdir: s.subdir || '', wrap: wrap, verdictEl: verdict, viewBtn: null, result: null, pane: pane };
    pane.samples.push(entry);

    const btnRun = el('button', 'btn mini primary', '▶');
    btnRun.title = '运行此样例';
    btnRun.addEventListener('click', (e) => { e.stopPropagation(); runSample(entry); });
    head.appendChild(btnRun);

    const btnView = el('button', 'btn mini hidden', '查看');
    btnView.title = '查看本次运行输出';
    btnView.addEventListener('click', (e) => { e.stopPropagation(); showSampleResultModal(entry); });
    entry.viewBtn = btnView;
    head.appendChild(btnView);

    const btnDel = el('button', 'btn mini', '✕');
    btnDel.title = '删除此样例';
    btnDel.addEventListener('click', (e) => { e.stopPropagation(); deleteSample(s); });
    head.appendChild(btnDel);

    let bodyBuilt = false;
    head.addEventListener('click', () => {
      wrap.classList.toggle('open');
      if (!bodyBuilt && wrap.classList.contains('open')) {
        // 懒加载：首次展开才请求并渲染样例内容
        bodyBuilt = true;
        buildSampleBody(wrap, s, inPath, outPath, sizeEl);
      }
    });
    wrap.appendChild(head);

    restoreSampleResult(entry);
    return wrap;
  }

  async function buildSampleBody(wrap, s, inPath, outPath, sizeEl) {
    const inData = await api('GET', '/api/file?path=' + encodeURIComponent(inPath) + '&max_size=' + BIG_SAMPLE);
    const outData = await api('GET', '/api/file?path=' + encodeURIComponent(outPath) + '&max_size=' + BIG_SAMPLE);
    sizeEl.textContent = fmtSize(inData.size || 0);
    const body = el('div', 'sample-body');
    if (inData.truncated || outData.truncated) {
      body.appendChild(el('div', 'hint', '大样例（' + fmtSize(inData.size || 0) + ' / ' + fmtSize(outData.size || 0) + '），内容不显示，可直接运行'));
    } else {
      body.appendChild(el('h5', '', '输入 (' + s.in + ')'));
      const taIn = el('textarea', '');
      taIn.value = inData.content || '';
      taIn.rows = 3;
      body.appendChild(taIn);

      body.appendChild(el('h5', '', '期望输出 (' + s.out + ')'));
      const taOut = el('textarea', '');
      taOut.value = outData.content || '';
      taOut.rows = 3;
      body.appendChild(taOut);

      taIn.addEventListener('change', () => api('PUT', '/api/file', { path: inPath, content: taIn.value }));
      taOut.addEventListener('change', () => api('PUT', '/api/file', { path: outPath, content: taOut.value }));
    }
    wrap.appendChild(body);
  }

  function buildManualSample(m, pane) {
    pane = pane || activePane();
    m.pane = pane;
    const wrap = el('div', 'sample');
    const head = el('div', 'sample-head');
    head.appendChild(el('span', 'name', m.label));
    head.appendChild(el('span', 'size', '缓存'));
    const verdict = el('span', 'verdict', '');
    head.appendChild(verdict);

    m.type = 'manual';
    m.wrap = wrap;
    m.verdictEl = verdict;
    m.viewBtn = null;
    pane.samples.push(m);

    const btnRun = el('button', 'btn mini primary', '▶');
    btnRun.title = '运行此样例';
    btnRun.addEventListener('click', (e) => { e.stopPropagation(); runSample(m); });
    head.appendChild(btnRun);

    const btnView = el('button', 'btn mini hidden', '查看');
    btnView.title = '查看本次运行输出';
    btnView.addEventListener('click', (e) => { e.stopPropagation(); showSampleResultModal(m); });
    m.viewBtn = btnView;
    head.appendChild(btnView);

    const btnDel = el('button', 'btn mini', '✕');
    btnDel.title = '删除此样例';
    btnDel.addEventListener('click', (e) => { e.stopPropagation(); deleteSample(m); });
    head.appendChild(btnDel);

    head.addEventListener('click', () => wrap.classList.toggle('open'));
    wrap.appendChild(head);

    const body = el('div', 'sample-body');
    body.appendChild(el('h5', '', '输入'));
    const taIn = el('textarea', '');
    taIn.value = m.in;
    taIn.rows = 3;
    body.appendChild(taIn);
    body.appendChild(el('h5', '', '期望输出'));
    const taOut = el('textarea', '');
    taOut.value = m.out;
    taOut.rows = 3;
    body.appendChild(taOut);
    wrap.appendChild(body);

    const persist = () => {
      m.in = taIn.value;
      m.out = taOut.value;
      const arr = getManualSamples();
      const item = arr.find(x => x.label === m.label);
      if (item) { item.in = m.in; item.out = m.out; }
      setManualSamples(arr);
    };
    taIn.addEventListener('change', persist);
    taOut.addEventListener('change', persist);

    restoreSampleResult(m);
    return wrap;
  }

  // ---------- 样例结果（会话内缓存） ----------
  const sampleCache = {}; // problem -> {label -> {v, r}}

  function restoreSampleResult(entry) {
    const key = (entry.pane || activePane()).currentProblem;
    const bucket = sampleCache[key];
    const cached = bucket && bucket[entry.label];
    if (cached) applySampleResult(entry, cached.v, cached.r);
  }

  function applySampleResult(entry, v, r) {
    entry.result = r;
    // 没有期望输出时：运行成功显示 OK，失败显示状态码
    const noExpect = r && (r.expected === undefined || r.expected === null || String(r.expected).trim() === '');
    const label = (noExpect && v === 'AC') ? 'OK' : v;
    entry.verdictEl.textContent = label + (r && r.time !== undefined ? ` (${r.time}ms)` : '');
    entry.verdictEl.className = 'verdict ' + v;
    entry.viewBtn.classList.remove('hidden');
    const key = (entry.pane || activePane()).currentProblem;
    if (!sampleCache[key]) sampleCache[key] = {};
    sampleCache[key][entry.label] = { v: label, r: r };
  }

  function diffLines(a, b) {
    const CAP = 800;
    const norm = (s) => String(s).replace(/\r$/, '');
    const A = a.map(norm).slice(0, CAP), B = b.map(norm).slice(0, CAP);
    const n = A.length, m = B.length;
    const dp = new Int32Array((n + 1) * (m + 1));
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        dp[i * (m + 1) + j] = A[i] === B[j]
          ? dp[(i + 1) * (m + 1) + j + 1] + 1
          : Math.max(dp[(i + 1) * (m + 1) + j], dp[i * (m + 1) + j + 1]);
      }
    }
    const out = [];
    let i = 0, j = 0;
    while (i < n && j < m) {
      if (A[i] === B[j]) { out.push({ t: 'same', x: A[i] }); i++; j++; }
      else if (dp[(i + 1) * (m + 1) + j] >= dp[i * (m + 1) + j + 1]) { out.push({ t: 'del', x: A[i] }); i++; }
      else { out.push({ t: 'ins', x: B[j] }); j++; }
    }
    while (i < n) { out.push({ t: 'del', x: A[i] }); i++; }
    while (j < m) { out.push({ t: 'ins', x: B[j] }); j++; }
    return out;
  }

  function buildSampleResultHtml(label, r) {
    const ERR_MSG = {
      TLE: '运行超时（Time Limit Exceeded）',
      MLE: '内存超限（Memory Limit Exceeded）',
      OLE: '输出超限（Output Limit Exceeded）',
      RE: '运行时错误（Runtime Error）',
      CE: '编译失败（Compile Error）',
    };
    const v = r.verdict || r.status || '';
    let html = '<div class="res-head">【' + esc(label) + '】<b class="verdict ' + esc(v) + '"> ' + esc(v) + '</b>';
    if (r.time !== undefined) html += '（' + r.time + ' ms，' + r.memory + ' MB）';
    html += '</div>';
    if (r.status && ERR_MSG[r.status]) {
      let msg = ERR_MSG[r.status];
      if (r.status === 'RE' && r.exit_code) {
        msg += '，退出码 ' + r.exit_code;
        if (r.exit_hint) msg += '（' + r.exit_hint + '）';
      }
      html += '<div class="res-err">✘ ' + esc(msg) + '</div>';
    }
    const hasExpected = r.expected !== undefined && r.expected !== null && String(r.expected).trim() !== '';
    const hasDiff = hasExpected && r.stdout !== undefined && r.stdout !== r.expected;
    if (hasDiff) {
      html += '<div class="hint">对比：<span class="diff-legend del">− 程序输出</span> <span class="diff-legend ins">＋ 期望输出</span></div>';
      html += '<div class="diff-box">';
      const diffs = diffLines(r.stdout.split('\n'), r.expected.split('\n'));
      const MAX = 400;
      let shown = 0, first = true;
      for (const d of diffs) {
        if (shown >= MAX) { html += '<div class="diff-line more">…（差异行过多，已截断）</div>'; break; }
        let cls = d.t;
        if (first && d.t !== 'same') { cls += ' first'; first = false; }
        html += '<div class="diff-line ' + cls + '">' + (d.t === 'same' ? ' ' : (d.t === 'del' ? '−' : '＋')) + ' ' + esc(d.x) + '</div>';
        shown++;
      }
      html += '</div>';
    } else {
      if (r.stdout !== undefined && r.stdout !== '') html += '<h5>程序输出</h5><pre class="modal-output">' + esc(truncate(r.stdout, 10000)) + '</pre>';
      if (r.expected !== undefined && r.expected !== '') html += '<h5>期望输出</h5><pre class="modal-output">' + esc(truncate(r.expected, 10000)) + '</pre>';
    }
    if (r.spj_log) html += '<h5>SPJ 输出</h5><pre class="modal-output">' + esc(truncate(r.spj_log, 10000)) + '</pre>';
    if (r.stderr) html += '<h5>stderr</h5><pre class="modal-output">' + esc(truncate(r.stderr, 10000)) + '</pre>';
    if (r.error) html += '<h5>错误</h5><pre class="modal-output">' + esc(truncate(r.error, 10000)) + '</pre>';
    if (r.log) html += '<h5>编译日志</h5><pre class="modal-output">' + esc(truncate(r.log, 10000)) + '</pre>';
    return html;
  }

  function showSampleResultModal(entry) {
    if (!entry.result) return;
    showModal('样例运行结果：' + entry.label, buildSampleResultHtml(entry.label, entry.result),
      [{ label: '关闭', fn: hideModal }], 'wide');
  }

  async function runSample(s) {
    s.verdictEl.textContent = '…';
    s.verdictEl.className = 'verdict pending';
    const pane = s.pane || activePane();
    try {
      await savePane(pane);
      await saveConfig(pane);
      const body = {
        path: pane.currentPath,
        io: pane.config.io,
        time_limit: pane.config.time_limit,
        memory_limit: pane.config.memory_limit,
      };
      if (s.type === 'manual') {
        body.input = s.in;
        body.expected = s.out;
      } else {
        const dir = parentDir(pane.currentPath);
        const base = (dir ? dir + '/' : '') + (s.subdir ? s.subdir + '/' : '');
        body.input_file = base + s.in;
        body.expected_file = base + s.out;
      }
      const r = await api('POST', '/api/run', body);
      if (r.error) {
        applySampleResult(s, '错误', { error: r.error });
        return;
      }
      if (!r.ok) {
        applySampleResult(s, 'CE', { log: r.log || '' });
        showCompileError('编译失败', r.log);
        return;
      }
      applySampleResult(s, r.verdict || r.status, r);
    } catch (e) {
      applySampleResult(s, '错误', { error: (e && e.message ? e.message : String(e)) });
    }
  }

  async function runAllSamples() {
    const pane = activePane();
    if (!pane.currentPath || !isRunnable(pane.currentPath)) {
      alert('请先打开 .cpp 文件');
      return;
    }
    if (!pane.samples || pane.samples.length === 0) { alert('当前题目没有样例'); return; }
    await savePane(pane);
    await saveConfig();
    const dir = parentDir(pane.currentPath);
    const samples = pane.samples.map(s => {
      const o = { label: s.label, type: s.type };
      if (s.type === 'manual') {
        o.input = s.in;
        o.expected = s.out;
      } else {
        const base = (dir ? dir + '/' : '') + (s.subdir ? s.subdir + '/' : '');
        o.in = base + s.in;
        o.out = base + s.out;
      }
      return o;
    });
    setCompileIndicator(true);
    try {
      const r = await api('POST', '/api/run_all', {
        path: pane.currentPath,
        io: pane.config.io,
        time_limit: pane.config.time_limit,
        memory_limit: pane.config.memory_limit,
        samples: samples,
      });
      setCompileIndicator(false);
      if (r.error) { alert(r.error); return; }
      if (!r.ok) {
        showCompileError('编译失败', r.log);
        return;
      }
      if (r.spj_compile_error) {
        showCompileError('SPJ 编译失败（样例按普通输出比较判定）', r.spj_compile_error);
      }
      const byLabel = {};
      for (const res of r.results) byLabel[res.label] = res;
      for (const s of pane.samples) {
        const res = byLabel[s.label];
        if (res) applySampleResult(s, res.verdict || res.status, res);
      }
    } finally {
      setCompileIndicator(false);
    }
  }

  async function deleteSample(s) {
    const pane = s.pane || activePane();
    if (s.type === 'manual') {
      const arr = getManualSamples(pane).filter(x => x.label !== s.label);
      setManualSamples(arr, pane);
    } else {
      const dir = parentDir(pane.currentPath);
      const base = (dir ? dir + '/' : '') + (s.subdir ? s.subdir + '/' : '');
      // 并行删除 + 不阻塞 UI
      api('POST', '/api/delete', { path: base + s.in });
      api('POST', '/api/delete', { path: base + s.out });
      const cached = problemCache[pane.currentProblem];
      if (cached && Array.isArray(cached.samples)) {
        cached.samples = cached.samples.filter(x =>
          !(x.label === s.label && x.in === s.in && (x.subdir || '') === (s.subdir || '')));
      }
    }
    // 本地立即重绘，不等网络
    const cached = problemCache[pane.currentProblem];
    if (cached) renderSamples(cached.samples || [], pane);
    else await loadProblem(pane.currentPath, null, true, pane);
    loadTree();
  }

  async function clearSamples() {
    const pane = sidepanelPane() || activePane();
    if (!pane.currentProblem) { alert('请先打开代码文件'); return; }
    const entries = (pane.samples || []).slice();
    const dir = parentDir(pane.currentPath);
    const dels = [];
    for (const s of entries) {
      if (s.type === 'manual') continue;
      const base = (dir ? dir + '/' : '') + (s.subdir ? s.subdir + '/' : '');
      dels.push(api('POST', '/api/delete', { path: base + s.in }));
      dels.push(api('POST', '/api/delete', { path: base + s.out }));
    }
    Promise.all(dels); // 并行删除，不阻塞
    setManualSamples([], pane);
    if (sampleCache[pane.currentProblem]) delete sampleCache[pane.currentProblem];
    const cached = problemCache[pane.currentProblem];
    if (cached) cached.samples = [];
    renderSamples([], pane);
    loadTree();
  }

  function nextSampleNo() {
    const pane = sidepanelPane() || activePane();
    let maxN = 0;
    for (const s of (pane.samples || [])) {
      const m = s.label.match(/(\d+)$/);
      if (m) maxN = Math.max(maxN, parseInt(m[1]));
    }
    return maxN + 1;
  }

  async function addSample() {
    const pane = sidepanelPane() || activePane();
    if (!pane.currentProblem) { alert('请先打开 .cpp 文件'); return; }
    const n = nextSampleNo();
    const arr = getManualSamples(pane);
    arr.push({ label: '样例' + n, in: '', out: '' });
    setManualSamples(arr, pane);
    await loadProblem(pane.currentPath, null, true, pane);
  }

  async function uploadFile(file, targetPath) {
    const buf = await file.arrayBuffer();
    const r = await fetch('/api/upload?path=' + encodeURIComponent(targetPath), {
      method: 'PUT',
      body: buf,
    });
    return r.json();
  }

  // ---------- 下载文件（浏览器强制下载，支持中文名） ----------
  function downloadFile(path) {
    if (!path) return;
    const a = document.createElement('a');
    a.href = '/api/raw?path=' + encodeURIComponent(path) + '&download=1';
    a.download = path.split('/').pop();
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  // ---------- 拖拽上传（通用文件） ----------
  function setupDragDrop() {
    const tree = $('#file-tree');
    let dragTarget = null;
    function clearDragTarget() {
      if (dragTarget) { dragTarget.classList.remove('drag-target'); dragTarget = null; }
    }
    tree.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.stopPropagation();
      tree.classList.add('dragover');
      const item = e.target.closest('.tree-item, .tree-children');
      const next = item || null;
      if (next !== dragTarget) {
        clearDragTarget();
        dragTarget = next;
        if (dragTarget) dragTarget.classList.add('drag-target');
      }
    });
    tree.addEventListener('dragleave', (e) => {
      if (!tree.contains(e.relatedTarget)) {
        tree.classList.remove('dragover');
        clearDragTarget();
      }
    });
    tree.addEventListener('drop', (e) => {
      e.preventDefault();
      e.stopPropagation();
      tree.classList.remove('dragover');
      clearDragTarget();
      const files = e.dataTransfer ? Array.from(e.dataTransfer.files) : [];
      if (files.length === 0) return;
      // 计算目标目录：落在文件上 → 同目录；落在目录/子树上 → 该目录；空白 → 根目录
      const t = e.target;
      let dirPath = '';
      const item = t.closest('.tree-item[data-path]');
      if (item) {
        dirPath = item.classList.contains('dir') ? item.dataset.path : parentDir(item.dataset.path);
      } else {
        const ch = t.closest('.tree-children[data-path]');
        if (ch && ch.dataset.path !== undefined) dirPath = ch.dataset.path;
      }
      handleDroppedFiles(files, dirPath);
    });
  }

  function fileExtOf(name) {
    const i = name.lastIndexOf('.');
    return i > 0 ? name.slice(i).toLowerCase() : '';
  }

  // 把文件直接上传到指定目录（dirPath 为空 = 工作区根目录）
  async function uploadFilesToDir(files, dirPath) {
    const dir = dirPath || '';
    const existing = new Set();
    (function walk(nodes) {
      for (const n of nodes || []) {
        existing.add(n.path);
        if (n.type === 'dir') walk(n.children || []);
      }
    })(state.tree);
    let uploaded = 0, skipped = 0, failed = 0;
    const skipNames = [];
    for (const f of files) {
      const full = (dir ? dir + '/' : '') + f.name;
      if (existing.has(full)) {
        if (!confirm('文件「' + f.name + '」已存在，是否覆盖？')) {
          skipped++; skipNames.push(f.name);
          continue;
        }
      }
      try {
        const r = await uploadFile(f, full);
        if (r && r.ok) uploaded++; else failed++;
      } catch (err) { failed++; }
    }
    await loadTree();
    let msg = '上传完成：成功 ' + uploaded + ' 个';
    if (skipped) msg += '，跳过 ' + skipped + ' 个（已存在）';
    if (failed) msg += '，失败 ' + failed + ' 个';
    if (skipNames.length) msg += '\n跳过：' + skipNames.join('、');
    alert(msg);
  }

  // 拖入 .in/.out/.ans/.a 且已打开 cpp → 保持原有“导入样例”行为
  async function handleDroppedFiles(fileList, dirPath) {
    const files = Array.from(fileList || []);
    if (files.length === 0) return;
    const SAMPLE_EXT = ['.in', '.out', '.ans', '.a'];
    const allSample = files.every(f => SAMPLE_EXT.includes(fileExtOf(f.name)));
    if (allSample && (sidepanelPane() || activePane()).currentProblem) {
      return importSamplesFromFiles(files);
    }
    await uploadFilesToDir(files, dirPath);
  }

  // 样例配对导入（原 handleDroppedFiles 逻辑，目标目录 = 侧栏归属面板 cpp 同目录 .samples/）
  async function importSamplesFromFiles(files) {
    const pane = sidepanelPane() || activePane();
    const INPUT_EXT = ['.in'];
    const OUTPUT_EXT = ['.out', '.ans', '.a'];
    const groups = {};   // stem -> {in: File, out: File}
    const unmatched = [];
    for (const f of files) {
      const dot = f.name.lastIndexOf('.');
      const stem = dot > 0 ? f.name.slice(0, dot) : '';
      const ext = dot > 0 ? f.name.slice(dot).toLowerCase() : '';
      if (!stem || (!INPUT_EXT.includes(ext) && !OUTPUT_EXT.includes(ext))) {
        unmatched.push(f.name);
        continue;
      }
      if (!groups[stem]) groups[stem] = {};
      if (INPUT_EXT.includes(ext)) groups[stem].in = f;
      else groups[stem].out = groups[stem].out || f;
    }
    const pairs = [];
    for (const stem of Object.keys(groups).sort()) {
      const g = groups[stem];
      if (g.in && g.out) pairs.push(g);
      else unmatched.push((g.in || g.out).name);
    }
    const name = pane.currentProblem;
    const dir = parentDir(pane.currentPath);
    const prefix = (dir ? dir + '/' : '') + '.samples/';  // 大样例放入隐藏目录
    let n = nextSampleNo();
    let cnt = 0;
    for (const g of pairs) {
      await uploadFile(g.in, prefix + name + n + '.in');
      await uploadFile(g.out, prefix + name + n + '.out');
      n++; cnt++;
    }
    await loadTree();
    await loadProblem(pane.currentPath, null, true, pane);
    if (unmatched.length === 0 && cnt > 0) return;
    let msg = cnt > 0
      ? ('已导入 ' + cnt + ' 组样例（存入隐藏目录 .samples/）。\n\n以下文件无法匹配 in/out 对，已忽略：\n')
      : '没有可导入的样例，以下文件无法匹配 in/out 对：\n';
    if (unmatched.length) msg += unmatched.map(x => '\u2022 ' + x).join('\n');
    alert(msg);
  }

let importInput = null;
  function importSample() {
    if (!(sidepanelPane() || activePane()).currentProblem) { alert('请先打开 .cpp 文件'); return; }
    if (!importInput) {
      importInput = document.createElement('input');
      importInput.type = 'file';
      importInput.multiple = true;
      importInput.accept = '.in,.out,.ans,.a';
      importInput.addEventListener('change', () => {
        handleDroppedFiles(importInput.files);
        importInput.value = '';
      });
    }
    importInput.click();
  }

  // ---------- 编译 / 运行 ----------
  function showResultModal(title, text) {
    showModal(title, '<pre class="modal-output">' + esc(text) + '</pre>', [
      { label: '关闭', fn: hideModal },
    ]);
  }

  function showCompileError(title, log) {
    try {
      localStorage.setItem('deepcoder.ce', JSON.stringify({
        title: title, log: log || '', path: activePath() || '', ts: Date.now(),
      }));
    } catch (e) { /* ignore */ }
    const w = window.open('/static/ce.html', 'deepcoder_ce', 'width=820,height=560');
    if (!w) showResultModal(title, log || '');
  }

  function setCompileIndicator(show) {
    $('#compile-indicator').classList.toggle('hidden', !show);
  }

  async function compileOnly() {
    const pane = activePane();
    if (!pane.currentPath || !isRunnable(pane.currentPath)) {
      alert('请先打开代码文件（.cpp / .c / .py / .java）');
      return;
    }
    await savePane(pane);
    setCompileIndicator(true);
    try {
      const r = await api('POST', '/api/compile', { path: pane.currentPath });
      setCompileIndicator(false);
      if (r.error) { showResultModal('编译', '【错误】\n' + r.error); return; }
      if (!r.ok) { showCompileError('编译失败', r.log); return; }
      showResultModal('编译', '【编译成功】' + (r.log ? '\n' + r.log : ''));
    } finally {
      setCompileIndicator(false);
    }
  }

  async function run(skipCompile) {
    const pane = activePane();
    if (!pane.currentPath || !isRunnable(pane.currentPath)) {
      alert('请先打开代码文件（.cpp / .c / .py / .java）');
      return;
    }
    await savePane(pane);
    await saveConfig();
    if (!skipCompile) setCompileIndicator(true);
    try {
      const r = await api('POST', '/api/run', {
        path: pane.currentPath,
        io: pane.config.io,
        time_limit: pane.config.time_limit,
        memory_limit: pane.config.memory_limit,
        skip_compile: !!skipCompile,
      });
      setCompileIndicator(false);
      if (r.error) { showResultModal('运行', '【错误】\n' + r.error); return; }
      if (!r.ok) { showCompileError('编译失败', r.log); return; }
      let text = `【${r.status}】 用时 ${r.time} ms，内存 ${r.memory} MB\n\n`;
      if (r.stderr) text += '--- stderr ---\n' + truncate(r.stderr, 10000) + '\n\n';
      text += '--- 输出 ---\n' + truncate(r.stdout || '（无输出）', 10000);
      showResultModal('运行结果', text);
    } finally {
      setCompileIndicator(false);
    }
  }

  // ---------- 交互模式（独立窗口，先开窗口再编译） ----------
  async function startInteractive() {
    const pane = activePane();
    if (!pane.currentPath || !isRunnable(pane.currentPath)) {
      alert('请先打开代码文件（.cpp / .c / .py / .java）');
      return;
    }
    await savePane(pane);
    const r = await api('POST', '/api/interactive_open', { path: pane.currentPath });
    if (r.error) { alert(r.error); return; }
    const w = window.open('/static/inter.html?session=' + encodeURIComponent(r.session),
      'inter_' + r.session, 'width=720,height=520');
    if (!w) {
      alert('弹出窗口被拦截，请允许本站弹出窗口');
      return;
    }
    api('POST', '/api/interactive_start', { session: r.session, path: pane.currentPath });
  }

  // ---------- 对拍（后台运行，独立窗口查看） ----------
  function getHackDefaults() {
    const pane = activePane();
    try {
      const c = JSON.parse(localStorage.getItem('deepcoder.hackcfg.' + pane.currentPath) || 'null');
      if (c && c.main && c.std && c.gen) return c;
    } catch (e) { /* ignore */ }
    const name = pane.currentProblem;
    const dir = parentDir(pane.currentPath || '');
    const curBase = pane.currentPath ? pane.currentPath.split('/').pop() : (name + '.cpp');
    // std/gen 默认扩展名跟随 main 的语言（cpp/c/py/java），保持多语言协调
    let ext = '.cpp';
    const m = (curBase || '').toLowerCase();
    if (m.endsWith('.py')) ext = '.py';
    else if (m.endsWith('.java')) ext = '.java';
    else if (m.endsWith('.c')) ext = '.c';
    // 默认路径与当前文件同目录（之前裸文件名会被解析到工作区根目录）
    const pre = dir ? dir + '/' : '';
    return { main: pre + curBase, std: pre + name + '_std' + ext, gen: pre + name + '_gen' + ext };
  }

  function openHackDialog() {
    if (!activePane().currentProblem) { alert('请先打开代码文件（.cpp / .c / .py / .java）'); return; }
    const d = getHackDefaults();
    showModal('对拍（std + gen + 主程序）', `
      <div class="field"><label>待测程序 main</label><input id="hack-main" value="${esc(d.main)}"></div>
      <div class="field"><label>标准/暴力解 std</label><input id="hack-std" value="${esc(d.std)}"></div>
      <div class="field"><label>数据生成器 gen</label><input id="hack-gen" value="${esc(d.gen)}"></div>
      <div class="field"><label>对拍轮数（清空或 0 = 无限对拍）</label><input id="hack-rounds" type="number" value="100"></div>
      <div class="hint">std / gen 需已存在。std 读 stdin 写 stdout；gen 向 stdout 输出随机数据。对拍在后台运行，点「对拍任务」打开窗口查看。</div>
    `, [
      { label: '取消', fn: hideModal },
      { label: '开始对拍', fn: startHack, primary: true },
    ]);
    // 光标默认在 std，回车跳到 gen，再回车开始对拍
    const stdInp = $('#hack-std');
    stdInp.focus();
    stdInp.select();
    stdInp.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        const g = $('#hack-gen');
        g.focus();
        g.select();
      }
    });
    $('#hack-gen').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        const b = $('#modal-actions .btn.primary');
        if (b) b.click();
      }
    });
  }

  async function startHack() {
    const body = {
      main: $('#hack-main').value,
      std: $('#hack-std').value,
      gen: $('#hack-gen').value,
      rounds: parseInt($('#hack-rounds').value) || -1,
      io: activePane().config.io,
      name: activePane().currentProblem,
      time_limit: activePane().config.time_limit,
      memory_limit: activePane().config.memory_limit,
    };
    hideModal();
    await savePane(activePane());
    await saveConfig();
    const r = await api('POST', '/api/hack', body);
    if (!r.ok) { alert(r.error || '启动失败'); return; }
    try {
      localStorage.setItem('deepcoder.hackcfg.' + activePane().currentPath,
        JSON.stringify({ main: body.main, std: body.std, gen: body.gen }));
    } catch (e) { /* ignore */ }
    pollHackBadge();
  }

  function openHackWindow() {
    const w = window.open('/static/hack.html', 'hacktasks', 'width=760,height=560');
    if (!w) alert('弹出窗口被拦截，请允许本站弹出窗口');
  }

  async function pollHackBadge() {
    try {
      const r = await api('GET', '/api/hack_list');
      if (!r.error && r.tasks) {
        let n = 0;
        for (const t of Object.values(r.tasks)) {
          if (t.status === 'running') n++;
        }
        const badge = $('#hack-badge');
        if (n > 0) { badge.textContent = n; badge.classList.remove('hidden'); }
        else badge.classList.add('hidden');
      }
    } catch (e) { /* ignore */ }
    setTimeout(pollHackBadge, 3000);
  }

  // ---------- 右侧栏拖拽调宽 ----------
  function setupSideResizer() {
    const panel = $('#sidepanel');
    const handle = $('#side-resizer');
    let dragging = null;
    const saved = parseInt(localStorage.getItem('deepcoder.sidewidth')) || 0;
    if (saved >= 220) panel.style.width = saved + 'px';
    handle.addEventListener('mousedown', (e) => {
      dragging = { startX: e.clientX, startW: panel.getBoundingClientRect().width };
      document.body.classList.add('resizing');
      e.preventDefault();
    });
    window.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      const w = Math.min(window.innerWidth * 0.6, Math.max(220, dragging.startW + (dragging.startX - e.clientX)));
      panel.style.width = w + 'px';
      if (state.editor) state.editor.refresh();
    });
    window.addEventListener('mouseup', () => {
      if (!dragging) return;
      try {
        localStorage.setItem('deepcoder.sidewidth', String(Math.round(panel.getBoundingClientRect().width)));
      } catch (e) { /* ignore */ }
      dragging = null;
      document.body.classList.remove('resizing');
      if (state.editor) state.editor.refresh();
    });
  }

  // ---------- 左侧文件树：拖拽调宽 + 隐藏/显示 ----------
  function setupSidebar() {
    const panel = $('#sidebar');
    const handle = $('#sidebar-resizer');
    const toggleBtn = $('#btn-toggle-sidebar');
    let dragging = null;

    const savedW = parseInt(localStorage.getItem('deepcoder.sidebar.width')) || 0;
    if (savedW >= 120) panel.style.width = savedW + 'px';
    const hidden = localStorage.getItem('deepcoder.sidebar.hidden') === '1';
    if (hidden) panel.classList.add('hidden');
    toggleBtn.textContent = hidden ? '☰' : '⏴';
    toggleBtn.title = hidden ? '显示文件树' : '隐藏文件树';

    handle.addEventListener('mousedown', (e) => {
      if (panel.classList.contains('hidden')) return;
      dragging = { startX: e.clientX, startW: panel.getBoundingClientRect().width };
      document.body.classList.add('resizing');
      e.preventDefault();
    });
    window.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      const w = Math.min(window.innerWidth * 0.5, Math.max(120, dragging.startW + (e.clientX - dragging.startX)));
      panel.style.width = w + 'px';
      if (state.editor) state.editor.refresh();
    });
    window.addEventListener('mouseup', () => {
      if (!dragging) return;
      try {
        localStorage.setItem('deepcoder.sidebar.width', String(Math.round(panel.getBoundingClientRect().width)));
      } catch (err) { /* ignore */ }
      dragging = null;
      document.body.classList.remove('resizing');
      if (state.editor) state.editor.refresh();
    });
    toggleBtn.addEventListener('click', () => {
      const willHide = !panel.classList.contains('hidden');
      panel.classList.toggle('hidden', willHide);
      toggleBtn.textContent = willHide ? '☰' : '⏴';
      toggleBtn.title = willHide ? '显示文件树' : '隐藏文件树';
      try {
        localStorage.setItem('deepcoder.sidebar.hidden', willHide ? '1' : '0');
      } catch (err) { /* ignore */ }
      if (state.editor) state.editor.refresh();
    });
  }

  // ---------- 快捷键帮助 ----------
  function openHelpDialog() {
    showModal('快捷键', `
      <div class="help-grid">
        <div><kbd>F9</kbd></div><div>仅编译</div>
        <div><kbd>F10</kbd></div><div>交互模式（独立窗口）</div>
        <div><kbd>F11</kbd></div><div>一次编译，运行全部样例</div>
        <div><kbd>Ctrl</kbd>+<kbd>Space</kbd></div><div>手动触发代码补全</div>        <div><kbd>Ctrl</kbd>+<kbd>S</kbd></div><div>保存当前文件</div>
        <div><kbd>Delete</kbd></div><div>删除选中的文件 / 文件夹</div>
        <div><kbd>Esc</kbd></div><div>关闭弹窗、补全列表、右键菜单及各独立窗口</div>
        <div><kbd>Ctrl</kbd>+滚轮</div><div>调节编辑器字号</div>
      </div>
      <div class="hint">文件树右键：新建 / 重命名 / 删除 / 上传 / 下载 / 在右侧打开。把任意文件拖入文件树可直接上传到对应目录；拖入 .in/.out 文件会自动配对为样例。分屏：右键文件选「在右侧打开」，拖动中间分隔条调整宽度，右键菜单可「关闭分屏」。</div>
    `, [
      { label: '关闭', fn: hideModal },
    ]);
  }

  // ---------- 弹窗 ----------
  function showModal(title, bodyHtml, actions, extraCls) {
    $('#modal').classList.toggle('wide', !!extraCls);
    $('#modal-title').textContent = title;
    $('#modal-body').innerHTML = bodyHtml;
    const actBox = $('#modal-actions');
    actBox.innerHTML = '';
    for (const a of actions) {
      const b = el('button', 'btn' + (a.primary ? ' primary' : ''), a.label);
      b.addEventListener('click', a.fn);
      actBox.appendChild(b);
    }
    $('#modal-mask').classList.remove('hidden');
  }
  function hideModal() { $('#modal-mask').classList.add('hidden'); }

  function autoFocusAndSubmit(selector) {
    const inp = $(selector);
    if (!inp) return;
    inp.focus();
    inp.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        const b = $('#modal-actions .btn.primary');
        if (b) b.click();
      }
    });
  }

  function promptNewFile(dirPath) {
    showModal('新建文件', `
      <div class="field"><label>目录</label><span>${esc(dirPath || '当前目录')}</span></div>
      <div class="field"><label>文件名（默认 .cpp）</label><input id="nf-name" placeholder="main.cpp"></div>
    `, [
      { label: '取消', fn: hideModal },
      { label: '创建', fn: async () => {
          let name = $('#nf-name').value.trim();
          if (!name) name = 'main.cpp';
          if (!name.includes('.')) name += '.cpp';
          const full = (dirPath ? dirPath + '/' : '') + name;
          const r = await api('POST', '/api/new_file', { path: full });
          if (r.error) { alert(r.error); return; }
          hideModal();
          await loadTree();
          openFile(full);
      }, primary: true },
    ]);
    autoFocusAndSubmit('#nf-name');
  }

  async function openTemplate(ext) {
    hideModal();
    const path = '.default.' + ext;
    const r = await api('GET', '/api/file?path=' + encodeURIComponent(path));
    if (r.error) {
      const c = await api('POST', '/api/new_file', { path });
      if (c.error) { alert(c.error); return; }
    }
    openFile(path);
  }

  function editTemplate() {
    showModal('编辑模板（选择语言）', `
      <div class="field">
        <label>选择要编辑的默认模板，保存后新建该语言的源文件时将使用此模板。</label>
      </div>
    `, [
      { label: 'C++（.default.cpp）', fn: () => openTemplate('cpp'), primary: true },
      { label: 'Python（.default.py）', fn: () => openTemplate('py') },
      { label: 'Java（.default.java）', fn: () => openTemplate('java') },
    ]);
  }

  function promptUpload(dirPath) {
    showModal('上传文件', `
      <div class="field"><label>目标目录</label><span>${esc(dirPath || '当前目录')}</span></div>
      <div class="field"><label>选择文件</label><input type="file" id="up-file"></div>
      <div class="hint">可上传 SPJ 的 checker.cpp、testlib.h 等任意文件。</div>
    `, [
      { label: '取消', fn: hideModal },
      { label: '上传', fn: async () => {
          const f = $('#up-file').files[0];
          if (!f) { alert('请选择文件'); return; }
          const full = (dirPath ? dirPath + '/' : '') + f.name;
          await uploadFile(f, full);
          hideModal();
          await loadTree();
      }, primary: true },
    ]);
  }

  function promptNewDir(dirPath) {
    showModal('新建文件夹', `
      <div class="field"><label>目录</label><span>${esc(dirPath || '当前目录')}</span></div>
      <div class="field"><label>文件夹名</label><input id="nd-name" placeholder="newfolder"></div>
    `, [
      { label: '取消', fn: hideModal },
      { label: '创建', fn: async () => {
          const name = $('#nd-name').value.trim();
          if (!name) { alert('文件夹名不能为空'); return; }
          const full = (dirPath ? dirPath + '/' : '') + name;
          const r = await api('POST', '/api/new_dir', { path: full });
          if (r.error) { alert(r.error); return; }
          hideModal();
          await loadTree();
      }, primary: true },
    ]);
    autoFocusAndSubmit('#nd-name');
  }

  function promptRename(node) {
    showModal('重命名', `
      <div class="field"><label>原名</label><span>${esc(node.path)}</span></div>
      <div class="field"><label>新名称</label><input id="rn-name" value="${esc(node.name)}"></div>
    `, [
      { label: '取消', fn: hideModal },
      { label: '确定', fn: async () => {
          const nn = $('#rn-name').value.trim();
          if (!nn) return;
          const newPath = (parentDir(node.path) ? parentDir(node.path) + '/' : '') + nn;
          const r = await api('POST', '/api/rename', { old: node.path, new: newPath });
          if (r.error) { alert(r.error); return; }
          const fix = (pane) => {
            if (pane.currentPath === node.path || (pane.currentPath && pane.currentPath.startsWith(node.path + '/'))) {
              pane.currentPath = newPath + pane.currentPath.slice(node.path.length);
            }
          };
          fix(state);
          fix(state.right);
          hideModal();
          await loadTree();
          updateFileStatus();
      }, primary: true },
    ]);
    const inp = $('#rn-name');
    inp.focus();
    inp.select();
    inp.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        const b = $('#modal-actions .btn.primary');
        if (b) b.click();
      }
    });
  }

  async function doDeleteSelected() {
    const targets = [...state.selected];
    if (targets.length === 0) return;
    state.selected.clear();
    // 先同步清掉两个面板中打开的对应文件，删除请求并行发出
    for (const p of targets) {
      if (state.currentPath && (state.currentPath === p || state.currentPath.startsWith(p + '/'))) clearEditorArea();
      if (state.right.currentPath && (state.right.currentPath === p || state.right.currentPath.startsWith(p + '/'))) clearRightPane();
    }
    Promise.all(targets.map(p => api('POST', '/api/delete', { path: p })))
      .then(() => loadTree())
      .catch(() => loadTree());
  }

  // ---------- 智能分号：struct Node 打出 { 的瞬间自动补 }; ----------
  function maybeSemicolonAfterOpenBrace(cm) {
    // 输入 '{' 时 autoCloseBrackets 已自动补 '}'，光标停在 {} 中间。
    // 若 '{' 前是 class/struct/enum/union 声明 → 立即在 '}' 后补 ';'
    try {
      const cur = cm.getCursor();
      const lineNo = cur.line;
      const line = cm.getLine(lineNo);
      if (line[cur.ch] !== '}') return;                    // 需要 autoClose 补出的 '}'
      if (cur.ch === 0 || line[cur.ch - 1] !== '{') return; // 光标紧跟在 '{' 后
      const before = line.slice(0, cur.ch - 1);
      const segStart = Math.max(before.lastIndexOf(';'), before.lastIndexOf('}'), 0);
      const seg = before.slice(segStart === 0 ? 0 : segStart + 1).trim();
      if (/^(typedef\s+)?(class|struct|union|enum)(\s+(class\s+)?\w+)?$/.test(seg)) {
        cm.replaceRange(';', { line: lineNo, ch: cur.ch + 1 });
      }
    } catch (e) { /* ignore */ }
  }

  // ---------- 智能分号：class Foo {} 输入 '}' 后自动补 ';' ----------
  function findMatchingBrace(cm, pos) {
    // 从光标前向前扫描找配对的 '{'（跳过字符串/注释）。
    // 注意：光标前的 '}' 是刚输入的当前闭合（由调用方保证），从 pos.ch-2 开始，
    // 避免它被误计为嵌套导致配对的 '{' 被抵消。
    let depth = 0;
    let lineNo = pos.line;
    let first = true;
    let st = null;
    try { st = pos.line > 0 ? cm.getStateAfter(pos.line - 1) : null; } catch (e) { st = null; }
    while (lineNo >= 0) {
      const text = cm.getLine(lineNo);
      const end = lineNo === pos.line ? pos.ch : text.length;
      const skip = new Array(text.length).fill(false);
      try {
        const tokens = cm.getLineTokens(lineNo, st);
        for (const t of tokens) {
          if (/string|comment/i.test(t.type || '')) {
            for (let i = t.start; i < t.end && i < text.length; i++) skip[i] = true;
          }
        }
        if (tokens.length) st = tokens[tokens.length - 1].state;
      } catch (e) { /* ignore */ }
      for (let i = end - (first ? 2 : 1); i >= 0; i--) {
        if (skip[i]) continue;
        const c = text[i];
        if (c === '}') depth++;
        else if (c === '{') {
          if (depth === 0) return { line: lineNo, ch: i };
          depth--;
        }
      }
      first = false;
      if (lineNo === 0) break;
      lineNo--;
    }
    return null;
  }

  function maybeAddSemicolon(cm) {
    try {
      const cur = cm.getCursor();
      const lineNo = cur.line;
      const line = cm.getLine(lineNo);
      if (cur.ch === 0 || line[cur.ch - 1] !== '}') return;   // 光标前必须是刚输入的 '}'
      const after = line.slice(cur.ch);
      if (!/^\s*$/.test(after)) return;                        // '}' 后已有内容（含分号）则跳过
      const open = findMatchingBrace(cm, cur);
      if (!open) return;
      const before = cm.getLine(open.line).slice(0, open.ch);
      const segStart = Math.max(before.lastIndexOf(';'), before.lastIndexOf('}'), 0);
      const seg = before.slice(segStart === 0 ? 0 : segStart + 1).trim();
      if (/^(typedef\s+)?(class|struct|union|enum)(\s+(class\s+)?\w+)?$/.test(seg)) {
        cm.replaceRange(';', { line: lineNo, ch: cur.ch });
      }
    } catch (e) { /* ignore */ }
  }

  // ---------- 代码补全（VSCode 风格弹层） ----------
  const completer = {
    box: null,
    items: [],
    index: 0,
    requestId: 0,
    timer: null,
    visible: false,
  };

  const COMPLETE_ICONS = { keyword: 'K', builtin: 'B', local: 'V', global: 'G', word: 'W' };

  function hideCompletionBox() {
    completer.visible = false;
    completer.items = [];
    completer.index = 0;
    if (completer.box) { completer.box.remove(); completer.box = null; }
  }

  function setCompleteIndex(i) {
    if (!completer.items.length) return;
    if (i < 0) i = completer.items.length - 1;
    if (i >= completer.items.length) i = 0;
    completer.index = i;
    if (!completer.box) return;
    const rows = completer.box.children;
    for (let k = 0; k < rows.length; k++) rows[k].classList.toggle('active', k === i);
    const active = rows[i];
    if (active && active.scrollIntoView) {
      try { active.scrollIntoView({ block: 'nearest' }); } catch (e) { /* ignore */ }
    }
  }

  function renderCompletionBox(cands) {
    hideCompletionBox();
    if (!cands || !cands.length) return;
    const box = el('div', 'dc-complete');
    completer.items = cands;
    completer.index = 0;
    cands.forEach((it, i) => {
      const row = el('div', 'dc-complete-item' + (i === 0 ? ' active' : ''));
      const ic = COMPLETE_ICONS[it.type] || 'W';
      row.appendChild(el('span', 'dc-complete-type ' + ic.toLowerCase(), ic));
      row.appendChild(el('span', 'dc-complete-label', it.label));
      if (it.detail) row.appendChild(el('span', 'dc-complete-detail', it.detail));
      row.addEventListener('mousedown', (e) => {
        e.preventDefault();
        setCompleteIndex(i);
        acceptCompletion();
      });
      row.addEventListener('mousemove', () => setCompleteIndex(i));
      box.appendChild(row);
    });
    document.body.appendChild(box);
    // 定位到光标下方，放不下则显示在上方
    const coords = activeEditor().cursorCoords(true, 'page');
    const bw = box.offsetWidth, bh = box.offsetHeight;
    let x = coords.left;
    if (x + bw > window.innerWidth - 8) x = window.innerWidth - bw - 8;
    let y = coords.bottom + 4;
    if (y + bh > window.innerHeight - 8) y = Math.max(8, coords.top - bh - 4);
    box.style.left = Math.max(8, x) + 'px';
    box.style.top = y + 'px';
    completer.box = box;
    completer.visible = true;
  }

  function currentPrefix() {
    const cm = activeEditor();
    const cur = cm.getCursor();
    const before = cm.getLine(cur.line).slice(0, cur.ch);
    const m = before.match(/[A-Za-z0-9_.:>\-\[\]<>]*$/);
    return m ? m[0] : '';
  }

  async function triggerCompletion(manual) {
    const cm = activeEditor();
    if (!cm || !activePath()) return;
    if (ghostState.active) cancelGhost(cm);   // 本地补全优先，取消 AI ghost
    const cursor = cm.getCursor();
    // 自动触发要求光标前是标识符字符；手动触发（Ctrl+Space 或 . :: -> 后）任意位置都允许
    if (!manual && !/[A-Za-z0-9_.:>-]$/.test(currentPrefix())) {
      hideCompletionBox();
      return;
    }
    const reqId = ++completer.requestId;
    const r = await api('POST', '/api/complete', {
      path: state.currentPath,
      content: cm.getValue(),
      line: cursor.line,
      ch: cursor.ch,
    });
    if (reqId !== completer.requestId) return;   // 过期请求丢弃
    if (completer.timer) { clearTimeout(completer.timer); completer.timer = null; }
    if (r.error || !r.candidates || !r.candidates.length) { hideCompletionBox(); return; }
    const c2 = cm.getCursor();                    // 请求期间光标已移动 → 放弃
    if (c2.line !== cursor.line || c2.ch !== cursor.ch) return;
    renderCompletionBox(r.candidates);
  }

  function scheduleAutoComplete() {
    if (completer.timer) clearTimeout(completer.timer);
    completer.timer = setTimeout(() => { completer.timer = null; triggerCompletion(false); }, 120);
  }

  function acceptCompletion() {
    if (ghostState.active) cancelGhost(activeEditor());   // 防 ghost 与本地补全冲突
    const it = completer.items[completer.index];
    hideCompletionBox();
    if (!it) return;
    const cm = activeEditor();
    const cursor = cm.getCursor();
    const prefix = currentPrefix();
    let insert = it.insertText || it.label;
    let cursorOffset = insert.length;                // 默认光标在末尾
    if (it.fn && !/\($/.test(insert)) {
      insert += '()';                                // 函数/方法补全自动加括号
      cursorOffset = insert.length - 1;              // 光标停在括号中间
    }
    let replaceLen = 0;
    if (insert.toLowerCase().endsWith(prefix.toLowerCase())) {
      replaceLen = prefix.length;                 // 整个前缀（含 obj. / std::）替换
    } else {
      const idm = prefix.match(/[A-Za-z_][A-Za-z0-9_]*$/);
      replaceLen = idm ? idm[0].length : 0;       // 只替换尾部标识符
    }
    cm.replaceRange(insert, { line: cursor.line, ch: cursor.ch - replaceLen }, cursor);
    if (cursorOffset < insert.length) {
      cm.setCursor({ line: cursor.line, ch: cursor.ch - replaceLen + cursorOffset });
    }
    cm.focus();
  }

  // ---------- AI 代码补全（ghost text：光标静止 5 秒触发，flash 不思考，Tab 确认） ----------
  const ghostState = {
    active: false,        // ghost 文本是否显示中
    marker: null,         // markText 返回的 marker
    start: null,          // {line,ch} 插入起点
    end: null,            // {line,ch} 插入终点
    text: '',             // 补全文本
    timer: null,          // 5 秒静止计时器
    requestId: 0,         // 请求序号（防过期）
    busy: false,          // 请求进行中
    suppress: false,      // ghost 自身操作（插入/取消）时抑制事件
    editor: null,         // 触发时所在编辑器
  };
  const GHOST_IDLE_MS = 5000;      // 光标静止多久触发
  const GHOST_MAX_LEN = 600;       // 补全文本上限

  function cleanGhostText(text) {
    text = (text || '').trim();
    text = text.replace(/^```[a-zA-Z0-9_+-]*\s*/i, '').replace(/```\s*$/, '');
    text = text.replace(/\s+$/, '');
    if (text.length > GHOST_MAX_LEN) text = text.slice(0, GHOST_MAX_LEN);
    return text;
  }

  // 显示灰色 ghost 文本（插入 + 标记，不移动光标）
  function showGhost(cm, rawText) {
    const text = cleanGhostText(rawText);
    if (!text) {  return; }
    const cur = cm.getCursor();
    const g = ghostState;
    g.suppress = true;
    try {
      cm.replaceRange(text, cur, null, '+aiGhost');
      const end = cm.posFromIndex(cm.indexFromPos(cur) + text.length);
      g.marker = cm.markText(cur, end, { className: 'ai-ghost-text', atomic: true });
      cm.setCursor(cur);          // 光标保持原位（触发 cursorActivity，被 suppress 忽略）
      g.active = true;
      g.start = { line: cur.line, ch: cur.ch };
      g.end = end;
      g.text = text;
      
    } catch (e) {
      g.active = false;
      g.marker = null;
    }
    g.suppress = false;
  }

  // Tab 确认：接受 ghost 文本（内容已插入，仅清除标记并移动光标）
  function acceptGhost(cm) {
    const g = ghostState;
    if (!g.active) return;
    const end = g.end;
    if (g.marker) { try { g.marker.clear(); } catch (e) { /* ignore */ } g.marker = null; }
    g.active = false;
    g.busy = false;
    clearTimeout(g.timer); g.timer = null;
    try { cm.setCursor(end); } catch (e) { /* ignore */ }
    if (!cm.hasFocus()) cm.focus();
  }

  // 取消 ghost：删除插入的文本
  function cancelGhost(cm) {
    const g = ghostState;
    clearTimeout(g.timer); g.timer = null;
    if (!g.active) return;
    if (g.marker) { try { g.marker.clear(); } catch (e) { /* ignore */ } g.marker = null; }
    g.active = false;
    g.busy = false;
    ++g.requestId;               // 使在途请求过期
    if (g.start && g.end) {
      g.suppress = true;
      try { cm.replaceRange('', g.start, g.end, '+aiGhostCancel'); } catch (e) { /* ignore */ }
      g.suppress = false;
    }
    g.start = g.end = null;
    g.text = '';
    
  }

  // 重置 5 秒静止计时器（用户输入 / 光标移动时调用）
  function resetGhostTimer(cm, pane) {
    const had = ghostState.timer;
    clearTimeout(ghostState.timer);
    ghostState.timer = null;
    if (!cm || !cm.hasFocus()) return;
    if (!pane || !isCodeExt(pane.currentPath || '')) return;
    if (ghostState.active) return;       // ghost 显示中不再重复触发
    const g = ghostState;
    g.editor = cm;
    g.timer = setTimeout(() => {
      g.timer = null;
      
      requestAiComplete(cm, pane);
    }, GHOST_IDLE_MS);
  }

  // 请求 AI 补全（flash 不思考）
  async function requestAiComplete(cm, pane) {
    const g = ghostState;
    if (g.active || g.busy) return;
    if (completer.visible) {  return; }
    const cur = cm.getCursor();
    if (!cur || cur.line == null) return;
    if (cm.getSelection()) {  return; }
    const content = cm.getValue();
    if (!content || content.length > 300000) {  return; }
    if (!isCodeExt(pane.currentPath || '')) {  return; }
    g.busy = true;
    const reqId = ++g.requestId;
    try {
      const r = await api('POST', '/api/ai_complete', {
        path: pane.currentPath || '',
        content: content,
        line: cur.line,
        ch: cur.ch,
      });
      if (reqId !== g.requestId) {  return; }
      if (!r || !r.text) {  return; }
      // 注意：这里不能检查 g.busy（当前请求自己的 busy 必然为 true，会在 finally 中复位）
      if (g.active || completer.visible || !cm.hasFocus()) {  return; }
      const cur2 = cm.getCursor();
      if (cur2.line !== cur.line || cur2.ch !== cur.ch) {  return; }
      if (cm.getValue() !== content) {  return; }
      if (cm.getSelection()) {  return; }
      
      showGhost(cm, r.text);
    } catch (e) {
    } finally {
      if (reqId === g.requestId) g.busy = false;
    }
  }

  function handleEditorKeydown(cm, e) {
    // AI ghost 补全：Tab 接受（优先于本地补全弹层）
    if (e.key === 'Tab' && ghostState.active) {
      e.preventDefault();
      acceptGhost(cm);
      return;
    }
    // Ctrl+Space：无论弹层是否打开都重新触发补全（VSCode 行为）
    if (e.ctrlKey && (e.key === ' ' || e.key === 'Space')) {
      e.preventDefault();
      triggerCompletion(true);
      return;
    }
    if (completer.visible) {
      if (e.key === 'ArrowDown') { e.preventDefault(); setCompleteIndex(completer.index + 1); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); setCompleteIndex(completer.index - 1); return; }
      if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); acceptCompletion(); return; }
      if (e.key === 'Escape') { e.preventDefault(); hideCompletionBox(); return; }
      if (e.key.length === 1 && /[A-Za-z0-9_.]/.test(e.key)) {
        scheduleAutoComplete();                    // 弹层打开时继续输入 → 重新请求过滤
        return;
      }
      hideCompletionBox();
    } else if (e.key === '}') {
      // autoCloseBrackets 跳过已自动补的 '}' 时不产生 change（或在 keypress 阶段才处理），
      // 这里延迟兜底触发智能分号（等 closebrackets 完成光标移动）
      setTimeout(() => maybeAddSemicolon(cm), 50);
    }
  }

  // 补全触发场景（change 后判定）：'.' 或 '::' 或 '->'。
  // 普通标识符输入不再自动弹出补全，避免干扰（Ctrl+Space 仍可手动触发）
  function completionTriggerForChange(cm, ins) {
    if (!ins) return false;
    const cur = cm.getCursor();
    const before = cm.getLine(cur.line).slice(0, cur.ch);
    if (ins.endsWith('.')) return true;                 // 对象成员 x.
    if (ins.endsWith(':')) return before.endsWith('::'); // 作用域限定 xx::
    if (ins.endsWith('>')) return before.endsWith('->'); // 指针成员 x->
    return false;
  }

  // ---------- 事件绑定 ----------
  function bind() {
    $('#btn-run').addEventListener('click', () => run(false));    $('#btn-run-all').addEventListener('click', runAllSamples);
    $('#btn-inter').addEventListener('click', startInteractive);
    $('#btn-add-sample').addEventListener('click', addSample);
    $('#btn-import-sample').addEventListener('click', importSample);
    $('#btn-clear-samples').addEventListener('click', clearSamples);
    $('#btn-hack').addEventListener('click', openHackDialog);
    $('#btn-hack-tasks').addEventListener('click', openHackWindow);
    $('#btn-template').addEventListener('click', editTemplate);
    $('#btn-refresh').addEventListener('click', loadTree);
    $('#btn-help').addEventListener('click', openHelpDialog);
    $('#btn-download').addEventListener('click', () => {
      if (!activePath()) { alert('请先打开一个文件'); return; }
      downloadFile(activePath());
    });
    $('#btn-md-preview').addEventListener('click', () => cycleMdModeFor('left'));
    $('#btn-md-preview-r').addEventListener('click', () => cycleMdModeFor('right'));
    $('#cfg-theme').addEventListener('change', saveEditorSettings);
    setupDragDrop();
    setupImageViewer();
    setupRightImageViewer();
    setupSideResizer();
    setupSidebar();
    setupSplitDivider();
    setupMdDivider();

    // CE 窗口点击错误行 → 跳转到编辑器对应位置
    window.addEventListener('storage', (e) => {
      if (e.key !== 'deepcoder.jump') return;
      let d = null;
      try { d = JSON.parse(e.newValue || 'null'); } catch (err) { d = null; }
      if (d && d.line) {
        const cm = state.editor;
        const pos = { line: Math.max(d.line - 1, 0), ch: Math.max((d.col || 1) - 1, 0) };
        cm.focus();
        cm.setCursor(pos);
        cm.scrollIntoView(pos, 60);
      }
    });

    // 文件树右键（含空白区域）
    $('#file-tree').addEventListener('contextmenu', (e) => {
      e.preventDefault();
      const item = e.target.closest('.tree-item');
      if (item) {
        const type = item.classList.contains('dir') ? 'dir' : 'file';
        showContextMenu(e, { type, path: item.dataset.path, name: item.querySelector('.name').textContent });
      } else {
        const children = e.target.closest('.tree-children');
        if (children && children.dataset.path !== undefined) {
          const path = children.dataset.path;
          showContextMenu(e, { type: 'dir', path, name: path.split('/').pop() });
        } else {
          showContextMenu(e, { type: 'dir', path: '', name: '工作目录' });
        }
      }
    });

    $('#io-select').addEventListener('change', () => saveConfig());
    $('#cfg-time').addEventListener('change', () => saveConfig());
    $('#cfg-mem').addEventListener('change', () => saveConfig());
    $('#cfg-spj').addEventListener('change', () => saveConfig());
    $('#cfg-compiler').addEventListener('change', saveCompileConfig);
    $('#cfg-flags').addEventListener('change', saveCompileConfig);
    $('#cfg-python').addEventListener('change', saveCompileConfig);
    $('#cfg-javac').addEventListener('change', saveCompileConfig);
    $('#cfg-java').addEventListener('change', saveCompileConfig);
    // 设置分区默认折叠，点标题展开/收起
    document.querySelectorAll('.sec-title').forEach(t => {
      const body = document.getElementById(t.dataset.sec);
      if (!body) return;
      body.classList.add('collapsed');
      t.classList.add('collapsed');
      t.addEventListener('click', () => {
        const collapsed = body.classList.toggle('collapsed');
        t.classList.toggle('collapsed', collapsed);
      });
    });
    // 任意键关闭无输入框的弹窗（编译/运行结果等），且按键不落入编辑器。
    // 用 capture 阶段拦截：先于 CodeMirror 的按键处理，避免修改编辑器内容。
    document.addEventListener('keydown', (e) => {
      const modal = $('#modal-mask');
      if (!modal || modal.classList.contains('hidden')) return;
      if (modal.querySelector('input, textarea, select')) return;  // 输入型弹窗：正常输入，Esc 关闭
      e.preventDefault();
      e.stopPropagation();
      hideModal();
    }, { capture: true });
    // 键盘：Ctrl+S 全局强制保存；Esc 关闭弹窗/右键菜单；Delete 删除选中；F9-F11 功能键
    document.addEventListener('keydown', (e) => {
      if (e.ctrlKey && !e.shiftKey && !e.altKey && (e.key === 's' || e.key === 'S')) {
        e.preventDefault();
        e.stopPropagation();
        saveCurrent();
        return;
      }
      if (e.key === 'Escape') {
        const modal = $('#modal-mask');
        if (modal && !modal.classList.contains('hidden')) { hideModal(); return; }
        const ctx = document.querySelector('.ctx-menu');
        if (ctx) { ctx.remove(); return; }
      }
      const inInput = e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA');
      if (inInput) return; // 编辑器或输入框内不拦截（编辑器内由 CodeMirror 处理）
      if (e.key === 'Delete' && state.selected.size > 0) {
        e.preventDefault();
        doDeleteSelected();
        return;
      }
      if (e.key === 'F9') {
        e.preventDefault();
        compileOnly();
      } else if (e.key === 'F10') {
        e.preventDefault();
        startInteractive();
      } else if (e.key === 'F11') {
        e.preventDefault();
        runAllSamples();
      }
    });
    // 关闭/刷新前提醒未保存（左右两个面板都检查）
    window.addEventListener('beforeunload', (e) => {
      if (state.dirty || state.right.dirty) { e.preventDefault(); e.returnValue = ''; }
    });
  }

  // ---------- AI 聊天分屏（参考 DeepChat 操作模式） ----------
  const CHAT_SETTINGS_KEY = 'deepcoder.chat.settings';
  const chatState = {
    messages: [],        // [{role:'system'|'user'|'assistant', content}]
    open: false,
    generating: false,
    controller: null,    // AbortController
    thinking: '',        // 当前 AI 消息的推理内容
    content: '',         // 当前 AI 消息的正文
    usage: null,         // 最近一次响应的 usage（tokens 统计）
    model: 'deepseek-v4-flash',  // 模型（参考 DeepChat 操作模式）
    effort: 'max',               // 思考深度：max | high
    showThinking: true,          // 是否显示思考过程
    tools: [],                   // 当前 AI 消息的工具调用记录 [{name,args,ok,result}]
  };

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function formatToolArgs(t) {
    const a = t.args || {};
    if (t.name === 'write_file') return a.path ? a.path : '(?)';
    if (t.name === 'read_file') return a.path ? a.path : '(?)';
    if (t.name === 'list_dir') return a.path ? a.path : '.';
    try { return JSON.stringify(a); } catch (e) { return ''; }
  }

  function loadChatSettings() {
    try {
      const s = JSON.parse(localStorage.getItem(CHAT_SETTINGS_KEY) || '{}');
      if (s.model) chatState.model = s.model;
      if (s.effort) chatState.effort = s.effort;
      if (s.showThinking !== undefined) chatState.showThinking = !!s.showThinking;
    } catch (e) { /* ignore */ }
  }

  function saveChatSettings() {
    try {
      localStorage.setItem(CHAT_SETTINGS_KEY, JSON.stringify({
        model: chatState.model,
        effort: chatState.effort,
        showThinking: chatState.showThinking,
      }));
    } catch (e) { /* ignore */ }
  }

  function syncChatToolbar() {
    const m = $('#ai-model'); if (m) m.value = chatState.model;
    const e = $('#ai-effort'); if (e) e.value = chatState.effort;
    const t = $('#ai-thinking-toggle');
    if (t) {
      t.classList.toggle('active', chatState.showThinking);
      t.textContent = chatState.showThinking ? '💭 思考' : '💭 思考:关';
      t.title = chatState.showThinking ? '隐藏思考过程' : '显示思考过程';
    }
  }

  function chatPanel() { return $('#ai-chat-panel'); }
  function chatDivider() { return $('#ai-chat-divider'); }
  function chatMsgBox() { return $('#ai-chat-messages'); }

  function toggleAiChat(force) {
    chatState.open = force !== undefined ? force : !chatState.open;
    chatPanel().classList.toggle('hidden', !chatState.open);
    chatDivider().classList.toggle('hidden', !chatState.open);
    if (chatState.open) {
      const w = localStorage.getItem('deepcoder.chatWidth');
      if (w) chatPanel().style.width = w + 'px';
      syncChatToolbar();
      $('#ai-chat-input').focus();
      if (!chatState.messages.length) renderChatWelcome();
      checkChatKey();
    }
    refreshAllEditors();
  }

  function refreshAllEditors() {
    if (state.editor) state.editor.refresh();
    if (state.right.editor) state.right.editor.refresh();
  }

  function renderChatWelcome() {
    const box = chatMsgBox();
    box.innerHTML = '';
    const w = el('div', 'ai-chat-welcome');
    w.innerHTML = '<div class="big">💬</div>与 DeepSeek 对话，<br>可询问算法思路、调试代码、讲解题目。<br><br>点击下方输入框开始聊天（Enter 发送）。';
    box.appendChild(w);
  }

  function appendChatMsg(role, content) {
    const box = chatMsgBox();
    const wc = box.querySelector('.ai-chat-welcome');
    if (wc) wc.remove();
    const wrap = el('div', 'chat-msg ' + role);
    const bubble = el('div', 'chat-bubble');
    if (role === 'user') {
      bubble.textContent = content;
    } else {
      bubble.innerHTML = '<span class="chat-cursor"></span>';
    }
    wrap.appendChild(bubble);
    box.appendChild(wrap);
    box.scrollTop = box.scrollHeight;
    return { wrap, bubble };
  }

  // 更新 AI 气泡：工具调用 + thinking（可折叠，按开关显隐）+ 正文 Markdown
  function renderAiBubble(node) {
    const { bubble } = node;
    let html = '';
    // 工具调用记录（读写权限，参考 DeepChat 操作模式的 command 展示）
    if (chatState.tools.length) {
      html += '<div class="chat-tools">' + chatState.tools.map(t => {
        let errTip = '';
        if (t.ok === false && t.result) {
          errTip = '<div class="chat-tool-err">' + escapeHtml(String(t.result).slice(0, 200)) + '</div>';
        }
        return '<div class="chat-tool ' + (t.ok === false ? 'err' : 'ok') + '">' +
          '<span class="chat-tool-name">🔧 ' + escapeHtml(t.name) + '</span>' +
          ' <span class="chat-tool-args">' + escapeHtml(formatToolArgs(t)) + '</span>' +
          '<span class="chat-tool-badge ' + (t.ok === false ? 'err' : 'ok') + '">' +
          (t.ok === false ? '✗' : (t.ok === true ? '✓' : '…')) + '</span>' +
          errTip + '</div>';
      }).join('') + '</div>';
    }
    if (chatState.thinking && chatState.showThinking) {
      html += '<details class="chat-thinking" open>' +
        '<summary>🤔 思考过程</summary>' +
        '<div class="chat-thinking-body"></div></details>';
    }
    html += '<div class="chat-md">' + renderMarkdown(chatState.content) + '</div>';
    if (chatState.generating) html += '<span class="chat-cursor"></span>';
    bubble.innerHTML = html;
    const td = bubble.querySelector('.chat-thinking-body');
    if (td) td.textContent = chatState.thinking;
    chatMsgBox().scrollTop = chatMsgBox().scrollHeight;
  }

  // 消息底部附加 usage 统计（模型/思考深度/tokens，参考 DeepChat 操作模式）
  function appendChatUsage(node) {
    const u = chatState.usage;
    if (!u || !node || !node.wrap) return;
    const meta = el('div', 'chat-meta');
    const modelLabel = { 'deepseek-v4-flash': '⚡ Flash', 'deepseek-v4-pro': '🧬 Pro' }[chatState.model] || chatState.model;
    const pt = u.prompt_tokens || 0;
    const ct = u.completion_tokens || 0;
    meta.textContent = '🤖 ' + modelLabel + ' ｜ 思考深度: ' + (chatState.effort === 'max' ? 'Max' : 'High') +
      ' ｜ 输入 ' + pt + ' tokens ｜ 输出 ' + ct + ' tokens';
    node.wrap.appendChild(meta);
  }

  async function checkChatKey() {
    try {
      const r = await api('GET', '/api/chat/key');
      const st = $('#ai-chat-status');
      if (st) st.textContent = r.has_key ? '' : '未配置 Key';
    } catch (e) { /* ignore */ }
  }

  function setChatSendBtn() {
    const btn = $('#btn-ai-send');
    btn.textContent = chatState.generating ? '■' : '➤';
    btn.classList.toggle('generating', chatState.generating);
    btn.title = chatState.generating ? '停止生成' : '发送';
  }

  async function sendChatMessage() {
    const input = $('#ai-chat-input');
    const text = input.value.trim();
    if (!text || chatState.generating) return;
    chatState.messages.push({ role: 'user', content: text });
    input.value = '';
    appendChatMsg('user', text);
    const node = appendChatMsg('ai', '');

    chatState.generating = true;
    chatState.thinking = '';
    chatState.content = '';
    chatState.usage = null;
    chatState.tools = [];
    chatState.controller = new AbortController();
    setChatSendBtn();

    let resp;
    try {
      resp = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: chatState.messages,
          model: chatState.model,
          reasoning_effort: chatState.effort,
          thinking_enabled: chatState.showThinking,
        }),
        signal: chatState.controller.signal,
      });
    } catch (e) {
      if (e.name === 'AbortError') {
        finalizeChatMessage(node, true);
        return;
      }
      finishChatError(node, '网络错误：' + e.message);
      return;
    }
    if (!resp.ok) {
      let msg = '服务器响应异常（HTTP ' + resp.status + '）';
      let needKey = false;
      try {
        const j = await resp.json();
        if (j.error) msg = j.error;
        if (j.need_key) needKey = true;
      } catch (e) { /* ignore */ }
      finishChatError(node, msg, needKey);
      return;
    }
    // 流式读取 SSE
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const chunk = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          for (const line of chunk.split('\n')) {
            if (!line.startsWith('data:')) continue;
            const data = line.slice(5).trim();
            if (!data) continue;
            let evt;
            try { evt = JSON.parse(data); } catch (e) { continue; }
            if (evt.type === 'thinking') {
              chatState.thinking += evt.content || '';
              renderAiBubble(node);
            } else if (evt.type === 'content_delta') {
              chatState.content += evt.content || '';
              renderAiBubble(node);
            } else if (evt.type === 'usage') {
              chatState.usage = evt.usage || chatState.usage;
            } else if (evt.type === 'done') {
              if (evt.usage) chatState.usage = evt.usage;
            } else if (evt.type === 'tool_call') {
              chatState.tools.push({ name: evt.name, args: evt.arguments || {}, ok: null, result: '' });
              renderAiBubble(node);
            } else if (evt.type === 'tool_result') {
              const last = chatState.tools[chatState.tools.length - 1];
              if (last && last.name === evt.name) {
                last.ok = !!evt.ok;
                last.result = evt.content || '';
              }
              renderAiBubble(node);
            } else if (evt.type === 'error') {
              finishChatError(node, evt.content || '未知错误');
              return;
            }
          }
        }
      }
      finalizeChatMessage(node, false);
    } catch (e) {
      if (e.name === 'AbortError') {
        finalizeChatMessage(node, true);
      } else {
        finishChatError(node, '读取响应失败：' + e.message);
      }
    }
  }

  function finalizeChatMessage(node, stopped) {
    if (chatState.content || chatState.thinking) {
      chatState.messages.push({ role: 'assistant', content: chatState.content || '(无正文，仅思考)' });
    } else {
      finishChatError(node, stopped ? '已停止' : 'AI 未返回内容');
      return;
    }
    chatState.generating = false;
    chatState.controller = null;
    renderAiBubble(node);
    appendChatUsage(node);
    setChatSendBtn();
    $('#ai-chat-input').focus();
    if (stopped) addChatNote('⏹ 已停止生成');
  }

  function finishChatError(node, msg, needKey) {
    chatState.generating = false;
    chatState.controller = null;
    if (node && node.bubble) {
      node.bubble.textContent = '';
      node.bubble.textContent = msg;
      node.bubble.style.color = 'var(--red)';
    }
    setChatSendBtn();
    addChatNote('❌ ' + msg);
    if (needKey) showChatKeyModal();
  }

  function addChatNote(text) {
    const box = chatMsgBox();
    const note = el('div', 'chat-msg');
    note.style.cssText = 'align-self:center;font-size:12px;color:var(--fg-dim);';
    note.textContent = text;
    box.appendChild(note);
    box.scrollTop = box.scrollHeight;
  }

  function stopChatGeneration() {
    if (chatState.controller) {
      try { chatState.controller.abort(); } catch (e) { /* ignore */ }
    }
  }

  function clearChat() {
    if (chatState.generating) stopChatGeneration();
    chatState.messages = [];
    chatState.thinking = '';
    chatState.content = '';
    chatState.usage = null;
    chatState.tools = [];
    renderChatWelcome();
  }

  function showChatKeyModal() {
    showModal('设置 API Key', '' +
      '<div style="margin-bottom:8px;color:var(--fg-dim);font-size:13px;">' +
      '填写 DeepSeek API Key（保存到 DeepCoder/api-key.env）。<br>可在 <b>platform.deepseek.com</b> 获取。</div>' +
      '<input id="chat-key-input" type="password" style="width:100%;padding:6px 8px;background:var(--bg);' +
      'color:var(--fg);border:1px solid var(--border);border-radius:4px;" placeholder="sk-...">',
      [
        { label: '保存', primary: true, fn: async () => {
            const key = $('#chat-key-input').value.trim();
            if (!key) return;
            const r = await api('PUT', '/api/chat/key', { key });
            hideModal();
            if (r.ok) {
              addChatNote('🔑 API Key 已保存');
              checkChatKey();
            } else {
              addChatNote('❌ 保存失败：' + (r.error || '未知错误'));
            }
          } },
        { label: '取消', fn: hideModal },
      ]);
    setTimeout(() => { const i = $('#chat-key-input'); if (i) i.focus(); }, 50);
  }

  function bindAiChat() {
    $('#btn-ai-chat').addEventListener('click', () => toggleAiChat());
    $('#btn-ai-close').addEventListener('click', () => toggleAiChat(false));
    $('#btn-ai-clear').addEventListener('click', () => {
      if (chatState.messages.length === 0) return;
      showModal('清空对话', '确定清空当前对话吗？', [
        { label: '清空', primary: true, fn: () => { clearChat(); hideModal(); } },
        { label: '取消', fn: hideModal },
      ]);
    });
    $('#btn-ai-key').addEventListener('click', showChatKeyModal);
    $('#btn-ai-send').addEventListener('click', () => {
      if (chatState.generating) stopChatGeneration();
      else sendChatMessage();
    });
    const input = $('#ai-chat-input');
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        e.stopPropagation();
        sendChatMessage();
      }
    });
    input.addEventListener('input', () => {
      input.style.height = 'auto';
      input.style.height = Math.min(140, input.scrollHeight) + 'px';
    });
    // 模型 / 思考深度 / 思考开关（参考 DeepChat 操作模式，设置持久化到 localStorage）
    $('#ai-model').addEventListener('change', () => {
      chatState.model = $('#ai-model').value;
      saveChatSettings();
    });
    $('#ai-effort').addEventListener('change', () => {
      chatState.effort = $('#ai-effort').value;
      saveChatSettings();
    });
    $('#ai-thinking-toggle').addEventListener('click', () => {
      chatState.showThinking = !chatState.showThinking;
      syncChatToolbar();
      saveChatSettings();
      // 重新渲染最后一条 AI 气泡，应用思考区显隐
      const box = chatMsgBox();
      const last = box.querySelector('.chat-msg.ai:last-of-type');
      if (last) {
        const b = last.querySelector('.chat-bubble');
        if (b) renderAiBubble({ bubble: b });
      }
    });
    loadChatSettings();
    syncChatToolbar();
    // 拖拽调整聊天面板宽度（rAF 节流，模式与分屏分隔条一致）
    const divider = chatDivider();
    let dragging = null, raf = null, lastEv = null;
    divider.addEventListener('mousedown', (e) => {
      dragging = { startX: e.clientX, startW: chatPanel().getBoundingClientRect().width };
      document.body.classList.add('resizing');
      e.preventDefault();
    });
    window.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      lastEv = e;
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = null;
        if (!dragging || !lastEv) return;
        const w = Math.min(window.innerWidth * 0.7, Math.max(260, dragging.startW - (lastEv.clientX - dragging.startX)));
        chatPanel().style.width = w + 'px';
      });
    });
    window.addEventListener('mouseup', () => {
      if (!dragging) return;
      if (raf) { cancelAnimationFrame(raf); raf = null; }
      if (lastEv) {
        const w = Math.min(window.innerWidth * 0.7, Math.max(260, dragging.startW - (lastEv.clientX - dragging.startX)));
        chatPanel().style.width = w + 'px';
      }
      try {
        localStorage.setItem('deepcoder.chatWidth', String(Math.round(chatPanel().getBoundingClientRect().width)));
      } catch (err) { /* ignore */ }
      dragging = null; lastEv = null;
      document.body.classList.remove('resizing');
      refreshAllEditors();
    });
  }

  // ---------- 启动 ----------
  async function main() {
    initEditor();
    initActivePaneVisual();
    bind();
    bindAiChat();
    await loadTree();
    await loadCompileConfig();
    refreshSidepanel();
    try {
      localStorage.removeItem('deepcoder.tabs'); // 清理旧标签页缓存
    } catch (e) { /* ignore */ }
    const r = await api('GET', '/api/initial_file');
    if (r.file) {
      await openFile(r.file, true);
    }
    pollHackBadge();
  }

  main();
})();
