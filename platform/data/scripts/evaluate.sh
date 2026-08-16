#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
set -a
source "$PROJECT_DIR/.env"
set +a
# 追加的参数原样透传给 evaluate.py（argparse 取最后一次出现的值，所以调用方也能
# 用 --limit 覆盖上面的 EVALUATION_QUERY_LIMIT）。make evaluate-ai 靠的就是这条路径。
# AI_TIMEOUT_MS / AI_CONNECT_TIMEOUT_MS 已随 .env 导出，evaluate.py 用它们推算客户端超时。
"$PROJECT_DIR/.venv-data/bin/python" "$PROJECT_DIR/data/scripts/evaluate.py" \
  --limit "${EVALUATION_QUERY_LIMIT:-200}" "$@"
