#!/bin/bash
# 消息工作台 · 首次启动全自动配置（由 App 调用，也可在终端里单独运行；可重复运行、幂等）。
#
# 做的事：安装/更新运行时 → 自动找到本机微信账号数据目录 → 复用或导入密钥并校验
#        → 生成网关配置与令牌(0600) → 建 venv → 写入并启动 launchd 保活服务
#        → 把只读 MCP 读取器自动登记到本机检测到的 agent 工具（Codex / Claude / Cursor / VS Code）。
# 需要用户亲自做的事（登录微信、授权、抓密钥前退出微信）不会硬做，而是输出 @@need，由 App 显示按钮。
#
# 输出协议（每行一条，App 解析）：
#   @@step <TAB> id <TAB> run|ok|warn|fail|skip <TAB> 说明
#   @@need <TAB> code <TAB> 说明          # 需要用户操作；脚本随后以 10 退出
#   @@root <TAB> 运行时目录
#   @@done <TAB> ok|need|fail
# 其他行是普通日志。绝不输出密钥或令牌。只读：没有任何发送微信消息的能力。
set -uo pipefail
umask 077
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

WD_HOME="${WD_HOME:-$HOME}"
export HOME="$WD_HOME"
PAYLOAD="${WD_PAYLOAD:-$(cd "$(dirname "$0")/.." && pwd)}"   # app bundle payload, or the repo itself
PREFIX="${WD_PREFIX:-com.gly}"
RPORT="${WD_READER_PORT:-5678}"
GPORT="${WD_GATEWAY_PORT:-8088}"
[[ -n "${WD_WECHAT_FILES:-}" ]] && export WECHAT_FILES_ROOT="$WD_WECHAT_FILES"
UIDN="$(id -u)"
CFG_RD="$HOME/.config/wechat-realtime-reader"
CFG_FE="$HOME/.config/wechat-frontend"
CFG_WD="$HOME/.config/wechatdesk"
LOGS="$HOME/Library/Logs/WeChatDesk"
LA="$HOME/Library/LaunchAgents"
READER_LABEL="$PREFIX.wechatreader.serve"
FRONT_LABEL="$PREFIX.wechatfrontend"
MANAGED_ROOT="$HOME/.local/share/wechatdesk/runtime"

step() { printf '@@step\t%s\t%s\t%s\n' "$1" "$2" "$3"; }
need() { printf '@@need\t%s\t%s\n' "$1" "$2"; printf '@@done\tneed\n'; exit 10; }
fail() { step "$1" fail "$2"; printf '@@done\tfail\n'; exit 1; }
loaded() { launchctl print "gui/$UIDN/$1" >/dev/null 2>&1; }
# run with a timeout; output to file $1
run_to() { local out="$1" t="$2"; shift 2; "$@" >"$out" 2>&1 & local pid=$!
  ( sleep "$t"; kill "$pid" 2>/dev/null ) & local w=$!; wait "$pid"; local rc=$?; kill "$w" 2>/dev/null; wait "$w" 2>/dev/null; return $rc; }

[[ "$(uname)" == "Darwin" ]] || fail env "只支持 macOS"
mkdir -p "$CFG_RD" "$CFG_FE" "$CFG_WD" "$LOGS" "$LA"
chmod 700 "$CFG_RD" "$CFG_FE" "$CFG_WD" "$LOGS"
TMPD="$(mktemp -d "$CFG_WD/.tmp.XXXXXX")"; trap 'rm -rf "$TMPD"' EXIT

# ---------------------------------------------------------------- 1. runtime
step runtime run "准备运行环境"
ROOT="${WD_ROOT:-}"
if [[ -z "$ROOT" ]]; then
  ROOT="$(defaults read com.gly.wechatdesk PlatformRoot 2>/dev/null || true)"
  [[ -n "$ROOT" && -x "$ROOT/wechat-realtime-reader/bin/wechat-reader" ]] || ROOT=""
fi
if [[ -z "$ROOT" && -x "$HOME/wechat-platform/wechat-realtime-reader/bin/wechat-reader" && -f "$HOME/wechat-platform/wechat-frontend/scripts/start.sh" ]]; then
  ROOT="$HOME/wechat-platform"     # existing source install: use it as-is
fi
[[ -n "$ROOT" ]] || ROOT="$MANAGED_ROOT"
MODE=external
# Treat as managed (copy from payload) when the target has no binary yet, even if WD_ROOT was set.
if [[ "$ROOT" == "$MANAGED_ROOT" || ! -x "$ROOT/wechat-realtime-reader/bin/wechat-reader" ]]; then
  MODE=managed
