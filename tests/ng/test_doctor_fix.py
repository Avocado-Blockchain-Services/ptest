"""Acceptance for `ptest doctor --fix`: plan, direct apply, mention."""
from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest.cli import main
from support import write_ptest_toml


PROJECT_ID = "ab" * 16


def _write_pyproject(root: Path, *, groups: dict | None = None) -> None:
    lines = ["[project]", 'name = "fixture"', "dependencies = []"]
    if groups:
        lines.append("")
        lines.append("[dependency-groups]")
        for name, deps in groups.items():
            lines.append(f"{name} = [")
            lines.extend(f'    "{dep}",' for dep in deps)
            lines.append("]")
    (root / "pyproject.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_addopts(root: Path, addopts: str) -> None:
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        f"addopts = '{addopts}'\n",
        encoding="utf-8",
    )


def _stub_xdist_venv(root: Path) -> None:
    from ptest.runtime.pytest_bridge import _COVERAGE_TUPLE

    packages = root / ".venv" / "lib" / "python3.12" / "site-packages"
    dist_info = packages / "pytest_xdist-3.8.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: pytest-xdist\nVersion: 3.8.0\n",
        encoding="utf-8",
    )
    # The tier probe reads versions from dist-info directory names only,
    # so these stubs pin the frozen pytest-cov/coverage pair by name.
    pytest_cov, coverage = _COVERAGE_TUPLE
    (packages / f"pytest_cov-{pytest_cov}.dist-info").mkdir(exist_ok=True)
    (packages / f"coverage-{coverage}.dist-info").mkdir(exist_ok=True)
    (root / ".venv" / "pyvenv.cfg").write_text(
        "home = /usr/bin\ninclude-system-site-packages = false\nversion = 3.12\n",
        encoding="utf-8",
    )


def _write_config(root: Path, *, args=(), setup: bool = False,
                  selection: bool = False, extra_lines=()) -> None:
    setup_block = None
    if setup:
        setup_block = {
            "argv": ["uv", "sync", "--locked"],
            "required_paths": [".venv/bin/python"],
            "network": True,
            "lifecycle_scripts": True,
        }
    tail = ""
    if selection:
        tail += "\n[selection]\nenabled = false\n"
    if extra_lines:
        tail += "\n".join(extra_lines) + "\n"
    write_ptest_toml(
        root, kind="pytest",
        launcher=("uv", "run", "--locked", "--no-sync", "python"),
        args=args, project_id=PROJECT_ID, setup=setup_block, tail=tail,
    )


