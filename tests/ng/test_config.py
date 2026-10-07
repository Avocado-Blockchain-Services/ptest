"""Fresh local configuration resolution and validation contracts."""
from __future__ import annotations

import builtins
import io
import json
import os
import sys
from pathlib import Path

import pytest

from ptest.config import _serialize_fresh, init_project, resolve_config
from ptest import contracts as C
from ptest.contracts import InitOptions, Problem, RunnerKind, SelectionPolicy


def test_v2_root_resolution_is_discriminated_from_v1(tmp_path):
    (tmp_path / ".ptest.toml").write_text(
        'version = 2\n[monorepo]\nchildren = ["api", "web"]\n',
        encoding="utf-8",
    )

    resolution = resolve_config(tmp_path)

    assert resolution.problem is None
    assert resolution.config is None
    assert resolution.monorepo is not None
    assert resolution.monorepo.children == ("api", "web")


@pytest.mark.parametrize("children", [[], ["api", "api"], ["api", "api/tests"], ["../api"], ["api\\web"], ["/api"]])
def test_v2_root_manifest_rejects_unsafe_children(tmp_path, children):
    (tmp_path / ".ptest.toml").write_text(
        "version = 2\n[monorepo]\nchildren = " + json.dumps(children) + "\n",
        encoding="utf-8",
    )

    resolution = resolve_config(tmp_path)

    assert resolution.monorepo is None
    assert resolution.problem is not None
    assert resolution.problem.code == "invalid-config"


def test_full_execution_runs_all_children_and_returns_first_failure():
    from ptest.monorepo import execute_full

    children = tuple(object() for _ in range(3))
    seen = []

    result = execute_full(children, lambda child: seen.append(child) or (7 if child is children[1] else 0))

    assert seen == list(children)
    assert result == 7


# Verbatim section 5 example from the frozen design, including its comments.
DESIGN_CONFIG = '''version = 1
project_id = "cd58ec6cf99748ce9f15dfce137f044d" # generated 128-bit hex at init

[runner]
kind = "pytest"
launcher = ["uv", "run", "--locked", "--no-sync", "python"]
args = []
full_args = ["--cov=sample", "--cov-fail-under=85"]
test_roots = ["tests"]
workers = 1
lifecycle = "cooperative-process-group"

[setup]
argv = ["uv", "sync", "--locked"]
required_paths = [".venv/bin/python"]
network = true
lifecycle_scripts = true # dependency build/lifecycle code may execute during setup

[resources]
locks = []
memory_mb_per_worker = 0 # 0 means unknown, not free memory
probe_isolation = "undeclared" # or "run-worker-namespaced"

[selection]
enabled = false
closed_inputs = false
input_roots = ["src", "tests"]
ignored_inputs = []
environment = []
full_triggers = ["pyproject.toml", "uv.lock", "conftest.py"]
always = []
no_tests = []
non_input_outputs = []
full_ratio = 0.70

[[selection.groups]]
name = "sample"
sources = ["src/sample"]
tests = ["tests/sample"]
'''


def _write_config(root: Path, *, project_id: str = "ab" * 16,
                  runner: str = "pytest", extra: str = "") -> Path:
    path = root / ".ptest.toml"
    path.write_text(
        "version = 1\n"
        f'project_id = "{project_id}"\n'
        "\n[runner]\n"
        f'kind = "{runner}"\n'
        'launcher = ["python"]\n'
        'test_roots = ["tests"]\n'
        "\n[resources]\n"
        "\n[selection]\n"
        "enabled = false\n"
        f"{extra}",
        encoding="utf-8",
    )
    return path


def test_missing_config_is_initialization_required(tmp_path):
    resolution = resolve_config(tmp_path)
    assert resolution.config is None
    assert resolution.path is None
    assert resolution.problem is not None
    assert resolution.problem.code == "initialization-required"


def _git_root(root):
    marker = root / ".git"
    marker.mkdir()
    (marker / "HEAD").write_text("ref: refs/heads/main\n")
    (marker / "config").write_text("[core]\n\trepositoryformatversion = 0\n")


