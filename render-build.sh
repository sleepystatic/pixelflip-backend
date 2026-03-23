#!/bin/bash
set -e

echo "📦 Installing Python dependencies..."
pip install -r requirements.txt

echo "🌐 Installing Chromium..."
apt-get update
apt-get install -y chromium chromium-driver

echo "✅ Build complete!"