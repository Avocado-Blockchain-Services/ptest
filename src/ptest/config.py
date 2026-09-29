"""Fresh, repository-local NG configuration and static initialization.

This module intentionally has no migration surface.  The only project input is
the nearest ``.ptest.toml``; initialization inspects a small allowlist of
native manifests without importing or executing repository code.
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import stat
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from . import contracts as C
from . import executability as _executability
from . import worktree as _worktree
from .files import create_exclusive, read_regular

_PHASE = "config"
_CONFIG_NAME = ".ptest.toml"
_CONFIG_MAX_BYTES = 256 * 1024
_NATIVE_MAX_BYTES = 256 * 1024
_MAX_GROUPS = 256
_MAX_PATH_ENTRIES = 4096
_MAX_LIST_ENTRIES = 4096
_MAX_STRING_LENGTH = 4096
_MAX_ARGV_TOKEN_LENGTH = 16384
_MAX_ARGV_ENTRIES = 256
_MAX_ARGV_BYTES = 131072
_MAX_MONOREPO_CHILDREN = 256
_ARGV_FIELDS = frozenset({
    ("runner", "launcher"), ("runner", "args"), ("runner", "full_args"),
    ("setup", "argv"),
})
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


def repository_root(cwd: Path | str) -> Path:
    """Return the static Git root used by repository bootstrap operations."""
    physical = _absolute_directory(cwd)
    return _git_boundary(physical) or physical


CONFIG_UNCOMMITTED = "config-uncommitted"


def git_root(cwd: Path | str) -> Path | None:
    """Static git boundary containing cwd (no subprocess), or None.

    None for non-git, unsafe or unavailable inputs. Never raises.
    """
    try:
        physical = _absolute_directory(cwd)
    except Exception:
        return None
    try:
        return _git_boundary(physical)
    except Exception:
        return None


def config_uncommitted(cwd: Path | str) -> C.Problem | None:
    """config-uncommitted Problem for a linked worktree missing local config.

    Returns the problem when cwd sits in a linked worktree with no nearest
    ``.ptest.toml`` and the main checkout has one at the same relative path
    (nearest-first); else None. Never raises, never loads main config.
    """
    try:
        physical = _absolute_directory(cwd)
    except Exception:
        return None
    try:
        _, _, _, found_problem = _find_config(physical)
    except Exception:
        return None
    if found_problem is None:
        return None
    if found_problem.code != "initialization-required":
        return None
    try:
        boundary = _git_boundary(physical)
    except Exception:
        return None
    if boundary is None:
        return None
    try:
        found = _worktree.main_config(physical, boundary)
    except Exception:
        return None
    if found is None:
        return None
    name = _CONFIG_NAME if found.relative == "." else f"{found.relative}/{_CONFIG_NAME}"
    return C.Problem(
        code=CONFIG_UNCOMMITTED,
        phase=_PHASE,
        retryable=False,
        message=(
            f"this is a linked git worktree without {name}; "
            f"the main checkout has {found.path}. "
            f"Worktrees only receive committed files. "
            f"Ask the user to commit {name} on the base branch. "
            f"Do not run ptest init here."
        ),
    )


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


def _check_tree_strings(value: object, location: tuple[str, ...] = ()) -> None:
    if isinstance(value, str):
        limit = _MAX_ARGV_TOKEN_LENGTH if location in _ARGV_FIELDS else _MAX_STRING_LENGTH
        _check_string(value, allow_empty=True, max_len=limit)
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_string(key)
            _check_tree_strings(item, (*location, key))
    elif isinstance(value, list):
        for item in value:
            _check_tree_strings(item, location)


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


def _path(value: object, root: Path, *, allow_final_symlink: bool = False) -> str:
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
    for index, part in enumerate(parts):
        cursor = cursor / part
        try:
            stamp = os.lstat(cursor)
        except FileNotFoundError:
            break
        except OSError:
            raise _problem("state-unavailable", "project path is unavailable")
        if stat.S_ISLNK(stamp.st_mode) and not (
            allow_final_symlink and index == len(parts) - 1
        ):
            raise _problem("unsafe-path", "project path crosses a symlink")
    return text


def _path_sequence(value: object, root: Path, *, allow_empty: bool = True,
                   allow_final_symlink: bool = False) -> tuple[str, ...]:
    items = _sequence(value, allow_empty=allow_empty)
    if len(items) > _MAX_PATH_ENTRIES:
        _fail()
    return tuple(_path(item, root, allow_final_symlink=allow_final_symlink)
                 for item in items)


def _argv(value: object, *, allow_empty: bool) -> tuple[str, ...]:
    items = _string_sequence(value, allow_empty=allow_empty,
                             max_entries=_MAX_ARGV_ENTRIES,
                             max_len=_MAX_ARGV_TOKEN_LENGTH)
    _command_bound(items)
    return items


def _command_bound(items: tuple[str, ...]) -> None:
    if (len(items) > _MAX_ARGV_ENTRIES
            or sum(len(item.encode("utf-8")) for item in items) > _MAX_ARGV_BYTES):
        _fail()


def _optional_timeout(table: dict, key: str) -> float | None:
    if key not in table:
        return None
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail()
    result = float(value)
    if not math.isfinite(result) or not 1 <= result <= C.MAX_COMPOUND_TIMEOUT_S:
        _fail()
    return result


def _runner(data: object, root: Path) -> C.RunnerConfig:
    table = _table(data)
    _keys(table, {"kind", "launcher", "args", "full_args", "test_roots",
                  "workers", "lifecycle", "timeout", "full_timeout"},
          required={"kind", "launcher"})
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
    _command_bound(launcher + args + full_args)
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
    timeout = _optional_timeout(table, "timeout")
    full_timeout = _optional_timeout(table, "full_timeout")
    try:
        return C.RunnerConfig(
            kind=kind, launcher=launcher, args=args, full_args=full_args,
            test_roots=roots, workers=workers, lifecycle=lifecycle,
            timeout_s=timeout, full_timeout_s=full_timeout,
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
    # Setup declares presence probes, not files to read. A normal venv's final
    # python entry is a symlink; lstat it without following its target. Ancestors
    # remain non-symlink paths, and actual reads still use read_regular.
    paths = _path_sequence(table["required_paths"], root, allow_empty=False,
                           allow_final_symlink=True)
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
    _check_tree_strings(data, ("selection",))
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
    if len({group.name for group in groups}) != len(groups):
        _fail()
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
        table = _table(data)
        _keys(table, {"version", "project_id", "runner", "setup", "resources",
                      "selection"}, required={"version", "project_id", "runner"})
        # Selection validation has its own fail-closed fallback below. Invalid
        # policy strings must not invalidate independently safe execution.
        for name, value in table.items():
            if name != "selection":
                _check_tree_strings(value, (name,))
        version = table["version"]
        project_id = table["project_id"]
        if type(version) is not int or version != 1:
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
        if problem.code == "initialization-required":
            uncommitted = config_uncommitted(physical_cwd)
            if uncommitted is not None:
                return C.ConfigResolution(root=physical_cwd, path=None,
                                          config=None, problem=uncommitted)
        return C.ConfigResolution(root=root, path=path, config=None,
                                  problem=problem)
    if raw is None or path is None:
        uncommitted = config_uncommitted(physical_cwd)
        if uncommitted is not None:
            return C.ConfigResolution(root=physical_cwd, path=None,
                                      config=None, problem=uncommitted)
        return C.ConfigResolution(
            root=physical_cwd, path=None, config=None,
            problem=_problem("initialization-required",
                             "project configuration is required"),
        )
    try:
        try:
            parsed = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict) and type(parsed.get("version")) is int \
                and parsed.get("version") == 2:
            from .monorepo import parse_monorepo_manifest
            manifest = parse_monorepo_manifest(raw, path)
            return C.ConfigResolution(root=root, path=path, config=None,
                                      monorepo=manifest,
                                      provenance=("monorepo",), problem=None)
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


def _native_present(root: Path, name: str) -> bool:
    """Check unparsed native evidence with lstat only, never content reads."""
    safe_root = _absolute_directory(root)
    target = safe_root / name
    try:
        stamp = os.lstat(target)
    except FileNotFoundError:
        return False
    except OSError:
        raise _problem("state-unavailable", "native project file is unavailable")
    if stat.S_ISLNK(stamp.st_mode):
        raise _problem("unsafe-path", "native project file path is unsafe")
    if not stat.S_ISREG(stamp.st_mode):
        raise _problem("unsafe-path", "native project file is not a file")
    return True


def _native_candidates(root: Path) -> tuple[C.RunnerKind, ...]:
    found: list[C.RunnerKind] = []
    for name in ("pytest.ini", "tox.ini", "setup.cfg", "conftest.py"):
        if _native_present(root, name):
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
        if _native_present(root, name):
            vitest_evidence = True
            break
    if vitest_evidence:
        found.append(C.RunnerKind.VITEST)
    if _native_present(root, "go.mod"):
        found.append(C.RunnerKind.GO)
    if _native_present(root, "Cargo.toml"):
        found.append(C.RunnerKind.CARGO)
    return tuple(found)


def _auto_monorepo_children(root: Path) -> tuple[tuple[str, C.RunnerKind], ...]:
    """Inspect only immediate, real child directories for one native runner."""
    found: list[tuple[str, C.RunnerKind]] = []
    try:
        entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except OSError:
        raise _problem("state-unavailable", "repository contents are unavailable")
    for entry in entries:
        try:
            if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                continue
            child = Path(entry.path)
            # Infrastructure trees can contain Python harness tests but are
            # not application runner children. Never infer pytest from that
            # harness when the directory itself declares Terraform config.
            try:
                with os.scandir(child) as child_entries:
                    if any(item.name.endswith((".tf", ".tf.json"))
                           for item in child_entries):
                        continue
            except OSError:
                raise _problem("state-unavailable", "repository contents are unavailable")
            candidates = _native_candidates(child)
        except OSError:
            raise _problem("state-unavailable", "repository contents are unavailable")
        if len(candidates) > 1:
            kinds = ", ".join(kind.value for kind in candidates)
            raise _problem(
                "invalid-config",
                f"{entry.name} has more than one test runner ({kinds}): declare it with "
                f"ptest init --child {entry.name} --runner <kind>")
        if len(candidates) == 1:
            found.append((entry.name, candidates[0]))
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


_ROOT_SCAN_SKIP = frozenset({
    "node_modules", "venv", "site-packages", "__pycache__", "build", "dist",
    "htmlcov", "__pypackages__",
})
_TESTPATHS_GLOB = re.compile(r"[*?\[]")
# Top-level folders that hold benchmarks, docs, examples or tooling rather
# than the suite (attrs' bench/ needs pytest-benchmark and is run on its own).
# They count only when no other folder holds tests.
_NON_SUITE_DIRS = frozenset({
    "bench", "benchmark", "benchmarks", "doc", "docs", "example", "examples",
    "script", "scripts", "tools", "tasks", "site",
})


def _is_pytest_test_file(name: str) -> bool:
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _declared_testpaths(root: Path) -> tuple[str, ...] | None:
    """``testpaths`` from the pytest config file pytest itself would pick.

    None when no config file declares them, or when they are not plain
    relative paths to existing directories or files (globs, absolute,
    escaping, missing) that ptest can pass on.
    """
    import configparser
    value: object = None
    for name, section in (("pytest.ini", "pytest"), (".pytest.ini", "pytest"),
                          ("pyproject.toml", None), ("tox.ini", "pytest"),
                          ("setup.cfg", "tool:pytest")):
        path = root / name
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        if section is None:
            try:
                options = tomllib.loads(text).get("tool", {}).get("pytest", {})
            except tomllib.TOMLDecodeError:
                return None
            if not isinstance(options, dict) or "ini_options" not in options:
                continue
            value = options["ini_options"].get("testpaths") \
                if isinstance(options["ini_options"], dict) else None
            break
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(text)
        except configparser.Error:
            return None
        if parser.has_section(section):
            value = parser.get(section, "testpaths", fallback=None)
            break
        if name.endswith("pytest.ini"):
            break  # pytest.ini wins even without a [pytest] section.
    if isinstance(value, str):
        value = value.split()
    if not isinstance(value, list) or not value:
        return None
    roots: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        rel = item.strip().rstrip("/") or "."
        if (_TESTPATHS_GLOB.search(rel) or rel.startswith("/")
                or ".." in rel.split("/") or not (root / rel).exists()):
            return None
        if rel not in roots:
            roots.append(rel)
    return tuple(roots)


def _dir_has_test_files(base: Path) -> bool:
    if base.is_file():
        return _is_pytest_test_file(base.name)
    for current, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in _ROOT_SCAN_SKIP]
        if any(_is_pytest_test_file(name) for name in files):
            return True
    return False


def _addopts_ignored_dirs(root: Path) -> set[str]:
    """Top-level folders the project's own addopts ``--ignore`` (fastapi:
    ``--ignore=docs_src``), so ``--full`` never passes them explicitly."""
    try:
        tokens = _executability._pytest_addopts(root)
    except Exception:
        return set()
    ignored: set[str] = set()
    for index, token in enumerate(tokens):
        value = None
        if token.startswith(("--ignore=", "--ignore-glob=")):
            value = token.split("=", 1)[1]
        elif token in ("--ignore", "--ignore-glob") and index + 1 < len(tokens):
            value = tokens[index + 1]
        if value:
            ignored.add(value.strip("/").removeprefix("./").split("/", 1)[0])
    return ignored


def pytest_test_roots(root: Path) -> tuple[str, ...]:
    """Directories a full pytest run of ``root`` covers.

    Declared ``testpaths`` when pytest has them; otherwise every top-level
    directory holding ``test_*.py``/``*_test.py`` files (pytest's own
    default collects all of them), ``.`` when test files sit at the root,
    and ``tests`` (or ``.``) when nothing is found.
    """
    declared = _declared_testpaths(root)
    if declared is not None:
        return declared
    found: list[str] = []
    try:
        entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except OSError:
        entries = []
    for entry in entries:
        if entry.name.startswith(".") or entry.name in _ROOT_SCAN_SKIP:
            continue
        try:
            if entry.is_file(follow_symlinks=False) and _is_pytest_test_file(entry.name):
                return (".",)
            if entry.is_dir(follow_symlinks=False) and _dir_has_test_files(Path(entry.path)):
                found.append(entry.name)
        except OSError:
            continue
    ignored = _addopts_ignored_dirs(root)
    found = [name for name in found if name not in ignored]
    suite = [name for name in found if name.lower() not in _NON_SUITE_DIRS]
    if suite or found:
        return tuple(suite or found)
    return ("tests",) if _directory_exists(root, "tests") else (".",)


# Where projects without a uv.lock keep their test requirements, in the
# order they are preferred.
_REQUIREMENT_FILES = (
    "requirements-dev.txt", "requirements-test.txt", "requirements-tests.txt",
    "dev-requirements.txt", "test-requirements.txt", "requirements/dev.txt",
    "requirements/test.txt", "requirements/tests.txt", "requirements.txt",
)
VENV_PYTHON = ".venv/bin/python"


def _read_small(root: Path, relative: str) -> str | None:
    """A project file of at most 1 MiB, read no-follow; None when unavailable."""
    if not _native_present(root, relative):
        return None
    try:
        return read_regular(root, relative, 1024 * 1024).decode("utf-8")
    except (C.Problem, OSError, UnicodeDecodeError):
        return None


def _mentions_pytest(requirements: object) -> bool:
    if isinstance(requirements, dict):
        requirements = list(requirements)
    if not isinstance(requirements, list):
        return False
    return any(isinstance(item, str)
               and re.match(r"\s*pytest(?![\w-])", item) is not None
               for item in requirements)


def _declares_python_dependencies(root: Path) -> bool:
    """A package manifest or requirements file, not just tool settings.

    A pyproject.toml that only configures pytest or ruff declares nothing
    to install, so the ambient interpreter stays in charge.
    """
    if any(_native_present(root, name)
           for name in ("setup.py", "setup.cfg", *_REQUIREMENT_FILES)):
        return True
    text = _read_small(root, "pyproject.toml")
    if text is None:
        return False
    try:
        pyproject = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    tool = pyproject.get("tool") if isinstance(pyproject.get("tool"), dict) else {}
    return ("project" in pyproject or "dependency-groups" in pyproject
            or "poetry" in tool)


def pytest_install_spec(root: Path) -> tuple[str, ...]:
    """``uv pip install`` arguments for a pytest project without a uv.lock.

    The project itself (editable) plus its test requirements from the first
    place that names pytest: a requirements file, a PEP 621 extra, a PEP 735
    dependency group, or Poetry dev dependencies. ``pytest`` is always added.
    """
    spec: list[str] = []
    installs_project = False
    for name in _REQUIREMENT_FILES:
        text = _read_small(root, name)
        if text is not None and re.search(r"(?m)^\s*pytest(?![\w-])", text):
            spec += ["-r", name]
            installs_project = bool(re.search(r"(?m)^\s*-e\s+\.", text))
            break
    pyproject: dict = {}
    text = _read_small(root, "pyproject.toml")
    if text is not None:
        try:
            pyproject = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            pyproject = {}
    project = pyproject.get("project") if isinstance(pyproject.get("project"), dict) else None
    buildable = (project is not None or _native_present(root, "setup.py")
                 or _native_present(root, "setup.cfg")
                 or isinstance(pyproject.get("tool", {}).get("poetry"), dict))
    if not spec:
        extras = project.get("optional-dependencies", {}) if project else {}
        groups = pyproject.get("dependency-groups", {})
        poetry = pyproject.get("tool", {}).get("poetry", {})
        extra = next((key for key, deps in (extras.items() if isinstance(extras, dict) else ())
                      if _mentions_pytest(deps)), None)
        group = next((key for key, deps in (groups.items() if isinstance(groups, dict) else ())
                      if _mentions_pytest(deps)), None)
        if extra is not None and buildable:
            spec.append(f"-e .[{extra}]")
            installs_project = True
        elif group is not None:
            spec += ["--group", group]
        elif isinstance(poetry, dict):
            tables = [poetry.get("dev-dependencies")]
            group_tables = poetry.get("group")
            if isinstance(group_tables, dict):
                tables += [value.get("dependencies") for value in group_tables.values()
                           if isinstance(value, dict)]
            names = [name for table in tables if isinstance(table, dict)
                     for name in table if name != "python"]
            if _mentions_pytest(names):
                spec += list(dict.fromkeys(names))
    if buildable and not installs_project:
        spec.insert(0, "-e")
        spec.insert(1, ".")
    if "pytest" not in spec:
        spec.append("pytest")
    return tuple(spec)


def _pytest_venv_setup(root: Path) -> C.SetupConfig:
    """Setup for a pytest project without a uv.lock: a checkout .venv via uv."""
    import shlex
    install = shlex.join(("uv", "pip", "install", "-q", "--python", VENV_PYTHON,
                          *pytest_install_spec(root)))
    return C.SetupConfig(
        argv=("sh", "-c", f"uv venv -q --allow-existing .venv && {install}"),
        required_paths=(VENV_PYTHON,),
        network=True, lifecycle_scripts=True,
    )


def node_install_argv(root: Path) -> tuple[str, ...] | None:
    """The frozen install command of the package manager whose lockfile exists."""
    if _native_present(root, "pnpm-lock.yaml"):
        return ("pnpm", "install", "--frozen-lockfile")
    if _native_present(root, "yarn.lock"):
        # Yarn Berry (2+) marks itself with .yarnrc.yml and spells it --immutable.
        return (("yarn", "install", "--immutable") if _native_present(root, ".yarnrc.yml")
                else ("yarn", "install", "--frozen-lockfile"))
    if _native_present(root, "bun.lock") or _native_present(root, "bun.lockb"):
        return ("bun", "install", "--frozen-lockfile")
    if _native_present(root, "package-lock.json"):
        return ("npm", "ci")
    return None


def _fresh_config(root: Path, target: Path, kind: C.RunnerKind) -> C.Config:
    if kind is C.RunnerKind.COMMAND:
        raise _problem("command-required", "an explicit command configuration is required")
    args: tuple[str, ...] = ()
    if kind is C.RunnerKind.PYTEST:
        roots = pytest_test_roots(root)
        locked = _native_present(root, "uv.lock")
        # Without a uv.lock, a project that declares its dependencies gets a
        # checkout .venv built by setup; a bare folder of tests keeps the
        # ambient interpreter.
        declared = not locked and _declares_python_dependencies(root)
        launcher = (("uv", "run", "--locked", "--no-sync", "python") if locked
                    else (VENV_PYTHON,) if declared else ("python",))
        # A fresh pytest config serializes xdist only for config-level
        # parallel-tier fallbacks (unsupported --dist, --maxprocesses):
        # neutralize those with "-n 0". Environment reasons
        # (missing/unqualified xdist, or a missing/unfrozen
        # pytest-cov/coverage pair) can change with uv sync, so they are
        # reported, never written. Qualified projects keep empty args and
        # run workers per granted slot.
        _serial_fallback = _executability.pytest_xdist_active(root)
        setup = None
        if locked:
            # pydantic-settings keeps pytest in a non-default "testing"
            # group: plain `uv sync` would leave pytest uninstalled.
            from . import doctor_fix
            argv: tuple[str, ...] = ("uv", "sync", "--locked")
            argv = doctor_fix._setup_extras_fix(root, kind, argv, args) or argv
            setup = C.SetupConfig(
                argv=argv,
                required_paths=(VENV_PYTHON,),
                network=True, lifecycle_scripts=True,
            )
        elif declared:
            setup = _pytest_venv_setup(root)
    elif kind is C.RunnerKind.VITEST:
        roots = ("tests",) if _directory_exists(root, "tests") else (".",)
        launcher = ("node",)
        setup = None
        install = node_install_argv(root)
        if install is not None:
            setup = C.SetupConfig(
                argv=install, required_paths=("node_modules",),
                network=True, lifecycle_scripts=True,
            )
    elif kind is C.RunnerKind.GO:
        roots = (".",)   # the Go adapter runs "." as every package (./...)
        launcher = ("go",)
        setup = None
    else:
        roots = (".",)
        launcher = ("cargo",)
        # lib, bin and integration-test targets; doctests stay out of the gate.
        args = ("--tests",)
        setup = None
    config = C.Config(
        runner=C.RunnerConfig(
            kind=kind, launcher=launcher, args=args, full_args=(),
            test_roots=roots, workers=1,
        ),
        setup=setup,
        resources=C.ResourceConfig(),
        # uv (pytest) and npm (vitest) own their environments and may
        # write generated tool artifacts under them (interpreter/package
        # links, Vite caches).  They are not project inputs, so each gets
        # one fixed, narrowly-scoped exemption in the generated config;
        # arbitrary ignored paths remain visible and continue to force a
        # conservative full decision.  Dependency changes stay governed by
        # the lockfile/setup fingerprint.
        selection=C.SelectionPolicy(
            enabled=kind is C.RunnerKind.PYTEST, closed_inputs=False,
            # Every pytest project runs from a setup-built .venv (uv sync,
            # or uv venv without a lock); Cargo builds into target/.
            non_input_outputs=(".venv",) if kind is C.RunnerKind.PYTEST
            and setup is not None else
            ("node_modules",) if kind is C.RunnerKind.VITEST else
            ("target",) if kind is C.RunnerKind.CARGO else (),
        ),
        project_id=secrets.token_hex(16), config_path=target,
    )
    if kind is C.RunnerKind.PYTEST and _serial_fallback:
        probe = replace(config, runner=replace(config.runner, args=()))
        if _executability.parallel_request(probe).config_level:
            config = replace(
                config, runner=replace(config.runner, args=("-n", "0")))
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
    if runner.timeout_s is not None:
        lines.append(f"timeout = {runner.timeout_s:g}")
    if runner.full_timeout_s is not None:
        lines.append(f"full_timeout = {runner.full_timeout_s:g}")
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


def _serialize_monorepo(children: tuple[str, ...]) -> bytes:
    return (
        "version = 2\n\n[monorepo]\n"
        f"children = {_toml_array(children)}\n"
    ).encode("utf-8")


def _validate_init_child(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise _problem("invalid-config", "monorepo child path is invalid")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise _problem("unsafe-path", "monorepo child path is unsafe")
    parts = tuple(value.split("/"))
    if any(not part or part in {".", ".."} for part in parts):
        raise _problem("unsafe-path", "monorepo child path is unsafe")
    return parts


def _safe_init_child(root: Path, declaration: str) -> Path:
    parts = _validate_init_child(declaration)
    cursor = root
    for part in parts:
        cursor = cursor / part
        try:
            stamp = os.lstat(cursor)
        except FileNotFoundError:
            raise _problem("state-unavailable", "monorepo child directory is unavailable")
        except OSError:
            raise _problem("state-unavailable", "monorepo child directory is unavailable")
        if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
            raise _problem("unsafe-path", "monorepo child directory is unsafe")
    return cursor


@dataclass(frozen=True, slots=True)
class _ChildInit:
    declaration: str
    root: Path
    config: C.Config | None
    data: bytes | None


def _plan_monorepo_init(root: Path, options: C.InitOptions) -> tuple[tuple[_ChildInit, ...], bytes]:
    if not 1 <= len(options.children) <= _MAX_MONOREPO_CHILDREN:
        raise _problem("invalid-config", "monorepo initialization requires between one and 256 children")
    declarations = []
    paths = []
    for declaration, runner in options.children:
        parts = _validate_init_child(declaration)
        if parts in paths:
            raise _problem("invalid-config", "monorepo child paths must be unique")
        if any(parts[:len(previous)] == previous or previous[:len(parts)] == parts
               for previous in paths):
            raise _problem("invalid-config", "monorepo child paths must not overlap")
        paths.append(parts)
        declarations.append(declaration)

    planned: list[_ChildInit] = []
    for declaration, runner in options.children:
        child_root = _safe_init_child(root, declaration)
        target, exists = _candidate_config(child_root)
        if exists:
            raw = read_regular(child_root, _CONFIG_NAME, _CONFIG_MAX_BYTES + 1)
            if len(raw) > _CONFIG_MAX_BYTES:
                raise _problem("invalid-config", "child project configuration exceeds its bound")
            try:
                config, _ = _parse_config(raw, child_root, target)
            except C.Problem:
                raise
            except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError, _ConfigInvalid):
                raise _problem("invalid-config", "child project configuration is invalid")
            planned.append(_ChildInit(declaration, child_root, config, None))
        else:
            config = _fresh_config(child_root, target, runner)
            planned.append(_ChildInit(declaration, child_root, config,
                                      _serialize_fresh(config)))
    return tuple(planned), _serialize_monorepo(tuple(declarations))


def _config_detail(target: str, action: str) -> C.ActionRecord:
    return C.ActionRecord(target=target, action=action, source="config")


def _executability_notes(
        items: tuple[_executability.Executability, ...],
) -> tuple[C.ActionRecord, ...]:
    """Project notes plus verified run notes in the frozen §3.2 grammar."""
    notes: list[C.ActionRecord] = [
        C.ActionRecord(
            target=f"{item.project} · {item.runner} · {item.verdict()}",
            action="note", source="config",
        )
        for item in items
    ]
    notes.extend(
        C.ActionRecord(target=f"run: {command}", action="note", source="config")
        for command in _executability.commands(items)
    )
    return tuple(notes)


def _child_state(root: Path, declaration: str) -> str:
    """Probe one declared child config read-only: ``"ok"``, ``"missing"`` or ``"invalid"``.

    Reads are no-follow and bounded and the bytes are parsed but never
    executed; runners are not run or installed and nothing is written.
    """
    try:
        parts = _validate_init_child(declaration)
    except C.Problem:
        return "invalid"
    cursor = root
    for part in parts:
        cursor = cursor / part
        try:
            stamp = os.lstat(cursor)
        except FileNotFoundError:
            return "missing"
        except OSError:
            return "invalid"
        if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
            return "invalid"
    target = cursor / _CONFIG_NAME
    try:
        stamp = os.lstat(target)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "invalid"
    if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISREG(stamp.st_mode):
        return "invalid"
    try:
        raw = read_regular(cursor, _CONFIG_NAME, _CONFIG_MAX_BYTES + 1)
    except C.Problem:
        return "invalid"
    if len(raw) > _CONFIG_MAX_BYTES:
        return "invalid"
    try:
        _parse_config(raw, cursor, target)
    except C.Problem:
        return "invalid"
    return "ok"


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
    details: tuple = ()
    manifest = getattr(resolution, "monorepo", None)
    if resolution.problem is None and manifest is not None:
        children = tuple(getattr(manifest, "children", ()) or ())
        entries: list = [_config_detail(_CONFIG_NAME, "already present")]
        for child in children:
            state = _child_state(root, child)
            if state == "ok":
                entries.append(_config_detail(f"{child}/{_CONFIG_NAME}", "already present"))
                continue
            if state == "missing":
                note = f"declared child '{child}' has no configuration"
                message = f"declared child '{child}' configuration is missing"
            else:
                note = f"declared child '{child}' configuration is invalid"
                message = f"declared child '{child}' configuration is invalid"
            entries.append(_config_detail(note, "note"))
            warnings = warnings + (C.Reason(
                code="invalid-config",
                message=message,
                paths=(),
            ),)
        details = tuple(entries) + _executability_notes(
            _executability.check_resolution(resolution))
    elif resolution.problem is None:
        details = details + _executability_notes(
            _executability.check_resolution(resolution))
    return C.InitResult(
        action=C.InitAction.EXISTING, target=target, exists=True,
        config=_summary(resolution.config) if resolution.config is not None else None,
        warnings=warnings,
        details=details,
    )


def _commit_paths(boundary: Path | None, root: Path,
                  names: tuple[str, ...]) -> tuple[str, ...]:
    """Created config paths relative to the git boundary (posix), or ()."""
    if boundary is None:
        return ()
    try:
        return tuple((root / name).relative_to(boundary).as_posix()
                     for name in names)
    except ValueError:
        return ()


FROM_MAIN_REFUSAL = (
    "--from-main only works in a linked git worktree whose main checkout "
    "has .ptest.toml at this path; nothing was copied"
)


def _init_from_main(physical_cwd: Path, dry_run: bool) -> C.InitResult:
    """Copy the main checkout's config bytes verbatim into this worktree."""
    boundary = _git_boundary(physical_cwd)
    found = _worktree.main_config(physical_cwd, boundary) \
        if boundary is not None else None
    if found is None:
        raise _problem("invalid-config", FROM_MAIN_REFUSAL)
    name = _CONFIG_NAME if found.relative == "." else \
        f"{found.relative}/{_CONFIG_NAME}"
    if found.relative == ".":
        dest = found.worktree.root
    else:
        dest = _safe_init_child(found.worktree.root, found.relative)
    try:
        root_raw = read_regular(
            found.worktree.main_root, name, _CONFIG_MAX_BYTES + 1)
    except C.Problem:
        raise _problem("state-unavailable",
                       "main checkout configuration is unavailable")
    if len(root_raw) > _CONFIG_MAX_BYTES:
        raise _problem("invalid-config",
                       "main checkout configuration is invalid")
    skip_warnings: list[C.Reason] = []
    pending: list[tuple[str, Path, bytes, int]] = []
    child_details: list[C.ActionRecord] = []
    for child in found.children:
        try:
            child_root = _safe_init_child(dest, child)
        except C.Problem:
            skip_warnings.append(C.Reason(
                code=CONFIG_UNCOMMITTED,
                message=f"did not copy {child}/{_CONFIG_NAME}: "
                f"that directory is missing in this worktree",
                paths=(),
            ))
            continue
        _, exists = _candidate_config(child_root)
        if exists:
            child_details.append(
                _config_detail(f"{child}/{_CONFIG_NAME}", "already present"))
            continue
        child_name = f"{child}/{_CONFIG_NAME}" if found.relative == "." else \
            f"{found.relative}/{child}/{_CONFIG_NAME}"
        try:
            child_raw = read_regular(
                found.worktree.main_root, child_name, _CONFIG_MAX_BYTES + 1)
        except C.Problem:
            raise _problem("state-unavailable",
                           "main checkout configuration is unavailable")
        if len(child_raw) > _CONFIG_MAX_BYTES:
            raise _problem("invalid-config",
                           "main checkout configuration is invalid")
        pending.append((child, child_root, child_raw, len(child_details)))
        child_details.append(
            _config_detail(f"{child}/{_CONFIG_NAME}",
                           "would create" if dry_run else "created"))
    stopgap = C.Reason(
        code=CONFIG_UNCOMMITTED,
        message=f"copied {name} from the main checkout {found.path}; "
        f"this copy is a temporary stopgap that may go stale. "
        f"The fix is to ask the user to commit {name} on the base branch.",
        paths=(),
    )
    target = dest / _CONFIG_NAME
    if dry_run:
        return C.InitResult(
            action=C.InitAction.PREVIEW, target=target, exists=False,
            config=None, warnings=tuple(skip_warnings),
            details=(_config_detail(_CONFIG_NAME, "would create"),)
            + tuple(child_details),
        )
    for _, child_root, child_raw, detail_index in pending:
        try:
            create_exclusive(child_root, _CONFIG_NAME, child_raw,
                             private=False)
        except C.Problem as problem:
            if problem.code != "already-exists":
                raise
            child_details[detail_index] = _config_detail(
                child_details[detail_index].target, "already present")
    try:
        create_exclusive(dest, _CONFIG_NAME, root_raw, private=False)
    except C.Problem as problem:
        if problem.code == "already-exists":
            return _existing_result(dest, target, resolve_config(dest))
        raise
    fresh = resolve_config(dest)
    return C.InitResult(
        action=C.InitAction.CREATED, target=target, exists=True,
        config=_summary(fresh.config) if fresh.config is not None else None,
        warnings=(stopgap, *skip_warnings),
        details=(_config_detail(_CONFIG_NAME, "created"),)
        + tuple(child_details),
        commit_paths=(),
    )