def test_explicit_children_create_root_dispatcher_and_child_configs(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git_root(root)
    api = root / "api"
    web = root / "web"
    api.mkdir()
    web.mkdir()
    (api / "pyproject.toml").write_text("[project]\ndependencies = []\n")
    (web / "package.json").write_text('{"devDependencies":{"vitest":"1"}}')

    result = init_project(root, InitOptions(
        runner=None, dry_run=False, reveal_command=False,
        children=(("api", RunnerKind.PYTEST), ("web", RunnerKind.VITEST)),
    ))

    assert result.target == root / ".ptest.toml"
    assert result.action.value == "created"
    assert (root / ".ptest.toml").read_text() == (
        'version = 2\n\n[monorepo]\nchildren = ["api", "web"]\n'
    )
    assert (api / ".ptest.toml").read_text().startswith("version = 1\n")
    assert (web / ".ptest.toml").read_text().startswith("version = 1\n")


def test_init_auto_bootstraps_immediate_runner_children_from_git_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git_root(root)
    (root / "api").mkdir()
    (root / "web").mkdir()
    (root / "api" / "pyproject.toml").write_text(
        '[project]\ndependencies = ["pytest>=8"]\n'
    )
    (root / "web" / "package.json").write_text('{"devDependencies":{"vitest":"1"}}')

    result = init_project(root, InitOptions(
        runner=None, dry_run=False, reveal_command=False,
    ))

    assert result.action.value == "created"
    assert (root / ".ptest.toml").read_text() == (
        'version = 2\n\n[monorepo]\nchildren = ["api", "web"]\n'
    )
    assert (root / "api" / ".ptest.toml").is_file()
    assert (root / "web" / ".ptest.toml").is_file()


def test_init_auto_bootstrap_excludes_terraform_subtrees(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git_root(root)
    api = root / "api"
    web = root / "web"
    terraform = root / "terraform"
    api.mkdir()
    web.mkdir()
    terraform.mkdir()
    (api / "pyproject.toml").write_text(
        '[project]\ndependencies = ["pytest>=8"]\n'
    )
    (web / "package.json").write_text('{"devDependencies":{"vitest":"1"}}')
    (terraform / "pyproject.toml").write_text(
        '[project]\ndependencies = ["pytest>=8"]\n'
    )
    (terraform / "versions.tf").write_text('terraform {}\n')

    result = init_project(root, InitOptions(
        runner=None, dry_run=False, reveal_command=False,
    ))

    assert result.action.value == "created"
    assert (root / ".ptest.toml").read_text() == (
        'version = 2\n\n[monorepo]\nchildren = ["api", "web"]\n'
    )
    assert (api / ".ptest.toml").is_file()
    assert (web / ".ptest.toml").is_file()
    assert not (terraform / ".ptest.toml").exists()


def test_init_preserves_unambiguous_root_runner_over_child_evidence(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git_root(root)
    (root / "pyproject.toml").write_text(
        '[project]\ndependencies = ["pytest>=8"]\n'
    )
    (root / "api").mkdir()
    (root / "web").mkdir()
    (root / "api" / "pyproject.toml").write_text(
        '[project]\ndependencies = ["pytest>=8"]\n'
    )
    (root / "web" / "package.json").write_text('{"devDependencies":{"vitest":"1"}}')

    result = init_project(root, InitOptions(
        runner=None, dry_run=False, reveal_command=False,
    ))

    assert result.action.value == "created"
    assert (root / ".ptest.toml").read_text().startswith("version = 1\n")
    assert not (root / "api" / ".ptest.toml").exists()


@pytest.mark.parametrize("child", ["", ".", "..", "../api", "/api", "api\\web", "api//web"])
def test_explicit_children_reject_unsafe_paths_before_writes(tmp_path, child):
    root = tmp_path / "repo"
    root.mkdir()
    _git_root(root)
    (root / "valid").mkdir()
    before = set(root.iterdir())

    with pytest.raises((Problem, ValueError), match="unsafe|invalid|nonempty"):
        init_project(root, InitOptions(
            runner=None, dry_run=False, reveal_command=False,
            children=((child, RunnerKind.PYTEST), ("valid", RunnerKind.PYTEST)),
        ))

    assert set(root.iterdir()) == before


def test_explicit_children_reject_duplicate_and_overlapping_paths_before_writes(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git_root(root)
    (root / "api" / "nested").mkdir(parents=True)
    before = set(root.iterdir())

    for children in (
        (("api", RunnerKind.PYTEST), ("api", RunnerKind.PYTEST)),
        (("api", RunnerKind.PYTEST), ("api/nested", RunnerKind.PYTEST)),
    ):
        with pytest.raises(Problem, match="unique|overlap"):
            init_project(root, InitOptions(
                runner=None, dry_run=False, reveal_command=False,
                children=children,
            ))
        assert set(root.iterdir()) == before


def test_explicit_children_reject_more_than_runtime_manifest_limit(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git_root(root)
    children = tuple((f"child-{index}", RunnerKind.PYTEST) for index in range(257))

    with pytest.raises(Problem, match="between one and 256"):
        init_project(root, InitOptions(
            runner=None, dry_run=False, reveal_command=False, children=children,
        ))
    assert not (root / ".ptest.toml").exists()


def test_nearest_config_wins_for_nested_roots(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    _write_config(outer, project_id="11" * 16)
    _write_config(inner, project_id="22" * 16)

    resolution = resolve_config(inner)

    assert resolution.root == inner
    assert resolution.path == inner / ".ptest.toml"
    assert resolution.config is not None
    assert resolution.config.project_id == "22" * 16


def test_parent_config_applies_without_git(tmp_path):
    root = tmp_path / "project"
    child = root / "src" / "package"
    child.mkdir(parents=True)
    _write_config(root, project_id="33" * 16)

    resolution = resolve_config(child)

    assert resolution.root == root
    assert resolution.config is not None
    assert resolution.config.project_id == "33" * 16


@pytest.mark.parametrize("operation", ["resolve", "preview", "init"])
@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("legacy_state", ["absent", "malformed", "hostile", "unreadable"])
def test_legacy_targets_are_never_opened_or_parsed(
    tmp_path, monkeypatch, operation, configured, legacy_state,
):
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    if configured:
        _write_config(root, project_id="44" * 16)
    legacy = tmp_path / "legacy"
    redirects = {
        "HOME": legacy / "home",
        "XDG_CONFIG_HOME": legacy / "config",
        "XDG_STATE_HOME": legacy / "state",
        "PTEST_HOME": legacy / "ptest-home",
        "PTEST_BACKEND": legacy / "remote",
    }
    files = [redirects["HOME"] / ".config/ptest/config.toml",
             redirects["XDG_CONFIG_HOME"] / "ptest/config.toml",
             redirects["PTEST_HOME"] / "config.toml"]
    if legacy_state != "absent":
        for path in files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "invalid = [\n" if legacy_state == "malformed" else
                '[projects.takeover]\nroot = "/"\nbackend = "remote"\n'
                'full = "$(touch legacy-executed)"\n'
            )
            if legacy_state == "unreadable":
                path.chmod(0)
    for name, value in redirects.items():
        monkeypatch.setenv(name, str(value))

    # Guard both Python file APIs and descriptor-relative OS opens. A swallowed
    # exception still fails the final assertion, so reading then ignoring a
    # legacy file cannot satisfy this contract.
    opened_dirs = {}
    attempts = []

    def guard(open_file):
        def checked(path, *args, **kwargs):
            candidate = None
            if not isinstance(path, int):
                candidate = Path(os.fsdecode(path))
                if not candidate.is_absolute():
                    candidate = opened_dirs.get(kwargs.get("dir_fd"), Path.cwd()) / candidate
                candidate = Path(os.path.abspath(candidate))
                if candidate.is_relative_to(legacy):
                    attempts.append(candidate)
                    raise AssertionError("legacy target was opened")
            result = open_file(path, *args, **kwargs)
            if isinstance(result, int) and candidate is not None:
                opened_dirs[result] = candidate
            return result
        return checked

    for owner, name in ((builtins, "open"), (io, "open"), (os, "open")):
        monkeypatch.setattr(owner, name, guard(getattr(owner, name)))
    # Prove the guards detect the attempted reads, even for an absent target.
    for open_file in (builtins.open, io.open):
        with pytest.raises(AssertionError, match="legacy target"):
            open_file(files[0], "rb")
    with pytest.raises(AssertionError, match="legacy target"):
        os.open(files[0], os.O_RDONLY)
    attempts.clear()

    if operation == "resolve":
        result = resolve_config(root)
        assert result.root == root
        if configured:
            assert result.problem is None
            assert result.config.project_id == "44" * 16
        else:
            assert result.config is None
            assert result.problem.code == "initialization-required"
    else:
        result = init_project(root, InitOptions(
            runner=None, dry_run=operation == "preview", reveal_command=False,
        ))
        assert result.target == root / ".ptest.toml"
        assert result.config.runner_kind is RunnerKind.PYTEST
        assert result.action.value == (
            "existing" if configured else "preview" if operation == "preview" else "created"
        )
    assert result.warnings == ()
    assert attempts == []


@pytest.mark.parametrize("project_id", ["a" * 31, "a" * 33, "A" * 32,
                                         "z" * 32])
def test_project_id_must_be_lowercase_hex32(tmp_path, project_id):
    _write_config(tmp_path, project_id=project_id)

    resolution = resolve_config(tmp_path)

    assert resolution.config is None
    assert resolution.problem is not None
    assert resolution.problem.code == "invalid-config"
    assert project_id not in resolution.problem.message


@pytest.mark.parametrize(
    "body",
    [
        'version = 1\nproject_id = "' + "ab" * 16 + '"\nversion = 1\n',
        'version = 1\nproject_id = "' + "ab" * 16 + '"\nunknown = true\n',
        'version = "1"\nproject_id = "' + "ab" * 16 + '"\n',
        'version = 1\nproject_id = "' + "ab" * 16 + '"\n[runner]\nkind = "pytest"\nlauncher = ["python"]\ntest_roots = ["../tests"]\n',
    ],
)
def test_invalid_execution_config_fails_closed(tmp_path, body):
    (tmp_path / ".ptest.toml").write_text(body, encoding="utf-8")

    resolution = resolve_config(tmp_path)

    assert resolution.config is None
    assert resolution.problem is not None
    assert resolution.problem.code == "invalid-config"


def test_invalid_selection_disables_only_selection(tmp_path):
    _write_config(
        tmp_path,
        extra='full_ratio = 0.01\n',
    )

    resolution = resolve_config(tmp_path)

    assert resolution.problem is None
    assert resolution.config is not None
    assert resolution.config.runner.kind is RunnerKind.PYTEST
    assert resolution.config.selection.enabled is False
    assert [warning.code for warning in resolution.warnings] == ["policy-invalid"]


def test_invalid_utf8_and_oversized_config_fail_closed(tmp_path):
    path = tmp_path / ".ptest.toml"
    path.write_bytes(b"\xff")
    resolution = resolve_config(tmp_path)
    assert resolution.problem is not None
    assert resolution.problem.code == "invalid-config"

    path.write_bytes(b"x" * (256 * 1024 + 1))
    resolution = resolve_config(tmp_path)
    assert resolution.problem is not None
    assert resolution.problem.code == "invalid-config"


def test_config_symlink_is_unsafe_and_not_followed(tmp_path, monkeypatch):
    import ptest.config as config_module

    outside = tmp_path / "outside.toml"
    outside.write_text("version = 1\n", encoding="utf-8")
    (tmp_path / ".ptest.toml").symlink_to(outside)
    reads = []

    def forbidden_read(*args):
        reads.append(args)
        pytest.fail("symlinked config must not be opened")

    monkeypatch.setattr(config_module, "read_regular", forbidden_read)

    resolution = resolve_config(tmp_path)

    assert resolution.config is None
    assert resolution.problem is not None
    assert resolution.problem.code == "unsafe-path"
    assert reads == []


def test_literal_argv_accepts_metacharacters_but_rejects_controls(tmp_path):
    _write_config(tmp_path)
    path = tmp_path / ".ptest.toml"
    path.write_text(
        'version = 1\nproject_id = "' + "ab" * 16 + '"\n'
        "[runner]\nkind = \"pytest\"\n"
        'launcher = ["python", "$(touch sentinel)", "a b"]\n'
        'test_roots = ["tests"]\n',
        encoding="utf-8",
    )
    resolution = resolve_config(tmp_path)
    assert resolution.config is not None
    assert resolution.config.runner.launcher[1] == "$(touch sentinel)"

    path.write_bytes(
        ('version = 1\nproject_id = "' + "ab" * 16 + '"\n'
         '[runner]\nkind = "pytest"\nlauncher = ["python\\u0001"]\n'
         'test_roots = ["tests"]\n').encode("utf-8")
    )
    resolution = resolve_config(tmp_path)
    assert resolution.problem is not None
    assert resolution.problem.code == "invalid-config"
    assert "python" not in resolution.problem.message


def test_verbatim_design_config_accepts_final_setup_symlink_without_reading_it(tmp_path, monkeypatch):
    path = tmp_path / ".ptest.toml"
    path.write_text(DESIGN_CONFIG)
    before = resolve_config(tmp_path)
    assert before.problem is None
    binary = tmp_path / ".venv/bin/python"
    binary.parent.mkdir(parents=True)
    binary.symlink_to(sys.executable)

    import ptest.config as config_module
    read_regular = config_module.read_regular
    reads = []

    def checked_read(root, relative, limit):
        reads.append(relative)
        assert relative == ".ptest.toml"
        return read_regular(root, relative, limit)

    monkeypatch.setattr(config_module, "read_regular", checked_read)
    result = resolve_config(tmp_path)

    assert result.problem is None
    assert result.warnings == ()
    assert result.config == before.config
    assert reads == [".ptest.toml"]
    assert result.config.project_id == "cd58ec6cf99748ce9f15dfce137f044d"
    assert result.config.runner.launcher == ("uv", "run", "--locked", "--no-sync", "python")
    assert result.config.runner.full_args == ("--cov=sample", "--cov-fail-under=85")
    assert result.config.setup.required_paths == (".venv/bin/python",)
    assert result.config.selection.groups[0].sources == ("src/sample",)
    assert path.read_text() == DESIGN_CONFIG


def test_moved_tree_retains_portable_config_and_nearest_root(tmp_path):
    root = tmp_path / "original"
    child = root / "src/sample"
    child.mkdir(parents=True)
    (root / ".ptest.toml").write_text(DESIGN_CONFIG)
    before = resolve_config(child)
    moved = tmp_path / "moved"
    root.rename(moved)

    result = resolve_config(moved / "src/sample")

    assert result.problem is None
    assert result.root == moved
    assert result.path == moved / ".ptest.toml"
    assert result.config.config_path == result.path
    assert result.config.project_id == before.config.project_id
    assert result.config.runner == before.config.runner
    assert result.config.setup == before.config.setup
    assert result.config.selection == before.config.selection


@pytest.mark.parametrize("version", ["1.0", "true", '"1"', "0", "2"])
def test_version_requires_exact_integer_one(tmp_path, version):
    (tmp_path / ".ptest.toml").write_text(
        DESIGN_CONFIG.replace("version = 1\n", f"version = {version}\n", 1)
    )
    result = resolve_config(tmp_path)
    assert result.config is None
    assert result.problem.code == "invalid-config"


def _write_argv_config(root, field, tokens):
    body = DESIGN_CONFIG
    lines = {
        "launcher": 'launcher = ["uv", "run", "--locked", "--no-sync", "python"]',
        "args": "args = []",
        "full_args": 'full_args = ["--cov=sample", "--cov-fail-under=85"]',
        "argv": 'argv = ["uv", "sync", "--locked"]',
    }
    # A one-byte launcher and empty runner tails make aggregate limits exact.
    for name, original in lines.items():
        values = tokens if name == field else ["x"] if name in {"launcher", "argv"} else []
        body = body.replace(original, f"{name} = {json.dumps(values, ensure_ascii=False)}")
    (root / ".ptest.toml").write_text(body)


@pytest.mark.parametrize("field", ["launcher", "args", "full_args", "argv"])
@pytest.mark.parametrize("length", [4097, 16384, 16385])
def test_argv_token_length_boundary_preserves_literal_tokens(tmp_path, field, length):
    token = "x" * length
    _write_argv_config(tmp_path, field, [token])
    result = resolve_config(tmp_path)
    if length <= 16384:
        assert result.problem is None
        section = result.config.setup if field == "argv" else result.config.runner
        assert getattr(section, field) == (token,)
    else:
        assert result.config is None
        assert result.problem.code == "invalid-config"
        assert token not in result.problem.message


@pytest.mark.parametrize("field", ["launcher", "argv"])
@pytest.mark.parametrize("count", [256, 257])
def test_argv_token_count_boundary(tmp_path, field, count):
    _write_argv_config(tmp_path, field, ["x"] * count)
    result = resolve_config(tmp_path)
    if count == 256:
        assert result.problem is None
    else:
        assert result.config is None
        assert result.problem.code == "invalid-config"


@pytest.mark.parametrize("field", ["launcher", "argv"])
@pytest.mark.parametrize("overflow", [False, True])
def test_argv_total_utf8_byte_boundary(tmp_path, field, overflow):
    # Every token is below the old 4 KiB cap; only the total bytes differ.
    tokens = ["é" * 4096] * 16 + (["x"] if overflow else [])
    assert sum(len(token.encode("utf-8")) for token in tokens) == 131072 + overflow
    _write_argv_config(tmp_path, field, tokens)
    result = resolve_config(tmp_path)
    if overflow:
        assert result.config is None
        assert result.problem.code == "invalid-config"
    else:
        assert result.problem is None


@pytest.mark.parametrize("bound", ["tokens", "bytes"])
@pytest.mark.parametrize("overflow", [False, True])
def test_runner_command_bounds_include_launcher_args_and_full_args(tmp_path, bound, overflow):
    if bound == "tokens":
        args = ["x"] * 254
        full_args = ["x"] * (1 + overflow)
    else:
        args = ["x" * 4096] * 31
        full_args = ["x" * (4095 + overflow)]
    _write_argv_config(tmp_path, "args", args)
    path = tmp_path / ".ptest.toml"
    path.write_text(path.read_text().replace("full_args = []", f"full_args = {json.dumps(full_args)}"))
    result = resolve_config(tmp_path)
    if overflow:
        assert result.config is None
        assert result.problem.code == "invalid-config"
    else:
        assert result.problem is None


@pytest.mark.parametrize("field", ["launcher", "args", "full_args", "argv"])
@pytest.mark.parametrize("value", ['"shell command"', '[1]', '[["nested"]]', '["bad\\u0000token"]'])
def test_hostile_argv_types_and_controls_fail_closed(tmp_path, field, value):
    _write_argv_config(tmp_path, field, ["sentinel"])
    path = tmp_path / ".ptest.toml"
    path.write_text(path.read_text().replace(f'{field} = ["sentinel"]', f"{field} = {value}"))
    result = resolve_config(tmp_path)
    assert result.config is None
    assert result.problem.code == "invalid-config"


@pytest.mark.parametrize("length", [4096, 4097])
def test_ordinary_string_limit_is_not_relaxed_for_non_argv_fields(tmp_path, length):
    # The absent first component avoids OS filename limits; this is the config
    # string bound, independently of filesystem component length restrictions.
    root = "absent/" + "x" * (length - len("absent/"))
    (tmp_path / ".ptest.toml").write_text(
        DESIGN_CONFIG.replace('test_roots = ["tests"]', f"test_roots = {json.dumps([root])}")
    )
    result = resolve_config(tmp_path)
    if length == 4096:
        assert result.problem is None
        assert result.config.runner.test_roots == (root,)
    else:
        assert result.config is None
        assert result.problem.code == "invalid-config"


@pytest.mark.parametrize("field", ["test_roots", "required_paths"])
@pytest.mark.parametrize("path", ["../escape", "/outside", "a//b", "a/./b", "a/../b",
                                  "C:/outside", "a\\b", "~/.config", ""])
def test_execution_paths_reject_unsafe_syntax(tmp_path, field, path):
    original = 'test_roots = ["tests"]' if field == "test_roots" else 'required_paths = [".venv/bin/python"]'
    (tmp_path / ".ptest.toml").write_text(DESIGN_CONFIG.replace(original, f"{field} = {json.dumps([path])}"))
    result = resolve_config(tmp_path)
    assert result.config is None
    assert result.problem.code == "invalid-config"


@pytest.mark.parametrize("component", [".venv", ".venv/bin", "tests"])
def test_setup_ancestor_and_runner_symlinks_remain_unsafe(tmp_path, component):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / component
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    (tmp_path / ".ptest.toml").write_text(DESIGN_CONFIG)
    result = resolve_config(tmp_path)
    assert result.config is None
    assert result.problem.code == "unsafe-path"


@pytest.mark.parametrize("policy", [
    'enabled = "yes"',
    'input_roots = "src"',
    'input_roots = [1]',
    'input_roots = [["src"]]',
    'input_roots = ["../outside"]',
    'input_roots = ["bad\\u0001path"]',
    'input_roots = ["' + "x" * 4097 + '"]',
    'environment = ["BAD=VALUE"]',
    'full_ratio = nan',
    'full_ratio = inf',
    'full_ratio = true',
    'unknown = true',
    'groups = ["not a table"]',
    'groups = [{name = "x", sources = [], tests = ["tests"]}]',
    'groups = [{name = "../x", sources = ["src"], tests = ["tests"]}]',
    'groups = [{name = "x", sources = ["src"], tests = ["tests"], unknown = true}]',
    'groups = [{name = "same", sources = ["src/a"], tests = ["tests/a"]}, '
    '{name = "same", sources = ["src/b"], tests = ["tests/b"]}]',
], ids=["boolean", "scalar-list", "list-type", "nested-list", "traversal", "control",
        "oversized-string", "environment", "nan", "infinity", "boolean-ratio", "unknown",
        "group-type", "empty-group", "unsafe-name", "group-unknown", "duplicate-groups"])
def test_hostile_selection_falls_back_without_retaining_partial_policy(tmp_path, policy):
    execution = DESIGN_CONFIG.split("[selection]")[0]
    (tmp_path / ".ptest.toml").write_text(execution + "[selection]\n" + policy + "\n")
    result = resolve_config(tmp_path)
    assert result.problem is None
    assert result.config.runner.kind is RunnerKind.PYTEST
    assert result.config.runner.full_args == ("--cov=sample", "--cov-fail-under=85")
    assert result.config.selection == SelectionPolicy(enabled=False, closed_inputs=False)
    assert [warning.code for warning in result.warnings] == ["policy-invalid"]


@pytest.mark.parametrize("bound", ["groups", "paths", "combined-paths"])
@pytest.mark.parametrize("overflow", [False, True])
def test_selection_group_and_path_count_bounds(tmp_path, bound, overflow):
    if bound == "groups":
        groups = [f'{{name = "g{i}", sources = ["src"], tests = ["tests"]}}'
                  for i in range(256 + overflow)]
        policy = "groups = [" + ",".join(groups) + "]"
    elif bound == "paths":
        policy = f'input_roots = {json.dumps(["src"] * (4096 + overflow))}'
    else:
        policy = (f'input_roots = {json.dumps(["src"] * (4094 + overflow))}\n'
                  'groups = [{name = "g", sources = ["src"], tests = ["tests"]}]')
    _write_config(tmp_path, extra=policy)
    result = resolve_config(tmp_path)
    assert result.problem is None
    assert [warning.code for warning in result.warnings] == (["policy-invalid"] if overflow else [])


def test_selection_symlink_disables_selection(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "src").symlink_to(outside, target_is_directory=True)
    (tmp_path / ".ptest.toml").write_text(DESIGN_CONFIG)
    result = resolve_config(tmp_path)
    assert result.problem is None
    assert result.config.selection == SelectionPolicy(enabled=False, closed_inputs=False)
    assert [warning.code for warning in result.warnings] == ["policy-invalid"]


def _fresh_pytest_args(root, target_name=".ptest.toml"):
    from ptest.config import _fresh_config
    config = _fresh_config(root, root / target_name, RunnerKind.PYTEST)
    return config.runner.args


@pytest.mark.parametrize("filename,content", [
    ("pyproject.toml", '[tool.pytest.ini_options]\naddopts = "--maxprocesses=4"\n'),
    ("pyproject.toml", '[tool.pytest.ini_options]\naddopts = "-n 4 --dist=each"\n'),
    ("pytest.ini", "[pytest]\naddopts = --dist=each\n"),
])
def test_fresh_pytest_config_disables_config_level_fallback_serially(
        tmp_path, filename, content):
    """Init writes -n 0 only for config-level tier fallbacks."""
    (tmp_path / filename).write_text(content, encoding="utf-8")

    assert _fresh_pytest_args(tmp_path) == ("-n", "0")


@pytest.mark.parametrize("filename,content", [
    ("pyproject.toml",
     '[tool.pytest.ini_options]\naddopts = \'-n 4 --dist=loadgroup -m "not slow"\'\n'),
    ("pyproject.toml",
     '[tool.pytest.ini_options]\naddopts = ["-n", "4", "--dist=loadgroup"]\n'),
    ("pytest.ini", "[pytest]\naddopts = -n 4\n"),
    ("tox.ini", "[pytest]\naddopts = --numprocesses=auto\n"),
    ("setup.cfg", "[tool:pytest]\naddopts = -p xdist\n"),
    ("pyproject.toml", '[tool.pytest]\naddopts = "--dist=load"\n'),
    ("pyproject.toml", '[tool.pytest.ini_options]\naddopts = \'-m "not slow"\'\n'),
    ("pyproject.toml", '[tool.pytest.ini_options]\naddopts = "-n 4 -p no:xdist"\n'),
    ("pytest.ini", "[pytest]\naddopts = -q\n"),
    ("pyproject.toml", "[project]\ndependencies = []\n"),
    ("pyproject.toml", '[tool.pytest.ini_options]\naddopts = "-n 4 --cov"\n'),
])
def test_fresh_pytest_config_without_config_level_fallback_keeps_empty_args(
        tmp_path, filename, content):
    """Environment fallbacks (missing/unqualified xdist, missing/unfrozen coverage) are reported, never written."""
    (tmp_path / filename).write_text(content, encoding="utf-8")

    assert _fresh_pytest_args(tmp_path) == ()


def test_fresh_pytest_serial_config_serializes_only_args_line(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "-n 4 --dist=each"\n', encoding="utf-8")
    result = init_project(tmp_path, InitOptions(
        runner=RunnerKind.PYTEST, dry_run=False, reveal_command=False))

    assert result.action.value == "created"
    body = (tmp_path / ".ptest.toml").read_text(encoding="utf-8")
    assert 'args = ["-n", "0"]' in body
    assert "full_args = []" in body


# ---- pytest test roots: what a full run must cover ---------------------------

def _touch(path: Path, text: str = "def test_x():\n    pass\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_pytest_roots_cover_every_top_level_test_directory(tmp_path):
    """fullon2 integrations: tests/ + launchpad/ + web/, no testpaths."""
    from ptest.config import pytest_test_roots
    _touch(tmp_path / "tests" / "test_a.py")
    _touch(tmp_path / "launchpad" / "test_b.py")
    _touch(tmp_path / "web" / "auth" / "test_c.py")
    _touch(tmp_path / "src" / "pkg" / "mod.py", "x = 1\n")
    _touch(tmp_path / ".venv" / "lib" / "test_vendored.py")
    _touch(tmp_path / "node_modules" / "x" / "test_js.py")

    assert pytest_test_roots(tmp_path) == ("launchpad", "tests", "web")


@pytest.mark.parametrize("name,body", [
    ("pytest.ini", "[pytest]\ntestpaths = tests integration\n"),
    ("pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests", "integration"]\n'),
    ("tox.ini", "[pytest]\ntestpaths =\n    tests\n    integration\n"),
    ("setup.cfg", "[tool:pytest]\ntestpaths = tests integration\n"),
])
def test_pytest_roots_follow_declared_testpaths(tmp_path, name, body):
    from ptest.config import pytest_test_roots
    (tmp_path / name).write_text(body, encoding="utf-8")
    _touch(tmp_path / "tests" / "test_a.py")
    _touch(tmp_path / "integration" / "test_b.py")
    _touch(tmp_path / "scripts" / "test_not_collected.py")

    assert pytest_test_roots(tmp_path) == ("tests", "integration")


def test_pytest_roots_ignore_glob_testpaths_and_scan(tmp_path):
    from ptest.config import pytest_test_roots
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = pkg*/tests\n", encoding="utf-8")
    _touch(tmp_path / "pkga" / "tests" / "test_a.py")

    assert pytest_test_roots(tmp_path) == ("pkga",)


def test_pytest_roots_root_level_test_file_is_dot(tmp_path):
    from ptest.config import pytest_test_roots
    _touch(tmp_path / "test_top.py")
    _touch(tmp_path / "tests" / "test_a.py")

    assert pytest_test_roots(tmp_path) == (".",)


def test_pytest_roots_fallback_without_test_files(tmp_path):
    from ptest.config import pytest_test_roots
    (tmp_path / "tests").mkdir()
    assert pytest_test_roots(tmp_path) == ("tests",)
    assert pytest_test_roots(tmp_path / "tests") == (".",)


def test_pytest_roots_accept_declared_test_files(tmp_path):
    """persea api: testpaths lists a file next to directories."""
    from ptest.config import pytest_test_roots
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\n'
        'testpaths = ["tests/test_m10.py", "tests/db", "tests/unit"]\n', encoding="utf-8")
    _touch(tmp_path / "tests" / "test_m10.py")
    _touch(tmp_path / "tests" / "db" / "test_a.py")
    _touch(tmp_path / "tests" / "unit" / "test_b.py")
    _touch(tmp_path / "scripts" / "test_manual.py")
    _touch(tmp_path / "packages" / "mcp" / "tests" / "test_c.py")

    assert pytest_test_roots(tmp_path) == ("tests/test_m10.py", "tests/db", "tests/unit")


def test_pytest_roots_skip_benchmark_and_docs_folders(tmp_path):
    """attrs: bench/test_benchmarks.py needs pytest-benchmark and is not the suite."""
    from ptest.config import pytest_test_roots
    _touch(tmp_path / "tests" / "test_a.py")
    _touch(tmp_path / "bench" / "test_benchmarks.py")
    _touch(tmp_path / "docs" / "test_doctest_helpers.py")

    assert pytest_test_roots(tmp_path) == ("tests",)


def test_pytest_roots_keep_non_suite_folder_when_it_is_all_there_is(tmp_path):
    from ptest.config import pytest_test_roots
    _touch(tmp_path / "benchmarks" / "test_speed.py")

    assert pytest_test_roots(tmp_path) == ("benchmarks",)


# ---- projects without a uv.lock: setup builds a checkout .venv ---------------

@pytest.mark.parametrize("files,expected", [
    # httpx: requirements.txt names pytest and installs the project itself.
    ({"pyproject.toml": '[project]\nname = "x"\n',
      "requirements.txt": "-e .[http2]\npytest==8.4.1\n"},
     ("-r", "requirements.txt")),
    # click/attrs style: a PEP 735 group holds pytest.
    ({"pyproject.toml": '[project]\nname = "x"\n[dependency-groups]\n'
                        'tests = ["pytest", "pytest-xdist"]\n'},
     ("-e", ".", "--group", "tests", "pytest")),
    # a PEP 621 extra holds pytest.
    ({"pyproject.toml": '[project]\nname = "x"\n[project.optional-dependencies]\n'
                        'test = ["pytest>=8"]\n'},
     ("-e .[test]", "pytest")),
    # rich: Poetry dev dependencies.
    ({"pyproject.toml": '[tool.poetry]\nname = "x"\n[tool.poetry.dev-dependencies]\n'
                        'pytest = "^7"\nattrs = "*"\n'},
     ("-e", ".", "pytest", "attrs")),
    # setup.py only.
    ({"setup.py": "from setuptools import setup\nsetup()\n"}, ("-e", ".", "pytest")),
])
def test_pytest_install_spec_follows_where_projects_declare_tests(tmp_path, files, expected):
    from ptest.config import pytest_install_spec
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")

    assert pytest_install_spec(tmp_path)[:len(expected)] == expected
    assert pytest_install_spec(tmp_path).count("pytest") <= 1


def test_fresh_pytest_config_without_lock_builds_a_venv(tmp_path):
    from ptest.config import _fresh_config
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    _touch(tmp_path / "tests" / "test_a.py")

    config = _fresh_config(tmp_path, tmp_path / ".ptest.toml", RunnerKind.PYTEST)

    assert config.runner.launcher == (".venv/bin/python",)
    assert config.setup.argv[:2] == ("sh", "-c")
    assert "uv venv -q --allow-existing .venv" in config.setup.argv[2]
    assert config.setup.required_paths == (".venv/bin/python",)
    assert config.selection.non_input_outputs == (".venv",)


@pytest.mark.parametrize("pyproject", [None, "[tool.pytest.ini_options]\naddopts = \"-q\"\n"])
def test_fresh_pytest_config_for_bare_tests_keeps_ambient_python(tmp_path, pyproject):
    """Tool settings alone declare nothing to install."""
    from ptest.config import _fresh_config
    if pyproject is not None:
        (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    _touch(tmp_path / "tests" / "test_a.py")

    config = _fresh_config(tmp_path, tmp_path / ".ptest.toml", RunnerKind.PYTEST)

    assert config.runner.launcher == ("python",)
    assert config.setup is None


@pytest.mark.parametrize("files,expected", [
    (("pnpm-lock.yaml",), ("pnpm", "install", "--frozen-lockfile")),
    (("yarn.lock",), ("yarn", "install", "--frozen-lockfile")),
    (("yarn.lock", ".yarnrc.yml"), ("yarn", "install", "--immutable")),
    (("bun.lock",), ("bun", "install", "--frozen-lockfile")),
    (("package-lock.json",), ("npm", "ci")),
    ((), None),
])
def test_node_install_follows_the_lockfile(tmp_path, files, expected):
    """ufo ships pnpm-lock.yaml; init used to suggest npm ci."""
    from ptest.config import node_install_argv
    for name in files:
        (tmp_path / name).write_text("", encoding="utf-8")

    assert node_install_argv(tmp_path) == expected


def test_setup_display_shows_the_script_of_sh_c():
    from ptest.contracts import display_setup
    assert display_setup(("sh", "-c", "uv venv && uv pip install pytest")) == \
        "uv venv && uv pip install pytest"
    assert display_setup(("uv", "sync", "--locked")) == "uv sync --locked"


def test_pytest_roots_skip_folders_the_project_ignores_in_addopts(tmp_path):
    """fastapi: addopts --ignore=docs_src keeps its tutorial code out."""
    from ptest.config import pytest_test_roots
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = ["--strict-config", "--ignore=docs_src"]\n',
        encoding="utf-8")
    _touch(tmp_path / "tests" / "test_a.py")
    _touch(tmp_path / "docs_src" / "app" / "test_main.py")

    assert pytest_test_roots(tmp_path) == ("tests",)


def test_workspace_root_with_one_runner_child_becomes_a_monorepo(tmp_path):
    """full-stack-fastapi-template: a uv workspace root, pytest in backend/."""
    root = tmp_path / "repo"
    root.mkdir()
    _git_root(root)
    (root / "pyproject.toml").write_text(
        '[tool.uv.workspace]\nmembers = ["backend"]\n', encoding="utf-8")
    backend = root / "backend"
    backend.mkdir()
    (backend / "pyproject.toml").write_text(
        '[project]\nname = "app"\n[dependency-groups]\ndev = ["pytest"]\n', encoding="utf-8")
    _touch(backend / "tests" / "test_a.py")
    (root / "frontend").mkdir()

    init_project(root, InitOptions(runner=None, dry_run=False, reveal_command=False))

    manifest = (root / ".ptest.toml").read_text(encoding="utf-8")
    assert 'children = ["backend"]' in manifest
    assert (backend / ".ptest.toml").exists()


def test_init_without_any_runner_says_what_to_do(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git_root(root)
    (root / "README.md").write_text("docs only\n", encoding="utf-8")

    with pytest.raises(Problem, match="no pytest, vitest, go or cargo project found"):
        init_project(root, InitOptions(runner=None, dry_run=False, reveal_command=False))


# --- config-uncommitted (linked worktree without committed config) ---

from ptest import config as config_api
from support import git, init_git_repo, write_ptest_toml


def _linked_worktree(tmp_path, *, commit_config=False, **toml):
    """main checkout (committed README) + linked worktree at tmp_path/wt."""
    main = init_git_repo(tmp_path / "main", files={"README.md": "x\n"})
    write_ptest_toml(main, **toml)
    if commit_config:
        git(main, "add", ".ptest.toml")
        git(main, "commit", "-q", "-m", "config")
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "wt", str(wt))
    return main, wt


def test_resolve_config_in_linked_worktree_without_config_is_uncommitted(tmp_path):
    main, wt = _linked_worktree(tmp_path)

    resolution = resolve_config(wt)

    assert resolution.config is None
    assert resolution.monorepo is None
    assert resolution.path is None
    assert resolution.problem is not None
    assert resolution.problem.code == "config-uncommitted"
    assert resolution.problem.phase == "config"
    assert resolution.problem.retryable is False
    assert resolution.problem.message == (
        "this is a linked git worktree without .ptest.toml; "
        f"the main checkout has {main}/.ptest.toml. "
        "Worktrees only receive committed files. "
        "Ask the user to commit .ptest.toml on the base branch, or, "
        "if it is already committed there, to update this branch from it. "
        "Do not run ptest init here."
    )


def test_resolve_config_branch_predates_committed_config_mentions_update(tmp_path):
    """A worktree branched before main committed the config names the update fix."""
    from support import git, init_git_repo, write_ptest_toml

    main = init_git_repo(tmp_path / "main", files={"README.md": "x\n"})
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "wt", str(wt))
    # The base branch commits .ptest.toml only AFTER the worktree branched.
    write_ptest_toml(main)
    git(main, "add", ".ptest.toml")
    git(main, "commit", "-q", "-m", "config")

    resolution = resolve_config(wt)

    assert resolution.problem is not None
    assert resolution.problem.code == "config-uncommitted"
    assert "update this branch" in resolution.problem.message


def test_resolve_config_never_reads_main_content(tmp_path):
    main = init_git_repo(tmp_path / "main", files={"README.md": "x\n"})
    (main / ".ptest.toml").write_text("version = 999\n[broken\n", encoding="utf-8")
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "wt", str(wt))

    resolution = resolve_config(wt)

    assert resolution.problem is not None
    assert resolution.problem.code == "config-uncommitted"


def test_resolve_config_nested_relative_path_names_subdir(tmp_path):
    main = init_git_repo(tmp_path / "main", files={"README.md": "x\n"})
    api = main / "api"
    api.mkdir()
    write_ptest_toml(api)
    (main / "api" / "x").mkdir()
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "wt", str(wt))
    (wt / "api" / "x").mkdir(parents=True)

    resolution = resolve_config(wt / "api" / "x")

    assert resolution.problem is not None
    assert resolution.problem.code == "config-uncommitted"
    assert "without api/.ptest.toml" in resolution.problem.message
    assert f"the main checkout has {main}/api/.ptest.toml" in resolution.problem.message


def test_resolve_config_unchanged_outside_linked_worktrees(tmp_path):
    main, wt = _linked_worktree(tmp_path, commit_config=True)

    committed = resolve_config(wt)
    assert committed.problem is None
    assert committed.config is not None

    bare_src = init_git_repo(tmp_path / "src", files={"README.md": "x\n"})
    git(tmp_path, "clone", "-q", "--bare", str(bare_src), "m.git")
    bare_wt = tmp_path / "bare-wt"
    git(tmp_path / "m.git", "worktree", "add", "--detach", str(bare_wt))
    bare_resolution = resolve_config(bare_wt)
    assert bare_resolution.problem is not None
    assert bare_resolution.problem.code == "initialization-required"

    plain = tmp_path / "plain"
    plain.mkdir()
    plain_resolution = resolve_config(plain)
    assert plain_resolution.problem is not None
    assert plain_resolution.problem.code == "initialization-required"


def test_resolve_config_worktree_where_main_has_no_config(tmp_path):
    main = init_git_repo(tmp_path / "main", files={"README.md": "x\n"})
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "wt", str(wt))

    resolution = resolve_config(wt)

    assert resolution.problem is not None
    assert resolution.problem.code == "initialization-required"


def test_git_root_and_config_uncommitted_never_raise(tmp_path):
    assert config_api.git_root(tmp_path / "missing") is None
    assert config_api.config_uncommitted(tmp_path / "missing") is None
    assert config_api.git_root("relative/path") is None
    assert config_api.config_uncommitted("relative/path") is None

    link = tmp_path / "link"
    target = tmp_path / "target"
    target.mkdir()
    link.symlink_to(target, target_is_directory=True)
    assert config_api.git_root(link) is None
    assert config_api.config_uncommitted(link) is None

    main, wt = _linked_worktree(tmp_path / "ok")
    assert config_api.git_root(wt) == wt
    assert config_api.git_root(main) == main
    assert config_api.config_uncommitted(main) is None


# --- post-test-stall: [runner] stall_timeout --------------------------------

def _write_stall_runner_config(root: Path, runner_extra: str = "") -> Path:
    path = root / ".ptest.toml"
    path.write_text(
        "version = 1\n"
        'project_id = "ab1234567890ab1234567890ab123456"\n'
        "\n[runner]\n"
        'kind = "command"\n'
        'launcher = ["echo"]\n'
        'args = ["hello"]\n'
        + runner_extra +
        "\n[resources]\n"
        "locks = []\n"
        "memory_mb_per_worker = 0\n"
        'probe_isolation = "undeclared"\n'
        "\n[selection]\n"
        "enabled = false\n"
        "closed_inputs = false\n"
        "input_roots = []\n"
        "ignored_inputs = []\n"
        "environment = []\n"
        "full_triggers = []\n"
        "always = []\n"
        "no_tests = []\n"
        "non_input_outputs = []\n"
        "full_ratio = 0.7\n",
        encoding="utf-8",
    )
    return path


def _stall_config(**runner_overrides):
    runner_fields = {
        "kind": C.RunnerKind.COMMAND,
        "launcher": ("echo",),
        "args": ("hello",),
        "full_args": (),
        "test_roots": (),
        "workers": 1,
        "lifecycle": "cooperative-process-group",
    }
    runner_fields.update(runner_overrides)
    return C.Config(
        runner=C.RunnerConfig(**runner_fields),
        setup=None,
        resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(enabled=False, closed_inputs=False),
        project_id="ab" * 16,
    )


def test_config_without_stall_timeout_leaves_it_unset(tmp_path):
    _write_stall_runner_config(tmp_path)
    resolution = resolve_config(tmp_path)
    assert resolution.problem is None
    assert resolution.config is not None
    assert resolution.config.runner.stall_timeout_s is None


@pytest.mark.parametrize("value", ["0", "10", "10.5", "120", "86400"])
def test_config_parses_valid_stall_timeout(tmp_path, value):
    _write_stall_runner_config(tmp_path, f"stall_timeout = {value}\n")
    resolution = resolve_config(tmp_path)
    assert resolution.problem is None
    assert resolution.config is not None
    assert resolution.config.runner.stall_timeout_s == float(value)


@pytest.mark.parametrize("value", [
    "-1", "5", "9.99", '"120"', "1e9", "true", "nan", "inf", "86400.5",
])
def test_config_rejects_invalid_stall_timeout(tmp_path, value):
    _write_stall_runner_config(tmp_path, f"stall_timeout = {value}\n")
    resolution = resolve_config(tmp_path)
    assert resolution.config is None
    assert resolution.problem is not None
    assert resolution.problem.code == "invalid-config"


def test_config_renders_stall_timeout_after_full_timeout_only_when_set():
    without = _serialize_fresh(_stall_config()).decode("utf-8")
    assert "stall_timeout" not in without
    rendered = _serialize_fresh(
        _stall_config(timeout_s=120, full_timeout_s=300,
                      stall_timeout_s=60)).decode("utf-8")
    assert "timeout = 120\nfull_timeout = 300\nstall_timeout = 60\n" in rendered
    rendered_zero = _serialize_fresh(
        _stall_config(stall_timeout_s=0)).decode("utf-8")
    assert "stall_timeout = 0\n" in rendered_zero


@pytest.mark.parametrize("value", [None, 0, 10.5, 120, 86400])
def test_config_stall_timeout_render_round_trips(tmp_path, value):
    kwargs = {} if value is None else {"stall_timeout_s": value}
    text = _serialize_fresh(_stall_config(**kwargs)).decode("utf-8")
    _write_stall_runner_config(tmp_path, "".join(
        line + "\n" for line in text.splitlines()
        if line.startswith("stall_timeout")))
    resolution = resolve_config(tmp_path)
    assert resolution.problem is None
    assert resolution.config is not None
    expected = None if value is None else float(value)
    assert resolution.config.runner.stall_timeout_s == expected


# --- dynamic selection: [selection] dynamic -----------------------------------

def _write_dynamic_config(root: Path, extra: str = "") -> Path:
    path = root / ".ptest.toml"
    path.write_text(
        "version = 1\n"
        'project_id = "ab1234567890ab1234567890ab123456"\n'
        "\n[runner]\n"
        'kind = "pytest"\n'
        'launcher = ["python"]\n'
        'test_roots = ["tests"]\n'
        "\n[resources]\n"
        "\n[selection]\n"
        "enabled = true\n"
        f"{extra}",
        encoding="utf-8",
    )
    return path


def test_selection_dynamic_defaults_true(tmp_path):
    _write_dynamic_config(tmp_path)
    resolution = resolve_config(tmp_path)
    assert resolution.problem is None
    assert resolution.config is not None
    assert resolution.config.selection.dynamic is True


@pytest.mark.parametrize("value, expected", [("true", True), ("false", False)])
def test_selection_dynamic_parses_bool(tmp_path, value, expected):
    _write_dynamic_config(tmp_path, f"dynamic = {value}\n")
    resolution = resolve_config(tmp_path)
    assert resolution.problem is None
    assert resolution.config is not None
    assert resolution.config.selection.dynamic is expected


@pytest.mark.parametrize("value", [
    "1", "0", "1.0", '"true"', '"false"', "[true]", "{a = 1}",
])
def test_selection_dynamic_non_bool_disables_selection(tmp_path, value):
    # Like every other [selection] type error (e.g. full_ratio = true):
    # fail closed with a policy-invalid warning, never invalid-config.
    _write_dynamic_config(tmp_path, f"dynamic = {value}\n")
    resolution = resolve_config(tmp_path)
    assert resolution.problem is None
    assert resolution.config is not None
    assert resolution.config.selection.enabled is False
    assert [warning.code for warning in resolution.warnings] == [
        "policy-invalid"]


def test_selection_dynamic_unknown_key_disables_selection(tmp_path):
    _write_dynamic_config(tmp_path, "dynamik = true\n")
    resolution = resolve_config(tmp_path)
    assert resolution.problem is None
    assert resolution.config is not None
    assert resolution.config.selection.enabled is False
    assert [warning.code for warning in resolution.warnings] == [
        "policy-invalid"]


def _dynamic_config(**overrides):
    fields = {
        "kind": C.RunnerKind.PYTEST,
        "launcher": ("python",),
        "args": (),
        "full_args": (),
        "test_roots": ("tests",),
        "workers": 1,
        "lifecycle": "cooperative-process-group",
    }
    return C.Config(
        runner=C.RunnerConfig(**fields),
        setup=None,
        resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(enabled=True, closed_inputs=False,
                                    **overrides),
        project_id="ab" * 16,
    )


def test_selection_dynamic_renders_only_when_false():
    rendered = _serialize_fresh(_dynamic_config()).decode("utf-8")
    assert "dynamic" not in rendered
    assert "full_ratio = 0.7\n" in rendered
    assert "dynamic = false\n" not in rendered
    rendered_false = _serialize_fresh(
        _dynamic_config(dynamic=False)).decode("utf-8")
    assert "full_ratio = 0.7\ndynamic = false\n" in rendered_false


def test_selection_dynamic_render_round_trips(tmp_path):
    text = _serialize_fresh(_dynamic_config(dynamic=False)).decode("utf-8")
    _write_dynamic_config(tmp_path, "".join(
        line + "\n" for line in text.splitlines()
        if line.startswith("dynamic")))
    resolution = resolve_config(tmp_path)
    assert resolution.problem is None
    assert resolution.config is not None
    assert resolution.config.selection.dynamic is False


def test_selection_dynamic_leaves_policy_digest_unchanged():
    import hashlib
    for policy in (C.SelectionPolicy(enabled=True, closed_inputs=False),
                   C.SelectionPolicy(enabled=True, closed_inputs=False,
                                     dynamic=False),
                   C.SelectionPolicy(enabled=True, closed_inputs=False,
                                     dynamic=True)):
        assert hashlib.sha256(repr(policy).encode()).hexdigest().startswith(
            "43bc98485c25")
