#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
set -a
source "$PROJECT_DIR/.env"
set +a
PYTHON="$PROJECT_DIR/.venv-data/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "Data environment is missing. Run make bootstrap first." >&2
  exit 1
fi

"$PYTHON" "$PROJECT_DIR/data/scripts/download.py"
"$PYTHON" "$PROJECT_DIR/data/scripts/process.py" \
  --product-limit "${PRODUCT_LIMIT:-20000}" \
  --query-limit "${QUERY_LIMIT:-10000}" \
  --seed "${DATA_SAMPLE_SEED:-searchops-lab-v1}"
"$PYTHON" "$PROJECT_DIR/data/scripts/validate.py"

