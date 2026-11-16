#!/bin/bash
# 应用内「抓密钥」流程（由 App 调用）。对微信做一份临时签名副本 + Frida hook，不改正式版、不关 SIP。
# 输出协议同 bootstrap.sh：@@step / @@need / @@done。绝不输出密钥本身。
set -uo pipefail
umask 077
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
HOME="${WD_HOME:-$HOME}"; export HOME
ROOT="${WD_ROOT:-$HOME/.local/share/wechatdesk/runtime}"
CFG="$HOME/.config/wechat-realtime-reader"
BIN="$ROOT/wechat-realtime-reader/bin/wechat-reader"
SKILL="$ROOT/wechat-realtime-reader/skills/wechat-db-keys"
# Prefer the skill shipped next to this script (app payload): it carries the bundled frida-inject
# and the newest keys.py even when ROOT is an older external install.
HERE="$(cd "$(dirname "$0")" && pwd)"
PSKILL="$HERE/../wechat-realtime-reader/skills/wechat-db-keys"
[[ -f "$PSKILL/scripts/keys.py" ]] && SKILL="$(cd "$PSKILL" && pwd)"
export WD_FRIDA_VENDOR_DIRS="$SKILL/vendor/frida:$ROOT/wechat-realtime-reader/skills/wechat-db-keys/vendor/frida"
APP="${WD_WECHAT_APP:-/Applications/WeChat.app}"
UIDN="$(id -u)"
TS="$(date +%Y%m%d-%H%M%S)"
capture_work=""; keepalive_paused=0; keepalive_label=""
KEEP="${WD_KEEPALIVE_LABELS:-com.gly.wechatkeepalive com.lampcat.wechatkeepalive}"

step(){ printf '@@step\t%s\t%s\t%s\n' "$1" "$2" "$3"; }
need(){ printf '@@need\t%s\t%s\n' "$1" "$2"; printf '@@done\tneed\n'; exit 10; }
fail(){ step "$1" fail "$2"; printf '@@done\tfail\n'; exit 1; }

cleanup(){
  if [[ -n "$capture_work" && -d "$capture_work" ]]; then
    pids="$(ps -axo pid=,command= | grep -F "$capture_work/WeChat.app/" | grep -v grep | awk '{print $1}')"
    [[ -n "$pids" ]] && kill -TERM $pids 2>/dev/null
    sleep 1
    # move leftover copy to Trash (best-effort)
    mv "$capture_work" "$HOME/.Trash/" 2>/dev/null || rm -rf "$capture_work"
    capture_work=""
  fi
  if [[ $keepalive_paused == 1 && -n "$keepalive_label" ]]; then
    launchctl bootstrap "gui/$UIDN" "$HOME/Library/LaunchAgents/$keepalive_label.plist" 2>/dev/null || true
  fi
}
trap cleanup EXIT

[[ -d "${APP}" ]] || need wechat_missing "找不到微信：${APP}。请先安装微信 4.x。"
[[ -x "${BIN}" ]] || fail env "找不到读取器：${BIN}"
[[ -f "${SKILL}/scripts/keys.py" ]] || fail env "缺少抓密钥脚本"
[[ -f "${CFG}/source-config.json" ]] || need wechat_login "还没有账号配置。请先完成首次启动。"
# stdlib-only Python is enough now (no pip): prefer the gateway venv, then system python3
PY=""
for c in "$ROOT/wechat-frontend/.venv/bin/python" /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  [[ "$c" == /usr/bin/python3 ]] && ! xcode-select -p >/dev/null 2>&1 && continue
  [[ -x "$c" ]] && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1 && { PY="$c"; break; }
done
[[ -n "$PY" ]] || fail env "找不到可用的 Python 3.8+（请先完成首次启动）"
DB_ROOT="$("$PY" -c "import json,os;print(os.path.expanduser(json.load(open('$CFG/source-config.json'))['db_base_path']))")"
[[ -d "${DB_ROOT}" ]] || need wechat_login "微信数据目录不存在。请登录微信一次后再试。"

step quit run "检查微信是否已退出"
for lab in $KEEP; do
  if launchctl print "gui/$UIDN/$lab" >/dev/null 2>&1; then
    launchctl bootout "gui/$UIDN/$lab" 2>/dev/null && keepalive_paused=1 && keepalive_label="$lab"
  fi
done
if pgrep -f "$APP/Contents/MacOS/WeChat" >/dev/null; then
  need wechat_quit "请先在微信菜单选择「退出微信」（关窗口不算）。退出后点「我已退出，继续」。"
fi
step quit ok "微信已退出"

step prep run "检查内置 Frida（无需联网安装；首次校验可能要一两分钟）"
FRIDA_INFO="$("$PY" "$SKILL/scripts/keys.py" frida-check 2>/dev/null | head -1)"
if [[ "$FRIDA_INFO" == backend=none* || -z "$FRIDA_INFO" ]]; then
  step prep warn "未找到内置 Frida，尝试从 GitHub 官方 Release 下载（校验 sha256，不走 pip）"
  "$PY" "$SKILL/vendor/frida/fetch.py" --dest "$ROOT/wechat-realtime-reader/skills/wechat-db-keys/vendor/frida" >/dev/null 2>&1 || true
  FRIDA_INFO="$("$PY" "$SKILL/scripts/keys.py" frida-check 2>/dev/null | head -1)"
  [[ "$FRIDA_INFO" == backend=none* || -z "$FRIDA_INFO" ]] && fail prep "安装包里缺少 Frida 组件，且无法下载。请重新下载完整的安装包（.dmg）后重试。"
