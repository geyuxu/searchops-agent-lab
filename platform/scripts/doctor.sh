#!/usr/bin/env bash
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
failures=0

check() {
  local label="$1"
  shift
  if output="$("$@" 2>&1)"; then
    printf '✓ %-18s %s\n' "$label" "$(printf '%s' "$output" | head -n 1)"
  else
    printf '✗ %-18s %s\n' "$label" "$(printf '%s' "$output" | head -n 1)"
    failures=$((failures + 1))
  fi
}

echo "Commerce SearchOps Lab doctor"
echo "Workspace: $PROJECT_DIR"
check "Docker" docker --version
check "Docker Compose" docker compose version
check "Docker daemon" docker info --format 'server {{.ServerVersion}} · {{.NCPU}} CPUs · {{.MemTotal}} bytes'
check "Node (host)" node --version
check "npm" npm --version
check "Python" python3 --version
check "uv" uv --version
check "Maven" mvn --version
if [[ -x /usr/libexec/java_home ]] && JAVA_21_HOME="$(/usr/libexec/java_home -v 21 2>/dev/null)"; then
  check "Java 21" "$JAVA_21_HOME/bin/java" -version
else
  echo "✗ Java 21            not found"
  failures=$((failures + 1))
fi
if [[ -f "$PROJECT_DIR/.env" ]]; then
  echo "✓ .env               present (local-only defaults)"
else
  echo "! .env               missing; make bootstrap will copy .env.example"
fi
echo "Note: Node applications build in pinned Node 24 containers; a different host Node version is acceptable."
exit "$failures"
