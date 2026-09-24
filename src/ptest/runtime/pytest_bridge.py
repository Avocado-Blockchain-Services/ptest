"""Dependency-light entry point executed by the selected project interpreter.

The module imports only the standard library until :func:`run` has validated
the generated protocol descriptor.  It intentionally has no ptest imports so
the target interpreter need only provide pytest itself.
"""
from __future__ import annotations

import json
import hashlib
import importlib.metadata
import os
import re
import shlex
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


class BridgeRefusal(RuntimeError):
    """A bridge-owned refusal, distinct from an exception in repository code."""

    def __init__(self, message: str, code: str = "native-config-invalid") -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


_REPORT_NAME = re.compile(r"native-a(00[1-9]|010)-[0-9a-f]{32}\.json\Z")
_COVERAGE_TUPLE = ("7.1.0", "7.15.0")


def _coverage_tuple() -> tuple[str, str]:
    """Require the frozen pytest-cov/coverage pair before native tests run."""
    try:
        pytest_cov = importlib.metadata.version("pytest-cov")
        coverage = importlib.metadata.version("coverage")
    except importlib.metadata.PackageNotFoundError:
        _fail("the frozen pytest-cov/coverage tuple is unavailable", "unsupported-capability")
    if (pytest_cov, coverage) != _COVERAGE_TUPLE:
        _fail(
            "pytest-cov/coverage versions are outside the frozen qualification tuple",
            "unsupported-capability",
        )
    return pytest_cov, coverage


def _report_limits() -> tuple[int, int, int, int]:
    """Read native report bounds from the frozen descriptor, never literals."""
    descriptor = Path(__file__).with_name("protocol-v1.json")
    try:
        value = json.loads(descriptor.read_text(encoding="utf-8"))
        limits = value["limits"]
        max_bytes = limits["native_report_max_bytes"]
        max_tests = limits["native_report_max_tests"]
        max_test_id_bytes = limits["test_id_max_bytes"]
        max_events = value["bridge_event"]["max_events"]
        if (type(max_bytes) is not int or type(max_tests) is not int
                or type(max_test_id_bytes) is not int
                or type(max_events) is not int or max_bytes <= 0
                or max_tests <= 0 or max_test_id_bytes <= 0
                or max_events <= 0):
            raise ValueError
        return max_bytes, max_tests, max_events, max_test_id_bytes
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        raise BridgeRefusal("native report descriptor limits are unavailable", "state-unavailable")


def _fail(message: str, code: str = "native-config-invalid") -> None:
    raise BridgeRefusal(message, code)


def _refusal_marker(code: str, message: str) -> None:
    # No argv, paths or project data: distinguish owned refusals from native exit 4.
    print("ptest-bridge-refusal: " + json.dumps({"code": code, "message": message}), file=sys.stderr)


def _protocol() -> dict[str, Any]:
    name = os.environ.get("PTEST_BRIDGE_PROTOCOL")
    if not name:
        _fail("missing protocol descriptor")
    try:
        raw = Path(name).read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        _fail("unreadable protocol descriptor")
    if not isinstance(data, dict) or data.get("protocol") != 1:
        _fail("unsupported protocol descriptor")
    return data


def _workers() -> int:
    value = os.environ.get("PTEST_GRANT_WORKERS")
    try:
        workers = int(value or "")
    except ValueError:
        _fail("invalid granted worker count")
    if workers < 1 or workers > 64:
        _fail("invalid granted worker count")
    return workers


def _python_version() -> None:
    """Keep the tested interpreter matrix explicit before importing pytest."""
    version = sys.version_info
    if sys.implementation.name != "cpython" or version[0] != 3 or not 11 <= version[1] <= 14:
        _fail("unsupported CPython version or implementation", "unsupported-capability")


def _validate_full_roots(roots: tuple[str, ...]) -> None:
    if not roots or len(set(roots)) != len(roots):
        _fail("full pytest roots must be nonempty and unique")
    for root in roots:
        if (not isinstance(root, str) or not root or root == "."
                or root.startswith(("-", "@", "/")) or "\\" in root
                or "::" in root
                or any(part in {"", ".", ".."} for part in root.split("/"))):
            _fail("full pytest roots must be literal project-relative paths")


def _selected_files() -> tuple[str, ...]:
    raw = os.environ.get("PTEST_PYTEST_SELECTED_FILES")
    if not raw:
        _fail("selected pytest execution has no exact file binding")
    try:
        files = tuple(json.loads(raw))
    except (TypeError, ValueError):
        _fail("selected pytest files are invalid")
    if (not files or len(files) > 256 or len(set(files)) != len(files)
            or any(not isinstance(path, str) or not path
                   or path.startswith(("-", "@", "/", "\\"))
                   or "\x00" in path or "::" in path
                   or any(part in {"", ".", ".."}
                          for part in path.replace("\\", "/").split("/"))
                   for path in files)):
        _fail("selected pytest files are invalid")
    checkout = os.environ.get("PTEST_PYTEST_CHECKOUT_ROOT")
    if not checkout:
        _fail("selected pytest files have no admitted checkout")
    try:
        root = os.path.realpath(checkout)
        for path in files:
            resolved = os.path.realpath(os.path.join(root, path))
            if (os.path.commonpath((root, resolved)) != root
                    or not os.path.isfile(resolved)):
                _fail("selected pytest file is outside the admitted checkout")
    except (OSError, ValueError):
        _fail("selected pytest file is outside the admitted checkout")
    return files


_FULL_REDIRECT_OPTIONS = {
    "-c", "--config-file", "--rootdir", "--confcutdir", "--noconftest",
    "--pyargs", "-o", "--override-ini", "--basetemp",
}
_FULL_NARROWING_OPTIONS = {
    "-k", "--keyword", "-m", "--markexpr", "--deselect", "--lf",
    "--last-failed", "--ff", "--failed-first", "--sw", "--stepwise",
    "--sw-skip", "--stepwise-skip", "--testmon", "--ignore", "--ignore-glob",
    "--collect-only", "--co", "--maxfail", "-x", "--setup-only", "--setup-plan",
    "--fixtures", "--funcargs", "--fixtures-per-test", "--markers",
    "--cache-show", "-h", "--help", "-V", "--version",
}
# Pytest 9 implements --strict, --strict-config and --strict-markers through
# OverrideIniAction, which appends exactly these entries to option.override_ini.
# They only tighten native config/marker strictness, so full mode preserves
# them; every other override_ini value remains a redirect refusal.
_SAFE_STRICT_OVERRIDES = frozenset({
    "strict=true", "strict_config=true", "strict_markers=true",
})


def _short_redirect_cluster(token: str) -> bool:
    """Recognise value-taking ``-c``/``-o`` inside a short-option cluster.

    The single cluster rule shared with :func:`cluster_narrow_name`:
    the scan stops at the first value-taking letter, so a ``k``/``m``-led
    cluster (``-kfoo``, ``-mnot_slow``) carries an attached expression and
    is never a redirect, and a ``c``/``o`` after that letter is part of
    the attached value. A ``c``/``o`` at or before it (``-co``, ``-vc``)
    still redirects.
    """
    if (not token.startswith("-") or token.startswith("--")
            or token.startswith("-W")):
        return False
    body = token[1:]
    if len(body) <= 1:
        return False
    for letter in body:
        if letter in ("c", "o"):
            return True
        if letter in _VALUE_FLAG_LEADS:
            return False
    return False


def _node_id_token(tokens: tuple[str, ...], index: int) -> bool:
    """Treat ``::`` as a node id only when it is a positional token.

    Warning filters use the same separator (for example ``-W
    error::DeprecationWarning``), but are option values rather than inventory
    selectors and must not be rejected as full-mode narrowing.
    """
    token = tokens[index]
    if "::" not in token or token.startswith("-"):
        return False
    return index == 0 or tokens[index - 1] not in {"-W", "--pythonwarnings"}


