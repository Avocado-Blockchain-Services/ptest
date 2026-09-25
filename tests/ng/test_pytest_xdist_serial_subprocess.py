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

import support

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
    # Environment fallbacks are reported, never written: the fake xdist
    # plugin has no verifiable install, so init leaves args empty and the
    # adapter generates -n 0 itself for the serial run.
    assert generated == []

    env = dict(os.environ)
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    # The test extra installs real xdist: without autoload disabled its
    # entry point would register the PYTHONPATH-shadowed fake module twice.
    # The explicit -p xdist.plugin still loads the fake plugin, which is
    # what this twin exercises.
    scoped = case.invoke(domain, root, "--", "tests/test_sample.py",
                         env={"PYTHONPATH": env["PYTHONPATH"],
                              "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}, timeout=15)

    assert scoped.code == 0, scoped.stderr.decode()
    assert b"1 deselected" in scoped.stdout
    assert b"1 passed" in scoped.stdout


def test_xdist_addopts_full_run_is_project_filtered_with_label(case, monkeypatch):
    """Section F: --full runs the checked-in suite and labels its filters."""
    from ptest import cli

    domain = case.domain()
    root = domain.root / "xdist-full"
    root.mkdir()
    _materialize(root)
    with (root / "tests" / "conftest.py").open("a", encoding="utf-8") as handle:
        handle.write("\n\ndef pytest_collection_modifyitems(items):\n    return None\n")

    monkeypatch.chdir(root)
    assert cli.main(("init", "--no-doctor", "--agents", "none")) == 0
    support.init_git_repo(root)

    label = "full (project-filtered: -m not slow; conftest collection hook)"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    full = case.invoke(domain, root, "--full",
                       env={"PYTHONPATH": env["PYTHONPATH"],
                            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}, timeout=60)

    assert full.code == 0, full.stderr.decode()
    assert b"1 deselected" in full.stdout
    assert b"1 passed" in full.stdout
    assert label in full.stderr.decode()
    assert full.result is not None
    data = full.result["data"]
    assert data["mode"] == "full"
    assert any(reason["code"] == "project-filtered" and reason["message"] == label
               for reason in data["reasons"])
    # The static prediction stays out of the plan: only the bridge-owned
    # run label may use the project-filtered code and wording.
    assert not any(reason["code"] == "project-filtered"
                   for reason in data["plan"]["reasons"])


def test_full_run_refuses_pytest_addopts_from_environment(case, monkeypatch):
    """Section F: PYTEST_ADDOPTS narrowing is still refused in full mode."""
    from ptest import cli

    domain = case.domain()
    root = domain.root / "xdist-env"
    root.mkdir()
    _materialize(root)

    monkeypatch.chdir(root)
    assert cli.main(("init", "--no-doctor", "--agents", "none")) == 0
    support.init_git_repo(root)

    env = dict(os.environ)
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTEST_ADDOPTS"] = "-m slow"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    full = case.invoke(domain, root, "--full", env=env, timeout=60)

    assert full.code == 4
    assert b"ptest-bridge-refusal" in full.stderr
