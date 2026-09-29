#!/usr/bin/env python3
"""Before/after offline doctor timing on a generated ~1000-file fixture.

T1 benchmark (not a test: never collected by the suite). It builds a
deterministic two-child monorepo (pytest- and vitest-shaped, fixed
``.ptest.toml`` files with fixed ``project_id`` values) in a temporary
directory, then times ``ptest doctor --offline`` and ``--offline --json``
in-process through ``ptest.cli.main`` from this checkout (never the
installed CLI). It prints one JSON line per mode::

    {"mode": "offline", "files": 1054, "seconds": 1.23}
    {"mode": "offline-json", "files": 1054, "seconds": 1.11}

Run from the checkout under test, e.g.::

    uv run python scripts/bench_doctor_offline.py

It also runs on the base commit: only ``main()`` and the stdlib are used,
so no T1 symbol is imported.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

API_PID = "aa" * 16
WEB_PID = "bb" * 16
PER_CHILD_SOURCES = 300
PER_CHILD_TESTS = 200


def _write(path: Path, text: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return 1


def _pytest_toml(*, kind: str, project_id: str) -> str:
    return (
        "version = 1\n"
        f"project_id = {json.dumps(project_id)}\n"
        "\n"
        "[runner]\n"
        f"kind = {json.dumps(kind)}\n"
        'launcher = ["echo"]\n'
        'args = ["hello"]\n'
        "full_args = []\n"
        'test_roots = ["tests"]\n'
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n'
    )


def build_fixture(root: Path) -> int:
    """Generate the deterministic fixture; return the file count."""
    count = _write(
        root / ".ptest.toml",
        'version = 2\n[monorepo]\nchildren = ["api", "web"]\n')
    api = root / "api"
    count += _write(api / ".ptest.toml",
                    _pytest_toml(kind="pytest", project_id=API_PID))
    count += _write(api / "tests" / "__init__.py", "")
    count += _write(api / "src" / "__init__.py", "")
    for index in range(PER_CHILD_SOURCES):
        count += _write(api / "src" / f"mod_{index:03d}.py",
                        f"VALUE_{index} = {index}\n")
    for index in range(PER_CHILD_TESTS):
        count += _write(
            api / "tests" / f"test_mod_{index:03d}.py",
            f"from src.mod_{index:03d} import VALUE_{index}\n"
            "\n"
            f"def test_value_{index:03d}():\n"
            f"    assert VALUE_{index} == {index}\n")
    web = root / "web"
    count += _write(web / ".ptest.toml",
                    _pytest_toml(kind="vitest", project_id=WEB_PID))
    count += _write(web / "package.json", json.dumps(
        {"name": "bench-web", "version": "1.0.0",
         "devDependencies": {"vitest": "1.0.0"}}) + "\n")
    count += _write(web / "vitest.config.ts",
                    "export default {};\n")
    for index in range(PER_CHILD_SOURCES):
        count += _write(web / "src" / f"mod_{index:03d}.ts",
                        f"export const value{index} = {index};\n")
    for index in range(PER_CHILD_TESTS):
        count += _write(
            web / "tests" / f"mod_{index:03d}.test.ts",
            f"import {{ value{index} }} from '../src/mod_{index:03d}';\n"
            "\n"
            f"test('value {index}', () => {{ expect(value{index}).toBe({index}); }});\n")
    return count


def time_mode(mode: str, extra: tuple[str, ...]) -> float:
    from ptest.cli import main

    raw = io.BytesIO()
    text = io.TextIOWrapper(raw, encoding="utf-8")
    started = time.perf_counter()
    with contextlib.redirect_stdout(text), contextlib.redirect_stderr(
            io.StringIO()):
        code = main(("doctor", "--offline") + extra)
    text.flush()
    elapsed = time.perf_counter() - started
    if code != 0:
        raise SystemExit(f"doctor --offline{''.join(' ' + a for a in extra)}"
                         f" exited {code}")
    return elapsed


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="bench-doctor-offline-") as tmp:
        root = Path(tmp) / "fixture"
        root.mkdir()
        files = build_fixture(root)
        previous = os.getcwd()
        os.chdir(root)
        try:
            grid_s = time_mode("offline", ())
            json_s = time_mode("offline-json", ("--json",))
        finally:
            os.chdir(previous)
    print(json.dumps({"mode": "offline", "files": files,
                      "seconds": round(grid_s, 3)}))
    print(json.dumps({"mode": "offline-json", "files": files,
                      "seconds": round(json_s, 3)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
