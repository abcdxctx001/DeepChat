/* ==========================================================================
   Chat5050 · 前端逻辑（无框架、无内联脚本）
   ========================================================================== */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const STORAGE_KEY = "chat5050.nickname";
  const MAX_UPLOAD_MB = Number(document.body.dataset.maxUpload) || 50;
  const MAX_TEXT = Number(document.body.dataset.maxText) || 4000;

  const chatEl = $("chat");
  const messagesEl = $("messages");
  const emptyEl = $("empty");
  const inputEl = $("input");
  const sendBtn = $("sendBtn");
  const attachBtn = $("attachBtn");
  const fileInput = $("fileInput");
  const uploadsEl = $("uploads");
  const counterEl = $("counter");
  const statusText = $("statusText");
  const onlineWrap = $("onlineWrap");
  const onlineText = $("onlineText");
  const onlinePanel = $("onlinePanel");
  const onlinePanelList = $("onlinePanelList");
  const onlinePanelEmpty = $("onlinePanelEmpty");
  const onlinePanelCount = $("onlinePanelCount");
  const onlinePanelClose = $("onlinePanelClose");
  const newPill = $("newPill");
  const loadOlderWrap = $("loadOlderWrap");
  const loadOlderBtn = $("loadOlderBtn");
  const loadOlderSpinner = $("loadOlderSpinner");
  const loadOlderText = $("loadOlderText");
  const dropOverlay = $("dropOverlay");
  const nickModal = $("nickModal");
  const nickForm = $("nickForm");
  const nickInput = $("nickInput");
  const nickError = $("nickError");
  const nickSubmit = $("nickSubmit");
  const meRole = $("meRole");
  const adminBtn = $("adminBtn");
  const adminModal = $("adminModal");
  const adminSearch = $("adminSearch");
  const adminRefresh = $("adminRefresh");
  const adminStats = $("adminStats");
  const adminList = $("adminList");
  const adminEmpty = $("adminEmpty");
  const adminError = $("adminError");
  const muteNotice = $("muteNotice");
  const muteNoticeText = $("muteNoticeText");
  const composerEl = document.querySelector(".composer");
  const apiModal = $("apiModal");
  const apiBase = $("apiBase");
  const apiSample = $("apiSample");
  const lightbox = $("lightbox");
  const lightboxImg = $("lightboxImg");
  const lightboxName = $("lightboxName");
  const lightboxDownload = $("lightboxDownload");
  const toastsEl = $("toasts");

  const GROUP = (document.querySelector(".brand-text h1") || {}).textContent || "大厅";
  const BASE_TITLE = `${GROUP} · Chat5050`;

  const state = {
    me: null,
    color: "#5b8cff",
    booted: false,
    latestId: 0,
    oldestId: null,
    hasMore: false,
    loadingOlder: false,
    seen: new Set(),
    messages: new Map(),
    onlineCount: 0,
    onlineNames: [],
    onlinePeople: [],
    onlineFetchedAt: 0,
    onlineLoading: false,
    onlinePanelTimer: null,
    connected: false,
    stickBottom: true,
    unread: 0,
    pollAbort: null,
    sending: false,
    dragDepth: 0,
    // ---- 权限相关 ----
    viewer: null,
    admins: new Set(),
    rev: 0,
    muted: false,
    muteDeadline: 0,
    muteTicker: null,
    roster: [],
    mutePresets: [],
    rootNick: "root",
    adminLoading: false,
  };

  // ------------------------------------------------------------------------
  // 工具
  // ------------------------------------------------------------------------

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function api(path, options = {}) {
    const init = { credentials: "same-origin", ...options };
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    init.headers = headers;

    let res;
    try {
      res = await fetch(path, init);
    } catch (err) {
      if (err && err.name === "AbortError") throw err;
      const netErr = new Error("无法连接服务器，请确认服务仍在运行");
      netErr.cause = err;
      throw netErr;
    }

    const raw = await res.text();
    let data = null;
    if (raw) {
      try {
        data = JSON.parse(raw);
      } catch {
        data = null;
      }
    }
    if (!res.ok || !data || data.ok === false) {
      const info = (data && data.error) || {};
      const err = new Error(info.message || `请求失败（HTTP ${res.status}）`);
      err.status = res.status;
      err.code = info.code || null;
      throw err;
    }
    return data;
  }

  function initial(name) {
    if (!name) return "?";
    return Array.from(name)[0] || "?";
  }

  function formatTime(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    const now = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    if (
      d.getFullYear() === now.getFullYear() &&
      d.getMonth() === now.getMonth() &&
      d.getDate() === now.getDate()
    ) {
      return hm;
    }
    const md = `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`;
    if (d.getFullYear() === now.getFullYear()) return md;
    return `${d.getFullYear()}-${md}`;
  }

  function fullTime(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    return d.toLocaleString("zh-CN", { hour12: false });
  }

  function fileExt(name) {
    const clean = String(name || "");
    const idx = clean.lastIndexOf(".");
    const ext = idx >= 0 ? clean.slice(idx + 1) : "";
    return (ext || "FILE").slice(0, 5);
  }

  function isNearBottom() {
    return chatEl.scrollHeight - chatEl.scrollTop - chatEl.clientHeight < 120;
  }

  function scrollToBottom(smooth = false) {
    requestAnimationFrame(() => {
      chatEl.scrollTo({
        top: chatEl.scrollHeight,
        behavior: smooth ? "smooth" : "auto",
      });
    });
  }

  function toast(message, type = "error") {
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.textContent = message;
    toastsEl.appendChild(el);
    while (toastsEl.children.length > 4) {
      toastsEl.firstElementChild.remove();
    }
    setTimeout(() => {
      el.classList.add("out");
      setTimeout(() => el.remove(), 200);
    }, 3400);
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast("已复制到剪贴板", "ok");
      return;
    } catch {
      /* 回退到 execCommand */
    }
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
      toast("已复制到剪贴板", "ok");
    } catch {
      toast("复制失败，请手动选择文本", "error");
    }
    ta.remove();
  }

  function linkify(container, text) {
    const re = /https?:\/\/[^\s<>"']+/g;
    let last = 0;
    let match;
    while ((match = re.exec(text)) !== null) {
      let url = match[0];
      let trailing = "";
      while (/[.,!?;:，。！？；：、）)\]】》]$/.test(url)) {
        trailing = url.slice(-1) + trailing;
        url = url.slice(0, -1);
      }
      if (match.index > last) {
        container.appendChild(document.createTextNode(text.slice(last, match.index)));
      }
      if (url) {
        const a = document.createElement("a");
        a.href = url;
        a.textContent = url;
        a.target = "_blank";
        a.rel = "noopener noreferrer nofollow";
        container.appendChild(a);
      }
      if (trailing) container.appendChild(document.createTextNode(trailing));
      last = match.index + match[0].length;
    }
    if (last < text.length) {
      container.appendChild(document.createTextNode(text.slice(last)));
    }
  }

  // ------------------------------------------------------------------------
  // 渲染消息
  // ------------------------------------------------------------------------

  function fileCard(message) {
    const att = message.attachment;
    const card = document.createElement("a");
    card.className = "file-card";
    card.href = att.download_url;
    card.setAttribute("download", att.name);
    card.title = `下载 ${att.name}`;

    const ext = document.createElement("span");
    ext.className = "file-ext";
    ext.textContent = fileExt(att.name);

    const info = document.createElement("span");
    info.className = "file-info";
    const name = document.createElement("span");
    name.className = "file-name";
    name.textContent = att.name;
    name.title = att.name;
    const size = document.createElement("span");
    size.className = "file-size";
    size.textContent = `${att.size_human || ""} · 点击下载`;
    info.append(name, size);

    const icon = document.createElement("span");
    icon.className = "file-dl";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = "↓";

    card.append(ext, info, icon);
    return card;
  }

  function openLightbox(message) {
    const att = message.attachment;
    if (!att) return;
    lightboxImg.src = att.url;
    lightboxImg.alt = att.name;
    lightboxName.textContent = att.name;
    lightboxDownload.href = att.download_url;
    lightboxDownload.setAttribute("download", att.name);
    lightbox.hidden = false;
  }

  function closeLightbox() {
    lightbox.hidden = true;
    lightboxImg.removeAttribute("src");
  }

  function appendAttachment(bubble, message) {
    const att = message.attachment;
    if (att.is_image) {
      const media = document.createElement("div");
      media.className = "msg-media";
      const img = document.createElement("img");
      img.className = "msg-img";
      img.src = att.url;
      img.alt = att.name;
      img.loading = "lazy";
      img.decoding = "async";
      img.tabIndex = 0;
      img.setAttribute("role", "button");
      img.title = "点击查看原图";
      const open = () => openLightbox(message);
      img.addEventListener("click", open);
      img.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          open();
        }
      });
      img.addEventListener("load", () => {
        if (state.stickBottom) scrollToBottom(false);
      });
      img.addEventListener("error", () => {
        media.replaceWith(fileCard(message));
      });
      media.appendChild(img);
      bubble.appendChild(media);
    } else {
      bubble.appendChild(fileCard(message));
    }
  }

  /**
   * 粗略判断一条消息是不是 ASCII 画（cowsay 这类）。
   * 是的话必须用等宽字体渲染，否则气泡和图案会全部散架。
   */
  function looksLikeArt(text) {
    if (!text || text.length < 24) return false;
    const lines = text.split("\n");
    if (lines.length < 4) return false;
    let rulers = 0;
    let marks = 0;
    for (const raw of lines) {
      const line = raw.replace(/\s+$/, "");
      if (!line.trim()) continue;
      if (/^[\s_\-=~*+]{6,}$/.test(line)) rulers += 1;           // 只有横线/下划线
      else if (/^[\s]*[\\/|<>()]/.test(line)) marks += 1;        // 以边框/斜杠开头
      else if (/[\\/|]{2,}/.test(line)) marks += 1;               // ||____ 之类
    }
    return rulers >= 2 && marks >= 2;
  }

  /** 当前身份是否有权撤回 / 撤销这条消息（前端只做展示，最终由服务端裁决）。 */
  function canRecall(message) {
    if (!message || message.recalled || !state.me) return false;
    const admin = Boolean(state.viewer && state.viewer.is_admin);
    return admin || message.nickname === state.me;
  }

  function canUnrecall(message) {
    if (!message || !message.recalled || !state.me) return false;
    const admin = Boolean(state.viewer && state.viewer.is_admin);
    return admin || message.recalled_by === state.me;
  }

  function messageAction(label, act, message, extraClass = "") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `msg-action ${extraClass}`.trim();
    btn.dataset.act = act;
    btn.dataset.id = String(message.id);
    btn.textContent = label;
    return btn;
  }

  function renderMessage(message) {
    const own = Boolean(state.me && message.nickname === state.me);
    const row = document.createElement("article");
    row.className = `msg${own ? " own" : ""}`;
    row.dataset.id = String(message.id);

    const avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.style.background = message.color || "#5b8cff";
    avatar.textContent = initial(message.nickname);
    avatar.title = message.nickname;
    avatar.setAttribute("aria-hidden", "true");

    const body = document.createElement("div");
    body.className = "msg-body";

    const meta = document.createElement("div");
    meta.className = "msg-meta";
    const nick = document.createElement("span");
    nick.className = "msg-nick";
    nick.textContent = message.nickname;
    nick.style.color = own ? "#d4e2ff" : message.color || "#9db9ff";
    const time = document.createElement("time");
    time.className = "msg-time";
    time.dateTime = message.created_at;
    time.textContent = formatTime(message.created_at);
    time.title = fullTime(message.created_at);
    meta.append(nick);
    if (state.admins.has(message.nickname)) {
      const badge = document.createElement("em");
      badge.className = "nick-badge";
      badge.textContent = "管理员";
      badge.title = "管理员";
      meta.appendChild(badge);
    }
    meta.appendChild(time);

    const actions = [];
    if (canRecall(message)) {
      actions.push(messageAction("撤回", "recall", message, "danger"));
    }
    if (canUnrecall(message)) {
      actions.push(messageAction("撤销撤回", "unrecall", message));
    }
    if (actions.length) {
      const wrap = document.createElement("span");
      wrap.className = "msg-actions";
      wrap.append(...actions);
      meta.appendChild(wrap);
    }

    const bubble = document.createElement("div");
    bubble.className = "bubble";

    if (message.recalled) {
      bubble.classList.add("bubble--recalled");
      const text = document.createElement("div");
      text.className = "recall-text";
      text.textContent = "该消息已被撤回";
      bubble.appendChild(text);

      const note = document.createElement("div");
      note.className = "recall-meta";
      const who = message.recalled_by || "未知用户";
      const when = message.recalled_at ? formatTime(message.recalled_at) : "";
      note.textContent = when ? `${who} 于 ${when} 撤回` : `${who} 撤回`;
      bubble.appendChild(note);

      if (canUnrecall(message)) {
        const undo = document.createElement("button");
        undo.type = "button";
        undo.className = "recall-undo";
        undo.dataset.act = "unrecall";
        undo.dataset.id = String(message.id);
        undo.textContent = "撤销撤回";
        undo.title = "恢复显示这条消息";
        bubble.appendChild(undo);
      }
    } else {
      if (message.text) {
        const text = document.createElement("div");
        text.className = "msg-text";
        linkify(text, message.text);
        if (message.kind === "cowsay" || looksLikeArt(message.text)) {
          text.classList.add("msg-text--art");
        }
        bubble.appendChild(text);
      }
      if (message.attachment) {
        bubble.classList.add("has-media");
        if (!message.text) bubble.classList.add("media-only");
        appendAttachment(bubble, message);
      }
    }

    body.append(meta, bubble);
    row.append(avatar, body);
    return row;
  }

  function renderFallbackMessage(message) {
    const own = Boolean(state.me && message.nickname === state.me);
    const row = document.createElement("article");
    row.className = `msg${own ? " own" : ""}`;
    row.dataset.id = String(message.id);

    const avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.style.background = message.color || "#5b8cff";
    avatar.textContent = initial(message.nickname);
    avatar.setAttribute("aria-hidden", "true");

    const body = document.createElement("div");
    body.className = "msg-body";

    const meta = document.createElement("div");
    meta.className = "msg-meta";
    const nick = document.createElement("span");
    nick.className = "msg-nick";
    nick.textContent = message.nickname || "?";
    const time = document.createElement("time");
    time.className = "msg-time";
    time.dateTime = message.created_at || "";
    time.textContent = formatTime(message.created_at);
    meta.append(nick, time);

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    const text = document.createElement("div");
    text.className = "msg-text";
    text.textContent = message.text || `（消息 #${message.id} 无法显示）`;
    bubble.appendChild(text);

    body.append(meta, bubble);
    row.append(avatar, body);
    return row;
  }

  // ------------------------------------------------------------------------
  // 撤回 / 权限状态同步
  // ------------------------------------------------------------------------

  function messageNode(id) {
    return messagesEl.querySelector(`article[data-id="${id}"]`);
  }

  /** 用最新数据原地重绘一条消息（不改变滚动位置）。 */
  function replaceMessageNode(message) {
    const old = messageNode(message.id);
    if (!old) return false;
    try {
      old.replaceWith(renderMessage(message));
      return true;
    } catch (err) {
      console.error("消息重绘失败", message.id, err);
      return false;
    }
  }

  function rerenderAllMessages() {
    const wasSticky = state.stickBottom;
    const previousHeight = chatEl.scrollHeight;
    const previousTop = chatEl.scrollTop;
    for (const message of state.messages.values()) {
      replaceMessageNode(message);
    }
    if (!wasSticky) {
      chatEl.scrollTop = previousTop + (chatEl.scrollHeight - previousHeight);
    }
  }

  /** 应用服务端增量：只更新已加载过的消息，未加载的等翻页时自然带回来。 */
  function applyRecallChanges(changes) {
    if (!Array.isArray(changes) || !changes.length) return 0;
    let applied = 0;
    for (const change of changes) {
      const current = state.messages.get(change.id);
      if (!current) continue;
      if (!change.recalled && current.recalled) {
        // 撤销撤回后正文需要重新取，因为撤回期间服务端没有下发原文
        refreshMessage(change.id);
        applied += 1;
        continue;
      }
      const next = {
        ...current,
        recalled: Boolean(change.recalled),
        recalled_by: change.recalled_by,
        recalled_at: change.recalled_at,
        recall_rev: change.recall_rev,
        text: change.recalled ? "" : current.text,
        attachment: change.recalled ? null : current.attachment,
      };
      state.messages.set(change.id, next);
      replaceMessageNode(next);
      applied += 1;
    }
    return applied;
  }

  async function refreshMessage(id) {
    try {
      const data = await api(`/api/messages/${id}`);
      state.messages.set(id, data.message);
      replaceMessageNode(data.message);
    } catch (err) {
      if (!handleAuthError(err)) console.warn("刷新消息失败", id, err);
    }
  }

  async function recallMessage(id, recalled) {
    try {
      const data = await api(
        `/api/messages/${id}/${recalled ? "recall" : "unrecall"}`,
        { method: "POST" }
      );
      if (data.rev) state.rev = Math.max(state.rev, data.rev);
      if (data.message) {
        state.messages.set(id, data.message);
        replaceMessageNode(data.message);
      } else {
        refreshMessage(id);
      }
      toast(recalled ? "已撤回这条消息" : "已恢复显示", "ok");
    } catch (err) {
      if (!handleAuthError(err)) toast(err.message, "error");
    }
  }

  function updateAdmins(list) {
    if (!Array.isArray(list)) return;
    const next = new Set(list);
    const same =
      next.size === state.admins.size &&
      Array.from(next).every((name) => state.admins.has(name));
    if (same) return;
    state.admins = next;
    rerenderAllMessages();
  }

  function formatDuration(totalSeconds) {
    const total = Math.max(0, Math.floor(totalSeconds));
    const days = Math.floor(total / 86400);
    const hours = Math.floor((total % 86400) / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    if (days) return hours ? `${days} 天 ${hours} 小时` : `${days} 天`;
    if (hours) return minutes ? `${hours} 小时 ${minutes} 分` : `${hours} 小时`;
    if (minutes) return seconds ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分`;
    return `${seconds} 秒`;
  }

  function muteLabel(viewer) {
    if (!viewer || !viewer.muted) return "";
    if (viewer.muted_forever) return "你已被永久禁言，暂时不能发言";
    const remaining = Math.max(
      0,
      Math.ceil((state.muteDeadline - Date.now()) / 1000)
    );
    if (remaining <= 0) return "禁言即将解除…";
    return `你已被禁言，还需等待 ${formatDuration(remaining)}`;
  }

  function stopMuteTicker() {
    if (state.muteTicker) clearInterval(state.muteTicker);
    state.muteTicker = null;
  }

  function startMuteTicker() {
    if (state.muteTicker) return;
    state.muteTicker = setInterval(() => {
      if (!state.muted) {
        stopMuteTicker();
        return;
      }
      const viewer = state.viewer;
      if (!viewer || viewer.muted_forever) {
        muteNoticeText.textContent = muteLabel(viewer);
        return;
      }
      if (state.muteDeadline && Date.now() >= state.muteDeadline) {
        stopMuteTicker();
        refreshMe();
        return;
      }
      muteNoticeText.textContent = muteLabel(viewer);
    }, 1000);
  }

  function applyMuteState(viewer) {
    const muted = Boolean(viewer && viewer.muted);
    state.muted = muted;
    if (muted) {
      if (viewer.muted_forever) {
        state.muteDeadline = 0;
      } else if (typeof viewer.mute_remaining === "number") {
        state.muteDeadline = Date.now() + viewer.mute_remaining * 1000;
      }
    } else {
      state.muteDeadline = 0;
    }

    muteNotice.hidden = !muted;
    if (composerEl) composerEl.classList.toggle("composer--muted", muted);
    inputEl.disabled = muted;
    attachBtn.disabled = muted;
    sendBtn.disabled = muted || state.sending;

    if (muted) {
      inputEl.placeholder = "你已被禁言，暂时不能发言";
      muteNoticeText.textContent = muteLabel(viewer);
      startMuteTicker();
    } else {
      inputEl.placeholder = `发消息到 ${GROUP}…`;
      stopMuteTicker();
    }
  }

  function applyViewer(viewer) {
    if (!viewer || typeof viewer !== "object") return;
    state.viewer = viewer;
    if (!state.me && viewer.nickname) state.me = viewer.nickname;
    const admin = Boolean(viewer.is_admin);
    adminBtn.hidden = !admin;
    meRole.hidden = !admin;
    if (!admin && !adminModal.hidden) closeAdminModal();
    applyMuteState(viewer);
  }

  async function refreshMe() {
    try {
      const me = await api("/api/me");
      applyViewer(me.viewer);
    } catch (err) {
      handleAuthError(err);
    }
  }

  function updateEmptyState() {
    emptyEl.hidden = state.seen.size > 0;
  }

  /**
   * 将消息追加进 DOM。只接受尚未见过的 id，天然去重。
   */
  function ingest(list, options = {}) {
    if (!Array.isArray(list)) return 0;

    const valid = list.filter(
      (message) => message && typeof message.id === "number"
    );
    if (!valid.length) return 0;

    // 游标只根据“服务器这次真正返回的消息”前进；自己的发送响应不能单独
    // 把游标推到更大的 id，否则中间尚未拉取的消息会被永久跳过。
    if (options.advanceCursor !== false) {
      state.latestId = Math.max(
        state.latestId,
        ...valid.map((message) => message.id)
      );
    }

    const fresh = [];
    for (const message of valid) {
      if (state.seen.has(message.id)) continue;
      fresh.push(message);
    }
    if (!fresh.length) {
      if (options.advanceCursor !== false) {
        state.latestId = Math.max(
          state.latestId,
          ...valid.map((message) => message.id)
        );
      }
      return 0;
    }

    // 单条消息渲染失败不能拖垮整批消息，否则整批都会被标记为已读而丢失。
    const nodes = [];
    for (const message of fresh) {
      let node;
      try {
        node = renderMessage(message);
      } catch (err) {
        console.error("消息渲染失败", message.id, err);
        node = renderFallbackMessage(message);
      }
      nodes.push(node);
    }

    // 正常情况按 id 追加；如果因为断线重连 / 自己先发消息而出现“空洞”，
    // 就把补拉到的消息插到正确位置，保证界面仍然是时间顺序。
    const lastChild = messagesEl.lastElementChild;
    const lastId = lastChild ? Number(lastChild.dataset.id) : 0;
    const firstId = Math.min(...fresh.map((message) => message.id));
    if (!lastChild || firstId > lastId) {
      const fragment = document.createDocumentFragment();
      for (const node of nodes) fragment.appendChild(node);
      messagesEl.appendChild(fragment);
    } else {
      for (const node of nodes) {
        const id = Number(node.dataset.id);
        let before = null;
        for (
          let child = messagesEl.firstElementChild;
          child;
          child = child.nextElementSibling
        ) {
          const childId = Number(child.dataset.id);
          if (Number.isFinite(childId) && childId > id) {
            before = child;
            break;
          }
        }
        messagesEl.insertBefore(node, before);
      }
    }

    for (const message of fresh) {
      state.seen.add(message.id);
      state.messages.set(message.id, message);
    }

    if (options.advanceCursor !== false) {
      state.latestId = Math.max(
        state.latestId,
        ...valid.map((message) => message.id)
      );
    }

    const minId = Math.min(...fresh.map((m) => m.id));
    state.oldestId = state.oldestId === null ? minId : Math.min(state.oldestId, minId);
    updateEmptyState();

    if (options.scroll || state.stickBottom) {
      state.stickBottom = true;
      scrollToBottom(Boolean(options.smooth));
    } else {
      state.unread += fresh.length;
      showNewPill();
    }
    return fresh.length;
  }

  function showNewPill() {
    newPill.replaceChildren(
      document.createTextNode(`${state.unread} 条新消息 `),
      Object.assign(document.createElement("span"), { textContent: "↓" })
    );
    newPill.lastElementChild.setAttribute("aria-hidden", "true");
    newPill.hidden = false;
  }

  function hideNewPill() {
    newPill.hidden = true;
    state.unread = 0;
  }

  // ------------------------------------------------------------------------
  // 连接 / 在线状态
  // ------------------------------------------------------------------------

  function setConnected(ok) {
    state.connected = ok;
    onlineWrap.classList.toggle("ok", ok);
    onlineWrap.classList.toggle("bad", !ok);
    if (ok) {
      statusText.textContent = "已连接 · 新消息实时推送";
      statusText.classList.remove("bad");
    } else {
      onlineText.textContent = "重连中…";
      statusText.textContent = "连接断开，正在自动重试…";
      statusText.classList.add("bad");
    }
    updateOnlineHeader();
  }

  function activeText(iso) {
    if (!iso) return "在线";
    const timestamp = Date.parse(iso);
    if (Number.isNaN(timestamp)) return "在线";
    const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
    if (seconds < 20) return "刚刚活跃";
    if (seconds < 60) return `${seconds} 秒前活跃`;
    return `${Math.max(1, Math.floor(seconds / 60))} 分钟前活跃`;
  }

  function currentOnlinePeople() {
    const known = new Map(
      state.onlinePeople.map((person) => [person.nickname, person])
    );
    const names = state.onlineNames.length
      ? state.onlineNames
      : state.onlinePeople.map((person) => person.nickname);
    return names.map((name) => {
      const person = known.get(name);
      if (person) return person;
      return {
        nickname: name,
        color: name === state.me ? state.color : "#5b8cff",
        last_seen: null,
      };
    });
  }

  function renderOnlinePanel(people) {
    const list = Array.isArray(people) ? people : [];
    onlinePanelCount.textContent = `${list.length} 人`;
    onlinePanelList.replaceChildren();
    onlinePanelEmpty.hidden = list.length > 0;
    if (!list.length) {
      onlinePanelEmpty.textContent = "当前无人在线";
      return;
    }

    const fragment = document.createDocumentFragment();
    for (const person of list) {
      const row = document.createElement("div");
      row.className = "online-person";

      const avatar = document.createElement("span");
      avatar.className = "avatar avatar-sm";
      avatar.style.background = person.color || "#5b8cff";
      avatar.textContent = initial(person.nickname);
      avatar.setAttribute("aria-hidden", "true");

      const info = document.createElement("span");
      info.className = "online-person-info";

      const nameLine = document.createElement("span");
      nameLine.className = "online-person-name";
      const name = document.createElement("span");
      name.textContent = person.nickname;
      name.title = person.nickname;
      nameLine.appendChild(name);
      if (person.nickname === state.me) {
        const you = document.createElement("em");
        you.className = "online-you";
        you.textContent = "你";
        nameLine.appendChild(you);
      }

      const time = document.createElement("span");
      time.className = "online-person-time";
      time.textContent = activeText(person.last_seen);

      info.append(nameLine, time);

      const dot = document.createElement("span");
      dot.className = "online-person-dot";
      dot.title = "在线";
      dot.setAttribute("aria-label", "在线");

      row.append(avatar, info, dot);
      fragment.appendChild(row);
    }
    onlinePanelList.appendChild(fragment);
  }

  function renderOnlinePanelError(message) {
    onlinePanelCount.textContent = "—";
    onlinePanelList.replaceChildren();
    onlinePanelEmpty.hidden = false;
    onlinePanelEmpty.textContent = message || "暂时无法获取在线成员";
  }

  function updateOnlineHeader() {
    if (state.connected) {
      onlineText.textContent = `${state.onlineCount} 人在线`;
    }
    onlineWrap.title = state.connected
      ? state.onlineNames.length
        ? `在线 ${state.onlineCount} 人 · 点击查看`
        : "点击查看在线成员"
      : "点击查看在线成员";
    onlineWrap.setAttribute(
      "aria-label",
      state.connected
        ? `查看在线成员，${state.onlineCount} 人在线`
        : "查看在线成员，连接中"
    );
  }

  function updateOnline(count, names) {
    if (typeof count === "number") state.onlineCount = count;
    if (Array.isArray(names)) state.onlineNames = names;
    updateOnlineHeader();
    if (!onlinePanel.hidden) {
      renderOnlinePanel(currentOnlinePeople());
      if (Date.now() - state.onlineFetchedAt > 8000) {
        refreshOnlinePanel({ silent: true });
      }
    }
  }

  async function refreshOnlinePanel(options = {}) {
    if (!state.me || state.onlineLoading) return;
    state.onlineLoading = true;
    try {
      const data = await api("/api/presence");
      const people = Array.isArray(data.online) ? data.online : [];
      state.onlinePeople = people;
      state.onlineNames = people.map((person) => person.nickname);
      state.onlineCount =
        typeof data.online_count === "number"
          ? data.online_count
          : people.length;
      state.onlineFetchedAt = Date.now();
      updateOnlineHeader();
      if (!onlinePanel.hidden) {
        renderOnlinePanel(people);
      }
    } catch (err) {
      if (err && err.status === 401) {
        closeOnlinePanel();
        state.me = null;
        openNickModal(false);
        return;
      }
      if (!onlinePanel.hidden) {
        if (!state.onlinePeople.length) {
          renderOnlinePanelError("无法获取在线成员，请检查网络连接");
        } else if (!options.silent) {
          toast("在线成员刷新失败", "error");
        }
      }
    } finally {
      state.onlineLoading = false;
    }
  }

  function stopOnlinePanelTimer() {
    if (state.onlinePanelTimer) {
      clearInterval(state.onlinePanelTimer);
      state.onlinePanelTimer = null;
    }
  }

  function startOnlinePanelTimer() {
    stopOnlinePanelTimer();
    state.onlinePanelTimer = setInterval(() => {
      if (onlinePanel.hidden || document.hidden) return;
      renderOnlinePanel(currentOnlinePeople());
      refreshOnlinePanel({ silent: true });
    }, 15000);
  }

  function openOnlinePanel() {
    if (!state.me || !onlinePanel.hidden) return;
    onlinePanel.hidden = false;
    onlineWrap.setAttribute("aria-expanded", "true");
    renderOnlinePanel(currentOnlinePeople());
    onlinePanel.focus({ preventScroll: true });
    refreshOnlinePanel({ silent: true });
    startOnlinePanelTimer();
  }

  function closeOnlinePanel(focusButton = false) {
    if (onlinePanel.hidden) return;
    onlinePanel.hidden = true;
    onlineWrap.setAttribute("aria-expanded", "false");
    stopOnlinePanelTimer();
    if (focusButton) onlineWrap.focus({ preventScroll: true });
  }

  function toggleOnlinePanel() {
    if (onlinePanel.hidden) openOnlinePanel();
    else closeOnlinePanel();
  }

  // ------------------------------------------------------------------------
  // 拉取 / 长轮询
  // ------------------------------------------------------------------------

  async function loadInitial() {
    const data = await api("/api/messages?limit=60");
    state.latestId = data.latest_id || 0;
    state.oldestId = data.oldest_id;
    state.hasMore = Boolean(data.has_more);
    state.rev = data.rev || 0;
    applyViewer(data.viewer);
    updateAdmins(data.admins);
    updateOnline(data.online_count, data.online);

    if (data.messages && data.messages.length) {
      state.stickBottom = true;
      ingest(data.messages, { scroll: true });
      chatEl.scrollTop = chatEl.scrollHeight;
    }
    updateEmptyState();
    updateLoadOlder();
    setConnected(true);
  }

  async function loadOlder() {
    if (state.loadingOlder || !state.hasMore || state.oldestId === null) return;
    state.loadingOlder = true;
    loadOlderBtn.disabled = true;
    loadOlderSpinner.hidden = false;
    loadOlderText.textContent = "加载中…";

    try {
      const data = await api(
        `/api/messages?before_id=${encodeURIComponent(state.oldestId)}&limit=60`
      );
      if (data.messages && data.messages.length) {
        const previousHeight = chatEl.scrollHeight;
        const previousTop = chatEl.scrollTop;
        const fragment = document.createDocumentFragment();
        for (const message of data.messages) {
          if (state.seen.has(message.id)) continue;
          state.seen.add(message.id);
          state.messages.set(message.id, message);
          fragment.appendChild(renderMessage(message));
        }
        messagesEl.prepend(fragment);
        state.oldestId =
          typeof data.oldest_id === "number" ? data.oldest_id : state.oldestId;
        state.hasMore = Boolean(data.has_more);
        chatEl.scrollTop = previousTop + (chatEl.scrollHeight - previousHeight);
        state.stickBottom = false;
        updateEmptyState();
      } else {
        state.hasMore = false;
      }
    } catch (err) {
      toast(err.message, "error");
    } finally {
      state.loadingOlder = false;
      loadOlderBtn.disabled = false;
      loadOlderSpinner.hidden = true;
      loadOlderText.textContent = "加载更早的消息";
      updateLoadOlder();
    }
  }

  function updateLoadOlder() {
    loadOlderWrap.hidden = !state.hasMore;
  }

  async function pollLoop() {
    for (;;) {
      if (!state.me) {
        await sleep(800);
        continue;
      }
      if (document.hidden) {
        await sleep(1200);
        continue;
      }

      const controller = new AbortController();
      state.pollAbort = controller;
      const timer = setTimeout(() => controller.abort(), 40000);
      try {
        const data = await api(
          `/api/messages?after_id=${encodeURIComponent(
            state.latestId
          )}&limit=200&wait=25&since_rev=${encodeURIComponent(state.rev || 0)}`,
          { signal: controller.signal }
        );
        clearTimeout(timer);
        setConnected(true);
        updateOnline(data.online_count, data.online);
        applyViewer(data.viewer);
        updateAdmins(data.admins);
        if (typeof data.rev === "number" && data.rev !== state.rev) {
          applyRecallChanges(data.recall_changes);
          state.rev = data.rev;
        }
        if (data.messages && data.messages.length) {
          ingest(data.messages);
        }
      } catch (err) {
        clearTimeout(timer);
        if (err && err.name === "AbortError") {
          if (!document.hidden) setConnected(false);
          await sleep(150);
          continue;
        }
        if (err && err.status === 401) {
          state.me = null;
          setConnected(false);
          openNickModal(false);
          return;
        }
        setConnected(false);
        await sleep(2200);
      } finally {
        if (state.pollAbort === controller) state.pollAbort = null;
      }
    }
  }

  // ------------------------------------------------------------------------
  // 发送文字 / 上传
  // ------------------------------------------------------------------------

  function autosize() {
    inputEl.style.height = "auto";
    inputEl.style.height = `${Math.min(inputEl.scrollHeight, 160)}px`;
  }

  function updateCounter() {
    const length = inputEl.value.length;
    counterEl.textContent = `${length} / ${MAX_TEXT}`;
    counterEl.classList.toggle("warn", length > MAX_TEXT * 0.9);
  }

  function handleAuthError(err) {
    if (err && err.status === 401) {
      state.me = null;
      setConnected(false);
      openNickModal(false);
      return true;
    }
    return false;
  }

  async function sendText() {
    if (state.sending || !state.me) return;
    if (state.muted) {
      toast(muteLabel(state.viewer) || "你已被禁言", "error");
      return;
    }
    const text = inputEl.value.replace(/\r\n/g, "\n").trim();
    if (!text) {
      inputEl.focus();
      return;
    }
    if (text.length > MAX_TEXT) {
      toast(`消息最多 ${MAX_TEXT} 个字符`, "error");
      return;
    }

    state.sending = true;
    sendBtn.disabled = true;
    try {
      const data = await api("/api/messages", {
        method: "POST",
        body: JSON.stringify({ text }),
      });
      inputEl.value = "";
      autosize();
      updateCounter();
      state.stickBottom = true;
      if (data.message) state.messages.set(data.message.id, data.message);
      ingest([data.message], { scroll: true, advanceCursor: false });
    } catch (err) {
      if (!handleAuthError(err)) {
        if (err && err.code === "muted") {
          state.muted = true;
          refreshMe();
        }
        toast(err.message, "error");
      }
    } finally {
      state.sending = false;
      sendBtn.disabled = state.muted;
      if (!state.muted) inputEl.focus();
    }
  }

  function createUploadChip(file) {
    const chip = document.createElement("div");
    chip.className = "upload-chip";

    const thumb = document.createElement("div");
    thumb.className = "upload-thumb";
    const isImage = (file.type || "").startsWith("image/");
    if (isImage) {
      const img = document.createElement("img");
      const objectUrl = URL.createObjectURL(file);
      img.src = objectUrl;
      img.alt = "";
      img.addEventListener("load", () => URL.revokeObjectURL(objectUrl), {
        once: true,
      });
      thumb.appendChild(img);
      chip.dataset.objectUrl = objectUrl;
    } else {
      thumb.textContent = fileExt(file.name);
    }

    const info = document.createElement("div");
    info.className = "upload-info";
    const name = document.createElement("div");
    name.className = "upload-name";
    name.textContent = file.name;
    name.title = file.name;
    const bar = document.createElement("div");
    bar.className = "upload-bar";
    const fill = document.createElement("div");
    fill.className = "upload-fill";
    bar.appendChild(fill);
    info.append(name, bar);

    const status = document.createElement("span");
    status.className = "upload-status";
    status.textContent = "0%";

    chip.append(thumb, info, status);
    uploadsEl.appendChild(chip);

    return {
      chip,
      progress(ratio) {
        const pct = Math.max(0, Math.min(100, Math.round(ratio * 100)));
        fill.style.width = `${pct}%`;
        status.textContent = `${pct}%`;
      },
      done() {
        chip.classList.add("done");
        fill.style.width = "100%";
        status.textContent = "完成";
        setTimeout(() => {
          if (chip.dataset.objectUrl) URL.revokeObjectURL(chip.dataset.objectUrl);
          chip.remove();
        }, 700);
      },
      fail(message) {
        chip.classList.add("failed");
        status.textContent = "失败";
        chip.title = message;
        if (chip.dataset.objectUrl) URL.revokeObjectURL(chip.dataset.objectUrl);
        setTimeout(() => chip.remove(), 5000);
      },
    };
  }

  function uploadOne(file) {
    return new Promise((resolve) => {
      const ui = createUploadChip(file);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/upload");
      xhr.responseType = "json";

      xhr.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable) ui.progress(event.loaded / event.total);
      });

      xhr.addEventListener("load", () => {
        const data = xhr.response;
        if (xhr.status >= 200 && xhr.status < 300 && data && data.ok) {
          ui.done();
          state.stickBottom = true;
          ingest([data.message], { scroll: true, advanceCursor: false });
        } else {
          const info = data && data.error ? data.error.message : "";
          const message = info || `上传失败（HTTP ${xhr.status}）`;
          ui.fail(message);
          if (xhr.status === 401) {
            state.me = null;
            openNickModal(false);
          } else {
            toast(message, "error");
          }
        }
        resolve();
      });

      xhr.addEventListener("error", () => {
        ui.fail("网络错误");
        toast("上传失败：网络错误", "error");
        resolve();
      });

      xhr.addEventListener("abort", () => {
        ui.fail("已取消");
        resolve();
      });

      const form = new FormData();
      form.append("file", file, file.name);
      xhr.send(form);
    });
  }

  async function uploadFiles(fileList) {
    if (!state.me) return;
    if (state.muted) {
      toast(muteLabel(state.viewer) || "你已被禁言", "error");
      return;
    }
    const files = Array.from(fileList || []);
    for (const file of files) {
      if (file.size > MAX_UPLOAD_MB * 1024 * 1024) {
        toast(`「${file.name}」超过 ${MAX_UPLOAD_MB} MB 限制`, "error");
        continue;
      }
      await uploadOne(file);
    }
  }

  // ------------------------------------------------------------------------
  // 昵称 / 弹窗
  // ------------------------------------------------------------------------

  function setMeUI(me) {
    state.me = me.nickname;
    state.color = me.color || "#5b8cff";
    $("meName").textContent = me.nickname;
    const avatar = $("meAvatar");
    avatar.textContent = initial(me.nickname);
    avatar.style.background = state.color;
    document.title = BASE_TITLE;
    if (me.viewer) applyViewer(me.viewer);
  }

  function openNickModal(canClose) {
    closeOnlinePanel();
    closeAdminModal();
    nickModal.hidden = false;
    nickModal.dataset.canClose = canClose && state.me ? "1" : "0";
    nickInput.value = state.me || localStorage.getItem(STORAGE_KEY) || "";
    nickError.hidden = true;
    nickError.textContent = "";
    nickSubmit.disabled = false;
    setTimeout(() => {
      nickInput.focus();
      nickInput.select();
    }, 40);
  }

  function closeNickModal(force = false) {
    if (!force && nickModal.dataset.canClose !== "1") return;
    nickModal.hidden = true;
  }

  async function enter(me) {
    setMeUI(me);
    localStorage.setItem(STORAGE_KEY, me.nickname);
    closeNickModal(true);

    if (state.booted) {
      location.reload();
      return;
    }
    state.booted = true;

    try {
      await loadInitial();
    } catch (err) {
      if (err.status === 401) {
        state.booted = false;
        state.me = null;
        openNickModal(false);
        return;
      }
      toast(err.message || "加载消息失败", "error");
      setConnected(false);
    }
    pollLoop();
  }

  // ------------------------------------------------------------------------
  // 权限管理面板（仅管理员可见）
  // ------------------------------------------------------------------------

  function closeAdminModal() {
    adminModal.hidden = true;
  }

  function adminPersonBadges(person) {
    const list = [];
    if (person.is_admin) list.push(["badge badge-admin", "管理员"]);
    if (person.locked) list.push(["badge badge-locked", "已锁定"]);
    if (person.muted) {
      list.push([
        "badge badge-muted",
        person.muted_forever ? "永久禁言" : "禁言中",
        person.muted_forever
          ? "永久禁言"
          : `禁言至 ${formatTime(person.muted_until)}`,
      ]);
    }
    if (person.online) list.push(["badge badge-online", "在线"]);
    if (person.nickname === state.me) list.push(["badge badge-me", "我"]);
    return list;
  }

  function renderAdminStats(counts) {
    const people = state.roster;
    const entries = [
      ["成员", counts.total ?? people.length],
      ["管理员", counts.admins ?? people.filter((p) => p.is_admin).length],
      ["禁言", counts.muted ?? people.filter((p) => p.muted).length],
      ["锁定", counts.locked ?? people.filter((p) => p.locked).length],
      ["在线", counts.online ?? people.filter((p) => p.online).length],
    ];
    adminStats.replaceChildren(
      ...entries.map(([label, value]) => {
        const span = document.createElement("span");
        span.className = "admin-stat";
        const strong = document.createElement("b");
        strong.textContent = String(value);
        span.append(`${label} `, strong);
        return span;
      })
    );
  }

  function renderAdminPerson(person) {
    const row = document.createElement("div");
    row.className = "admin-person";
    row.dataset.nick = person.nickname;

    const main = document.createElement("div");
    main.className = "admin-person-main";

    const top = document.createElement("div");
    top.className = "admin-person-top";
    const name = document.createElement("span");
    name.className = "admin-person-name";
    name.textContent = person.nickname;
    name.title = person.nickname;
    top.appendChild(name);
    for (const [cls, text, title] of adminPersonBadges(person)) {
      const badge = document.createElement("span");
      badge.className = cls;
      badge.textContent = text;
      if (title) badge.title = title;
      top.appendChild(badge);
    }

    const sub = document.createElement("span");
    sub.className = "admin-person-sub";
    const bits = [`${person.message_count || 0} 条消息`];
    if (person.ip_count) bits.push(`${person.ip_count} 个 IP`);
    if (person.last_seen) bits.push(`最近 ${formatTime(person.last_seen)}`);
    else bits.push("尚未登录网页");
    sub.textContent = bits.join(" · ");
    sub.title = person.ips && person.ips.length ? person.ips.join("\n") : "";
    main.append(top, sub);

    const actions = document.createElement("div");
    actions.className = "admin-actions";

    const isRootPerson = person.nickname === state.rootNick;
    const rootLocked = isRootPerson && state.me !== state.rootNick;

    const roleBtn = document.createElement("button");
    roleBtn.type = "button";
    roleBtn.className = `admin-action${person.is_admin ? " on" : ""}`;
    roleBtn.dataset.act = "role";
    roleBtn.dataset.nick = person.nickname;
    roleBtn.dataset.role = person.is_admin ? "user" : "admin";
    roleBtn.textContent = person.is_admin ? "取消管理员" : "设为管理员";
    roleBtn.disabled = rootLocked || isRootPerson;
    if (isRootPerson) roleBtn.title = "root 始终是管理员";
    else if (rootLocked) roleBtn.title = "只有 root 本人可以修改 root 的权限";

    const muteSelect = document.createElement("select");
    muteSelect.className = "admin-action admin-select";
    muteSelect.dataset.mute = person.nickname;
    muteSelect.disabled = rootLocked;
    muteSelect.setAttribute("aria-label", `设置「${person.nickname}」的禁言`);
    if (rootLocked) muteSelect.title = "只有 root 本人可以修改 root 的权限";
    const placeholder = new Option(
      person.muted ? "调整禁言…" : "禁言…",
      ""
    );
    placeholder.disabled = true;
    placeholder.selected = true;
    muteSelect.appendChild(placeholder);
    for (const preset of state.mutePresets) {
      muteSelect.appendChild(new Option(preset.label, String(preset.seconds)));
    }
    if (person.muted) muteSelect.appendChild(new Option("解除禁言", "off"));

    const lockBtn = document.createElement("button");
    lockBtn.type = "button";
    lockBtn.className = `admin-action${person.locked ? " on" : ""}`;
    lockBtn.dataset.act = "lock";
    lockBtn.dataset.nick = person.nickname;
    lockBtn.dataset.lock = person.locked ? "0" : "1";
    lockBtn.textContent = person.locked ? "解除锁定" : "锁定昵称";
    lockBtn.disabled = rootLocked;
    if (rootLocked) lockBtn.title = "只有 root 本人可以修改 root 的权限";
    else if (person.locked)
      lockBtn.title = (person.locked_ips || []).join("\n") || "尚无绑定 IP，首次登录即认领";

    actions.append(roleBtn, muteSelect, lockBtn);
    row.append(main, actions);
    return row;
  }

  function renderAdminList() {
    const keyword = (adminSearch.value || "").trim().toLowerCase();
    const people = state.roster.filter(
      (person) => !keyword || person.nickname.toLowerCase().includes(keyword)
    );
    adminList.replaceChildren();
    adminEmpty.hidden = people.length > 0;
    if (!people.length) return;
    const fragment = document.createDocumentFragment();
    for (const person of people) fragment.appendChild(renderAdminPerson(person));
    adminList.appendChild(fragment);
  }

  async function loadRoster() {
    if (state.adminLoading) return;
    state.adminLoading = true;
    adminRefresh.disabled = true;
    try {
      const data = await api("/api/admin/identities");
      state.roster = Array.isArray(data.identities) ? data.identities : [];
      state.mutePresets = Array.isArray(data.mute_presets)
        ? data.mute_presets
        : [];
      state.rootNick = data.root || "root";
      renderAdminStats(data.counts || {});
      renderAdminList();
      adminError.hidden = true;
    } catch (err) {
      if (!handleAuthError(err)) {
        adminError.textContent = err.message;
        adminError.hidden = false;
        adminList.replaceChildren();
      }
    } finally {
      state.adminLoading = false;
      adminRefresh.disabled = false;
    }
  }

  async function openAdminModal() {
    if (!state.viewer || !state.viewer.is_admin) return;
    closeOnlinePanel();
    adminError.hidden = true;
    adminEmpty.hidden = true;
    adminModal.hidden = false;
    adminSearch.value = "";
    adminList.replaceChildren();
    const loading = document.createElement("p");
    loading.className = "admin-empty";
    loading.textContent = "正在读取名单…";
    adminList.appendChild(loading);
    await loadRoster();
    adminSearch.focus();
  }

  async function adminUpdate(nickname, payload) {
    adminError.hidden = true;
    try {
      const data = await api(
        `/api/admin/identities/${encodeURIComponent(nickname)}`,
        { method: "POST", body: JSON.stringify(payload) }
      );
      const person = data.identity;
      if (person) {
        const index = state.roster.findIndex(
          (item) => item.nickname === nickname
        );
        if (index >= 0) state.roster[index] = person;
        else state.roster.push(person);
        // 统计数字也要跟着刷新，否则会出现「禁言 0」但行内显示禁言中的矛盾
        renderAdminStats({});
        renderAdminList();
      }
      toast(`已更新「${nickname}」的权限`, "ok");
      if (nickname === state.me) refreshMe();
    } catch (err) {
      if (handleAuthError(err)) return;
      adminError.textContent = err.message;
      adminError.hidden = false;
    }
  }

  function bindAdminPanel() {
    adminBtn.addEventListener("click", openAdminModal);
    adminRefresh.addEventListener("click", () => loadRoster());

    let searchTimer = null;
    adminSearch.addEventListener("input", () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(renderAdminList, 80);
    });

    adminList.addEventListener("click", (event) => {
      const btn = event.target.closest("button[data-act]");
      if (!btn || btn.disabled) return;
      const nickname = btn.dataset.nick;
      if (btn.dataset.act === "role") {
        const role = btn.dataset.role;
        const verb = role === "admin" ? "设为管理员" : "取消管理员";
        if (role === "user" && !confirm(`确定要${verb}「${nickname}」吗？`)) return;
        adminUpdate(nickname, { role });
      }
      if (btn.dataset.act === "lock") {
        const lock = btn.dataset.lock === "1";
        if (
          lock &&
          !confirm(
            `锁定「${nickname}」后，只有它绑定过的 IP 能在网页上使用这个昵称，该 IP 也不能换成别的昵称。确定锁定吗？`
          )
        ) {
          return;
        }
        adminUpdate(nickname, { lock });
      }
    });

    adminList.addEventListener("change", (event) => {
      const select = event.target.closest("select[data-mute]");
      if (!select || !select.value) return;
      const nickname = select.dataset.mute;
      const value = select.value;
      select.value = "";
      if (value === "off") {
        adminUpdate(nickname, { mute: null });
        return;
      }
      const seconds = Number(value);
      if (!Number.isFinite(seconds)) return;
      adminUpdate(nickname, {
        mute: seconds > 0 ? { seconds } : { forever: true },
      });
    });
  }

  function openApiModal() {
    closeOnlinePanel();
    closeAdminModal();
    const base = location.origin;
    apiBase.textContent = base;
    apiSample.textContent = [
      "# 健康检查（无需昵称）",
      `curl ${base}/api/health`,
      "",
      "# 读取最近 20 条消息",
      `curl "${base}/api/messages?nickname=alice&limit=20"`,
      "",
      "# 发送文字",
      `curl -X POST ${base}/api/messages \\`,
      '  -H "Content-Type: application/json" \\',
      `  -d '{"nickname":"alice","text":"大家好！"}'`,
      "",
      "# 上传图片 / 文件",
      `curl -X POST ${base}/api/upload \\`,
      '  -F "nickname=alice" \\',
      '  -F "file=@./photo.png"',
      "",
      "# 撤回 / 撤销撤回（本人或管理员）",
      `curl -X POST "${base}/api/messages/123/recall?nickname=alice"`,
      `curl -X POST "${base}/api/messages/123/unrecall?nickname=alice"`,
      "",
      "# 管理员：禁言 / 锁定 / 授权",
      `curl -X POST "${base}/api/admin/identities/bob?nickname=root" \\`,
      '  -H "Content-Type: application/json" \\',
      `  -d '{"mute":{"seconds":600}}'`,
      "",
      "# 长轮询：有新消息 / 撤回事件立即返回（最多等 25 秒）",
      `curl "${base}/api/messages?nickname=alice&after_id=0&since_rev=0&wait=25"`,
    ].join("\n");
    apiModal.hidden = false;
  }

  // ------------------------------------------------------------------------
  // 事件绑定
  // ------------------------------------------------------------------------

  function bind() {
    bindAdminPanel();

    messagesEl.addEventListener("click", (event) => {
      const action = event.target.closest("[data-act]");
      if (!action) return;
      const id = Number(action.dataset.id);
      if (!Number.isFinite(id)) return;
      if (action.dataset.act === "recall") recallMessage(id, true);
      if (action.dataset.act === "unrecall") recallMessage(id, false);
    });

    nickForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const nickname = nickInput.value.trim();
      if (!nickname) {
        nickError.textContent = "请输入昵称";
        nickError.hidden = false;
        return;
      }
      nickSubmit.disabled = true;
      nickError.hidden = true;
      try {
        const data = await api("/api/login", {
          method: "POST",
          body: JSON.stringify({ nickname }),
        });
        localStorage.setItem(STORAGE_KEY, data.nickname);
        await enter(data);
      } catch (err) {
        nickError.textContent = err.message;
        nickError.hidden = false;
        nickSubmit.disabled = false;
      }
    });

    $("nickBtn").addEventListener("click", () => openNickModal(true));
    $("apiBtn").addEventListener("click", openApiModal);

    onlineWrap.addEventListener("click", toggleOnlinePanel);
    onlinePanelClose.addEventListener("click", () => closeOnlinePanel(true));
    document.addEventListener("pointerdown", (event) => {
      if (onlinePanel.hidden) return;
      if (
        onlinePanel.contains(event.target) ||
        onlineWrap.contains(event.target)
      ) {
        return;
      }
      closeOnlinePanel();
    });

    document.querySelectorAll("[data-close]").forEach((el) => {
      el.addEventListener("click", () => {
        const name = el.dataset.close;
        if (name === "nick") closeNickModal();
        if (name === "api") apiModal.hidden = true;
        if (name === "admin") closeAdminModal();
      });
    });

    document.querySelectorAll(".copy-btn[data-copy]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const source = document.getElementById(btn.dataset.copy);
        if (source) copyText(source.textContent);
      });
    });
    $("copyBaseBtn").addEventListener("click", () => copyText(location.origin));

    sendBtn.addEventListener("click", sendText);
    attachBtn.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      uploadFiles(fileInput.files);
      fileInput.value = "";
    });

    inputEl.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        sendText();
      }
    });
    inputEl.addEventListener("input", () => {
      autosize();
      updateCounter();
    });

    inputEl.addEventListener("paste", (event) => {
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
        uploadFiles(files);
      }
    });

    chatEl.addEventListener(
      "scroll",
      () => {
        const near = isNearBottom();
        state.stickBottom = near;
        if (near) hideNewPill();
      },
      { passive: true }
    );

    newPill.addEventListener("click", () => {
      state.stickBottom = true;
      hideNewPill();
      scrollToBottom(true);
    });

    loadOlderBtn.addEventListener("click", loadOlder);

    lightbox.addEventListener("click", (event) => {
      if (event.target === lightbox) closeLightbox();
    });
    $("lightboxClose").addEventListener("click", closeLightbox);

    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (!lightbox.hidden) {
        closeLightbox();
      } else if (!apiModal.hidden) {
        apiModal.hidden = true;
      } else if (!adminModal.hidden) {
        closeAdminModal();
      } else if (!nickModal.hidden) {
        closeNickModal();
      } else if (!onlinePanel.hidden) {
        closeOnlinePanel(true);
      }
    });

    // 拖拽上传
    const hasFiles = (event) =>
      Boolean(
        event.dataTransfer &&
          Array.from(event.dataTransfer.types || []).includes("Files")
      );

    window.addEventListener("dragenter", (event) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      state.dragDepth += 1;
      dropOverlay.hidden = false;
    });
    window.addEventListener("dragover", (event) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
    });
    window.addEventListener("dragleave", (event) => {
      if (!hasFiles(event)) return;
      state.dragDepth = Math.max(0, state.dragDepth - 1);
      if (state.dragDepth === 0) dropOverlay.hidden = true;
    });
    window.addEventListener("drop", (event) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      state.dragDepth = 0;
      dropOverlay.hidden = true;
      uploadFiles(event.dataTransfer.files);
    });

    document.addEventListener("visibilitychange", () => {
      if (state.pollAbort) state.pollAbort.abort();
      if (!document.hidden && !onlinePanel.hidden) {
        renderOnlinePanel(currentOnlinePeople());
        refreshOnlinePanel({ silent: true });
      }
    });
    window.addEventListener("online", () => {
      if (state.pollAbort) state.pollAbort.abort();
      toast("网络已恢复", "ok");
    });
    window.addEventListener("offline", () => {
      setConnected(false);
    });
  }

  // ------------------------------------------------------------------------
  // 启动
  // ------------------------------------------------------------------------

  async function boot() {
    bind();
    autosize();
    updateCounter();
    updateEmptyState();
    setConnected(false);

    try {
      const group = await api("/api/group");
      if (group.group) {
        document.querySelector(".brand-text h1").textContent = group.group.name;
        document.title = `${group.group.name} · Chat5050`;
      }
    } catch {
      /* 群信息失败不阻塞 */
    }

    try {
      const me = await api("/api/me");
      await enter(me);
      return;
    } catch (err) {
      if (err.status && err.status !== 401) {
        toast(err.message, "error");
      }
    }

    // 页面刷新后尝试用上次的昵称自动进入
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) {
      try {
        const me = await api("/api/login", {
          method: "POST",
          body: JSON.stringify({ nickname: saved }),
        });
        await enter(me);
        return;
      } catch {
        /* 让用户手动输入 */
      }
    }

    openNickModal(false);
    setConnected(false);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
