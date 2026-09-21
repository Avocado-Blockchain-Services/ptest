#!/usr/bin/env python3
"""Build the self-contained archive consumed by the public install.sh wrapper."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tarfile
import tempfile
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ptest-wheel", required=True, type=Path)
    parser.add_argument("--psutil-wheel", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--python-tag", required=True)
    parser.add_argument("--platform-tag", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    for wheel in (args.ptest_wheel, args.psutil_wheel):
        if not wheel.is_absolute() or wheel.is_symlink() or not wheel.is_file():
            raise SystemExit(f"release wheel must be an absolute regular file: {wheel}")
    output = args.output.resolve()
    if output.suffixes[-2:] != [".tar", ".gz"]:
        raise SystemExit("--output must end in .tar.gz")
    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="ptest-release-") as temporary:
        root = Path(temporary) / f"ptest-{args.version}"
        wheelhouse = root / "wheelhouse"
        scripts = root / "scripts"
        wheelhouse.mkdir(parents=True)
        scripts.mkdir()
        for artifact in ("install.sh", "README.md"):
            shutil.copy2(source / artifact, root / artifact)
        shutil.copy2(source / "scripts" / "install.py", scripts / "install.py")
        ptest = wheelhouse / args.ptest_wheel.name
        psutil = wheelhouse / args.psutil_wheel.name
        shutil.copy2(args.ptest_wheel, ptest)
        shutil.copy2(args.psutil_wheel, psutil)
        manifest = {
            "version": 1, "ptest_version": args.version,
            "python_tag": args.python_tag, "platform_tag": args.platform_tag,
            "wheels": [
                {"filename": ptest.name, "sha256": _sha256(ptest),
                 "package": "ptest-ng", "version": args.version},
                {"filename": psutil.name, "sha256": _sha256(psutil),
                 "package": "psutil", "version": "7.2.2"},
            ],
        }
        (wheelhouse / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output, "w:gz") as archive:
            archive.add(root, arcname=root.name, recursive=True)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
