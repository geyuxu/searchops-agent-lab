#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
set -a
source "$PROJECT_DIR/.env"
set +a
"$PROJECT_DIR/.venv-data/bin/python" "$PROJECT_DIR/data/scripts/evaluate.py" \
  --limit "${EVALUATION_QUERY_LIMIT:-200}"

