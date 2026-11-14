#!/bin/bash
# Build 消息工作台.app (+ .dmg) from the command line. Needs only Xcode Command Line Tools (swiftc).
set -euo pipefail
cd "$(dirname "$0")"
APP_NAME="消息工作台"
BUNDLE_ID="com.gly.wechatdesk"
VERSION="1.0.0"
BUILD="build"; DIST="dist"
APP="$DIST/$APP_NAME.app"
rm -rf "$BUILD" "$APP" "$DIST/$APP_NAME.dmg"; mkdir -p "$BUILD" "$DIST"

echo "==> compile"
swiftc -O -target arm64-apple-macos13.0 -framework Cocoa -framework WebKit src/main.swift -o "$BUILD/WeChatDesk"

echo "==> icon"
swiftc -O src/makeicon.swift -o "$BUILD/makeicon"
"$BUILD/makeicon" "$BUILD/AppIcon.iconset"
iconutil -c icns "$BUILD/AppIcon.iconset" -o "$BUILD/AppIcon.icns"

echo "==> bundle"
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
plutil -lint "$APP/Contents/Info.plist" >/dev/null

echo "==> sign (ad-hoc)"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

echo "==> dmg"
STAGE="$BUILD/dmg"; mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"; ln -s /Applications "$STAGE/Applications"
hdiutil create -quiet -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO "$DIST/$APP_NAME.dmg"
echo "==> done: $APP and $DIST/$APP_NAME.dmg"
