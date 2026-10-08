"""Doctor Test policy facts (T2): static per-project coverage configuration facts.

Read-only and bounded: ``collect`` inspects a bounded set of config files,
never raises for repository content, spawns no processes, and writes nothing.
"""
from __future__ import annotations

import configparser
import os
import re
import shlex
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent_rules import TEST_POLICY_PATH, InstructionScan, coverage_instruction_lines
from . import contracts as C
from . import files

__all__ = [
    "CoverageGate",
    "ProjectPolicyFacts",
    "PolicyReport",
    "InstructionScan",
    "collect",
    "TEST_POLICY_PATH",
]

_FILE_BOUND = 256 * 1024
_MAX_NOTES = 10
_MAX_OMIT = 20
_MAX_OMIT_CHARS = 120

_PRAGMA_RE = re.compile(
    r"#\s*pragma:\s*no\s*cover", re.IGNORECASE)
_COV_FAIL_UNDER_EQ = re.compile(
    r"--cov-fail-under\s*=\s*(\S+)")
_TRUE_WORDS = {"true", "1", "yes", "on"}

_SKIP_DIR_NAMES = frozenset({
    "node_modules", "__pycache__", "site-packages", "venv", ".venv",
    "build", "dist",
})
_VITEST_CONFIG_RES = (
    re.compile(r"^vitest\.config\.(ts|mts|cts|js|mjs|cjs)$"),
    re.compile(r"^vitest\.workspace\.(ts|mts|cts|js|mjs|cjs)$"),
    re.compile(r"^vite\.config\.(ts|mts|cts|js|mjs|cjs)$"),
)

_PRAGMA_MAX_ENTRIES = 20_000
_PRAGMA_MAX_PY_FILES = 5_000
_PRAGMA_MAX_FILE_BYTES = 512 * 1024
_PRAGMA_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_PRAGMA_BUDGET_S = 2.0


@dataclass(frozen=True, slots=True)
class CoverageGate:
    source: str  # e.g. "pyproject.toml [tool.coverage.report] fail_under"
    value: str  # as written, e.g. "85"; never computed


@dataclass(frozen=True, slots=True)
class ProjectPolicyFacts:
    project: str  # "." or the v2 declaration
    gates: tuple[CoverageGate, ...] = ()
    branch: bool = False
    branch_sources: tuple[str, ...] = ()
    omit: tuple[str, ...] = ()  # <= 20 patterns, each <= 120 chars
    omit_total: int = 0
    pragma_count: int = 0
    pragma_complete: bool = True  # False when a scan bound stopped the count
    vitest_not_inspected: bool = False
    instructions: InstructionScan = field(default_factory=InstructionScan)
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PolicyReport:
    projects: tuple[ProjectPolicyFacts, ...] = ()
    root_instructions: InstructionScan | None = None  # monorepo root only; None for standalone
    policy_installed: bool = False


def _note(notes: list[str], text: str) -> None:
    if len(notes) < _MAX_NOTES:
        notes.append(text)


def _read_text(project_dir: Path, name: str) -> str | None:
    """File text or None when the file is simply absent.

    Raises _Unreadable when present but not inspectable (caller turns it
    into a plain note, never an exception).
    """
    try:
        stamp = os.lstat(project_dir / name)
    except FileNotFoundError:
        return None
    except OSError:
        raise _Unreadable(name)
    if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISREG(stamp.st_mode):
        raise _Unreadable(name)
    if stamp.st_size > _FILE_BOUND:
        raise _Unreadable(name)
    try:
        raw = bytes(files.read_regular(project_dir, name, _FILE_BOUND + 1))
    except (C.Problem, OSError, ValueError):
        raise _Unreadable(name)
    if len(raw) > _FILE_BOUND:
        raise _Unreadable(name)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _Unreadable(name)


