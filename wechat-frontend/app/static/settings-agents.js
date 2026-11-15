// Settings → Agent 工具接入（MCP）. Talks only to the local gateway. Receive-only: no send path.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const inApp = !!(window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.wfCopy);
  let info = null;

  async function api(method, path, body) {
    const opt = { method, credentials: "same-origin", headers: {} };
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    const r = await fetch(path, opt);
    if (r.status === 401) { location.href = "/login?next=/settings"; throw new Error("unauthorized"); }
    let d = {}; try { d = await r.json(); } catch (_) {}
    if (!r.ok) throw new Error((d && (d.detail || d.error)) || ("HTTP " + r.status));
    return d;
  }
  function say(t, kind) { const m = $("agMsg"); m.textContent = t || ""; m.className = "msg" + (kind ? " " + kind : ""); }
  function copy(t) {
    if (inApp) { window.webkit.messageHandlers.wfCopy.postMessage(t); return Promise.resolve(); }
    return navigator.clipboard.writeText(t);
  }
  const RES = { added: "已接入", updated: "已更新", unchanged: "无需改动", removed: "已撤销", absent: "原本就没有",
    skipped: "跳过", "user-managed": "已有同名配置（你自己加的），未改动", error: "失败" };

  function render(st) {
    const list = $("agList"); list.textContent = "";
    let n = 0;
    (st.tools || []).forEach((t) => {
      const row = document.createElement("div"); row.className = "agrow";
      const name = document.createElement("span");
      name.textContent = t.label;
      const k = document.createElement("span"); k.className = "k";
      let state;
      if (!t.detected && !t.registered) state = "未检测到";
      else if (t.error) state = "⚠️ " + t.error;
      else if (t.user_managed) state = "已有同名配置（未改动）";
      else if (t.registered) { state = t.up_to_date ? "✓ 已接入" : "已接入（待更新）"; n++; }
      else if (t.optout) state = "已撤销";
      else state = "未接入";
      k.textContent = state;
      k.title = t.config + (t.backup ? "\n备份：" + t.backup : "");
      const b = document.createElement("button"); b.type = "button"; b.className = "btn";
      if (t.registered && !t.user_managed) { b.textContent = "撤销"; b.onclick = () => act("unregister", [t.id]); }
      else { b.textContent = "接入"; b.onclick = () => act("register", [t.id]); b.disabled = !t.detected || t.user_managed || !!t.error; }
      row.append(name, k, b);
      list.append(row);
    });
    const badge = $("agBadge");
    badge.textContent = n ? ("已接入 " + n + " 个") : "未接入";
    badge.className = "badge" + (n ? " ok" : "");
    if (!st.reader_bin_exists) say("找不到读取器程序：" + st.command, "err");
  }
  async function load() {
    try { render(await api("GET", "/api/agents")); } catch (e) { say(e.message, "err"); }
    try {
      info = await api("GET", "/api/mcp-info");
      const dl = $("mcpInfo"); dl.textContent = "";
      const add = (k, v) => { const dt = document.createElement("dt"); dt.textContent = k; const dd = document.createElement("dd"); dd.textContent = v; dl.append(dt, dd); };
      add("stdio 命令", info.stdio.command + " " + info.stdio.args.join(" "));
      add("HTTP 地址", info.http.url);
      add("HTTP 鉴权", info.http.auth);
      add("可用工具", info.tools.join(", "));
    } catch (_) {}
  }
  async function act(action, tools) {
    if (action === "unregister" && !confirm("从该工具中移除微信读取器？（会先备份配置文件；其它配置不受影响）")) return;
    say("处理中…");
    try {
      const r = await api("POST", "/api/agents", { action, tools });
      render(r.status);
      say(r.results.map((x) => {
        const lab = ((r.status.tools || []).find((t) => t.id === x.id) || {}).label || x.id;
        return lab + "：" + (RES[x.result] || x.result) + (x.why ? "（" + x.why + "）" : "");
      }).join("\n") + (r.ok ? "\n\n已改动的工具需要重启（或新开会话）后生效。" : ""), r.ok ? "ok" : "err");
    } catch (e) { say(e.message, "err"); }
  }
  $("btnAgAll").onclick = () => act("register", null);
  $("btnAgRefresh").onclick = load;
  $("btnCopyStdio").onclick = async () => {
    if (!info) return;
    const j = { mcpServers: { "wechat-reader": { command: info.stdio.command, args: info.stdio.args, env: info.stdio.env } } };
    await copy(JSON.stringify(j, null, 2)); say("已复制 stdio 配置（不含令牌）。", "ok");
  };
  $("btnCopyReaderTok").onclick = async () => {
    if (!confirm("复制读取器 HTTP 令牌？令牌 = 完整读权限，只在本机使用。")) return;
    try { const r = await api("POST", "/api/mcp-info/token", { action: "reveal" }); await copy(r.token); say("已复制到剪贴板（未显示）。", "ok"); }
    catch (e) { say(e.message, "err"); }
  };
  load();
})();
