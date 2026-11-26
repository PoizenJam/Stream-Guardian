#!/bin/bash
set -e

echo "========================================"
echo " Building Stream Guardian"
echo "========================================"
echo

echo "Installing dependencies..."
pip install -r requirements.txt pyinstaller

echo
echo "Building executable..."
pyinstaller build.spec --clean

echo
echo "========================================"
echo " Build complete!"
echo " Output: dist/StreamGuardian"
echo "========================================"
