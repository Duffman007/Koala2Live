#!/bin/bash
set -e

APP_NAME="Koala2Live"
APP_BUNDLE="${APP_NAME}.app"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "🔨 Building ${APP_NAME}..."

if ! command -v swiftc &>/dev/null; then
    echo "❌ Xcode Command Line Tools required."
    echo "   Run: xcode-select --install"
    exit 1
fi

# Remove old build and flush launch cache
rm -rf "$SCRIPT_DIR/${APP_BUNDLE}"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -u "$SCRIPT_DIR/${APP_BUNDLE}" 2>/dev/null || true

mkdir -p "$SCRIPT_DIR/${APP_BUNDLE}/Contents/MacOS"
mkdir -p "$SCRIPT_DIR/${APP_BUNDLE}/Contents/Resources"

echo "   Compiling Swift (arm64)..."
swiftc -O -target arm64-apple-macosx11.0 \
    -o "$SCRIPT_DIR/${APP_BUNDLE}/Contents/MacOS/${APP_NAME}_arm64" \
    "$SCRIPT_DIR/Koala2Live.swift"

echo "   Compiling Swift (x86_64)..."
swiftc -O -target x86_64-apple-macosx11.0 \
    -o "$SCRIPT_DIR/${APP_BUNDLE}/Contents/MacOS/${APP_NAME}_x86" \
    "$SCRIPT_DIR/Koala2Live.swift"

echo "   Creating universal binary..."
lipo -create \
    "$SCRIPT_DIR/${APP_BUNDLE}/Contents/MacOS/${APP_NAME}_arm64" \
    "$SCRIPT_DIR/${APP_BUNDLE}/Contents/MacOS/${APP_NAME}_x86" \
    -output "$SCRIPT_DIR/${APP_BUNDLE}/Contents/MacOS/${APP_NAME}"
rm "$SCRIPT_DIR/${APP_BUNDLE}/Contents/MacOS/${APP_NAME}_arm64" \
   "$SCRIPT_DIR/${APP_BUNDLE}/Contents/MacOS/${APP_NAME}_x86"

echo "   Copying resources..."
cp "$SCRIPT_DIR/Info.plist"       "$SCRIPT_DIR/${APP_BUNDLE}/Contents/"
cp "$SCRIPT_DIR/KoalaALS.py"      "$SCRIPT_DIR/${APP_BUNDLE}/Contents/Resources/"
cp "$SCRIPT_DIR/background.png"   "$SCRIPT_DIR/${APP_BUNDLE}/Contents/Resources/"
cp "$SCRIPT_DIR/AppIcon.icns"     "$SCRIPT_DIR/${APP_BUNDLE}/Contents/Resources/"
cp "$SCRIPT_DIR/version.txt"      "$SCRIPT_DIR/${APP_BUNDLE}/Contents/Resources/"
cp "$SCRIPT_DIR/changelog.txt"    "$SCRIPT_DIR/${APP_BUNDLE}/Contents/Resources/"

echo "✅ Done! ${APP_BUNDLE} is ready."
echo "   Double-click to run, or move to /Applications."
