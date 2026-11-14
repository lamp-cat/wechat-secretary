// In-app Settings page. Talks only to the local gateway (/api/*, cookie-authenticated).
// Receive-only app: nothing here can send a WeChat message.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let revealed = false;

  async function api(method, path, body) {
    const opt = { method, credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    const r = await fetch(path, opt);
    if (r.status === 401) {
      location.href = "/login?next=/settings";
      throw new Error("unauthorized");
    }
    let data = null;
    try { data = await r.json(); } catch (_) { data = {}; }
    if (!r.ok) {
      const d = data || {};
      let m = d.error || (typeof d.detail === "string" ? d.detail : "") || ("HTTP " + r.status);
      if (Array.isArray(d.detail)) m = d.detail.map((x) => x.msg).join("; ");
      if (d.error && d.detail && typeof d.detail === "string") m += "\n" + d.detail;
      if (d.hint) m += "\n提示：" + d.hint;
      const e = new Error(m); e.data = d; throw e;
    }
    return data;
  }

  function say(el, text, kind) {
    const n = $(el);
    n.textContent = text || "";
    n.className = "msg" + (kind ? " " + kind : "");
  }

  async function busy(btn, fn) {
    const b = $(btn); b.disabled = true;
    try { return await fn(); } finally { b.disabled = false; }
  }

  function copyText(t) {
    try {
      if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.wfCopy) {
        window.webkit.messageHandlers.wfCopy.postMessage(t); return Promise.resolve();
      }
    } catch (_) {}
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(t).catch(() => legacyCopy(t));
    }
    return Promise.resolve(legacyCopy(t));
  }
  function legacyCopy(t) {
    const ta = document.createElement("textarea");
    ta.value = t; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); } finally { ta.remove(); }
  }

  // ---------------------------------------------------------------- AI
  function currentModel() {
    return ($("modelInput").value.trim() || $("modelSelect").value || "").trim();
  }
  function setModelOptions(list, selected) {
    const sel = $("modelSelect");
    sel.innerHTML = "";
    const first = document.createElement("option");
    first.value = ""; first.textContent = list.length ? `（${list.length} 个模型，选择一个）` : "（无列表，请手动输入）";
    sel.appendChild(first);
    for (const m of list) {
      const o = document.createElement("option");
      o.value = m; o.textContent = m; sel.appendChild(o);
    }
    if (selected && list.includes(selected)) { sel.value = selected; $("modelInput").value = ""; }
    else if (selected) { $("modelInput").value = selected; }
  }

  function probeBody() {
    const b = { base_url: $("baseUrl").value.trim() || null, proxy: $("proxy").value.trim(), model: currentModel() || null };
    const k = $("apiKey").value.trim();
    if (k) b.api_key = k;
    return b;
  }

  function renderAI(c) {
    $("aiEnabled").checked = !!c.enabled;
    $("baseUrl").value = c.base_url || "";
    $("preset").value = [...$("preset").options].some((o) => o.value === c.base_url) ? c.base_url : "";
    $("apiKey").value = "";
    $("clearKey").checked = false;
    $("keyHint").textContent = c.api_key_set ? `已保存：${c.api_key_masked}（留空 = 保持不变）` : "尚未保存 Key";
    $("proxy").value = c.proxy || "";
    $("temperature").value = c.temperature;
    $("maxTokens").value = c.max_tokens;
    $("aiFile").textContent = c.file || "";
    const cur = $("modelSelect").value;
    if (![...$("modelSelect").options].some((o) => o.value === c.model)) setModelOptions([], c.model);
    else if (cur !== c.model) $("modelSelect").value = c.model;
    const b = $("aiBadge");
    b.textContent = c.ready ? "已启用" : (c.enabled ? "缺少 Key" : "未启用");
    b.className = "badge" + (c.ready ? " ok" : "");
  }

  async function loadAI() {
    renderAI(await api("GET", "/api/ai-config"));
  }

  async function fetchModels() {
    say("aiMsg", "正在获取模型列表…");
    try {
      const r = await api("POST", "/api/ai-models", probeBody());
      setModelOptions(r.models || [], currentModel());
      say("aiMsg", `获取到 ${r.count} 个模型（${r.latency_ms} ms）。${r.count ? "请在下拉框中选择。" : "列表为空，请手动输入模型名。"}`, "ok");
    } catch (e) {
      say("aiMsg", "获取模型列表失败：" + e.message + "\n可以直接在右侧手动输入模型名。", "err");
    }
  }

  async function testAI() {
    if (!currentModel()) { say("aiMsg", "请先选择或输入模型", "err"); return; }
    say("aiMsg", "正在测试（发送一条极短的测试请求）…");
    try {
      const r = await api("POST", "/api/ai-test", probeBody());
      say("aiMsg", `✅ 连接成功：${r.model}，耗时 ${r.latency_ms} ms，回复「${r.reply}」。记得点「保存 AI 设置」。`, "ok");
    } catch (e) {
      say("aiMsg", "❌ 测试失败：" + e.message, "err");
    }
  }

  async function saveAI() {
    const body = {
      enabled: $("aiEnabled").checked,
      base_url: $("baseUrl").value.trim(),
      model: currentModel(),
      proxy: $("proxy").value.trim(),
      clear_api_key: $("clearKey").checked,
    };
    const k = $("apiKey").value.trim();
    if (k && !body.clear_api_key) body.api_key = k;
    const t = parseFloat($("temperature").value); if (!isNaN(t)) body.temperature = t;
    const m = parseInt($("maxTokens").value, 10); if (!isNaN(m)) body.max_tokens = m;
    try {
      const r = await api("PUT", "/api/ai-config", body);
      renderAI(r);
      say("aiMsg", r.ready ? "已保存，AI 已启用，立即生效。" : (r.enabled ? "已保存，但还没有 API Key。" : "已保存（AI 未启用）。"), r.ready ? "ok" : "");
    } catch (e) {
      say("aiMsg", "保存失败：" + e.message, "err");
    }
  }

  // ---------------------------------------------------------------- token
  async function loadToken() {
    const t = await api("GET", "/api/auth/token");
    revealed = false;
    $("tokenView").value = t.set ? t.masked : "（未设置）";
    $("btnReveal").textContent = "显示";
    $("tokenMeta").textContent = `${t.length} 位 · 文件 ${t.file}${t.mode ? "（权限 " + t.mode.replace("0o", "") + "）" : ""}`;
  }

  async function revealToken() {
    if (revealed) { await loadToken(); return; }
    const r = await api("POST", "/api/auth/token", { action: "reveal" });
    $("tokenView").value = r.token; revealed = true;
    $("btnReveal").textContent = "隐藏";
  }

  async function copyToken() {
    const r = await api("POST", "/api/auth/token", { action: "reveal" });
    await copyText(r.token);
    say("authMsg", "已复制到剪贴板。注意不要外传。", "ok");
  }

  async function rotateToken() {
    if (!confirm("确定重置访问令牌？\n旧令牌立即失效：其它浏览器需重新登录，飞书秒搭等外部调用需更新为新令牌。")) return;
    try {
      const r = await api("POST", "/api/auth/token", { action: "rotate" });
      await loadToken();
      say("authMsg", "✅ " + r.note, "ok");
    } catch (e) { say("authMsg", "重置失败：" + e.message, "err"); }
  }

  async function setToken() {
    const v = $("customToken").value.trim();
    if (!v) return;
    if (!confirm("确定把访问令牌改为你输入的值？旧令牌立即失效。")) return;
    try {
      const r = await api("POST", "/api/auth/token", { action: "set", token: v });
      $("customToken").value = "";
      await loadToken();
      say("authMsg", "✅ " + r.note, "ok");
    } catch (e) { say("authMsg", "设置失败：" + e.message, "err"); }
  }

  // ---------------------------------------------------------------- source
  function fmtFresh(f) {
    if (!f) return "—";
    if (typeof f === "string") return f;
    const parts = [];
    for (const k of ["state", "status", "lag_ms", "age_ms", "updated_at", "last_sync"]) if (f[k] !== undefined) parts.push(`${k}=${f[k]}`);
    return parts.join(" · ") || JSON.stringify(f).slice(0, 160);
  }

  async function loadSource() {
    const s = await api("GET", "/api/source");
    const rows = [
      ["微信账号", s.wxid || "未配置"],
      ["数据目录", (s.db_base_path || "—") + (s.db_dir_exists ? "  ✓" : "  ✗ 不存在")],
      ["微信版本", s.wechat_version || "未找到 /Applications/WeChat.app"],
      ["微信运行", s.wechat_running ? "运行中" : "未运行"],
      ["数据库密钥", s.keys_entries ? `${s.keys_entries} 个库已配置` : "未配置"],
      ["图片密钥", s.image_key ? "已配置" : "未配置"],
      ["读取服务", s.reader + (s.reader_status && s.reader_status.error ? "  ✗ " + s.reader_status.error : "  ✓")],
      ["数据新鲜度", fmtFresh(s.reader_status && s.reader_status.freshness)],
    ];
    const dl = $("srcList"); dl.innerHTML = "";
    for (const [k, v] of rows) {
      const dt = document.createElement("dt"); dt.textContent = k;
      const dd = document.createElement("dd"); dd.textContent = v;
      dl.append(dt, dd);
    }
    const ok = s.source_config && s.db_dir_exists && s.keys_entries > 0 && !(s.reader_status && s.reader_status.error);
    $("srcBadge").textContent = ok ? "已连接" : "需处理";
    $("srcBadge").className = "badge " + (ok ? "ok" : "bad");
  }

  async function doctor() {
    say("srcMsg", "正在验证密钥（逐个数据库校验首页，不输出密钥）…");
    try {
      const r = await api("POST", "/api/keys/doctor");
      $("doctorOut").textContent = r.output || "";
      $("doctorOut").classList.remove("hidden");
      const how = r.doctor === "ran" ? "逐库校验" : "以读取服务实时同步状态判断";
      say("srcMsg", r.ok ? `✅ 密钥有效（${how}），无需重新抓取。` : "❌ 验证未通过（见下方输出）。若刚升级过微信，可点「重新抓取密钥…」。", r.ok ? "ok" : "err");
    } catch (e) { say("srcMsg", "验证失败：" + e.message, "err"); }
  }

  async function recapture() {
    const msg = "重新抓取密钥是一次性的 Frida 步骤：\n\n" +
      "• 需要先完全退出微信（菜单 微信 → 退出）\n" +
      "• 会复制一份临时微信并临时签名后启动、捕获密钥，结束后自动清理进程\n" +
      "• 不修改正式版微信、不关闭 SIP；有一定账号风控风险\n" +
      "• 每一步都会在终端里让你确认\n\n确定在“终端”中打开引导脚本？";
    if (!confirm(msg)) return;
    try {
      const r = await api("POST", "/api/keys/recapture");
      say("srcMsg", r.note, "ok");
    } catch (e) { say("srcMsg", "打开失败：" + e.message, "err"); }
  }

  // ---------------------------------------------------------------- wire
  $("preset").addEventListener("change", () => { if ($("preset").value) $("baseUrl").value = $("preset").value; });
  $("toggleKey").addEventListener("click", () => { const i = $("apiKey"); i.type = i.type === "password" ? "text" : "password"; });
  $("modelSelect").addEventListener("change", () => { if ($("modelSelect").value) $("modelInput").value = ""; });
  $("btnModels").addEventListener("click", () => busy("btnModels", fetchModels));
  $("btnTest").addEventListener("click", () => busy("btnTest", testAI));
  $("btnSaveAI").addEventListener("click", () => busy("btnSaveAI", saveAI));
  $("btnReveal").addEventListener("click", () => revealToken().catch((e) => say("authMsg", e.message, "err")));
  $("btnCopy").addEventListener("click", () => copyToken().catch((e) => say("authMsg", e.message, "err")));
  $("btnRotate").addEventListener("click", () => busy("btnRotate", rotateToken));
  $("btnSetToken").addEventListener("click", () => busy("btnSetToken", setToken));
  $("btnRefreshSrc").addEventListener("click", () => busy("btnRefreshSrc", () => loadSource().catch((e) => say("srcMsg", e.message, "err"))));
  $("btnDoctor").addEventListener("click", () => busy("btnDoctor", doctor));
  $("btnRecapture").addEventListener("click", recapture);

  loadAI().catch((e) => say("aiMsg", e.message, "err"));
  loadToken().catch((e) => say("authMsg", e.message, "err"));
  loadSource().catch((e) => say("srcMsg", e.message, "err"));
})();
