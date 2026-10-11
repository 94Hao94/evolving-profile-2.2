#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
fail=0
if grep -RInE --exclude='check-no-secrets.sh' --exclude='*.lock' --exclude='*.map' \
  --exclude-dir='node_modules' --exclude-dir='.next' --exclude-dir='standalone' --exclude-dir='output' --exclude-dir='.pytest_cache' --exclude-dir='__pycache__' \
  'sk-[A-Za-z0-9]{12,}|gsk_[A-Za-z0-9]{12,}|AKIA[0-9A-Z]{16}|BEGIN (RSA|OPENSSH|EC) PRIVATE KEY|刘中洋|liuzhongyang-memory-core' "$ROOT"; then
  fail=1
fi
if rg -n --hidden --glob '!.git/**' --glob '!*.lock' --glob '!*.map' \
  'personal-memory' "$ROOT" >/dev/null; then
  :
else
  echo "warning: no default personal-memory placeholder found" >&2
fi
if [ "$fail" -ne 0 ]; then
  echo "secret or personal-data scan failed" >&2
  exit 1
fi
echo "secret and personal-data scan passed"