def init_project(cwd: Path, options: C.InitOptions) -> C.InitResult:
    """Preview or exclusively create one fresh native project config."""
    if not isinstance(options, C.InitOptions):
        raise TypeError("init_project requires InitOptions")
    physical_cwd = _absolute_directory(cwd)
    resolution = resolve_config(physical_cwd)
    if resolution.path is not None:
        return _existing_result(resolution.root, resolution.path, resolution)
    if resolution.problem is not None \
            and resolution.problem.code == CONFIG_UNCOMMITTED:
        if not options.from_main:
            raise resolution.problem
        return _init_from_main(physical_cwd, options.dry_run)
    if options.from_main:
        raise _problem("invalid-config", FROM_MAIN_REFUSAL)

    # Initialization anchors at the repository boundary even when invoked from
    # a nested source directory. Runtime resolution remains nearest-config and
    # does not gain any discovery behavior from this bootstrap convenience.
    boundary = _git_boundary(physical_cwd)
    root = boundary if boundary is not None and boundary == physical_cwd else physical_cwd
    target = root / _CONFIG_NAME
    try:
        _, target_exists = _candidate_config(root)
    except C.Problem as problem:
        return _existing_result(root, target, C.ConfigResolution(
            root=root, path=target, config=None, problem=problem))
    if target_exists:
        return _existing_result(root, target, resolve_config(root))

    children = options.children
    root_candidates = _native_candidates(root) if not children else ()
    if not children and options.runner is None and len(root_candidates) != 1:
        auto_children = _auto_monorepo_children(root)
        # A workspace root with no runner of its own and one runner child
        # (full-stack-fastapi-template: backend/) is still a monorepo.
        if len(auto_children) >= 2 or (auto_children and not root_candidates):
            children = auto_children

    if children:
        child_options = C.InitOptions(
            runner=options.runner, dry_run=options.dry_run,
            reveal_command=options.reveal_command, children=children,
        )
        planned, root_data = _plan_monorepo_init(root, child_options)
        child_details = tuple(
            _config_detail(
                f"{child.declaration}/{_CONFIG_NAME}",
                ("would create" if options.dry_run else "created")
                if child.data is not None else "already present",
            )
            for child in planned
        )
        planned_items = tuple(
            _executability.check_config(child.config, project=child.declaration)
            for child in planned
        )
        if options.dry_run:
            return C.InitResult(
                action=C.InitAction.PREVIEW, target=target, exists=False,
                config=None, warnings=(),
                details=(_config_detail(_CONFIG_NAME, "would create"),) + child_details
                + _executability_notes(planned_items),
            )
        for child in planned:
            if child.data is not None:
                create_exclusive(child.root, _CONFIG_NAME, child.data, private=False)
        create_exclusive(root, _CONFIG_NAME, root_data, private=False)
        created_names = tuple(
            f"{child.declaration}/{_CONFIG_NAME}" for child in planned
            if child.data is not None
        ) + (_CONFIG_NAME,)
        return C.InitResult(
            action=C.InitAction.CREATED, target=target, exists=True,
            config=None, warnings=(),
            details=(_config_detail(_CONFIG_NAME, "created"),) + child_details
            + _executability_notes(planned_items),
            commit_paths=_commit_paths(boundary, root, created_names),
        )

    if options.runner is not None:
        kind = options.runner
    else:
        candidates = _native_candidates(root)
        if len(candidates) > 1:
            kinds = ", ".join(kind.value for kind in candidates)
            raise _problem(
                "invalid-config",
                f"found {kinds} here: choose one with ptest init --runner <kind>, or "
                "declare each project with --child DIR --runner KIND")
        if not candidates:
            raise _problem(
                "invalid-config",
                "no pytest, vitest, go or cargo project found here or in its top-level "
                "folders: run ptest init --runner <kind> to set one up anyway")
        kind = candidates[0]
    config = _fresh_config(root, target, kind)
    fresh_notes = _executability_notes(
        (_executability.check_config(config, project="."),))
    if options.dry_run:
        return C.InitResult(
            action=C.InitAction.PREVIEW, target=target, exists=False,
            config=_summary(config), warnings=(),
            details=(_config_detail(_CONFIG_NAME, "would create"),) + fresh_notes,
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
        details=(_config_detail(_CONFIG_NAME, "created"),) + fresh_notes,
        commit_paths=_commit_paths(boundary, root, (_CONFIG_NAME,)),
    )