fi
case "$FRIDA_INFO" in
  *source=bundled*) step prep ok "使用内置 Frida（$(sed -E 's/.*version=([^ ]+).*/\1/' <<<"$FRIDA_INFO")）" ;;
  *) step prep ok "使用本机已有 Frida：$(cut -d' ' -f1-3 <<<"$FRIDA_INFO")" ;;
esac
step prep run "准备临时签名副本（不改正式版微信）"
capture_work="$(mktemp -d /private/tmp/wechat-db-keys.XXXXXX)"
echo "临时目录：${capture_work}"
ditto --noextattr --noqtn "${APP}" "${capture_work}/WeChat.app" || fail prep "复制微信失败"
codesign --force --deep --sign - --entitlements "${SKILL}/references/capture-entitlements.plist" "${capture_work}/WeChat.app" || fail prep "临时签名失败"
codesign --verify --deep --strict "${capture_work}/WeChat.app" || fail prep "签名校验失败"
mkdir -p "$CFG/key-capture"; chmod 700 "$CFG" "$CFG/key-capture"
step prep ok "临时副本与 Frida 就绪"

step capture run "挂监听并启动临时微信（最长 60 秒；出现扫码页请在手机上确认）"
CAP="$CFG/key-capture/candidates-$TS.jsonl"
"$PY" "$SKILL/scripts/keys.py" capture --app "$capture_work/WeChat.app" --seconds 60 --output "$CAP" 2>"$CFG/key-capture/capture-$TS.err"; crc=$?
chmod 600 "$CFG/key-capture/capture-$TS.err" 2>/dev/null
pids="$(ps -axo pid=,command= | grep -F "$capture_work/WeChat.app/" | grep -v grep | awk '{print $1}')"
[[ -n "$pids" ]] && kill -TERM $pids 2>/dev/null
count=0; [[ -f "$CAP" ]] && count="$(wc -l < "$CAP" | tr -d ' ')"
if [[ "${count}" -eq 0 ]]; then
  if grep -q '\[permission\]' "$CFG/key-capture/capture-$TS.err" 2>/dev/null; then
    fail capture "Frida 没有权限挂到临时微信上。若系统弹出「开发者工具」授权请点允许，然后重试。"
  elif [[ $crc -ne 0 ]]; then
    fail capture "抓取过程出错（$(grep -o 'Operation failed.*' "$CFG/key-capture/capture-$TS.err" | head -1 | cut -c1-160)）。"
  fi
  fail capture "没有捕获到候选密钥。请确认临时微信曾真正登录过（可点开几个聊天），然后重试。"
fi
step capture ok "捕获到 ${count} 条候选（内容未显示）"

step pair run "离线配对（HMAC 校验，只输出数量）"
NEW="$CFG/keys-new-$TS.json"
"$PY" "${SKILL}/scripts/keys.py" pair --capture "${CAP}" --db-root "${DB_ROOT}" --output "${NEW}" || fail pair "配对失败，原 keys.json 未改动。"
step pair ok "配对完成"

step merge run "合并到 keys.json（先备份）"
[[ -f "$CFG/keys.json" ]] && cp -p "$CFG/keys.json" "$CFG/keys.json.bak-$TS" && chmod 600 "$CFG/keys.json.bak-$TS"
"$PY" - "$CFG/keys.json" "$NEW" <<'PY'
import json, os, sys, tempfile
old_p, new_p = sys.argv[1], sys.argv[2]
old = json.load(open(old_p)) if os.path.isfile(old_p) else {}
new = json.load(open(new_p))
old.update(new)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(old_p)); os.fchmod(fd, 0o600)
with os.fdopen(fd, "w") as f: json.dump(old, f, indent=2); f.write("\n")
os.replace(tmp, old_p); os.chmod(old_p, 0o600)
print(f"合并 {len(new)} 个库，共 {len(old)} 个库。")
PY
step merge ok "密钥已写入（0600）"

step verify run "验证密钥并清理"
WECHAT_READER_CONFIG="$CFG" "$BIN" doctor >"$CFG/key-capture/doctor-$TS.txt" 2>&1; rc=$?
# restart reader if running
for lab in ${WD_READER_LABEL:-com.gly.wechatreader.serve} com.lampcat.wechatreader; do
  launchctl kickstart -k "gui/$UIDN/$lab" 2>/dev/null || true
done
open -a "$APP" 2>/dev/null || true
if [[ ${rc} == 0 ]]; then step verify ok "密钥验证通过"; printf '@@done\tok\n'; exit 0
else step verify warn "部分库未通过（见设置页「验证密钥」）；主要消息库可用即可"; printf '@@done\tok\n'; exit 0
fi
