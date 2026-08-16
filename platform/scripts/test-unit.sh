#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$PROJECT_DIR/.runtime/config"
export XDG_CONFIG_HOME="$PROJECT_DIR/.runtime/config"
if [[ "$(uname -s)" == "Darwin" ]]; then
  JAVA_HOME="$(/usr/libexec/java_home -v 21)"
  export JAVA_HOME PATH="$JAVA_HOME/bin:$PATH"
fi

(cd "$PROJECT_DIR/services/ai-adapter" && .venv/bin/ruff check app tests && .venv/bin/pytest)
(cd "$PROJECT_DIR/data" && ../.venv-data/bin/ruff check scripts tests && ../.venv-data/bin/pytest -q tests)
(cd "$PROJECT_DIR/services/search-service" && mvn test)
(cd "$PROJECT_DIR/services/commerce" && npm test && npm run build)
(cd "$PROJECT_DIR/packages/api-contracts/typescript" && npm run typecheck)
(cd "$PROJECT_DIR/packages/api-contracts/java" && mvn -q test)
python3 -m json.tool "$PROJECT_DIR/packages/api-contracts/openapi/ai-adapter.openapi.json" >/dev/null
python3 -m json.tool "$PROJECT_DIR/packages/api-contracts/openapi/searchops-tools.openapi.json" >/dev/null
python3 -m json.tool "$PROJECT_DIR/packages/api-contracts/json-schema/ai-contracts.schema.json" >/dev/null
(cd "$PROJECT_DIR/apps/storefront" && npm run typecheck && npm run build)
(cd "$PROJECT_DIR/apps/operations-console" && npm run typecheck && npm run build)
