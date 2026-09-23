"""Serial xdist regression: persea-shaped addopts run under ``-n 0``.

Materializes ``fixtures/pytest/xdist_addopts`` into a tmp project, runs
``ptest init``, then runs one scoped test file. The generated runner args
must disable xdist, the bridge must accept the loaded-but-inactive fake
xdist plugin, and the ``-m`` filter from addopts must still apply.
"""
from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "pytest" / "xdist_addopts"


def _materialize(root: Path) -> None:
    shutil.copyfile(FIXTURES / "pyproject.toml.txt", root / "pyproject.toml")
    package = root / "xdist"
    package.mkdir()
    shutil.copyfile(FIXTURES / "xdist_init.py.txt", package / "__init__.py")
    shutil.copyfile(FIXTURES / "xdist_plugin.py.txt", package / "plugin.py")
    tests = root / "tests"
    tests.mkdir()
    shutil.copyfile(FIXTURES / "conftest.py.txt", tests / "conftest.py")
    shutil.copyfile(FIXTURES / "test_sample.py.txt", tests / "test_sample.py")


def test_xdist_addopts_init_serial_then_scoped_run(case, monkeypatch):
    from ptest import cli

    domain = case.domain()
    root = domain.root / "xdist-proj"
    root.mkdir()
    _materialize(root)

    # The invoke harness injects --result-json ahead of the subcommand, which
    # the init grammar does not accept; drive init through cli.main instead.
    monkeypatch.chdir(root)
    assert cli.main(("init", "--no-doctor", "--agents", "none")) == 0
    generated = tomllib.loads((root / ".ptest.toml").read_text())["runner"]["args"]
    assert generated == ["-n", "0"]

    env = dict(os.environ)
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    scoped = case.invoke(domain, root, "--", "tests/test_sample.py",
                         env={"PYTHONPATH": env["PYTHONPATH"]}, timeout=15)

    assert scoped.code == 0, scoped.stderr.decode()
    assert b"1 deselected" in scoped.stdout
    assert b"1 passed" in scoped.stdout
