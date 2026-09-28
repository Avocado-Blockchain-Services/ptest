#!/bin/sh
# ptest one-line installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/Avocado-Blockchain-Services/ptest/main/get.sh | sh
#
# Downloads the release bundle for this machine, verifies its SHA-256, and
# runs the bundled installer (which installs from the bundled wheels only).
# Environment:
#   PTEST_VERSION   install this version (e.g. 0.3.3) instead of the latest
#   PTEST_BASE_URL  download base (default: the GitHub release of that version)
set -eu

repo="Avocado-Blockchain-Services/ptest"

say() { printf 'ptest-install: %s\n' "$*" >&2; }
die() { say "$*"; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$2"; }

need curl "curl is required"
need tar "tar is required"
need uv "uv is required: curl -LsSf https://astral.sh/uv/install.sh | sh  (then open a new shell and rerun)"

case "$(uname -s)" in
    Linux) os=linux ;;
    Darwin) os=macos ;;
    *) die "unsupported OS $(uname -s): ptest supports macOS and Linux" ;;
esac
case "$(uname -m)" in
    x86_64|amd64) arch=x86_64 ;;
    arm64|aarch64) arch=$([ "$os" = macos ] && echo arm64 || echo aarch64) ;;
    *) die "unsupported CPU $(uname -m): ptest supports x86_64 and arm64" ;;
esac

version="${PTEST_VERSION:-}"
if [ -z "$version" ]; then
    latest=$(curl -fsSLI -o /dev/null -w '%{url_effective}' \
        "https://github.com/$repo/releases/latest") || die "could not reach GitHub"
    version="${latest##*/v}"
    case "$version" in
        [0-9]*.[0-9]*.[0-9]*) ;;
        *) die "could not resolve the latest release (got $latest)" ;;
    esac
fi
version="${version#v}"
base="${PTEST_BASE_URL:-https://github.com/$repo/releases/download/v$version}"
asset="ptest-$version-$os-$arch.tar.gz"

# ptest needs CPython 3.11-3.14; the macOS system python3 is older, so let
# uv find (or fetch) a suitable interpreter.
python=$(uv python find --system '>=3.11,<3.15' 2>/dev/null || true)
if [ -z "$python" ]; then
    say "no CPython 3.11-3.14 found; installing one with uv"
    uv python install 3.13 >&2 || die "uv python install 3.13 failed"
    python=$(uv python find --system '>=3.11,<3.15') || die "no usable Python after install"
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/ptest-install.XXXXXX")
trap 'rm -rf "$work"' EXIT INT TERM

say "downloading ptest $version for $os-$arch"
curl -fsSL -o "$work/$asset" "$base/$asset" || die "download failed: $base/$asset"
curl -fsSL -o "$work/$asset.sha256" "$base/$asset.sha256" || die "download failed: $base/$asset.sha256"

expected=$(awk '{print $1; exit}' "$work/$asset.sha256")
if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum "$work/$asset" | awk '{print $1}')
else
    actual=$(shasum -a 256 "$work/$asset" | awk '{print $1}')
fi
[ -n "$expected" ] && [ "$expected" = "$actual" ] || die "checksum mismatch for $asset"

tar -xzf "$work/$asset" -C "$work"
PTEST_PYTHON="$python" "$work/ptest-$version/install.sh"

case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) say "add ~/.local/bin to PATH:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
say "installed: $("$HOME/.local/bin/ptest" --version 2>/dev/null || echo "$HOME/.local/bin/ptest")"
say "next: cd your-repo && ptest init"
