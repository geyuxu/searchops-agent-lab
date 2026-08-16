#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
"$PROJECT_DIR/.venv-data/bin/python" "$PROJECT_DIR/tests/integration_policy.py"
"$PROJECT_DIR/services/ai-adapter/.venv/bin/pytest" -q "$PROJECT_DIR/services/ai-adapter/tests/test_contract.py"

