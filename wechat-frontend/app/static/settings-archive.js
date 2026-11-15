// Settings: local message archive (opt-in, default OFF). Talks only to the local gateway.
// Receive-only app: nothing here can send a message.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let cfg = null;
  let sessions = [];
  let selected = new Set();

  async function api(method, path, body) {
    const opt = { method, credentials: "same-origin", headers: {} };
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    const r = await fetch(path, opt);
    if (r.status === 401) { location.href = "/login?next=/settings"; throw new Error("unauthorized"); }
    let d = {}; try { d = await r.json(); } catch (_) {}
    if (!r.ok) {
      let m = (typeof d.detail === "string" ? d.detail : "") || ("HTTP " + r.status);
      if (Array.isArray(d.detail)) m = d.detail.map((x) => x.msg).join("; ");
      throw new Error(m);
    }
    return d;
  }
  function say(text, kind) { const n = $("arcMsg"); n.textContent = text || ""; n.className = "msg" + (kind ? " " + kind : ""); }
  async function busy(id, fn) { const b = $(id); b.disabled = true; try { return await fn(); } catch (e) { say(e.message, "err"); } finally { b.disabled = false; } }
  const mode = () => (document.querySelector('input[name="arcMode"]:checked') || {}).value || "all";
  const kindCn = { chatroom: "群聊", friend: "私聊", stranger: "私聊", official: "公众号" };

  function renderStatus(sum) {
    const st = sum.status || {}, s = sum.stats;
    const on = !!(sum.config && sum.config.enabled);
    const b = $("arcBadge");
    b.textContent = on ? (st.running ? "运行中" : "已开启") : "已关闭";
    b.className = "badge" + (on ? (st.last_error ? " bad" : " ok") : "");
    const rows = [["状态", on ? (st.phase === "syncing" ? "同步中…" : "空闲，等待新消息") : "未开启"]];
    if (s) {
      const a = s.attachments || {};
      const parts = Object.keys(a).map((k) => `${k} ${a[k]}`).join("，");
      rows.push(["已归档", `${s.conversations} 个会话，${s.messages} 条消息`]);
      rows.push(["附件", parts || "无"]);
      if (s.backfill_pending) rows.push(["补历史", `还有 ${s.backfill_pending} 个会话在补`]);
    } else rows.push(["已归档", "（目录中还没有归档）"]);
    rows.push(["最后同步", st.last_sync_at || "—"]);
    if (st.last_error) rows.push(["错误", st.last_error]);
    if (st.dir_error) rows.push(["目录", st.dir_error]);
    const dl = $("arcStatus"); dl.innerHTML = "";
    for (const [k, v] of rows) {
      const dt = document.createElement("dt"); dt.textContent = k;
      const dd = document.createElement("dd"); dd.textContent = v;
      dl.append(dt, dd);
    }
  }

  function fill(sum) {
    cfg = sum.config;
    $("arcEnabled").checked = !!cfg.enabled;
    for (const r of document.querySelectorAll('input[name="arcMode"]')) r.checked = r.value === cfg.mode;
    selected = new Set(cfg.selected_chats || []);
    $("arcDir").value = cfg.archive_dir || "";
    $("arcDir").disabled = !!cfg.enabled;
    $("arcBackfill").checked = !!cfg.backfill_history;
    $("arcDays").value = cfg.backfill_days || 0;
    $("arcMedia").checked = cfg.copy_media !== false;
    $("arcInterval").value = cfg.interval_sec || 15;
    togglePicker();
    renderStatus(sum);
  }

  function renderList() {
    const kw = $("arcSearch").value.trim().toLowerCase();
    const box = $("arcList"); box.innerHTML = "";
    const list = sessions.filter((s) => !kw || (s.name || "").toLowerCase().includes(kw));
    for (const s of list.slice(0, 400)) {
      const lab = document.createElement("label");
      const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = selected.has(s.username);
      cb.addEventListener("change", () => { cb.checked ? selected.add(s.username) : selected.delete(s.username); count(); });
      const nm = document.createElement("span"); nm.textContent = s.name || "(未命名)";
      const k = document.createElement("span"); k.className = "k"; k.textContent = kindCn[s.kind] || s.kind || "";
      lab.append(cb, nm, k); box.appendChild(lab);
    }
    if (!list.length) box.textContent = "没有匹配的会话";
    count();
  }
  function count() { $("arcSelCount").textContent = `已选 ${selected.size} 个`; }

  async function togglePicker() {
    const show = mode() === "selected";
    $("arcPicker").classList.toggle("hidden", !show);
    if (show && !sessions.length) {
      try { sessions = (await api("GET", "/api/sessions?limit=1000")).sessions || []; } catch (e) { say("会话列表读取失败：" + e.message, "err"); }
      renderList();
    }
  }

  async function load() { fill(await api("GET", "/api/archive")); }

  async function save() {
    const enable = $("arcEnabled").checked;
    const body = {
      mode: mode(),
      selected_chats: Array.from(selected),
      backfill_history: $("arcBackfill").checked,
      backfill_days: parseInt($("arcDays").value || "0", 10) || 0,
      copy_media: $("arcMedia").checked,
      interval_sec: parseInt($("arcInterval").value || "15", 10) || 15,
      enabled: enable,
    };
    if (!cfg.enabled) body.archive_dir = $("arcDir").value.trim();
    if (body.mode === "selected" && enable && !body.selected_chats.length) { say("「只归档指定对话」模式下请至少勾选一个会话。", "err"); return; }
    if (enable && !cfg.enabled) {
      const dir = body.archive_dir || cfg.archive_dir;
      const scope = body.mode === "all" ? "全部对话（群聊和私聊，以后新出现的会话也会自动纳入）" : `你勾选的 ${body.selected_chats.length} 个对话`;
      const msg = "确定开启消息归档？\n\n" +
        `• 归档范围：${scope}\n` +
        `• 存放位置：${dir}（只存本机，不上传云端）\n` +
        "• 谁能读取：这台电脑上以你的账户运行的任何程序都能读取该目录，包括你授权读取它的 AI agent（如豆包）。请只把这个目录授权给你信任的工具。\n" +
        "• 随时可以关闭；关闭后立即停止同步，已归档的数据保留，需要时可在下方手动删除。";
      if (!confirm(msg)) { $("arcEnabled").checked = false; return; }
      body.confirm = true;
    }
    fill(await api("PUT", "/api/archive", body));
    say(enable ? "✅ 已保存，归档运行中。" : "✅ 已保存。归档处于关闭状态。", "ok");
  }

  async function syncNow() {
    await api("POST", "/api/archive/sync");
    say("已触发同步，几秒后点「刷新状态」查看。", "ok");
    setTimeout(() => api("GET", "/api/archive").then(renderStatus).catch(() => {}), 3000);
  }

  async function del() {
    const dir = (cfg && cfg.archive_dir) || "";
    if (!confirm(`将关闭归档并删除整个目录：\n${dir}\n\n删除后不可恢复。确定继续？`)) return;
    const t = prompt("请输入「删除」两个字以确认：");
    if (t !== "删除") { say("已取消，未删除任何数据。", ""); return; }
    const r = await api("POST", "/api/archive/delete", { confirm: "DELETE" });
    say(r.deleted ? "已删除归档目录，归档已关闭。" : ("未删除：" + (r.detail || "")), r.deleted ? "ok" : "");
    await load();
  }

  for (const r of document.querySelectorAll('input[name="arcMode"]')) r.addEventListener("change", togglePicker);
  $("arcSearch").addEventListener("input", renderList);
  $("btnArcSave").addEventListener("click", () => busy("btnArcSave", save));
  $("btnArcSync").addEventListener("click", () => busy("btnArcSync", syncNow));
  $("btnArcRefresh").addEventListener("click", () => busy("btnArcRefresh", load));
  $("btnArcDelete").addEventListener("click", () => busy("btnArcDelete", del));
  load().catch((e) => say(e.message, "err"));
  setInterval(() => { if (cfg && cfg.enabled && !document.hidden) api("GET", "/api/archive").then(renderStatus).catch(() => {}); }, 10000);
})();
