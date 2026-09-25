"""Repo, config, monorepo and venv factories (test-only, T3 owned).

Function-scoped factory fixtures; every resource lives under the test's
``tmp_path`` (via the ``parent`` override when a test needs a specific
base).  Unknown keyword arguments raise ``TypeError`` through the
explicit signatures (or through :func:`support.ptest_toml_text` for the
``**toml`` passthrough).

Fixtures (all function-scoped, all names globally unique):

* ``git_repo`` — ``git init`` plus an optional commit; dirty state is
  made by writing files after creation.
* ``ptest_project`` — a project directory with a rendered ``.ptest.toml``
  (``**toml`` goes to :func:`support.ptest_toml_text`), optional extra
  files and an optional ``git init`` + commit.
* ``monorepo`` — a version-2 dispatcher root plus child projects, each
  child either ``**toml`` kwargs, a literal TOML string, or ``None``
  for the default command project.
* ``venv_stub`` — a ``.venv`` with ``pyvenv.cfg`` and
  ``lib/python3.12/site-packages`` dist-info markers for the parallel
  tier's environment probes.

Plain helpers (not fixtures):

* ``ptest_toml`` — render a ``.ptest.toml`` from kwargs.
* ``fake_git_marker`` — detection-only fake ``.git`` directory (no git
  binary, no commits); for tests that only need repository detection,
  never for change-classification tests.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from support import (
    init_git_repo,
    write_file,
    write_ptest_toml,
)


def _child_root(base: Path | None, name: str | None, tmp_path: Path) -> Path:
    """Resolve the factory target directory from ``parent``/``name``."""
    base_path = Path(base) if base is not None else tmp_path
    return base_path if name is None else base_path / name


def _check_relative(relative) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"file key must be a relative path: {relative!r}")
    return path


def ptest_toml(root, **kwargs) -> Path:
    """Render a ``.ptest.toml`` in ``root`` from kwargs.

    Keyword arguments go verbatim to :func:`support.ptest_toml_text`,
    which raises ``TypeError`` on unknown keys.  Returns the toml path.
    """
    return write_ptest_toml(Path(root), **kwargs)


def fake_git_marker(root) -> Path:
    """Create a detection-only fake ``.git`` directory under ``root``.

    Writes ``HEAD`` (``ref: refs/heads/main``) and a minimal ``config``.
    No git binary runs and no commit exists, so change classification
    against this marker fails closed; use :func:`git_repo` (via the
    ``git_repo`` fixture) whenever a test needs real git behaviour.
    """
    root = Path(root)
    marker = root / ".git"
    marker.mkdir(parents=True, exist_ok=True)
    write_file(marker / "HEAD", "ref: refs/heads/main\n")
    write_file(marker / "config", "[core]\n\trepositoryformatversion = 0\n")
    return marker


@pytest.fixture
def git_repo(tmp_path):
    """Create a real git repo: ``git init -b branch`` + optional commit.

    ``files`` maps relative paths to ``str``/``bytes`` content and is
    committed when ``commit`` is true.  Dirty state is made by writing
    files after creation (see :func:`support.write_file` and
    :func:`support.git_commit_all`).
    """
    def make(name: str = "repo", *, parent=None, files=None,
             commit: bool = True, branch: str = "main") -> Path:
        root = _child_root(parent, name, tmp_path)
        return init_git_repo(root, files=files, branch=branch,
                             commit=commit)
    return make


@pytest.fixture
def ptest_project(tmp_path):
    """Create a project directory with a rendered ``.ptest.toml``.

    ``**toml`` goes verbatim to :func:`support.ptest_toml_text`
    (``kind``, ``launcher``, ``args``, ``full_args``, ``test_roots``,
    ``workers``, ``project_id``, ``runner_extra``, ``setup``, ``tail``).
    ``files`` maps relative paths to ``str``/``bytes`` content.
    ``git=True`` runs ``git init -b branch`` and commits everything.
    """
    def make(name: str = "proj", *, parent=None, git: bool = False,
             files=None, branch: str = "main", **toml) -> Path:
        root = _child_root(parent, name, tmp_path)
        root.mkdir(parents=True, exist_ok=True)
        write_ptest_toml(root, **toml)
        for relative, content in (files or {}).items():
            write_file(root / _check_relative(relative), content)
        if git:
            init_git_repo(root, branch=branch)
        return root
    return make


@pytest.fixture
def monorepo(tmp_path):
    """Create a version-2 dispatcher root with child projects.

    ``children`` maps a child name to its ``.ptest.toml`` spec: a dict
    of :func:`support.ptest_toml_text` kwargs, a literal TOML string
    (byte-identical inputs where defaults would change behaviour), or
    ``None`` for the default command project.  ``root_toml`` overrides
    the rendered dispatcher manifest (default lists the children in
    order).  ``git=True`` commits the whole tree.  ``name=None`` uses
    ``parent`` itself as the dispatcher root.
    """
    def make(children: dict, *, parent=None, name: str | None = "mono",
             git: bool = False, branch: str = "main",
             root_toml: str | None = None) -> Path:
        names = list(children)
        root = _child_root(parent, name, tmp_path)
        root.mkdir(parents=True, exist_ok=True)
        if root_toml is None:
            listing = ", ".join(json.dumps(child) for child in names)
            root_toml = f"version = 2\n[monorepo]\nchildren = [{listing}]\n"
        write_file(root / ".ptest.toml", root_toml)
        for child, spec in children.items():
            child_dir = root / _check_relative(child)
            child_dir.mkdir(parents=True, exist_ok=True)
            if spec is None:
                write_ptest_toml(child_dir)
            elif isinstance(spec, str):
                write_file(child_dir / ".ptest.toml", spec)
            elif isinstance(spec, dict):
                write_ptest_toml(child_dir, **spec)
            else:
                raise TypeError(
                    "monorepo child spec must be a dict, str or None: "
                    f"{child!r}")
        if git:
            init_git_repo(root, branch=branch)
        return root
    return make


#: Python minor layout the parallel-tier environment probes accept.
_VENV_SITE_PARENT = Path("lib") / "python3.12" / "site-packages"


@pytest.fixture
def venv_stub(tmp_path):
    """Create a ``.venv`` stub under ``root`` for environment probes.

    Writes ``pyvenv.cfg`` plus ``lib/python3.12/site-packages`` with
    one ``<prefix>-<version>.dist-info`` directory per requested
    package: ``xdist`` → ``pytest_xdist`` (a single version string or
    an iterable of them, so duplicate-install fallbacks stay
    expressible), ``pytest`` → ``pytest``, ``pytest_cov`` →
    ``pytest_cov``, ``coverage`` → ``coverage``.  Returns the venv path.
    """
    def make(root, *, pytest=None, pytest_cov=None,
             coverage=None, xdist=None) -> Path:
        venv = Path(root) / ".venv"
        site = venv / _VENV_SITE_PARENT
        site.mkdir(parents=True, exist_ok=True)
        write_file(venv / "pyvenv.cfg", "home = /usr/bin\n")
        markers: list[tuple[str, object]] = [
            ("pytest-", pytest),
            ("pytest_cov-", pytest_cov),
            ("coverage-", coverage),
        ]
        for prefix, version in markers:
            if version is not None:
                (site / f"{prefix}{version}.dist-info").mkdir(exist_ok=True)
        if xdist is not None:
            versions = (xdist,) if isinstance(xdist, str) else tuple(xdist)
            for version in versions:
                (site / f"pytest_xdist-{version}.dist-info").mkdir(
                    exist_ok=True)
        return venv
    return make


__all__ = [
    "ptest_toml",
    "fake_git_marker",
    "git_repo",
    "ptest_project",
    "monorepo",
    "venv_stub",
]