class _Unreadable(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def _not_inspected_note(name: str, notes: list[str]) -> None:
    _note(notes, f"{name} could not be read; its coverage settings were "
                 "not inspected")


def _parse_fail_under(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _parse_branch(value: Any) -> bool | None:
    """True/False when the value states branch mode, else None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return None


def _split_omit(value: Any) -> list[str]:
    if isinstance(value, str):
        parts = value.replace(",", "\n").split("\n")
    elif isinstance(value, (list, tuple)):
        parts = [str(item) for item in value]
    else:
        return []
    return [part.strip() for part in parts if part.strip()]


def _toml_table(doc: Any, *keys: str) -> dict | None:
    node: Any = doc
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node if isinstance(node, dict) else None


def _ini_value(parser: configparser.ConfigParser, name: str, section: str,
               option: str, notes: list[str]) -> str | None:
    """One raw INI value or None when unreadable (never raises).

    pytest and coverage.py do not apply ``%`` interpolation, so the
    parser is built with ``interpolation=None``; this is a second net
    for any other read failure. A failure adds one per-file note and
    keeps every fact already gathered.
    """
    try:
        value = parser.get(section, option, fallback=None)
    except configparser.Error:
        _note(notes, f"{name} could not be parsed; its coverage "
                     "settings were not inspected")
        return None
    return value


def _gather_coverage_py(project_dir: Path, notes: list[str],
                        gates: list[CoverageGate],
                        branch_sources: list[str],
                        omit: list[str]) -> None:
    """coverage.py gates from pyproject/.coveragerc/setup.cfg/tox.ini (D12)."""
    import tomllib

    # pytest reads exactly one config file and pytest.ini always wins, so
    # when it exists the addopts of the other files are not in effect.
    try:
        pytest_ini_text = _read_text(project_dir, "pytest.ini")
        pytest_ini_present = pytest_ini_text is not None
    except _Unreadable as exc:
        _not_inspected_note(exc.name, notes)
        pytest_ini_text = None
        pytest_ini_present = True
    # TOML source first (pyproject), then INI sources in coverage.py order.
    try:
        text = _read_text(project_dir, "pyproject.toml")
    except _Unreadable as exc:
        _not_inspected_note(exc.name, notes)
        text = None
    if text is not None:
        try:
            doc = tomllib.loads(text)
        except (tomllib.TOMLDecodeError, ValueError):
            _note(notes, "pyproject.toml could not be parsed; its coverage "
                         "settings were not inspected")
            doc = None
        if doc is not None:
            report = _toml_table(doc, "tool", "coverage", "report") or {}
            run = _toml_table(doc, "tool", "coverage", "run") or {}
            value = _parse_fail_under(report.get("fail_under"))
            if value is not None:
                gates.append(CoverageGate(
                    "pyproject.toml [tool.coverage.report] fail_under",
                    value))
            branch = _parse_branch(run.get("branch"))
            if branch is True:
                branch_sources.append(
                    "pyproject.toml [tool.coverage.run] branch")
            for section in (run, report):
                omit.extend(_split_omit(section.get("omit")))
            addopts = _toml_table(doc, "tool", "pytest", "ini_options") or {}
            if not pytest_ini_present:
                _gather_addopts(addopts.get("addopts"), "pyproject.toml",
                                gates, branch_sources, notes)
    ini_specs = (
        (".coveragerc", "report", "run"),
        ("setup.cfg", "coverage:report", "coverage:run"),
        ("tox.ini", "coverage:report", "coverage:run"),
    )
    for name, report_section, run_section in ini_specs:
        try:
            text = _read_text(project_dir, name)
        except _Unreadable as exc:
            _not_inspected_note(exc.name, notes)
            continue
        if text is None:
            continue
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(text)
        except configparser.Error:
            _note(notes, f"{name} could not be parsed; its coverage "
                         "settings were not inspected")
            continue
        if parser.has_section(report_section):
            value = _parse_fail_under(
                _ini_value(parser, name, report_section, "fail_under",
                           notes))
            if value is not None:
                gates.append(CoverageGate(
                    f"{name} [{report_section}] fail_under", value))
        if parser.has_section(run_section):
            if _parse_branch(
                    _ini_value(parser, name, run_section, "branch",
                               notes)) is True:
                branch_sources.append(f"{name} [{run_section}] branch")
            for section in (run_section, report_section):
                if parser.has_section(section):
                    omit.extend(_split_omit(
                        _ini_value(parser, name, section, "omit", notes)))
    # pytest addopts from INI-style configs.
    addopts_specs = (
        ("pytest.ini", "pytest"),
        ("setup.cfg", "tool:pytest"),
        ("tox.ini", "pytest"),
    )
    for name, section in addopts_specs:
        if pytest_ini_present and name != "pytest.ini":
            continue
        if name == "pytest.ini":
            text = pytest_ini_text
        else:
            try:
                text = _read_text(project_dir, name)
            except _Unreadable:
                continue  # noted when read as a coverage source
        if text is None:
            continue
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(text)
        except configparser.Error:
            if name == "pytest.ini":
                _note(notes, "pytest.ini could not be parsed; its pytest "
                             "addopts were not inspected")
            continue  # setup.cfg/tox.ini: noted when read as coverage sources
        if parser.has_section(section):
            _gather_addopts(_ini_value(parser, name, section, "addopts",
                                       notes),
                            name, gates, branch_sources, notes)


def _gather_addopts(addopts: Any, source: str, gates: list[CoverageGate],
                    branch_sources: list[str], notes: list[str]) -> None:
    if addopts is None:
        return
    if isinstance(addopts, (list, tuple)):
        tokens = [str(item) for item in addopts]
    elif isinstance(addopts, str):
        try:
            tokens = shlex.split(addopts)
        except ValueError:
            _note(notes, f"{source} pytest addopts could not be split; "
                         "its coverage settings were not inspected")
            return
    else:
        return
    _gather_cov_tokens(tokens, f"{source} addopts --cov-fail-under",
                       f"{source} addopts --cov-branch",
                       gates, branch_sources, notes)


def _gather_cov_tokens(tokens: list[str], fail_source: str,
                       branch_source: str, gates: list[CoverageGate],
                       branch_sources: list[str], notes: list[str]) -> None:
    skip_next = False
    for index, token in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        if token == "--cov-branch":
            branch_sources.append(branch_source)
            continue
        match = _COV_FAIL_UNDER_EQ.search(token)
        if match is not None:
            gates.append(CoverageGate(fail_source, match.group(1)))
            continue
        if token == "--cov-fail-under":
            if index + 1 < len(tokens):
                gates.append(CoverageGate(fail_source, tokens[index + 1]))
                skip_next = True
            continue
    if any(token == "--cov-config" or token.startswith("--cov-config=")
           for token in tokens):
        _note(notes, "a pytest-cov --cov-config flag was seen; coverage.py "
                     "settings from that file were not inspected")


def _gather_runner_args(runner: Any, gates: list[CoverageGate],
                        branch_sources: list[str], notes: list[str]) -> None:
    for label, tokens in (("[runner] args", getattr(runner, "args", ())),
                          ("[runner] full_args",
                           getattr(runner, "full_args", ()))):
        try:
            token_list = [str(item) for item in tokens]
        except TypeError:
            continue
        _gather_cov_tokens(
            token_list, f"{label} --cov-fail-under",
            f"{label} --cov-branch", gates, branch_sources, notes)


def _scan_instructions(project_dir: Path, notes: list[str]) -> InstructionScan:
    try:
        return coverage_instruction_lines(Path(project_dir))
    except Exception:
        _note(notes, "instruction files were not inspected")
        return InstructionScan()


def _vitest_present(project_dir: Path, runner: Any) -> bool:
    kind = getattr(getattr(runner, "kind", None), "value",
                   getattr(runner, "kind", None))
    if kind == "vitest" or str(kind) == "vitest":
        return True
    try:
        entries = {entry.name for entry in os.scandir(project_dir)}
    except OSError:
        return False
    # Name match only: these configs are code and are never opened (D12).
    return any(pattern.match(name) is not None
               for pattern in _VITEST_CONFIG_RES for name in entries)


def _count_pragma(project_dir: Path, test_roots: tuple[str, ...],
                  notes: list[str]) -> tuple[int, bool]:
    """Count `# pragma: no cover` lines under project_dir (D12 bounds)."""
    roots: list[tuple[str, ...]] = []
    for root in test_roots:
        text = str(root).strip().strip("/")
        if text and text != ".":
            roots.append(tuple(text.split("/")))

    def _under_test_root(rel: Path) -> bool:
        parts = rel.parts
        return any(parts[:len(root)] == root for root in roots)
    count = 0
    entries = 0
    py_files = 0
    total_bytes = 0
    complete = True
    deadline = time.monotonic() + _PRAGMA_BUDGET_S
    stack = [Path(project_dir)]
    try:
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as iterator:
                    children = list(iterator)
            except OSError:
                continue
            for entry in children:
                entries += 1
                if entries > _PRAGMA_MAX_ENTRIES:
                    return count, False
                if time.monotonic() > deadline:
                    return count, False
                try:
                    is_symlink = entry.is_symlink()
                except OSError:
                    continue
                if is_symlink:
                    continue  # never follow symlinks
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                name = entry.name
                if is_dir:
                    if name.startswith(".") or name in _SKIP_DIR_NAMES \
                            or name.endswith(".egg-info"):
                        continue
                    try:
                        rel = Path(entry.path).relative_to(project_dir)
                    except ValueError:
                        continue
                    if _under_test_root(rel):
                        continue
                    stack.append(Path(entry.path))
                    continue
                if not name.endswith(".py"):
                    continue
                try:
                    rel = Path(entry.path).relative_to(project_dir)
                except ValueError:
                    continue
                if _under_test_root(rel):
                    continue
                py_files += 1
                if py_files > _PRAGMA_MAX_PY_FILES:
                    return count, False
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                if size > _PRAGMA_MAX_FILE_BYTES:
                    complete = False
                    continue
                total_bytes += size
                if total_bytes > _PRAGMA_MAX_TOTAL_BYTES:
                    return count, False
                if time.monotonic() > deadline:
                    return count, False
                try:
                    fd = os.open(entry.path,
                                 os.O_RDONLY | os.O_NOFOLLOW
                                 | os.O_NONBLOCK)
                except OSError:
                    continue  # symlink swapped in, FIFO, or unreadable
                try:
                    with os.fdopen(fd, "rb") as handle:
                        raw = handle.read(_PRAGMA_MAX_FILE_BYTES + 1)
                except OSError:
                    continue
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                count += sum(1 for line in text.split("\n")
                             if _PRAGMA_RE.search(line))
    except Exception:
        _note(notes, "pragma lines were not fully counted")
        return count, False
    if not complete:
        return count, False
    return count, True


def _project_facts(name: str, project_dir: Path,
                   runner: Any) -> ProjectPolicyFacts:
    notes: list[str] = []
    gates: list[CoverageGate] = []
    branch_sources: list[str] = []
    omit: list[str] = []
    try:
        _gather_coverage_py(project_dir, notes, gates, branch_sources, omit)
        if runner is not None:
            _gather_runner_args(runner, gates, branch_sources, notes)
        # D12 precedence note when gates come from more than one source.
        if len({gate.source for gate in gates}) > 1:
            _note(notes, "coverage.py reads only the first of "
                         "`.coveragerc`, `setup.cfg`, `tox.ini`, "
                         "`pyproject.toml` with coverage settings that "
                         "applies; a pytest-cov flag applies on top")
        seen: set[str] = set()
        unique_omit: list[str] = []
        for pattern in omit:
            if pattern not in seen:
                seen.add(pattern)
                unique_omit.append(pattern)
        omit_total = len(unique_omit)
        omit_shown = tuple(pattern[:_MAX_OMIT_CHARS]
                           for pattern in unique_omit[:_MAX_OMIT])
        test_roots: tuple[str, ...] = ()
        if runner is not None:
            try:
                test_roots = tuple(str(item)
                                   for item in runner.test_roots)
            except TypeError:
                test_roots = ()
        pragma_count, pragma_complete = _count_pragma(
            project_dir, test_roots, notes)
        vitest = _vitest_present(project_dir, runner)
        instructions = _scan_instructions(project_dir, notes)
        return ProjectPolicyFacts(
            project=name, gates=tuple(gates),
            branch=bool(branch_sources),
            branch_sources=tuple(branch_sources),
            omit=omit_shown, omit_total=omit_total,
            pragma_count=pragma_count, pragma_complete=pragma_complete,
            vitest_not_inspected=vitest, instructions=instructions,
            notes=tuple(notes))
    except Exception as exc:  # never raise for repository content
        _note(notes, f"project {name} could not be inspected ({exc})")
        return ProjectPolicyFacts(
            project=name,
            instructions=(InstructionScan()),
            notes=tuple(notes))


def _policy_installed(root: Path) -> bool:
    try:
        stamp = os.lstat(Path(root) / TEST_POLICY_PATH)
    except OSError:
        return False
    return stat.S_ISREG(stamp.st_mode)


def collect(resolution: C.ConfigResolution) -> PolicyReport:
    """Collect static Test policy facts for doctor (read-only, bounded)."""
    try:
        root = Path(resolution.root)
    except Exception:
        return PolicyReport()
    try:
        manifest = getattr(resolution, "monorepo", None)
        declarations = tuple(getattr(manifest, "children", ()) or ())
    except Exception:
        declarations = ()
    try:
        if declarations:
            # v2: children only (like executability.check_resolution); the
            # root itself is covered by root_instructions below.
            from .config import resolve_config

            projects: list[ProjectPolicyFacts] = []
            for declaration in declarations:
                try:
                    child = resolve_config(root / declaration)
                    runner = (child.config.runner
                              if child.config is not None else None)
                except Exception:
                    runner = None
                projects.append(_project_facts(
                    str(declaration), root / declaration, runner))
            root_scan = _scan_instructions(root, [])
            return PolicyReport(
                projects=tuple(projects), root_instructions=root_scan,
                policy_installed=_policy_installed(root))
        runner = (resolution.config.runner
                  if resolution.config is not None else None)
        project = _project_facts(".", root, runner)
        return PolicyReport(
            projects=(project,), root_instructions=None,
            policy_installed=_policy_installed(root))
    except Exception:
        return PolicyReport()
