#!/usr/bin/env bash
# Запуск КТК ЭЛОУ-АВТ (dev)
set -e
cd "$(dirname "$0")"
python3 -m venv .venv 2>/dev/null || true
source .venv/bin/activate
pip install -q -r requirements.txt
echo "Открой http://localhost:8000"
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
