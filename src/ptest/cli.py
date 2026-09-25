"""Closed NG command-line grammar and static dispatch.

This slice owns parsing and inspection only.  Execution deliberately stops at
the typed capability boundary until the scheduler/guard orchestration lands.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import stat
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from . import agent_assessment, agent_providers, agent_rules, config as config_api
from . import contracts as C
from . import doctor, doctor_fix, executability, files, help as help_api, history
from . import init_changed, init_render, init_smoke
from . import operations, platform, progress, recommendations, scheduler
from . import uninstall as uninstall_api
from . import render
from .adapters import pytest as pytest_adapter
from .adapters import vitest as vitest_adapter
from .runners import adapter_for


_INSPECTION = frozenset({
    "init", "register", "where", "status", "history", "plan",
    "doctor", "guide", "rules", "uninstall",
})
_EXECUTION_VALUE = frozenset({
    "--base", "--workers", "--queue-timeout", "--result-json",
})
_EXECUTION_BOOL = frozenset({"--changed", "--full", "--no-setup", "--shadow",
                             "-v", "--verbose", "-q", "--quiet"})
_REVIEW_TOTAL_TIMEOUT_S = 1800
_REVIEW_CONFIG_MAX_BYTES = 256 * 1024
# Keep collection preflight aligned with the writer's child/file proof bound.
_REVIEW_SOURCE_PROOF_MAX = recommendations.MAX_SOURCE_PROOF_ENTRIES


def _problem(code: str, message: str, *, phase: str = "cli") -> C.Problem:
    return C.Problem(code=code, message=message, phase=phase)


@dataclass(frozen=True, slots=True)
class ParsedArgs:
    command: str | None = None
    mode: C.Mode = C.Mode.AUTOMATIC
    runner_argv: tuple[str, ...] = ()
    fixture_domain: Path | None = None
    base: str | None = None
    workers: int | None = None
    queue_timeout_s: float = C.DEFAULT_QUEUE_TIMEOUT_S
    no_setup: bool = False
    shadow: bool = False
    verbose: bool = False
    quiet: bool = False
    result_path: str | None = None
    changed: bool = False
    full: bool = False
    json: bool = False
    reveal_command: bool = False
    dry_run: bool = False
    runner: C.RunnerKind | None = None
    scope: str | None = None
    history_limit: int = 20
    write: str | None = None
    probe: C.ProbeOptions | None = None
    max_entries: int | None = None
    max_files: int | None = None
    max_file_bytes: int | None = None
    max_total_bytes: int | None = None
    apply_rules: bool = False
    uninstall_self: bool = False
    uninstall_yes: bool = False
    children: tuple = ()
    agents: tuple[str, ...] = ()
    agents_explicit: bool = False
    help_topic: str | None = None
    reviewer: str | None = None
    reviewer_explicit: bool = False
    allow_model_review: bool = False
    review_timeout_s: int = 300
    review_timeout_explicit: bool = False
    review_model: str | None = None
    review_model_explicit: bool = False
    review_concurrency: int = 4
    review_concurrency_explicit: bool = False
    offline: bool = False
    fix: bool = False
    doctor_request: bool | None = None
    smoke: bool | None = None
    changed_setup: str | None = None


@dataclass(frozen=True, slots=True)
class _CliPrefix:
    fixture_domain: Path | None
    remainder_index: int
    command: str | None
    problem: C.Problem | None


def _value(args: Sequence[str], index: int, option: str) -> tuple[str, int]:
    if index + 1 >= len(args):
        raise _problem("invalid-config", "option requires a value")
    value = args[index + 1]
    if not isinstance(value, str) or not value or value.startswith("--"):
        raise _problem("invalid-config", "option requires a value")
    return value, index + 2


def _integer(value: str, *, lo: int, hi: int) -> int:
    try:
        parsed = int(value, 10)
    except (TypeError, ValueError):
        raise _problem("invalid-config", "option value is invalid") from None
    if parsed < lo or parsed > hi:
        raise _problem("invalid-bound", "option value is outside its bound")
    return parsed


def _number(value: str, *, lo: float, hi: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise _problem("invalid-config", "option value is invalid") from None
    if parsed != parsed or parsed in (float("inf"), float("-inf")) or not lo <= parsed <= hi:
        raise _problem("invalid-bound", "option value is outside its bound")
    return parsed


def _probe_integer(value: str, *, lo: int, hi: int) -> int:
    try:
        return _integer(value, lo=lo, hi=hi)
    except C.Problem as problem:
        if problem.code == "invalid-bound":
            raise _problem("invalid-config", "probe option value is invalid") from None
        raise


def _probe_number(value: str, *, lo: float, hi: float) -> float:
    try:
        return _number(value, lo=lo, hi=hi)
    except C.Problem as problem:
        if problem.code == "invalid-bound":
            raise _problem("invalid-config", "probe option value is invalid") from None
        raise


def _walk_cli_prefix(args: Sequence[str]) -> _CliPrefix:
    """Locate one leading fixture domain and the closed inspection command."""
    fixture = None
    problem = None
    index = 0
    while index < len(args) and args[index] == "--fixture-domain":
        try:
            value, next_index = _value(args, index, "--fixture-domain")
        except C.Problem as error:
            return _CliPrefix(fixture, index, None, problem or error)
        if fixture is not None and problem is None:
            problem = _problem("invalid-config", "option cannot be repeated")
        path = Path(value)
        if not path.is_absolute() and problem is None:
            problem = _problem("unsafe-path", "fixture domain must be an absolute path")
        if fixture is None:
            fixture = path
        index = next_index
    command = (args[index] if index < len(args)
               and isinstance(args[index], str)
               and args[index] in _INSPECTION else None)
    return _CliPrefix(fixture, index, command, problem)


def _parse_execution(args: Sequence[str], *, command: str | None = None) -> ParsedArgs:
    mode = C.Mode.AUTOMATIC
    changed = command == "changed"
    full = False
    workers = None
    base = None
    queue_timeout = C.DEFAULT_QUEUE_TIMEOUT_S
    no_setup = False
    shadow = False
    verbose = False
    quiet = False
    result_path = None
    tail: tuple[str, ...] = ()
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            tail = tuple(args[index + 1:])
            break
        if token not in _EXECUTION_VALUE and token not in _EXECUTION_BOOL:
            tail = tuple(args[index:])
            break
        if token in _EXECUTION_BOOL:
            if token == "--changed":
                if changed or full:
                    raise _problem("invalid-config", "execution modes cannot be combined")
                changed = True
            elif token == "--full":
                if changed or full:
                    raise _problem("invalid-config", "execution modes cannot be combined")
                full = True
            elif token == "--no-setup":
                no_setup = True
            elif token in ("-v", "--verbose"):
                verbose = True
            elif token in ("-q", "--quiet"):
                quiet = True
            else:
                shadow = True
            index += 1
            continue
        value, index = _value(args, index, token)
        if token == "--base":
            if base is not None:
                raise _problem("invalid-config", "option cannot be repeated")
            base = value
        elif token == "--workers":
            if workers is not None:
                raise _problem("invalid-config", "option cannot be repeated")
            workers = _integer(value, lo=1, hi=64)
        elif token == "--queue-timeout":
            queue_timeout = _number(value, lo=1, hi=C.MAX_QUEUE_TIMEOUT_S)
        else:
            if result_path is not None:
                raise _problem("invalid-config", "option cannot be repeated")
            result_path = value
    if full:
        if base is not None:
            raise _problem("invalid-config", "--base is unavailable with --full")
        mode = C.Mode.FULL
        if tail:
            raise _problem("invalid-config", "full execution cannot accept runner narrowing")
    elif changed:
        mode = C.Mode.AUTOMATIC
        if tail:
            raise _problem("invalid-config", "changed execution cannot accept runner arguments")
    elif tail:
        mode = C.Mode.SCOPED
    if shadow and mode is not C.Mode.AUTOMATIC:
        raise _problem("invalid-config", "shadow execution requires automatic mode")
    return ParsedArgs(
        command=command, mode=mode, runner_argv=tail,
        base=base, workers=workers, queue_timeout_s=queue_timeout,
        no_setup=no_setup, shadow=shadow, result_path=result_path,
        changed=changed, full=full, verbose=verbose, quiet=quiet,
    )


def _parse_inspection(command: str, args: Sequence[str]) -> ParsedArgs:
    if command == "init":
        runner = None
        dry_run = reveal = False
        children = []
        agents = ()
        agents_explicit = False
        doctor_request = None
        doctor_seen = no_doctor_seen = False
        reviewer = None
        reviewer_seen = allow_seen = timeout_seen = False
        model_seen = concurrency_seen = False
        allow_model_review = False
        review_timeout_s = 300
        review_model = None
        review_concurrency = 4
        smoke: bool | None = None
        smoke_seen = no_smoke_seen = False
        changed_setup: str | None = None
        changed_setup_seen = False
        index = 0
        while index < len(args):
            token = args[index]
            if token == "--dry-run":
                dry_run = True
            elif token == "--changed-setup":
                value, index = _value(args, index, token)
                if changed_setup_seen:
                    raise _problem("invalid-config", "option cannot be repeated")
                changed_setup_seen = True
                try:
                    changed_setup = init_changed.parse_choice(value)
                except C.Problem as problem:
                    raise _problem(problem.code, problem.message) from None
                continue
            elif token == "--reveal-command":
                reveal = True
            elif token == "--runner":
                value, index = _value(args, index, token)
                try:
                    runner = C.RunnerKind(value)
                except ValueError:
                    raise _problem("unsupported-capability", "runner profile is not supported") from None
                continue
            elif token == "--child":
                child, index = _value(args, index, token)
                if index >= len(args) or args[index] != "--runner":
                    raise _problem("invalid-config", "--child requires a following --runner")
                value, index = _value(args, index, "--runner")
                try:
                    child_runner = C.RunnerKind(value)
                except ValueError:
                    raise _problem("unsupported-capability", "runner profile is not supported") from None
                children.append((child, child_runner))
                continue
            elif token == "--agents":
                value, index = _value(args, index, token)
                agents_explicit = True
                if value == "none":
                    agents = ()
                elif value == "all":
                    agents = agent_rules.SUPPORTED_AGENTS
                else:
                    names = tuple(part.strip() for part in value.split(","))
                    if (not names or any(not name for name in names)
                            or any(name not in agent_rules.SUPPORTED_AGENTS for name in names)):
                        raise _problem("unsupported-capability", "agent integration is not supported")
                    agents = tuple(dict.fromkeys(names))
                continue
            elif token == "--doctor":
                if doctor_seen or no_doctor_seen:
                    raise _problem("invalid-config", "init doctor modes cannot be combined or repeated")
                doctor_seen = True
                doctor_request = True
            elif token == "--no-doctor":
                if no_doctor_seen or doctor_seen:
                    raise _problem("invalid-config", "init doctor modes cannot be combined or repeated")
                no_doctor_seen = True
                doctor_request = False
            elif token == "--reviewer":
                value, index = _value(args, index, token)
                if reviewer_seen:
                    raise _problem("invalid-config", "option cannot be repeated")
                reviewer_seen = True
                supported = {"auto", *agent_providers.SUPPORTED_REVIEWERS}
                if value not in supported:
                    raise _problem("unsupported-capability", "reviewer is not supported")
                reviewer = value
                continue
            elif token == "--allow-model-review":
                if allow_seen:
                    raise _problem("invalid-config", "option cannot be repeated")
                allow_seen = True
                allow_model_review = True
            elif token == "--review-timeout":
                value, index = _value(args, index, token)
                if timeout_seen:
                    raise _problem("invalid-config", "option cannot be repeated")
                timeout_seen = True
                review_timeout_s = _integer(value, lo=10, hi=900)
                continue
            elif token == "--review-model":
                value, index = _value(args, index, token)
                if model_seen:
                    raise _problem("invalid-config", "option cannot be repeated")
                model_seen = True
                review_model = value
                continue
            elif token == "--review-concurrency":
                value, index = _value(args, index, token)
                if concurrency_seen:
                    raise _problem("invalid-config", "option cannot be repeated")
                concurrency_seen = True
                review_concurrency = _integer(value, lo=1, hi=8)
                continue
            elif token == "--smoke":
                if smoke_seen or no_smoke_seen:
                    raise _problem("invalid-config", "init smoke modes cannot be combined or repeated")
                smoke_seen = True
                smoke = True
            elif token == "--no-smoke":
                if no_smoke_seen or smoke_seen:
                    raise _problem("invalid-config", "init smoke modes cannot be combined or repeated")
                no_smoke_seen = True
                smoke = False
            elif token == "--json":
                # Init's frozen grammar omits --json, but accepting it is
                # harmless only when it is explicitly requested by automation.
                pass
            else:
                raise _problem("invalid-config", "unknown inspection option")
            index += 1
        review_options_seen = (reviewer_seen or allow_seen or timeout_seen
                               or model_seen or concurrency_seen)
        if review_options_seen:
            if doctor_request is not True:
                raise _problem("invalid-config", "review options require --doctor")
        if "--json" in args and (doctor_request is True or review_options_seen):
            raise _problem("invalid-config", "init review cannot be combined with --json")
        if dry_run and (doctor_request is True or review_options_seen):
            raise _problem("invalid-config", "init review cannot be combined with --dry-run")
        if "--json" in args and changed_setup is not None:
            raise _problem("invalid-config", "init changed-setup cannot be combined with --json")
        return ParsedArgs(command=command, runner=runner, dry_run=dry_run,
                          reveal_command=reveal, json="--json" in args,
                          children=tuple(children), agents=agents,
                          agents_explicit=agents_explicit,
                          reviewer=reviewer, reviewer_explicit=reviewer_seen,
                          allow_model_review=allow_model_review,
                          review_timeout_s=review_timeout_s,
                          review_timeout_explicit=timeout_seen,
                          review_model=review_model,
                          review_model_explicit=model_seen,
                          review_concurrency=review_concurrency,
                          review_concurrency_explicit=concurrency_seen,
                          doctor_request=doctor_request,
                          smoke=smoke, changed_setup=changed_setup)
    if command == "register":
        if any(token not in {"--json"} for token in args):
            raise _problem("invalid-config", "unknown inspection option")
        return ParsedArgs(command=command, json="--json" in args)
    if command == "rules":
        if not args:
            return ParsedArgs(command=command)
        if tuple(args) == ("--apply",):
            return ParsedArgs(command=command, apply_rules=True)
        raise _problem("invalid-config", "rules accepts only --apply")
    if command == "uninstall":
        uninstall_self = uninstall_yes = uninstall_dry = uninstall_json = False
        for token in args:
            if token == "--self":
                uninstall_self = True
            elif token == "--yes":
                uninstall_yes = True
            elif token == "--dry-run":
                uninstall_dry = True
            elif token == "--json":
                uninstall_json = True
            else:
                raise _problem("invalid-config", "unknown inspection option")
        return ParsedArgs(command=command, json=uninstall_json,
                          dry_run=uninstall_dry,
                          uninstall_self=uninstall_self,
                          uninstall_yes=uninstall_yes)
    if command in {"where", "status", "plan"}:
        allowed = {"--json", "--reveal-command"} if command == "where" else {"--json"}
        base = None
        index = 0
        while index < len(args):
            token = args[index]
            if token == "--base" and command == "plan":
                base, index = _value(args, index, token)
                continue
            if token not in allowed:
                raise _problem("invalid-config", "unknown inspection option")
            index += 1
        return ParsedArgs(command=command, json="--json" in args,
                          reveal_command="--reveal-command" in args,
                          base=base)
    if command == "history":
        target = None
        limit = 20
        index = 0
        while index < len(args):
            token = args[index]
            if token == "--json":
                index += 1
            elif token == "--limit":
                value, index = _value(args, index, token)
                limit = _integer(value, lo=1, hi=200)
            elif token.startswith("-"):
                raise _problem("invalid-config", "unknown inspection option")
            elif target is None:
                target = token
                index += 1
            else:
                raise _problem("invalid-config", "history accepts one filter")
        return ParsedArgs(command=command, json="--json" in args,
                          scope=target, history_limit=limit)
    if command == "doctor":
        json_output = False
        reviewer = None
        reviewer_seen = allow_seen = timeout_seen = False
        model_seen = concurrency_seen = False
        allow_model_review = offline = False
        offline_seen = False
        fix = fix_dry = False
        fix_seen = dry_seen = False
        review_timeout_s = 300
        review_model = None
        review_concurrency = 4
        scope = None
        limits: dict[str, int] = {}
        probe = False
        probe_repeat = 2
        probe_workers = 2
        probe_timeout = C.DEFAULT_ATTEMPT_TIMEOUT_S
        probe_no_setup = False
        probe_result_path = None
        probe_repeat_seen = probe_workers_seen = probe_timeout_seen = False
        probe_no_setup_seen = probe_result_path_seen = False
        index = 0
        while index < len(args):
            token = args[index]
            if token == "--json":
                json_output = True
            elif token in {"--scope", "--max-entries", "--max-files",
                           "--max-file-bytes", "--max-total-bytes"}:
                value, index = _value(args, index, token)
                if token == "--scope":
                    scope = value
                else:
                    limits[token[2:].replace("-", "_")] = _integer(value, lo=1, hi=10**9)
                continue
            elif token == "--reviewer":
                value, index = _value(args, index, token)
                if reviewer_seen:
                    raise _problem("invalid-config", "option cannot be repeated")
                reviewer_seen = True
                supported = {"auto", *agent_providers.SUPPORTED_REVIEWERS}
                if value not in supported:
                    raise _problem("unsupported-capability", "reviewer is not supported")
                reviewer = value
                continue
            elif token == "--allow-model-review":
                if allow_seen:
                    raise _problem("invalid-config", "option cannot be repeated")
                allow_seen = True
                allow_model_review = True
            elif token == "--review-timeout":
                value, index = _value(args, index, token)
                if timeout_seen:
                    raise _problem("invalid-config", "option cannot be repeated")
                timeout_seen = True
                review_timeout_s = _integer(value, lo=10, hi=900)
                continue
            elif token == "--review-model":
                value, index = _value(args, index, token)
                if model_seen:
                    raise _problem("invalid-config", "option cannot be repeated")
                model_seen = True
                review_model = value
                continue
            elif token == "--review-concurrency":
                value, index = _value(args, index, token)
                if concurrency_seen:
                    raise _problem("invalid-config", "option cannot be repeated")
                concurrency_seen = True
                review_concurrency = _integer(value, lo=1, hi=8)
                continue
            elif token == "--offline":
                if offline_seen:
                    raise _problem("invalid-config", "option cannot be repeated")
                offline_seen = True
                offline = True
            elif token == "--fix":
                if fix_seen:
                    raise _problem("invalid-config", "option cannot be repeated")
                fix_seen = True
                fix = True
            elif token == "--dry-run":
                if dry_seen:
                    raise _problem("invalid-config", "option cannot be repeated")
                dry_seen = True
                fix_dry = True
            elif token == "--probe":
                if probe:
                    raise _problem("invalid-config", "option cannot be repeated")
                probe = True
            elif token == "--repeat":
                value, index = _value(args, index, token)
                probe_repeat = _probe_integer(value, lo=1, hi=5)
                probe_repeat_seen = True
                continue
            elif token == "--workers":
                value, index = _value(args, index, token)
                probe_workers = _probe_integer(value, lo=1, hi=64)
                probe_workers_seen = True
                continue
            elif token == "--attempt-timeout":
                value, index = _value(args, index, token)
                probe_timeout = _probe_number(value, lo=1, hi=120)
                probe_timeout_seen = True
                continue
            elif token == "--no-setup":
                probe_no_setup = True
                probe_no_setup_seen = True
            elif token == "--result-json":
                probe_result_path, index = _value(args, index, token)
                probe_result_path_seen = True
                continue
            else:
                raise _problem("invalid-config", "unknown inspection option")
            index += 1
        probe_options_seen = (
            probe_repeat_seen or probe_workers_seen or probe_timeout_seen
            or probe_no_setup_seen or probe_result_path_seen)
        if probe:
            if scope is None:
                raise _problem("invalid-config", "doctor probe requires --scope")
            if fix or fix_dry:
                raise _problem("invalid-config", "doctor probe cannot combine with --fix")
            if json_output:
                raise _problem("invalid-config", "doctor probe cannot combine output modes")
            if (reviewer_seen or allow_seen or timeout_seen
                    or model_seen or concurrency_seen or offline):
                raise _problem("invalid-config", "doctor probe cannot combine review modes")
            if limits:
                raise _problem("invalid-config", "doctor probe cannot combine static scan limits")
            return ParsedArgs(
                command=command, scope=scope,
                probe=C.ProbeOptions(scope=scope, repeat=probe_repeat,
                                     workers=probe_workers,
                                     attempt_timeout_s=probe_timeout),
                no_setup=probe_no_setup, result_path=probe_result_path,
            )
        if probe_options_seen:
            raise _problem("invalid-config", "probe options require --probe")
        if (reviewer_seen or allow_seen or timeout_seen or model_seen
                or concurrency_seen) and offline:
            raise _problem("invalid-config", "review options cannot combine with --offline")
        if fix:
            if json_output or scope is not None or limits:
                raise _problem("invalid-config", "--fix takes no output, scope, or scan-limit options")
            if (reviewer_seen or allow_seen or timeout_seen or model_seen
                    or concurrency_seen):
                raise _problem("invalid-config", "--fix never runs a model review")
            return ParsedArgs(command=command, fix=fix,
                              dry_run=fix_dry, offline=offline)
        if fix_dry:
            raise _problem("invalid-config", "fix options require --fix")
        return ParsedArgs(command=command, json=json_output,
                          scope=scope, reviewer=reviewer,
                          reviewer_explicit=reviewer_seen,
                          allow_model_review=allow_model_review,
                          review_timeout_s=review_timeout_s,
                          review_timeout_explicit=timeout_seen,
                          review_model=review_model,
                          review_model_explicit=model_seen,
                          review_concurrency=review_concurrency,
                          review_concurrency_explicit=concurrency_seen,
                          offline=offline, **limits)
    if command == "guide":
        if not args:
            return ParsedArgs(command=command)
        if args[0] != "--write":
            raise _problem("invalid-config", "unknown inspection option")
        write, index = _value(args, 0, "--write")
        if index != len(args):
            raise _problem("invalid-config", "unknown inspection option")
        return ParsedArgs(command=command, write=write)
    raise _problem("invalid-config", "unknown command")


_COMMAND_WORD = re.compile(r"[a-z][a-z-]{0,31}")
# Words people reach for that mean an existing command.
_COMMAND_ALIASES = {"install": "init", "setup": "init", "configure": "init"}


class _UnknownCommand(C.Problem):
    """A bare word that is neither a command nor an existing test path."""

    def __init__(self, word: str) -> None:
        commands = sorted(_INSPECTION | {"help", "changed"})
        guess = _COMMAND_ALIASES.get(word) or next(
            iter(difflib.get_close_matches(word, commands, n=1, cutoff=0.7)), None)
        hint = f' Did you mean "ptest {guess}"?' if guess else ""
        super().__init__(code="invalid-config", phase="cli",
                         message=f'unknown command "{word}".{hint}')


def _looks_like_unknown_command(token: str) -> bool:
    # Only a plain lowercase word that names no existing path: runner tails,
    # test paths and node ids keep flowing to the runner unchanged.
    return (_COMMAND_WORD.fullmatch(token) is not None
            and not os.path.lexists(token))


def _parse_args(args: tuple[str, ...], prefix: _CliPrefix) -> ParsedArgs:
    if any(not isinstance(token, str) for token in args):
        raise _problem("invalid-config", "arguments must be strings")
    if prefix.problem is not None:
        raise prefix.problem
    remaining = args[prefix.remainder_index:]
    if prefix.command is not None:
        rest = remaining[1:]
        if "--help" in rest or "-h" in rest:
            # A help flag on a recognized inspection command never runs that
            # command. The flag must stand alone; mixed help+action arguments
            # are rejected instead of silently showing help or executing.
            if tuple(rest) not in (("--help",), ("-h",)):
                raise _problem("invalid-config",
                               "help cannot be combined with other options")
            parsed = ParsedArgs(command="help", help_topic=prefix.command)
        else:
            parsed = _parse_inspection(prefix.command, rest)
    elif remaining and remaining[0] == "help":
        # The topic is never echoed: unknown input stays out of the error so
        # hostile bytes cannot reach output; the fixed hint names the topics.
        rest = remaining[1:]
        if not rest:
            parsed = ParsedArgs(command="help")
        elif len(rest) == 1 and rest[0] in help_api.TOPICS:
            parsed = ParsedArgs(command="help", help_topic=rest[0])
        elif len(rest) == 1:
            raise _problem("invalid-config",
                           f"unknown help topic; {help_api.HINT}")
        else:
            raise _problem("invalid-config", "help accepts at most one topic")
    elif remaining and remaining[0] == "changed":
        parsed = _parse_execution(remaining[1:], command="changed")
    elif remaining and remaining[0] in {"--help", "-h"}:
        if len(remaining) > 1:
            raise _problem("invalid-config",
                           "help accepts no additional arguments")
        parsed = ParsedArgs(command="help")
    elif remaining and remaining[0] == "--version":
        parsed = ParsedArgs(command="version")
    elif remaining and _looks_like_unknown_command(remaining[0]):
        raise _UnknownCommand(remaining[0])
    else:
        parsed = _parse_execution(remaining)
    return replace(parsed, fixture_domain=prefix.fixture_domain)


def parse_argv(argv: Sequence[str] | None = None) -> ParsedArgs:
    """Parse ptest's closed prefix grammar without inspecting runner tails."""
    args = tuple(sys.argv[1:] if argv is None else argv)
    return _parse_args(args, _walk_cli_prefix(args))


