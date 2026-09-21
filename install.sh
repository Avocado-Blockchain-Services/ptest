#!/usr/bin/env bash
set -euo pipefail

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$source_dir/scripts/install.py" "$@"
