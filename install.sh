#!/bin/bash
# 消息助手 · 一键安装(macOS / Apple Silicon)
# 只做可自动化的部分:建 venv、装依赖、生成本机配置与令牌、编译 App、装前端后台服务。
# 一次性的「抓微信数据库密钥」与系统「完全磁盘访问权限」需你手动完成(见末尾提示)。
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
READER="$REPO/wechat-realtime-reader"
FRONTEND="$REPO/wechat-frontend"
CFG_FE="$HOME/.config/wechat-frontend"
CFG_RD="$HOME/.config/wechat-realtime-reader"

echo "==> 环境检查"
[[ "$(uname)" == "Darwin" ]] || { echo "只支持 macOS"; exit 1; }
command -v swiftc >/dev/null || { echo "缺 swiftc,请先装 Xcode Command Line Tools: xcode-select --install"; exit 1; }
command -v python3 >/dev/null || { echo "缺 python3"; exit 1; }

echo "==> 前端:创建 venv + 安装依赖"
python3 -m venv "$FRONTEND/.venv"
"$FRONTEND/.venv/bin/pip" install -q -U pip
"$FRONTEND/.venv/bin/pip" install -q -r "$FRONTEND/requirements.txt"

echo "==> 生成本机配置(0600,已存在则保留)"
mkdir -p "$CFG_FE" "$CFG_RD"; chmod 700 "$CFG_FE" "$CFG_RD"
[[ -f "$CFG_FE/config.json" ]] || { cp "$REPO/config/config.json.template" "$CFG_FE/config.json"; chmod 600 "$CFG_FE/config.json"; }
if [[ ! -f "$CFG_FE/gateway-token" ]]; then
  LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40 > "$CFG_FE/gateway-token"
  chmod 600 "$CFG_FE/gateway-token"
  echo "   已生成新的网关令牌(内容保密,见 $CFG_FE/gateway-token)"
fi
if [[ ! -f "$CFG_RD/source-config.json" ]]; then
  cp "$REPO/config/source-config.json.template" "$CFG_RD/source-config.json"; chmod 600 "$CFG_RD/source-config.json"
  echo "   ⚠️ 已放置数据源模板,请把其中 wxid 与 db_base_path 改成你的真实值(见 README「一次性:抓密钥」)"
fi

echo "==> 编译 App"
bash "$REPO/app/build.sh"

echo "==> 让 App 指向本仓库目录"
defaults write com.gly.wechatdesk PlatformRoot "$REPO" || true

echo "==> 安装前端后台服务(launchd)"
mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s#__FRONTEND__#$FRONTEND#g" -e "s#__HOME__#$HOME#g" \
  "$FRONTEND/launchd/com.gly.wechatfrontend.plist.template" > "$HOME/Library/LaunchAgents/com.gly.wechatfrontend.plist"
launchctl bootout "gui/$(id -u)/com.gly.wechatfrontend" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.gly.wechatfrontend.plist" || true

echo
echo "==================== 安装完成(自动部分) ===================="
echo "App:        $REPO/app/dist/消息工作台.app"
echo "前端网关:   http://127.0.0.1:8088  (由 launchd 保活)"
echo
echo "还需你手动完成(一次性):"
echo "  1) 抓微信数据库密钥:登录并保持微信在线,运行 $FRONTEND/scripts/recapture-keys.sh"
echo "     (详见 $READER/README.md;密钥会写入 $CFG_RD,权限 0600)"
echo "  2) 给读取器进程开『完全磁盘访问权限』:系统设置 → 隐私与安全性 → 完全磁盘访问权限"
echo "  3) 双击打开 App;首次用 AI 请在 App 设置里填 Base URL / API Key / 选模型并启用"
echo "============================================================"