def _domain_public(domain: C.DomainPaths | None) -> dict | None:
    if domain is None or domain.domain_id is None:
        return None
    return {"id": domain.domain_id, "fixture": domain.fixture}


def _domain_from_env(domain: C.DomainPaths | None) -> bool:
    return bool(domain is not None and not domain.fixture
                and os.environ.get("PTEST_STATE_DIR"))


def _domain_facts(domain: C.DomainPaths | None) -> dict:
    return {
        "domain_root": None if domain is None else str(domain.root),
        "domain_from_env": _domain_from_env(domain),
    }


def _domain_text(domain: C.DomainPaths | None) -> str:
    text = "domain: " + render.terminal_text(
        "<unresolved>" if domain is None else str(domain.root))
    if _domain_from_env(domain):
        text += " (PTEST_STATE_DIR)"
    return text


def _checkout(config: C.Config) -> C.CheckoutIdentity:
    root = config.checkout.root if config.checkout else config.config_path.parent
    checkout_id = hashlib.sha256(os.fsencode(os.path.realpath(root))).hexdigest()[:32]
    return C.CheckoutIdentity(project_id=config.project_id,
                              checkout_id=checkout_id, root=root)


def _summary(config: C.Config) -> C.ConfigSummary:
    # Pytest executes under the enforced one-slot basic-serial grant, so its
    # static summaries report the effective serial worker count rather than
    # the configured value. Generic commands keep their configured count.
    workers = 1 if config.runner.kind is C.RunnerKind.PYTEST else config.runner.workers
    scoped = C.summarize_command(
        config.runner.kind, C.Mode.SCOPED,
        config.runner.launcher + config.runner.args,
        workers=workers, provenance=("config",),
    )
    full = C.summarize_command(
        config.runner.kind, C.Mode.FULL,
        config.runner.launcher + config.runner.args + config.runner.full_args
        + (config.runner.test_roots if config.runner.kind is C.RunnerKind.PYTEST else ()),
        workers=workers, provenance=("config",),
    )
    return C.summarize_config(config, scoped=scoped, full=full)


