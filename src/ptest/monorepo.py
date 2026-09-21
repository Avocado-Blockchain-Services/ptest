"""Strict, explicit dispatch for a v2 monorepo root."""
from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import contracts as C


@dataclass(frozen=True, slots=True)
class MonorepoManifest:
    children: tuple[str, ...]
    source_path: Path


@dataclass(frozen=True, slots=True)
class ChildTarget:
    declaration: str
    directory: Path
    config: C.Config


@dataclass(frozen=True, slots=True)
class RoutedChildRequest:
    target: ChildTarget
    scopes: tuple[str, ...]


def _problem(message: str, code: str = "invalid-config") -> C.Problem:
    return C.Problem(code=code, message=message, phase="config", retryable=False)


def _safe_segments(value: object, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise _problem("monorepo path is invalid")
    if value.startswith("/") or value.endswith("/") or "//" in value:
        raise _problem("monorepo path is invalid")
    parts = tuple(value.split("/"))
    if not allow_empty and not parts:
        raise _problem("monorepo path is invalid")
    if any(not part or part in {".", ".."} for part in parts):
        raise _problem("monorepo path is invalid")
    if len(parts[0]) == 2 and parts[0][1] == ":":
        raise _problem("monorepo path is invalid")
    return parts


def parse_monorepo_manifest(raw: bytes, source_path: Path) -> MonorepoManifest:
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError):
        raise _problem("project configuration is invalid") from None
    if not isinstance(data, dict) or set(data) != {"version", "monorepo"}:
        raise _problem("project configuration is invalid")
    if type(data.get("version")) is not int or data["version"] != 2:
        raise _problem("project configuration is invalid")
    table = data.get("monorepo")
    if not isinstance(table, dict) or set(table) != {"children"}:
        raise _problem("project configuration is invalid")
    children = table["children"]
    if not isinstance(children, list) or not 1 <= len(children) <= 256:
        raise _problem("project configuration is invalid")
    declarations = tuple("/".join(_safe_segments(item)) for item in children)
    if len(set(declarations)) != len(declarations):
        raise _problem("monorepo children must be unique")
    paths = [tuple(item.split("/")) for item in declarations]
    if any(a == b[:len(a)] or b == a[:len(b)] for i, a in enumerate(paths)
           for b in paths[i + 1:]):
        raise _problem("monorepo children must not overlap")
    return MonorepoManifest(children=declarations, source_path=Path(source_path))


def _lstat(path: Path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        raise _problem("monorepo child is unavailable", "state-unavailable") from None
    except OSError:
        raise _problem("monorepo child cannot be inspected", "state-unavailable") from None


def preflight_children(root_dir: Path, manifest: MonorepoManifest) -> tuple[ChildTarget, ...]:
    from .config import _parse_config

    root_dir = Path(root_dir)
    targets: list[ChildTarget] = []
    root_real = root_dir.resolve(strict=True)
    for declaration in manifest.children:
        current = root_dir
        for component in declaration.split("/"):
            current = current / component
            stamp = _lstat(current)
            if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
                raise _problem(f"monorepo child is unsafe: {declaration}", "unsafe-path")
        directory = current.resolve(strict=True)
        if directory != root_real and root_real not in directory.parents:
            raise _problem(f"monorepo child escapes root: {declaration}", "unsafe-path")
        config_path = current / ".ptest.toml"
        stamp = _lstat(config_path)
        if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISREG(stamp.st_mode):
            raise _problem(f"monorepo child config is unsafe: {declaration}", "unsafe-path")
        try:
            raw = config_path.read_bytes()
            config, _warnings = _parse_config(raw, current, config_path)
        except C.Problem:
            raise
        except (OSError, ValueError):
            raise _problem(f"monorepo child config is invalid: {declaration}") from None
        targets.append(ChildTarget(declaration=declaration, directory=current, config=config))
    return tuple(targets)


def route_scopes(scopes: tuple[str, ...], children: tuple[ChildTarget, ...]) -> RoutedChildRequest:
    if not scopes:
        raise _problem("a monorepo scope is required")
    by_name = {child.declaration: child for child in children}
    selected: str | None = None
    rebased: list[str] = []
    for scope in scopes:
        parts = _safe_segments(scope)
        child_name = parts[0]
        if len(parts) < 2 or child_name not in by_name:
            raise _problem("scope must select one declared monorepo child")
        if selected is not None and selected != child_name:
            raise _problem("scopes must select one monorepo child")
        selected = child_name
        rebased.append("/".join(parts[1:]))
    assert selected is not None
    return RoutedChildRequest(target=by_name[selected], scopes=tuple(rebased))


def execute_full(children: tuple[ChildTarget, ...], execute_child: Callable[[ChildTarget], int]) -> int:
    first_failure = 0
    for child in children:
        code = execute_child(child)
        if code and not first_failure:
            first_failure = code
    return first_failure
