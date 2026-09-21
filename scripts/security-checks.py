#!/usr/bin/env python3
"""Run local security gates; an unavailable or insensitive detector is failure."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

TOOLS = ("bandit", "pip-audit", "gitleaks")


def run_gate(root: Path, tool: str) -> dict:
    executable = shutil.which(tool)
    if not executable:
        return {"tool": tool, "status": "unpassed", "reason": "unavailable"}
    with tempfile.TemporaryDirectory(prefix="ptest-security-") as temp:
        fixture = Path(temp) / "sensitivity.py"
        fixture.write_text("password = 'not-a-real-secret-but-detector-fixture'\n")
        if tool == "bandit":
            probe = [executable, "-q", "-r", str(fixture)]
            scope = [executable, "-q", "-r", str(root / "src"), "-lll", "-iii"]
        elif tool == "pip-audit":
            requirements = Path(temp) / "requirements.txt"
            requirements.write_text("jinja2==2.10\n")
            probe = [executable, "--requirement", str(requirements)]
            locked = Path(temp) / "locked.txt"
            exported = subprocess.run(["uv", "export", "--locked", "--no-emit-project", "--format", "requirements-txt", "--output-file", str(locked)], cwd=root, capture_output=True, text=True)
            if exported.returncode != 0:
                return {"tool": tool, "status": "unpassed", "reason": "lock-export-failed"}
            scope = [executable, "--requirement", str(locked)]
        else:
            probe = [executable, "detect", "--source", temp, "--no-banner"]
            scope = [executable, "detect", "--source", str(root), "--no-banner"]
        sensitivity = subprocess.run(probe, cwd=root, capture_output=True, text=True)
        if sensitivity.returncode == 0:
            return {"tool": tool, "status": "unpassed", "reason": "sensitivity-failed"}
        result = subprocess.run(scope, cwd=root, capture_output=True, text=True)
        return {"tool": tool, "status": "passed" if result.returncode == 0 else "unpassed", "returncode": result.returncode}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    results = [run_gate(args.root, tool) for tool in TOOLS]
    print(json.dumps({"version": 1, "gates": results}, sort_keys=True))
    return 0 if all(item["status"] == "passed" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
