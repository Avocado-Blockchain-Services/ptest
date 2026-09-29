"""Before/after benchmark for rank_candidates (T2, not a test).

Generates a deterministic ~1000-file, ~700-role fixture in a temporary
directory, builds one online packet through ``build_packets`` on the
generated child (which exercises ``rank_candidates`` over the pool), and
prints one JSON line: {"mode", "files", "seconds"}.

Runs on base 985d43e/09f6f40 as well as on the optimized tree; record both
numbers in the T2 report. Must be launched from the checkout under test so
``ptest.cli.main`` resolves to that checkout (never the installed CLI).
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path


CHILDREN = ("api", "web")
FILES_PER_CHILD = 500


def _write_py(path: Path, index: int) -> None:
    path.write_text(
        "import sqlite3\n"
        f"DB_URL_{index} = 'sqlite:///t{index}.db'\n\n"
        f"def handle_{index}(request):\n"
        f"    conn = sqlite3.connect(DB_URL_{index})\n"
        "    cur = conn.cursor()\n"
        "    cur.execute('SELECT 1')\n"
        "    conn.commit()\n"
        "    return cur.fetchall()\n",
        encoding="utf-8",
    )


def _write_test(path: Path, index: int) -> None:
    path.write_text(
        f"def test_handle_{index}(db):\n"
        f"    from src.mod{index} import handle_{index}\n"
        f"    assert handle_{index}(db) is not None\n",
        encoding="utf-8",
    )


def _write_js(path: Path, index: int) -> None:
    path.write_text(
        "import { useState } from 'react';\n"
        f"export function Comp{index}(props) {{\n"
        "  const [x, setX] = useState(0);\n"
        f"  return fetch('/api/{index}');\n}}\n",
        encoding="utf-8",
    )


def build_fixture(root: Path) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "ng"))
    from factories_agents import agent_v1_config_text

    (root / ".ptest.toml").write_text(
        "version = 2\n[monorepo]\nchildren = "
        + json.dumps(list(CHILDREN)) + "\n",
        encoding="utf-8",
    )
    total = 0
    for child_no, child in enumerate(CHILDREN):
        base = root / child
        (base / "src").mkdir(parents=True)
        (base / "tests").mkdir(parents=True)
        (base / ".ptest.toml").write_text(
            agent_v1_config_text(f"{child_no:02d}" * 16, "pytest"),
            encoding="utf-8",
        )
        for i in range(FILES_PER_CHILD):
            kind = i % 5
            if kind in (0, 1):
                _write_py(base / "src" / f"mod{i:03d}.py", i)
            elif kind == 2:
                _write_test(base / "tests" / f"test_mod{i:03d}.py", i)
            elif kind == 3:
                _write_js(base / "src" / f"comp{i:03d}.js", i)
            else:
                _write_test(base / "tests" / f"test_js{i:03d}.py", i)
            total += 1
        (base / "tests" / "conftest.py").write_text(
            "import pytest\n@pytest.fixture\ndef db():\n    return object()\n",
            encoding="utf-8",
        )
        total += 1
    return total


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        total = build_fixture(root)
        from ptest import agent_assessment as AA
        from ptest import contracts as C
        from ptest import config as config_api
        from ptest import doctor
        from factories_agents import agent_domain as _domain

        resolution = config_api.resolve_config(root)
        workspace = doctor.inspect_workspace(
            _domain(root), resolution, C.DEFAULT_SCAN_LIMITS, None)
        # Warm up caches once so the timed run measures steady-state ranking.
        AA.build_packets(workspace, resolution)
        started = time.perf_counter()
        AA.build_packets(workspace, resolution)
        seconds = time.perf_counter() - started
        print(json.dumps({"mode": "rank_candidates",
                          "files": total, "seconds": round(seconds, 3)}))


if __name__ == "__main__":
    main()
