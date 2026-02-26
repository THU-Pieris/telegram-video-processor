#!/usr/bin/env bash
set -euo pipefail

# Downloads the latest static ffmpeg release build (amd64) into ./vendor/ffmpeg
# Source: johnvansickle.com static builds (Linux x86_64)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENDOR_DIR="$PROJECT_DIR/vendor/ffmpeg"
TMP_DIR="$PROJECT_DIR/vendor/.ffmpeg-tmp"
ARCHIVE="$TMP_DIR/ffmpeg-release-amd64-static.tar.xz"
URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"

mkdir -p "$TMP_DIR"
rm -rf "$VENDOR_DIR"

echo "Downloading latest ffmpeg static build..."
curl -fL "$URL" -o "$ARCHIVE"

echo "Extracting..."
tar -xJf "$ARCHIVE" -C "$TMP_DIR"

EXTRACTED_DIR="$(find "$TMP_DIR" -maxdepth 1 -mindepth 1 -type d -name 'ffmpeg-*' | head -n 1)"
if [[ -z "$EXTRACTED_DIR" ]]; then
  echo "Could not find extracted ffmpeg directory" >&2
  exit 1
fi

mkdir -p "$VENDOR_DIR"
cp "$EXTRACTED_DIR/ffmpeg" "$VENDOR_DIR/ffmpeg"
cp "$EXTRACTED_DIR/ffprobe" "$VENDOR_DIR/ffprobe"
chmod +x "$VENDOR_DIR/ffmpeg" "$VENDOR_DIR/ffprobe"

echo "Installed to: $VENDOR_DIR"
"$VENDOR_DIR/ffmpeg" -version | head -n 1

rm -rf "$TMP_DIR"