def _split_addopts(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        try:
            return tuple(shlex.split(value, posix=True))
        except ValueError:
            _fail("pytest addopts contains malformed quoting")
    if isinstance(value, (tuple, list)) and all(isinstance(item, str) for item in value):
        return tuple(value)
    _fail("pytest addopts has an invalid shape")


def _checked_in_addopts(config: Any) -> tuple[str, ...]:
    """Return addopts from the project's checked-in pytest configuration.

    This is only the ini value: ``PYTEST_ADDOPTS`` from the environment is a
    separate invocation-time source and stays refused in full mode.
    """
    try:
        configured = config.getini("addopts")
    except (AttributeError, ValueError, TypeError):
        configured = ()
    return _split_addopts(configured)


def full_redirect_name(tokens: tuple[str, ...], index: int) -> str | None:
    """Display name when full mode refuses ``tokens[index]`` as a redirect.

    Redirects (``-c``/``--rootdir``/``--confcutdir``/``-o`` family, argfiles,
    node ids, redirect clusters) can silently replace the project's
    checked-in configuration, so they stay refused even inside checked-in
    addopts. Plain narrowing filters, including short-flag clusters (which
    can only respell narrowing/boolean flags, never redirect
    configuration), are project-owned in that position and are allowed
    there instead (recorded in the run label).
    """
    token = tokens[index]
    option = token.split("=", 1)[0]
    if option in _FULL_REDIRECT_OPTIONS:
        return option
    if _short_redirect_cluster(token):
        return token
    if token.startswith("@"):
        return token
    if _node_id_token(tokens, index):
        return token
    return None


# Section F MEDIUM allowlist: the only narrowing spellings a project's own
# checked-in configuration may contribute to a full run. Everything else
# that narrows or observes instead of running (``--co``, ``--lf``, ``--sw``,
# ``--testmon``, ``--setup-only``, ``--fixtures``, ``--markers``,
# ``--cache-show``, ``-h``, ``-V``) stays refused even from checked-in
# configuration. Short clusters (``-vx``, ``-vk EXPR``) narrow only through
# ``x``/``k``/``m``, which are all allowlisted, so they stay allowed.
_FULL_INI_ALLOWED = frozenset({
    "-k", "--keyword", "-m", "--markexpr", "--deselect",
    "--ignore", "--ignore-glob", "--maxfail", "-x", "--exitfirst",
})

# Effective pytest option attributes a checked-in allowlisted spelling may
# supply. ``pyargs`` is intentionally absent: it redirects native
# configuration and is never project-owned narrowing.
_FULL_INI_ALLOWED_ATTRS = frozenset({
    "keyword", "markexpr", "deselect", "ignore", "ignore_glob", "maxfail",
})


# Narrowing filters whose expression arrives as the next token, so the run
# label can record the filter with its value (``-m not slow``).
_FULL_VALUE_FILTERS = frozenset({
    "-k", "--keyword", "-m", "--markexpr", "--deselect",
    "--ignore", "--ignore-glob", "--maxfail",
})


def full_ini_refusal_name(tokens: tuple[str, ...], index: int) -> str | None:
    """Display name when full mode refuses ``tokens[index]`` from checked-in config.

    Redirects are excluded here (they stay refused but are reported
    separately); only non-allowlisted narrowing/observation spellings are
    named. Returns None when the token is project-owned in this position.
    """
    if full_redirect_name(tokens, index) is not None:
        return None
    refusal = full_refusal_name(tokens, index)
    if refusal is None:
        return None
    if refusal in _FULL_INI_ALLOWED or cluster_narrow_name(tokens[index]) is not None:
        return None
    return refusal


def full_narrowing_text(tokens: tuple[str, ...]) -> str | None:
    """Render checked-in narrowing filters for the full-mode project label.

    Only tokens the full gate allows from checked-in configuration are
    rendered; redirects and non-allowlisted narrowing are excluded (they
    stay refused). Returns None when no narrowing filter is present.
    """
    parts: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if (full_refusal_name(tokens, index) is None
                or full_redirect_name(tokens, index) is not None
                or full_ini_refusal_name(tokens, index) is not None):
            index += 1
            continue
        option = token.split("=", 1)[0]
        if ("=" not in token
                and (option in _FULL_VALUE_FILTERS
                     or (cluster_narrow_name(token) is not None
                         and token[-1:] in ("k", "m")))
                and index + 1 < len(tokens)):
            parts.append(f"{token} {tokens[index + 1]}")
            index += 2
            continue
        parts.append(token)
        index += 1
    return "; ".join(parts) if parts else None


# Effective pytest option attribute to the CLI spellings that set it, so
# the full gate can tell checked-in narrowing apart from invocation-time
# narrowing. ``pyargs`` is intentionally absent: it redirects native
# configuration and is never project-owned narrowing.
_NARROWING_ATTR_FLAGS: dict[str, tuple[str, ...]] = {
    "keyword": ("-k", "--keyword"),
    "markexpr": ("-m", "--markexpr"),
    "deselect": ("--deselect",),
    "lf": ("--lf", "--last-failed"),
    "failedfirst": ("--ff", "--failed-first"),
    "stepwise": ("--sw", "--stepwise"),
    "stepwise_skip": ("--sw-skip", "--stepwise-skip"),
    "testmon": ("--testmon",),
    "ignore": ("--ignore",),
    "ignore_glob": ("--ignore-glob",),
    "maxfail": ("--maxfail", "--exitfirst", "-x"),
    "collectonly": ("--collect-only", "--co"),
    "setuponly": ("--setup-only",),
    "setupplan": ("--setup-plan",),
    "showfixtures": ("--fixtures",),
    "show_fixtures_per_test": ("--fixtures-per-test",),
    "funcargs": ("--funcargs",),
    "markers": ("--markers",),
    "cacheshow": ("--cache-show",),
    "help": ("-h", "--help"),
    "version": ("-V", "--version"),
}


def _flag_supplies(tokens: tuple[str, ...], attr: str) -> bool:
    """True when ``tokens`` spell the narrowing flag behind ``attr``.

    Covers exact flags, ``--flag=value`` forms, attached short values
    (``-kEXPR``), trailing-``k``/``m`` clusters (``-vk EXPR``), and
    clusters respelling ``-x`` — the same vocabulary
    :func:`full_refusal_name` and :func:`full_narrowing_text` classify.
    """
    for token in tokens:
        if not isinstance(token, str):
            continue
        option = token.split("=", 1)[0]
        if option in _NARROWING_ATTR_FLAGS.get(attr, ()):
            return True
        if attr in ("keyword", "markexpr"):
            short = "-k" if attr == "keyword" else "-m"
            if (token.startswith(short) and len(token) > 2
                    and not token.startswith("--")):
                return True
            if (cluster_narrow_name(token) is not None
                    and token.endswith(short[1:])):
                return True
        if (attr == "maxfail" and cluster_narrow_name(token) is not None
                and "x" in token):
            return True
    return False


def _ini_flag_values(tokens: tuple[str, ...], attr: str) -> list[str]:
    """Raw values the checked-in tokens supply for a narrowing attribute.

    Mirrors the spellings :func:`_flag_supplies` classifies (``--flag=value``,
    ``--flag value``, attached ``-kEXPR``/``-mEXPR``, trailing-``k``/``m``
    clusters, ``-x``/``--exitfirst`` counting as maxfail 1) so the effective
    option value can be compared against its checked-in source instead of
    its spelling. A conftest ``pytest_configure`` mutation therefore fails
    closed instead of passing on another spelling of the same flag.
    """
    flags = _NARROWING_ATTR_FLAGS.get(attr, ())
    short = {"keyword": "k", "markexpr": "m"}.get(attr)
    out: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        option = token.split("=", 1)[0]
        if "=" in token and option in flags:
            out.append(token.split("=", 1)[1])
        elif (short is not None and token.startswith("-" + short)
                and len(token) > 2 and not token.startswith("--")):
            out.append(token[2:])
        elif option in flags:
            if attr == "maxfail" and option in ("-x", "--exitfirst"):
                out.append("1")
            elif index + 1 < len(tokens):
                out.append(tokens[index + 1])
        elif (short is not None
                and cluster_narrow_name(token) is not None
                and token.endswith(short)):
            if index + 1 < len(tokens):
                out.append(tokens[index + 1])
        elif (attr == "maxfail" and cluster_narrow_name(token) is not None
                and "x" in token):
            out.append("1")
        index += 1
    return out


def _ini_value_matches(name: str, value: Any, tokens: tuple[str, ...]) -> bool:
    """True when the effective narrowing option equals its checked-in source."""
    raw = _ini_flag_values(tokens, name)
    if name in ("keyword", "markexpr"):
        return isinstance(value, str) and value in raw
    if name == "maxfail":
        try:
            return int(value) in [int(item) for item in raw]
        except (TypeError, ValueError):
            return False
    if name in ("deselect", "ignore", "ignore_glob"):
        if not isinstance(value, (list, tuple)):
            return False
        return sorted(str(item) for item in value) == sorted(raw)
    return False


# Short flags that take a value: a cluster starting with one carries an
# attached value (``-kEXPR``, ``-n2``, ``-Werror``), so the cluster rule
# below leaves it to the attached-expression classifiers.  The set is
# pytest's own short-option table (``pytest --help``: -c FILE, -k
# EXPRESSION, -m MARKEXPR, -n (xdist), -o OVERRIDE_INI, -p name, -r chars,
# -W PYTHONWARNINGS); every other short flag is boolean or a counter.
_VALUE_FLAG_LEADS = frozenset({"W", "c", "k", "m", "n", "o", "p", "r"})


def cluster_narrow_name(token: str) -> str | None:
    """Display name when a short-option cluster narrows full mode, else None.

    The single cluster rule shared by the bridge and ``adapters/pytest``:
    only the letters before the first value-taking flag can hide the
    boolean ``x`` flag (``-vrx`` is ``-v`` plus ``-r x``, but ``-xvr``
    still narrows), and a cluster whose first value-taking letter is a
    trailing ``k``/``m`` whose expression arrives as the next token
    (``-vk foo`` narrows, but ``-vrk`` is ``-v`` plus ``-r`` with char
    ``k``) narrows the inventory. Attached values (``-kEXPR``, ``-n2``)
    are classified elsewhere, as are clusters led by a value-taking flag.
    """
    if not token.startswith("-") or token.startswith("--"):
        return None
    body = token[1:]
    if not body or not body.isalpha():
        return None
    if len(body) == 1:
        return token if body == "x" else None
    if body[0] in _VALUE_FLAG_LEADS:
        return None
    prefix = body
    first_value_index = None
    for index, letter in enumerate(body):
        if letter in _VALUE_FLAG_LEADS:
            prefix = body[:index]
            first_value_index = index
            break
    if "x" in prefix:
        return token
    if first_value_index == len(body) - 1 and body[-1] in ("k", "m"):
        return token
    return None


def full_refusal_name(tokens: tuple[str, ...], index: int) -> str | None:
    """Display name when full mode refuses ``tokens[index]``, else None.

    This is the token-level classification behind
    :func:`_reject_full_addopts`, exposed so static checks (executability)
    derive the same verdict instead of a second list. ``--exitfirst`` is
    included because parsed pytest maps it to ``maxfail=1``, which the
    option check below refuses; short clusters go through the one shared
    :func:`cluster_narrow_name` rule.
    """
    token = tokens[index]
    option = token.split("=", 1)[0]
    redirect_cluster = _short_redirect_cluster(token)
    short_narrow = len(token) > 2 and token.startswith(("-k", "-m"))
    maxfail_zero = (option == "--maxfail" and (
        token.partition("=")[2] == "0"
        or ("=" not in token and index + 1 < len(tokens) and tokens[index + 1] == "0")
    ))
    if maxfail_zero:
        return None
    if token.startswith("@"):
        return token
    if option in _FULL_REDIRECT_OPTIONS or option in _FULL_NARROWING_OPTIONS:
        return option
    if option == "--exitfirst":
        return option
    if redirect_cluster:
        return token
    if short_narrow:
        return token[:2]
    if _node_id_token(tokens, index):
        return token
    if cluster_narrow_name(token) is not None:
        return token
    return None


def _reject_full_addopts(tokens: tuple[str, ...]) -> None:
    for index in range(len(tokens)):
        if full_refusal_name(tokens, index) is not None:
            _fail("full pytest plans cannot accept addopts narrowing or configuration redirects")


def _reject_full_ini_addopts(tokens: tuple[str, ...]) -> None:
    """Refuse checked-in addopts that redirect native configuration.

    Only the allowlisted narrowing filters (``-k``/``-m``/``--deselect``/
    ``--ignore``/``--ignore-glob``/``-x``/``--exitfirst``/``--maxfail``) are
    the project's own suite definition in this position: they are allowed
    and recorded in the run label instead of refused. Everything else that
    narrows or observes stays refused.
    """
    for index in range(len(tokens)):
        if full_redirect_name(tokens, index) is not None:
            _fail("full pytest plans cannot accept addopts narrowing or configuration redirects")
        if full_ini_refusal_name(tokens, index) is not None:
            _fail("full pytest plans cannot accept addopts narrowing or configuration redirects")


def _validate_native_paths(config: Any, checkout_root: str | None,
                           config_path: str | None) -> None:
    """Bind Pytest's effective config paths to the admitted checkout."""
    if not checkout_root:
        return
    try:
        expected = os.path.realpath(checkout_root)
    except (TypeError, ValueError):
        _fail("pytest checkout root binding is invalid")
    if not os.path.isabs(checkout_root) or not expected:
        _fail("pytest checkout root binding is invalid")

    def real(value: object) -> str:
        try:
            text = os.fspath(value)
        except TypeError:
            _fail("pytest native configuration paths are invalid")
        if not isinstance(text, str) or not os.path.isabs(text):
            _fail("pytest native configuration paths are invalid")
        return os.path.realpath(text)

    option = getattr(config, "option", None)
    effective_roots = []
    # ``option.rootdir`` is the effective command-line value.  The public
    # ``config.rootdir`` attribute can be stale or replaced by a plugin, so it
    # is deliberately not trusted for redirect detection.
    for value in (getattr(option, "rootdir", None), getattr(config, "rootpath", None)):
        if value is None:
            continue
        native_root = real(value)
        try:
            inside = os.path.commonpath((expected, native_root)) == expected
        except ValueError:
            inside = False
        if not inside:
            _fail("pytest native configuration paths are outside the admitted checkout")
        effective_roots.append(native_root)
    if effective_roots and len(set(effective_roots)) != 1:
        _fail("pytest native configuration root metadata disagrees")

    inipath = getattr(config, "inipath", None)
    inifilename = getattr(option, "inifilename", None)
    if inipath is None:
        if inifilename not in (None, ""):
            _fail("pytest native configuration paths have no matching ini path")
    else:
        ini = real(inipath)
        if os.path.commonpath((expected, ini)) != expected:
            _fail("pytest native configuration paths are outside the admitted checkout")
        if inifilename in (None, ""):
            inifilename = os.path.basename(ini)
        if not isinstance(inifilename, str) or inifilename != os.path.basename(ini):
            _fail("pytest native configuration paths have mismatched ini metadata")
        try:
            if os.path.islink(os.fspath(inipath)) or not os.path.isfile(os.fspath(inipath)):
                _fail("pytest native configuration ini path is not a regular file")
        except OSError:
            _fail("pytest native configuration ini path is unavailable")

    if config_path:
        configured = real(config_path)
        if os.path.commonpath((expected, configured)) != expected:
            _fail("pytest ptest configuration path is outside the admitted checkout")


def _report_binding() -> tuple[Path, dict[str, str]] | None:
    """Read the executor-owned report binding, when this is an admitted run."""
    path_value = os.environ.get("PTEST_PYTEST_REPORT_PATH")
    profile_value = os.environ.get("PTEST_PYTEST_PROFILE")
    # A normal direct bridge call has no executor report binding.  Inherited
    # identity hints alone must not turn it into a malformed report run; only
    # an explicitly allocated path enters the authenticated report path.
    markers = (
        path_value, os.environ.get("PTEST_RUN_ID"),
        os.environ.get("PTEST_GRANT_NONCE"), os.environ.get("PTEST_PYTEST_ATTEMPT"),
        os.environ.get("PTEST_PYTEST_EXECUTION"),
        # The basic-serial label is a harmless default inherited by direct
        # bridge unit tests; only an attempted raw advanced switch enters the
        # authenticated report path without an allocated report target.
        profile_value if profile_value == "advanced" else None,
    )
    if not any(value is not None for value in markers):
        return None
    if (not path_value or not os.environ.get("PTEST_PYTEST_ATTEMPT")
            or not os.environ.get("PTEST_RUN_ID")
            or not os.environ.get("PTEST_GRANT_NONCE")):
        _fail("executor report identity is incomplete")
    if not os.path.isabs(path_value):
        _fail("executor must bind a private pytest report path")
    path = Path(path_value)
    if _REPORT_NAME.fullmatch(path.name) is None:
        _fail("executor report basename is invalid")
    run_id = os.environ.get("PTEST_RUN_ID", "")
    nonce = os.environ.get("PTEST_GRANT_NONCE", "")
    attempt = os.environ.get("PTEST_PYTEST_ATTEMPT", "")
    execution = os.environ.get("PTEST_PYTEST_EXECUTION", "")
    profile = os.environ.get("PTEST_PYTEST_PROFILE", "basic_serial")
    if (not re.fullmatch(r"[0-9a-f]{32}", run_id)
            or not re.fullmatch(r"[0-9a-f]{64}", nonce)
            or not re.fullmatch(r"a(00[1-9]|010)", attempt)
            or execution not in {"scoped", "selected", "full"}
            or profile not in {"basic_serial", "advanced"}):
        _fail("invalid pytest report identity")
    if not path.parent.is_dir():
        _fail("pytest report directory is unavailable")
    declared_execution = os.environ.get("PTEST_EXECUTION", execution)
    if declared_execution != execution:
        _fail("pytest report identity does not match the execution binding")
    try:
        parent = path.parent
        stamp = parent.stat()
        if stamp.st_uid != os.getuid() or stamp.st_mode & 0o077:
            _fail("pytest report directory is not private")
        if os.path.realpath(parent) != str(parent):
            _fail("pytest report directory is not private")
    except OSError:
        _fail("pytest report directory is unavailable")
    return path, {
        "run_id": run_id, "nonce": nonce, "attempt_id": attempt,
        "execution_mode": execution,
        "effective_profile": profile,
    }


def _empty_narrowing() -> dict[str, Any]:
    """Blank bridge-owned narrowing report for unfiltered or refused runs."""
    return {"narrowing": None, "conftest_hooks": [], "notes": []}


def _write_report(path: Path, identity: dict[str, str], *, runtime: str,
                  native_exit: int | None, bridge_exit: int,
                  complete: bool, problem: str | None,
                  project_narrowing: dict[str, Any] | None = None) -> None:
    payload = {
        "protocol": 1,
        **identity,
        "runner": "pytest",
        "observed_runtime_version": runtime,
        "effective_profile": identity.get("effective_profile", "basic_serial"),
        "terminal_complete": complete,
        "native_exit_code": native_exit,
        "bridge_exit_code": bridge_exit,
        "problem": problem,
        "project_narrowing": project_narrowing if isinstance(project_narrowing, dict)
        else _empty_narrowing(),
    }
    raw = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    max_bytes, _, _, _ = _report_limits()
    if len(raw) > max_bytes:
        raise BridgeRefusal("pytest terminal report exceeds its bound", "capacity-exceeded")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("pytest terminal report write made no progress")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_attempt_report(path: Path, identity: dict[str, str], *, runtime: str,
                          native_exit: int | None, bridge_exit: int,
                          complete: bool, problem: str | None,
                          runtime_identity: str, runtime_facts: dict[str, object],
                          inventory: list[dict[str, object]],
                          workers: list[dict[str, str]], coverage_complete: bool,
                          reporters_complete: bool,
                          inventory_complete: bool | None = None,
                          project_narrowing: dict[str, Any] | None = None) -> None:
    payload = {
        "protocol": 1, **identity, "runner": "pytest",
        "observed_runtime_version": runtime,
        "terminal_complete": complete,
        "native_exit_code": native_exit if complete else None,
        "bridge_exit_code": bridge_exit,
        "problem": problem,
        "project_narrowing": project_narrowing if isinstance(project_narrowing, dict)
        else _empty_narrowing(),
        "runtime_identity": runtime_identity,
        "runtime_facts": runtime_facts,
        "inventory": {
            "adapter": "pytest", "version": runtime,
            "complete": complete if inventory_complete is None else inventory_complete,
            "tests": inventory,
            "digest": hashlib.sha256(json.dumps(inventory, sort_keys=True,
                                                  separators=(",", ":")).encode()).hexdigest(),
        },
        "workers": workers,
        "coverage": {"complete": coverage_complete},
        "reporters": {"complete": reporters_complete},
    }
    raw = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    max_bytes, max_tests, max_events, max_test_id_bytes = _report_limits()
    # The native report is the compact terminal projection of the frozen
    # bridge-event stream. Bound the represented inventory plus terminal event
    # with the same descriptor event budget even though no unbounded event
    # stream is emitted by this bridge.
    if (not isinstance(inventory, list)
            or len(raw) > max_bytes or len(inventory) > max_tests
            or len(inventory) + 1 > max_events):
        raise BridgeRefusal("pytest advanced terminal report exceeds its bound", "capacity-exceeded")
    if any(not isinstance(item, dict)
           or not isinstance(item.get("id"), str)
           or len(item["id"].encode("utf-8")) > max_test_id_bytes
           for item in inventory):
        raise BridgeRefusal("pytest test identity exceeds its bound", "capacity-exceeded")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("pytest terminal report write made no progress")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


# Full-only collection hooks are the project's own suite definition when
# they live in a conftest.py under the admitted checkout (section F);
# reporting hooks and non-conftest plugins stay refused. The four
# collection-time hooks below can silently narrow a full run (a
# pytest_pycollect_makeitem returning [] drops tests with RC 0), so they
# are checked exactly like modifyitems/ignore_collect: accepted and
# recorded only from a checkout conftest, refused from other plugins
# unless the module is approved (pytest_asyncio and anyio implement
# pytest_pycollect_makeitem and stay approved).
_FULL_COLLECTION_HOOKS = frozenset({
    "pytest_collection_modifyitems", "pytest_ignore_collect",
    "pytest_pycollect_makeitem", "pytest_collect_file",
    "pytest_collect_directory", "pytest_make_collect_report",
})


# Hook-only modules that neither distribute, reorder, nor re-run tests stay
# additive under the basic-serial grant. anyio arrives transitively with
# FastAPI/httpx/starlette and only wraps test calls (pytest_pyfunc_call),
# so it is approved at the same module-prefix granularity. There is no
# version pin: like pytest_asyncio/pytest_timeout, approval is by hook
# ownership, not by version (the only version pin in this bridge is the
# frozen pytest-cov/coverage evidence tuple, which is unrelated).
_BASIC_APPROVED_HOOK_MODULES = ("pytest_asyncio", "pytest_timeout", "anyio")


class OwnedPlugin:
    """Additive profile gate; it neither replaces reporters nor parses addopts."""

    _approved_hook_modules = _BASIC_APPROVED_HOOK_MODULES

    def __init__(self, workers: int, execution: str | None = None,
                 roots: tuple[str, ...] | None = None,
                 runtime: str = "unknown") -> None:
        self.workers = workers
        self.execution = execution if execution is not None else os.environ.get("PTEST_EXECUTION")
        # Capture the executor-owned checkout/config binding before project
        # imports can mutate the environment.
        self.checkout_root = os.environ.get("PTEST_PYTEST_CHECKOUT_ROOT")
        self.config_path = os.environ.get("PTEST_PYTEST_CONFIG_PATH")
        if roots is None:
            try:
                decoded = json.loads(os.environ.get("PTEST_TEST_ROOTS", "null"))
                roots = tuple(decoded) if isinstance(decoded, list) else ()
            except ValueError:
                roots = ()
        self.roots = roots
        self.refused = False
        self._config: Any | None = None
        # The bridge-owned record of allowed full-mode narrowing. The run
        # label is built from this report, never from a static prediction.
        self._narrowing_report: dict[str, Any] | None = None

    def _refuse(self, message: str) -> None:
        from pytest import UsageError
        self.refused = True
        _refusal_marker("native-config-invalid", message)
        raise UsageError(f"native-config-invalid: {message}")

    def _conftest_relpath(self, plugin: Any) -> str | None:
        """Project-relative path when ``plugin`` is a conftest module in the checkout."""
        if not isinstance(plugin, ModuleType):
            return None
        path = getattr(plugin, "__file__", None)
        if not isinstance(path, str) or not path:
            return None
        try:
            candidate = os.path.realpath(path)
            expected = os.path.realpath(self.checkout_root) if self.checkout_root else ""
        except (OSError, ValueError):
            return None
        if not expected or os.path.basename(candidate) != "conftest.py":
            return None
        try:
            if os.path.commonpath((expected, candidate)) != expected:
                return None
        except ValueError:
            return None
        return os.path.relpath(candidate, expected).replace(os.sep, "/")

    def _project_conftest_hook(self, hook: str, implementation: Any) -> str | None:
        """Project-relative conftest path when a full-only collection hook is owned.

        Only the ``_FULL_COLLECTION_HOOKS`` collection hooks
        (``pytest_collection_modifyitems``/``pytest_ignore_collect`` plus
        the ``pytest_pycollect_makeitem``/``pytest_collect_file``/
        ``pytest_collect_directory``/``pytest_make_collect_report`` family)
        defined in a ``conftest.py`` module under the admitted checkout
        count as the project's own suite definition. The plugin object must
        itself be that module (a registered class instance or any other
        object is refused even when its code lives in a conftest), and the
        hook function must be defined in it (a re-exported import is
        refused). Hooks from installed plugins, conftests outside the
        checkout, and reporting hooks stay refused. Missing file evidence
        fails closed. Returns None when the hook is not project-owned.
        """
        if self.execution != "full" or hook not in _FULL_COLLECTION_HOOKS:
            return None
        plugin = getattr(implementation, "plugin", None)
        relpath = self._conftest_relpath(plugin)
        if relpath is None:
            return None
        function = getattr(implementation, "function", None)
        if getattr(function, "__module__", None) != getattr(plugin, "__name__", None):
            return None
        return relpath

    def _full_narrowing_report(self, config: Any, accepted_hooks: list[str],
                               loaded: list[Any]) -> dict[str, Any]:
        """Report exactly what full mode allowed into the attempt report.

        The single source of truth behind the ``full (project-filtered:
        ...)`` label: the effective checked-in narrowing text, every
        accepted conftest-hook file, and non-default collection ini/conftest
        narrowing (``norecursedirs``/``python_files``/``python_functions``,
        ``collect_ignore``/``collect_ignore_glob``). Only reached after
        every refusal gate above passed, so everything reported here was
        allowed.
        """
        try:
            ini_tokens = _checked_in_addopts(config)
        except BridgeRefusal:
            ini_tokens = ()
        notes = self._full_notes(config, loaded)
        return {
            "narrowing": full_narrowing_text(ini_tokens),
            "conftest_hooks": sorted(set(accepted_hooks)),
            "notes": notes,
        }

    def _full_notes(self, config: Any, loaded: list[Any]) -> list[str]:
        """Non-default collection narrowing notes for the project label."""
        notes: list[str] = []
        for name in ("norecursedirs", "python_files", "python_functions"):
            try:
                effective = config.getini(name)
            except (AttributeError, ValueError, TypeError):
                continue
            try:
                default = config._parser._inidict[name][2]
            except (AttributeError, KeyError, IndexError, TypeError):
                continue
            try:
                same = list(effective or []) == list(default or [])
            except TypeError:
                continue
            if not same:
                try:
                    text = " ".join(str(item) for item in (effective or []))
                except TypeError:
                    text = ""
                notes.append((f"{name}={text}"[:200] or f"{name} (project)"))
        for plugin in loaded:
            relpath = self._conftest_relpath(plugin)
            if relpath is None:
                continue
            for attr in ("collect_ignore", "collect_ignore_glob"):
                ignored = getattr(plugin, attr, [])
                if isinstance(ignored, str):
                    ignored = [ignored]
                if (isinstance(ignored, (list, tuple)) and len(ignored) <= 64
                        and any(isinstance(item, str) and item for item in ignored)):
                    notes.append(f"{attr} in {relpath}")
        return sorted(set(notes))[:64]

    def allowed_narrowing(self) -> dict[str, Any]:
        """Bridge-owned narrowing report for the attempt writer (never None)."""
        report = self._narrowing_report
        if not isinstance(report, dict):
            return {"narrowing": None, "conftest_hooks": [], "notes": []}
        narrowing = report.get("narrowing")
        hooks = report.get("conftest_hooks")
        notes = report.get("notes")
        return {
            "narrowing": narrowing if isinstance(narrowing, str) else None,
            "conftest_hooks": [item for item in hooks
                               if isinstance(item, str)] if isinstance(hooks, list) else [],
            "notes": [item for item in notes
                      if isinstance(item, str)] if isinstance(notes, list) else [],
        }

    def _validate(self, config: Any, *, generated: bool) -> None:
        self._config = config
        option = config.option
        manager = getattr(config, "pluginmanager", None)
        # Project-owned hook files accepted below; the narrowing report at
        # the end of the full branch records them even when no plugin
        # manager is present (checked-in ini narrowing still applies).
        accepted_hooks: list[str] = []
        loaded_plugins: list[Any] = []
        if manager is not None:
            try:
                loaded = manager.list_name_plugin()
            except (AttributeError, TypeError):
                loaded = ()
            is_blocked = getattr(manager, "is_blocked", None)
            # ``-p no:xdist`` also leaves xdist's loop-on-fail helper visible
            # under its own entry-point name.  It is part of the canonical
            # blocked xdist family, not an active executor.  Do not generalize
            # this exemption to arbitrary aliases: a project can register the
            # real xdist plugin under another name and must still be refused.
            blocked_xdist_ids = {
                id(module) for module_name in ("xdist", "xdist.looponfail")
                if (module := sys.modules.get(module_name)) is not None
            }
            blocked_plugin_ids = {
                id(plugin) for _name, plugin in loaded
                if plugin is not None
                and id(plugin) in blocked_xdist_ids
                and callable(is_blocked)
                and is_blocked("xdist")
            }
            configured = getattr(option, "numprocesses", None)
            explicit_tx = list(getattr(option, "tx", None) or [])
            # Loaded but inactive xdist is allowed under the one-slot grant:
            # "-n 0" keeps the plugin (and its hookspecs, which conftests may
            # implement) while xdist itself serializes. Any active state below
            # still refuses with the existing messages.
            xdist_inactive = (
                self.workers == 1
                and configured in (None, 0, "0")
                and not explicit_tx
                and not any(getattr(option, name, None)
                            for name in ("px", "rsyncdir", "looponfail"))
            )
            exempt_ids = set(blocked_plugin_ids)
            if xdist_inactive:
                for _candidate_name, candidate in loaded:
                    if candidate is None:
                        continue
                    candidate_module = str(getattr(candidate, "__name__", "") or
                                           getattr(type(candidate), "__module__", ""))
                    # Exempt by module only: a plugin merely registered
                    # under the xdist name from another module stays
                    # refused below.
                    if candidate_module.split(".", 1)[0] == "xdist":
                        exempt_ids.add(id(candidate))
            for name, plugin in loaded:
                if plugin is None:  # pluggy records blocked names with a None value.
                    continue
                module = str(getattr(plugin, "__name__", "") or
                             getattr(type(plugin), "__module__", ""))
                if module.split(".", 1)[0] == "xdist" \
                        and id(plugin) not in exempt_ids:
                    self._refuse("pytest xdist is not owned by the serial grant")
                executors = {"forked", "parallel", "rerunfailures", "repeat", "loop"}
                normalized = str(name).replace("-", "_").removeprefix("pytest_")
                package = str(module).split(".", 1)[0].removeprefix("pytest_")
                if normalized in executors or package in executors:
                    self._refuse("pytest execution-control plugin is not owned by the serial grant")
            # Inspect registered hook owners, including specname aliases.
            # Reporters and ordinary fixtures remain additive; an unqualified
            # executor cannot bypass the serial loop even without a -n flag.
            hooks = ("pytest_cmdline_main", "pytest_collection",
                     "pytest_runtestloop", "pytest_runtest_protocol",
                     "pytest_runtest_call", "pytest_pyfunc_call")
            if self.execution == "full":
                hooks += ("pytest_collection_modifyitems", "pytest_ignore_collect",
                          "pytest_pycollect_makeitem", "pytest_collect_file",
                          "pytest_collect_directory", "pytest_make_collect_report",
                          "pytest_runtest_makereport", "pytest_report_teststatus",
                          "pytest_sessionfinish")
            loaded_plugins = [plugin for _, plugin in loaded]
            for hook in hooks:
                for implementation in getattr(manager.hook, hook).get_hookimpls():
                    if implementation.plugin is self:
                        continue
                    if id(implementation.plugin) in exempt_ids:
                        continue
                    module = getattr(implementation.function, "__module__", "")
                    if str(module).startswith("_pytest."):
                        continue
                    if any(str(module) == prefix or str(module).startswith(prefix + ".")
                           for prefix in getattr(self, "_approved_hook_modules", ())):
                        continue
                    owned = self._project_conftest_hook(hook, implementation)
                    if owned is not None:
                        accepted_hooks.append(owned)
                        continue
                    self._refuse("unqualified pytest execution hook is not owned by the serial grant")
        if any(getattr(option, name, None) for name in ("px", "rsyncdir", "looponfail")):
            self._refuse("remote/proxy or loop-on-fail pytest execution is unsupported")
        try:
            rsyncdirs = config.getini("rsyncdirs")
        except ValueError:  # Option is not registered without xdist.
            rsyncdirs = []
        if rsyncdirs:
            self._refuse("pytest rsync configuration is unsupported")
        tx = list(getattr(option, "tx", None) or [])
        expected = ["popen"] * self.workers if generated and self.workers > 1 else []
        if tx != expected:
            self._refuse("explicit or unexpected pytest transports are unsupported")
        configured = getattr(option, "numprocesses", None)
        if self.workers == 1 and configured not in (None, 0, "0"):
            self._refuse("serial grant cannot use xdist")
        if self.workers > 1 and str(configured) != str(self.workers):
            self._refuse("xdist worker count differs from admission grant")
        maximum = getattr(option, "maxprocesses", None)
        if maximum is not None and str(maximum) != str(self.workers):
            self._refuse("xdist maximum worker count differs from admission grant")
        if self.execution == "full":
            try:
                # ``pytest_cmdline_main`` runs before native root/config
                # discovery is complete; effective path fields are enforced
                # on the first post-configure gate below.
                if generated:
                    _validate_native_paths(config, self.checkout_root, self.config_path)
                # The environment is invocation-time narrowing and stays
                # refused wholesale; checked-in addopts narrowing is the
                # project's own suite definition and is only redirect-gated
                # here (allowed filters are recorded in the run label).
                _reject_full_addopts(_split_addopts(os.environ.get("PYTEST_ADDOPTS")))
                _reject_full_ini_addopts(_checked_in_addopts(config))
            except BridgeRefusal as refusal:
                self._refuse(refusal.message)
            narrowing = ("keyword", "markexpr", "deselect", "lf", "failedfirst",
                         "stepwise", "stepwise_skip", "testmon", "ignore",
                         "ignore_glob", "maxfail", "collectonly", "pyargs",
                         "setuponly", "setupplan", "showfixtures",
                         "show_fixtures_per_test", "markers", "cacheshow",
                         "help", "version")
            # Attribute each effective narrowing option to its source:
            # invocation args and PYTEST_ADDOPTS stay refused, checked-in
            # addopts narrowing is the project's own suite definition
            # (allowed, recorded in the run label), and anything else
            # (programmatic mutation, unowned plugins) fails closed.
            invocation_args = getattr(
                getattr(config, "invocation_params", None), "args", ())
            if not isinstance(invocation_args, (tuple, list)):
                invocation_args = ()
            try:
                invocation_tokens = tuple(
                    str(token) for token in invocation_args) + _split_addopts(
                        os.environ.get("PYTEST_ADDOPTS"))
                ini_tokens = _checked_in_addopts(config)
            except BridgeRefusal as refusal:
                self._refuse(refusal.message)
            for name in narrowing:
                effective = getattr(option, name, None)
                if not effective:
                    continue
                if _flag_supplies(invocation_tokens, name):
                    self._refuse("full pytest plans cannot narrow the inventory")
                # Checked-in narrowing is project-owned only when the
                # effective value equals its checked-in source: a conftest
                # mutation of the same spelling fails closed instead of
                # passing on the ini label.
                if (name in _FULL_INI_ALLOWED_ATTRS
                        and _flag_supplies(ini_tokens, name)
                        and _ini_value_matches(name, effective, ini_tokens)):
                    continue
                self._refuse("full pytest plans cannot narrow the inventory")
            invocation = getattr(getattr(config, "invocation_params", None), "args", ())
            if not isinstance(invocation, (tuple, list)):
                invocation = ()
            redirects = {"-c", "--config-file", "--rootdir", "--confcutdir",
                         "--noconftest", "--pyargs", "-o", "--override-ini",
                         "--basetemp"}
            for token in invocation:
                option_name = str(token).split("=", 1)[0]
                token_text = str(token)
                redirect_cluster = _short_redirect_cluster(token_text)
                if option_name in redirects or redirect_cluster:
                    self._refuse("full pytest plans cannot redirect native configuration")
            if any(getattr(option, name, None) for name in
                   ("noconftest", "pyargs", "confcutdir", "basetemp")):
                self._refuse("full pytest plans cannot redirect native configuration")
            for entry in getattr(option, "override_ini", None) or ():
                if not isinstance(entry, str) or entry.strip() not in _SAFE_STRICT_OVERRIDES:
                    self._refuse("full pytest plans cannot redirect native configuration")
            roots = self.roots
            _validate_full_roots(roots)
            if config.args != list(roots):
                self._refuse("full pytest inventory differs from configured roots")
            # Every full gate above passed: record what was allowed. The run
            # label is built from this report, never from a static scan.
            self._narrowing_report = self._full_narrowing_report(
                config, accepted_hooks, loaded_plugins)

    def pytest_cmdline_main(self, config: Any) -> Any:
        """Wrap before xdist replaces explicit tx with local popen transports."""
        self._validate(config, generated=False)
        return (yield)

    def pytest_configure(self, config: Any) -> None:
        self._validate(config, generated=True)

    def pytest_collection(self, session: Any) -> Any:
        """Check again after configure hooks, before collecting test modules."""
        self._validate(session.config, generated=True)
        return (yield)

    def pytest_collection_finish(self, session: Any) -> Any:
        """Check collection-loaded conftests before entering test execution."""
        result = yield
        self._validate(session.config, generated=True)
        return result

    def pytest_runtestloop(self, session: Any) -> Any:
        """Check execution hooks immediately before entering the test loop."""
        self._validate(session.config, generated=True)
        return (yield)

    def pytest_runtest_protocol(self, item: Any, nextitem: Any) -> Any:
        """Check execution hooks before each item can replace its protocol."""
        self._validate(item.config, generated=True)
        return (yield)

    def pytest_runtest_call(self, item: Any) -> Any:
        """Check per-item registrations at the test-body execution boundary."""
        self._validate(item.config, generated=True)
        return (yield)

    def pytest_xdist_setupnodes(self, config: Any, specs: Any) -> None:
        """Check the final gateway boundary, before xdist creates any worker."""
        self._validate(config, generated=True)
        expected_keys = {"_spec", "env", "execmodel", "popen", "id"}
        worker_ids = set()
        for spec in specs:
            try:
                attributes = vars(spec)
            except TypeError:
                attributes = {}
            worker_id = attributes.get("id")
            if (set(attributes) != expected_keys
                    or attributes.get("_spec") != "execmodel=main_thread_only//popen"
                    or attributes.get("env") != {}
                    or attributes.get("execmodel") != "main_thread_only"
                    or attributes.get("popen") is not True
                    or not isinstance(worker_id, str)
                    or not worker_id.startswith("gw")
                    or not worker_id[2:].isdigit()):
                self._refuse("xdist gateway specifications differ from admission grant")
            worker_ids.add(worker_id)
        if len(specs) != self.workers or len(worker_ids) != self.workers:
            self._refuse("xdist gateway specifications differ from admission grant")


class AdvancedPlugin(OwnedPlugin):
    """Additive native inventory recorder for a qualified advanced attempt.

    The recorder is intentionally part of the bridge-owned plugin, so native
    test IDs and outcomes never have to be reconstructed from stdout/JUnit.
    Parallel worker instrumentation is accepted only when a future supported
    xdist hook supplies distinct identities; this implementation's serial
    profile emits the actual executor-bound ``w000`` identity.
    """

    def __init__(self, workers: int, execution: str | None = None,
                 roots: tuple[str, ...] | None = None,
                 runtime: str = "unknown") -> None:
        super().__init__(workers, execution, roots)
        self.inventory: dict[str, dict[str, object]] = {}
        self.collection_complete = False
        self.coverage_complete = False
        self.reporters_complete = False
        self._plugin_facts: tuple = ()
        self._hook_facts: tuple = ()
        self._effective_options: dict[str, str] = {}
        self._terminal_observed = False
        self._runtime = runtime
        self._initial_runtime_identity: str | None = None
        self._initial_runtime_facts: dict[str, object] | None = None
        self._terminal_runtime_identity: str | None = None
        self._terminal_runtime_facts: dict[str, object] | None = None
        self._approved_hook_modules = ("pytest_cov",) + _BASIC_APPROVED_HOOK_MODULES

    @staticmethod
    def _dependency_facts() -> tuple[tuple[str, str], ...]:
        try:
            values = sorted({
                (str(item.metadata.get("Name", "")).lower(),
                 str(item.version))
                for item in importlib.metadata.distributions()
                if item.metadata.get("Name")
            })
        except (importlib.metadata.PackageNotFoundError, OSError, ValueError):
            return ()
        return tuple(values[:512])

    def pytest_sessionstart(self, session: Any) -> None:
        """Capture pre-collection facts after all configure hooks settle."""
        self._refresh_runtime_facts(session.config)
        self._initial_runtime_facts = self.runtime_facts(self._runtime)
        self._initial_runtime_identity = self._facts_identity(self._initial_runtime_facts)

    def pytest_runtestloop(self, session: Any) -> Any:
        """Recheck selected identity immediately before native execution."""
        self._validate(session.config, generated=True)
        self._refresh_runtime_facts(session.config)
        current_facts = self.runtime_facts(self._runtime)
        current_identity = self._facts_identity(current_facts)
        expected = os.environ.get("PTEST_EXPECTED_RUNTIME_IDENTITY")
        if expected is not None and current_identity != expected:
            self._refuse("observed native runtime identity changed before tests")
        self._initial_runtime_facts = current_facts
        self._initial_runtime_identity = current_identity
        return (yield)

    def _refresh_runtime_facts(self, config: Any) -> None:
        """Capture live plugin, hook, and effective-option state.

        Pytest permits project hooks/plugins to register during the lifecycle;
        terminal identity must therefore use a fresh snapshot rather than the
        configure-time cache.
        """
        manager = getattr(config, "pluginmanager", None)
        loaded = () if manager is None else manager.list_name_plugin()
        # Presence of a coverage plugin or a reporter config flag is not
        # evidence that the native run actually observed either one.  The
        # terminal report is qualified only from runtime state in
        # finalize_evidence below, after all tests have emitted reports.
        self.coverage_complete = False
        self.reporters_complete = False
        plugin_facts = []
        for name, plugin in loaded:
            if plugin is None:
                continue
            stable_name = str(name)
            # Pluggy uses object ids for anonymous registrations. They are
            # process-local and would make an otherwise identical baseline
            # and selected run appear to have different runtimes.
            if stable_name.isdigit():
                stable_name = ""
            stable_module = str(getattr(plugin, "__name__", ""))
            if not stable_module:
                plugin_type = type(plugin)
                stable_module = (f"{plugin_type.__module__}."
                                 f"{plugin_type.__qualname__}")
            plugin_facts.append((stable_name, stable_module,
                                 str(getattr(plugin, "__version__", ""))))
        self._plugin_facts = tuple(sorted(plugin_facts))
        critical = ("pytest_cmdline_main", "pytest_collection",
                    "pytest_collection_finish", "pytest_runtestloop",
                    "pytest_runtest_protocol", "pytest_runtest_call",
                    "pytest_runtest_logreport")
        hooks = []
        if manager is not None:
            for hook_name in critical:
                for implementation in getattr(manager.hook, hook_name).get_hookimpls():
                    if implementation.plugin is self:
                        continue
                    module = str(getattr(implementation.function, "__module__", ""))
                    hooks.append((hook_name, module,
                                  str(getattr(implementation.function, "__name__", ""))))
        self._hook_facts = tuple(sorted(set(hooks)))
        dynamic = {"args", "file_or_dir", "rootdir", "inipath", "inifilename"}
        option = getattr(config, "option", None)
        self._effective_options = {
            str(key): repr(value) for key, value in vars(option).items()
            if key not in dynamic
        } if option is not None else {}

    def finalize_evidence(self) -> None:
        if self._config is not None:
            self._refresh_runtime_facts(self._config)
        manager = self._config.pluginmanager if self._config is not None else None
        terminal = None if manager is None else manager.get_plugin("terminalreporter")
        coverage_plugin = None
        if manager is not None:
            for name, plugin in manager.list_name_plugin():
                normalized = str(name).replace("-", "_").lower()
                module = str(getattr(plugin, "__name__", ""))
                if (normalized in {"_cov", "cov", "pytest_cov", "pytest_cov_plugin"}
                        or module == "pytest_cov" or module.startswith("pytest_cov.")):
                    # pytest-cov exposes both its import module and the
                    # controller plugin under distinct names; only the
                    # controller owns measured coverage data.
                    if getattr(plugin, "cov_controller", None) is not None:
                        coverage_plugin = plugin
                        break
                    if coverage_plugin is None:
                        coverage_plugin = plugin
        controller = getattr(coverage_plugin, "cov_controller", None)
        coverage = getattr(controller, "cov", None)
        measured_files = None
        try:
            data = None if coverage is None else coverage.get_data()
            measured_files = None if data is None else data.measured_files()
        except (AttributeError, OSError, TypeError, ValueError):
            measured_files = None
        self.coverage_complete = bool(
            measured_files is not None
            and isinstance(measured_files, (set, list, tuple))
            and bool(measured_files)
            and all(isinstance(path, str) and path for path in measured_files)
            and self.checkout_root is not None
            and all(
                os.path.commonpath((os.path.realpath(self.checkout_root),
                                    os.path.realpath(path)))
                == os.path.realpath(self.checkout_root)
                for path in measured_files
            ))
        # A terminal reporter is considered observed only after it handled at
        # least one native test report; merely enabling reporters in config is
        # not a qualification signal.
        self.reporters_complete = bool(
            terminal is not None and self._terminal_observed)
        self._terminal_runtime_facts = self.runtime_facts(self._runtime)
        self._terminal_runtime_identity = self._facts_identity(self._terminal_runtime_facts)
        if (self._initial_runtime_identity is None
                or self._terminal_runtime_identity != self._initial_runtime_identity):
            changed = tuple(key for key in self._terminal_runtime_facts
                            if self._initial_runtime_facts is None
                            or self._initial_runtime_facts.get(key) != self._terminal_runtime_facts.get(key))
            self._refuse("native runtime identity changed between pre-run and terminal capture"
                         + (f" (fields: {','.join(changed)})" if changed else ""))
        expected = os.environ.get("PTEST_EXPECTED_RUNTIME_IDENTITY")
        if expected is not None and expected != self._terminal_runtime_identity:
            changed = tuple(key for key in self._terminal_runtime_facts
                           if self._initial_runtime_facts is None
                           or self._initial_runtime_facts.get(key) != self._terminal_runtime_facts.get(key))
            self._refuse("observed native runtime identity changed"
                         + (f" (fields: {','.join(changed)})" if changed else ""))

    def pytest_collection_finish(self, session: Any) -> Any:
        result = yield
        self._validate(session.config, generated=True)
        seen: set[str] = set()
        for item in getattr(session, "items", ()):
            nodeid = str(getattr(item, "nodeid", ""))
            if not nodeid or nodeid in seen:
                self._refuse("duplicate or missing native test identity")
            seen.add(nodeid)
            path = str(getattr(item, "fspath", ""))
            try:
                checkout_root = os.path.realpath(self.checkout_root or os.getcwd())
                resolved_path = os.path.realpath(path)
                if os.path.commonpath((checkout_root, resolved_path)) != checkout_root:
                    self._refuse("native test identity is outside the checkout")
                path = os.path.relpath(resolved_path, checkout_root).replace(os.sep, "/")
            except (TypeError, ValueError):
                self._refuse("native test identity is outside the checkout")
            if path == ".." or path.startswith("../") or path.startswith("/"):
                self._refuse("native test identity is outside the checkout")
            self.inventory[nodeid] = {
                "id": nodeid, "file": path, "outcome": "unknown",
                "setup_s": None, "call_s": None, "teardown_s": None,
            }
        self.collection_complete = True
        # Collection can register ordinary pytest lifecycle plugins after
        # session start. This is the last pre-execution point, so refresh the
        # authenticated baseline here.
        self._refresh_runtime_facts(session.config)
        self._initial_runtime_facts = self.runtime_facts(self._runtime)
        self._initial_runtime_identity = self._facts_identity(self._initial_runtime_facts)
        return result

    def pytest_runtest_logreport(self, report: Any) -> None:
        self._terminal_observed = True
        nodeid = str(getattr(report, "nodeid", ""))
        item = self.inventory.get(nodeid)
        if item is None:
            self._refuse("native outcome has no collected test identity")
        phase = getattr(report, "when", "")
        duration = getattr(report, "duration", None)
        if phase == "call":
            item["call_s"] = duration
            item["outcome"] = {
                "passed": "passed", "failed": "failed", "skipped": "skipped",
            }.get(str(getattr(report, "outcome", "unknown")), "unknown")
        elif phase == "setup":
            item["setup_s"] = duration
            if getattr(report, "failed", False):
                item["outcome"] = "error"
            elif getattr(report, "skipped", False):
                item["outcome"] = "skipped"
        elif phase == "teardown":
            item["teardown_s"] = duration
            if getattr(report, "failed", False):
                item["outcome"] = "error"

    def worker_identities(self) -> list[dict[str, str]]:
        if self.workers > 1:
            self._refuse("native parallel worker identity is not qualified")
        worker_id = os.environ.get("PTEST_WORKER_ID", "")
        prefix = os.environ.get("PTEST_RESOURCE_PREFIX", "")
        checkout_id = os.environ.get("PTEST_CHECKOUT_ID", "")
        run_id = os.environ.get("PTEST_RUN_ID", "")
        attempt_id = os.environ.get("PTEST_PYTEST_ATTEMPT", "")
        if (not re.fullmatch(r"w(?:0[0-5][0-9]|06[0-3])", worker_id)
                or not re.fullmatch(r"[0-9a-f]{32}", checkout_id)
                or not re.fullmatch(r"[0-9a-f]{32}", run_id)
                or not re.fullmatch(r"a(00[1-9]|010)", attempt_id)
                or prefix != f"pt_{checkout_id[:8]}_{run_id}_{attempt_id}_{worker_id}"):
            self._refuse("native worker identity is missing or malformed")
        return [{"worker_id": worker_id, "resource_prefix": prefix}]

    def pytest_xdist_node_collection_finished(self, node: Any, ids: Any) -> None:
        worker = getattr(getattr(node, "gateway", None), "id", None)
        if not isinstance(worker, str) or not re.fullmatch(r"gw[0-9]+", worker):
            self._refuse("native parallel worker identity is malformed")
        self._refuse("native parallel worker identity is not qualified")

    def runtime_facts(self, runtime: str) -> dict[str, object]:
        try:
            variants = json.loads(os.environ.get("PTEST_PYTEST_COMMAND_VARIANTS", "null"))
        except (TypeError, ValueError):
            self._refuse("command identity is malformed")
        if (not isinstance(variants, list) or len(variants) != 2
                or not all(isinstance(item, list)
                           and all(isinstance(token, str) for token in item)
                           for item in variants)):
            self._refuse("command identity is malformed")
        return {
            "runner": "pytest", "version": runtime,
            "python": sys.version, "implementation": sys.implementation.name,
            "cache_tag": sys.implementation.cache_tag,
            "roots": list(self.roots),
            "profile": "advanced",
            "plugins": self._plugin_facts,
            # Distribution identities are intentionally distinct from loaded
            # plugin identities; a plugin list alone cannot prove dependency
            # stability across the native lifecycle.
            "dependencies": self._dependency_facts(),
            "hooks": self._hook_facts,
            "effective_options": self._effective_options,
            "command_variants": variants,
            # These are configured/effective identities.  Actual observation
            # is represented only by the bounded terminal report booleans,
            # never by a config truthiness shortcut in this digest.
            "coverage": tuple(sorted(
                name for name, _module, _version in self._plugin_facts
                if "cov" in name.lower())),
            "reporters": tuple(sorted(
                name for name, _module, _version in self._plugin_facts
                if "report" in name.lower())),
            "platform": {"system": sys.platform, "os": os.name,
                          "machine": os.uname().machine if hasattr(os, "uname") else "unknown"},
        }

    @staticmethod
    def _facts_identity(observed: dict[str, object]) -> str:
        return hashlib.sha256(json.dumps(
            observed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def run(argv: list[str] | tuple[str, ...] | None = None) -> int:
    """Run pytest natively after validating the immutable bridge descriptor."""
    binding = _report_binding()
    runtime = "unknown"
    native_exit: int | None = None
    bridge_exit = 70
    complete = False
    problem: str | None = None
    narrowing_report = _empty_narrowing()
    advanced_plugin: AdvancedPlugin | None = None
    advanced_runtime_identity = hashlib.sha256(b"unavailable").hexdigest()
    advanced_runtime_facts: dict[str, object] = {}
    if argv is None:
        argv = tuple(sys.argv[1:])
    if not isinstance(argv, (list, tuple)) or not all(isinstance(item, str) for item in argv):
        _fail("pytest argv must be a string array")
    # Executing this file must have the same cwd imports as `python -m pytest`.
    sys.path[:1] = [os.getcwd()]
    try:
        _protocol()
        workers = _workers()
        _python_version()
        execution = os.environ.get("PTEST_EXECUTION")
        if execution is not None and execution not in {"scoped", "selected", "full"}:
            _fail("pytest execution binding is invalid")
        try:
            roots = tuple(json.loads(os.environ.get("PTEST_TEST_ROOTS", "null")))
        except (TypeError, ValueError):
            roots = ()
        if execution == "full" and (not roots or not all(isinstance(root, str) for root in roots)):
            _fail("full pytest roots are invalid")
        if execution == "full":
            _validate_full_roots(roots)
            # Refuse executor-provided controls before pytest parses native
            # addopts.  In particular, a combined ``-qc`` may otherwise make
            # pytest open an attacker-selected file before our hooks run.
            _reject_full_addopts(_split_addopts(os.environ.get("PYTEST_ADDOPTS")))
            _reject_full_addopts(tuple(argv))
        selected_files = _selected_files() if execution == "selected" else ()
        if selected_files and tuple(argv[-len(selected_files):]) != selected_files:
            _fail("selected pytest files differ from the executor binding")
        profile = os.environ.get("PTEST_PYTEST_PROFILE", "basic_serial")
        if profile not in {"basic_serial", "advanced"}:
            _fail("pytest profile is unsupported")
        if profile == "advanced" and (
                binding is None or binding[1].get("effective_profile") != "advanced"):
            _fail("advanced pytest profile requires an authenticated report binding",
                  "unsupported-capability")
        if profile == "advanced" and workers > 1:
            _fail("pytest advanced worker identity is not qualified", "unsupported-capability")
        if profile == "advanced":
            _coverage_tuple()
        native_argv = list(argv)
        try:
            import pytest
        except ImportError:
            _fail("pytest is unavailable in the selected interpreter")
        runtime = str(pytest.__version__)
        if runtime not in {"8.4.2", "9.0.3", "9.1.0", "9.1.1"}:
            _fail("pytest version is outside the candidate table", "unsupported-capability")
        # Mark hooks only after the selected interpreter and pytest have been checked.
        plugin_type = AdvancedPlugin if profile == "advanced" else OwnedPlugin
        pytest.hookimpl(wrapper=True, tryfirst=True)(plugin_type.pytest_cmdline_main)
        pytest.hookimpl(tryfirst=True)(plugin_type.pytest_configure)
        pytest.hookimpl(wrapper=True, tryfirst=True)(plugin_type.pytest_collection)
        pytest.hookimpl(wrapper=True, tryfirst=True)(plugin_type.pytest_collection_finish)
        pytest.hookimpl(wrapper=True, tryfirst=True)(plugin_type.pytest_runtestloop)
        pytest.hookimpl(wrapper=True, tryfirst=True)(plugin_type.pytest_runtest_protocol)
        pytest.hookimpl(wrapper=True, tryfirst=True)(plugin_type.pytest_runtest_call)
        pytest.hookimpl(tryfirst=True, optionalhook=True)(plugin_type.pytest_xdist_setupnodes)
        if profile == "advanced":
            pytest.hookimpl(tryfirst=True)(AdvancedPlugin.pytest_runtest_logreport)
            pytest.hookimpl(tryfirst=True, optionalhook=True)(AdvancedPlugin.pytest_xdist_node_collection_finished)
        plugin = (plugin_type(workers, execution, roots, runtime)
                  if profile == "advanced"
                  else plugin_type(workers, execution, roots))
        if profile == "advanced":
            advanced_plugin = plugin
        native_exit = int(pytest.main(native_argv, plugins=[plugin]))
        bridge_exit = native_exit
        # Hooks may register only after the final item boundary (for example
        # from teardown). Qualify the completed native run before allowing the
        # bridge to certify its terminal report.
        if plugin._config is not None:
            try:
                plugin._validate(plugin._config, generated=True)
            except pytest.UsageError:
                pass
        if plugin.refused:
            problem = "bridge-refused"
            bridge_exit = 4 if native_exit == 0 else native_exit
            return bridge_exit
        # The label source of truth: what the bridge actually allowed.
        narrowing_report = plugin.allowed_narrowing()
        if advanced_plugin is not None:
            advanced_plugin.finalize_evidence()
            advanced_runtime_facts = advanced_plugin._terminal_runtime_facts or {}
            advanced_runtime_identity = advanced_plugin._terminal_runtime_identity or hashlib.sha256(b"unavailable").hexdigest()
        complete = True
        problem = "native-failure" if native_exit else None
        return native_exit
    except BridgeRefusal:
        bridge_exit = 4
        problem = "bridge-refused"
        raise
    finally:
        if binding is not None and (complete or problem == "bridge-refused"):
            try:
                if binding[1].get("effective_profile") == "advanced":
                    inventory = [] if advanced_plugin is None else list(advanced_plugin.inventory.values())
                    report_runtime_facts = advanced_runtime_facts
                    report_runtime_identity = advanced_runtime_identity
                    if advanced_plugin is not None:
                        report_runtime_facts = (
                            advanced_runtime_facts
                            or advanced_plugin._terminal_runtime_facts
                            or advanced_plugin._initial_runtime_facts
                            or {})
                        report_runtime_identity = (
                            advanced_runtime_identity
                            if advanced_runtime_facts
                            else (advanced_plugin._terminal_runtime_identity
                                  or advanced_plugin._initial_runtime_identity
                                  or advanced_runtime_identity))
                    _write_attempt_report(
                        binding[0], binding[1], runtime=runtime,
                        native_exit=native_exit if complete else None,
                        bridge_exit=bridge_exit, complete=complete,
                        problem=problem, runtime_identity=report_runtime_identity,
                        runtime_facts=report_runtime_facts,
                        inventory=inventory,
                        workers=[] if advanced_plugin is None else advanced_plugin.worker_identities(),
                        coverage_complete=bool(advanced_plugin is not None and advanced_plugin.coverage_complete),
                        reporters_complete=bool(advanced_plugin is not None and advanced_plugin.reporters_complete),
                        inventory_complete=bool(advanced_plugin is not None and advanced_plugin.collection_complete),
                        project_narrowing=narrowing_report,
                    )
                else:
                    _write_report(binding[0], binding[1], runtime=runtime,
                                  native_exit=native_exit if complete else None,
                                  bridge_exit=bridge_exit, complete=complete,
                                  problem=problem,
                                  project_narrowing=narrowing_report)
            except BridgeRefusal:
                # A descriptor/size refusal is an authenticated bridge
                # refusal and must escape so the caller cannot mistake a
                # missing report for a native terminal result.
                raise
            except OSError:
                # An executor-owned report path collision must not rewrite the
                # native exit code.  The controller consumes no report and
                # therefore returns the fail-closed incomplete/70 outcome.
                pass


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except BridgeRefusal as error:
        _refusal_marker(error.code, error.message)
        raise SystemExit(4)
