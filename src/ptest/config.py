"""Fresh, repository-local NG configuration and static initialization.

This module intentionally has no migration surface.  The only project input is
the nearest ``.ptest.toml``; initialization inspects a small allowlist of
native manifests without importing or executing repository code.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
import tomllib
from pathlib import Path

from . import contracts as C
from .files import create_exclusive, read_regular

_PHASE = "config"
_CONFIG_NAME = ".ptest.toml"
_CONFIG_MAX_BYTES = 256 * 1024
_NATIVE_MAX_BYTES = 256 * 1024
_MAX_GROUPS = 256
_MAX_PATH_ENTRIES = 4096
_MAX_LIST_ENTRIES = 4096
_MAX_STRING_LENGTH = 4096
_CONTROL_CHARS = frozenset(
    chr(code) for code in range(0x20)
) | frozenset({chr(0x7F)})
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")


class _ConfigInvalid(Exception):
    pass


def _problem(code: str, message: str) -> C.Problem:
    return C.Problem(code=code, message=message, phase=_PHASE,
                     retryable=False)


def _fail(message: str = "project configuration is invalid") -> None:
    raise _ConfigInvalid(message)


def _absolute_directory(value: Path | str) -> Path:
    """Return an existing absolute directory after a no-symlink walk."""
    path = Path(value)
    if not path.is_absolute():
        path = Path(os.getcwd()) / path
    path = Path(os.path.normpath(str(path)))
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        try:
            stamp = os.lstat(cursor)
        except FileNotFoundError:
            raise _problem("state-unavailable", "working directory is unavailable")
        except OSError:
            raise _problem("state-unavailable", "working directory is unavailable")
        if stat.S_ISLNK(stamp.st_mode):
            raise _problem("unsafe-path", "working directory crosses a symlink")
    try:
        stamp = os.lstat(path)
    except OSError:
        raise _problem("state-unavailable", "working directory is unavailable")
    if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
        raise _problem("unsafe-path", "working directory is not a directory")
    return path


def _git_boundary(cwd: Path) -> Path | None:
    """Find a static Git worktree marker without invoking Git."""
    current = cwd
    while True:
        marker = current / ".git"
        try:
            stamp = os.lstat(marker)
        except FileNotFoundError:
            stamp = None
        except OSError:
            raise _problem("state-unavailable", "project boundary is unavailable")
        if stamp is not None:
            if stat.S_ISLNK(stamp.st_mode):
                raise _problem("unsafe-path", "project boundary crosses a symlink")
            if stat.S_ISDIR(stamp.st_mode):
                # A real worktree marker has these two regular entries.  Do
                # not mistake an unrelated empty ``.git`` directory (common
                # in temporary fixtures) for a project boundary.
                try:
                    head = os.lstat(marker / "HEAD")
                    config = os.lstat(marker / "config")
                except FileNotFoundError:
                    stamp = None
                except OSError:
                    raise _problem("state-unavailable", "project boundary is unavailable")
                else:
                    if (stat.S_ISLNK(head.st_mode) or stat.S_ISLNK(config.st_mode)
                            or not stat.S_ISREG(head.st_mode)
                            or not stat.S_ISREG(config.st_mode)):
                        raise _problem("unsafe-path", "project boundary is unsafe")
                    return current
            elif stat.S_ISREG(stamp.st_mode):
                return current
            else:
                raise _problem("unsafe-path", "project boundary is unsafe")
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _candidate_config(root: Path) -> tuple[Path, bool]:
    target = root / _CONFIG_NAME
    try:
        stamp = os.lstat(target)
    except FileNotFoundError:
        return target, False
    except OSError:
        raise _problem("state-unavailable", "project configuration is unavailable")
    if stat.S_ISLNK(stamp.st_mode):
        raise _problem("unsafe-path", "project configuration path is unsafe")
    if not stat.S_ISREG(stamp.st_mode):
        raise _problem("unsafe-path", "project configuration is not a file")
    return target, True


def _find_config(cwd: Path) -> tuple[Path, Path | None, bytes | None, C.Problem | None]:
    """Find the nearest config, bounded by a static Git worktree marker."""
    boundary = _git_boundary(cwd)
    current = cwd
    while True:
        try:
            target, exists = _candidate_config(current)
        except C.Problem as problem:
            return current, current / _CONFIG_NAME, None, problem
        if exists:
            try:
                raw = read_regular(current, _CONFIG_NAME, _CONFIG_MAX_BYTES + 1)
            except C.Problem as problem:
                safe = problem.code if problem.code in {
                    "unsafe-path", "state-unavailable", "invalid-bound",
                } else "state-unavailable"
                return current, target, None, _problem(
                    safe, "project configuration is unavailable")
            if len(raw) > _CONFIG_MAX_BYTES:
                return current, target, raw[:0], _problem(
                    "invalid-config", "project configuration exceeds its bound")
            return current, target, raw, None
        if boundary is not None and current == boundary:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return cwd, None, None, _problem(
        "initialization-required", "project configuration is required")


def _check_string(value: object, *, allow_empty: bool = False,
                  max_len: int = _MAX_STRING_LENGTH) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        _fail()
    if len(value) > max_len or any(char in _CONTROL_CHARS for char in value):
        _fail()
    return value


def _check_tree_strings(value: object) -> None:
    if isinstance(value, str):
        _check_string(value, allow_empty=True)
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_string(key)
            _check_tree_strings(item)
    elif isinstance(value, list):
        for item in value:
            _check_tree_strings(item)


def _table(value: object) -> dict:
    if not isinstance(value, dict):
        _fail()
    return value


def _keys(value: dict, allowed: set[str], *, required: set[str] = set()) -> None:
    if set(value) - allowed or required - set(value):
        _fail()


def _sequence(value: object, *, allow_empty: bool = True,
              max_entries: int = _MAX_LIST_ENTRIES) -> tuple:
    if not isinstance(value, list) or len(value) > max_entries:
        _fail()
    if not allow_empty and not value:
        _fail()
    return tuple(value)


def _string_sequence(value: object, *, allow_empty: bool = True,
                     max_entries: int = _MAX_LIST_ENTRIES,
                     max_len: int = _MAX_STRING_LENGTH) -> tuple[str, ...]:
    items = _sequence(value, allow_empty=allow_empty, max_entries=max_entries)
    return tuple(_check_string(item, allow_empty=True, max_len=max_len)
                 for item in items)


def _path(value: object, root: Path) -> str:
    text = _check_string(value, max_len=_MAX_STRING_LENGTH)
    if (text.startswith("/") or "\\" in text or text.startswith("~")
            or re.match(r"^[A-Za-z]:", text)
            or text == ".."):
        _fail()
    if text == ".":
        return text
    parts = text.split("/")
    if not parts or any(not part or part in {".", ".."} for part in parts):
        _fail()
    cursor = root
    for part in parts:
        cursor = cursor / part
        try:
            stamp = os.lstat(cursor)
        except FileNotFoundError:
            break
        except OSError:
            raise _problem("state-unavailable", "project path is unavailable")
        if stat.S_ISLNK(stamp.st_mode):
            raise _problem("unsafe-path", "project path crosses a symlink")
    return text


def _path_sequence(value: object, root: Path, *, allow_empty: bool = True) -> tuple[str, ...]:
    items = _sequence(value, allow_empty=allow_empty)
    if len(items) > _MAX_PATH_ENTRIES:
        _fail()
    return tuple(_path(item, root) for item in items)


def _argv(value: object, *, allow_empty: bool) -> tuple[str, ...]:
    items = _string_sequence(value, allow_empty=allow_empty, max_entries=256)
    total = 0
    for item in items:
        if "\x00" in item:
            _fail()
        total += len(item.encode("utf-8"))
        if len(item) > 16384:
            _fail()
    if total > 131072:
        _fail()
    return items


def _runner(data: object, root: Path) -> C.RunnerConfig:
    table = _table(data)
    _keys(table, {"kind", "launcher", "args", "full_args", "test_roots",
                  "workers", "lifecycle"}, required={"kind", "launcher"})
    kind_value = table["kind"]
    if not isinstance(kind_value, str):
        _fail()
    try:
        kind = C.RunnerKind(kind_value)
    except ValueError:
        _fail()
    launcher = _argv(table["launcher"], allow_empty=False)
    args = _argv(table.get("args", []), allow_empty=True)
    full_args = _argv(table.get("full_args", []), allow_empty=True)
    roots_value = table.get("test_roots", [])
    roots = _path_sequence(
        roots_value, root,
        allow_empty=kind is C.RunnerKind.COMMAND,
    )
    if kind is not C.RunnerKind.COMMAND and not roots:
        _fail()
    workers = table.get("workers", 1)
    lifecycle = table.get("lifecycle", "cooperative-process-group")
    if isinstance(workers, bool) or not isinstance(workers, int):
        _fail()
    if not isinstance(lifecycle, str):
        _fail()
    try:
        return C.RunnerConfig(
            kind=kind, launcher=launcher, args=args, full_args=full_args,
            test_roots=roots, workers=workers, lifecycle=lifecycle,
        )
    except (TypeError, ValueError):
        _fail()
    raise AssertionError("unreachable")


def _setup(data: object, root: Path) -> C.SetupConfig | None:
    if data is None:
        return None
    table = _table(data)
    _keys(table, {"argv", "required_paths", "network", "lifecycle_scripts"},
          required={"argv", "required_paths", "network", "lifecycle_scripts"})
    argv = _argv(table["argv"], allow_empty=False)
    paths = _path_sequence(table["required_paths"], root, allow_empty=False)
    network = table["network"]
    lifecycle_scripts = table["lifecycle_scripts"]
    if not isinstance(network, bool) or not isinstance(lifecycle_scripts, bool):
        _fail()
    try:
        return C.SetupConfig(
            argv=argv, required_paths=paths, network=network,
            lifecycle_scripts=lifecycle_scripts,
        )
    except (TypeError, ValueError):
        _fail()
    raise AssertionError("unreachable")


def _resources(data: object) -> C.ResourceConfig:
    if data is None:
        data = {}
    table = _table(data)
    _keys(table, {"locks", "memory_mb_per_worker", "probe_isolation"})
    locks = _string_sequence(table.get("locks", []), max_entries=32,
                             max_len=64)
    memory = table.get("memory_mb_per_worker", 0)
    probe = table.get("probe_isolation", "undeclared")
    if isinstance(memory, bool) or not isinstance(memory, int) or not isinstance(probe, str):
        _fail()
    try:
        return C.ResourceConfig(
            locks=locks, memory_mb_per_worker=memory,
            probe_isolation=probe,
        )
    except (TypeError, ValueError):
        _fail()
    raise AssertionError("unreachable")


def _group(value: object, root: Path) -> C.Group:
    table = _table(value)
    _keys(table, {"name", "sources", "tests"},
          required={"name", "sources", "tests"})
    name = _check_string(table["name"], max_len=64)
    sources = _path_sequence(table["sources"], root, allow_empty=False)
    tests = _path_sequence(table["tests"], root, allow_empty=False)
    try:
        return C.Group(name=name, sources=sources, tests=tests)
    except (TypeError, ValueError):
        _fail()
    raise AssertionError("unreachable")


def _selection(data: object, root: Path) -> C.SelectionPolicy:
    if data is None:
        data = {}
    table = _table(data)
    _keys(table, {"enabled", "closed_inputs", "input_roots", "ignored_inputs",
                  "environment", "full_triggers", "always", "no_tests",
                  "non_input_outputs", "full_ratio", "groups"})
    enabled = table.get("enabled", False)
    closed_inputs = table.get("closed_inputs", False)
    if not isinstance(enabled, bool) or not isinstance(closed_inputs, bool):
        _fail()
    path_fields = (
        "input_roots", "ignored_inputs", "full_triggers", "always",
        "no_tests", "non_input_outputs",
    )
    paths = {
        field: _path_sequence(table.get(field, []), root)
        for field in path_fields
    }
    if sum(len(items) for items in paths.values()) > _MAX_PATH_ENTRIES:
        _fail()
    environment = _string_sequence(table.get("environment", []), max_entries=256,
                                   max_len=128)
    if any(not _ENVIRONMENT_NAME.fullmatch(item) for item in environment):
        _fail()
    groups_value = _sequence(table.get("groups", []), max_entries=_MAX_GROUPS)
    groups = tuple(_group(item, root) for item in groups_value)
    if sum(len(group.sources) + len(group.tests) for group in groups) \
            + sum(len(items) for items in paths.values()) > _MAX_PATH_ENTRIES:
        _fail()
    ratio = table.get("full_ratio", 0.70)
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
        _fail()
    try:
        return C.SelectionPolicy(
            enabled=enabled, closed_inputs=closed_inputs,
            input_roots=paths["input_roots"],
            ignored_inputs=paths["ignored_inputs"],
            environment=environment,
            full_triggers=paths["full_triggers"],
            always=paths["always"], no_tests=paths["no_tests"],
            non_input_outputs=paths["non_input_outputs"],
            full_ratio=ratio, groups=groups,
        )
    except (TypeError, ValueError):
        _fail()
    raise AssertionError("unreachable")


def _parse_config(raw: bytes, root: Path, path: Path) -> tuple[C.Config, tuple[C.Reason, ...]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _problem("invalid-config", "project configuration is invalid")
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        raise _problem("invalid-config", "project configuration is invalid")
    try:
        _check_tree_strings(data)
        table = _table(data)
        _keys(table, {"version", "project_id", "runner", "setup", "resources",
                      "selection"}, required={"version", "project_id", "runner"})
        version = table["version"]
        project_id = table["project_id"]
        if isinstance(version, bool) or version != 1:
            _fail()
        if not isinstance(project_id, str) or not _HEX32.fullmatch(project_id):
            _fail()
        runner = _runner(table["runner"], root)
        setup = _setup(table.get("setup"), root)
        resources = _resources(table.get("resources"))
    except C.Problem:
        raise
    except (TypeError, ValueError, _ConfigInvalid):
        raise _problem("invalid-config", "project configuration is invalid")

    warnings: tuple[C.Reason, ...] = ()
    try:
        selection = _selection(table.get("selection"), root)
    except (C.Problem, TypeError, ValueError, _ConfigInvalid):
        selection = C.SelectionPolicy(enabled=False, closed_inputs=False)
        warnings = (C.Reason(
            code="policy-invalid",
            message="selection policy is invalid; automatic selection is disabled",
            paths=(),
        ),)
    try:
        config = C.Config(
            runner=runner, setup=setup, resources=resources,
            selection=selection, project_id=project_id, config_path=path,
        )
    except (TypeError, ValueError):
        raise _problem("invalid-config", "project configuration is invalid")
    return config, warnings


def _summary(config: C.Config) -> C.ConfigSummary:
    scoped_argv = config.runner.launcher + config.runner.args
    full_argv = scoped_argv + config.runner.full_args
    scoped = C.summarize_command(
        config.runner.kind, C.Mode.SCOPED, scoped_argv,
        workers=config.runner.workers, provenance=("config",),
    )
    full = C.summarize_command(
        config.runner.kind, C.Mode.FULL, full_argv,
        workers=config.runner.workers, provenance=("config",),
    )
    return C.summarize_config(config, scoped=scoped, full=full)


def resolve_config(cwd: Path) -> C.ConfigResolution:
    """Resolve only the nearest safe repository-local ``.ptest.toml``."""
    try:
        physical_cwd = _absolute_directory(cwd)
    except C.Problem as problem:
        raw_root = Path(cwd)
        if not raw_root.is_absolute():
            raw_root = Path(os.getcwd()) / raw_root
        return C.ConfigResolution(root=raw_root, path=None, config=None,
                                  problem=problem)
    root, path, raw, problem = _find_config(physical_cwd)
    if problem is not None:
        return C.ConfigResolution(root=root, path=path, config=None,
                                  problem=problem)
    if raw is None or path is None:
        return C.ConfigResolution(
            root=physical_cwd, path=None, config=None,
            problem=_problem("initialization-required",
                             "project configuration is required"),
        )
    try:
        config, warnings = _parse_config(raw, root, path)
    except C.Problem as parse_problem:
        return C.ConfigResolution(root=root, path=path, config=None,
                                  problem=parse_problem)
    return C.ConfigResolution(
        root=root, path=path, config=config,
        provenance=("config",), warnings=warnings, problem=None,
    )


def _native_file(root: Path, name: str) -> bytes | None:
    target = root / name
    try:
        stamp = os.lstat(target)
    except FileNotFoundError:
        return None
    except OSError:
        raise _problem("state-unavailable", "native project file is unavailable")
    if stat.S_ISLNK(stamp.st_mode):
        raise _problem("unsafe-path", "native project file path is unsafe")
    if not stat.S_ISREG(stamp.st_mode):
        raise _problem("unsafe-path", "native project file is not a file")
    try:
        raw = read_regular(root, name, _NATIVE_MAX_BYTES + 1)
    except C.Problem as problem:
        code = problem.code if problem.code in {"unsafe-path", "state-unavailable"} else "state-unavailable"
        raise _problem(code, "native project file is unavailable")
    if len(raw) > _NATIVE_MAX_BYTES:
        raise _problem("invalid-config", "native project file exceeds its bound")
    return raw


def _native_root(cwd: Path) -> Path:
    boundary = _git_boundary(cwd)
    return boundary if boundary is not None else cwd


def _native_candidates(root: Path) -> tuple[C.RunnerKind, ...]:
    found: list[C.RunnerKind] = []
    for name in ("pytest.ini", "tox.ini", "setup.cfg", "conftest.py"):
        if _native_file(root, name) is not None:
            found.append(C.RunnerKind.PYTEST)
            break
    pyproject = _native_file(root, "pyproject.toml")
    if pyproject is not None:
        try:
            parsed = tomllib.loads(pyproject.decode("utf-8"))
            tool = parsed.get("tool", {}) if isinstance(parsed, dict) else {}
            if isinstance(tool, dict) and isinstance(tool.get("pytest"), dict):
                if C.RunnerKind.PYTEST not in found:
                    found.append(C.RunnerKind.PYTEST)
            project = parsed.get("project", {}) if isinstance(parsed, dict) else {}
            groups = parsed.get("dependency-groups", {}) if isinstance(parsed, dict) else {}
            dependency_values = []
            if isinstance(project, dict):
                dependency_values.append(project.get("dependencies", []))
                optional = project.get("optional-dependencies", {})
                if isinstance(optional, dict):
                    dependency_values.extend(optional.values())
            if isinstance(groups, dict):
                dependency_values.extend(groups.values())
            if any(
                isinstance(item, str) and re.match(r"^pytest(?:[<>=!~;]|$)", item)
                for values in dependency_values if isinstance(values, list)
                for item in values
            ) and C.RunnerKind.PYTEST not in found:
                found.append(C.RunnerKind.PYTEST)
        except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError):
            pass
    package = _native_file(root, "package.json")
    vitest_evidence = False
    if package is not None:
        try:
            parsed = json.loads(package.decode("utf-8"))
            if isinstance(parsed, dict):
                for field in ("dependencies", "devDependencies",
                              "optionalDependencies", "peerDependencies"):
                    values = parsed.get(field)
                    if isinstance(values, dict) and "vitest" in values:
                        vitest_evidence = True
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            pass
    for name in ("vitest.config.ts", "vitest.config.js", "vitest.config.mts",
                 "vitest.config.mjs", "vitest.config.cts", "vitest.config.cjs"):
        if _native_file(root, name) is not None:
            vitest_evidence = True
            break
    if vitest_evidence:
        found.append(C.RunnerKind.VITEST)
    if _native_file(root, "go.mod") is not None:
        found.append(C.RunnerKind.GO)
    if _native_file(root, "Cargo.toml") is not None:
        found.append(C.RunnerKind.CARGO)
    return tuple(found)


def _directory_exists(root: Path, name: str) -> bool:
    target = root / name
    try:
        stamp = os.lstat(target)
    except FileNotFoundError:
        return False
    except OSError:
        raise _problem("state-unavailable", "native project path is unavailable")
    if stat.S_ISLNK(stamp.st_mode):
        raise _problem("unsafe-path", "native project path is unsafe")
    return stat.S_ISDIR(stamp.st_mode)


def _fresh_config(root: Path, target: Path, kind: C.RunnerKind) -> C.Config:
    if kind is C.RunnerKind.COMMAND:
        raise _problem("command-required", "an explicit command configuration is required")
    if kind is C.RunnerKind.PYTEST:
        roots = ("tests",) if _directory_exists(root, "tests") else (".",)
        launcher = ("uv", "run", "--locked", "--no-sync", "python") \
            if _native_file(root, "uv.lock") is not None else ("python",)
        setup = None
        if _native_file(root, "uv.lock") is not None:
            setup = C.SetupConfig(
                argv=("uv", "sync", "--locked"),
                required_paths=(".venv/bin/python",),
                network=True, lifecycle_scripts=True,
            )
    elif kind is C.RunnerKind.VITEST:
        roots = ("tests",) if _directory_exists(root, "tests") else (".",)
        launcher = ("node",)
        setup = None
        if _native_file(root, "package-lock.json") is not None:
            setup = C.SetupConfig(
                argv=("npm", "ci"), required_paths=("node_modules",),
                network=True, lifecycle_scripts=True,
            )
    elif kind is C.RunnerKind.GO:
        roots = (".",)
        launcher = ("go",)
        setup = None
    else:
        roots = (".",)
        launcher = ("cargo",)
        setup = None
    config = C.Config(
        runner=C.RunnerConfig(
            kind=kind, launcher=launcher, args=(), full_args=(),
            test_roots=roots, workers=1,
        ),
        setup=setup,
        resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(enabled=False, closed_inputs=False),
        project_id=secrets.token_hex(16), config_path=target,
    )
    return config


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _serialize_fresh(config: C.Config) -> bytes:
    runner = config.runner
    lines = [
        "version = 1",
        f"project_id = {_toml_string(config.project_id)}",
        "",
        "[runner]",
        f"kind = {_toml_string(runner.kind.value)}",
        f"launcher = {_toml_array(runner.launcher)}",
        f"args = {_toml_array(runner.args)}",
        f"full_args = {_toml_array(runner.full_args)}",
        f"test_roots = {_toml_array(runner.test_roots)}",
        f"workers = {runner.workers}",
        f"lifecycle = {_toml_string(runner.lifecycle)}",
    ]
    if config.setup is not None:
        setup = config.setup
        lines.extend([
            "",
            "[setup]",
            f"argv = {_toml_array(setup.argv)}",
            f"required_paths = {_toml_array(setup.required_paths)}",
            f"network = {str(setup.network).lower()}",
            f"lifecycle_scripts = {str(setup.lifecycle_scripts).lower()}",
        ])
    resources = config.resources
    lines.extend([
        "",
        "[resources]",
        f"locks = {_toml_array(resources.locks)}",
        f"memory_mb_per_worker = {resources.memory_mb_per_worker}",
        f"probe_isolation = {_toml_string(resources.probe_isolation)}",
        "",
        "[selection]",
        f"enabled = {str(config.selection.enabled).lower()}",
        f"closed_inputs = {str(config.selection.closed_inputs).lower()}",
        f"input_roots = {_toml_array(config.selection.input_roots)}",
        f"ignored_inputs = {_toml_array(config.selection.ignored_inputs)}",
        f"environment = {_toml_array(config.selection.environment)}",
        f"full_triggers = {_toml_array(config.selection.full_triggers)}",
        f"always = {_toml_array(config.selection.always)}",
        f"no_tests = {_toml_array(config.selection.no_tests)}",
        f"non_input_outputs = {_toml_array(config.selection.non_input_outputs)}",
        f"full_ratio = {config.selection.full_ratio:g}",
    ])
    return ("\n".join(lines) + "\n").encode("utf-8")


def _existing_result(root: Path, target: Path, resolution: C.ConfigResolution) -> C.InitResult:
    warnings: tuple[C.Reason, ...] = resolution.warnings
    if resolution.problem is not None:
        code = resolution.problem.code
        if code not in C.REASON_CODES:
            code = "invalid-config"
        warnings = warnings + (C.Reason(
            code=code,
            message="existing project configuration could not be used",
            paths=(),
        ),)
    return C.InitResult(
        action=C.InitAction.EXISTING, target=target, exists=True,
        config=_summary(resolution.config) if resolution.config is not None else None,
        warnings=warnings,
    )


def init_project(cwd: Path, options: C.InitOptions) -> C.InitResult:
    """Preview or exclusively create one fresh native project config."""
    if not isinstance(options, C.InitOptions):
        raise TypeError("init_project requires InitOptions")
    physical_cwd = _absolute_directory(cwd)
    resolution = resolve_config(physical_cwd)
    if resolution.path is not None:
        return _existing_result(resolution.root, resolution.path, resolution)

    root = _native_root(physical_cwd)
    target = root / _CONFIG_NAME
    try:
        _, target_exists = _candidate_config(root)
    except C.Problem as problem:
        return _existing_result(root, target, C.ConfigResolution(
            root=root, path=target, config=None, problem=problem))
    if target_exists:
        return _existing_result(root, target, resolve_config(root))

    if options.runner is not None:
        kind = options.runner
    else:
        candidates = _native_candidates(root)
        if len(candidates) != 1:
            raise _problem("invalid-config", "an explicit runner choice is required")
        kind = candidates[0]
    config = _fresh_config(root, target, kind)
    if options.dry_run:
        return C.InitResult(
            action=C.InitAction.PREVIEW, target=target, exists=False,
            config=_summary(config), warnings=(),
        )
    try:
        create_exclusive(root, _CONFIG_NAME, _serialize_fresh(config),
                         private=False)
    except C.Problem as problem:
        if problem.code == "already-exists":
            return _existing_result(root, target, resolve_config(root))
        raise
    return C.InitResult(
        action=C.InitAction.CREATED, target=target, exists=True,
        config=_summary(config), warnings=(),
    )
