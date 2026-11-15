/* ==========================================================================
   Chat5050 · 共享桌面（二维画布）
   --------------------------------------------------------------------------
   与 app.js 的分工：
     app.js  负责身份、线性消息流、长轮询、权限、模式切换
     desk.js 负责相机、物件渲染、绘制、多人位置、增量同步
   两者只通过 window.Chat5050 这个桥接对象通信。
   ========================================================================== */

(() => {
  "use strict";

  const bridge = window.Chat5050;
  if (!bridge) return;

  const $ = (id) => document.getElementById(id);
  const el = {
    desk: $("desk"),
    viewport: $("deskViewport"),
    grid: $("deskGrid"),
    world: $("deskWorld"),
    items: $("deskItems"),
    views: $("deskViews"),
    cursors: $("deskCursors"),
    liveStroke: $("deskLiveStroke"),
    livePolyline: $("deskLivePolyline"),
    marquee: $("deskMarquee"),
    tools: $("deskTools"),
    options: $("deskOptions"),
    palette: $("deskPalette"),
    width: $("deskWidth"),
    widthLabel: $("deskWidthLabel"),
    hud: $("deskHud"),
    hint: $("deskHintText"),
    coords: $("deskCoords"),
    liveDot: $("deskLiveDot"),
    muted: $("deskMuted"),
    people: $("deskPeople"),
    zoomLabel: $("deskZoomLabel"),
    minimap: $("deskMinimap"),
    minimapCanvas: $("deskMinimapCanvas"),
    minimapCount: $("deskMinimapCount"),
    empty: $("deskEmpty"),
    editor: $("deskEditor"),
    editorInput: $("deskEditorInput"),
    editorSave: $("deskEditorSave"),
    editorCancel: $("deskEditorCancel"),
    fontBar: $("deskFontBar"),
    fontScope: $("deskFontScope"),
    fontValue: $("deskFontValue"),
    fontPresets: $("deskFontPresets"),
    fontMinus: $("deskFontMinus"),
    fontPlus: $("deskFontPlus"),
    fontClose: $("deskFontClose"),
    editorFontValue: $("deskEditorFontValue"),
    editorFontMinus: $("deskEditorFontMinus"),
    editorFontPlus: $("deskEditorFontPlus"),
    menu: $("deskMenu"),
    help: $("deskHelp"),
    helpBtn: $("deskHelpBtn"),
    drop: $("deskDrop"),
    drawer: $("deskDrawer"),
    drawerToggle: $("deskDrawerToggle"),
    drawerClose: $("deskDrawerClose"),
    drawerList: $("deskDrawerList"),
    drawerForm: $("deskDrawerForm"),
    drawerInput: $("deskDrawerInput"),
    drawerDot: $("deskDrawerDot"),
    fileInput: $("deskFileInput"),
    badge: $("deskBadge"),
    viewsToggle: $("deskViewsToggle"),
    backBtn: $("deskBackBtn"),
  };

  const PALETTE = [
    "#ff6b7a", "#f5b544", "#3ddc97",
    "#5b8cff", "#a78bfa", "#e9eefa",
  ];
  const CAMERA_KEY = "chat5050.desk.camera";
  const ZOOM_MIN = 0.15;
  const ZOOM_MAX = 4;
  const TOOLS = ["select", "hand", "marquee", "text", "pen", "eraser", "file"];
  // 每张文字便签独立字号（世界像素），逐张保存在物件 style.fontSize
  const NOTE_FONT_MIN = 10;
  const NOTE_FONT_MAX = 96;
  const NOTE_FONT_DEFAULT = 18;
  const NOTE_FONT_KEY = "chat5050.desk.noteFont";

  const state = {
    active: false,
    snapshotDone: false,
    connected: false,
    mode: bridge.getMode(),
    me: null,
    viewer: null,
    muted: false,
    admin: false,
    clientId: "d" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4),
    items: new Map(),
    cursors: new Map(),
    rev: 0,
    presenceRev: 0,
    camera: { x: 0, y: 0, zoom: 1 },
    vw: 1,
    vh: 1,
    pointer: { x: 0, y: 0 },
    pointerScreen: { x: 0, y: 0 },
    tool: "select",
    color: PALETTE[3],
    width: 3,
    noteFont: NOTE_FONT_DEFAULT,
    selection: new Set(),
    marquee: null,
    showViews: false,
    spaceDown: false,
    pointers: new Map(),
    pan: null,
    drag: null,
    resize: null,
    stroke: null,
    pinch: null,
    fly: null,
    editor: null,
    menuId: null,
    pendingFilePos: null,
    undo: [],
    dragDepth: 0,
    pollAbort: null,
    lastCursorSig: "",
    lastCursorAt: 0,
    saveCameraTimer: null,
    minimapTimer: null,
  };

  // ------------------------------------------------------------------------
  // 小工具
  // ------------------------------------------------------------------------

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  const round2 = (value) => Math.round(value * 100) / 100;
  const toast = (message, type = "error") => bridge.toast(message, type);

  function clampFont(value, fallback = NOTE_FONT_DEFAULT) {
    const size = Number(value);
    if (!Number.isFinite(size)) return fallback;
    return clamp(Math.round(size), NOTE_FONT_MIN, NOTE_FONT_MAX);
  }

  /** 旧物件没有 fontSize 字段时按 18px 渲染，与改造前 15px × 1.2 的视觉效果一致。 */
  function noteFontSize(item) {
    const raw = item && item.style && item.style.fontSize;
    const size = Number(raw);
    return Number.isFinite(size) && size >= NOTE_FONT_MIN && size <= NOTE_FONT_MAX
      ? size
      : NOTE_FONT_DEFAULT;
  }

  try {
    const saved = Number(localStorage.getItem(NOTE_FONT_KEY));
    if (saved >= NOTE_FONT_MIN && saved <= NOTE_FONT_MAX) state.noteFont = saved;
  } catch {
    /* 隐私模式下忽略 */
  }

  function hexToRgb(hex) {
    const match = /^#?([0-9a-f]{6})$/i.exec(String(hex || ""));
    if (!match) return { r: 91, g: 140, b: 255 };
    const value = parseInt(match[1], 16);
    return { r: (value >> 16) & 255, g: (value >> 8) & 255, b: value & 255 };
  }

  function blendHex(hex, base, ratio) {
    const a = hexToRgb(hex);
    const b = hexToRgb(base);
    const channel = (x, y) =>
      Math.round(x * ratio + y * (1 - ratio)).toString(16).padStart(2, "0");
    return `#${channel(a.r, b.r)}${channel(a.g, b.g)}${channel(a.b, b.b)}`;
  }

  function restoreCamera() {
    try {
      const raw = JSON.parse(localStorage.getItem(CAMERA_KEY) || "null");
      if (raw && Number.isFinite(raw.x) && Number.isFinite(raw.y) && Number.isFinite(raw.zoom)) {
        state.camera = {
          x: clamp(raw.x, -5e6, 5e6),
          y: clamp(raw.y, -5e6, 5e6),
          zoom: clamp(raw.zoom, ZOOM_MIN, ZOOM_MAX),
        };
      }
    } catch {
      /* 忽略损坏的本地相机记录 */
    }
  }

  function saveCamera() {
    clearTimeout(state.saveCameraTimer);
    state.saveCameraTimer = setTimeout(() => {
      try {
        localStorage.setItem(CAMERA_KEY, JSON.stringify(state.camera));
      } catch {
        /* 隐私模式下 localStorage 可能不可用 */
      }
    }, 350);
  }

  function viewportRect() {
    return el.viewport.getBoundingClientRect();
  }

  function toWorld(sx, sy) {
    const z = state.camera.zoom;
    return {
      x: (sx - state.vw / 2) / z + state.camera.x,
      y: (sy - state.vh / 2) / z + state.camera.y,
    };
  }

  function toScreen(x, y) {
    const z = state.camera.zoom;
    return {
      x: (x - state.camera.x) * z + state.vw / 2,
      y: (y - state.camera.y) * z + state.vh / 2,
    };
  }

  function localPoint(event) {
    const rect = viewportRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  function measure() {
    const rect = viewportRect();
    state.vw = Math.max(1, rect.width);
    state.vh = Math.max(1, rect.height);
  }

  // ------------------------------------------------------------------------
  // 相机
  // ------------------------------------------------------------------------

  function applyCamera() {
    const z = state.camera.zoom;
    const tx = state.vw / 2 - state.camera.x * z;
    const ty = state.vh / 2 - state.camera.y * z;
    el.world.style.transform = `translate(${tx}px, ${ty}px) scale(${z})`;

    // 网格步长随缩放自适应，保证屏幕上始终 18~90px 一格
    let step = 40;
    while (step * z < 18) step *= 5;
    while (step * z > 90) step /= 5;
    const minor = step * z;
    const major = minor * 5;
    el.grid.style.backgroundSize =
      `${minor}px ${minor}px, ${minor}px ${minor}px, ` +
      `${major}px ${major}px, ${major}px ${major}px`;
    el.grid.style.backgroundPosition = `${tx}px ${ty}px`;

    const inv = 1 / z;
    el.cursors.style.setProperty("--inv", String(inv));
    el.views.style.setProperty("--inv", String(inv));
    el.views.style.setProperty("--view-border", `${(1.6 * inv).toFixed(2)}px`);

    el.zoomLabel.textContent = `${Math.round(z * 100)}%`;
    el.coords.textContent = `${Math.round(state.pointer.x)}, ${Math.round(state.pointer.y)}`;
    saveCamera();
    scheduleMinimap();
  }

  function zoomAt(factor, sx, sy) {
    const before = toWorld(sx, sy);
    const z = clamp(state.camera.zoom * factor, ZOOM_MIN, ZOOM_MAX);
    state.camera.zoom = z;
    state.camera.x = before.x - (sx - state.vw / 2) / z;
    state.camera.y = before.y - (sy - state.vh / 2) / z;
    state.fly = null;
    applyCamera();
  }

  function flyTo(x, y, zoom) {
    const target = {
      x,
      y,
      zoom: clamp(zoom == null ? state.camera.zoom : zoom, ZOOM_MIN, ZOOM_MAX),
    };
    state.fly = {
      from: { ...state.camera },
      to: target,
      start: performance.now(),
      duration: 420,
    };
    requestAnimationFrame(stepFly);
  }

  function stepFly(now) {
    const fly = state.fly;
    if (!fly) return;
    const t = clamp((now - fly.start) / fly.duration, 0, 1);
    const eased = 1 - Math.pow(1 - t, 3);
    state.camera.x = fly.from.x + (fly.to.x - fly.from.x) * eased;
    state.camera.y = fly.from.y + (fly.to.y - fly.from.y) * eased;
    state.camera.zoom = fly.from.zoom + (fly.to.zoom - fly.from.zoom) * eased;
    applyCamera();
    if (t < 1) requestAnimationFrame(stepFly);
    else state.fly = null;
  }

  function contentBounds() {
    let minX = 0;
    let minY = 0;
    let maxX = 0;
    let maxY = 0;
    for (const item of state.items.values()) {
      minX = Math.min(minX, item.x);
      minY = Math.min(minY, item.y);
      maxX = Math.max(maxX, item.x + (item.w || 200));
      maxY = Math.max(maxY, item.y + (item.h || 90));
    }
    const halfW = state.vw / (2 * state.camera.zoom);
    const halfH = state.vh / (2 * state.camera.zoom);
    minX = Math.min(minX, state.camera.x - halfW);
    minY = Math.min(minY, state.camera.y - halfH);
    maxX = Math.max(maxX, state.camera.x + halfW);
    maxY = Math.max(maxY, state.camera.y + halfH);
    for (const cursor of state.cursors.values()) {
      const view = cursor.viewport;
      if (!view) continue;
      minX = Math.min(minX, view.x);
      minY = Math.min(minY, view.y);
      maxX = Math.max(maxX, view.x + view.w);
      maxY = Math.max(maxY, view.y + view.h);
    }
    const pad = Math.max(120, (maxX - minX) * 0.08, (maxY - minY) * 0.08);
    return { x: minX - pad, y: minY - pad, w: maxX - minX + pad * 2, h: maxY - minY + pad * 2 };
  }

  function fitAll() {
    const bounds = contentBounds();
    const zoom = clamp(
      Math.min((state.vw - 140) / bounds.w, (state.vh - 170) / bounds.h),
      ZOOM_MIN,
      1.5
    );
    flyTo(bounds.x + bounds.w / 2, bounds.y + bounds.h / 2, zoom);
  }

  // ------------------------------------------------------------------------
  // 物件渲染
  // ------------------------------------------------------------------------

  function noteStyle(item) {
    const raw = item.style && item.style.color;
    const color = /^#[0-9a-fA-F]{6}$/.test(String(raw || ""))
      ? raw
      : state.color;
    const note =
      item.style && item.style.note && /^#[0-9a-fA-F]{6}$/.test(item.style.note)
        ? item.style.note
        : blendHex(color, "#141c2b", 0.18);
    return { color, note };
  }

  function metaLine(item, node) {
    const meta = document.createElement("div");
    meta.className = "desk-item-meta";
    const who = document.createElement("b");
    who.textContent = item.nickname;
    const when = document.createElement("span");
    when.textContent = bridge.formatTime(item.created_at);
    meta.append(who, when);
    node.appendChild(meta);
  }

  function parseStroke(body) {
    try {
      const points = JSON.parse(body || "[]");
      return Array.isArray(points) ? points : [];
    } catch {
      return [];
    }
  }

  function makeItemNode(item) {
    const node = document.createElement("div");
    node.className = `desk-item desk-item--${item.kind}`;
    node.dataset.id = String(item.id);
    node.style.left = `${item.x}px`;
    node.style.top = `${item.y}px`;
    node.style.zIndex = String(Math.round(item.z || item.id));
    node.style.width = `${Math.max(1, item.w || (item.kind === "text" ? 260 : 1))}px`;
    if (item.h) node.style.height = `${item.h}px`;

    if (item.kind === "text") {
      const palette = noteStyle(item);
      node.style.setProperty("--note-bg", palette.note);
      node.style.setProperty("--note-accent", palette.color);
      node.style.setProperty("--note-font-size", `${noteFontSize(item)}px`);
      if (item.style && item.style.source && item.style.source.author) {
        const tag = document.createElement("span");
        tag.className = "desk-note-source";
        tag.textContent = `来自线性 · ${item.style.source.author}`;
        node.appendChild(tag);
      }
      const text = document.createElement("div");
      text.className = "desk-note-text";
      bridge.linkify(text, item.body || "");
      node.appendChild(text);
      const meta = document.createElement("div");
      meta.className = "desk-note-meta";
      const who = document.createElement("span");
      who.textContent = item.nickname;
      const when = document.createElement("span");
      when.textContent = bridge.formatTime(item.created_at);
      meta.append(who, when);
      node.appendChild(meta);
    } else if (item.kind === "image") {
      const img = document.createElement("img");
      img.src = item.attachment ? item.attachment.url : "";
      img.alt = item.attachment ? item.attachment.name : "";
      img.draggable = false;
      node.appendChild(img);
      if (item.attachment && item.attachment.download_url) {
        const link = document.createElement("a");
        link.className = "desk-image-download";
        link.href = item.attachment.download_url;
        link.setAttribute("download", item.attachment.name);
        link.title = `下载 ${item.attachment.name}`;
        link.textContent = "↓ 下载";
        node.appendChild(link);
      }
      metaLine(item, node);
    } else if (item.kind === "file") {
      const att = item.attachment || {};
      const ext = document.createElement("div");
      ext.className = "desk-file-ext";
      const name = String(att.name || "file");
      ext.textContent = (name.includes(".") ? name.split(".").pop() : "FILE").slice(0, 5);
      const main = document.createElement("div");
      main.className = "desk-file-main";
      const title = document.createElement("div");
      title.className = "desk-file-name";
      title.textContent = name;
      const sub = document.createElement("div");
      sub.className = "desk-file-sub";
      sub.textContent = att.size_human || "";
      main.append(title, sub);
      node.append(ext, main);
      if (att.download_url) {
        const link = document.createElement("a");
        link.className = "desk-file-link";
        link.href = att.download_url;
        link.setAttribute("download", name);
        link.title = `下载 ${name}`;
        link.textContent = "↓ 下载";
        node.appendChild(link);
      }
      if (item.body) {
        const caption = document.createElement("div");
        caption.className = "desk-note-source";
        caption.textContent = item.body.slice(0, 80);
        node.appendChild(caption);
      }
      metaLine(item, node);
    } else if (item.kind === "stroke") {
      const points = parseStroke(item.body);
      const color = (item.style && item.style.color) || "#f5b544";
      const width = Number(item.style && item.style.width) || 3;
      const w = Math.max(1, item.w || 1);
      const h = Math.max(1, item.h || 1);
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("width", String(w));
      svg.setAttribute("height", String(h));
      svg.style.position = "absolute";
      svg.style.left = "0";
      svg.style.top = "0";
      svg.style.overflow = "visible";
      if (points.length === 1) {
        const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        circle.setAttribute("cx", "0");
        circle.setAttribute("cy", "0");
        circle.setAttribute("r", String(Math.max(1, width / 2)));
        circle.setAttribute("fill", color);
        svg.appendChild(circle);
        const hit = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        hit.setAttribute("class", "desk-stroke-hit");
        hit.setAttribute("cx", "0");
        hit.setAttribute("cy", "0");
        hit.setAttribute("r", String(Math.max(10, width)));
        hit.setAttribute("fill", "transparent");
        svg.appendChild(hit);
      } else {
        const coords = points
          .map((point) => `${Number(point[0]) || 0},${Number(point[1]) || 0}`)
          .join(" ");
        const hit = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
        hit.setAttribute("class", "desk-stroke-hit");
        hit.setAttribute("points", coords);
        hit.setAttribute("stroke", "transparent");
        hit.setAttribute("stroke-width", String(Math.max(14, width * 3)));
        hit.setAttribute("fill", "none");
        const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
        line.setAttribute("class", "desk-stroke-line");
        line.setAttribute("points", coords);
        line.setAttribute("stroke", color);
        line.setAttribute("stroke-width", String(width));
        line.setAttribute("stroke-linecap", "round");
        line.setAttribute("stroke-linejoin", "round");
        line.setAttribute("fill", "none");
        svg.append(hit, line);
      }
      node.appendChild(svg);
    }
    return node;
  }

  function itemNode(id) {
    return el.items.querySelector(`.desk-item[data-id="${id}"]`);
  }

  function upsertItem(item) {
    if (!item || typeof item.id !== "number") return;
    if (item.deleted) {
      state.items.delete(item.id);
      const old = itemNode(item.id);
      if (old) old.remove();
      if (state.selection.has(item.id)) {
        state.selection.delete(item.id);
        applySelection();
      }
      return;
    }
    const previous = state.items.get(item.id);
    // 拖动过程中可能有多个 PATCH 并发返回，旧版本不能覆盖新位置
    if (previous && typeof item.rev === "number" && item.rev < previous.rev) return;
    state.items.set(item.id, item);

    // 自己正在拖动 / 缩放时不要被服务端回包打断，等松手再对齐
    const busy =
      (state.drag && state.drag.origins && state.drag.origins.has(item.id)) ||
      (state.resize && state.resize.id === item.id);
    const old = itemNode(item.id);
    if (busy && old) return;

    const node = makeItemNode(item);
    if (state.selection.has(item.id)) {
      node.classList.add("is-selected");
      if (state.selection.size === 1 && item.kind !== "stroke") addResizeHandle(node, item);
    }
    if (old) old.replaceWith(node);
    else el.items.appendChild(node);

    // 便签不存高度，插入后量一次供小地图 / 视野计算使用
    if (item.kind === "text" && !item.h) {
      item.h = node.offsetHeight || 90;
    }
    if (!previous) scheduleMinimap();
  }

  function applyItems(list) {
    if (!Array.isArray(list) || !list.length) return;
    for (const item of list) upsertItem(item);
    for (const item of list) {
      if (typeof item.rev === "number" && item.rev > state.rev) state.rev = item.rev;
    }
    updateEmptyState();
    updateBadge();
    el.minimapCount.textContent = `${state.items.size} 个物件`;
    scheduleMinimap();
    updateFontBar();
  }

  function addResizeHandle(node, item) {
    if (item.kind === "stroke") return;
    if (node.querySelector(".desk-resize")) return;
    const handle = document.createElement("div");
    handle.className = "desk-resize";
    handle.dataset.resize = "1";
    node.appendChild(handle);
  }

  // ------------------------------------------------------------------------
  // 选择集：单选 / Ctrl(⌘) 加减选中 / 框选批量选中
  // ------------------------------------------------------------------------

  function setToolHint() {
    if (state.tool === "text") {
      el.hint.textContent = "点击空白处新建便签 · 双击便签可编辑";
    } else if (state.tool === "pen") {
      el.hint.textContent = "按住拖动即可涂鸦 · 右侧调色板换颜色与粗细";
    } else if (state.tool === "eraser") {
      el.hint.textContent = "点一下物件即可删除（仅限自己或管理员）";
    } else if (state.tool === "file") {
      el.hint.textContent = "点击空白选择文件，或直接把文件拖进来";
    } else if (state.tool === "marquee") {
      el.hint.textContent = "拖出矩形框选物件 · Ctrl/⌘ + 拖动可追加选中";
    } else if (state.tool === "select") {
      el.hint.textContent = "滚轮平移 · Ctrl/⌘ + 点击多选 · Shift + 拖动框选";
    } else {
      el.hint.textContent = "滚轮平移 · Ctrl/⌘ + 滚轮缩放 · 双击空白写便签";
    }
  }

  function updateSelectionHint() {
    const count = state.selection.size;
    el.hint.classList.toggle("is-selection", count > 1);
    if (count > 1) {
      el.hint.textContent = `已选 ${count} 个物件 · 拖动整组移动 · Delete 批量删除`;
    } else {
      setToolHint();
    }
  }

  function applySelection() {
    const single = state.selection.size === 1 ? Array.from(state.selection)[0] : null;
    for (const node of el.items.children) {
      const id = Number(node.dataset.id);
      const selected = state.selection.has(id);
      node.classList.toggle("is-selected", selected);
      const handle = node.querySelector(".desk-resize");
      if (handle) handle.remove();
      // 多选时不显示缩放手柄，避免把整组拖动误当成缩放
      if (selected && single === id) {
        const item = state.items.get(id);
        if (item && item.kind !== "stroke") addResizeHandle(node, item);
      }
    }
    updateSelectionHint();
    updateFontBar();
  }

  function setSelection(ids) {
    state.selection = new Set(
      (ids || [])
        .map(Number)
        .filter((id) => Number.isFinite(id) && state.items.has(id))
    );
    applySelection();
  }

  function selectItem(id) {
    setSelection(id == null ? [] : [id]);
  }

  function toggleSelection(id) {
    if (state.selection.has(id)) state.selection.delete(id);
    else state.selection.add(id);
    applySelection();
  }

  function selectionIds() {
    return Array.from(state.selection);
  }

  function permitted(item) {
    return Boolean(item && (item.nickname === state.me || state.admin));
  }

  // ------------------------------------------------------------------------
  // 每张文字便签的独立字号
  // ------------------------------------------------------------------------

  /** 当前选中里「作者/管理员可编辑」的文字便签，其它物件不参与字号调整。 */
  function fontTargets() {
    return selectionIds().filter((id) => {
      const item = state.items.get(id);
      return item && item.kind === "text" && permitted(item);
    });
  }

  /** 本地先预览，避免等网络往返才有反馈。 */
  function previewFontSize(ids, size) {
    for (const id of ids) {
      const item = state.items.get(id);
      if (!item) continue;
      state.items.set(id, {
        ...item,
        style: { ...(item.style || {}), fontSize: size },
      });
      const node = itemNode(id);
      if (node) node.style.setProperty("--note-font-size", `${size}px`);
    }
  }

  function updateFontBar() {
    if (!el.fontBar) return;
    const ids = fontTargets();
    const visible =
      bridge.getMode() === "desk" && !state.muted && !state.editor && ids.length > 0;
    el.fontBar.hidden = !visible;
    if (!visible) return;
    const sizes = ids.map((id) => noteFontSize(state.items.get(id)));
    const same = sizes.every((size) => size === sizes[0]);
    el.fontScope.textContent = `${ids.length} 张`;
    if (document.activeElement !== el.fontValue) {
      el.fontValue.value = same ? String(sizes[0]) : "";
      el.fontValue.placeholder = same ? "" : "多种";
    }
    for (const button of el.fontPresets.querySelectorAll("[data-size]")) {
      button.classList.toggle(
        "is-active",
        same && Number(button.dataset.size) === sizes[0]
      );
    }
  }

  /** 单选走 PATCH，多选走批量接口；服务端把字号合并进各自的 style。 */
  async function setItemFontSize(ids, value) {
    const targets = (ids && ids.length ? ids : fontTargets())
      .map(Number)
      .filter((id) => {
        const item = state.items.get(id);
        return item && item.kind === "text" && permitted(item);
      });
    if (!targets.length) return null;
    const first = targets[0];
    const size = clampFont(value, noteFontSize(state.items.get(first)));
    previewFontSize(targets, size);
    updateFontBar();
    if (targets.length === 1) {
      const item = state.items.get(first);
      return patchItem(first, {
        style: { ...(item.style || {}), fontSize: size },
      });
    }
    const result = await batchItems(
      "font_size",
      targets.map((id) => ({ id })),
      { fontSize: size },
      true
    );
    if (result) return result;
    // 批量接口偶发失败（例如数据库瞬时只读）时退化成逐个更新，保证字号一定落库
    for (const id of targets) {
      const item = state.items.get(id);
      if (item) {
        await patchItem(id, { style: { ...(item.style || {}), fontSize: size } });
      }
    }
    return null;
  }

  function stepSelectionFont(delta) {
    const ids = fontTargets();
    if (!ids.length) return;
    setItemFontSize(ids, noteFontSize(state.items.get(ids[0])) + delta);
  }

  function setEditorFontSize(value) {
    if (!state.editor) return;
    const size = clampFont(value, state.editor.fontSize);
    state.editor.fontSize = size;
    el.editorFontValue.value = String(size);
    el.editorInput.style.fontSize = `${size}px`;
  }

  // ------------------------------------------------------------------------
  // 在线位置：光标 / 视野 / 成员条 / 角标
  // ------------------------------------------------------------------------

  function cursorNode(cursor) {
    const node = document.createElement("div");
    node.className = "desk-cursor";
    node.dataset.cid = cursor.client_id;
    node.style.setProperty("--cursor-color", cursor.color || "#5b8cff");
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 20 24");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M2 1.5 2 20l4.6-4.7 3.1 6.4 3-1.5-3.1-6.3H17L2 1.5Z");
    path.setAttribute("fill", cursor.color || "#5b8cff");
    path.setAttribute("stroke", "#0a1020");
    path.setAttribute("stroke-width", "1.2");
    svg.appendChild(path);
    const label = document.createElement("div");
    label.className = "desk-cursor-label";
    label.textContent = cursor.nickname || "?";
    node.append(svg, label);
    return node;
  }

  function renderCursors() {
    const seen = new Set();
    for (const cursor of state.cursors.values()) {
      seen.add(cursor.client_id);
      let node = el.cursors.querySelector(`[data-cid="${cursor.client_id}"]`);
      if (!node) {
        node = cursorNode(cursor);
        el.cursors.appendChild(node);
      }
      node.style.left = `${cursor.x}px`;
      node.style.top = `${cursor.y}px`;
      const label = node.querySelector(".desk-cursor-label");
      if (label) {
        label.replaceChildren(document.createTextNode(cursor.nickname || "?"));
        if (cursor.tool && cursor.tool !== "select") {
          const tool = document.createElement("span");
          tool.className = "desk-cursor-tool";
          tool.textContent = toolName(cursor.tool);
          label.appendChild(tool);
        }
      }
    }
    for (const child of Array.from(el.cursors.children)) {
      if (!seen.has(child.dataset.cid)) child.remove();
    }
  }

  function toolName(tool) {
    return (
      { hand: "平移", text: "便签", pen: "画笔", eraser: "橡皮", file: "文件" }[tool] || ""
    );
  }

  function renderViews() {
    el.views.replaceChildren();
    if (!state.showViews) return;
    for (const cursor of state.cursors.values()) {
      const view = cursor.viewport;
      if (!view) continue;
      const node = document.createElement("div");
      node.className = "desk-view";
      node.style.setProperty("--view-color", cursor.color || "#5b8cff");
      node.style.left = `${view.x}px`;
      node.style.top = `${view.y}px`;
      node.style.width = `${view.w}px`;
      node.style.height = `${view.h}px`;
      const label = document.createElement("span");
      label.className = "desk-view-label";
      label.textContent = `${cursor.nickname} 的视角 · ${Math.round(cursor.zoom * 100)}%`;
      node.appendChild(label);
      el.views.appendChild(node);
    }
  }

  function distinctPeople() {
    const people = new Map();
    for (const cursor of state.cursors.values()) {
      const current = people.get(cursor.nickname);
      if (!current || cursor.idle < current.idle) people.set(cursor.nickname, cursor);
    }
    return Array.from(people.values());
  }

  function renderPeople() {
    const people = distinctPeople();
    el.people.replaceChildren();
    if (!people.length) {
      const empty = document.createElement("span");
      empty.className = "desk-people-empty";
      empty.textContent = "桌面上暂时只有你";
      el.people.appendChild(empty);
    } else {
      for (const person of people.slice(0, 8)) {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "desk-person";
        chip.title = `跳到 ${person.nickname} 的视角`;
        const avatar = document.createElement("span");
        avatar.className = "desk-person-avatar";
        avatar.style.background = person.color || "#5b8cff";
        avatar.textContent = bridge.initial(person.nickname);
        const name = document.createElement("span");
        name.textContent = person.nickname;
        chip.append(avatar, name);
        chip.addEventListener("click", () => {
          flyTo(person.cx, person.cy, person.zoom);
          toast(`已跳到 ${person.nickname} 的视角`, "ok");
        });
        el.people.appendChild(chip);
      }
    }
    state.peopleCount = people.length;
    el.liveDot.classList.toggle("is-live", Boolean(state.connected && (people.length || state.active)));
    updateBadge();
  }

  function updateBadge() {
    const count = state.items.size;
    const people = state.cursors.size ? distinctPeople().length : 0;
    if (!count && !people) {
      el.badge.hidden = true;
      return;
    }
    el.badge.hidden = false;
    el.badge.textContent = people ? `${count} · ${people}人` : String(count);
    el.badge.title = `${count} 个物件，${people} 人正在桌面`;
    el.badge.classList.toggle("is-live", people > 0);
  }

  function applyPresence(list) {
    if (!Array.isArray(list)) return;
    const next = new Map();
    for (const cursor of list) {
      if (!cursor || cursor.client_id === state.clientId) continue;
      next.set(cursor.client_id, cursor);
    }
    state.cursors = next;
    renderCursors();
    renderViews();
    renderPeople();
    scheduleMinimap();
  }

  // ------------------------------------------------------------------------
  // 小地图
  // ------------------------------------------------------------------------

  function scheduleMinimap() {
    if (state.minimapTimer) return;
    state.minimapTimer = requestAnimationFrame(() => {
      state.minimapTimer = null;
      drawMinimap();
    });
  }

  function drawMinimap() {
    const canvas = el.minimapCanvas;
    const ctx = canvas.getContext("2d");
    const W = canvas.width;
    const H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    const bounds = contentBounds();
    const scale = Math.min(W / bounds.w, H / bounds.h);
    const offX = (W - bounds.w * scale) / 2 - bounds.x * scale;
    const offY = (H - bounds.h * scale) / 2 - bounds.y * scale;
    const mx = (x) => x * scale + offX;
    const my = (y) => y * scale + offY;
    state.mapTransform = { scale, offX, offY };

    // 原点
    ctx.strokeStyle = "rgba(255,255,255,0.25)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(mx(0) - 4, my(0));
    ctx.lineTo(mx(0) + 4, my(0));
    ctx.moveTo(mx(0), my(0) - 4);
    ctx.lineTo(mx(0), my(0) + 4);
    ctx.stroke();

    for (const item of state.items.values()) {
      const w = Math.max(3, (item.w || 180) * scale);
      const h = Math.max(2, (item.h || 80) * scale);
      ctx.fillStyle = item.kind === "stroke"
        ? (item.style && item.style.color) || "#f5b544"
        : item.color || "#5b8cff";
      ctx.globalAlpha = 0.62;
      ctx.fillRect(mx(item.x), my(item.y), w, h);
      ctx.globalAlpha = 1;
    }

    for (const cursor of state.cursors.values()) {
      const view = cursor.viewport;
      if (!view) continue;
      ctx.strokeStyle = cursor.color || "#5b8cff";
      ctx.globalAlpha = 0.75;
      ctx.lineWidth = 1;
      ctx.strokeRect(mx(view.x), my(view.y), view.w * scale, view.h * scale);
      ctx.globalAlpha = 1;
    }

    const halfW = state.vw / (2 * state.camera.zoom);
    const halfH = state.vh / (2 * state.camera.zoom);
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 1.6;
    ctx.fillStyle = "rgba(255,255,255,0.06)";
    ctx.fillRect(
      mx(state.camera.x - halfW), my(state.camera.y - halfH),
      halfW * 2 * scale, halfH * 2 * scale
    );
    ctx.strokeRect(
      mx(state.camera.x - halfW), my(state.camera.y - halfH),
      halfW * 2 * scale, halfH * 2 * scale
    );
  }

  function minimapToWorld(event) {
    if (!state.mapTransform) return null;
    const rect = el.minimapCanvas.getBoundingClientRect();
    const x = ((event.clientX - rect.left) / rect.width) * el.minimapCanvas.width;
    const y = ((event.clientY - rect.top) / rect.height) * el.minimapCanvas.height;
    const { scale, offX, offY } = state.mapTransform;
    return { x: (x - offX) / scale, y: (y - offY) / scale };
  }

  // ------------------------------------------------------------------------
  // 同步：快照 + 增量长轮询 + 光标心跳
  // ------------------------------------------------------------------------

  async function loadSnapshot() {
    let before = Number.MAX_SAFE_INTEGER;
    for (let page = 0; page < 30; page += 1) {
      const data = await bridge.api(
        `/api/desk/snapshot?before_id=${before}&limit=400`
      );
      if (page === 0) state.rev = Math.max(state.rev, data.rev || 0);
      state.presenceRev = Math.max(state.presenceRev, data.presence_rev || 0);
      applyItems(data.items);
      applyPresence(data.presence);
      if (!data.has_more || !data.items.length) break;
      before = Math.min(...data.items.map((item) => item.id));
    }
    state.snapshotDone = true;
    updateEmptyState();
  }

  async function eventLoop() {
    for (;;) {
      if (!state.active) {
        await sleep(600);
        continue;
      }
      if (document.hidden) {
        await sleep(1200);
        continue;
      }
      const controller = new AbortController();
      state.pollAbort = controller;
      const timer = setTimeout(() => controller.abort(), 30000);
      try {
        const data = await bridge.api(
          `/api/desk/events?since_rev=${encodeURIComponent(state.rev)}` +
          `&since_presence=${encodeURIComponent(state.presenceRev)}` +
          "&wait=20&limit=300",
          { signal: controller.signal }
        );
        clearTimeout(timer);
        state.connected = true;
        applyItems(data.items);
        if (typeof data.rev === "number" && !data.has_more) {
          state.rev = Math.max(state.rev, data.rev);
        }
        if (typeof data.presence_rev === "number") {
          state.presenceRev = Math.max(state.presenceRev, data.presence_rev);
        }
        applyPresence(data.presence);
        renderPeople();
        if (data.has_more) continue;
      } catch (err) {
        clearTimeout(timer);
        if (err && err.name === "AbortError") {
          await sleep(120);
          continue;
        }
        state.connected = false;
        el.liveDot.classList.remove("is-live");
        if (err && err.status === 401) {
          state.active = false;
          await sleep(2000);
        } else {
          await sleep(1800);
        }
      } finally {
        if (state.pollAbort === controller) state.pollAbort = null;
      }
    }
  }

  function heartbeat(force = false) {
    if (!state.active || bridge.getMode() !== "desk" || document.hidden) return;
    const payload = {
      client_id: state.clientId,
      x: round2(state.pointer.x),
      y: round2(state.pointer.y),
      cx: round2(state.camera.x),
      cy: round2(state.camera.y),
      zoom: state.camera.zoom,
      vw: state.vw / state.camera.zoom,
      vh: state.vh / state.camera.zoom,
      tool: state.tool,
    };
    const signature = [
      payload.x, payload.y, payload.cx, payload.cy,
      payload.zoom.toFixed(2), payload.tool,
    ].join("|");
    const now = Date.now();
    if (!force && signature === state.lastCursorSig && now - state.lastCursorAt < 5000) return;
    state.lastCursorSig = signature;
    state.lastCursorAt = now;
    bridge.api("/api/desk/cursor", {
      method: "POST",
      body: JSON.stringify(payload),
    }).catch(() => {});
  }

  function leaveDesk() {
    bridge.api("/api/desk/leave", {
      method: "POST",
      body: JSON.stringify({ client_id: state.clientId }),
    }).catch(() => {});
  }

  // ------------------------------------------------------------------------
  // 写入操作
  // ------------------------------------------------------------------------

  function checkWritable() {
    if (state.muted) {
      toast("你已被禁言，暂时只能浏览桌面", "error");
      return false;
    }
    return true;
  }

  async function createItem(payload, file) {
    if (!checkWritable()) return null;
    let options;
    if (file) {
      const form = new FormData();
      form.append("meta", JSON.stringify(payload));
      form.append("file", file, file.name || "file");
      options = { method: "POST", body: form };
    } else {
      options = { method: "POST", body: JSON.stringify(payload) };
    }
    try {
      const data = await bridge.api("/api/desk/items", options);
      applyItems([data.item]);
      if (typeof data.rev === "number") state.rev = Math.max(state.rev, data.rev);
      selectItem(data.item.id);
      return data.item;
    } catch (err) {
      toast(err.message || "放置失败", "error");
      return null;
    }
  }

  async function patchItem(id, payload, quiet = false) {
    try {
      const data = await bridge.api(`/api/desk/items/${id}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
      applyItems([data.item]);
      if (typeof data.rev === "number") state.rev = Math.max(state.rev, data.rev);
      return data.item;
    } catch (err) {
      if (!quiet) toast(err.message || "更新失败", "error");
      return null;
    }
  }

  async function deleteItem(id) {
    if (!checkWritable()) return;
    const item = state.items.get(id);
    if (!item) return;
    try {
      const data = await bridge.api(`/api/desk/items/${id}`, { method: "DELETE" });
      applyItems([data.item]);
      if (typeof data.rev === "number") state.rev = Math.max(state.rev, data.rev);
      state.undo.push({ id, kind: item.kind });
      if (state.undo.length > 20) state.undo.shift();
      toast("已删除 · Ctrl/⌘ + Z 可撤销", "ok");
    } catch (err) {
      toast(err.message || "删除失败", "error");
    }
  }

  /** 一次请求批量移动 / 删除；服务端共用一个 rev，增量同步只需拉一次。 */
  async function batchItems(action, entries, extra = {}, quiet = false) {
    if (!checkWritable() || !entries.length) return null;
    try {
      const data = await bridge.api("/api/desk/items/batch", {
        method: "POST",
        body: JSON.stringify({ action, items: entries, ...extra }),
      });
      applyItems(data.items || []);
      if (typeof data.rev === "number") state.rev = Math.max(state.rev, data.rev);
      return data;
    } catch (err) {
      if (!quiet) toast(err.message || "批量操作失败", "error");
      return null;
    }
  }

  async function deleteSelection() {
    if (!checkWritable() || !state.selection.size) return;
    const ids = selectionIds();
    const allowed = ids.filter((id) => permitted(state.items.get(id)));
    if (allowed.length < ids.length) toast("已跳过他人放置的物件", "error");
    if (!allowed.length) return;
    const data = await batchItems("delete", allowed.map((id) => ({ id })));
    if (!data) return;
    for (const id of allowed) {
      const item = state.items.get(id);
      state.undo.push({ id, kind: item ? item.kind : "text" });
    }
    if (state.undo.length > 20) state.undo.splice(0, state.undo.length - 20);
    setSelection([]);
    toast(`已删除 ${data.changed} 个物件 · Ctrl/⌘ + Z 可撤销`, "ok");
  }

  async function undoDelete() {
    const last = state.undo.pop();
    if (!last) return;
    try {
      const data = await bridge.api(`/api/desk/items/${last.id}/restore`, {
        method: "POST",
      });
      applyItems([data.item]);
      if (typeof data.rev === "number") state.rev = Math.max(state.rev, data.rev);
      selectItem(last.id);
      toast("已恢复", "ok");
    } catch (err) {
      toast(err.message || "恢复失败", "error");
    }
  }

  async function sendToLinear(item) {
    if (!checkWritable()) return;
    if (item.kind === "stroke") {
      toast("笔画请转成文字后再发到线性流", "error");
      return;
    }
    const payload = {};
    if (item.attachment) payload.attachment_id = item.attachment.id;
    if (item.kind === "text") payload.text = item.body;
    else if (item.body) payload.text = item.body;
    else if (item.attachment) payload.text = "";
    else {
      toast("这个物件没有可发送的内容", "error");
      return;
    }
    try {
      await bridge.api("/api/messages", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      toast("已发到线性流", "ok");
      openDrawer();
    } catch (err) {
      toast(err.message || "发送失败", "error");
    }
  }

  function nextFreeSpot() {
    // 以相机中心为起点做一圈圈搜索，找到不与现有物件重叠的位置
    const candidates = [];
    for (let ring = 0; ring < 7; ring += 1) {
      for (let dy = -ring; dy <= ring; dy += 1) {
        for (let dx = -ring; dx <= ring; dx += 1) {
          if (Math.max(Math.abs(dx), Math.abs(dy)) !== ring) continue;
          candidates.push({
            x: round2(state.camera.x - 140 + dx * 300),
            y: round2(state.camera.y - 90 + dy * 190),
          });
        }
      }
    }
    for (const spot of candidates) {
      let free = true;
      for (const item of state.items.values()) {
        if (
          spot.x < item.x + (item.w || 200) + 24 &&
          spot.x + 280 + 24 > item.x &&
          spot.y < item.y + (item.h || 90) + 24 &&
          spot.y + 120 + 24 > item.y
        ) {
          free = false;
          break;
        }
      }
      if (free) return spot;
    }
    return candidates[0];
  }

  async function pinMessage(message) {
    if (!message) return;
    const spot = nextFreeSpot();
    const source = {
      from: "linear",
      author: message.nickname,
      message_id: message.id,
    };
    let payload;
    if (message.attachment) {
      payload = {
        kind: message.attachment.is_image ? "image" : "file",
        x: spot.x,
        y: spot.y,
        w: message.attachment.is_image ? 320 : 260,
        h: message.attachment.is_image ? 220 : 96,
        body: message.text || "",
        attachment_id: message.attachment.id,
        style: { color: message.color, source },
      };
    } else {
      payload = {
        kind: "text",
        x: spot.x,
        y: spot.y,
        w: 280,
        body: message.text || "（空消息）",
        style: {
          color: message.color,
          note: blendHex(message.color, "#141c2b", 0.18),
          source,
        },
      };
    }
    const item = await createItem(payload);
    if (item) {
      bridge.setMode("desk");
      flyTo(item.x + 140, item.y + 70, Math.max(state.camera.zoom, 0.8));
      toast("已钉到桌面", "ok");
    }
  }

  // ------------------------------------------------------------------------
  // 文件放置
  // ------------------------------------------------------------------------

  function imageSize(file) {
    return new Promise((resolve) => {
      if (!file.type || !file.type.startsWith("image/")) {
        resolve(null);
        return;
      }
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        URL.revokeObjectURL(url);
        resolve({ w: img.naturalWidth || 320, h: img.naturalHeight || 220 });
      };
      img.onerror = () => {
        URL.revokeObjectURL(url);
        resolve(null);
      };
      img.src = url;
    });
  }

  async function placeFiles(files, at) {
    if (!checkWritable()) return;
    const list = Array.from(files || []);
    if (!list.length) return;
    for (let index = 0; index < list.length; index += 1) {
      const file = list[index];
      const dims = await imageSize(file);
      const isImage = Boolean(dims);
      let w = 260;
      let h = 96;
      if (dims) {
        w = clamp(dims.w, 120, 420);
        h = clamp((w * dims.h) / dims.w, 80, 420);
      }
      const spot = {
        x: round2((at ? at.x : state.camera.x) + index * 26),
        y: round2((at ? at.y : state.camera.y) + index * 26),
      };
      await createItem(
        {
          kind: isImage ? "image" : "file",
          x: spot.x,
          y: spot.y,
          w,
          h,
          body: "",
          style: { color: state.color },
        },
        file
      );
    }
    toast(`已放置 ${list.length} 个文件`, "ok");
  }

  // ------------------------------------------------------------------------
  // 工具 / 编辑器 / 菜单
  // ------------------------------------------------------------------------

  function setTool(tool) {
    if (!TOOLS.includes(tool)) return;
    if (state.muted && !["select", "hand", "marquee"].includes(tool)) {
      toast("已被禁言，只能浏览桌面", "error");
      return;
    }
    state.tool = tool;
    for (const button of el.tools.querySelectorAll("[data-tool]")) {
      button.classList.toggle("is-active", button.dataset.tool === tool);
    }
    el.viewport.dataset.tool = tool;
    closeEditor();
    closeMenu();
    updateSelectionHint();
  }

  function applyMuteState(viewer) {
    state.viewer = viewer || null;
    state.muted = Boolean(viewer && viewer.muted);
    state.admin = Boolean(viewer && viewer.is_admin);
    el.muted.hidden = !state.muted;
    for (const button of el.tools.querySelectorAll("[data-tool]")) {
      if (["select", "hand", "marquee"].includes(button.dataset.tool)) continue;
      button.classList.toggle("is-disabled", state.muted);
    }
    if (state.muted) {
      setTool("select");
      closeEditor();
    }
    updateFontBar();
  }

  function openEditor(world, screen, item) {
    if (!checkWritable()) return;
    if (item && !permitted(item)) {
      toast("只有作者或管理员能编辑这张便签", "error");
      return;
    }
    state.editor = {
      item: item || null,
      world: { ...world },
      fontSize: item ? noteFontSize(item) : state.noteFont,
    };
    el.editorInput.value = item ? item.body || "" : "";
    setEditorFontSize(state.editor.fontSize);
    updateFontBar();
    const left = clamp(screen.x + 8, 8, Math.max(8, state.vw - 300));
    const top = clamp(screen.y + 8, 54, Math.max(54, state.vh - 200));
    el.editor.style.left = `${left}px`;
    el.editor.style.top = `${top}px`;
    el.editor.hidden = false;
    el.editorSave.textContent = item ? "保存修改" : "放到桌面";
    setTimeout(() => {
      el.editorInput.focus();
      el.editorInput.setSelectionRange(el.editorInput.value.length, el.editorInput.value.length);
    }, 30);
  }

  function closeEditor() {
    if (!state.editor) return;
    state.editor = null;
    el.editor.hidden = true;
    el.editorInput.style.fontSize = "";
    updateFontBar();
  }

  async function saveEditor() {
    const editor = state.editor;
    if (!editor) return;
    const text = el.editorInput.value.replace(/\r\n/g, "\n");
    closeEditor();
    if (!text.trim()) {
      if (editor.item) toast("便签内容不能为空", "error");
      return;
    }
    if (editor.item) {
      await patchItem(editor.item.id, {
        body: text,
        style: { ...(editor.item.style || {}), fontSize: editor.fontSize },
      });
      return;
    }
    state.noteFont = editor.fontSize;
    try {
      localStorage.setItem(NOTE_FONT_KEY, String(state.noteFont));
    } catch {
      /* 隐私模式下本次会话内仍生效 */
    }
    const palette = noteStyle({ style: { color: state.color } });
    await createItem({
      kind: "text",
      x: round2(editor.world.x),
      y: round2(editor.world.y),
      w: 260,
      body: text,
      style: {
        color: palette.color,
        note: palette.note,
        fontSize: editor.fontSize,
      },
    });
    setTool("select");
  }

  /** 触发附件下载。链接必须真实挂到 DOM 上，脱离文档的 a.click() 在部分浏览器会被忽略。 */
  function downloadAttachment(att) {
    if (!att || !att.download_url) return;
    const link = document.createElement("a");
    link.href = att.download_url;
    link.setAttribute("download", att.name || "file");
    link.style.display = "none";
    document.body.appendChild(link);
    link.click();
    setTimeout(() => link.remove(), 0);
  }

  function closeMenu() {
    el.menu.hidden = true;
    state.menuId = null;
  }

  function openMenu(screen, item) {
    closeMenu();
    state.menuId = item.id;
    el.menu.replaceChildren();
    const multi = state.selection.size > 1 && state.selection.has(item.id);
    const info = document.createElement("div");
    info.className = "desk-menu-info";
    info.textContent = multi
      ? `已选中 ${state.selection.size} 个物件`
      : `${item.nickname} · ${bridge.formatTime(item.created_at)} · #${item.id}`;
    el.menu.appendChild(info);

    const canEdit = permitted(item);
    const add = (label, handler, className = "", disabled = false) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      button.className = className;
      button.disabled = disabled;
      button.addEventListener("click", () => {
        closeMenu();
        handler();
      });
      el.menu.appendChild(button);
    };

    if (multi) {
      add("取消选择", () => setSelection([]));
    } else {
      if (item.kind === "text") {
        add("编辑内容", () => openEditor({ x: item.x, y: item.y }, screen, item), "", !canEdit);
        add("调整这张的字号", () => {
          setSelection([item.id]);
          updateFontBar();
          el.fontValue.focus();
          el.fontValue.select();
        }, "", !canEdit);
      }
      add("置顶", async () => {
        let top = 0;
        for (const other of state.items.values()) top = Math.max(top, other.z || other.id);
        await patchItem(item.id, { z: top + 1 });
      }, "", !canEdit);
      if (item.kind !== "stroke") {
        add("发到线性流", () => sendToLinear(item));
      }
      if (item.attachment) {
        add(item.kind === "image" ? "下载原图" : "下载文件", () =>
          downloadAttachment(item.attachment)
        );
      }
    }
    add(
      multi ? `删除选中的 ${state.selection.size} 个` : "删除",
      () => (multi ? deleteSelection() : deleteItem(item.id)),
      "danger",
      multi ? false : !canEdit
    );

    el.menu.style.left = `${clamp(screen.x, 8, Math.max(8, state.vw - 180))}px`;
    el.menu.style.top = `${clamp(screen.y, 54, Math.max(54, state.vh - 220))}px`;
    el.menu.hidden = false;
  }

  function updateEmptyState() {
    const show =
      state.snapshotDone &&
      bridge.getMode() === "desk" &&
      state.items.size === 0 &&
      !state.muted;
    el.empty.hidden = !show;
    if (show) {
      for (const button of el.empty.querySelectorAll("[data-quick]")) {
        button.disabled = false;
      }
    }
  }

  // ------------------------------------------------------------------------
  // 线性流抽屉
  // ------------------------------------------------------------------------

  function openDrawer() {
    el.drawer.hidden = false;
    el.drawerToggle.classList.add("is-on");
    el.drawerDot.hidden = true;
    renderDrawer();
  }

  function closeDrawer() {
    el.drawer.hidden = true;
    el.drawerToggle.classList.remove("is-on");
  }

  function renderDrawer() {
    if (el.drawer.hidden) return;
    const nearBottom =
      el.drawerList.scrollHeight - el.drawerList.scrollTop - el.drawerList.clientHeight < 60;
    const messages = bridge.getLinearMessages().slice(-60);
    el.drawerList.replaceChildren();
    if (!messages.length) {
      const empty = document.createElement("p");
      empty.className = "desk-drawer-msg";
      empty.textContent = "线性流里还没有消息。";
      el.drawerList.appendChild(empty);
    }
    for (const message of messages) {
      const row = document.createElement("div");
      row.className = "desk-drawer-msg pin";
      const head = document.createElement("div");
      head.className = "desk-drawer-msg-head";
      const nick = document.createElement("b");
      nick.textContent = message.nickname;
      nick.style.color = message.color || "#9db9ff";
      const time = document.createElement("time");
      time.textContent = bridge.formatTime(message.created_at);
      const pin = document.createElement("button");
      pin.type = "button";
      pin.className = "desk-drawer-msg-pin";
      pin.textContent = "钉到桌面";
      pin.addEventListener("click", () => pinMessage(message));
      head.append(nick, time, pin);
      const body = document.createElement("p");
      if (message.recalled) {
        body.textContent = "该消息已被撤回";
      } else if (message.attachment) {
        body.textContent = `${message.text ? message.text + "\n" : ""}[文件] ${message.attachment.name}`;
      } else {
        body.textContent = message.text || "";
      }
      row.append(head, body);
      el.drawerList.appendChild(row);
    }
    if (nearBottom) el.drawerList.scrollTop = el.drawerList.scrollHeight;
  }

  // ------------------------------------------------------------------------
  // 指针交互：平移 / 拖动 / 缩放物件 / 画笔 / 双指缩放
  // ------------------------------------------------------------------------

  function capture(event) {
    try {
      el.viewport.setPointerCapture(event.pointerId);
    } catch {
      /* 某些浏览器在合成事件上不支持捕获 */
    }
  }

  function release(event) {
    try {
      el.viewport.releasePointerCapture(event.pointerId);
    } catch {
      /* 已经释放 */
    }
  }

  function startPan(event, screen) {
    state.pan = {
      id: event.pointerId,
      startX: screen.x,
      startY: screen.y,
      camX: state.camera.x,
      camY: state.camera.y,
    };
    state.fly = null;
    el.viewport.classList.add("is-panning");
    capture(event);
  }

  function startMarquee(event, screen, additive) {
    state.marquee = {
      pointerId: event.pointerId,
      start: screen,
      current: screen,
      base: additive ? new Set(state.selection) : null,
    };
    el.marquee.hidden = false;
    updateMarqueeBox();
    capture(event);
  }

  function updateMarqueeBox() {
    const box = state.marquee;
    if (!box) return;
    el.marquee.style.left = `${Math.min(box.start.x, box.current.x)}px`;
    el.marquee.style.top = `${Math.min(box.start.y, box.current.y)}px`;
    el.marquee.style.width = `${Math.abs(box.current.x - box.start.x)}px`;
    el.marquee.style.height = `${Math.abs(box.current.y - box.start.y)}px`;
  }

  function finishMarquee() {
    const box = state.marquee;
    state.marquee = null;
    el.marquee.hidden = true;
    if (!box) return;
    const min = toWorld(
      Math.min(box.start.x, box.current.x),
      Math.min(box.start.y, box.current.y)
    );
    const max = toWorld(
      Math.max(box.start.x, box.current.x),
      Math.max(box.start.y, box.current.y)
    );
    // 太小的框视为一次点击（清空选择），不误选整片画布
    if (max.x - min.x < 4 && max.y - min.y < 4) {
      if (!box.base) setSelection([]);
      return;
    }
    const ids = new Set(box.base || []);
    for (const item of state.items.values()) {
      const w = item.w || 0;
      const h = item.h || 0;
      if (
        item.x + w >= min.x && item.x <= max.x &&
        item.y + h >= min.y && item.y <= max.y
      ) {
        ids.add(item.id);
      }
    }
    setSelection(Array.from(ids));
  }

  function startDrag(event, screen, item) {
    // 点在已选中的物件上、且当前是多选 → 整组一起移动
    const ids = state.selection.has(item.id) && state.selection.size > 1
      ? selectionIds()
      : [item.id];
    const origins = new Map();
    for (const id of ids) {
      const target = state.items.get(id);
      if (!target) continue;
      origins.set(id, { x: target.x, y: target.y });
      const node = itemNode(id);
      if (node) node.classList.add("is-dragging");
    }
    if (!origins.size) return;
    state.drag = {
      origins,
      pointerId: event.pointerId,
      start: toWorld(screen.x, screen.y),
      dx: 0,
      dy: 0,
      moved: false,
      lastPatch: 0,
    };
    capture(event);
  }

  function startResize(event, screen, item) {
    state.resize = {
      id: item.id,
      pointerId: event.pointerId,
      start: toWorld(screen.x, screen.y),
      origW: item.w || 200,
      origH: item.h || 100,
      w: item.w || 200,
      h: item.h || 100,
      lastPatch: 0,
    };
    capture(event);
  }

  function startStroke(event, world) {
    if (!checkWritable()) return;
    state.stroke = {
      pointerId: event.pointerId,
      points: [{ x: world.x, y: world.y }],
      color: state.color,
      width: state.width,
    };
    el.liveStroke.hidden = false;
    el.livePolyline.setAttribute("stroke", state.color);
    el.livePolyline.setAttribute(
      "stroke-width",
      String(Math.max(1, state.width * state.camera.zoom))
    );
    updateLiveStroke();
    capture(event);
  }

  function updateLiveStroke() {
    const stroke = state.stroke;
    if (!stroke) return;
    const points = stroke.points.map((point) => {
      const screen = toScreen(point.x, point.y);
      return `${screen.x.toFixed(1)},${screen.y.toFixed(1)}`;
    });
    if (points.length === 1) points.push(`${points[0]}`);
    el.livePolyline.setAttribute("points", points.join(" "));
  }

  function cancelStroke() {
    state.stroke = null;
    el.liveStroke.hidden = true;
    el.livePolyline.setAttribute("points", "");
  }

  function finishStroke() {
    const stroke = state.stroke;
    cancelStroke();
    if (!stroke || !stroke.points.length) return;
    const xs = stroke.points.map((point) => point.x);
    const ys = stroke.points.map((point) => point.y);
    const minX = Math.min(...xs);
    const minY = Math.min(...ys);
    const relative = stroke.points.map((point) => [
      round2(point.x - minX),
      round2(point.y - minY),
    ]);
    createItem({
      kind: "stroke",
      x: round2(minX),
      y: round2(minY),
      body: JSON.stringify(relative),
      style: { color: stroke.color, width: stroke.width },
    });
  }

  function startPinch() {
    state.pan = null;
    cancelStroke();
    state.drag = null;
    state.resize = null;
    const points = Array.from(state.pointers.values());
    if (points.length < 2) return;
    const dist = Math.hypot(points[1].x - points[0].x, points[1].y - points[0].y);
    const rect = viewportRect();
    const center = {
      x: (points[0].x + points[1].x) / 2 - rect.left,
      y: (points[0].y + points[1].y) / 2 - rect.top,
    };
    state.pinch = {
      dist: Math.max(1, dist),
      world: toWorld(center.x, center.y),
      zoom: state.camera.zoom,
    };
    state.fly = null;
  }

  function updatePinch() {
    const points = Array.from(state.pointers.values());
    if (points.length < 2 || !state.pinch) return;
    const dist = Math.max(1, Math.hypot(points[1].x - points[0].x, points[1].y - points[0].y));
    const rect = viewportRect();
    const center = {
      x: (points[0].x + points[1].x) / 2 - rect.left,
      y: (points[0].y + points[1].y) / 2 - rect.top,
    };
    const zoom = clamp(state.pinch.zoom * (dist / state.pinch.dist), ZOOM_MIN, ZOOM_MAX);
    state.camera.zoom = zoom;
    state.camera.x = state.pinch.world.x - (center.x - state.vw / 2) / zoom;
    state.camera.y = state.pinch.world.y - (center.y - state.vh / 2) / zoom;
    applyCamera();
  }

  function onPointerDown(event) {
    if (event.button === 2) return;

    // 物件内的链接 / 按钮必须保留浏览器默认行为：否则下面的
    // preventDefault() + setPointerCapture() 会吞掉 <a download> 的下载，
    // 也会让按钮的 click 落到画布上变成一次拖拽。
    const nativeTarget = event.target.closest
      ? event.target.closest("a[href], button, input, textarea, select")
      : null;
    if (nativeTarget) {
      closeMenu();
      el.help.hidden = true;
      return;
    }

    state.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (state.pointers.size >= 2) {
      capture(event);
      startPinch();
      return;
    }

    const screen = localPoint(event);
    const world = toWorld(screen.x, screen.y);
    state.pointer = world;
    state.pointerScreen = screen;
    closeMenu();
    el.help.hidden = true;

    const itemNodeTarget = event.target.closest ? event.target.closest(".desk-item") : null;
    const forcePan = event.button === 1 || state.spaceDown || state.tool === "hand";

    if (forcePan) {
      startPan(event, screen);
      event.preventDefault();
      return;
    }

    if (state.tool === "marquee") {
      startMarquee(event, screen, event.ctrlKey || event.metaKey || event.shiftKey);
      event.preventDefault();
      return;
    }

    if (state.tool === "pen") {
      startStroke(event, world);
      event.preventDefault();
      return;
    }
    if (state.tool === "eraser") {
      if (itemNodeTarget) deleteItem(Number(itemNodeTarget.dataset.id));
      return;
    }
    if (state.tool === "text") {
      if (itemNodeTarget && itemNodeTarget.classList.contains("desk-item--text")) {
        const item = state.items.get(Number(itemNodeTarget.dataset.id));
        if (item) openEditor({ x: item.x, y: item.y }, screen, item);
      } else if (!itemNodeTarget) {
        openEditor(world, screen, null);
      }
      return;
    }
    if (state.tool === "file") {
      state.pendingFilePos = world;
      el.fileInput.click();
      return;
    }

    // 选择工具
    if (itemNodeTarget) {
      const id = Number(itemNodeTarget.dataset.id);
      const item = state.items.get(id);
      if (!item) return;
      if (event.ctrlKey || event.metaKey) {
        toggleSelection(id);
        event.preventDefault();
        return;
      }
      if (!state.selection.has(id)) selectItem(id);
      if (event.target.closest(".desk-resize") && item.kind !== "stroke") {
        startResize(event, screen, item);
      } else if (item.kind !== "stroke" || event.target.closest(".desk-stroke-hit")) {
        startDrag(event, screen, item);
      }
      event.preventDefault();
    } else {
      // 空白处：Shift 拖动 = 框选（Ctrl/⌘ + Shift 可追加），普通拖动 = 平移
      if (event.shiftKey) {
        startMarquee(event, screen, event.ctrlKey || event.metaKey);
        event.preventDefault();
        return;
      }
      setSelection([]);
      startPan(event, screen);
    }
  }

  function onPointerMove(event) {
    if (state.pointers.has(event.pointerId)) {
      state.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    }
    const screen = localPoint(event);
    state.pointer = toWorld(screen.x, screen.y);
    state.pointerScreen = screen;
    el.coords.textContent = `${Math.round(state.pointer.x)}, ${Math.round(state.pointer.y)}`;

    if (state.pinch && state.pointers.size >= 2) {
      updatePinch();
      return;
    }

    if (state.marquee && state.marquee.pointerId === event.pointerId) {
      state.marquee.current = screen;
      updateMarqueeBox();
      return;
    }

    if (state.pan && state.pan.id === event.pointerId) {
      const z = state.camera.zoom;
      state.camera.x = state.pan.camX - (screen.x - state.pan.startX) / z;
      state.camera.y = state.pan.camY - (screen.y - state.pan.startY) / z;
      applyCamera();
      return;
    }

    if (state.drag) {
      const world = toWorld(screen.x, screen.y);
      const drag = state.drag;
      drag.dx = round2(world.x - drag.start.x);
      drag.dy = round2(world.y - drag.start.y);
      drag.moved = true;
      const entries = [];
      for (const [id, origin] of drag.origins) {
        const x = round2(origin.x + drag.dx);
        const y = round2(origin.y + drag.dy);
        const node = itemNode(id);
        if (node) {
          node.style.left = `${x}px`;
          node.style.top = `${y}px`;
        }
        const item = state.items.get(id);
        if (item) state.items.set(id, { ...item, x, y });
        entries.push({ id, x, y });
      }
      const now = Date.now();
      if (now - drag.lastPatch > 220) {
        drag.lastPatch = now;
        if (entries.length === 1) {
          patchItem(entries[0].id, { x: entries[0].x, y: entries[0].y }, true);
        } else {
          batchItems("move", entries);
        }
      }
      return;
    }

    if (state.resize) {
      const world = toWorld(screen.x, screen.y);
      const resize = state.resize;
      resize.w = Math.max(70, round2(resize.origW + world.x - resize.start.x));
      resize.h = Math.max(48, round2(resize.origH + world.y - resize.start.y));
      const node = itemNode(resize.id);
      if (node) {
        node.style.width = `${resize.w}px`;
        node.style.height = `${resize.h}px`;
      }
      return;
    }

    if (state.stroke) {
      const points = state.stroke.points;
      const last = points[points.length - 1];
      const threshold = 1.4 / state.camera.zoom;
      if (Math.hypot(state.pointer.x - last.x, state.pointer.y - last.y) >= threshold) {
        if (points.length < 1500) {
          points.push({ x: state.pointer.x, y: state.pointer.y });
          updateLiveStroke();
        }
      }
    }
  }

  function onPointerUp(event) {
    state.pointers.delete(event.pointerId);
    if (state.pinch && state.pointers.size < 2) state.pinch = null;
    if (state.marquee && state.marquee.pointerId === event.pointerId) finishMarquee();

    if (state.pan && state.pan.id === event.pointerId) {
      state.pan = null;
      el.viewport.classList.remove("is-panning");
    }
    if (state.drag) {
      const drag = state.drag;
      state.drag = null;
      for (const id of drag.origins.keys()) {
        const node = itemNode(id);
        if (node) node.classList.remove("is-dragging");
      }
      if (drag.moved) {
        const entries = [];
        for (const [id, origin] of drag.origins) {
          entries.push({ id, x: round2(origin.x + drag.dx), y: round2(origin.y + drag.dy) });
        }
        if (entries.length === 1) {
          patchItem(entries[0].id, { x: entries[0].x, y: entries[0].y });
        } else {
          batchItems("move", entries);
        }
      }
    }
    if (state.resize) {
      const resize = state.resize;
      state.resize = null;
      patchItem(resize.id, { w: resize.w, h: resize.h });
    }
    if (state.stroke && state.stroke.pointerId === event.pointerId) finishStroke();
    release(event);
  }

  // ------------------------------------------------------------------------
  // 事件绑定
  // ------------------------------------------------------------------------

  function bind() {
    for (const button of el.tools.querySelectorAll("[data-tool]")) {
      button.addEventListener("click", () => setTool(button.dataset.tool));
    }
    el.helpBtn.addEventListener("click", () => {
      el.help.hidden = !el.help.hidden;
    });

    PALETTE.forEach((color) => {
      const swatch = document.createElement("button");
      swatch.type = "button";
      swatch.className = "desk-swatch";
      swatch.style.setProperty("--swatch", color);
      swatch.title = color;
      swatch.addEventListener("click", () => {
        state.color = color;
        for (const other of el.palette.children) other.classList.remove("is-active");
        swatch.classList.add("is-active");
      });
      el.palette.appendChild(swatch);
    });
    el.palette.children[3].classList.add("is-active");

    el.width.addEventListener("input", () => {
      state.width = Number(el.width.value) || 3;
      el.widthLabel.textContent = String(state.width);
    });

    el.viewport.addEventListener("pointerdown", onPointerDown);
    el.viewport.addEventListener("pointermove", onPointerMove);
    el.viewport.addEventListener("pointerup", onPointerUp);
    el.viewport.addEventListener("pointercancel", onPointerUp);
    el.viewport.addEventListener("lostpointercapture", () => {
      if (state.pointers.size === 0) {
        state.pan = null;
        el.viewport.classList.remove("is-panning");
      }
    });

    el.viewport.addEventListener(
      "wheel",
      (event) => {
        event.preventDefault();
        const screen = localPoint(event);
        if (event.ctrlKey || event.metaKey) {
          zoomAt(Math.exp(-event.deltaY * 0.01), screen.x, screen.y);
        } else {
          const z = state.camera.zoom;
          state.camera.x += event.deltaX / z;
          state.camera.y += event.deltaY / z;
          state.fly = null;
          applyCamera();
        }
      },
      { passive: false }
    );

    el.viewport.addEventListener("dblclick", (event) => {
      const screen = localPoint(event);
      const node = event.target.closest ? event.target.closest(".desk-item--text") : null;
      if (node) {
        const item = state.items.get(Number(node.dataset.id));
        if (item) openEditor({ x: item.x, y: item.y }, screen, item);
      } else if (!(event.target.closest && event.target.closest(".desk-item"))) {
        openEditor(toWorld(screen.x, screen.y), screen, null);
      }
    });

    el.viewport.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      const node = event.target.closest ? event.target.closest(".desk-item") : null;
      if (!node) {
        closeMenu();
        return;
      }
      const item = state.items.get(Number(node.dataset.id));
      if (item) {
        selectItem(item.id);
        openMenu(localPoint(event), item);
      }
    });

    // 文件拖放
    const hasFiles = (event) =>
      Boolean(
        event.dataTransfer &&
        Array.from(event.dataTransfer.types || []).includes("Files")
      );
    el.viewport.addEventListener("dragenter", (event) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      event.stopPropagation();
      state.dragDepth += 1;
      el.drop.hidden = false;
    });
    el.viewport.addEventListener("dragover", (event) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      event.stopPropagation();
      event.dataTransfer.dropEffect = "copy";
    });
    el.viewport.addEventListener("dragleave", (event) => {
      if (!hasFiles(event)) return;
      event.stopPropagation();
      state.dragDepth = Math.max(0, state.dragDepth - 1);
      if (state.dragDepth === 0) el.drop.hidden = true;
    });
    el.viewport.addEventListener("drop", (event) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      event.stopPropagation();
      state.dragDepth = 0;
      el.drop.hidden = true;
      const screen = localPoint(event);
      placeFiles(event.dataTransfer.files, toWorld(screen.x, screen.y));
    });

    document.addEventListener("paste", (event) => {
      if (bridge.getMode() !== "desk") return;
      if (event.target && event.target.closest && event.target.closest("input, textarea")) return;
      const items = event.clipboardData ? event.clipboardData.items : null;
      if (!items) return;
      const files = [];
      for (const item of items) {
        if (item.kind === "file") {
          const file = item.getAsFile();
          if (file) files.push(file);
        }
      }
      if (files.length) {
        event.preventDefault();
        placeFiles(files, state.pointer);
      }
    });

    el.fileInput.addEventListener("change", () => {
      placeFiles(el.fileInput.files, state.pendingFilePos || state.camera);
      el.fileInput.value = "";
      state.pendingFilePos = null;
    });

    // 编辑器
    el.editorSave.addEventListener("click", saveEditor);
    el.editorCancel.addEventListener("click", closeEditor);
    el.editorInput.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeEditor();
      } else if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        saveEditor();
      }
    });

    // 便签独立字号条 + 编辑器内的字号选择
    el.fontMinus.addEventListener("click", () => stepSelectionFont(-2));
    el.fontPlus.addEventListener("click", () => stepSelectionFont(2));
    el.fontValue.addEventListener("change", () =>
      setItemFontSize(fontTargets(), el.fontValue.value)
    );
    el.fontValue.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        setItemFontSize(fontTargets(), el.fontValue.value);
        el.fontValue.blur();
      }
    });
    el.fontPresets.addEventListener("click", (event) => {
      const button = event.target.closest("[data-size]");
      if (button) setItemFontSize(fontTargets(), button.dataset.size);
    });
    el.fontClose.addEventListener("click", () => setSelection([]));
    el.editorFontMinus.addEventListener("click", () =>
      setEditorFontSize(state.editor.fontSize - 2)
    );
    el.editorFontPlus.addEventListener("click", () =>
      setEditorFontSize(state.editor.fontSize + 2)
    );
    el.editorFontValue.addEventListener("change", () =>
      setEditorFontSize(el.editorFontValue.value)
    );

    // 顶部 HUD
    $("deskZoomIn").addEventListener("click", () => zoomAt(1.25, state.vw / 2, state.vh / 2));
    $("deskZoomOut").addEventListener("click", () => zoomAt(0.8, state.vw / 2, state.vh / 2));
    el.zoomLabel.addEventListener("click", () => flyTo(state.camera.x, state.camera.y, 1));
    $("deskZoomFit").addEventListener("click", fitAll);
    $("deskZoomHome").addEventListener("click", () => flyTo(0, 0, 1));
    el.backBtn.addEventListener("click", () => bridge.setMode("linear"));
    el.viewsToggle.addEventListener("click", () => {
      state.showViews = !state.showViews;
      el.viewsToggle.classList.toggle("is-on", state.showViews);
      el.viewsToggle.textContent = state.showViews ? "隐藏视野" : "视野";
      renderViews();
    });
    el.drawerToggle.addEventListener("click", () => {
      if (el.drawer.hidden) openDrawer();
      else closeDrawer();
    });
    el.drawerClose.addEventListener("click", closeDrawer);
    el.drawerForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const text = el.drawerInput.value.trim();
      if (!text) return;
      if (state.muted) {
        toast("你已被禁言，暂时不能发言", "error");
        return;
      }
      el.drawerInput.value = "";
      bridge.sendLinearText(text);
    });

    el.minimapCanvas.addEventListener("pointerdown", (event) => {
      const world = minimapToWorld(event);
      if (world) flyTo(world.x, world.y, state.camera.zoom);
    });
    el.minimapCanvas.addEventListener("pointermove", (event) => {
      if (event.buttons !== 1) return;
      const world = minimapToWorld(event);
      if (world) {
        state.camera.x = world.x;
        state.camera.y = world.y;
        applyCamera();
      }
    });

    for (const button of el.empty.querySelectorAll("[data-quick]")) {
      button.addEventListener("click", () => {
        const quick = button.dataset.quick;
        if (quick === "text") {
          openEditor(
            { x: state.camera.x - 130, y: state.camera.y - 50 },
            { x: state.vw / 2 - 140, y: state.vh / 2 - 100 },
            null
          );
        } else if (quick === "file") {
          state.pendingFilePos = { x: state.camera.x - 130, y: state.camera.y - 50 };
          el.fileInput.click();
        } else {
          setTool("pen");
        }
      });
    }

    document.addEventListener("pointerdown", (event) => {
      if (!el.menu.hidden && !el.menu.contains(event.target)) closeMenu();
      if (!el.help.hidden && !el.help.contains(event.target) && event.target !== el.helpBtn) {
        el.help.hidden = true;
      }
    });

    document.addEventListener("keydown", (event) => {
      if (bridge.getMode() !== "desk") return;
      if (document.querySelector(".modal:not([hidden])")) return;
      if (event.key === "Escape") {
        closeEditor();
        closeMenu();
        el.help.hidden = true;
        setSelection([]);
        return;
      }
      const typing =
        event.target &&
        event.target.closest &&
        event.target.closest("input, textarea, [contenteditable='true']");
      if (typing) return;
      if (event.code === "Space") {
        state.spaceDown = true;
        el.viewport.classList.add("is-space");
        event.preventDefault();
        return;
      }
      const key = event.key.toLowerCase();
      if ((event.ctrlKey || event.metaKey) && key === "a") {
        event.preventDefault();
        setSelection(Array.from(state.items.keys()));
        return;
      }
      if ((event.ctrlKey || event.metaKey) && key === "z") {
        event.preventDefault();
        undoDelete();
        return;
      }
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (key === "delete" || key === "backspace") {
        if (state.selection.size) {
          event.preventDefault();
          deleteSelection();
        }
        return;
      }
      const shortcuts = {
        v: "select", h: "hand", m: "marquee",
        t: "text", p: "pen", e: "eraser", u: "file",
      };
      if (shortcuts[key]) {
        setTool(shortcuts[key]);
        return;
      }
      if (key === "[" || key === "]") {
        if (!el.fontBar.hidden) {
          event.preventDefault();
          stepSelectionFont(key === "]" ? 2 : -2);
        }
        return;
      }
      if (key === "+" || key === "=") zoomAt(1.25, state.vw / 2, state.vh / 2);
      if (key === "-" || key === "_") zoomAt(0.8, state.vw / 2, state.vh / 2);
      if (key === "0") flyTo(0, 0, 1);
    });
    document.addEventListener("keyup", (event) => {
      if (event.code === "Space") {
        state.spaceDown = false;
        el.viewport.classList.remove("is-space");
      }
    });

    document.addEventListener("visibilitychange", () => {
      if (state.pollAbort) state.pollAbort.abort();
      if (!document.hidden) heartbeat(true);
    });
    window.addEventListener("beforeunload", () => {
      try {
        const blob = new Blob(
          [JSON.stringify({ client_id: state.clientId })],
          { type: "application/json" }
        );
        navigator.sendBeacon("/api/desk/leave", blob);
      } catch {
        /* 页面关闭时尽力而为 */
      }
    });

    if (typeof ResizeObserver !== "undefined") {
      const observer = new ResizeObserver(() => {
        measure();
        applyCamera();
      });
      observer.observe(el.viewport);
    } else {
      window.addEventListener("resize", () => {
        measure();
        applyCamera();
      });
    }

    // 桥接事件
    bridge.on("viewer", ({ viewer }) => applyMuteState(viewer));
    bridge.on("enter", (detail) => onEnter(detail));
    bridge.on("mode", ({ mode }) => onMode(mode));
    bridge.on("messages", () => {
      renderDrawer();
      if (!el.drawer.hidden) el.drawerDot.hidden = true;
      else el.drawerDot.hidden = false;
    });
  }

  function onEnter(detail) {
    state.me = (detail && detail.me && detail.me.nickname) || bridge.state.me;
    applyMuteState((detail && detail.viewer) || bridge.state.viewer);
    state.active = true;
    loadSnapshot()
      .then(() => {
        state.connected = true;
        renderPeople();
        if (bridge.getMode() === "desk") heartbeat(true);
      })
      .catch((err) => {
        toast(err.message || "桌面加载失败", "error");
      });
    eventLoop();
    setInterval(() => heartbeat(), 300);
    if (bridge.getMode() === "desk") onMode("desk");
  }

  function onMode(mode) {
    state.mode = mode;
    if (mode === "desk") {
      closeDrawer();
      closeEditor();
      closeMenu();
      el.help.hidden = true;
      state.showViews = false;
      el.viewsToggle.classList.remove("is-on");
      el.viewsToggle.textContent = "视野";
      requestAnimationFrame(() => {
        measure();
        applyCamera();
        el.viewport.focus({ preventScroll: true });
        heartbeat(true);
      });
    } else {
      closeEditor();
      closeMenu();
      closeDrawer();
      cancelStroke();
      selectItem(null);
      leaveDesk();
    }
    updateEmptyState();
    updateFontBar();
  }

  // 对外的桥：线性消息上的「钉到桌面」按钮会调用这里
  window.Chat5050Desk = {
    pinMessage,
    focusItem(id) {
      const item = state.items.get(Number(id));
      if (item) {
        bridge.setMode("desk");
        flyTo(item.x + (item.w || 200) / 2, item.y + (item.h || 80) / 2, 1);
      }
    },
    get itemCount() {
      return state.items.size;
    },
    selection: () => selectionIds(),
    setSelection: (ids) => setSelection(ids),
    batch: batchItems,
    camera: () => ({ ...state.camera }),
    // 供自动化与控制台使用：给指定便签（或当前选中）单独设字号
    setItemFontSize: (ids, size) => setItemFontSize(ids, size),
    noteFontSize: (id) => noteFontSize(state.items.get(Number(id))),
    fontTargets: () => fontTargets(),
  };

  restoreCamera();
  bind();
  measure();
  applyCamera();
  updateEmptyState();
})();
