#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -f "$PROJECT_DIR/.commerce-searchops-lab-root" ]]; then
  echo "Refusing to clean: project marker is missing." >&2
  exit 1
fi
cd "$PROJECT_DIR"
docker compose down --volumes --remove-orphans
find "$PROJECT_DIR/data/raw" -type f ! -name '.gitkeep' -delete
find "$PROJECT_DIR/data/processed" -type f ! -name '.gitkeep' -delete
if [[ -d "$PROJECT_DIR/.runtime" ]]; then
  rm -rf "$PROJECT_DIR/.runtime"
fi
echo "Removed only this project's containers, named volumes, raw/processed data and .runtime directory."

