#!/usr/bin/env bash
set -euo pipefail

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
destination=${1:-"${HOME}/.local/bin"}

mkdir -p -- "$destination"
bundle_root="${destination}/.ptest-bundles"
mkdir -p -- "$bundle_root"
bundle=$(mktemp -d "${bundle_root}/bundle.XXXXXX")

install -m 0644 -- "${source_dir}/spot_queue.py" "${bundle}/spot_queue.py"
install -m 0644 -- "${source_dir}/spot_controller.py" "${bundle}/spot_controller.py"
install -m 0755 -- "${source_dir}/ptest" "${bundle}/ptest"

# Build the complete pair out of sight, then atomically switch the one public
# executable pointer. An interrupted install leaves the previous bundle live.
link="${bundle_root}/.ptest-link.$(basename -- "$bundle")"
ln -s -- ".ptest-bundles/$(basename -- "$bundle")/ptest" "$link"
mv -Tf -- "$link" "${destination}/ptest"

printf 'installed ptest and spot_queue.py in %s\n' "$destination"