fi
if [[ $MODE == managed ]]; then
  want="$(cat "$PAYLOAD/VERSION" 2>/dev/null || echo dev)"
  have="$(cat "$ROOT/VERSION" 2>/dev/null || echo none)"
  if [[ "$want" != "$have" || ! -x "$ROOT/wechat-realtime-reader/bin/wechat-reader" ]]; then
    mkdir -p "$ROOT"
    for d in wechat-realtime-reader wechat-frontend setup; do
      [[ -d "${PAYLOAD}/${d}" ]] || fail runtime "安装包不完整：缺少 ${d}"
      # keep the venv; replace code
      rsync -a --delete --exclude '.venv' --exclude '__pycache__' "${PAYLOAD}/${d}/" "${ROOT}/${d}/" || fail runtime "复制运行时失败"
    done
    cp "$PAYLOAD/VERSION" "$ROOT/VERSION" 2>/dev/null || echo dev > "$ROOT/VERSION"
    chmod -R go-w "$ROOT"
    CODE_UPDATED=1
    step runtime ok "已安装运行时 ${want} → ${ROOT}"
  else
    step runtime ok "运行时已是最新（${have}）"
  fi
else
  step runtime ok "使用已有安装：${ROOT}"
fi
CODE_UPDATED="${CODE_UPDATED:-0}"
BIN="$ROOT/wechat-realtime-reader/bin/wechat-reader"
FE="$ROOT/wechat-frontend"
[[ -x "${BIN}" ]] || fail runtime "找不到读取器程序：${BIN}"
printf '@@root\t%s\n' "$ROOT"

# ---------------------------------------------------------------- 2. WeChat + account
step wechat run "查找本机微信"
WECHAT_APP=""
for a in /Applications/WeChat.app "$HOME/Applications/WeChat.app"; do [[ -d "$a" ]] && WECHAT_APP="$a" && break; done
[[ -z "$WECHAT_APP" ]] && WECHAT_APP="$(mdfind 'kMDItemCFBundleIdentifier == "com.tencent.xinWeChat"' 2>/dev/null | head -1)"
if [[ -n "$WECHAT_APP" ]]; then
  ver="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$WECHAT_APP/Contents/Info.plist" 2>/dev/null)"
  step wechat ok "微信 ${ver}: ${WECHAT_APP}"
else
  step wechat warn "没有在「应用程序」里找到微信"
fi

