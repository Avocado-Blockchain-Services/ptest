#!/usr/bin/env bash
set -euo pipefail

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
has_wheelhouse=false
has_manifest=false
for argument in "$@"; do
    [ "$argument" = "--wheelhouse" ] && has_wheelhouse=true
    [ "$argument" = "--manifest" ] && has_manifest=true
done

if [ "$has_wheelhouse" = true ] || [ "$has_manifest" = true ]; then
    if [ "$has_wheelhouse" != true ] || [ "$has_manifest" != true ]; then
        echo "install.sh: --wheelhouse and --manifest must be supplied together" >&2
        exit 2
    fi
    exec python3 "$source_dir/scripts/install.py" "$@"
fi

wheelhouse="$source_dir/wheelhouse"
manifest="$wheelhouse/manifest.json"
if [ ! -d "$wheelhouse" ] || [ ! -f "$manifest" ]; then
    echo "install.sh: this source checkout has no release wheelhouse." >&2
    echo "Download a verified ptest release archive, or pass --wheelhouse and --manifest explicitly." >&2
    exit 2
fi

destination="${HOME}/.local/ptest"
forwarded=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --dest)
            [ "$#" -ge 2 ] || { echo "install.sh: --dest requires a value" >&2; exit 2; }
            destination="$2"
            shift 2
            ;;
        *)
            forwarded+=("$1")
            shift
            ;;
    esac
done
python3 "$source_dir/scripts/install.py" --dest "$destination" \
    --wheelhouse "$wheelhouse" --manifest "$manifest" "${forwarded[@]}"

# The default installation is a user command, so publish its PATH entry only
# after the immutable bundle itself has installed successfully.
if [ "$destination" = "${HOME}/.local/ptest" ]; then
    bin_dir="${HOME}/.local/bin"
    launcher="$bin_dir/ptest"
    mkdir -p "$bin_dir"
    if [ -e "$launcher" ] && [ ! -L "$launcher" ]; then
        echo "install.sh: existing launcher is not a symlink: $launcher" >&2
        exit 1
    fi
    temporary_launcher="$bin_dir/.ptest-link-$$"
    ln -s ../ptest/ptest "$temporary_launcher"
    mv -f "$temporary_launcher" "$launcher"
fi
