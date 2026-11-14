/* 伴侣浮窗 — read-only companion panel. Drafts only; there is NO send path in this file. */
(() => {
  const POLL_MS = 4000;
  const SESSIONS_MS = 30000;
  const AI_DEBOUNCE_MS = 1200;
  const KIND = { chatroom: "群聊", friend: "私聊", private: "私聊", official: "公众号", stranger: "陌生人" };
  const KIND_TEXT = { image: "[图片]", voice: "[语音]", video: "[视频]", emoji: "[表情]", file: "[文件]", link: "[链接]", location: "[位置]", system: "[系统消息]" };

  const state = {
    sessions: [],
    candidates: [],
    chat: null,
    following: localStorage.getItem("sb_follow") !== "0",
    aiOn: null, // null unknown, true ok, false not configured
    aiHowto: "",
    lastMsgKey: "",
    summaryCache: new Map(),
    suggestCache: new Map(),
    aiTimer: null,
    sessionsAt: 0,
  };

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };

  async function api(path, opts = {}) {
    const { method = "GET", params, body } = opts;
    let url = path;
    if (params) {
      const q = new URLSearchParams();
      Object.entries(params).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== "") q.set(k, v); });
      if (q.toString()) url += "?" + q.toString();
    }
    const init = { method, headers: {}, credentials: "same-origin" };
    if (body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
    const r = await fetch(url, init);
    if (r.status === 401) { location.href = "/login?next=/sidebar"; throw new Error("unauthorized"); }
    const ct = r.headers.get("content-type") || "";
    const data = ct.includes("application/json") ? await r.json() : await r.text();
    if (!r.ok) {
      const err = new Error((data && (data.error || data.detail)) || r.statusText);
      err.status = r.status; err.data = data; throw err;
    }
    return data;
  }

  function toast(msg) {
    const t = $("toast"); t.textContent = msg; t.classList.remove("hidden");
    clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), 1600);
  }

  function fmtTime(iso) {
    if (!iso) return "";
    const d = new Date(iso); if (isNaN(d)) return "";
    const now = new Date();
    const hm = d.toTimeString().slice(0, 5);
    return d.toDateString() === now.toDateString() ? hm : `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
  }

  function avatarNode(username, name, cls) {
    const fallback = () => el("div", cls || "avatar", (name || username || "?").slice(0, 1));
    if (!username) return fallback();
    const img = el("img", cls || "avatar");
    img.alt = ""; img.loading = "lazy";
    img.src = "/api/avatar/" + encodeURIComponent(username);
    img.onerror = () => { const f = fallback(); if (img.id) { f.id = img.id; f.dataset.user = img.dataset.user || ""; } img.replaceWith(f); };
    return img;
  }

  /* ---------- follow / lock ---------- */
  function renderFollow() {
    const b = $("followBtn");
    b.textContent = state.following ? "● 跟随中" : "🔒 已锁定";
    b.className = "chip follow " + (state.following ? "on" : "off");
    const h = $("followHint");
    h.title = "";
    if (!state.following) { h.textContent = "已锁定当前会话，不再自动切换。点“已锁定”恢复跟随。"; return; }
    h.textContent = "自动跟随“最近打开的会话”（估计值：只在会话有未读时更新，可能滞后几秒，手机上读消息也会触发）。不准时手动选会话即可锁定。";
  }
  function setFollowing(on) {
    state.following = on; localStorage.setItem("sb_follow", on ? "1" : "0"); renderFollow(); renderHead();
    if (on) pollActive();
  }

  /* ---------- sessions & picker ---------- */
  async function loadSessions(force) {
    if (!force && Date.now() - state.sessionsAt < SESSIONS_MS && state.sessions.length) return;
    try {
      const d = await api("/api/sessions", { params: { limit: 400 } });
      state.sessions = d.sessions || []; state.sessionsAt = Date.now();
      setFresh(d.freshness);
    } catch (e) { if (e.message !== "unauthorized") setFresh(null, true); }
  }

  function setFresh(f, err) {
    const d = $("freshDot");
    d.className = "dot " + (err ? "err" : f && f.status === "fresh" ? "fresh" : "stale");
    d.title = err ? "读取失败" : f ? `数据：${f.status}，滞后 ${f.lag_ms || 0}ms` : "";
  }

  function ddItem(s, extra) {
    const it = el("div", "dd-item");
    it.append(avatarNode(s.username, s.name));
    it.append(el("span", "n", s.name || s.username));
    if (s.unread) it.append(el("span", "badge", String(s.unread > 99 ? "99+" : s.unread)));
    it.append(el("span", "k", extra || KIND[s.kind] || ""));
    it.addEventListener("mousedown", (ev) => { ev.preventDefault(); pick(s); });
    return it;
  }

  function renderDropdown() {
    const dd = $("chatDropdown");
    const q = $("chatSearch").value.trim().toLowerCase();
    dd.innerHTML = "";
    if (!q && state.candidates.length) {
      dd.append(el("div", "dd-sec", "最近打开（估计）"));
      state.candidates.forEach((c) => dd.append(ddItem(c, fmtTime(c.opened_at))));
      dd.append(el("div", "dd-sec", "最近会话"));
    }
    const list = state.sessions.filter((s) => !q || (s.name || "").toLowerCase().includes(q) || (s.username || "").toLowerCase().includes(q));
    list.slice(0, 80).forEach((s) => dd.append(ddItem(s)));
    if (!list.length) dd.append(el("div", "dd-sec", "无匹配会话"));
    dd.classList.remove("hidden");
  }

  function pick(s) {
    $("chatDropdown").classList.add("hidden");
    $("chatSearch").value = ""; $("chatSearch").blur();
    setFollowing(false); // manual choice locks
    selectChat(s);
  }

  /* ---------- active chat ---------- */
  async function pollActive() {
    try {
      const d = await api("/api/active-chat", { params: { top: 5 } });
      state.candidates = d.candidates || [];
      if (state.following && d.active && (!state.chat || state.chat.username !== d.active.username)) {
        selectChat(d.active, true);
      } else if (state.following && d.active && state.chat) {
        state.chat.opened_at = d.active.opened_at; renderHead();
      }
    } catch (e) { /* keep current selection on transient errors */ }
  }

  function selectChat(s, auto) {
    const known = state.sessions.find((x) => x.username === s.username) || {};
    state.chat = { username: s.username, name: s.name || known.name || s.username, kind: s.kind || known.kind, opened_at: s.opened_at, auto: !!auto, via: auto ? "estimate" : "manual" };
    state.lastMsgKey = "";
    renderHead();
    $("recentBox").innerHTML = "";
    loadRecent();
    renderCachedOrIdle();
    clearTimeout(state.aiTimer);
    if ($("autoAI").checked) state.aiTimer = setTimeout(() => runAI(false), AI_DEBOUNCE_MS);
  }

  function renderHead() {
    const c = state.chat; if (!c) return;
    $("chatHead").classList.remove("empty");
    const av = $("chatAvatar");
    if (av.dataset.user !== c.username) { const n = avatarNode(c.username, c.name); n.id = "chatAvatar"; n.dataset.user = c.username; av.replaceWith(n); }
    $("chatName").textContent = c.name;
    const bits = [KIND[c.kind] || ""];
    if (state.following && c.opened_at) bits.push(`最近打开 ${fmtTime(c.opened_at)}（估计）`);
    else if (!state.following) bits.push("已锁定");
    $("chatMeta").textContent = bits.filter(Boolean).join(" · ");
  }

  /* ---------- recent messages ---------- */
  async function loadRecent() {
    const c = state.chat; if (!c) return;
    try {
      const d = await api("/api/messages", { params: { chat: c.username, limit: 15 } });
      if (!state.chat || state.chat.username !== c.username) return;
      setFresh(d.freshness);
      const msgs = d.messages || [];
      const key = msgs.length ? `${msgs[msgs.length - 1].local_id}:${msgs.length}` : "empty";
      if (key === state.lastMsgKey) return;
      state.lastMsgKey = key;
      const box = $("recentBox");
      const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40 || !box.childElementCount;
      box.innerHTML = "";
      if (!msgs.length) box.append(el("div", "muted", "暂无消息"));
      const group = c.username.endsWith("@chatroom");
      msgs.forEach((m) => {
        const b = el("div", "msg" + (m.is_self ? " self" : ""));
        const who = m.is_self ? "我" : (group ? (m.sender_name || m.sender || "") : "");
        b.append(el("span", "s", [who, fmtTime(m.time)].filter(Boolean).join(" · ")));
        const txt = (m.text || "").trim() || KIND_TEXT[m.kind] || `[${m.kind || "消息"}]`;
        b.append(document.createTextNode(txt));
        box.append(b);
      });
      if (atBottom) box.scrollTop = box.scrollHeight;
    } catch (e) {
      if (e.message !== "unauthorized") { $("recentBox").innerHTML = ""; $("recentBox").append(el("div", "muted", "消息读取失败：" + e.message)); }
    }
  }

  /* ---------- AI: summary + suggestions (drafts only) ---------- */
  function scope() { const [t, v] = $("scopeSel").value.split(":"); return t === "d" ? { n: 200, days: +v } : { n: +v }; }
  function sumKey() { return state.chat ? `${state.chat.username}|${$("scopeSel").value}` : ""; }

  function aiOffNode() {
    const d = el("div", "ai-off");
    d.append(el("div", null, "AI 未配置 —— 填好外部 API 后即可使用。"));
    const c = el("code", null, "点右上角 ⚙ 打开应用设置 → AI 服务：填 Base URL、API Key，选模型并启用");
    d.append(c);
    return d;
  }

  function renderCachedOrIdle() {
    const sb = $("summaryBox"), gb = $("suggestBox");
    const s = state.summaryCache.get(sumKey());
    if (s) renderSummary(s); else { sb.className = "card-body muted"; sb.textContent = state.aiOn === false ? "" : "点击“总结”或等待自动总结。"; if (state.aiOn === false) sb.append(aiOffNode()); }
    const g = state.suggestCache.get(state.chat.username);
    if (g) renderSuggest(g); else { gb.className = "card-body muted"; gb.textContent = state.aiOn === false ? "" : "点击“生成”或等待自动生成。"; if (state.aiOn === false) gb.append(aiOffNode()); }
  }

  function li(arr, fmt) { const ul = el("ul"); arr.forEach((x) => ul.append(el("li", null, fmt ? fmt(x) : x))); return ul; }

  function renderSummary(res) {
    const box = $("summaryBox"); box.className = "card-body sum"; box.innerHTML = "";
    const s = res.summary || {};
    const sec = (title, node) => { box.append(el("h4", null, title)); box.append(node); };
    if (s.overview) sec("概况", el("p", null, s.overview));
    if ((s.topics || []).length) sec("关键话题", li(s.topics));
    if ((s.open_threads || []).length) sec("待我回复 / 未了结", li(s.open_threads));
    if ((s.commitments || []).length) sec("约定与日期", li(s.commitments, (x) => [x.what, x.when && `（${x.when}）`, x.who && ` · ${x.who}`].filter(Boolean).join("")));
    if (s.tone) sec("语气", el("p", null, s.tone));
    if ((s.reply_tips || []).length) sec("回复提示", li(s.reply_tips));
    if (!box.childElementCount) box.append(el("p", "muted", "（无可总结内容）"));
    const sc = res.scope || {};
    box.append(el("div", "foot", `基于 ${sc.messages || 0} 条消息 · ${fmtTime(sc.from)} → ${fmtTime(sc.to)} · 生成于 ${fmtTime(res.generated_at)}`));
  }

  async function copyText(t) {
    try { await navigator.clipboard.writeText(t); return true; }
    catch (_) {
      const ta = el("textarea"); ta.value = t; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.append(ta); ta.select(); let ok = false; try { ok = document.execCommand("copy"); } catch (e) {} ta.remove(); return ok;
    }
  }

  function renderSuggest(res) {
    const box = $("suggestBox"); box.className = "card-body"; box.innerHTML = "";
    const list = res.suggestions || [];
    if (!list.length) box.append(el("div", "muted", "（没有生成建议）"));
    list.forEach((t) => {
      const row = el("div", "sug");
      row.append(el("div", "t", t));
      const b = el("button", null, "复制");
      b.title = "复制草稿，自己粘贴到微信";
      b.addEventListener("click", async () => {
        const ok = await copyText(t);
        b.textContent = ok ? "已复制" : "复制失败"; b.classList.toggle("ok", ok);
        toast(ok ? "已复制草稿，去微信里粘贴发送" : "复制失败，请手动选中复制");
        setTimeout(() => { b.textContent = "复制"; b.classList.remove("ok"); }, 1500);
      });
      row.append(b); box.append(row);
    });
    if (res.stale) box.append(el("div", "muted", "有新消息，可点“生成”刷新。"));
  }

  function aiError(box, e) {
    box.className = "card-body"; box.innerHTML = "";
    if (e.status === 501) { state.aiOn = false; box.append(aiOffNode()); return; }
    box.append(el("div", "ai-off", "AI 调用失败：" + e.message));
  }

  async function runSummary(manual) {
    const c = state.chat; if (!c) return;
    const key = sumKey();
    if (!manual && state.summaryCache.has(key)) return renderSummary(state.summaryCache.get(key));
    if (!manual && state.aiOn === false) return;
    const box = $("summaryBox"); box.className = "card-body muted loading"; box.textContent = "正在总结";
    $("sumBtn").disabled = true;
    try {
      const res = await api("/api/summarize", { method: "POST", body: Object.assign({ chat: c.username, name: c.name }, scope()) });
      state.aiOn = true; state.summaryCache.set(key, res);
      if (sumKey() === key) renderSummary(res);
    } catch (e) { if (state.chat && state.chat.username === c.username) aiError(box, e); }
    finally { $("sumBtn").disabled = false; }
  }

  async function runSuggest(manual) {
    const c = state.chat; if (!c) return;
    const cached = state.suggestCache.get(c.username);
    if (!manual && cached) { cached.stale = cached.msgKey !== state.lastMsgKey && !!state.lastMsgKey; return renderSuggest(cached); }
    if (!manual && state.aiOn === false) return;
    const box = $("suggestBox"); box.className = "card-body muted loading"; box.textContent = "正在生成草稿";
    $("sugBtn").disabled = true;
    try {
      const res = await api("/api/suggest", { method: "POST", body: { chat: c.username, n_context: 30, count: 4 } });
      state.aiOn = true; res.msgKey = state.lastMsgKey; state.suggestCache.set(c.username, res);
      if (state.chat && state.chat.username === c.username) renderSuggest(res);
    } catch (e) { if (state.chat && state.chat.username === c.username) aiError(box, e); }
    finally { $("sugBtn").disabled = false; }
  }

  function runAI(manual) { runSummary(manual); runSuggest(manual); }

  /* ---------- wire up ---------- */
  function init() {
    if (localStorage.getItem("sb_theme") === "light") document.body.classList.add("light");
    $("themeBtn").addEventListener("click", () => { document.body.classList.toggle("light"); localStorage.setItem("sb_theme", document.body.classList.contains("light") ? "light" : "dark"); });
    $("followBtn").addEventListener("click", () => setFollowing(!state.following));
    $("autoAI").checked = localStorage.getItem("sb_autoai") !== "0";
    $("autoAI").addEventListener("change", () => localStorage.setItem("sb_autoai", $("autoAI").checked ? "1" : "0"));
    $("sumBtn").addEventListener("click", () => runSummary(true));
    $("sugBtn").addEventListener("click", () => runSuggest(true));
    $("scopeSel").addEventListener("change", () => runSummary(false));
    const inp = $("chatSearch");
    inp.addEventListener("focus", () => { loadSessions(); renderDropdown(); });
    inp.addEventListener("input", renderDropdown);
    inp.addEventListener("blur", () => setTimeout(() => $("chatDropdown").classList.add("hidden"), 120));
    inp.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") { inp.blur(); }
      if (ev.key === "Enter") { const first = $("chatDropdown").querySelector(".dd-item"); if (first) first.dispatchEvent(new MouseEvent("mousedown")); }
    });
    renderFollow();
    loadSessions(true).then(() => { pollActive(); });
    setInterval(() => { if (!document.hidden) { pollActive(); loadRecent(); loadSessions(); } }, POLL_MS);
  }
  init();
})();
