#!/bin/bash
set -e

echo "========================================"
echo " Building Bitrate Guardian"
echo "========================================"
echo

echo "Installing dependencies..."
pip install -r requirements.txt

echo
echo "Building executable..."
pyinstaller build.spec --clean

echo
echo "========================================"
echo " Build complete!"
echo " Output: dist/BitrateGuardian"
echo "========================================"