def _where_payload(resolution: C.ConfigResolution, domain: C.DomainPaths | None) -> dict:
    config = resolution.config
    if config is None:
        return {
            "root": str(resolution.root), "config_path": None,
            "initialized": False, "runner_kind": None, "capability": None,
            "commands": [], "effective_limits": {"max_slots": None,
            "max_jobs": None, "memory_mb": None, "repo_workers": None},
            "provenance": list(resolution.provenance),
            "warnings": [],
            **_domain_facts(domain),
        }
    adapter_for(config.runner.kind)  # closed registry validation only
    if config.runner.kind is C.RunnerKind.PYTEST:
        inspected = pytest_adapter.inspect_capability(config)
    elif config.runner.kind is C.RunnerKind.VITEST:
        inspected = C.Capability(
            execution=C.ExecutionTier.EXCLUSIVE_COMMAND, selection=False,
            lifecycle="cooperative-process-group",
            limitations=(C.Reason(
                code="unsupported-capability",
                message=vitest_adapter.VITEST_EXCLUSIVE_NOTE,
            ),),
        )
    else:
        inspected = C.Capability(
            execution=C.ExecutionTier.UNAVAILABLE, selection=False,
            lifecycle="cooperative-process-group",
            limitations=(C.Reason(
                code="unsupported-capability",
                message="unavailable",
            ),),
        )
    capability = {
        "execution": inspected.execution.value,
        "selection": inspected.selection,
        "lifecycle": inspected.lifecycle,
        "limitations": [_reason(item) for item in inspected.limitations],
    }
    try:
        limits = scheduler.effective_limits(domain) if domain is not None else C.EffectiveLimits()
    except C.Problem:
        limits = C.EffectiveLimits()
    summary = _summary(config)
    return {
        "root": str(resolution.root),
        "config_path": None if resolution.path is None else str(resolution.path),
        "initialized": True,
        "runner_kind": config.runner.kind.value,
        "capability": capability,
        "commands": [
            {"kind": item.kind.value, "mode": item.mode.value,
             "argument_count": item.argument_count,
             "generated_options": list(item.generated_options),
             "workers": item.workers, "provenance": list(item.provenance)}
            for item in summary.commands
        ],
        "effective_limits": {
            "max_slots": limits.max_slots, "max_jobs": limits.max_jobs,
            "memory_mb": limits.memory_mb, "repo_workers": limits.repo_workers,
        },
        "provenance": list(resolution.provenance),
        "warnings": [_reason(item) for item in resolution.warnings],
        **_domain_facts(domain),
    }


def _reason(reason: C.Reason) -> dict:
    return {"code": reason.code, "message": reason.message,
            "paths": list(reason.paths)}


def _document(kind: str, payload: dict | None = None,
              *, error: C.Problem | None = None,
              domain: C.DomainPaths | None = None) -> bytes:
    return render.render_json(C.PublicDocument(
        kind=kind, ptest_version=C.PTEST_VERSION, domain=_domain_public(domain),
        data=payload, error=error,
    ))


def _emit_error(problem: C.Problem, *, kind: str, json_output: bool,
                domain: C.DomainPaths | None = None) -> int:
    if json_output:
        sys.stdout.buffer.write(_document(kind, error=problem, domain=domain))
    else:
        # A refused run keeps its existing `code: message` line; when the
        # run never got to show it, the line also carries the once-per-run
        # verbosity hint. Machine documents stay byte-identical.
        suffix = ""
        if kind == "run" and progress.claim_hint():
            suffix = f" · {progress.HINT}"
        print(render.terminal_text(problem) + suffix, file=sys.stderr)
    if problem.code in {"review-timeout", "execution-timeout"}:
        return 124
    if problem.code in {"review-cancelled", "cancelled"}:
        return 130
    return 2 if problem.code not in {"coordinator-unavailable", "queue-timeout"} else 75


def _interactive_review() -> bool:
    """TTY review is interactive only outside CI, even if stdin is a TTY."""
    return sys.stdin.isatty() and "CI" not in os.environ


def _ask_init_smoke(runnable: Sequence[init_smoke.SmokePlan]) -> bool:
    """TTY consent for the smoke run; names each file that would execute."""
    print("Smoke candidates:", file=sys.stderr)
    for plan in runnable:
        print(f"  {render.terminal_text(
            init_smoke.display_command(plan.project, plan.candidate))}",
              file=sys.stderr)
    print(init_smoke.SMOKE_QUESTION, file=sys.stderr)
    try:
        answer = input()
    except (EOFError, KeyboardInterrupt):
        return False
    return init_smoke.parse_consent(answer)


def _ask_init_setup(plan: init_smoke.SmokePlan) -> bool:
    """TTY consent to run owed setup through ptest before the smoke."""
    argv = render.terminal_text(" ".join(plan.setup_argv or ()))
    print(init_smoke.SETUP_QUESTION.format(argv=argv), file=sys.stderr)
    try:
        answer = input()
    except (EOFError, KeyboardInterrupt):
        return False
    return init_smoke.parse_consent(answer)


def _init_facts(cwd: Path) -> tuple:
    """Best-effort FACT_KEYS dicts per project for the init renderers."""
    try:
        items = executability.check_resolution(
            config_api.resolve_config(cwd))
        return tuple(item.facts() for item in items)
    except Exception:
        # Facts never change init's configuration-based outcome: trouble
        # resolving them renders the notes-only shape instead.
        return ()


def _render_init_text(result, rules, *, parsed: ParsedArgs,
                      repo_name: str, facts: tuple) -> str:
    """Render the init header: wordmark, projects, grouped file actions."""
    return init_render.render_init(
        result, rules, dry_run=parsed.dry_run, agents=parsed.agents,
        repo_name=repo_name, color=sys.stdout.isatty(), facts=facts)


def _render_init_footer_text(result, rules, *, parsed: ParsedArgs,
                             smoke: tuple, plans: tuple,
                             facts: tuple) -> str:
    """Render the init footer (smoke, next steps, restart); "" when none.

    The footer owns the smoke block: callers must not also write
    ``init_smoke.format_smoke`` output. Fail-closed: a renderer bug
    surfaces instead of silently dropping next steps or the restart line.
    """
    return init_render.render_init_footer(
        result, rules, dry_run=parsed.dry_run, smoke=smoke,
        plans=plans, facts=facts, color=sys.stdout.isatty())


def _init_smoke_results(parsed: ParsedArgs, cwd: Path, plans: tuple,
                        domain) -> tuple:
    """One smoke run per project; never changes init's outcome or status.

    Dry runs, machine output, and explicit opt-out never execute. TTY init
    asks once; non-interactive init runs only with ``--smoke``. Owed setup
    never installs silently: TTY init offers to run it through ptest first,
    and non-interactive init skips with the working advice. The caller
    formats the results and feeds them to the init footer.
    """
    if parsed.dry_run or parsed.json or parsed.smoke is False:
        return ()
    if not plans:
        return ()
    if parsed.smoke is True:
        return tuple(
            init_smoke.skip_result(plan, init_smoke.setup_advice(plan))
            if plan.skip_reason is None and plan.setup_argv is not None
            else init_smoke.run_plan(
                domain, plan, fixture_domain=parsed.fixture_domain)
            for plan in plans
        )
    runnable = [plan for plan in plans if plan.skip_reason is None]
    if not runnable or not _interactive_review() \
            or not _ask_init_smoke(runnable):
        return ()
    results = []
    for plan in plans:
        if plan.skip_reason is not None:
            results.append(init_smoke.skip_result(plan, plan.skip_reason))
        elif plan.setup_argv is not None and not _ask_init_setup(plan):
            results.append(
                init_smoke.skip_result(plan, init_smoke.setup_advice(plan)))
        else:
            if plan.setup_argv is not None:
                reason = init_smoke.run_setup(
                    domain, plan, fixture_domain=parsed.fixture_domain)
                if reason is not None:
                    results.append(init_smoke.skip_result(plan, reason))
                    continue
            results.append(init_smoke.run_plan(
                domain, plan, fixture_domain=parsed.fixture_domain))
    return tuple(results)


def _plan_targets(smoke_plans: tuple) -> tuple:
    """``((declaration, config), ...)`` from the smoke plans.

    The plans resolve the just-written (or pre-existing) configs, so
    the coverage probe and the planner both see the current bytes
    without a second child preflight.
    """
    return tuple(
        (plan.project, plan.config)
        for plan in smoke_plans if plan.config is not None)


def _preview_targets(root: Path, result, parsed: ParsedArgs) -> tuple:
    """``((declaration, probe config), ...)`` init would create, for dry-run.

    Probe configs are built with the same ``_fresh_config`` init writes,
    so the frozen-pair check previews the real outcome; nothing is read
    from or written to disk.
    """
    if result.config is not None:
        kind = result.config.runner_kind
        if kind is not C.RunnerKind.PYTEST:
            return ()
        return ((".", config_api._fresh_config(
            root, result.target, kind)),)
    children = parsed.children or config_api._auto_monorepo_children(root)
    targets = []
    for declaration, kind in children:
        if kind is not C.RunnerKind.PYTEST:
            continue
        child_root = root.joinpath(*declaration.split("/"))
        targets.append((declaration, config_api._fresh_config(
            child_root, child_root / ".ptest.toml", kind)))
    return tuple(targets)


def _run_changed_baseline(parsed: ParsedArgs, root: Path,
                          declaration: str) -> None:
    """Run one full baseline for a fresh `--changed` setup.

    A failure never changes init's outcome or status: runner outcomes
    surface through the normal run output, infrastructure problems
    become one skip line.
    """
    from . import monorepo
    resolution = config_api.resolve_config(root)
    if declaration == ".":
        config = resolution.config
    else:
        diagnosis = monorepo.diagnose_child(root, declaration)
        config = diagnosis.config if diagnosis.kind == "ok" else None
    if config is None:
        print(f"{declaration}: baseline run skipped "
              "(configuration is unavailable)")
        return
    try:
        domain = platform.domain_paths(parsed.fixture_domain)
        full = operations.execute(
            domain, config,
            C.RunRequest(mode=C.Mode.FULL, workers=parsed.workers,
                         queue_timeout_s=parsed.queue_timeout_s,
                         fixture_domain=parsed.fixture_domain))
    except C.Problem as problem:
        print(f"{declaration}: baseline run skipped ({problem.code})")
        return
    for reason in full.reasons:
        print(render.terminal_text(f"{reason.code}: {reason.message}"),
              file=sys.stderr)


