#!/bin/bash
# Build 消息工作台.app (+ .dmg). Needs Xcode Command Line Tools (swiftc).
# Embeds wechat-realtime-reader + wechat-frontend + setup/ into Contents/Resources/payload
# so a downloaded .app is self-contained (zero-config on first launch).
set -euo pipefail
cd "$(dirname "$0")"
APP_NAME="消息工作台"
BUNDLE_ID="com.gly.wechatdesk"
VERSION="${WD_VERSION:-1.2.0}"
BUILD="build"; DIST="dist"
APP="$DIST/$APP_NAME.app"
REPO="$(cd .. && pwd)"
PAYLOAD_SRC="$REPO"
rm -rf "$BUILD" "$APP" "$DIST/$APP_NAME.dmg" "$DIST/WeChatDesk-v${VERSION}.dmg"
mkdir -p "$BUILD" "$DIST"

echo "==> compile"
swiftc -O -target arm64-apple-macos13.0 -framework Cocoa -framework WebKit src/main.swift -o "$BUILD/WeChatDesk"

echo "==> icon"
swiftc -O src/makeicon.swift -o "$BUILD/makeicon"
"$BUILD/makeicon" "$BUILD/AppIcon.iconset"
iconutil -c icns "$BUILD/AppIcon.iconset" -o "$BUILD/AppIcon.icns"

echo "==> bundle skeleton"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BUILD/WeChatDesk" "$APP/Contents/MacOS/WeChatDesk"
cp "$BUILD/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key><string>WeChatDesk</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$(date +%Y%m%d%H%M)</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSPrincipalClass</key><string>NSApplication</string>
  <key>NSHumanReadableCopyright</key><string>本地只读消息助手 · 不发送任何消息</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST

echo "==> embed payload (reader + frontend + setup)"
P="$APP/Contents/Resources/payload"
mkdir -p "$P"
echo "$VERSION" > "$P/VERSION"
# reader: binary + skills (for key capture) + go.mod (for module path awareness)
mkdir -p "$P/wechat-realtime-reader/bin" "$P/wechat-realtime-reader/skills"
cp "$PAYLOAD_SRC/wechat-realtime-reader/bin/wechat-reader" "$P/wechat-realtime-reader/bin/"
chmod +x "$P/wechat-realtime-reader/bin/wechat-reader"
rsync -a --delete "$PAYLOAD_SRC/wechat-realtime-reader/skills/" "$P/wechat-realtime-reader/skills/"
# frontend: python sources + scripts + requirements (venv is created on first launch)
mkdir -p "$P/wechat-frontend"
rsync -a --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' --exclude 'state' \
  "$PAYLOAD_SRC/wechat-frontend/" "$P/wechat-frontend/"
chmod +x "$P/wechat-frontend/scripts/"*.sh
# setup scripts
mkdir -p "$P/setup"
cp "$PAYLOAD_SRC/setup/"*.sh "$P/setup/"
chmod +x "$P/setup/"*.sh

echo "==> sign (ad-hoc)"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

echo "==> dmg"
STAGE="$BUILD/dmg"; mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"; ln -s /Applications "$STAGE/Applications"
hdiutil create -quiet -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO "$DIST/$APP_NAME.dmg"
# ASCII-named release artifact
cp "$DIST/$APP_NAME.dmg" "$DIST/WeChatDesk-v${VERSION}.dmg"
echo "==> done: $APP"
echo "    $DIST/$APP_NAME.dmg"
echo "    $DIST/WeChatDesk-v${VERSION}.dmg"