def _write_uv_pyproject(root: Path, *, dependencies=(), extras=None,
                        groups=None, addopts="") -> None:
    """Write a uv-style pyproject with main deps, extras, groups, addopts."""
    lines = ["[project]", 'name = "fixture"', "dependencies = ["]
    lines.extend(f'    "{dep}",' for dep in dependencies)
    lines.append("]")
    if extras:
        lines.append("")
        lines.append("[project.optional-dependencies]")
        for name, deps in extras.items():
            lines.append(f"{name} = [")
            lines.extend(f'    "{dep}",' for dep in deps)
            lines.append("]")
    if groups:
        lines.append("")
        lines.append("[dependency-groups]")
        for name, deps in groups.items():
            lines.append(f"{name} = [")
            lines.extend(f'    "{dep}",' for dep in deps)
            lines.append("]")
    if addopts is not None:
        lines.append("")
        lines.append("[tool.pytest.ini_options]")
        lines.append(f"addopts = '{addopts}'")
    (root / "pyproject.toml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


def _write_uv_project(root: Path, *, dependencies=(), extras=None,
                      groups=None, addopts="", args=()) -> Path:
    """One uv pytest fixture: tests, lock, pyproject, venv stubs, config."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_example.py").write_text(
        "def test_example():\n    assert True\n", encoding="utf-8")
    (root / "tests" / "conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n    return None\n",
        encoding="utf-8")
    (root / "uv.lock").write_text("", encoding="utf-8")
    _write_uv_pyproject(root, dependencies=dependencies, extras=extras,
                        groups=groups, addopts=addopts)
    _stub_xdist_venv(root)
    _write_config(root, args=args, setup=True)
    return root


def _write_pytest_project(root: Path, *, addopts="-n 4", groups=None,
                          lock=True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_example.py").write_text(
        "def test_example():\n    assert True\n", encoding="utf-8")
    (root / "tests" / "conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n    return None\n",
        encoding="utf-8")
    if addopts is not None:
        _write_addopts(root, addopts)
    if groups is not None or addopts is not None:
        base = root / "pyproject.toml"
        if groups is not None:
            _write_pyproject(root, groups=groups)
            if addopts is not None:
                with base.open("a", encoding="utf-8") as handle:
                    handle.write("\n[tool.pytest.ini_options]\n"
                                 f"addopts = '{addopts}'\n")
    if lock:
        (root / "uv.lock").write_text("", encoding="utf-8")
    _stub_xdist_venv(root)
    return root


def _no_review(monkeypatch):
    monkeypatch.setattr(
        "ptest.cli.agent_providers.resolve_reviewer",
        lambda *args, **kwargs: pytest.fail("fix resolved a reviewer"),
    )
    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews",
        lambda *args, **kwargs: pytest.fail("fix launched a reviewer"),
    )
    monkeypatch.setattr(
        "ptest.operations.execute",
        lambda *args, **kwargs: pytest.fail("fix executed project tests"),
    )


def test_fix_drops_stale_n0_when_parallel_tier_qualifies(
        tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "stale")
    _write_config(root, args=("-n", "0"))
    before = (root / ".ptest.toml").read_bytes()
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    out = capsys.readouterr().out
    assert "updated .ptest.toml" in out
    raw = (root / ".ptest.toml").read_text(encoding="utf-8")
    assert "\nargs = []\n" in raw
    assert f'project_id = "{PROJECT_ID}"' in raw
    # Only the managed args line changed.
    assert [line for line in raw.splitlines() if not line.startswith("args = ")] == [
        line for line in before.decode().splitlines() if not line.startswith("args = ")]


def test_fix_drops_stale_n0_with_cov_when_tier_qualifies(
        tmp_path, monkeypatch, capsys):
    """A stale `-n 0` is dropped on a `--cov` project when the tier qualifies.

    Regression pin for the coverage merge: if stale detection regressed
    to treating `--cov` as serial, the tier reason would no longer be
    exactly "sets -n 0" and the fallback would be kept.
    """
    root = _write_pytest_project(tmp_path / "stale-cov")
    _write_config(root, args=("-n", "0", "--cov", "pkg",
                              "--cov-report", "term"))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix", "--dry-run")) == 0
    out = capsys.readouterr().out
    assert "DRAFT" in out
    assert '-args = ["-n", "0", "--cov", "pkg", "--cov-report", "term"]' in out
    assert '+args = ["--cov", "pkg", "--cov-report", "term"]' in out
    assert "serial" not in out.lower()

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    raw = (root / ".ptest.toml").read_text(encoding="utf-8")
    assert '"-n", "0"' not in raw
    assert '"--cov"' in raw


def test_fix_leaves_default_dev_group_out_of_setup_argv(
        tmp_path, monkeypatch, capsys):
    """pytest in the default `dev` group needs no flag: plain sync covers it."""
    root = _write_uv_project(tmp_path / "dev", groups={"dev": ["pytest>=8"]})
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    raw = (root / ".ptest.toml").read_text(encoding="utf-8")
    assert 'argv = ["uv", "sync", "--locked"]' in raw
    assert "--group" not in raw
    assert "--extra" not in raw


def test_fix_adds_extra_for_ptest_shaped_pyproject(tmp_path, monkeypatch, capsys):
    """Shaped like ptest itself: pytest in dev, cov/xdist in the test extra."""
    root = _write_uv_project(
        tmp_path / "shaped",
        groups={"dev": ["pytest==9.1.1", "bandit==1.9.4"]},
        extras={"test": ["coverage==7.15.0", "pytest-cov==7.1.0",
                         "pytest-xdist==3.8.0"]},
        addopts="-n auto --dist loadgroup",
        args=("--cov", "pkg", "--cov-report", "term"),
    )
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    raw = (root / ".ptest.toml").read_text(encoding="utf-8")
    assert 'argv = ["uv", "sync", "--locked", "--extra", "test"]' in raw
    assert "--group" not in raw


def test_fix_adds_group_for_plugin_in_non_default_group(
        tmp_path, monkeypatch, capsys):
    """pytest-cov in a non-default group is named with --group."""
    root = _write_uv_project(
        tmp_path / "nongroup",
        dependencies=["pytest>=8"],
        groups={"testdeps": ["pytest-cov==7.1.0"]},
        args=("--cov",),
    )
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    raw = (root / ".ptest.toml").read_text(encoding="utf-8")
    assert 'argv = ["uv", "sync", "--locked", "--group", "testdeps"]' in raw


def test_fix_never_invents_a_missing_extra(tmp_path, monkeypatch, capsys):
    """--cov in args but no cov plugin anywhere: no flag is invented."""
    root = _write_uv_project(
        tmp_path / "missing", dependencies=["pytest>=8"], args=("--cov",),
    )
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    raw = (root / ".ptest.toml").read_text(encoding="utf-8")
    assert 'argv = ["uv", "sync", "--locked"]' in raw
    assert "--group" not in raw
    assert "--extra" not in raw


def test_fix_adds_extra_for_xdist_when_n_is_used(tmp_path, monkeypatch, capsys):
    """-n in runner args needs pytest-xdist from its extra."""
    root = _write_uv_project(
        tmp_path / "xdist",
        dependencies=["pytest>=8"],
        extras={"test": ["pytest-xdist==3.8.0"]},
        args=("-n", "auto"),
    )
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    raw = (root / ".ptest.toml").read_text(encoding="utf-8")
    assert 'argv = ["uv", "sync", "--locked", "--extra", "test"]' in raw


def test_fix_proposes_selection_draft_with_cov(tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "cov", addopts="")
    _write_config(root, args=("--cov",), selection=True)
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix", "--dry-run")) == 0
    out = capsys.readouterr().out
    assert "enabled = true" in out
    assert "closed_inputs = true" in out
    assert "input_roots" in out
    assert "full_triggers" in out
    assert "DRAFT" in out
    # Dry run writes nothing.
    assert 'enabled = false' in (root / ".ptest.toml").read_text(encoding="utf-8")

    assert main(("doctor", "--fix")) == 0
    raw = (root / ".ptest.toml").read_text(encoding="utf-8")
    assert "enabled = true" in raw
    assert "closed_inputs = true" in raw
    capsys.readouterr()


# The ten `[selection]` lines exactly as the 0.2.x `_serialize_fresh`
# writes them for a uv pytest project: every key present, `enabled` off.
_GENERATED_SELECTION = (
    "enabled = false",
    "closed_inputs = false",
    "input_roots = []",
    "ignored_inputs = []",
    "environment = []",
    "full_triggers = []",
    "always = []",
    "no_tests = []",
    'non_input_outputs = [".venv"]',
    "full_ratio = 0.7",
)


def test_fix_flips_generator_default_enabled_without_cov(
        tmp_path, monkeypatch, capsys):
    """A generator-shaped `enabled = false` table flips enablement only.

    No `--cov`, so the coverage draft branch does not apply: the fix is
    exactly one `enabled = true` line, with no draft and no `groups` key.
    """
    root = _write_pytest_project(tmp_path / "genoff")
    _write_config(root, args=("-n", "0"),
                  extra_lines=("", "[selection]", *_GENERATED_SELECTION))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix", "--dry-run")) == 0
    out = capsys.readouterr().out
    assert "enabled = true" in out
    assert "closed_inputs = true" not in out
    assert 'enabled = false' in (root / ".ptest.toml").read_text(
        encoding="utf-8")

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    selection = tomllib.loads(
        (root / ".ptest.toml").read_text(encoding="utf-8"))["selection"]
    assert selection == {
        "enabled": True,
        "closed_inputs": False,
        "input_roots": [],
        "ignored_inputs": [],
        "environment": [],
        "full_triggers": [],
        "always": [],
        "no_tests": [],
        "non_input_outputs": [".venv"],
        "full_ratio": 0.7,
    }

    assert main(("doctor", "--fix")) == 0
    assert "config is up to date" in capsys.readouterr().out


@pytest.mark.parametrize("selection_lines", [
    ("[selection]", "enabled = false"),
    ("[selection]", *_GENERATED_SELECTION[:-1], "full_ratio = 0.5"),
    ("[selection]", *_GENERATED_SELECTION, "groups = []"),
    ("[selection]", *[line for line in _GENERATED_SELECTION
                       if not line.startswith("always =")]),
], ids=["hand-written", "tuned-ratio", "extra-groups", "missing-always"])
def test_fix_leaves_explicit_selection_off_alone(
        tmp_path, monkeypatch, capsys, selection_lines):
    """Anything but a generator-shaped table is the user's choice: no flip."""
    root = _write_pytest_project(tmp_path / "explicit")
    _write_config(root, args=("-n", "0"),
                  extra_lines=("", *selection_lines))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    before = tomllib.loads(
        (root / ".ptest.toml").read_text(encoding="utf-8"))["selection"]

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    after = tomllib.loads(
        (root / ".ptest.toml").read_text(encoding="utf-8"))["selection"]
    assert after == before
    assert after["enabled"] is False


def test_fix_appends_missing_selection_table_at_eof(
        tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "nosel", addopts="")
    _write_config(root, args=("--cov",))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    raw = (root / ".ptest.toml").read_text(encoding="utf-8")
    assert "\n[selection]\nenabled = true\nclosed_inputs = true\n" in raw
    assert main(("doctor", "--fix")) == 0
    assert "config is up to date" in capsys.readouterr().out


def test_fix_keeps_hand_tuned_selection_values_and_enables_only(
        tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "tuned", addopts="")
    _write_config(root, args=("--cov",), extra_lines=(
        "",
        "[selection]",
        "enabled = false",
        'input_roots = ["tests", "libs/mylib"]',
        "closed_inputs = false",
        'groups = [{name = "core", sources = ["src/core"], '
        'tests = ["tests/test_core.py"]}]',
    ))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix", "--dry-run")) == 0
    out = capsys.readouterr().out
    assert "enabled = true" in out
    # Default-valued keys count as unset: the draft closes and fills them.
    assert "closed_inputs = true" in out
    assert "full_triggers" in out
    # Non-default hand-tuned values stay untouched (visible as context).
    assert '"libs/mylib"' in out
    assert '"src/core"' in out

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    raw = (root / ".ptest.toml").read_text(encoding="utf-8")
    parsed = tomllib.loads(raw)
    assert parsed["selection"]["enabled"] is True
    assert parsed["selection"]["closed_inputs"] is True
    assert parsed["selection"]["input_roots"] == ["tests", "libs/mylib"]
    assert parsed["selection"]["groups"] == [
        {"name": "core", "sources": ["src/core"],
         "tests": ["tests/test_core.py"]}]
    # Absent keys are still filled from the draft.
    assert parsed["selection"]["full_triggers"]


def test_fix_drafts_default_valued_selection_keys(tmp_path, monkeypatch, capsys):
    """Init-fresh defaults (false/empty lists) count as unset: drafted."""
    root = _write_pytest_project(tmp_path / "defaults", addopts="")
    _write_config(root, args=("--cov",), extra_lines=(
        "",
        "[selection]",
        "enabled = false",
        "closed_inputs = false",
        "input_roots = []",
        "full_triggers = []",
        "groups = []",
    ))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix", "--dry-run")) == 0
    out = capsys.readouterr().out
    assert "enabled = true" in out
    assert "closed_inputs = true" in out
    assert "input_roots" in out
    assert "full_triggers" in out
    assert "groups" in out
    # Dry run writes nothing.
    assert "closed_inputs = false" in (
        root / ".ptest.toml").read_text(encoding="utf-8")

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    parsed = tomllib.loads((root / ".ptest.toml").read_text(encoding="utf-8"))
    assert parsed["selection"]["enabled"] is True
    assert parsed["selection"]["closed_inputs"] is True
    assert parsed["selection"]["input_roots"]
    assert parsed["selection"]["full_triggers"]
    assert parsed["selection"]["groups"]
    assert main(("doctor", "--fix")) == 0
    assert "config is up to date" in capsys.readouterr().out


def test_fix_enabled_but_not_closed_is_out_of_date(
        tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "notclosed", addopts="")
    _write_config(root, args=("--cov",), extra_lines=(
        "",
        "[selection]",
        "enabled = true",
        "closed_inputs = false",
    ))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix", "--dry-run")) == 0
    out = capsys.readouterr().out
    assert "closed_inputs = true" in out
    assert "config is up to date" not in out

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    assert main(("doctor", "--fix")) == 0
    assert "config is up to date" in capsys.readouterr().out


def test_fix_drafts_groups_from_src_layout(tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "pkgs", addopts="")
    (root / "src" / "shop_cart").mkdir(parents=True)
    (root / "src" / "shop_cart" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "shop_search").mkdir(parents=True)
    (root / "src" / "shop_search" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "shop_cart").mkdir(exist_ok=True)
    (root / "tests" / "shop_cart" / "test_cart.py").write_text(
        "def test_cart():\n    assert True\n", encoding="utf-8")
    _write_config(root, args=("--cov",))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    parsed = tomllib.loads((root / ".ptest.toml").read_text(encoding="utf-8"))
    assert parsed["selection"]["input_roots"] == [
        "src/shop_cart", "src/shop_search", "tests"]
    # Import-derived, not folder-name-derived: test_cart.py imports no src
    # package, so no package claims it and every group falls back to the
    # declared test roots (fail safe).
    assert parsed["selection"]["groups"] == [
        {"name": "shop_cart", "sources": ["src/shop_cart"],
         "tests": ["tests"]},
        {"name": "shop_search", "sources": ["src/shop_search"],
         "tests": ["tests"]},
    ]


def _write_import_twin(root: Path) -> Path:
    """Persea-like twin: pkg_b imports pkg_a; unit tests; factories; conftests."""
    files = {
        "pyproject.toml": '[project]\nname = "twin"\ndependencies = []\n',
        "uv.lock": "",
        "src/pkg_a/__init__.py": "VALUE = 1\n",
        "src/pkg_a/core.py": (
            "from pkg_a import VALUE\n\n"
            "def double():\n    return VALUE * 2\n"),
        "src/pkg_b/__init__.py": "",
        "src/pkg_b/svc.py": (
            "from pkg_a.core import double\n\n"
            "def quad():\n    return double() * 2\n"),
        "tests/__init__.py": "",
        "tests/conftest.py": "",
        "tests/unit/__init__.py": "",
        "tests/unit/conftest.py": "",
        "tests/unit/test_a.py": (
            "from pkg_a.core import double\n\n"
            "def test_double():\n    assert double() == 2\n"),
        "tests/unit/test_both.py": (
            "from pkg_a.core import double\n"
            "from pkg_b.svc import quad\n\n"
            "def test_quad():\n    assert quad() == 4\n"),
        "tests/test_b.py": (
            "from pkg_b.svc import quad\n\n"
            "def test_quad_b():\n    assert quad() == 4\n"),
        "tests/test_plain.py": "def test_plain():\n    assert True\n",
        "tests/test_factory_user.py": (
            "from tests.factories.f import make\n\n"
            "def test_make():\n    assert make() == 1\n"),
        "tests/factories/__init__.py": "",
        "tests/factories/conftest.py": "",
        "tests/factories/f.py": "def make():\n    return 1\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _write_config(root, args=("--cov",))
    return root


def _twin_selection(root: Path) -> dict:
    return tomllib.loads(
        (root / ".ptest.toml").read_text(encoding="utf-8"))["selection"]


def test_fix_drafts_import_derived_groups_with_transitive_deps(
        tmp_path, monkeypatch, capsys):
    root = _write_import_twin(tmp_path / "twin")
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    selection = _twin_selection(root)
    assert selection["input_roots"] == ["src/pkg_a", "src/pkg_b", "tests"]
    # pkg_b transitively imports pkg_a, so a pkg_a change must also pull
    # tests that only import pkg_b. tests/unit is fully covered by pkg_a's
    # files and compresses to the directory.
    assert selection["groups"] == [
        {"name": "pkg_a", "sources": ["src/pkg_a"],
         "tests": ["tests/test_b.py", "tests/unit"]},
        {"name": "pkg_b", "sources": ["src/pkg_b"],
         "tests": ["tests/test_b.py", "tests/unit/test_both.py"]},
    ]


def test_fix_drafts_full_triggers_cover_conftests_and_helpers(
        tmp_path, monkeypatch, capsys):
    root = _write_import_twin(tmp_path / "twin-triggers")
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    triggers = _twin_selection(root)["full_triggers"]
    assert ".ptest.toml" in triggers
    assert "uv.lock" in triggers
    assert "pyproject.toml" in triggers
    # Every conftest.py under the child, at any depth.
    assert "tests/conftest.py" in triggers
    assert "tests/unit/conftest.py" in triggers
    # The shared factories dir is imported by tests but holds no tests
    # itself, so it compresses to one directory trigger (which also covers
    # its nested conftest.py).
    assert "tests/factories" in triggers
    assert "tests/factories/conftest.py" not in triggers
    # The tests package init executes on every tests.factories import.
    assert "tests/__init__.py" in triggers


def test_fix_leaves_unimported_test_file_out_of_every_group(
        tmp_path, monkeypatch, capsys):
    root = _write_import_twin(tmp_path / "twin-plain")
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    groups = _twin_selection(root)["groups"]
    assert groups
    for group in groups:
        assert "tests/test_plain.py" not in group["tests"]
        assert "tests/test_factory_user.py" not in group["tests"]
    # The importing tests are claimed exactly once per dependency direction.
    by_name = {group["name"]: group["tests"] for group in groups}
    assert "tests/unit/test_a.py" in by_name["pkg_a"] or "tests/unit" in by_name["pkg_a"]
    assert "tests/unit/test_a.py" not in by_name["pkg_b"]
    assert "tests/unit/test_both.py" in by_name["pkg_b"] or "tests/unit" in by_name["pkg_b"]


def test_fix_never_compresses_triggers_over_test_dirs(
        tmp_path, monkeypatch, capsys):
    root = _write_import_twin(tmp_path / "twin-shadow")
    (root / "tests" / "unit" / "helpers.py").write_text(
        "def h():\n    return 1\n", encoding="utf-8")
    test_a = root / "tests" / "unit" / "test_a.py"
    test_a.write_text(
        "from pkg_a.core import double\n"
        "from tests.unit import helpers\n\n"
        "def test_double():\n    assert double() == 2\n"
        "    assert helpers.h() == 1\n",
        encoding="utf-8")
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    selection = _twin_selection(root)
    # Every non-test module under tests/unit is imported, but the
    # directory also holds tests: compressing the trigger to tests/unit
    # would shadow the groups and force full on any unit-test change.
    assert "tests/unit" not in selection["full_triggers"]
    assert "tests/unit/helpers.py" in selection["full_triggers"]
    assert "tests/unit/__init__.py" in selection["full_triggers"]
    # Group compression is unaffected: pkg_a still covers tests/unit.
    by_name = {group["name"]: group["tests"]
               for group in selection["groups"]}
    assert "tests/unit" in by_name["pkg_a"]


def test_fix_treats_unparseable_test_file_as_importing_everything(
        tmp_path, monkeypatch, capsys):
    root = _write_import_twin(tmp_path / "twin-broken")
    (root / "tests" / "test_broken.py").write_text(
        "def broken(:\n", encoding="utf-8")
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    groups = _twin_selection(root)["groups"]
    # Fail safe: a file the static scan cannot read is never silently
    # skipped; it joins every group.
    assert groups
    for group in groups:
        assert "tests/test_broken.py" in group["tests"]


def test_fix_handles_quoted_table_header_without_duplication(
        tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "quoted", addopts="")
    _write_config(root, args=("--cov",), selection=True)
    quoted = (root / ".ptest.toml").read_text(encoding="utf-8")
    quoted = quoted.replace("[selection]", '["selection"]')
    (root / ".ptest.toml").write_text(quoted, encoding="utf-8")
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    raw = (root / ".ptest.toml").read_text(encoding="utf-8")
    tomllib.loads(raw)
    assert '["selection"]' in raw
    assert "\n[selection]\n" not in raw
    assert "enabled = true" in raw


def test_fix_handles_quoted_key_without_duplication(
        tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "quoted-key")
    _write_config(root, args=("-n", "0"))
    quoted = (root / ".ptest.toml").read_text(encoding="utf-8")
    quoted = quoted.replace('args = ["-n", "0"]', '"args" = ["-n", "0"]')
    (root / ".ptest.toml").write_text(quoted, encoding="utf-8")
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    raw = (root / ".ptest.toml").read_text(encoding="utf-8")
    tomllib.loads(raw)
    assert "args" in raw
    assert '"-n", "0"' not in raw


def test_fix_preserves_unmanaged_settings_byte_for_byte(
        tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "kept")
    _write_config(root, args=("-n", "0"), extra_lines=(
        "# a user comment",
        "",
        "[selection]",
        "enabled = false",
        "full_ratio = 0.5",
    ))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    lines = (root / ".ptest.toml").read_text(encoding="utf-8").splitlines()
    assert "# a user comment" in lines
    assert "enabled = false" in lines
    assert "full_ratio = 0.5" in lines


def test_fix_refuses_symlinked_config(tmp_path, monkeypatch, capsys):
    root = tmp_path / "linked"
    _write_pytest_project(root)
    _write_config(root, args=("-n", "0"))
    real = root / ".ptest.toml"
    target = root / "real.toml"
    target.write_bytes(real.read_bytes())
    real.unlink()
    real.symlink_to(target)
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 2
    captured = capsys.readouterr()
    assert "symlink" in captured.err or "unsafe-path" in captured.err
    assert target.read_bytes() == target.read_bytes()


def test_fix_refuses_concurrent_change(tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "race")
    _write_config(root, args=("-n", "0"))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)
    import ptest.files as files_module

    real_read = files_module.read_regular
    calls = []

    def raced_read(rdir, relative, limit):
        calls.append(relative)
        if relative == ".ptest.toml" and len(calls) == 2:
            path = Path(rdir) / relative
            path.write_bytes(real_read(rdir, relative, limit)
                             + b"# raced edit\n")
        return real_read(rdir, relative, limit)

    monkeypatch.setattr(files_module, "read_regular", raced_read)

    assert main(("doctor", "--fix")) == 2
    captured = capsys.readouterr()
    assert "changed" in captured.err


def test_fix_rejects_yes_flag_as_unknown_option(
        tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "noyes")
    _write_config(root, args=("-n", "0"))
    before = (root / ".ptest.toml").read_bytes()
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix", "--yes")) == 2
    captured = capsys.readouterr()
    assert "unknown inspection option" in captured.err
    assert (root / ".ptest.toml").read_bytes() == before


def test_fix_applies_without_asking_on_non_tty(tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "quiet")
    _write_config(root, args=("-n", "0"))
    before = (root / ".ptest.toml").read_bytes()
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    assert "\nargs = []\n" in (root / ".ptest.toml").read_text(encoding="utf-8")
    assert (root / ".ptest.toml").read_bytes() != before


def test_fix_applies_without_prompt_on_tty(tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "tty")
    _write_config(root, args=("-n", "0"))
    before = (root / ".ptest.toml").read_bytes()
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    _no_review(monkeypatch)

    def _no_input(*args, **kwargs):
        raise AssertionError("doctor --fix asked a question")

    monkeypatch.setattr("builtins.input", _no_input)
    assert main(("doctor", "--fix")) == 0
    captured = capsys.readouterr()
    assert "updated .ptest.toml" in captured.out
    assert "[y/N]" not in captured.err + captured.out
    assert "Apply these changes to" not in captured.err + captured.out
    assert (root / ".ptest.toml").read_bytes() != before


def test_fix_second_run_reports_up_to_date(tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "idem")
    _write_config(root, args=("-n", "0"))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    assert main(("doctor", "--fix")) == 0
    assert "config is up to date" in capsys.readouterr().out


def test_fix_covers_monorepo_children_and_leaves_dispatcher(
        tmp_path, monkeypatch, capsys):
    root = tmp_path / "mono"
    (root / "api" / "tests").mkdir(parents=True)
    (root / ".ptest.toml").write_text(
        'version = 2\n\n[monorepo]\nchildren = ["api"]\n', encoding="utf-8")
    before_root = (root / ".ptest.toml").read_bytes()
    api = root / "api"
    _write_pytest_project(api)
    _write_config(api, args=("-n", "0"))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    out = capsys.readouterr().out
    assert "updated api/.ptest.toml" in out
    assert (root / ".ptest.toml").read_bytes() == before_root
    assert "\nargs = []\n" in (api / ".ptest.toml").read_text(encoding="utf-8")


def test_doctor_mentions_fix_when_config_out_of_date(
        tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "mention")
    _write_config(root, args=("-n", "0"))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--offline")) == 0
    out = capsys.readouterr().out
    assert "config is out of date" in out
    assert "ptest doctor --fix" in out

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    assert main(("doctor", "--offline")) == 0
    # The staleness mention is gone once the config is up to date.  The
    # grid's gap-driven "Next: ptest doctor --fix" line is separate
    # contracted behavior (see test_doctor_grid.py), so this assertion
    # targets the mention text rather than the bare command substring.
    assert "config is out of date" not in capsys.readouterr().out


def test_review_text_mentions_fix_when_config_out_of_date(
        tmp_path, monkeypatch, capsys):
    import json as json_module

    from ptest.agent_providers import (
        ProviderResult,
        QualificationStatus,
        ReviewerAdapter,
    )

    root = _write_pytest_project(tmp_path / "review-mention")
    _write_config(root, args=("-n", "0"))
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR",
                       str(tmp_path / "locks"))
    monkeypatch.setattr(
        "ptest.cli.agent_providers.qualification_status",
        lambda name: QualificationStatus(
            name=name, qualified=True, argv=(name,),
            note="synthetic fix-mention profile"),
    )
    monkeypatch.setattr(
        "ptest.cli.agent_providers.resolve_reviewer",
        lambda name, env: ReviewerAdapter(
            name=name, executable=f"/synthetic/{name}",
            argv=(f"/synthetic/{name}",), qualified=True,
            qualification_note="synthetic fix-mention adapter"),
    )

    def launch_many(adapter, requests, timeout_s, **kwargs):
        results = []
        for _request, _schema in requests:
            results.append(ProviderResult(
                provider=adapter.name, ok=True,
                assessment=json_module.dumps({
                    "status": "unknown",
                    "rationale": ("The supplied bounded evidence does not "
                                  "establish this criterion."),
                    "evidence": [],
                    "finding": None,
                    "needs": [],
                }).encode("utf-8"),
                error="", exit_code=0, timed_out=False, cancelled=False,
                truncated=False, pid=7301, argv=adapter.argv,
                # Payload-only scratch label; never a filesystem path.
                scratch="/tmp/ptest-fix-mention",
            ))
        return tuple(results)

    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews", launch_many)
    monkeypatch.setattr(
        "ptest.operations.execute",
        lambda *args, **kwargs: pytest.fail("mention run executed tests"),
    )

    assert main(("doctor", "--reviewer", "claude",
                 "--allow-model-review")) == 0
    out = capsys.readouterr().out
    assert "config is out of date" in out
    assert "ptest doctor --fix" in out


def test_fix_combines_with_offline_and_never_reviews(
        tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "offfix")
    _write_config(root, args=("-n", "0"))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix", "--offline")) == 0
    assert "\nargs = []\n" in (root / ".ptest.toml").read_text(encoding="utf-8")
    capsys.readouterr()


def _write_vitest_project(root: Path) -> Path:
    """One vitest fixture without a [selection] exemption for node_modules."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "package-lock.json").write_text('{"lockfileVersion": 3}\n',
                                            encoding="utf-8")
    write_ptest_toml(root, kind="vitest", launcher=("node",),
                     project_id=PROJECT_ID)
    return root


def test_fix_adds_node_modules_to_vitest_config_and_is_idempotent(
        tmp_path, monkeypatch, capsys):
    """Vite's cache lives under node_modules: fix exempts it, once."""
    root = _write_vitest_project(tmp_path / "vitefix")
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    raw = (root / ".ptest.toml").read_text(encoding="utf-8")
    assert 'non_input_outputs = ["node_modules"]' in raw

    assert main(("doctor", "--fix")) == 0
    assert "config is up to date" in capsys.readouterr().out
    assert (root / ".ptest.toml").read_text(encoding="utf-8") == raw
