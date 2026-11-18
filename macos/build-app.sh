#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
bundle="$stage/页畔.app"
app="$bundle/Contents"
mkdir -p "$app/MacOS" "$app/Resources"
clang -fobjc-arc -framework Cocoa macos/launcher.m -o "$app/MacOS/QmdMargin"
cp macos/Info.plist "$app/Info.plist"
cp macos/AppIcon.icns "$app/Resources/AppIcon.icns"
codesign --force --sign - "$bundle"
codesign --verify --strict "$bundle"
ditto --norsrc --noextattr "$bundle" "页畔.app"
printf '已生成 %s/页畔.app\n' "$PWD"
