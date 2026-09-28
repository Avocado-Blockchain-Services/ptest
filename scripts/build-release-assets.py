#!/usr/bin/env python3
"""Build every release asset get.sh downloads: one bundle per platform plus
its ``.sha256`` file.

    uv build --wheel --out-dir /tmp/dist
    scripts/build-release-assets.py --ptest-wheel /tmp/dist/ptest_ng-X-py3-none-any.whl \\
        --version X --out /tmp/assets

The pinned psutil wheel for each platform comes from PyPI and must match the
SHA-256 digest PyPI publishes for it.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

PSUTIL_VERSION = "7.2.2"
# get.sh asset suffix -> (psutil wheel platform part, manifest platform tag)
PLATFORMS = {
    "linux-x86_64": ("manylinux2010_x86_64.manylinux_2_12_x86_64.manylinux_2_28_x86_64",
                     "manylinux_2_28_x86_64"),
    "linux-aarch64": ("manylinux2014_aarch64.manylinux_2_17_aarch64.manylinux_2_28_aarch64",
                      "manylinux_2_28_aarch64"),
    "macos-arm64": ("macosx_11_0_arm64", "macosx_11_0_arm64"),
    "macos-x86_64": ("macosx_10_9_x86_64", "macosx_10_9_x86_64"),
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _psutil_files() -> dict[str, dict]:
    url = f"https://pypi.org/pypi/psutil/{PSUTIL_VERSION}/json"
    with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=30) as response:
        return {item["filename"]: item for item in json.loads(response.read())["urls"]}


def _bundle_builder():
    path = Path(__file__).resolve().parent / "build-release-bundle.py"
    spec = importlib.util.spec_from_file_location("build_release_bundle", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ptest-wheel", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    wheel = args.ptest_wheel.resolve()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    files = _psutil_files()
    builder = _bundle_builder()
    with tempfile.TemporaryDirectory(prefix="ptest-assets-") as temporary:
        for suffix, (wheel_platform, platform_tag) in PLATFORMS.items():
            name = f"psutil-{PSUTIL_VERSION}-cp36-abi3-{wheel_platform}.whl"
            entry = files.get(name)
            if entry is None:
                raise SystemExit(f"PyPI has no {name}")
            with urlopen(Request(entry["url"]), timeout=60) as response:
                payload = response.read()
            if _sha256(payload) != entry["digests"]["sha256"]:
                raise SystemExit(f"{name} does not match PyPI's SHA-256")
            psutil = Path(temporary) / name
            psutil.write_bytes(payload)
            asset = out / f"ptest-{args.version}-{suffix}.tar.gz"
            builder.main(["--ptest-wheel", str(wheel), "--psutil-wheel", str(psutil),
                          "--version", args.version, "--python-tag", "cp36",
                          "--platform-tag", platform_tag, "--output", str(asset)])
            digest = _sha256(asset.read_bytes())
            (out / f"{asset.name}.sha256").write_text(f"{digest}  {asset.name}\n",
                                                      encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
