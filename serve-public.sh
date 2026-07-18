#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "Serving public/ with serve.py on port 8765"
exec python3 "$SCRIPT_DIR/serve.py" --host 0.0.0.0 --port 8765
