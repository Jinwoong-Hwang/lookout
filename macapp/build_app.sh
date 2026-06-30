#!/bin/bash
# Lookout.app 빌드 (한 번만 실행하면 됨). 이후엔 더블클릭/Dock으로 실행.
set -euo pipefail
cd "$(dirname "$0")"
APP="Lookout.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "→ 아이콘 생성…"
swiftc -O gen_icon.swift -o gen_icon -framework Cocoa
./gen_icon
iconutil -c icns Lookout.iconset -o "$APP/Contents/Resources/Lookout.icns"
rm -rf gen_icon Lookout.iconset

echo "→ swiftc 컴파일…"
swiftc -O main.swift -o "$APP/Contents/MacOS/Lookout" -framework Cocoa -framework WebKit

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Lookout</string>
  <key>CFBundleDisplayName</key><string>Lookout</string>
  <key>CFBundleIdentifier</key><string>io.lookout.app</string>
  <key>CFBundleExecutable</key><string>Lookout</string>
  <key>CFBundleIconFile</key><string>Lookout</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>NSPrincipalClass</key><string>NSApplication</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST

echo "→ ad-hoc 코드서명…"
codesign --force --deep --sign - "$APP" 2>/dev/null || echo "  (codesign 생략 — 실행엔 지장 없음)"

# 기존 Hermes.app 잔재 제거
rm -rf "/Applications/Hermes.app" "$HOME/Applications/Hermes.app" 2>/dev/null || true

# 설치: /Applications 시도, 실패하면 ~/Applications
DEST="/Applications"
if cp -R "$APP" "$DEST/" 2>/dev/null; then
  echo "✅ 설치: $DEST/$APP"
else
  mkdir -p "$HOME/Applications"
  cp -R "$APP" "$HOME/Applications/"
  DEST="$HOME/Applications"
  echo "✅ 설치: $DEST/$APP (/Applications 권한 없어 사용자 폴더에 설치)"
fi
echo "→ 실행: open \"$DEST/$APP\"  (또는 Spotlight에서 'Lookout')"
