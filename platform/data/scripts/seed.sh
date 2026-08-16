#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="$PROJECT_DIR/.venv-data/bin/python"

"$PYTHON" "$PROJECT_DIR/data/scripts/validate.py"
"$PYTHON" "$PROJECT_DIR/data/scripts/seed_commerce.py"
curl --fail --silent --show-error \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: seed-search-index' \
  -d '{}' \
  http://localhost:8080/api/v1/index/rebuild
echo
"$PYTHON" "$PROJECT_DIR/data/scripts/simulate_traffic.py"

