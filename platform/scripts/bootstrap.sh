#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if [[ ! -f .env ]]; then
  cp .env.example .env
fi

python3 -m venv .venv-data
.venv-data/bin/python -m pip install --upgrade pip
.venv-data/bin/python -m pip install -r data/requirements-dev.txt

python3 -m venv services/ai-adapter/.venv
services/ai-adapter/.venv/bin/python -m pip install --upgrade pip
services/ai-adapter/.venv/bin/python -m pip install -r services/ai-adapter/requirements-dev.txt

(cd services/commerce && npm ci)
(cd apps/storefront && npm ci)
(cd apps/operations-console && npm ci)
(cd tests/e2e && npm ci)

if [[ "$(uname -s)" == "Darwin" ]]; then
  JAVA_HOME="$(/usr/libexec/java_home -v 21)"
  export JAVA_HOME PATH="$JAVA_HOME/bin:$PATH"
fi
(cd services/search-service && mvn -q -DskipTests dependency:go-offline)

echo "Bootstrap complete. Next: make data && make up && make seed"

