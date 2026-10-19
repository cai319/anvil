#!/usr/bin/env bash
# macOS/Linux 打包脚本：装依赖 → build 前端 → PyInstaller 单目录分发。
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -r backend/requirements.txt pyinstaller

(cd frontend && npm install && npm run build)

pyinstaller --clean --noconfirm packaging/anvil.spec
echo "产物在 dist/anvil-dist/（anvil + anvil-server）"
