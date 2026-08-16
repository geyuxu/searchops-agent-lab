#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
services=(postgres redis elasticsearch ai-adapter search-service commerce storefront operations-console)
deadline=$((SECONDS + 360))

while (( SECONDS < deadline )); do
  unhealthy=()
  for service in "${services[@]}"; do
    container="$(docker compose ps -q "$service")"
    if [[ -z "$container" ]]; then
      unhealthy+=("$service:missing")
      continue
    fi
    state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container")"
    if [[ "$state" != "healthy" && "$state" != "running" ]]; then
      unhealthy+=("$service:$state")
    fi
  done
  if (( ${#unhealthy[@]} == 0 )); then
    echo "All core services are healthy."
    exit 0
  fi
  printf 'Waiting for: %s\n' "${unhealthy[*]}"
  sleep 5
done

docker compose ps
echo "Services did not become healthy in time. Inspect with make logs." >&2
exit 1