def _run_changed_setup(parsed: ParsedArgs, cwd: Path, result,
                       smoke_plans: tuple = ()) -> None:
    """Post-smoke `--changed` setup: question, config draft, baseline run.

    One line per pytest project; vitest and other runners are never
    asked. `--json` never reaches here (rejected at parse); dry runs
    preview only; existing configs point at `doctor --fix` and are
    never rewritten.
    """
    if parsed.json:
        return
    if parsed.dry_run:
        root = result.target.parent
        for declaration, probe in _preview_targets(root, result, parsed):
            project = render.terminal_text(declaration)
            if init_changed.has_frozen_pair(probe):
                print(init_changed.DRY_RUN_LINE.format(project=project))
            else:
                print(init_changed.NEEDS_COV_LINE.format(project=project))
        return
    root = config_api.resolve_config(cwd).root
    targets = _plan_targets(smoke_plans)
    if result.action is C.InitAction.EXISTING:
        for declaration, config in targets:
            if config.runner.kind is not C.RunnerKind.PYTEST:
                continue
            project = render.terminal_text(declaration)
            if not init_changed.has_frozen_pair(config):
                print(init_changed.NEEDS_COV_LINE.format(project=project))
            elif not init_changed.selection_enabled(config):
                print(init_changed.EXISTING_LINE.format(project=project))
        return
    for declaration, config in targets:
        if config.runner.kind is not C.RunnerKind.PYTEST:
            continue
        project = render.terminal_text(declaration)
        if not init_changed.has_frozen_pair(config):
            print(init_changed.NEEDS_COV_LINE.format(project=project))
            continue
        choice = parsed.changed_setup
        if choice is None:
            if _interactive_review():
                choice = init_changed.ask_choice(declaration)
            else:
                choice = init_changed.DEFAULT_CHOICE
        if choice == "no":
            continue
        init_changed.apply_setup(
            root, init_changed.SetupTarget(declaration, config))
        if choice == "now":
            print(init_changed.NOW_LINE.format(project=project))
            _run_changed_baseline(parsed, root, declaration)
        else:
            print(init_changed.LATER_LINE.format(project=project))


def _review_consent_problem() -> C.Problem:
    return _problem(
        "consent-required",
        "non-interactive review requires an explicit reviewer and "
        "--allow-model-review",
    )


def _collect_installed_reviewers() -> tuple[tuple, list[str]]:
    """Collect qualified installed adapters in SUPPORTED_REVIEWERS order.

    Installed means resolve_reviewer does not raise provider-unavailable.
    Returns (installed, unsupported) where unsupported carries the
    qualification notes for the existing provider-unqualified message.
    """
    installed = []
    unsupported: list[str] = []
    for name in agent_providers.SUPPORTED_REVIEWERS:
        status = agent_providers.qualification_status(name)
        if not status.qualified:
            unsupported.append(f"{name} is not supported: {status.note}")
            continue
        try:
            installed.append(agent_providers.resolve_reviewer(name, os.environ))
        except C.Problem as problem:
            if problem.code == "provider-unavailable":
                continue
            raise
    return tuple(installed), unsupported


def _unqualified_review_problem(unsupported: list[str]) -> C.Problem:
    detail = "install claude or codex"
    if unsupported:
        detail += "; " + "; ".join(unsupported)
    return _problem(
        "provider-unqualified",
        "no qualified review provider is installed (" + detail + ")",
    )


def _choose_review_adapter(parsed: ParsedArgs, *, interactive: bool):
    """Resolve one adapter, or None when the user skips the reviewer menu.

    An explicit concrete reviewer never shows a menu. Otherwise the
    qualified installed reviewers are collected in SUPPORTED_REVIEWERS
    order: none fails closed as before, one is used directly, and two or
    more are offered once by number. An empty, invalid, out-of-range, or
    EOF menu answer is a decline (None), exactly like declining the
    disclosure prompt.
    """
    selected = parsed.reviewer
    if selected not in (None, "auto"):
        return agent_providers.resolve_reviewer(selected, os.environ)

    if not interactive:
        # Automation must name a concrete provider; auto selection must never
        # turn an explicit consent flag into permission to inspect PATH.
        raise _review_consent_problem()

    installed, unsupported = _collect_installed_reviewers()
    if not installed:
        raise _unqualified_review_problem(unsupported)
    if len(installed) == 1:
        return installed[0]
    lines = ["Choose a reviewer for this review:"]
    for index, adapter in enumerate(installed, start=1):
        lines.append(f"  {index}) {render.terminal_text(adapter.name)}")
    print("\n".join(lines) + "\nNumber (Enter to skip): ",
          end="", file=sys.stderr, flush=True)
    try:
        answer = input().strip()
    except EOFError:
        return None
    try:
        choice = int(answer)
    except ValueError:
        return None
    if not 1 <= choice <= len(installed):
        return None
    return installed[choice - 1]


def _declined_review_output(parsed: ParsedArgs, resolution: C.ConfigResolution,
                            domain: C.DomainPaths) -> bool:
    """Render the shared decline outcome: offline result, exit 0 upstream."""
    print(
        "Optimization review is disabled; showing offline static doctor output.",
        file=sys.stderr,
    )
    _doctor_static_output(parsed, resolution, domain)
    return True


def _require_review_qualification(selected: str | None = None) -> None:
    """Enforce only the selected provider's shared qualification record.

    The shared status record stays the sole qualification authority: an
    unqualified selection fails closed with provider-unqualified and the
    record note, before any launch. Auto selection enforces per provider
    while resolving.
    """
    if selected in (None, "auto"):
        return
    status = agent_providers.qualification_status(selected)
    if not status.qualified:
        raise _problem(
            "provider-unqualified",
            f"reviewer {selected} is unqualified: {status.note}",
        )


_REVIEW_MODEL_CACHE_DIR = "review-models"
_REVIEW_MODEL_CACHE_MAX_BYTES = 4096
_MODEL_PICK_TIMEOUT_S = 120
_MODEL_PICK_SCHEMA = b'{"type":"string","description":"one listed model slug"}'


def _declared_review_model(provider: str, override: str | None,
                           environ: Mapping[str, str]) -> str | None:
    """Pre-consent, subprocess-free model choice: flag, env, claude alias.

    Returns None when the model is decided after consent (codex discovery
    plus one pick call in :func:`_resolve_review_model`).
    """
    if not isinstance(provider, str):
        raise TypeError("provider must be str")
    if override is not None and not isinstance(override, str):
        raise TypeError("override must be str or None")
    if not isinstance(environ, Mapping):
        raise TypeError("environ must be a mapping")
    if override:
        return override
    env_value = environ.get("PTEST_REVIEW_MODEL")
    if isinstance(env_value, str) and env_value:
        return env_value
    if provider == "claude":
        return "haiku"
    return None


def _read_cached_review_model(cache_root: Path, provider: str,
                              cli_version: str | None) -> str | None:
    """Cached pick result, honored only for the same CLI version."""
    if cli_version is None:
        return None
    try:
        raw = files.read_regular(Path(cache_root), f"{provider}.json",
                                 _REVIEW_MODEL_CACHE_MAX_BYTES + 1)
    except (OSError, C.Problem):
        return None
    if len(raw) > _REVIEW_MODEL_CACHE_MAX_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("cli_version") != cli_version:
        return None
    model = data.get("model")
    if not isinstance(model, str) or not model:
        return None
    if agent_providers.MODEL_RE.fullmatch(model) is None:
        # Corrupted entry: fall back instead of failing every review with
        # invalid-bound until the CLI version changes.
        return None
    return model


def _write_cached_review_model(cache_root: Path, provider: str,
                               cli_version: str | None, model: str) -> None:
    """Persist a pick result; cache failures never fail the review."""
    if cli_version is None:
        return
    try:
        # The caller created cache_root with ensure_private_dir already.
        files.publish_atomic(
            Path(cache_root), f"{provider}.json",
            json.dumps({"cli_version": cli_version, "model": model},
                       sort_keys=True).encode("utf-8"))
    except (OSError, C.Problem):
        pass


