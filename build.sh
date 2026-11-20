#!/usr/bin/env bash
# ==============================================================================
# VidGrab Extension Clean Non-Destructive Packaging Script
# ==============================================================================
set -euo pipefail

PROJECT_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
DEFAULT_OUT="$PROJECT_ROOT/packaging/out/extension-$(date -u +%Y%m%dT%H%M%SZ)-$$"
OUT_DIR="${1:-$DEFAULT_OUT}"

if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
else
    echo "Error: python3/python is required to run packaging/package_extension.py" >&2
    exit 1
fi

echo "Packaging Chrome and Firefox extensions cleanly into: $OUT_DIR"
echo "(Tracked dist/ files are never deleted or overwritten by build.sh.)"
exec "$PYTHON_BIN" -B "$PROJECT_ROOT/packaging/package_extension.py" --output "$OUT_DIR" "${@:2}"