SRC="$CFG_RD/source-config.json"
src_ok() { [[ -f "$SRC" ]] && /usr/bin/plutil -extract db_base_path raw -o - "$SRC" >/dev/null 2>&1 && [[ -d "$(/usr/bin/plutil -extract db_base_path raw -o - "$SRC" 2>/dev/null | sed "s#^~#$HOME#")" ]]; }
step account run "查找微信账号数据目录"
if src_ok; then
  step account ok "已绑定账号：$(/usr/bin/plutil -extract wxid raw -o - "${SRC}" 2>/dev/null)"
else
  if [[ -f "$SRC" ]]; then cp -p "$SRC" "$SRC.bak-$(date +%Y%m%d-%H%M%S)"; FORCE=-force; else FORCE=; fi
  if ! run_to "$TMPD/detect" 30 "$BIN" detect -write $FORCE -json; then
    if grep -qi "permission denied" "$TMPD/detect"; then
      need data_permission "没有权限读取微信数据目录。请在弹出的系统提示里点「允许」；如果没有弹窗，请到 系统设置 → 隐私与安全性 → 完全磁盘访问权限，打开「消息工作台」后点重试。"
    fi
    [[ -z "${WECHAT_APP}" ]] && need wechat_missing "这台 Mac 上还没有安装微信 4.x。请先安装并登录微信，然后点重试。"
    need wechat_login "还没找到微信账号数据。请打开微信并扫码登录一次（保持登录），然后点重试。"
  fi
  n="$(grep -c '"wxid"' "$TMPD/detect")"
  chosen="$(/usr/bin/plutil -extract chosen raw -o - "$TMPD/detect" 2>/dev/null)"
  step account ok "自动绑定账号 ${chosen}（共发现 ${n} 个账号目录，取最近活跃的一个）"
fi
DB_ROOT="$(/usr/bin/plutil -extract db_base_path raw -o - "$SRC" 2>/dev/null | sed "s#^~#$HOME#")"

# ---------------------------------------------------------------- 3. keys
step keys run "检查数据库密钥"
KEYS="$CFG_RD/keys.json"
if [[ ! -s "$KEYS" && -n "${WD_IMPORT_KEYS_FROM:-}" && -s "$WD_IMPORT_KEYS_FROM/keys.json" ]]; then
  # reuse keys captured earlier by another install on this Mac (same account only)
  other="$(/usr/bin/plutil -extract db_base_path raw -o - "$WD_IMPORT_KEYS_FROM/source-config.json" 2>/dev/null | sed "s#^~#${WD_IMPORT_HOME:-$HOME}#")"
  if [[ "$other" == "$DB_ROOT" ]]; then
    cp "$WD_IMPORT_KEYS_FROM/keys.json" "$KEYS"; chmod 600 "$KEYS"
    [[ -s "$WD_IMPORT_KEYS_FROM/image-key.json" && ! -e "$CFG_RD/image-key.json" ]] && cp "$WD_IMPORT_KEYS_FROM/image-key.json" "$CFG_RD/image-key.json" && chmod 600 "$CFG_RD/image-key.json"
    echo "已从 ${WD_IMPORT_KEYS_FROM} 导入已有密钥（同一账号）。"
  fi
fi
if [[ ! -s "$KEYS" ]]; then
  need keys "需要抓取一次微信数据库密钥（只在首次或微信大版本升级后需要）。点「开始抓取」后按窗口里的提示操作，全程不修改正式版微信。"
fi
chmod 600 "$KEYS" "$SRC" 2>/dev/null
WECHAT_READER_CONFIG="$CFG_RD" run_to "$TMPD/doctor" 60 "$BIN" doctor; rc=$?
okn="$(grep -c ' ok (' "$TMPD/doctor")"; badn="$(grep -cE 'FAIL|unreadable|bad key' "$TMPD/doctor")"
if grep -q "permission denied" "$TMPD/doctor"; then
  need data_permission "没有权限读取微信数据库。请到 系统设置 → 隐私与安全性 → 完全磁盘访问权限，打开「消息工作台」和读取器程序 wechat-reader（可点「在访达中显示读取器」拖进去），然后点重试。"
fi
if [[ $rc -ne 0 && "$okn" -eq 0 ]]; then
  if [[ $rc -gt 128 ]]; then
    need data_permission "密钥校验超时，通常是 macOS 在等你授权访问其他 App 的数据。请在系统弹窗里点「允许」后重试。"
  fi
  need keys_failed "现有密钥校验未通过（常见于微信升级后）。需要重新抓取一次密钥。"
fi
if [[ "$badn" -gt 0 ]]; then
  step keys warn "密钥校验：${okn} 个库通过，${badn} 个未通过（主要消息库可用即可；如消息缺失可在设置里重新抓取）"
else
  step keys ok "密钥校验通过（${okn} 个库）"
fi

# ---------------------------------------------------------------- 4. python / venv
step python run "准备网关运行环境（Python）"
PY=""
if xcode-select -p >/dev/null 2>&1 && /usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then PY=/usr/bin/python3
elif [[ -x /opt/homebrew/bin/python3 ]]; then PY=/opt/homebrew/bin/python3
elif [[ -x /usr/local/bin/python3 ]]; then PY=/usr/local/bin/python3
fi
[[ -n "${WD_PYTHON:-}" ]] && PY="$WD_PYTHON"
[[ -n "${PY}" ]] || need clt "需要先安装 Apple 命令行工具（自带 Python，约几分钟）。点「安装」后在系统弹窗里确认，装好后点重试。"
VENV="$FE/.venv"
REQ_SHA="$(shasum -a 256 "$FE/requirements.txt" | cut -c1-16)"
if [[ ! -x "$VENV/bin/python" || "$(cat "$VENV/.wd-req" 2>/dev/null)" != "$REQ_SHA" ]]; then
  if [[ $MODE == external && -x "$VENV/bin/python" ]]; then
    step python ok "使用已有虚拟环境"
  else
    echo "创建虚拟环境并安装依赖（首次约 1 分钟，需要联网）…"
    "${PY}" -m venv "${VENV}" || fail python "创建虚拟环境失败"
    "$VENV/bin/python" -m pip install --disable-pip-version-check -q -U pip >/dev/null 2>&1
    "$VENV/bin/python" -m pip install --disable-pip-version-check --progress-bar off -r "$FE/requirements.txt" 2>&1 | grep -E '^(Collecting|Successfully|ERROR)' || true
    "${VENV}/bin/python" -c 'import fastapi, uvicorn, httpx' 2>/dev/null || fail python "安装 Python 依赖失败（请检查网络后重试）"
    echo "$REQ_SHA" > "$VENV/.wd-req"
    step python ok "依赖已安装"
  fi
else
  step python ok "运行环境就绪"
fi

# ---------------------------------------------------------------- 5. gateway config + token
step config run "生成本机配置"
if [[ ! -f "$CFG_FE/config.json" ]]; then
  cat > "$CFG_FE/config.json" <<JSON
{
  "bind_host": "127.0.0.1",
  "bind_port": $GPORT,
  "reader_base": "http://127.0.0.1:$RPORT",
  "cors_origins": []
}
JSON
fi
if [[ ! -s "$CFG_FE/gateway-token" ]]; then
  LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40 > "$CFG_FE/gateway-token"
fi
chmod 600 "$CFG_FE/config.json" "$CFG_FE/gateway-token"
step config ok "配置与访问令牌已就绪（0600，内容不显示）"

# ---------------------------------------------------------------- 6. launchd services
step services run "启动后台服务（launchd 保活）"
write_plist() { # label out args...
  local label="$1" out="$2"; shift 2
  local args="" a; for a in "$@"; do args+="<string>$(printf '%s' "$a" | sed 's/&/\&amp;/g;s/</\&lt;/g')</string>"; done
  cat > "$out" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <!-- generated by WeChatDesk (消息工作台) bootstrap.sh -->
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>$args</array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>Umask</key><integer>63</integer>
  <key>StandardOutPath</key><string>$LOGS/${label##*.wechat}.log</string>
  <key>StandardErrorPath</key><string>$LOGS/${label##*.wechat}.log</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key><string>$HOME</string>
    <key>WECHAT_READER_CONFIG</key><string>$CFG_RD</string>
    <key>WF_PLATFORM_ROOT</key><string>$ROOT</string>
  </dict>
</dict></plist>
PL
}
ensure_service() { # label args...
  local label="$1"; shift
  local p="$LA/$label.plist" changed=0
  if [[ -f "$p" ]] && ! grep -q "generated by WeChatDesk" "$p"; then
    : # a plist we did not write (older install): keep it untouched
  else
    write_plist "$label" "$TMPD/$label.plist" "$@"
    if ! cmp -s "$TMPD/$label.plist" "$p"; then cp "$TMPD/$label.plist" "$p"; chmod 644 "$p"; changed=1; fi
  fi
  if loaded "$label"; then
    if [[ $changed == 1 ]]; then launchctl bootout "gui/$UIDN/$label" 2>/dev/null; sleep 1; launchctl bootstrap "gui/$UIDN" "$p"
    elif [[ $CODE_UPDATED == 1 ]]; then launchctl kickstart -k "gui/$UIDN/$label" 2>/dev/null; fi
  else
    launchctl bootstrap "gui/$UIDN" "$p" 2>/dev/null || launchctl kickstart "gui/$UIDN/$label" 2>/dev/null
  fi
  loaded "$label"
}
ensure_service "${READER_LABEL}" "${BIN}" serve -addr "127.0.0.1:${RPORT}" || fail services "读取服务启动失败（日志：${LOGS}/reader.serve.log）"
ensure_service "${FRONT_LABEL}" "${FE}/scripts/start.sh" || fail services "网关启动失败（日志：${LOGS}/frontend.log）"
step services ok "读取服务 127.0.0.1:${RPORT}、网关 127.0.0.1:${GPORT} 已由 launchd 保活"

# ---------------------------------------------------------------- 7. agent tools (MCP)
if [[ "${WD_SKIP_AGENTS:-0}" == 1 ]]; then
  step agents skip "已跳过自动接入 agent 工具"
else
  step agents run "把只读 MCP 读取器接入本机 agent 工具"
  REG="$PAYLOAD/wechat-frontend/app/agent_register.py"; [[ -f "$REG" ]] || REG="$FE/app/agent_register.py"
  APY="$VENV/bin/python"; [[ -x "$APY" ]] || APY="$PY"
  if "$APY" "$REG" auto --bin "$BIN" --config-dir "$CFG_RD" > "$TMPD/agents" 2>&1; then
    added="$(awk -F'\t' '$2=="added"||$2=="updated"{print $1}' "$TMPD/agents" | paste -sd, -)"
    have="$(awk -F'\t' '$2=="unchanged"{print $1}' "$TMPD/agents" | paste -sd, -)"
    errs="$(awk -F'\t' '$2=="error"{print $1": "$4}' "$TMPD/agents" | paste -sd';' -)"
    msg=""; [[ -n "${added}" ]] && msg+="新接入：${added}；"; [[ -n "${have}" ]] && msg+="已接入：${have}；"
    [[ -z "${msg}" ]] && msg="没有检测到支持的 agent 工具（可在设置里随时接入）"
    if [[ -n "${errs}" ]]; then step agents warn "${msg} 失败：${errs}"; else step agents ok "${msg}"; fi
  else
    step agents warn "自动接入 agent 工具未完成（可在设置 → Agent 工具接入里重试）"
  fi
fi

printf '@@done\tok\n'
exit 0
