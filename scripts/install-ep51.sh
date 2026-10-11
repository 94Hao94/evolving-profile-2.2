#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODE="local"
DRY_RUN=0
SKIP_DEPS=0
NO_LAUNCH=0

usage() {
  cat <<'EOF'
Usage: ./scripts/install-ep51.sh [--mode local] [--dry-run] [--skip-deps] [--no-launch]

Preflights the sanitized EP 5.1 package, creates a local .env template, installs
dependencies when requested, builds the Console, and writes a local launch
manifest. It never imports production memory, enables JEV/cloud/RAG, or starts a
service unless a future explicit launcher is added.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="${2:?missing mode}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --skip-deps) SKIP_DEPS=1; shift ;;
    --no-launch) NO_LAUNCH=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$MODE" == "local" ]] || { echo "Only --mode local is supported in EP 5.1." >&2; exit 2; }

run() {
  printf '+ %q' "$@"; printf '\n'
  (( DRY_RUN )) || "$@"
}

echo "EP 5.1 local deployment preflight: $ROOT"
run python3 "$ROOT/scripts/release-preflight.py" --root "$ROOT" --json
run "$ROOT/scripts/verify-package.sh"

if [[ ! -e "$ROOT/.env" ]]; then
  run cp "$ROOT/.env.example" "$ROOT/.env"
  (( DRY_RUN )) || chmod 600 "$ROOT/.env"
  echo "Created .env from .env.example; fill provider/database values before runtime use."
fi

if (( ! SKIP_DEPS )); then
  if command -v uv >/dev/null 2>&1 && [[ -f "$ROOT/api/pyproject.toml" ]]; then
    run uv sync --project "$ROOT/api"
  else
    echo "uv unavailable; skipped Python dependency install (use --skip-deps after provisioning)."
  fi
  if command -v npm >/dev/null 2>&1; then
    run npm ci --prefix "$ROOT"
  else
    echo "npm unavailable; cannot build the Console." >&2
    exit 1
  fi
fi

# Next.js can accidentally type-check a stale standalone tree as source. Keep
# the previous build recoverable but out of the source tree before rebuilding.
BUILD_QUARANTINE="$(mktemp -d "${TMPDIR:-/tmp}/ep51-build.XXXXXX")"
if [[ -e "$ROOT/console/standalone" ]]; then
  run mv "$ROOT/console/standalone" "$BUILD_QUARANTINE/standalone"
fi
if [[ -e "$ROOT/console/.next" ]]; then
  run mv "$ROOT/console/.next" "$BUILD_QUARANTINE/.next"
fi

run npm run build --prefix "$ROOT/console"

if (( ! DRY_RUN )); then
  mkdir -p "$ROOT/output"
  python3 - "$ROOT/output/ep51-local-launch.json" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
path.write_text(json.dumps({
    "product": "Evolving Profile",
    "version": "5.1.0",
    "mode": "local",
    "console_build": "verified",
    "provider_configured": False,
    "external_rag_enabled": False,
    "jev_enabled": False,
    "cloud_backup_enabled": False,
    "launch": "manual",
    "boundary": "Sanitized package only; no production Bank, credentials, or receipts imported."
}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
fi

if (( NO_LAUNCH )); then
  echo "EP 5.1 package built and verified; service launch skipped (--no-launch)."
else
  echo "EP 5.1 package built and verified. Runtime launch remains explicit: configure .env and start API/Console using api/README.md."
fi