def _pick_review_model(adapter, entries: tuple[dict, ...]) -> str | None:
    """One pick call carrying only the listed models; exact slug or None."""
    slugs = [entry["slug"] for entry in entries]
    payload = {
        "instruction": (
            "Reply with exactly one listed model slug: the single "
            "cheapest adequate model for a bounded source-text checklist "
            "review. Output only the slug, with no other text."),
        "models": [{"slug": entry["slug"],
                    "display_name": entry["display_name"],
                    "description": entry["description"]}
                   for entry in entries],
    }
    try:
        request = json.dumps(payload, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return None
    try:
        result = agent_providers.launch_review(
            adapter, request, _MODEL_PICK_SCHEMA, _MODEL_PICK_TIMEOUT_S,
            lambda event: None)
    except C.Problem:
        return None
    if not result.ok:
        return None
    try:
        choice = result.assessment.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    return choice if choice in slugs else None


def _resolve_review_model(adapter, cache_root: Path,
                          declared: str | None) -> tuple[str | None,
                                                         str | None]:
    """Post-consent model choice: declared, cache, cheap pick, default.

    Returns ``(model, cli_version)``; ``model`` None means the provider
    default (no model flag). Declared models win and are never cached.
    """
    if not isinstance(adapter, agent_providers.ReviewerAdapter):
        raise TypeError("adapter must be ReviewerAdapter")
    if declared is not None and not isinstance(declared, str):
        raise TypeError("declared must be str or None")
    version = agent_providers.cli_version(adapter)
    if declared:
        return (declared, version)
    if adapter.name == "claude":
        return ("haiku", version)
    if adapter.name == "codex":
        cached = _read_cached_review_model(cache_root, adapter.name,
                                           version)
        if cached is not None:
            return (cached, version)
        try:
            entries = agent_providers.discover_model_entries(adapter)
        except OSError:
            entries = ()
        if entries:
            picked = _pick_review_model(adapter, entries)
            if picked is not None:
                _write_cached_review_model(cache_root, adapter.name,
                                           version, picked)
                return (picked, version)
        return (None, version)
    return (None, version)


def _render_review_disclosure(adapter, resolution: C.ConfigResolution,
                              *, ask: bool = True, calls: int | None = None,
                              concurrency: int = 4,
                              model: str | None = None) -> bool:
    """Short pre-consent disclosure: at most three lines, then the prompt.

    Line 1 names the provider, the project, and the planned model calls;
    lines 2-3 name the exclusions, the cost note, and the pointer to the
    full legal text (``ptest doctor --help``) and the model-free static
    review (``ptest doctor --offline``).
    """
    if sys.stderr.isatty():
        # The collecting spinner line has no trailing newline; terminate
        # it before the consent text, as the success/error paths do.
        print(file=sys.stderr)
    if calls is not None:
        if isinstance(calls, bool) or not isinstance(calls, int) \
                or calls < 0:
            raise TypeError("calls must be a nonnegative int or None")
        if isinstance(concurrency, bool) or not isinstance(concurrency, int):
            raise TypeError("concurrency must be int")
    provider = render.terminal_text(adapter.name[:120])
    project = render.terminal_text(resolution.root.name[:120])
    if calls is None:
        first = (f"Model review disclosure: {provider} gets bounded source "
                 f"excerpts from {project}.")
    else:
        chosen = (render.terminal_text(str(model)[:128])
                  if model else "")
        tail = (f"model {chosen}."
                if chosen else
                "model chosen from the provider list after consent "
                "(one extra call sends only that list).")
        first = (f"Model review disclosure: {provider} gets bounded source "
                 f"excerpts from {project}: {calls} calls, "
                 f"{concurrency} at a time, {tail}")
    disclosure = "\n".join((
        first,
        "Secrets, private files, agent instructions, dependency folders, "
        "caches and build output are never sent. Provider costs may apply.",
        "Full disclosure: ptest doctor --help. Static review without a "
        "model: ptest doctor --offline.",
    ))
    if not ask:
        print(disclosure, file=sys.stderr)
        return True
    print(
        disclosure + "\n"
        "Run this review once? [y/N]: ",
        end="", file=sys.stderr, flush=True,
    )
    try:
        answer = input().strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _resolution_items(resolution: C.ConfigResolution) -> tuple:
    """One executability pass per doctor run, shared by facts and answers."""
    return executability.check_resolution(resolution)


def _project_facts(resolution: C.ConfigResolution,
                   *, _items=None) -> dict[str, dict]:
    """Map each project declaration to its FACT_KEYS facts dict."""
    items = _items if _items is not None else _resolution_items(resolution)
    return {item.project: item.facts() for item in items}


def _execution_facts(resolution: C.ConfigResolution,
                     *, _items=None) -> dict[str, dict]:
    """Map each project to its public executability fact for review children."""
    items = _items if _items is not None else _resolution_items(resolution)
    return {item.project: item.to_public() for item in items}


def _plan_item_reviews(packet, domain: C.DomainPaths,
                       resolution: C.ConfigResolution, *, facts=None):
    """Plan per-item reviews, answering deterministically where possible.

    Deterministic items (TIMING-001, SELECT-001, PARALLEL-001) are
    answered from ptest's own facts with no model call; every other item
    keeps its provider request. Fail-closed: deterministic answers are
    always applied, so the disclosed call count and the 'no model call'
    promise stay exact. Callers that already hold this packet's
    executability facts pass them in so the config is checked once.
    """
    from . import deterministic_items as deterministic

    answers = deterministic.answers_for(domain, resolution, packet,
                                        facts=facts)
    return agent_assessment.plan_item_reviews(packet, answers=answers)


def _assemble_with_parallel(packet, reviews, replies):
    """Assemble one child, finalizing the PARALLEL-001 safety gating.

    The planned PARALLEL-001 answer carries the safe provisional fix
    when no parallel runner is configured; once the sibling rows are
    assembled their safety outcomes are known, so the answer is
    finalized with ``deterministic_items.finalize_parallel`` and the
    child is reassembled when it changed. Pure otherwise: no model call
    and no new provider request either way.
    """
    from . import deterministic_items as deterministic

    assessment = agent_assessment.assemble_child(packet, reviews, replies)
    index = next((position for position, review in enumerate(reviews)
                  if review.item_id == deterministic.PARALLEL_ITEM_ID
                  and review.answer is not None), None)
    if index is None:
        return assessment
    statuses = {row.id: row.status for row in assessment.rows}
    safety_gap = any(statuses.get(item_id) == "gap"
                     for item_id in deterministic.PARALLEL_SAFETY_IDS)
    final = deterministic.finalize_parallel(
        reviews[index].answer, safety_gap)
    if final == reviews[index].answer:
        return assessment
    patched = (reviews[:index]
               + (replace(reviews[index], answer=final),)
               + reviews[index + 1:])
    return agent_assessment.assemble_child(packet, patched, replies)


def _review_failure_reason(result) -> str | None:
    """Map one per-item provider result to its review row reason.

    Returns None when the result carries a usable one-row reply; cancelled
    results never become rows and fail the whole review instead.
    """
    if result.cancelled:
        raise _problem("review-cancelled", "review was cancelled")
    if result.timed_out:
        return "timed out"
    if result.truncated or result.error == "output-exhausted":
        return "output exceeded its bound"
    if result.error == "tool-attempt":
        return "provider attempted a tool"
    if result.error == "invalid-assessment":
        return "invalid reply"
    if result.error == "provider-unavailable":
        return "provider unavailable"
    if result.ok and result.exit_code in (0, None):
        return None
    return "provider exited with an error"


# An all-failed review names only this many distinct reasons, most common
# first, each reason capped so the provider-failed message stays small.
_FAILURE_SUMMARY_MAX_REASONS = 5
_FAILURE_REASON_MAX_CHARS = 120


def _summarize_review_failures(reviewed_rows) -> str:
    """Count distinct per-item failure reasons, most common first.

    Reasons are ptest-owned validation/provider strings, but each is
    still bounded and passed through terminal_text so the aggregate
    message stays inert and small.
    """
    counts: dict[str, int] = {}
    for _, row in reviewed_rows:
        rationale = row.rationale
        if rationale.startswith(agent_assessment.FAILED_PREFIX):
            reason = rationale[len(agent_assessment.FAILED_PREFIX):]
        else:
            reason = rationale
        reason = render.terminal_text(reason[:_FAILURE_REASON_MAX_CHARS])
        counts[reason] = counts.get(reason, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    shown = ranked[:_FAILURE_SUMMARY_MAX_REASONS]
    parts = [f"{count} × {reason}" for reason, count in shown]
    extra = len(ranked) - len(shown)
    if extra:
        parts.append(f"+{extra} more reason{'s' if extra != 1 else ''}")
    return "; ".join(parts)


def _review_profile(model: str | None) -> str:
    """Provider profile naming the per-item review and its model."""
    profile = f"ptest-item-review-v1 model={model or 'provider-default'}"
    if len(profile.encode("utf-8")) > 128:
        return "ptest-item-review-v1"
    return profile


def _run_review_entry(parsed: ParsedArgs, resolution: C.ConfigResolution,
                      domain: C.DomainPaths, *, interactive: bool,
                      preconsented: bool = False) -> bool:
    """Return true for a declined offline result; all review paths fail closed."""
    if not interactive and not (
            parsed.reviewer_explicit and parsed.reviewer not in (None, "auto")
            and parsed.allow_model_review):
        raise _review_consent_problem()

    _require_review_qualification(parsed.reviewer)
    adapter = _choose_review_adapter(parsed, interactive=interactive)
    if adapter is None:
        return _declined_review_output(parsed, resolution, domain)
    if not adapter.qualified:
        raise _problem(
            "provider-unqualified",
            f"reviewer {adapter.name} is unqualified: "
            f"{adapter.qualification_note}",
        )
    return _run_doctor_review(parsed, resolution, domain, adapter=adapter,
                              interactive=interactive,
                              preconsented=preconsented)


def _doctor_limits(parsed: ParsedArgs) -> C.ScanLimits:
    return C.ScanLimits(
        entries=parsed.max_entries or C.DEFAULT_SCAN_LIMITS.entries,
        files=parsed.max_files or C.DEFAULT_SCAN_LIMITS.files,
        file_bytes=parsed.max_file_bytes or C.DEFAULT_SCAN_LIMITS.file_bytes,
        total_bytes=parsed.max_total_bytes or C.DEFAULT_SCAN_LIMITS.total_bytes,
        findings=C.DEFAULT_SCAN_LIMITS.findings,
        output_bytes=C.DEFAULT_SCAN_LIMITS.output_bytes,
        elapsed_s=C.DEFAULT_SCAN_LIMITS.elapsed_s,
        depth=C.DEFAULT_SCAN_LIMITS.depth,
        ast_nodes=C.DEFAULT_SCAN_LIMITS.ast_nodes,
    )


def _review_config_identity(resolution: C.ConfigResolution,
                            workspace: doctor.WorkspaceInspection) -> str:
    """Hash root and child config bytes in declaration order, without following links."""
    paths = []
    if resolution.path is not None:
        paths.append(resolution.path)
    for repo in workspace.repositories:
        if repo.config is not None and repo.config.config_path is not None:
            paths.append(repo.config.config_path)
    unique = {}
    for path in paths:
        try:
            relative = Path(path).relative_to(resolution.root).as_posix()
        except ValueError:
            raise _problem("stale-evidence", "configuration identity escaped the project root") from None
        raw = files.read_regular(resolution.root, relative,
                                 _REVIEW_CONFIG_MAX_BYTES + 1)
        if len(raw) > _REVIEW_CONFIG_MAX_BYTES:
            raise _problem("stale-evidence", "configuration identity exceeds its bound")
        unique[relative] = hashlib.sha256(raw).hexdigest()
    identity = {
        "root": str(resolution.root),
        "configurations": sorted(unique.items()),
        "declarations": [repo.declaration for repo in workspace.repositories],
    }
    return hashlib.sha256(json.dumps(
        identity, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True).encode("utf-8")).hexdigest()


def _previous_report_identity(root: Path):
    """Read a prior report through a no-follow descriptor for guarded replacement."""
    path = root / "recommendations.md"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise _problem("report-conflict", "existing recommendations.md cannot be opened safely") from None
    try:
        try:
            stamp = os.fstat(fd)
        except OSError:
            raise _problem("report-conflict", "existing recommendations.md cannot be inspected safely") from None
        if (not stat.S_ISREG(stamp.st_mode) or stamp.st_uid != os.geteuid()
                or stamp.st_size > 1024 * 1024):
            raise _problem("report-conflict", "existing recommendations.md cannot be replaced safely")
        chunks = []
        remaining = 1024 * 1024 + 1
        while remaining:
            try:
                chunk = os.read(fd, min(65536, remaining))
            except OSError:
                raise _problem("report-conflict", "existing recommendations.md cannot be read safely") from None
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > 1024 * 1024:
            raise _problem("report-conflict", "existing recommendations.md exceeds its bound")
        return recommendations.PublishedIdentity(
            sha256=hashlib.sha256(raw).hexdigest(), st_dev=stamp.st_dev,
            st_ino=stamp.st_ino, size=len(raw))
    finally:
        os.close(fd)


def _assessment_limitations(packets, *, top_level: bool = False) -> list[dict]:
    limitations = []
    if top_level:
        limitations.extend((
            {"code": "execution-not-run",
             "message": "No project tests or services were executed during this review.",
             "paths": []},
            {"code": "timing-unmeasured",
             "message": "No qualified runtime measurements were collected.",
             "paths": []},
        ))
    for packet in packets:
        if (not packet.excerpts or packet.excluded_count
                or packet.truncated_count):
            limitation = {
                "code": "partial-evidence",
                "message": (
                    "No source files were admitted to this project packet."
                    if not packet.excerpts else
                    f"Bounded evidence omitted "
                    f"{C.plural(packet.excluded_count, 'entry')} and "
                    f"truncated {C.plural(packet.truncated_count, 'file')} "
                    f"or excerpts."),
                "paths": [packet.scope],
            }
            if limitation not in limitations:
                limitations.append(limitation)
        for fact in packet.dependencies:
            code = {
                "missing": "dependency-missing",
                "unsupported": "dependency-unsupported",
                "uninspectable": "dependency-uninspectable",
            }.get(fact.status)
            if code is None:
                continue
            limitation = {"code": code, "message": fact.detail,
                          "paths": [] if fact.ref_path is None else [fact.ref_path]}
            if limitation not in limitations:
                limitations.append(limitation)
    return limitations[:64]


def _initialization_required_limitation(
        resolution: C.ConfigResolution) -> dict | None:
    """Describe the deterministic standalone blocker independently of review."""
    problem = resolution.problem
    if (resolution.config is None and resolution.monorepo is None
            and problem is not None
            and problem.code == "initialization-required"):
        return {
            "code": "capability-unsupported",
            "message": (
                "initialization-required: standalone ptest configuration is "
                "missing; ptest is not execution-ready until you run "
                "`ptest init`. Checklist percentages describe agent review "
                "only."),
            "paths": [],
        }
    return None


def _child_assessment_data(packet, assessment, limitations: list[dict], *,
                         execution: dict | None = None,
                         facts: dict | None = None) -> dict:
    score = assessment.score
    child = {
        "project_id": assessment.project_id,
        "scope": assessment.scope,
        "packet_sha256": assessment.packet_sha256,
        "rows": [{
            "id": row.id, "status": row.status,
            "rationale": row.rationale, "label": row.label,
            # Additive report-only count: the public validator accepts it
            # and projection drops it from the JSON document, while the
            # raw child data still carries it to recommendations.md.
            "dropped_citations": row.dropped_citations,
            "evidence": [{
                "path": item.path, "start_line": item.start_line,
                "end_line": item.end_line, "sha256": item.sha256,
            } for item in row.evidence],
        } for row in assessment.rows],
        "score": (None if score is None else {
            "satisfied": score.satisfied, "applicable": score.applicable,
            "percent": score.percent,
        }),
        "findings": [{
            "id": item.id, "summary": item.summary,
            "suggested_change": item.suggested_change,
            "recipe_id": item.recipe_id,
            "evidence": [{
                "path": cite.path, "start_line": cite.start_line,
                "end_line": cite.end_line, "sha256": cite.sha256,
            } for cite in item.evidence],
        } for item in assessment.findings],
        "limitations": limitations,
    }
    if execution is not None:
        child["execution"] = execution
    if facts is not None:
        # Terminal/report-only project facts: the public validator
        # tolerates the additive key and projection drops it from the
        # JSON document, while the renderers read it for the per-project
        # runs/parallel/setup lines (falling back to execution).
        child["facts"] = facts
    return child


def _state_anchor(root: Path) -> Path:
    """Anchor the inside-checkout refusal on the Git repository root."""
    try:
        return config_api.repository_root(root)
    except C.Problem:
        return root


def _run_doctor_review(parsed: ParsedArgs, resolution: C.ConfigResolution,
                       domain: C.DomainPaths, *, adapter, interactive: bool,
                       preconsented: bool = False) -> bool:
    """Collect, per-item review, revalidate, then publish one review.

    Returns true for a declined offline result; all review paths fail
    closed. One focused model call runs per (project, checklist item);
    deterministically skipped items take no call.
    """
    platform.validate_state_outside_checkout(domain, _state_anchor(resolution.root))
    started = time.monotonic()
    deadline = started + _REVIEW_TOTAL_TIMEOUT_S
    limits = _doctor_limits(parsed)
    previous = _previous_report_identity(resolution.root)
    active_progress = ["collecting", adapter.name, resolution.root.name]
    last_progress_at = [started]

    def ensure_deadline() -> None:
        if time.monotonic() >= deadline:
            raise _problem("review-timeout", "total review deadline expired")

    def emit_progress(phase: str, provider: str, project: str,
                      elapsed_s: float, emitted_at: float | None = None):
        line = (f"doctor: {render.terminal_text(phase)} "
                f"{render.terminal_text(project)} with "
                f"{render.terminal_text(provider)} · "
                f"{max(0, int(elapsed_s))}s")
        if sys.stderr.isatty():
            spinner = "|/-\\"[int(max(0, elapsed_s)) % 4]
            print(f"\r{spinner} {line}",
                  end="", file=sys.stderr, flush=True)
        else:
            print(line, file=sys.stderr, flush=True)
        last_progress_at[0] = (time.monotonic() if emitted_at is None
                               else emitted_at)

    def progress(phase: str, provider: str, project: str, elapsed_s: float):
        active_progress[:] = [phase, provider, project]
        emit_progress(phase, provider, project, elapsed_s)

    def heartbeat() -> None:
        now = time.monotonic()
        if now - last_progress_at[0] >= 15:
            phase, provider, project = active_progress
            emit_progress(phase, provider, project, now - started,
                          emitted_at=now)

    try:
        ensure_deadline()
        progress("collecting", adapter.name, resolution.root.name,
                 time.monotonic() - started)
        workspace = doctor.inspect_workspace(domain, resolution, limits, parsed.scope)
        ensure_deadline()
        packets = agent_assessment.build_packets(
            workspace, resolution, deadline=deadline, progress=heartbeat)
        ensure_deadline()
        if not packets:
            raise _problem("invalid-config", "no selected project evidence is available")
        if sum(len(packet.excerpts) for packet in packets) > _REVIEW_SOURCE_PROOF_MAX:
            raise _problem(
                "invalid-bound",
                "collected source exceeds the guarded report proof bound",
            )
        config_identity = _review_config_identity(resolution, workspace)
        declaration_set = tuple(repo.declaration for repo in workspace.repositories)
        # One executability pass for the whole run: the stale-evidence
        # check below raises when source or configuration changes during
        # review, so these facts stay valid through publication.
        review_items = _resolution_items(resolution)
        review_fact_map = _project_facts(resolution, _items=review_items)
        plans = [_plan_item_reviews(packet, domain, resolution,
                                    facts=review_fact_map.get(
                                        packet.declaration))
                 for packet in packets]
        ensure_deadline()
        calls = sum(1 for reviews in plans for review in reviews
                    if review.request is not None)
        declared = _declared_review_model(
            adapter.name, parsed.review_model, os.environ)
        model = None
        version = None
        effective = adapter
        if calls:
            if not _render_review_disclosure(
                    adapter, resolution,
                    ask=(interactive and not preconsented),
                    calls=calls, concurrency=parsed.review_concurrency,
                    model=declared):
                return _declined_review_output(parsed, resolution, domain)
            ensure_deadline()
            scheduler.prepare_state_directory(domain)
            cache_root = files.ensure_private_dir(
                domain.root, _REVIEW_MODEL_CACHE_DIR)
            ensure_deadline()
            model, version = _resolve_review_model(
                adapter, cache_root, declared)
            if model is not None:
                effective = agent_providers.with_model(adapter, model)
        assessments = []
        for packet, reviews in zip(packets, plans):
            ensure_deadline()
            pending = [(review.request, review.schema)
                       for review in reviews if review.request is not None]
            results: tuple = ()
            if pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _problem("review-timeout", "total review deadline expired")
                timeout_s = min(parsed.review_timeout_s, int(remaining))
                if timeout_s < 1:
                    raise _problem("review-timeout", "total review deadline expired")
                progress("reviewing", adapter.name, packet.scope,
                         time.monotonic() - started)
                try:
                    ensure_deadline()
                    results = agent_providers.launch_reviews(
                        effective, pending, timeout_s,
                        concurrency=parsed.review_concurrency,
                        on_done=lambda index, result, scope=packet.scope: progress(
                            "reviewing", adapter.name, scope,
                            time.monotonic() - started),
                        progress=lambda _event: heartbeat())
                except C.Problem as problem:
                    if problem.code == "review-cancelled":
                        raise _problem("review-cancelled", "review was cancelled") from None
                    raise
                except KeyboardInterrupt:
                    raise _problem("review-cancelled", "review was cancelled") from None
                ensure_deadline()
            replies = []
            cursor = 0
            for review in reviews:
                if review.request is None:
                    replies.append(None)
                    continue
                result = results[cursor]
                cursor += 1
                reason = _review_failure_reason(result)
                replies.append(result.assessment if reason is None else reason)
            progress("validating", adapter.name, packet.scope,
                     time.monotonic() - started)
            ensure_deadline()
            assessments.append(_assemble_with_parallel(
                packet, reviews, tuple(replies)))
            ensure_deadline()

        reviewed_rows = [(review, row)
                         for reviews, assessment in zip(plans, assessments)
                         for review, row in zip(reviews, assessment.rows)
                         if review.request is not None]
        if reviewed_rows and all(row.rationale.startswith(
                agent_assessment.FAILED_PREFIX) for _, row in reviewed_rows):
            raise _problem(
                "provider-failed",
                "review provider did not return a valid assessment: "
                + _summarize_review_failures(reviewed_rows))

        # Re-resolve config and source packets immediately before publication.
        progress("validating", adapter.name, resolution.root.name,
                 time.monotonic() - started)
        ensure_deadline()
        try:
            fresh_resolution = config_api.resolve_config(resolution.root)
            fresh_workspace = doctor.inspect_workspace(
                domain, fresh_resolution, limits, parsed.scope)
            ensure_deadline()
            fresh_packets = agent_assessment.build_packets(
                fresh_workspace, fresh_resolution, deadline=deadline,
                progress=heartbeat)
            fresh_declarations = tuple(
                repo.declaration for repo in fresh_workspace.repositories)
            fresh_identity = _review_config_identity(
                fresh_resolution, fresh_workspace)
            ensure_deadline()
        except C.Problem as problem:
            if problem.code == "review-timeout":
                raise
            raise _problem(
                "stale-evidence",
                "source or configuration could not be revalidated after review",
            ) from None
        if (fresh_declarations != declaration_set
                or fresh_identity != config_identity
                or tuple(packet.packet_sha256 for packet in fresh_packets)
                != tuple(packet.packet_sha256 for packet in packets)):
            raise _problem("stale-evidence", "source or configuration changed during review")

        facts = _execution_facts(resolution, _items=review_items)
        project_facts = _project_facts(resolution, _items=review_items)
        child_data = []
        initialization_blocker = _initialization_required_limitation(resolution)
        for packet, assessment in zip(packets, assessments):
            child_limitations = _assessment_limitations((packet,))
            if initialization_blocker is not None and packet.declaration == ".":
                child_limitations.insert(0, dict(initialization_blocker))
            child_data.append(_child_assessment_data(
                packet, assessment, child_limitations,
                execution=facts.get(packet.declaration),
                facts=project_facts.get(packet.declaration)))
        limitations = _assessment_limitations(packets, top_level=True)
        if initialization_blocker is not None:
            limitations.insert(0, dict(initialization_blocker))
        draft_data = {
            "schema": C.AGENT_ASSESSMENT_SCHEMA,
            "provider": {"name": adapter.name,
                         "cli_version": version or "unreported",
                         "profile": _review_profile(model)},
            "children": child_data,
            "limitations": limitations,
            "publication": {"status": "created", "path": "recommendations.md",
                            "sha256": "0" * 64},
        }
        draft_document = C.PublicDocument(
            kind="agent-assessment", ptest_version=C.PTEST_VERSION,
            domain=_domain_public(domain), data=draft_data, error=None)
        # Validate the complete public contract before any report write.
        render.render_json(draft_document)
        report_input = C.PublicDocument(
            kind="agent-assessment", ptest_version=C.PTEST_VERSION,
            domain=None, data=draft_data, error=None)
        report_payload = recommendations.render_recommendations(report_input)
        source_proof = [{
            "path": excerpt.path,
            "start_line": excerpt.start_line,
            "end_line": excerpt.end_line,
            "sha256": excerpt.sha256,
            "byte_count": len(excerpt.text.encode("utf-8")),
        } for packet in packets for excerpt in packet.excerpts]
        ensure_deadline()
        progress("publishing", adapter.name, resolution.root.name,
                 time.monotonic() - started)
        ensure_deadline()
        publication = recommendations.publish_recommendations(
            resolution.root, report_payload, previous,
            source_proof=source_proof, deadline=deadline)
        draft_data["publication"] = {
            "status": publication.status, "path": publication.path,
            "sha256": publication.sha256,
        }
        document = C.PublicDocument(
            kind="agent-assessment", ptest_version=C.PTEST_VERSION,
            domain=_domain_public(domain), data=draft_data, error=None)
        if sys.stderr.isatty():
            print(file=sys.stderr)
        if parsed.json:
            sys.stdout.buffer.write(render.render_json(document))
        else:
            sys.stdout.write(render.render_agent_assessment(
                child_data, workspace, report_path=publication.path,
                publication_status=publication.status,
                color=sys.stdout.isatty()))
            mention = _fix_mention(resolution)
            if mention is not None:
                sys.stdout.write(mention + "\n")
        return False
    except C.Problem:
        if sys.stderr.isatty():
            print(file=sys.stderr)
        raise
    except KeyboardInterrupt:
        if sys.stderr.isatty():
            print(file=sys.stderr)
        raise _problem("review-cancelled", "review was cancelled") from None


def _doctor_static_output(parsed: ParsedArgs, resolution: C.ConfigResolution,
                          domain: C.DomainPaths) -> None:
    workspace = doctor.inspect_workspace(domain, resolution,
                                         _doctor_limits(parsed), parsed.scope)
    if parsed.json:
        sys.stdout.buffer.write(_doctor_offline_assessment_json(
            resolution, domain, workspace))
    else:
        sys.stdout.write(render.render_doctor(
            workspace.aggregate, workspace=workspace))
        mention = _fix_mention(resolution)
        if mention is not None:
            sys.stdout.write(mention + "\n")


def _fix_mention(resolution: C.ConfigResolution) -> str | None:
    """Name ``ptest doctor --fix`` when the plan would change something.

    Best effort only: planning is read-only, but a refusal here must
    never break doctor output, so every planning failure means silence.
    """
    try:
        plan = doctor_fix.plan_all(resolution.root, resolution)
    except Exception:
        return None
    if plan.change_count:
        return (f"config is out of date: {plan.change_count} changes — "
                "run ptest doctor --fix to review them")
    return None


def _run_doctor_fix(parsed: ParsedArgs, resolution: C.ConfigResolution) -> int:
    """Show the config diff and apply it. Never reviews, never asks.

    The ``--fix`` flag itself is the consent; ``--dry-run`` previews only.
    """
    plan = doctor_fix.plan_all(resolution.root, resolution)
    if plan.refusals:
        refusal = plan.refusals[0]
        raise _problem(refusal.code, f"{refusal.rel}: {refusal.message}")
    if not plan.files:
        print("config is up to date")
        return 0
    sys.stdout.write(doctor_fix.render_diff(plan))
    if parsed.dry_run:
        return 0
    updated = doctor_fix.apply_plan(resolution.root, plan)
    for rel in updated:
        print(f"updated {rel}")
    if doctor_fix.selection_enabled_by(plan):
        print("run a parallel full baseline once to record a baseline")
    return 0


_OFFLINE_UNKNOWN_REASON = "offline static run: model review unavailable"


def _doctor_offline_assessment_json(
        resolution: C.ConfigResolution,
        domain: C.DomainPaths, workspace) -> bytes:
    """Build the versioned assessment document from static facts only.

    No provider is launched and no report is written: deterministic items
    are answered from ptest's own facts while every item needing a model
    call becomes an ``unknown`` row carrying the offline reason.
    """
    packets = agent_assessment.build_packets(workspace, resolution)
    if not packets:
        raise _problem("invalid-config", "no selected project evidence is available")
    review_items = _resolution_items(resolution)
    executions = _execution_facts(resolution, _items=review_items)
    project_facts = _project_facts(resolution, _items=review_items)
    initialization_blocker = _initialization_required_limitation(resolution)
    child_data = []
    for packet in packets:
        reviews = _plan_item_reviews(
            packet, domain, resolution,
            facts=project_facts.get(packet.declaration))
        replies = tuple(
            None if review.answer is not None or review.request is None
            else _OFFLINE_UNKNOWN_REASON
            for review in reviews)
        assessment = _assemble_with_parallel(packet, reviews, replies)
        child_limitations = _assessment_limitations((packet,))
        if initialization_blocker is not None and packet.declaration == ".":
            child_limitations.insert(0, dict(initialization_blocker))
        child_data.append(_child_assessment_data(
            packet, assessment, child_limitations,
            execution=executions.get(packet.declaration),
            facts=project_facts.get(packet.declaration)))
    limitations = _assessment_limitations(packets, top_level=True)
    if initialization_blocker is not None:
        limitations.insert(0, dict(initialization_blocker))
    document = C.PublicDocument(
        kind="agent-assessment", ptest_version=C.PTEST_VERSION,
        domain=_domain_public(domain),
        data={
            "schema": C.AGENT_ASSESSMENT_SCHEMA,
            "provider": {"name": "offline",
                         "cli_version": C.PTEST_VERSION,
                         "profile": "ptest-offline-v1"},
            "children": child_data,
            "limitations": limitations,
            "publication": {"status": "skipped",
                            "path": "recommendations.md",
                            "sha256": "0" * 64},
        },
        error=None)
    return render.render_json(document)


def _uninstall_consented() -> bool:
    """Ask once on a TTY; any non-yes answer (including EOF) declines."""
    if not sys.stdin.isatty():
        return False
    print("Remove these? [y/N]", file=sys.stderr)
    try:
        answer = input().strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _run_uninstall(parsed: ParsedArgs, cwd: Path) -> int:
    domain: C.DomainPaths | None = None
    try:
        root = config_api.repository_root(cwd)
        domain = platform.domain_paths(parsed.fixture_domain)
        plan = uninstall_api.plan_repo(root, domain)
        self_plan = (uninstall_api.plan_self() if parsed.uninstall_self
                     else uninstall_api.SelfPlan(requested=False))
        if self_plan.refused is not None:
            raise _problem(
                "not-install-layout",
                f"refusing --self: {self_plan.refused_path} "
                f"{self_plan.refused}")
        removals = [entry for entry in plan.entries
                    if entry.action == uninstall_api.REMOVE]
        self_work = self_plan.requested and self_plan.root is not None
        if parsed.dry_run:
            if parsed.json:
                sys.stdout.buffer.write(_document(
                    "uninstall",
                    uninstall_api.document_data(
                        plan, applied=None, dry_run=True,
                        self_plan=self_plan, domain=domain),
                    domain=domain))
            else:
                sys.stdout.write(uninstall_api.render_text(
                    plan, dry_run=True, self_plan=self_plan,
                    domain=domain))
            return 0
        if not parsed.json and not parsed.uninstall_yes:
            # `--yes` prints only the result below; anything else shows
            # the plan once here (dry-run/preview/consent paths).
            sys.stdout.write(uninstall_api.render_text(
                plan, self_plan=self_plan, domain=domain))
        if parsed.json and not parsed.uninstall_yes and (removals or self_work):
            # `--json` never prompts: exactly one success document
            # carrying the plan with applied=false, exit non-zero.
            sys.stdout.buffer.write(_document(
                "uninstall",
                uninstall_api.document_data(
                    plan, applied=None, dry_run=False,
                    self_plan=self_plan, domain=domain),
                domain=domain))
            print("refusing to remove without --yes; re-run with --yes "
                  "to remove these entries", file=sys.stderr)
            return 2
        if removals or self_work:
            if not parsed.uninstall_yes and not _uninstall_consented():
                if sys.stdin.isatty():
                    raise _problem("confirmation-declined",
                                   "uninstall declined; nothing changed")
                raise _problem(
                    "confirmation-required",
                    "refusing to remove without --yes; re-run with --yes "
                    "to remove these entries")
        applied = uninstall_api.apply_repo(plan, domain)
        self_result = uninstall_api.SelfApplied()
        if self_work:
            assert self_plan.root is not None
            self_result = uninstall_api.apply_self(self_plan)
        self_removed = self_result.removed
        self_link_removed = self_result.link_removed
        if parsed.json:
            sys.stdout.buffer.write(_document(
                "uninstall",
                uninstall_api.document_data(
                    plan, applied=applied, dry_run=False,
                    self_plan=self_plan, self_removed=self_removed,
                    self_link_removed=self_link_removed,
                    self_kept=self_result.kept, domain=domain),
                domain=domain))
        else:
            if parsed.uninstall_yes:
                sys.stdout.write(uninstall_api.render_text(
                    plan, applied=applied, self_plan=self_plan,
                    self_kept=self_result.kept, domain=domain))
            else:
                # The plan was already printed before consent; follow it
                # with only the summary line plus any --self kept files.
                sys.stdout.write(uninstall_api.render_text(
                    plan, applied=applied, self_plan=self_plan,
                    self_kept=self_result.kept, domain=domain,
                    summary_only=True))
            if self_removed:
                print("ptest was uninstalled")
        if self_removed and parsed.json:
            print("ptest was uninstalled", file=sys.stderr)
        # Skipped entries are informational: they are reported in the
        # plan/result above and never turn a successful run non-zero.
        return 0
    except C.Problem as problem:
        return _emit_error(problem, kind="uninstall",
                           json_output=parsed.json, domain=domain)


def _static_dispatch(parsed: ParsedArgs, cwd: Path) -> int:
    command = parsed.command
    if command == "help":
        # Static read-only route: no config inspection, no writes, no prompt,
        # no coordinator, no setup, no tests. Fail closed on unknown topics.
        text = (help_api.overview() if parsed.help_topic is None
                else help_api.topic(parsed.help_topic))
        if text is None:
            return _emit_error(
                _problem("invalid-config",
                         f"unknown help topic; {help_api.HINT}"),
                kind="help", json_output=False)
        print(text)
        return 0
    if command == "version":
        print(C.PTEST_VERSION)
        return 0
    if command == "guide":
        text = render.render_guide()
        if parsed.write is not None:
            root = config_api.resolve_config(cwd).root
            files.create_exclusive(root, parsed.write, text.encode("utf-8"), private=False)
        sys.stdout.write(text)
        return 0
    if command == "rules":
        try:
            result = (agent_rules.apply(cwd) if parsed.apply_rules
                      else agent_rules.preview(cwd))
            label = "applied" if parsed.apply_rules else "preview"
            print(f"{label}: " + (", ".join(result.actions) or "already configured"))
            return 0
        except C.Problem as problem:
            return _emit_error(problem, kind="rules", json_output=False)
    if command == "uninstall":
        return _run_uninstall(parsed, cwd)
    if command == "init":
        try:
            agents = _init_agents(parsed, json_output=parsed.json)
            root = config_api.repository_root(cwd)
            plan = agent_rules.preview(root, agents=agents) if agents else None
            result = config_api.init_project(cwd, C.InitOptions(
                runner=parsed.runner, dry_run=parsed.dry_run,
                reveal_command=parsed.reveal_command,
                children=parsed.children,
                agents=agents,
            ))
            applied = None
            if agents and not parsed.dry_run:
                applied = agent_rules.apply(root, agents=agents)
            payload = C.serialize_init_result(result)
            rules = applied if applied is not None else plan
            if parsed.json:
                # Machine output stays byte-compatible: no facts, no footer.
                sys.stdout.buffer.write(_document("init", payload))
            else:
                facts = () if parsed.dry_run else _init_facts(cwd)
                sys.stdout.write(_render_init_text(
                    result, rules, parsed=parsed, repo_name=root.name,
                    facts=facts))
            if parsed.reveal_command:
                print("unredacted-command-disclosure: explicit preview requested",
                      file=sys.stderr)
            smoke_results: tuple = ()
            smoke_plans: tuple = ()
            if not parsed.dry_run and not parsed.json:
                try:
                    smoke_domain = platform.domain_paths(
                        parsed.fixture_domain)
                    smoke_plans = init_smoke.plan_resolution(
                        config_api.resolve_config(cwd), smoke_domain)
                except Exception:
                    smoke_plans = ()
                smoke_results = _init_smoke_results(
                    parsed, cwd, smoke_plans, smoke_domain
                    if smoke_plans else None)
                footer = _render_init_footer_text(
                    result, rules, parsed=parsed, smoke=smoke_results,
                    plans=smoke_plans, facts=facts)
                if footer:
                    sys.stdout.write("\n" + footer)
            _run_changed_setup(parsed, cwd, result, smoke_plans)
            offer_review = (
                parsed.doctor_request is True
                or (parsed.doctor_request is None and not parsed.json
                    and not parsed.dry_run and _interactive_review())
            )
            if offer_review:
                explicit_request = parsed.doctor_request is True
                try:
                    resolution = config_api.resolve_config(cwd)
                    domain = platform.domain_paths(parsed.fixture_domain)
                    declined = _run_review_entry(
                        parsed, resolution, domain,
                        interactive=_interactive_review(),
                        preconsented=(explicit_request
                                      and parsed.allow_model_review),
                    )
                    if declined:
                        return 0
                except C.Problem as problem:
                    if not explicit_request:
                        print(
                            "review unavailable; initialization succeeded "
                            f"without review ({render.terminal_text(problem.code)})",
                            file=sys.stderr,
                        )
                        return 0
                    print("initialization succeeded; review incomplete", file=sys.stderr)
                    return _emit_error(
                        problem, kind="init", json_output=False)
            return 0
        except C.Problem as problem:
            return _emit_error(problem, kind="init", json_output=parsed.json)
    if command == "register":
        try:
            resolution = config_api.resolve_config(cwd)
            if resolution.config is not None:
                summary = _summary(resolution.config)
                preview = C.RegisterPreview(
                    root=str(resolution.root), initialized=True,
                    proposed_runner=resolution.config.runner.kind,
                    commands=summary.commands,
                    required_actions=(), warnings=resolution.warnings,
                )
            else:
                # Dry-run initialization performs only bounded native manifest
                # inspection and never writes.
                result = config_api.init_project(cwd, C.InitOptions(
                    runner=None, dry_run=True, reveal_command=False,
                ))
                preview = C.RegisterPreview(
                    root=str(result.target.parent), initialized=False,
                    proposed_runner=None if result.config is None else result.config.runner_kind,
                    commands=() if result.config is None else result.config.commands,
                    required_actions=("initialize",), warnings=result.warnings,
                )
            payload = {
                "root": preview.root, "initialized": preview.initialized,
                "proposed_runner": None if preview.proposed_runner is None else preview.proposed_runner.value,
                "commands": [{"kind": item.kind.value, "mode": item.mode.value,
                              "argument_count": item.argument_count,
                              "generated_options": list(item.generated_options),
                              "workers": item.workers, "provenance": list(item.provenance)}
                              for item in preview.commands],
                "required_actions": list(preview.required_actions),
                "warnings": [_reason(item) for item in preview.warnings],
            }
            if parsed.json:
                sys.stdout.buffer.write(_document("register", payload))
            else:
                print(f"register: {'initialized' if preview.initialized else 'preview'}")
            return 0
        except C.Problem as problem:
            return _emit_error(problem, kind="register", json_output=parsed.json)
    try:
        resolution = config_api.resolve_config(cwd)
        if command == "where":
            domain = platform.domain_paths(parsed.fixture_domain)
            payload = _where_payload(resolution, domain)
            if parsed.json:
                sys.stdout.buffer.write(_document("where", payload, domain=domain))
            else:
                print(f"root: {render.terminal_text(payload['root'])}")
                print(f"initialized: {payload['initialized']}")
                if payload["capability"] is not None:
                    print(f"capability: {payload['capability']['execution']}")
                    for limitation in payload["capability"]["limitations"]:
                        print(f"limitation: {render.terminal_text(limitation['message'])}")
                print(_domain_text(domain))
            if parsed.reveal_command:
                if resolution.config is None:
                    print("unredacted-command-disclosure: no command is configured",
                          file=sys.stderr)
                else:
                    command = (resolution.config.runner.launcher
                               + resolution.config.runner.args
                               + resolution.config.runner.full_args)
                    print(
                        "unredacted-command-disclosure: "
                        + json.dumps(list(command), ensure_ascii=True),
                        file=sys.stderr,
                    )
            return 0
        if command == "plan":
            if resolution.config is None:
                if resolution.problem is not None:
                    raise resolution.problem
            assert resolution.config is not None
            plan = C.Plan(mode=C.Mode.FULL, execution="full",
                          reasons=(C.Reason(code="selection-disabled",
                                            message="static plan requires a verified execution identity",
                                            paths=()),), static_preview=True)
            payload = {"mode": plan.mode.value, "execution": plan.execution,
                       "files": [], "reasons": [_reason(item) for item in plan.reasons],
                       "input_digest": None, "compatibility": None,
                       "baseline_run_id": None, "static_preview": True}
            if parsed.json:
                sys.stdout.buffer.write(_document("plan", payload))
            else:
                print("plan: full (static preview)")
            return 0
        if command == "status":
            domain = platform.domain_paths(parsed.fixture_domain)
            limits = scheduler.effective_limits(domain)
            leases = scheduler.reconcile(domain)
            payload = {
                "effective_limits": {"max_slots": limits.max_slots,
                "max_jobs": limits.max_jobs, "memory_mb": limits.memory_mb,
                "repo_workers": limits.repo_workers},
                "queued": [_lease(item) for item in leases if item.state is C.LeaseState.QUEUED],
                "active": [_lease(item) for item in leases if item.state not in {
                    C.LeaseState.QUEUED, C.LeaseState.RELEASED, C.LeaseState.CANCELLED}],
                **_domain_facts(domain),
            }
            if parsed.json:
                sys.stdout.buffer.write(_document("status", payload, domain=domain))
            else:
                print(f"queued: {len(payload['queued'])}\nactive: {len(payload['active'])}")
                print(_domain_text(domain))
            return 0
        if command == "history":
            if parsed.scope is not None:
                # Public history exposes summaries, not the complete per-test
                # inventory needed for exact test-ID or file matching.
                raise _problem("unsupported-capability", "history test/file filters are not supported in this slice")
            domain = platform.domain_paths(parsed.fixture_domain)
            if resolution.config is None:
                raise resolution.problem or _problem("initialization-required", "project configuration is required")
            payload = history.read_history_payload(domain, _checkout(resolution.config), parsed.history_limit)
            if parsed.json:
                sys.stdout.buffer.write(_document("history", payload, domain=domain))
            else:
                print(f"history: {len(payload['summaries'])} runs")
            return 0
        if command == "doctor":
            domain = platform.domain_paths(parsed.fixture_domain)
            if parsed.fix:
                return _run_doctor_fix(parsed, resolution)
            if parsed.probe is not None:
                if resolution.config is None:
                    raise resolution.problem or _problem(
                        "initialization-required",
                        "project configuration is required",
                    )
                result = operations.execute(
                    domain, resolution.config,
                    C.RunRequest(
                        mode=C.Mode.PROBE,
                        no_setup=parsed.no_setup,
                        result_path=parsed.result_path,
                        fixture_domain=parsed.fixture_domain,
                        probe=parsed.probe,
                    ),
                )
                for reason in result.reasons:
                    print(render.terminal_text(f"{reason.code}: {reason.message}"),
                          file=sys.stderr)
                return result.exit_code
            if parsed.offline:
                _doctor_static_output(parsed, resolution, domain)
                return 0
            declined = _run_review_entry(
                parsed, resolution, domain,
                interactive=_interactive_review(),
            )
            if declined:
                return 0
            return 0
    except C.Problem as problem:
        return _emit_error(
            problem, kind=command or "where",
            json_output=parsed.json,
        )
    raise _problem("invalid-config", "unknown command")


def _init_agents(parsed: ParsedArgs, *, json_output: bool = False) -> tuple[str, ...]:
    if parsed.agents_explicit or json_output or not sys.stdin.isatty():
        return parsed.agents
    print("Install repository-local ptest guidance for which agents? "
          "[all/claude,codex,opencode,gemini/none] (default: all):",
          file=sys.stderr)
    try:
        choice = input().strip().lower()
    except EOFError:
        return ()
    if choice == "none":
        return ()
    if not choice or choice == "all":
        return agent_rules.SUPPORTED_AGENTS
    names = tuple(part.strip() for part in choice.split(","))
    if (any(not name for name in names)
            or any(name not in agent_rules.SUPPORTED_AGENTS for name in names)):
        raise _problem("unsupported-capability", "agent integration is not supported")
    return tuple(dict.fromkeys(names))


# Worst-of rank for a monorepo total: a cancelled or incomplete child
# outranks a failed one, matching what each child's own end line says.
_CHILD_STATUS_RANK = {
    C.Status.PASSED: 0,
    C.Status.NO_TESTS_NEEDED: 0,
    C.Status.FAILED: 1,
    C.Status.NOT_RUN: 2,
    C.Status.INCOMPLETE: 3,
    C.Status.CANCELLED: 4,
}


def _worst_status(statuses: list[C.Status]) -> C.Status:
    """Worst child outcome for the total line; empty means all passed."""
    worst = C.Status.PASSED
    for status in statuses:
        if _CHILD_STATUS_RANK[status] > _CHILD_STATUS_RANK[worst]:
            worst = status
    return worst


def _summed_counts(items: list[C.Counts | None]) -> C.Counts | None:
    """Sum per-child bridge counts for a monorepo total, or None if any are missing."""
    if not items or any(item is None for item in items):
        return None
    fields = ("collected", "executed", "passed", "failed", "skipped", "unknown")
    summed = {}
    for field in fields:
        values = [getattr(item, field) for item in items]
        if all(value is not None for value in values):
            summed[field] = sum(values)
    return C.Counts(**summed)


def _emit_monorepo_total(child_outcomes: list[tuple[int, C.Status, C.Counts | None]],
                          started: float, code: int, *, quiet: bool) -> None:
    """Emit the existing total line over per-child outcomes."""
    total_counts = _summed_counts(
        [counts for _, _, counts in child_outcomes])
    status = _worst_status(
        [outcome for _, outcome, _ in child_outcomes])
    hint = (status in (C.Status.FAILED, C.Status.INCOMPLETE,
                       C.Status.NOT_RUN)
            and progress.claim_hint())
    progress.emit(progress.format_end(
        status, counts=total_counts,
        duration_s=time.monotonic() - started, exit_code=code,
        hint=hint, lead="total", color=sys.stderr.isatty()), quiet=quiet)


def _lease(item: C.LeaseView) -> dict:
    return {
        "run_id": item.run_id, "checkout_id": item.checkout_id,
        "state": item.state.value, "sequence": item.sequence,
        "requested_slots": item.requested_slots, "slots": item.slots,
        "memory_estimate_mb": item.memory_estimate_mb,
        "reserved_memory_mb": item.reserved_memory_mb, "phase": item.phase,
        "age_s": item.age_s, "queue_wait_s": item.queue_wait_s,
        "ownership": item.ownership, "fixture": item.fixture,
        "reasons": [_reason(reason) for reason in item.reasons],
    }


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = tuple(sys.argv[1:] if argv is None else argv)
    prefix = _walk_cli_prefix(raw_args)
    json_requested = _inspection_json_requested(raw_args, prefix)
    try:
        parsed = _parse_args(raw_args, prefix)
        if parsed.command in _INSPECTION or parsed.command in {"help", "version"}:
            return _static_dispatch(parsed, Path.cwd())
        resolution = config_api.resolve_config(Path.cwd())
        progress.reset()
        if resolution.monorepo is not None:
            from . import monorepo
            domain = platform.domain_paths(parsed.fixture_domain)
            platform.validate_state_outside_checkout(
                domain, _state_anchor(resolution.root))
            children = monorepo.preflight_children(resolution.root, resolution.monorepo)
            if parsed.full:
                child_outcomes: list[tuple[int, C.Status, C.Counts | None]] = []

                def run_full(child):
                    result = operations.execute(
                        domain, child.config,
                        C.RunRequest(mode=C.Mode.FULL, workers=parsed.workers,
                                     queue_timeout_s=parsed.queue_timeout_s,
                                     no_setup=parsed.no_setup,
                                     result_path=parsed.result_path,
                                     fixture_domain=parsed.fixture_domain,
                                     verbose=parsed.verbose, quiet=parsed.quiet),
                    )
                    for reason in result.reasons:
                        print(render.terminal_text(f"{reason.code}: {reason.message}"), file=sys.stderr)
                    child_outcomes.append(
                        (result.exit_code, result.status, result.counts))
                    return result.exit_code
                started = time.monotonic()
                code = monorepo.execute_full(children, run_full)
                _emit_monorepo_total(child_outcomes, started, code,
                                     quiet=parsed.quiet)
                return code
            if parsed.changed:
                changed_started = time.monotonic()
                changed_outcomes: list[tuple[int, C.Status, C.Counts | None]] = []
                first_failure = 0
                heads = (monorepo.child_baseline_heads(domain, children)
                         if parsed.base is None else None)
                for item in monorepo.select_changed_children(
                        resolution.root, children, parsed.base, heads):
                    if not item.run:
                        if not parsed.quiet:
                            print(progress.format_no_changes(
                                render.terminal_text(item.target.declaration),
                                color=sys.stderr.isatty()), file=sys.stderr)
                        changed_outcomes.append(
                            (0, C.Status.NO_TESTS_NEEDED, None))
                        continue
                    result = operations.execute(
                        domain, item.target.config,
                        monorepo.child_changed_request(
                            item.target, base=parsed.base,
                            workers=parsed.workers,
                            queue_timeout_s=parsed.queue_timeout_s,
                            no_setup=parsed.no_setup,
                            result_path=parsed.result_path,
                            fixture_domain=parsed.fixture_domain,
                            verbose=parsed.verbose, quiet=parsed.quiet),
                    )
                    for reason in result.reasons:
                        print(render.terminal_text(f"{reason.code}: {reason.message}"), file=sys.stderr)
                    changed_outcomes.append(
                        (result.exit_code, result.status, result.counts))
                    if result.exit_code and not first_failure:
                        first_failure = result.exit_code
                _emit_monorepo_total(changed_outcomes, changed_started,
                                     first_failure, quiet=parsed.quiet)
                return first_failure
            routed = monorepo.route_scopes(parsed.runner_argv, children)
            result = operations.execute(
                domain, routed.target.config,
                C.RunRequest(mode=C.Mode.SCOPED, argv=routed.scopes,
                             workers=parsed.workers, queue_timeout_s=parsed.queue_timeout_s,
                             no_setup=parsed.no_setup,
                             shadow=parsed.shadow, result_path=parsed.result_path,
                             fixture_domain=parsed.fixture_domain,
                             verbose=parsed.verbose, quiet=parsed.quiet,
                             display_argv=parsed.runner_argv),
            )
            for reason in result.reasons:
                print(render.terminal_text(f"{reason.code}: {reason.message}"), file=sys.stderr)
            return result.exit_code
        if resolution.config is None:
            # An explicit runner suffix has already crossed ptest's closed
            # prefix grammar.  Without a configured adapter there is no
            # capability authority to execute it; classify that request as
            # unavailable without inspecting or echoing its literal tokens.
            # Bare/automatic execution keeps the initialization guidance.
            if (parsed.runner_argv and resolution.problem is not None
                    and resolution.problem.code == "initialization-required"):
                raise _problem(
                    "unsupported-capability",
                    "literal execution requires a configured runner profile",
                )
            raise resolution.problem or _problem(
                "initialization-required", "project configuration is required",
            )
        domain = platform.domain_paths(parsed.fixture_domain)
        request = C.RunRequest(
            mode=parsed.mode,
            argv=parsed.runner_argv,
            base=parsed.base,
            workers=parsed.workers,
            queue_timeout_s=parsed.queue_timeout_s,
            no_setup=parsed.no_setup,
            shadow=parsed.shadow,
            result_path=parsed.result_path,
            fixture_domain=parsed.fixture_domain,
            probe=parsed.probe,
            verbose=parsed.verbose,
            quiet=parsed.quiet,
        )
        result = operations.execute(domain, resolution.config, request)
        for reason in result.reasons:
            print(render.terminal_text(f"{reason.code}: {reason.message}"), file=sys.stderr)
        return result.exit_code
    except _UnknownCommand as problem:
        print(render.terminal_text(f"ptest: {problem.message}"), file=sys.stderr)
        print(file=sys.stderr)
        print(help_api.overview(), file=sys.stderr)
        return 2
    except C.Problem as problem:
        kind = prefix.command or "run"
        return _emit_error(problem, kind=kind, json_output=json_requested)


def _inspection_json_requested(args: Sequence[str], prefix: _CliPrefix) -> bool:
    """Choose machine errors independent of closed inspection option order.

    Guide is text-only. Execution tails never give --json ptest semantics.
    """
    return (prefix.command is not None and prefix.command != "guide"
            and "--json" in args[prefix.remainder_index + 1:])


__all__ = ["ParsedArgs", "parse_argv", "main"]
