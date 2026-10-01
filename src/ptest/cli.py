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
import signal
import stat
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from . import agent_assessment, agent_providers, agent_rules, config as config_api
from . import contracts as C
from . import doctor, doctor_fix, executability, files, help as help_api, history
from . import init_render, init_smoke, lastgreen
from . import operations, platform, progress, recommendations, scheduler
from . import uninstall as uninstall_api
from . import update as update_api
from . import worktree as worktree_api
from . import render
from .adapters import pytest as pytest_adapter
from .adapters import vitest as vitest_adapter
from .runners import adapter_for


_INSPECTION = frozenset({
    "init", "register", "where", "status", "history", "plan",
    "doctor", "guide", "rules", "uninstall", "update",
})
_UPDATE_CHECK_EXEMPT = frozenset({"help", "version", "update", "uninstall"})
_EXECUTION_VALUE = frozenset({
    "--base", "--workers", "--queue-timeout", "--timeout", "--result-json",
})
_EXECUTION_BOOL = frozenset({"--changed", "--full", "--again", "--no-setup", "--shadow",
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
    timeout_s: float | None = None
    no_setup: bool = False
    shadow: bool = False
    verbose: bool = False
    quiet: bool = False
    result_path: str | None = None
    changed: bool = False
    full: bool = False
    again: bool = False
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
    update_check: bool = False
    update_version: str | None = None
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
    from_main: bool = False


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
    word = (args[index] if index < len(args)
              and isinstance(args[index], str) else None)
    # `upgrade` is the only alias that routes: the init aliases stay
    # suggestions so `ptest install` keeps its closed-grammar refusal.
    if word == "upgrade":
        word = "update"
    command = word if word in _INSPECTION else None
    return _CliPrefix(fixture, index, command, problem)


def _parse_execution(args: Sequence[str], *, command: str | None = None) -> ParsedArgs:
    mode = C.Mode.AUTOMATIC
    changed = command == "changed"
    full = False
    again = False
    workers = None
    base = None
    queue_timeout = C.DEFAULT_QUEUE_TIMEOUT_S
    timeout = None
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
            elif token == "--again":
                if again:
                    raise _problem("invalid-config", "option cannot be repeated")
                again = True
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
        elif token == "--timeout":
            if timeout is not None:
                raise _problem("invalid-config", "option cannot be repeated")
            timeout = _number(value, lo=1, hi=C.MAX_COMPOUND_TIMEOUT_S)
        else:
            if result_path is not None:
                raise _problem("invalid-config", "option cannot be repeated")
            result_path = value
    if again and not full:
        raise _problem("invalid-config", "--again requires --full")
    if full:
        if base is not None:
            raise _problem("invalid-config", "--base is unavailable with --full")
        mode = C.Mode.FULL
        # A runner tail with --full is accepted here and routed at
        # dispatch: at a monorepo root a whole-child scope runs that one
        # child's full gate, anything else (and any standalone tail) is
        # rejected in plain words there.
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
        timeout_s=timeout,
        no_setup=no_setup, shadow=shadow, result_path=result_path,
        changed=changed, full=full, again=again, verbose=verbose, quiet=quiet,
    )


def _parse_inspection(command: str, args: Sequence[str]) -> ParsedArgs:
    if command == "init":
        runner = None
        dry_run = reveal = from_main = False
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
        index = 0
        while index < len(args):
            token = args[index]
            if token == "--dry-run":
                dry_run = True
            elif token == "--reveal-command":
                reveal = True
            elif token == "--from-main":
                from_main = True
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
        if from_main and (runner is not None or children):
            raise _problem("invalid-config", "--from-main copies the main checkout's config; it cannot be combined with --runner or --child")
        return ParsedArgs(command=command, runner=runner, dry_run=dry_run,
                          reveal_command=reveal, json="--json" in args,
                          from_main=from_main,
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
                          smoke=smoke)
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
    if command == "update":
        update_check = update_json = False
        update_version = None
        seen: set[str] = set()
        index = 0
        while index < len(args):
            token = args[index]
            if token == "--check":
                if "check" in seen:
                    raise _problem("invalid-config",
                                   "option cannot be repeated")
                seen.add("check")
                update_check = True
                index += 1
            elif token == "--json":
                if "json" in seen:
                    raise _problem("invalid-config",
                                   "option cannot be repeated")
                seen.add("json")
                update_json = True
                index += 1
            elif token == "--version":
                if "version" in seen:
                    raise _problem("invalid-config",
                                   "option cannot be repeated")
                try:
                    value, index = _value(args, index, token)
                except C.Problem:
                    raise _problem(
                        "invalid-config",
                        "--version must be a release number like 0.3.7",
                    ) from None
                seen.add("version")
                if update_api.valid_version(value) is None:
                    raise _problem(
                        "invalid-config",
                        "--version must be a release number like 0.3.7")
                update_version = value
            else:
                raise _problem("invalid-config", "unknown inspection option")
        if update_check and update_version is not None:
            raise _problem("invalid-config",
                           "--check and --version cannot be combined")
        return ParsedArgs(command=command, json=update_json,
                          update_check=update_check,
                          update_version=update_version)
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
        verbose = False
        verbose_seen = False
        quiet = False
        quiet_seen = False
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
            elif token in ("-v", "--verbose"):
                if verbose_seen:
                    raise _problem("invalid-config", "option cannot be repeated")
                verbose_seen = True
                verbose = True
            elif token in ("-q", "--quiet"):
                if quiet_seen:
                    raise _problem("invalid-config", "option cannot be repeated")
                quiet_seen = True
                quiet = True
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
            if json_output or verbose_seen or quiet_seen:
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
            if json_output or scope is not None or limits or quiet_seen:
                raise _problem("invalid-config", "--fix takes no output, scope, or scan-limit options")
            if (reviewer_seen or allow_seen or timeout_seen or model_seen
                    or concurrency_seen):
                raise _problem("invalid-config", "--fix never runs a model review")
            return ParsedArgs(command=command, fix=fix,
                              dry_run=fix_dry, offline=offline,
                              verbose=verbose)
        if quiet_seen and not offline:
            raise _problem("invalid-config", "--quiet requires --offline")
        if fix_dry or verbose_seen:
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
                          offline=offline, quiet=quiet, **limits)
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
_COMMAND_ALIASES = {"install": "init", "setup": "init", "configure": "init",
                    "upgrade": "update"}


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
            word = remaining[0]
            if parsed.command == "update" and not rest and os.path.lexists(word):
                # Commands win over paths, but a bare `ptest update` beside
                # an `update/` test folder would install software where the
                # user most likely meant to run tests: make them choose.
                raise _problem(
                    "invalid-config",
                    f"{word} is both a ptest command and a path here: run "
                    f"`ptest ./{word}` for its tests, or `ptest update` from "
                    f"another directory to update ptest")
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
        if (resolution.problem is not None
                and resolution.problem.code == "config-uncommitted"):
            warnings = [{"code": "config-uncommitted",
                         "message": resolution.problem.message, "paths": []}]
        else:
            warnings = []
        return {
            "root": str(resolution.root), "config_path": None,
            "initialized": False, "runner_kind": None, "capability": None,
            "commands": [], "effective_limits": {"max_slots": None,
            "max_jobs": None, "memory_mb": None, "repo_workers": None},
            "provenance": list(resolution.provenance),
            "warnings": warnings,
            **_domain_facts(domain),
        }
    adapter_for(config.runner.kind)  # closed registry validation only
    if config.runner.kind is C.RunnerKind.PYTEST:
        inspected = pytest_adapter.inspect_capability(config)
    elif config.runner.kind is C.RunnerKind.VITEST:
        capped = vitest_adapter.bound(config) is not None
        inspected = C.Capability(
            execution=(C.ExecutionTier.BOUNDED_NATIVE if capped
                       else C.ExecutionTier.EXCLUSIVE_COMMAND),
            selection=False,
            lifecycle="cooperative-process-group",
            limitations=(C.Reason(
                code="unsupported-capability",
                message=(vitest_adapter.VITEST_CAPPED_NOTE if capped
                         else vitest_adapter.VITEST_EXCLUSIVE_NOTE),
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
    if problem.code in {"coordinator-unavailable", "queue-timeout",
                        "update-unavailable"}:
        return 75
    return 2


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
        else:
            # Consent to the smoke covers its owed setup: run it without
            # a second question (the setup line names the command).
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


def _existing_changed_targets(root: Path) -> tuple:
    """``((declaration, config), ...)`` resolved read-only from disk.

    Mirrors the real-run existing-config targets so a dry-run preview
    describes exactly what the real run will do; nothing is written.
    """
    from . import monorepo
    resolution = config_api.resolve_config(root)
    manifest = getattr(resolution, "monorepo", None)
    if manifest is None:
        if resolution.config is None:
            return ()
        return ((".", resolution.config),)
    targets = []
    for child in tuple(getattr(manifest, "children", ()) or ()):
        diagnosis = monorepo.diagnose_child(root, child)
        config = diagnosis.config if diagnosis.kind == "ok" else None
        if config is not None:
            targets.append((child, config))
    return tuple(targets)


SELECTION_OFF_LINE = (
    "{project}: selection is off; run `ptest doctor --fix` so bare ptest "
    "runs only the tests your change reaches"
)


def _report_selection_off(parsed: ParsedArgs, result, smoke_plans: tuple = ()) -> None:
    """One line per existing pytest config whose change selection is off.

    Fresh pytest configs already enable selection, so they stay silent;
    existing configs are never rewritten here (``ptest doctor --fix`` is).
    """
    if parsed.json or result.action is not C.InitAction.EXISTING:
        return
    root = result.target.parent
    targets = (_existing_changed_targets(root) if parsed.dry_run
               else _plan_targets(smoke_plans))
    for declaration, config in targets:
        selection = getattr(config, "selection", None)
        if (config.runner.kind is C.RunnerKind.PYTEST
                and not (selection is not None and selection.enabled is True)):
            line = SELECTION_OFF_LINE.format(project=render.terminal_text(declaration))
            print(line[len(".: "):] if declaration == "." else line)


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


def _declared_review_model(provider: str, override: str | None,
                           environ: Mapping[str, str]) -> str | None:
    """Resolve flag, environment override, then the product model policy."""
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
    if provider == "codex":
        return "gpt-6-sol"
    if provider == "claude":
        return "opus"
    return None


def _resolve_review_model(adapter, cache_root: Path | None,
                          declared: str | None, *,
                          require_listed_default: bool = False
                          ) -> tuple[str | None, str | None]:
    """Resolve the requested model after consent, with no picker/cache path."""
    if not isinstance(adapter, agent_providers.ReviewerAdapter):
        raise TypeError("adapter must be ReviewerAdapter")
    if declared is not None and not isinstance(declared, str):
        raise TypeError("declared must be str or None")
    version = agent_providers.cli_version(adapter)
    if declared:
        if require_listed_default and adapter.name == "codex":
            try:
                listed = agent_providers.discover_models(adapter)
            except OSError:
                listed = ()
            if declared not in listed:
                raise _problem(
                    "provider-unqualified",
                    f"default Codex model {declared} is not listed by this CLI; "
                    "choose an explicit --review-model override",
                )
        return (declared, version)
    return (None, version)


def _render_review_disclosure(adapter, resolution: C.ConfigResolution,
                              *, ask: bool = True, calls: int | None = None,
                              followups: int | None = None,
                              concurrency: int = 4,
                              model: str | None = None) -> bool:
    """Short pre-consent disclosure: at most three lines, then the prompt.

    Line 1 names the provider, project, requested model, and call ceiling;
    lines 2-3 name source exclusions, the cost note, and the pointer to the
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
        tail = (f"requested model {chosen}."
                if chosen else
                "requested model is unavailable.")
        planned_followups = calls if followups is None else followups
        if isinstance(planned_followups, bool) or not isinstance(
                planned_followups, int) or planned_followups < 0:
            raise TypeError("followups must be a nonnegative int or None")
        first = (f"Model review disclosure: {provider} gets bounded source "
                 f"excerpts from {project}: {calls} initial + up to "
                 f"{planned_followups} verification calls "
                 f"({calls + planned_followups} maximum), "
                 f"{concurrency} at a time, {tail}")
    disclosure = "\n".join((
        first,
        "Excluded private files, instructions, dependencies, caches and "
        "build output are omitted; source may contain undetected secrets. "
        "Provider account costs may apply.",
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


def _literal_runner_scope(candidate: str) -> bool:
    """True only when one argv token parses as an ordinary scoped run."""
    try:
        parsed = parse_argv((candidate,))
    except (C.Problem, _UnknownCommand):
        return False
    return (parsed.command is None
            and parsed.mode is C.Mode.SCOPED
            and parsed.runner_argv == (candidate,)
            and not parsed.changed and not parsed.full)


def _recommendation_verification_scopes(workspace, resolution
                                        ) -> tuple[str | None, ...]:
    """Return only test paths accepted by the ptest command router.

    A whole child verifies with ``ptest --full`` (a bare child name
    would also route, but the report keeps the integrated gate).
    Monorepo candidates are checked by the same ``route_scopes``
    function used for execution; standalone scopes were
    already path-validated by ``doctor.inspect_workspace`` and are checked
    again against its path grammar here.
    """
    repositories = tuple(workspace.repositories)
    if resolution.monorepo is None:
        scopes = []
        for repository in repositories:
            scope = repository.local_scope
            try:
                checked = doctor._workspace_scope_value(scope)
            except C.Problem:
                scopes.append(None)
                continue
            scopes.append(
                checked if checked is not None
                and _literal_runner_scope(checked) else None)
        return tuple(scopes)

    from . import monorepo

    targets = tuple(
        monorepo.ChildTarget(
            declaration=repository.declaration,
            directory=Path(resolution.root) / repository.declaration,
            config=repository.config,
        )
        for repository in repositories
        if isinstance(repository.config, C.Config)
    )
    scopes: list[str | None] = []
    for repository in repositories:
        local_scope = repository.local_scope
        if local_scope is None or not isinstance(repository.config, C.Config):
            scopes.append(None)
            continue
        candidate = f"{repository.declaration}/{local_scope}"
        try:
            routed = monorepo.route_scopes((candidate,), targets)
        except C.Problem:
            scopes.append(None)
            continue
        if (routed.target.declaration != repository.declaration
                or routed.scopes != (local_scope,)):
            scopes.append(None)
            continue
        scopes.append(
            candidate if _literal_runner_scope(candidate) else None)
    return tuple(scopes)


def _recommendation_suite_identities(packets) -> tuple[tuple[str, str, str], ...]:
    """Return bounded runner and config-status identities in packet order.

    Config paths and argv remain private to the packet. This report-only
    identity lets the reviewer distinguish the selected scoped and full
    Vitest profiles without publishing source names or command details.
    """
    from . import review_context as RC

    runners = frozenset({"pytest", "vitest", "command"})
    statuses = frozenset({"resolved", "partial", "unavailable"})
    identities = []
    for packet in packets:
        runner = getattr(packet, "runner_kind", None)
        context = getattr(packet, "context", None)
        if runner not in runners:
            runner = "unknown"
        fallback = getattr(context, "config_status", "unavailable")
        if fallback not in statuses:
            fallback = "unavailable"
        profiles = getattr(context, "suite_profiles", ())
        by_name = {
            profile.name: profile.status
            for profile in profiles
            if isinstance(profile, RC.SuiteProfile)
        } if isinstance(profiles, (tuple, list)) else {}
        scoped = by_name.get("scoped", fallback)
        full = by_name.get("full", fallback)
        identities.append((runner,
                           scoped if scoped in statuses else "unavailable",
                           full if full in statuses else "unavailable"))
    return tuple(identities)


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
    """Name the source-ID protocol and requested model within the public cap."""
    if model is not None and not isinstance(model, str):
        raise TypeError("model must be str or None")
    requested = model or "provider-default"
    printable = "".join(char if char.isprintable() else " "
                         for char in requested)
    requested = " ".join(printable.split()) or "provider-default"
    prefix = "ptest-source-id-v3 requested-model="
    profile = prefix + requested
    if len(profile.encode("utf-8")) <= 128:
        return profile
    room = 128 - len(prefix.encode("utf-8")) - len("...".encode("utf-8"))
    clipped = requested.encode("utf-8")[:room].decode("utf-8", "ignore")
    return prefix + clipped + "..."


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
        context = getattr(packet, "context", None)
        config_status = getattr(context, "config_status", "unavailable")
        if (not agent_assessment.packet_has_evidence(packet)
                or packet.excluded_count
                or packet.truncated_count
                or config_status in ("partial", "unavailable")):
            excluded_inventory = len(getattr(
                context, "known_excluded", ()))
            evidence_limits = (
                f"{packet.excluded_count} collector exclusions; "
                f"{packet.truncated_count} files or excerpts omitted or "
                "truncated by bounds; "
                f"safe suite-exclusion inventory: {excluded_inventory} "
                f"{'entry' if excluded_inventory == 1 else 'entries'}; "
                f"runner configuration status: {config_status}.")
            limitation = {
                "code": "partial-evidence",
                "message": (
                    ("No source files were admitted to this project packet. "
                     if not agent_assessment.packet_has_evidence(packet)
                     else "Evidence limits: ")
                    + evidence_limits),
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
                    followups=calls,
                    model=declared):
                return _declined_review_output(parsed, resolution, domain)
            ensure_deadline()
            scheduler.prepare_state_directory(domain)
            ensure_deadline()
            explicit_model = (parsed.review_model is not None
                              or bool(os.environ.get("PTEST_REVIEW_MODEL")))
            model, version = _resolve_review_model(
                adapter, None, declared,
                require_listed_default=(adapter.name == "codex"
                                        and not explicit_model))
            if model is not None:
                effective = agent_providers.with_model(adapter, model)
        assessments = []
        completed_reviews = []
        followup_calls = 0
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
                        progress=lambda _event: heartbeat(),
                        deadline=deadline)
                except C.Problem as problem:
                    if problem.code == "review-cancelled":
                        raise _problem("review-cancelled", "review was cancelled") from None
                    raise
                except KeyboardInterrupt:
                    raise _problem("review-cancelled", "review was cancelled") from None
                ensure_deadline()
            replies: list[bytes | str | None] = [None] * len(reviews)
            cursor = 0
            for index, review in enumerate(reviews):
                if review.request is None:
                    continue
                result = results[cursor]
                cursor += 1
                reason = _review_failure_reason(result)
                replies[index] = result.assessment if reason is None else reason

            final_reviews = list(reviews)
            followup_jobs = []
            for index, review in enumerate(reviews):
                initial_reply = replies[index]
                if review.request is None or not isinstance(
                        initial_reply, (bytes, bytearray)):
                    continue
                next_review, failure = agent_assessment.plan_followup_review(
                    packet, review, bytes(initial_reply))
                if failure is not None:
                    replies[index] = failure
                elif next_review is not None:
                    followup_jobs.append((index, next_review))

            if followup_jobs:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _problem("review-timeout",
                                   "total review deadline expired")
                timeout_s = min(parsed.review_timeout_s, int(remaining))
                if timeout_s < 1:
                    raise _problem("review-timeout",
                                   "total review deadline expired")
                followup_pending = [(review.request, review.schema)
                                    for _index, review in followup_jobs]
                progress("verifying evidence", adapter.name, packet.scope,
                         time.monotonic() - started)
                try:
                    ensure_deadline()
                    followup_results = agent_providers.launch_reviews(
                        effective, followup_pending, timeout_s,
                        concurrency=parsed.review_concurrency,
                        on_done=lambda index, result, scope=packet.scope: progress(
                            "verifying evidence", adapter.name, scope,
                            time.monotonic() - started),
                        progress=lambda _event: heartbeat(),
                        deadline=deadline)
                except C.Problem as problem:
                    if problem.code == "review-cancelled":
                        raise _problem("review-cancelled",
                                       "review was cancelled") from None
                    raise
                except KeyboardInterrupt:
                    raise _problem("review-cancelled",
                                   "review was cancelled") from None
                ensure_deadline()
                for (index, followup_review), result in zip(
                        followup_jobs, followup_results):
                    final_reviews[index] = followup_review
                    reason = _review_failure_reason(result)
                    replies[index] = result.assessment if reason is None else reason
                followup_calls += len(followup_jobs)
            progress("validating", adapter.name, packet.scope,
                     time.monotonic() - started)
            ensure_deadline()
            assessments.append(_assemble_with_parallel(
                packet, tuple(final_reviews), tuple(replies)))
            completed_reviews.append(tuple(final_reviews))
            ensure_deadline()

        reviewed_rows = [(review, row)
                         for reviews, assessment in zip(completed_reviews,
                                                        assessments)
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
        report_payload = recommendations.render_recommendations(
            report_input,
            verification_scopes=_recommendation_verification_scopes(
                workspace, resolution),
            suite_identities=_recommendation_suite_identities(packets))
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
                color=sys.stdout.isatty(), repo=resolution.root.name,
                provider=(f"{adapter.name}/{model or 'provider-default'}"),
                duration_s=time.monotonic() - started,
                calls=calls + followup_calls,
                encoding=sys.stdout.encoding))
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


def _offline_progress(parsed: ParsedArgs,
                      resolution: C.ConfigResolution,
                      ) -> Callable[[str, int], None] | None:
    """One stderr progress line per offline child on a TTY, else None.

    ``-q``/``--quiet`` and a non-TTY stderr suppress the narration; JSON
    stdout is never written.
    """
    if parsed.quiet:
        return None
    try:
        tty = sys.stderr.isatty()
    except (OSError, ValueError):
        return None
    if not tty:
        return None
    root_name = resolution.root.name

    def emit(declaration: str, file_count: int) -> None:
        label = root_name if declaration == "." else declaration
        line = (f"ptest: doctor: inspecting "
                f"{render.terminal_text(label)} · "
                f"{C.plural(file_count, 'file')}")
        try:
            print(line, file=sys.stderr, flush=True)
        except OSError:
            pass

    return emit


def _doctor_static_output(parsed: ParsedArgs, resolution: C.ConfigResolution,
                          domain: C.DomainPaths) -> None:
    started = time.monotonic()
    deadline = started + _REVIEW_TOTAL_TIMEOUT_S
    on_child = _offline_progress(parsed, resolution)
    workspace = doctor.inspect_workspace(domain, resolution,
                                         _doctor_limits(parsed), parsed.scope)
    if parsed.json:
        sys.stdout.buffer.write(_doctor_offline_assessment_json(
            resolution, domain, workspace, deadline=deadline,
            on_child=on_child))
    else:
        child_data, _limitations, publication = _offline_assessment_parts(
            resolution, domain, workspace, deadline=deadline,
            on_child=on_child)
        sys.stdout.write(render.render_agent_assessment(
            child_data, workspace, report_path=publication["path"],
            publication_status=publication["status"],
            color=sys.stdout.isatty(), repo=resolution.root.name,
            provider="offline", duration_s=time.monotonic() - started,
            calls=0, encoding=sys.stdout.encoding))
        mention = _fix_mention(resolution)
        if mention is not None:
            sys.stdout.write(mention + "\n")
        uncommitted = _uncommitted_mention(resolution)
        if uncommitted is not None:
            sys.stdout.write(uncommitted + "\n")


def _uncommitted_mention(resolution: C.ConfigResolution) -> str | None:
    """Name uncommitted ptest files after doctor output; None when clean.

    Best effort only: never breaks doctor output.
    """
    try:
        paths = worktree_api.uncommitted_config_files(
            resolution.root, include_agent_rules=True)
    except Exception:
        return None
    if not paths:
        return None
    head = ", ".join(paths[:5])
    tail = f" (+{len(paths) - 5} more)" if len(paths) > 5 else ""
    return render.terminal_text(
        f"not committed: {head}{tail} — new worktrees and clones "
        "won't have them; commit them on the base branch")


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
    """Show the config preview and apply it. Never reviews, never asks.

    The ``--fix`` flag itself is the consent; ``--dry-run`` previews only.
    The preview is a short per-key summary (the ``[selection]`` mapping
    drafts compress to counts); ``-v`` shows the full unified diff. The
    file on disk always receives the full content.
    """
    plan = doctor_fix.plan_all(resolution.root, resolution)
    if plan.refusals:
        refusal = plan.refusals[0]
        raise _problem(refusal.code, f"{refusal.rel}: {refusal.message}")
    if not plan.files:
        print("config is up to date")
        return 0
    if parsed.verbose:
        sys.stdout.write(doctor_fix.render_diff(plan))
    else:
        sys.stdout.write(doctor_fix.render_summary(plan))
    if parsed.dry_run:
        return 0
    updated = doctor_fix.apply_plan(resolution.root, plan)
    for rel in updated:
        print(f"updated {rel}")
    if doctor_fix.selection_enabled_by(plan):
        print("selection enabled: bare ptest now runs the tests your change "
              "reaches (no baseline needed); run ptest --full once before handoff")
    return 0


_OFFLINE_UNKNOWN_REASON = "offline static run: model review unavailable"
_OFFLINE_DEADLINE_MESSAGE = (
    "Offline static inspection stopped at the doctor deadline "
    f"({_REVIEW_TOTAL_TIMEOUT_S} s); this project was not fully inspected.")


def _offline_assessment_parts(resolution: C.ConfigResolution,
                              domain: C.DomainPaths, workspace, *,
                              deadline: float | None = None,
                              on_child: Callable[[str, int], None] | None = None):
    """Shared offline children, limitations and publication record.

    Both ``--json`` and the terminal grid render from these parts, so the
    versioned document and the terminal table always describe the same
    assessment. No provider is launched and no report is written:
    deterministic items are answered from ptest's own facts while every
    item needing a model call becomes an ``unknown`` row carrying the
    offline reason. Children cut off by ``deadline`` carry the offline
    deadline partial-evidence limitation instead of inspected evidence.
    """
    result = agent_assessment.build_static_packets(
        workspace, resolution, deadline=deadline, on_child=on_child)
    packets = result.packets
    expired = set(result.deadline_expired)
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
        if packet.declaration in expired:
            deadline_limitation = {
                "code": "partial-evidence",
                "message": _OFFLINE_DEADLINE_MESSAGE,
                "paths": [packet.scope],
            }
            if deadline_limitation not in child_limitations:
                child_limitations.append(deadline_limitation)
        child_data.append(_child_assessment_data(
            packet, assessment, child_limitations,
            execution=executions.get(packet.declaration),
            facts=project_facts.get(packet.declaration)))
    limitations = _assessment_limitations(packets, top_level=True)
    if initialization_blocker is not None:
        limitations.insert(0, dict(initialization_blocker))
    for packet in packets:
        if packet.declaration in expired:
            deadline_limitation = {
                "code": "partial-evidence",
                "message": _OFFLINE_DEADLINE_MESSAGE,
                "paths": [packet.scope],
            }
            if deadline_limitation not in limitations:
                limitations.append(deadline_limitation)
    limitations = limitations[:64]
    publication = {"status": "skipped",
                   "path": "recommendations.md",
                   "sha256": "0" * 64}
    return child_data, limitations, publication


def _doctor_offline_assessment_json(
        resolution: C.ConfigResolution,
        domain: C.DomainPaths, workspace, *,
        deadline: float | None = None,
        on_child: Callable[[str, int], None] | None = None) -> bytes:
    """Build the versioned assessment document from static facts only."""
    child_data, limitations, publication = _offline_assessment_parts(
        resolution, domain, workspace, deadline=deadline, on_child=on_child)
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
            "publication": publication,
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


def _run_update(parsed: ParsedArgs) -> int:
    domain: C.DomainPaths | None = None
    try:
        domain = platform.domain_paths(parsed.fixture_domain)
        result = update_api.run_update(requested=parsed.update_version,
                                       check_only=parsed.update_check)
        if parsed.json:
            sys.stdout.buffer.write(_document(
                "update", update_api.document_data(result), domain=domain))
        else:
            sys.stdout.write(update_api.render_text(result))
        return 0
    except C.Problem as problem:
        return _emit_error(problem, kind="update",
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
            # Refresh the provider skills already installed here too, so
            # `rules --apply` clears an outdated-guidance warning on its own.
            installed = agent_rules.installed_providers(cwd)
            result = (agent_rules.apply(cwd, agents=installed) if parsed.apply_rules
                      else agent_rules.preview(cwd, agents=installed))
            label = "applied" if parsed.apply_rules else "preview"
            print(f"{label}: " + (", ".join(result.actions) or "already configured"))
            return 0
        except C.Problem as problem:
            return _emit_error(problem, kind="rules", json_output=False)
    if command == "uninstall":
        return _run_uninstall(parsed, cwd)
    if command == "update":
        return _run_update(parsed)
    if command == "init":
        try:
            if not parsed.from_main:
                uncommitted = config_api.config_uncommitted(cwd)
                if uncommitted is not None:
                    raise uncommitted
            elif (config_api.config_uncommitted(cwd) is None
                    and config_api.resolve_config(cwd).path is None):
                # init_project would refuse invalid-config here; fail
                # before the agents prompt asks anything. An existing
                # config keeps the from_main-is-ignored path below.
                raise _problem("invalid-config",
                               config_api.FROM_MAIN_REFUSAL)
            agents = _init_agents(parsed, json_output=parsed.json)
            root = config_api.repository_root(cwd)
            plan = agent_rules.preview(root, agents=agents) if agents else None
            result = config_api.init_project(cwd, C.InitOptions(
                runner=parsed.runner, dry_run=parsed.dry_run,
                reveal_command=parsed.reveal_command,
                children=parsed.children,
                agents=agents,
                from_main=parsed.from_main,
            ))
            applied = None
            if agents and not parsed.dry_run:
                applied = agent_rules.apply(root, agents=agents)
            elif not parsed.dry_run and not parsed.agents_explicit:
                # No prompt (an agent, CI, a pipe): still refresh guidance
                # ptest itself installed earlier, never anything new. It is
                # best effort: the config is already written, so a refresh
                # problem is one line, never init's result.
                try:
                    refreshed = agent_rules.refresh(root)
                except C.Problem as problem:
                    progress.emit(f"ptest: agent guidance not refreshed: "
                                  f"{problem.code} — run ptest rules --apply",
                                  quiet=False)
                else:
                    applied = refreshed if refreshed.changed else None
            if applied is not None and config_api.git_root(cwd) is not None:
                extra = tuple(detail.target for detail in applied.details
                              if detail.source == "guidance"
                              and detail.action in ("created", "updated")
                              and detail.target not in result.commit_paths)
                if extra:
                    result = replace(
                        result, commit_paths=result.commit_paths + extra)
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
            _report_selection_off(parsed, result, smoke_plans)
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
                if (resolution.problem is not None
                        and resolution.problem.code == "config-uncommitted"):
                    print(render.terminal_text(resolution.problem),
                          file=sys.stderr)
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
            if (resolution.problem is not None
                    and resolution.problem.code == "config-uncommitted"):
                raise resolution.problem
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
                        timeout_s=parsed.timeout_s,
                    ),
                )
                _emit_reasons(result)
                return result.exit_code
            if parsed.offline:
                _doctor_static_output(parsed, resolution, domain)
                return 0
            declined = _run_review_entry(
                parsed, resolution, domain,
                interactive=_interactive_review(),
            )
            if not declined and not parsed.json:
                uncommitted = _uncommitted_mention(resolution)
                if uncommitted is not None:
                    sys.stdout.write(uncommitted + "\n")
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
                          started: float, code: int, *, quiet: bool,
                          next_step: str | None = None) -> None:
    """Emit the existing total line over per-child outcomes."""
    total_counts = _summed_counts(
        [counts for _, _, counts in child_outcomes])
    status = _worst_status(
        [outcome for _, outcome, _ in child_outcomes])
    hint = (next_step is None
            and status in (C.Status.FAILED, C.Status.INCOMPLETE,
                           C.Status.NOT_RUN)
            and progress.claim_hint())
    progress.emit(progress.format_end(
        status, counts=total_counts,
        duration_s=time.monotonic() - started, exit_code=code,
        hint=hint, lead="total", color=sys.stderr.isatty(),
        next_step=next_step), quiet=quiet)


def _impact_api():
    """Graph-selection module, imported lazily at the routing boundary."""
    from . import impact
    return impact


def _impact_note(impact, label: str, *, reference: str) -> str:
    """Start-line note for one impact verdict (paths/label escaped).

    ``reference`` names the reference the changed set was diffed against:
    the last green run or the branch base.
    """
    if impact.kind == "full":
        return (f"{reference} → full suite: "
                f"{render.terminal_text(impact.reason)}")
    changed = tuple(impact.changed)
    if changed:
        first = render.terminal_text(changed[0])
        extra = len(changed) - 1
        count = "" if extra == 0 else f" (+{extra} file{'s' if extra > 1 else ''})"
        head = f"{reference}: {first}{count}"
    else:
        head = reference
    if impact.kind == "selected":
        return (f"{head} → {len(impact.files)} of {impact.total} test files "
                f"({impact.direct} direct · {impact.via} via importers)")
    if impact.kind == "vitest":
        return f"{head} → vitest --changed {render.terminal_text(label)}"
    return f"{head} → no tests affected · ptest --full runs everything"


def _impact_run_request(parsed: ParsedArgs, impact, base,
                        *, next_hint: bool,
                        reference: str) -> C.RunRequest | None:
    """Map one impact verdict to its run request; None means skip the run."""
    if impact.kind == "none":
        return None
    if impact.kind == "selected":
        mode, argv = C.Mode.SCOPED, tuple(impact.files)
    elif impact.kind == "vitest":
        mode, argv = C.Mode.SCOPED, ("--changed", base.sha or "HEAD")
    else:
        mode, argv = C.Mode.FULL, ()
    return C.RunRequest(
        mode=mode, argv=argv, base=None,
        workers=parsed.workers, queue_timeout_s=parsed.queue_timeout_s,
        timeout_s=parsed.timeout_s, no_setup=parsed.no_setup,
        result_path=parsed.result_path,
        fixture_domain=parsed.fixture_domain,
        verbose=parsed.verbose, quiet=parsed.quiet,
        changed_note=_impact_note(impact, base.label, reference=reference),
        next_hint=next_hint)


def _consult_reference(parsed: ParsedArgs, domain: C.DomainPaths, top,
                       project: str, base,
                       repo_changed: tuple[str, ...] | None):
    """Diff one project since its last green run, else the branch base.

    Returns ``(base, changed, reference, green)`` where ``base`` carries
    the reference commit (a green point or the branch base).  An explicit
    ``--base`` skips the cache entirely.  Cache I/O never raises and never
    changes the fallback verdict, only the changed set and the reference.
    """
    explicit = parsed.base is not None
    fallback = lastgreen.fallback_reference(base.label,
                                            explicit_base=explicit)
    if top is None:
        return base, repo_changed, fallback, False
    try:
        consultation = lastgreen.consult(
            domain.root, top, project,
            fallback_sha=base.sha, fallback_label=base.label,
            fallback_changed=repo_changed, explicit_base=explicit)
    except Exception:
        return base, repo_changed, fallback, False
    return consultation, consultation.changed, consultation.reference, \
        consultation.green


def _is_test_file_token(token: str) -> bool:
    """Whether a run argv entry names a pytest test file (or node)."""
    base = token.split("::", 1)[0].rsplit("/", 1)[-1]
    return ((base.startswith("test_") and base.endswith(".py"))
            or base.endswith("_test.py"))


def _emit_reasons(result) -> None:
    """Print one result's reason lines, unless SIGINT cancelled the run.

    A Ctrl-C run prints only its ``ptest: cancelled ...`` end line; the
    protocol-mismatch / state-unavailable follow-ons are consequences of
    the cancellation. Genuine failures keep their lines.
    """
    if getattr(result, "signal", None) == signal.SIGINT:
        return
    for reason in result.reasons:
        print(render.terminal_text(f"{reason.code}: {reason.message}"),
              file=sys.stderr)


def _warn_stale_guidance(root: Path) -> None:
    """Nudge once per run when managed guidance is a recognised older version.

    Read-only: never writes, and stays silent on current, user-edited, or
    missing guidance. Failures never affect the run.
    """
    try:
        from . import agent_rules
        stale = agent_rules.guidance_outdated(root)
    except Exception:
        return
    if stale:
        progress.emit("ptest: agent guidance is outdated — run ptest rules "
                      "--apply to update", quiet=False)


def _notice_config_upgrade(resolution: C.ConfigResolution, parsed) -> None:
    """Once per ptest version and checkout: say when doctor --fix would
    improve this config. init never rewrites an existing .ptest.toml, so an
    upgrade reaches old configs only through doctor --fix. Best-effort and
    read-only for the project; any problem means silence."""
    try:
        if resolution.config is None and resolution.monorepo is None:
            return
        domain = platform.domain_paths(parsed.fixture_domain)
        state = Path(domain.root)
        if not state.is_dir():
            return
        seen_dir = state / "seen-versions"
        if not seen_dir.exists():
            files.ensure_private_dir(state, "seen-versions")
        name = hashlib.sha256(os.fsencode(os.path.realpath(resolution.root))).hexdigest()[:32]
        try:
            seen = files.read_regular(seen_dir, name, 64).decode("ascii").strip()
        except (C.Problem, UnicodeDecodeError):
            seen = ""
        if seen == C.PTEST_VERSION:
            return
        files.publish_atomic(seen_dir, name, C.PTEST_VERSION.encode("ascii"))
        plan = doctor_fix.plan_all(resolution.root, resolution)
        count = int(plan.change_count)
        if count:
            progress.emit(
                f"ptest: ptest {C.PTEST_VERSION} can improve this config "
                f"({count} change{'s' if count != 1 else ''}) — run ptest doctor --fix",
                quiet=parsed.quiet)
    except Exception:
        return


def _warn_uncommitted_config(resolution: C.ConfigResolution, *,
                             quiet: bool) -> None:
    """One stderr line per executing run when config is not committed.

    Best effort only: never alters output or exit status, never fails a run.
    """
    try:
        if resolution.config is None and resolution.monorepo is None:
            return
        paths = worktree_api.uncommitted_config_files(resolution.root)
        if not paths:
            return
        if len(paths) == 1:
            line = (f"ptest: {paths[0]} is not committed"
                    " — new worktrees won't have it")
        else:
            head = ", ".join(paths[:3])
            tail = f" (+{len(paths) - 3} more)" if len(paths) > 3 else ""
            line = (f"ptest: {head}{tail} are not committed"
                    " — new worktrees won't have them")
        progress.emit(render.terminal_text(line), quiet=quiet)
    except Exception:
        return


def _normalize_scope_text(scope: str) -> str:
    """Strip ``./`` prefixes and a trailing ``/`` (shell completion)."""
    while scope.startswith("./"):
        scope = scope[2:]
    return scope.rstrip("/")


def _scope_parts(scope: str) -> tuple[str, ...] | None:
    """Safe relative segments for a path scope; None when not a path.

    Runner flags (a leading ``-``) are never paths: they keep the legacy
    literal handling instead of nearest-config routing.
    """
    from . import monorepo

    typed = _normalize_scope_text(scope)
    if not typed or typed.startswith("-"):
        return None
    try:
        return monorepo._safe_segments(typed)
    except C.Problem:
        return None


def _split_standalone_scopes(scopes: tuple[str, ...], root: Path):
    """Split standalone scopes into always-run files and changed folders.

    A ``::`` node id is always a file; otherwise an existing directory is
    a folder and anything else is a file the user named explicitly.
    Returns ``(files, folders)`` of ``(typed, local)`` pairs.
    """
    from . import monorepo

    files: list[tuple[str, str]] = []
    folders: list[tuple[str, str]] = []
    for scope in scopes:
        typed = _normalize_scope_text(scope)
        try:
            parts = monorepo._safe_segments(typed)
        except C.Problem:
            raise _problem(
                "invalid-config",
                "test paths must be relative to the repository root, "
                'without ".." (for example "tests/test_x.py")') from None
        local = "/".join(parts)
        if "::" in local or not (Path(root) / local).is_dir():
            files.append((typed, local))
        else:
            folders.append((typed, local))
    return files, folders


def _folder_note(typeds: tuple[str, ...], selected: int, total: int) -> str:
    """Start-line note for a folder-changed run: scope plus file counts."""
    return f"{' '.join(typeds)} → {selected} of {total} test files"


def _run_scoped_request(parsed: ParsedArgs, argv: tuple[str, ...], *,
                        note: str | None = None,
                        display: tuple[str, ...] | None = None,
                        next_hint: bool = False) -> C.RunRequest:
    """Build one literal scoped request (an explicitly narrowed run)."""
    return C.RunRequest(
        mode=C.Mode.SCOPED, argv=argv,
        workers=parsed.workers, queue_timeout_s=parsed.queue_timeout_s,
        timeout_s=parsed.timeout_s,
        no_setup=parsed.no_setup,
        result_path=parsed.result_path,
        fixture_domain=parsed.fixture_domain,
        verbose=parsed.verbose, quiet=parsed.quiet,
        changed_note=note, display_argv=display, next_hint=next_hint)


def _note_run(domain: C.DomainPaths, root: Path, project: str,
              request: C.RunRequest, result) -> None:
    """Record the green point on pass, failed test files on failure.

    Cache only: a bare/--changed/scoped/full pass moves the project's
    verified point, a failure merges its test files into the last-failed
    set.  Never raises and never affects the run outcome.
    """
    try:
        if request.shadow or request.probe is not None:
            return
        if request.mode is not C.Mode.SCOPED \
                and request.mode is not C.Mode.FULL:
            return
        top = _impact_api().git_top(Path(root))
        if top is None:
            return
        status = getattr(result, "status", None)
        if status is C.Status.PASSED:
            lastgreen.record_pass(domain.root, os.fspath(top), project, top)
        elif status is C.Status.FAILED:
            argv = getattr(request, "argv", ()) or ()
            files = tuple(token.split("::", 1)[0] for token in argv
                          if isinstance(token, str)
                          and _is_test_file_token(token))
            lastgreen.record_failure(domain.root, os.fspath(top), project,
                                     files)
    except Exception:
        return


def _impact_base_and_changed(parsed: ParsedArgs, root: Path):
    """Resolve the branch base and repo-level changed set once per run."""
    impact_api = _impact_api()
    top = impact_api.git_top(root)
    base = impact_api.resolve_base(top, parsed.base)
    return impact_api, top, base, impact_api.changed_files(top, base)


def _emit_ignored_count(parsed: ParsedArgs, impact) -> None:
    """Verbose-only note for changed files excluded as build output or as
    non-code outside the project's source and test areas. Never printed
    on the normal line: without ``-v`` the excluded files stay silent."""
    count = getattr(impact, "ignored", 0) or 0
    if parsed.verbose and not parsed.quiet and count > 0:
        word = "file" if count == 1 else "files"
        progress.emit(f"ptest: -v ignored {count} non-code/output {word}",
                      quiet=False)


def _run_impact_standalone(parsed: ParsedArgs, resolution: C.ConfigResolution,
                           domain: C.DomainPaths) -> int:
    """Route bare `ptest` / `ptest --changed` through the import graph."""
    assert resolution.config is not None
    impact_api, top, base, repo_changed = _impact_base_and_changed(
        parsed, resolution.root)
    base, repo_changed, reference, green = _consult_reference(
        parsed, domain, top, "", base, repo_changed)
    impact = impact_api.plan(top, resolution.root, resolution.config,
                             repo_changed)
    _emit_ignored_count(parsed, impact)
    request = _impact_run_request(parsed, impact, base, next_hint=True,
                                  reference=reference)
    if request is None:
        project = render.terminal_text(resolution.root.name)
        if impact.changed:
            progress.emit(progress.format_impact(
                project, _impact_note(impact, base.label,
                                      reference=reference),
                color=sys.stderr.isatty()), quiet=parsed.quiet)
        elif green:
            progress.emit(progress.format_no_green_changes(
                color=sys.stderr.isatty()), quiet=parsed.quiet)
        else:
            progress.emit(progress.format_nothing_changed(
                render.terminal_text(base.label),
                color=sys.stderr.isatty(),
                no_green_run=parsed.base is None), quiet=parsed.quiet)
        return 0
    result = operations.execute(domain, resolution.config, request)
    _note_run(domain, resolution.root, "", request, result)
    _emit_reasons(result)
    return result.exit_code


#: Rejection when --again names a scope: it reruns the whole gate only.
_AGAIN_WITH_SCOPE = ("--again reruns the whole gate, so it takes no path "
                     "(use bare `ptest --full --again`)")


def _under_any(path: str, folders: tuple[str, ...]) -> bool:
    """Whether a child-relative file sits under any folder scope."""
    return any(path == folder or folder == ""
               or path.startswith(folder + "/") for folder in folders)


def _all_under_note(typeds: tuple[str, ...]) -> str:
    """Start-line note for a --full folder run: every test under it."""
    shown = " ".join(render.terminal_text(item) for item in typeds)
    return f"all tests under {shown}"


def _run_folder_monorepo(parsed: ParsedArgs, resolution: C.ConfigResolution,
                         domain: C.DomainPaths, split) -> int:
    """Changed-mode run for folder scopes (plus always-run files)."""
    from . import monorepo

    target = split.target
    impact_api, top, base, repo_changed = _impact_base_and_changed(
        parsed, resolution.root)
    child_base, child_changed, reference, _green = _consult_reference(
        parsed, domain, top, target.declaration, base, repo_changed)
    impact = impact_api.plan(top, target.directory, target.config,
                             child_changed)
    _emit_ignored_count(parsed, impact)
    always = [item.local for item in split.files]
    folders = tuple(item.local for item in split.folders)
    typeds = tuple(item.typed for item in split.folders)
    if impact.kind == "vitest":
        argv = (("--changed", child_base.sha or "HEAD")
                + tuple(local for local in folders if local)
                + tuple(always))
        note = (f"{' '.join(render.terminal_text(item) for item in typeds)}"
                f" → vitest --changed "
                f"{render.terminal_text(child_base.label)}")
        request = _run_scoped_request(parsed, argv, note=note, next_hint=True)
        result = operations.execute(domain, target.config, request)
        _note_run(domain, resolution.root, target.declaration, request,
                  result)
        _emit_reasons(result)
        return result.exit_code
    if impact.kind == "full":
        argv = []
        for local in folders:
            if local:
                argv.append(local)
            else:
                argv.extend(monorepo._child_test_roots(target))
        argv.extend(item for item in always if item not in argv)
        if argv:
            request = _run_scoped_request(
                parsed, tuple(argv), note=_all_under_note(typeds),
                next_hint=True)
        else:
            request = _impact_run_request(parsed, impact, child_base,
                                          next_hint=True, reference=reference)
        result = operations.execute(domain, target.config, request)
        _note_run(domain, resolution.root, target.declaration, request,
                  result)
        _emit_reasons(result)
        return result.exit_code
    if impact.kind == "selected":
        under = [path for path in impact.files
                 if _under_any(path, folders)]
        selected = list(under) + [item for item in always
                                  if item not in under]
    else:
        selected = list(always)
    if not selected:
        project = render.terminal_text(target.declaration)
        for item in split.folders:
            progress.emit(progress.format_no_changes_under(
                project, render.terminal_text(item.typed),
                color=sys.stderr.isatty()), quiet=parsed.quiet)
        return 0
    request = _run_scoped_request(
        parsed, tuple(selected),
        note=_folder_note(typeds, len(selected), impact.total),
        next_hint=True)
    result = operations.execute(domain, target.config, request)
    _note_run(domain, resolution.root, target.declaration, request, result)
    _emit_reasons(result)
    return result.exit_code


def _run_full_folder_monorepo(parsed: ParsedArgs,
                              resolution: C.ConfigResolution,
                              domain: C.DomainPaths, split) -> int:
    """``ptest --full <folder>``: every test under the folders, no gate."""
    from . import monorepo

    argv: list[str] = []
    for item in split.folders:
        if item.local:
            argv.append(item.local)
        else:
            argv.extend(monorepo._child_test_roots(split.target))
    request = _run_scoped_request(
        parsed, tuple(argv),
        note=_all_under_note(tuple(item.typed for item in split.folders)),
        next_hint=True)
    result = operations.execute(domain, split.target.config, request)
    _note_run(domain, resolution.root, split.target.declaration, request,
              result)
    _emit_reasons(result)
    return result.exit_code


def _run_folder_standalone(parsed: ParsedArgs,
                           resolution: C.ConfigResolution,
                           domain: C.DomainPaths, files, folders) -> int:
    """Changed-mode run for standalone folder scopes (plus files)."""
    assert resolution.config is not None
    impact_api, top, base, repo_changed = _impact_base_and_changed(
        parsed, resolution.root)
    base, repo_changed, reference, _green = _consult_reference(
        parsed, domain, top, "", base, repo_changed)
    impact = impact_api.plan(top, resolution.root, resolution.config,
                             repo_changed)
    _emit_ignored_count(parsed, impact)
    always = [local for _, local in files]
    under_roots = tuple(local for _, local in folders)
    typeds = tuple(typed for typed, _ in folders)
    if impact.kind == "vitest":
        argv = (("--changed", base.sha or "HEAD")
                + under_roots + tuple(always))
        note = (f"{' '.join(render.terminal_text(item) for item in typeds)}"
                f" → vitest --changed {render.terminal_text(base.label)}")
        request = _run_scoped_request(parsed, argv, note=note, next_hint=True)
        result = operations.execute(domain, resolution.config, request)
        _note_run(domain, resolution.root, "", request, result)
        _emit_reasons(result)
        return result.exit_code
    if impact.kind == "full":
        argv = list(under_roots)
        argv.extend(item for item in always if item not in argv)
        if argv:
            request = _run_scoped_request(
                parsed, tuple(argv), note=_all_under_note(typeds),
                next_hint=True)
        else:
            request = _impact_run_request(parsed, impact, base,
                                          next_hint=True, reference=reference)
        result = operations.execute(domain, resolution.config, request)
        _note_run(domain, resolution.root, "", request, result)
        _emit_reasons(result)
        return result.exit_code
    if impact.kind == "selected":
        under = [path for path in impact.files
                 if _under_any(path, under_roots)]
        selected = list(under) + [item for item in always
                                  if item not in under]
    else:
        selected = list(always)
    if not selected:
        project = render.terminal_text(resolution.root.name)
        for typed, _ in folders:
            progress.emit(progress.format_no_changes_under(
                project, render.terminal_text(typed),
                color=sys.stderr.isatty()), quiet=parsed.quiet)
        return 0
    request = _run_scoped_request(
        parsed, tuple(selected),
        note=_folder_note(typeds, len(selected), impact.total),
        next_hint=True)
    result = operations.execute(domain, resolution.config, request)
    _note_run(domain, resolution.root, "", request, result)
    _emit_reasons(result)
    return result.exit_code


def _run_full_folder_standalone(parsed: ParsedArgs,
                                resolution: C.ConfigResolution,
                                domain: C.DomainPaths, folders) -> int:
    """``ptest --full <folder>`` outside a monorepo: everything there."""
    assert resolution.config is not None
    argv = tuple(local for _, local in folders)
    typeds = tuple(typed for typed, _ in folders)
    request = _run_scoped_request(parsed, argv,
                                  note=_all_under_note(typeds),
                                  next_hint=True)
    result = operations.execute(domain, resolution.config, request)
    _note_run(domain, resolution.root, "", request, result)
    _emit_reasons(result)
    return result.exit_code


def _nearest_config_dir(parts: tuple[str, ...], cwd: Path) -> Path | None:
    """Nearest directory at or above a scope holding a regular .ptest.toml."""
    start = Path(cwd, *parts)
    cursor = start if start.is_dir() else start.parent
    top = Path(cwd)
    while True:
        candidate = cursor / ".ptest.toml"
        try:
            stamp = os.lstat(candidate)
        except FileNotFoundError:
            stamp = None
        except OSError:
            stamp = None
        if stamp is not None and stat.S_ISREG(stamp.st_mode) \
                and not stat.S_ISLNK(stamp.st_mode):
            return cursor
        if cursor == top:
            return None
        cursor = cursor.parent


def _reroute_nearest(parsed: ParsedArgs, cwd: Path):
    """Route path scopes through the nearest config when cwd has none.

    Returns None when a scope is not a path (the caller keeps its legacy
    handling), ``("missing", typed)`` when no config sits above a path,
    else ``(resolution, rebased scopes)`` rooted at the nearest config.
    """
    from . import config as config_api

    narrowed: list[tuple[str, tuple[str, ...]]] = []
    for scope in parsed.runner_argv:
        parts = _scope_parts(scope)
        if parts is None:
            return None
        narrowed.append((_normalize_scope_text(scope), parts))
    hits: list[tuple[str, tuple[str, ...], Path]] = []
    for typed, parts in narrowed:
        root = _nearest_config_dir(parts, cwd)
        if root is None:
            return ("missing", typed)
        hits.append((typed, parts, root))
    roots = {os.fspath(found) for _, _, found in hits}
    if len(roots) != 1:
        raise _problem("invalid-config", "run one project at a time: all "
                                        "paths must be inside the same project")
    root = hits[0][2]
    resolution = config_api.resolve_config(root)
    if resolution.config is None and resolution.monorepo is None:
        raise resolution.problem or _problem(
            "initialization-required", "project configuration is required")
    rebased: list[str] = []
    for typed, parts, found in hits:
        text = Path(cwd, *parts).relative_to(found).as_posix()
        rebased.append("" if text == "." else text)
    return resolution, tuple(rebased)


def _run_monorepo_automatic(parsed: ParsedArgs, children,
                            domain: C.DomainPaths) -> int:
    """Legacy per-child AUTOMATIC loop for shadow/probe at a monorepo root.

    The graph engine owns bare/`--changed`; shadowing and probing keep the
    history/baseline engine one child at a time, flags carried through.
    """
    started = time.monotonic()
    outcomes: list[tuple[int, C.Status, C.Counts | None]] = []
    first_failure = 0
    cancelled = False
    for child in children:
        result = operations.execute(
            domain, child.config,
            C.RunRequest(
                mode=C.Mode.AUTOMATIC, base=parsed.base,
                workers=parsed.workers,
                queue_timeout_s=parsed.queue_timeout_s,
                timeout_s=parsed.timeout_s,
                no_setup=parsed.no_setup, shadow=parsed.shadow,
                result_path=parsed.result_path,
                fixture_domain=parsed.fixture_domain,
                probe=parsed.probe,
                verbose=parsed.verbose, quiet=parsed.quiet),
        )
        _emit_reasons(result)
        if getattr(result, "signal", None) == signal.SIGINT:
            cancelled = True
        outcomes.append((result.exit_code, result.status, result.counts))
        if result.exit_code and not first_failure:
            first_failure = result.exit_code
    if not cancelled:
        _emit_monorepo_total(outcomes, started, first_failure,
                             quiet=parsed.quiet)
    return first_failure


def _run_impact_monorepo(parsed: ParsedArgs, resolution: C.ConfigResolution,
                         children, domain: C.DomainPaths) -> int:
    """Route a monorepo root bare/`--changed` run child by child."""
    impact_api, top, base, repo_changed = _impact_base_and_changed(
        parsed, resolution.root)
    consulted = []
    for child in children:
        child_base, child_changed, reference, green = _consult_reference(
            parsed, domain, top, child.declaration, base, repo_changed)
        consulted.append((child, child_base, child_changed, reference, green))
    planned = [(child, child_base, impact_api.plan(top, child.directory,
                                                   child.config,
                                                   child_changed), reference)
               for child, child_base, child_changed, reference, green
               in consulted]
    if all(impact.kind == "none" and not impact.changed
           for _, _, impact, _ in planned):
        for _, _, impact, _ in planned:
            _emit_ignored_count(parsed, impact)
        greens = [green for _, _, _, _, green in consulted]
        if all(greens):
            progress.emit(progress.format_no_green_changes(
                color=sys.stderr.isatty()), quiet=parsed.quiet)
        elif any(greens):
            # Children compare against different references (some have a
            # green run, some not): make no claim about a single reference.
            progress.emit(progress.format_no_changes_anywhere(
                color=sys.stderr.isatty()), quiet=parsed.quiet)
        else:
            progress.emit(progress.format_nothing_changed(
                render.terminal_text(base.label),
                color=sys.stderr.isatty(),
                no_green_run=parsed.base is None), quiet=parsed.quiet)
        return 0
    started = time.monotonic()
    outcomes: list[tuple[int, C.Status, C.Counts | None]] = []
    first_failure = 0
    narrowed = False
    cancelled = False
    for child, child_base, impact, reference in planned:
        _emit_ignored_count(parsed, impact)
        request = _impact_run_request(parsed, impact, child_base,
                                      next_hint=False, reference=reference)
        if request is None:
            if impact.changed:
                narrowed = True
            if not parsed.quiet:
                print(progress.format_no_changes(
                    render.terminal_text(child.declaration),
                    color=sys.stderr.isatty()), file=sys.stderr)
            outcomes.append((0, C.Status.NO_TESTS_NEEDED, None))
            continue
        if request.mode is C.Mode.SCOPED:
            narrowed = True
        result = operations.execute(domain, child.config, request)
        _note_run(domain, resolution.root, child.declaration, request,
                  result)
        _emit_reasons(result)
        if getattr(result, "signal", None) == signal.SIGINT:
            cancelled = True
        outcomes.append((result.exit_code, result.status, result.counts))
        if result.exit_code and not first_failure:
            first_failure = result.exit_code
    worst = _worst_status([status for _, status, _ in outcomes])
    if not cancelled:
        _emit_monorepo_total(outcomes, started, first_failure,
                             quiet=parsed.quiet,
                             next_step=progress.next_step(worst, narrowed))
    return first_failure


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
        if parsed.command not in _UPDATE_CHECK_EXEMPT:
            update_api.startup_check(
                raw_args, quiet=parsed.quiet,
                json_output=parsed.json,
                fixture=parsed.fixture_domain is not None)
        if parsed.command in _INSPECTION or parsed.command in {"help", "version"}:
            return _static_dispatch(parsed, Path.cwd())
        resolution = config_api.resolve_config(Path.cwd())
        if (resolution.monorepo is None and resolution.config is None
                and parsed.runner_argv and resolution.problem is not None
                and resolution.problem.code in {"initialization-required",
                                                "config-uncommitted"}):
            # No root manifest is required to route a path: it resolves
            # to the nearest config at or above it. Bare `ptest` still
            # needs the root manifest to discover projects.
            rerouted = _reroute_nearest(parsed, Path.cwd())
            if isinstance(rerouted, tuple) and rerouted[0] == "missing":
                missing_parts = _scope_parts(rerouted[1])
                if missing_parts is not None:
                    start = Path(Path.cwd(), *missing_parts)
                    scope_dir = start if start.is_dir() else start.parent
                    uncommitted = config_api.config_uncommitted(scope_dir)
                    if uncommitted is None:
                        # The scope path itself is usually absent from the
                        # worktree (uncommitted main files never arrive), so
                        # fall back to the invocation directory, which names
                        # the same frozen problem via the nearest-first walk.
                        uncommitted = config_api.config_uncommitted(Path.cwd())
                    if uncommitted is not None:
                        raise uncommitted
                print(render.terminal_text(
                    f"ptest: no ptest project for {rerouted[1]}"
                    " — run ptest init there"), file=sys.stderr)
                return 2
            if rerouted is not None:
                resolution, rebased = rerouted
                kept = tuple(scope for scope in rebased if scope != "")
                parsed = replace(parsed, runner_argv=kept)
                if not kept and not parsed.full:
                    parsed = replace(parsed, mode=C.Mode.AUTOMATIC)
        progress.reset()
        # A config-uncommitted refusal follows below and forbids `ptest
        # init`; the stale-guidance hint must not name it first.
        if (resolution.problem is None
                or resolution.problem.code != "config-uncommitted"):
            _warn_stale_guidance(resolution.root)
        _warn_uncommitted_config(resolution, quiet=parsed.quiet)
        _notice_config_upgrade(resolution, parsed)
        if resolution.monorepo is not None:
            from . import monorepo
            domain = platform.domain_paths(parsed.fixture_domain)
            platform.validate_state_outside_checkout(
                domain, _state_anchor(resolution.root))
            children = monorepo.preflight_children(resolution.root, resolution.monorepo)
            if parsed.full:
                def child_full_request() -> C.RunRequest:
                    return C.RunRequest(
                        mode=C.Mode.FULL, workers=parsed.workers,
                        queue_timeout_s=parsed.queue_timeout_s,
                        timeout_s=parsed.timeout_s,
                        no_setup=parsed.no_setup,
                        result_path=parsed.result_path,
                        fixture_domain=parsed.fixture_domain,
                        verbose=parsed.verbose, quiet=parsed.quiet,
                        again=parsed.again)

                cancelled: list[bool] = []

                def run_full(child):
                    request = child_full_request()
                    result = operations.execute(
                        domain, child.config, request)
                    _note_run(domain, resolution.root, child.declaration,
                              request, result)
                    _emit_reasons(result)
                    if getattr(result, "signal", None) == signal.SIGINT:
                        cancelled.append(True)
                    child_outcomes.append(
                        (result.exit_code, result.status, result.counts))
                    return result.exit_code
                child_outcomes: list[tuple[int, C.Status, C.Counts | None]] = []
                if parsed.runner_argv:
                    # `ptest --full <folder>` runs every test under those
                    # folders: not the integrated gate (no
                    # already-verified skip, no join).
                    if parsed.again:
                        raise _problem("invalid-config", _AGAIN_WITH_SCOPE)
                    split = monorepo.split_scopes(
                        parsed.runner_argv, children)
                    if split.files:
                        raise _problem(
                            "invalid-config",
                            monorepo.full_scope_message(
                                children, typed=split.files[0].typed))
                    return _run_full_folder_monorepo(
                        parsed, resolution, domain, split)
                started = time.monotonic()
                code = monorepo.execute_full(children, run_full)
                if not cancelled:
                    _emit_monorepo_total(child_outcomes, started, code,
                                         quiet=parsed.quiet)
                return code
            # Bare `ptest` at a monorepo root is the cheap loop: no scope
            # and no mode flag selects the same changed path as --changed.
            # (Both arrive here as AUTOMATIC with no runner tail.)
            if parsed.changed or parsed.mode is C.Mode.AUTOMATIC:
                if parsed.shadow or parsed.probe is not None:
                    return _run_monorepo_automatic(parsed, children, domain)
                return _run_impact_monorepo(parsed, resolution, children,
                                            domain)
            # A path only narrows WHERE: files (and node ids) always run,
            # folders run the changed tests under them.
            split = monorepo.split_scopes(parsed.runner_argv, children)
            if split.folders:
                return _run_folder_monorepo(parsed, resolution, domain,
                                            split)
            scoped_request = C.RunRequest(
                mode=C.Mode.SCOPED,
                argv=tuple(item.local for item in split.files),
                workers=parsed.workers, queue_timeout_s=parsed.queue_timeout_s,
                timeout_s=parsed.timeout_s,
                no_setup=parsed.no_setup,
                shadow=parsed.shadow, result_path=parsed.result_path,
                fixture_domain=parsed.fixture_domain,
                verbose=parsed.verbose, quiet=parsed.quiet,
                display_argv=parsed.runner_argv)
            result = operations.execute(
                domain, split.target.config, scoped_request)
            _note_run(domain, resolution.root, split.target.declaration,
                      scoped_request, result)
            _emit_reasons(result)
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
        if parsed.full and parsed.runner_argv:
            # `ptest --full <folder>` runs every test under those
            # folders: not the integrated gate (no already-verified
            # skip, no join). A named file is rejected in plain words.
            if parsed.again:
                raise _problem("invalid-config", _AGAIN_WITH_SCOPE)
            from . import monorepo
            scoped_files, scoped_folders = _split_standalone_scopes(
                parsed.runner_argv, resolution.root)
            if scoped_files:
                raise _problem(
                    "invalid-config",
                    monorepo.full_scope_message(
                        (), typed=scoped_files[0][0]))
            standalone_domain = platform.domain_paths(parsed.fixture_domain)
            return _run_full_folder_standalone(
                parsed, resolution, standalone_domain, scoped_folders)
        domain = platform.domain_paths(parsed.fixture_domain)
        if (parsed.mode is C.Mode.AUTOMATIC and not parsed.shadow
                and parsed.probe is None):
            return _run_impact_standalone(parsed, resolution, domain)
        if parsed.mode is C.Mode.SCOPED:
            scoped_files, scoped_folders = _split_standalone_scopes(
                parsed.runner_argv, resolution.root)
            if scoped_folders:
                return _run_folder_standalone(
                    parsed, resolution, domain, scoped_files, scoped_folders)
        request = C.RunRequest(
            mode=parsed.mode,
            argv=parsed.runner_argv,
            base=parsed.base,
            workers=parsed.workers,
            queue_timeout_s=parsed.queue_timeout_s,
            timeout_s=parsed.timeout_s,
            no_setup=parsed.no_setup,
            shadow=parsed.shadow,
            result_path=parsed.result_path,
            fixture_domain=parsed.fixture_domain,
            probe=parsed.probe,
            verbose=parsed.verbose,
            quiet=parsed.quiet,
            again=parsed.again,
        )
        result = operations.execute(domain, resolution.config, request)
        _note_run(domain, resolution.root, "", request, result)
        _emit_reasons(result)
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
