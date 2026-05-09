#!/usr/bin/env bash
set -euo pipefail

BUILD_DIR="lambda/query/build"
ZIP_OUT="lambda/query.zip"

echo "Cleaning previous build..."
rm -rf "$BUILD_DIR" "$ZIP_OUT"
mkdir -p "$BUILD_DIR"

echo "Installing dependencies..."
pip install \
  --requirement lambda/query/requirements.txt \
  --target "$BUILD_DIR" \
  --platform mlinux_2_x86_64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  --upgrade \
  --quiet

echo "Copying handler..."
cp lambda/query/query.py "$BUILD_DIR/query.py"

echo "Zipping..."
cd "$BUILD_DIR" && zip -r "../../query.zip" . -x "*.pyc" -x "*/__pycache__/*"
cd -

echo "Done — $(du -sh $ZIP_OUT | cut -f1) written to $ZIP_OUT"
