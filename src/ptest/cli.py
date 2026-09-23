"""Closed NG command-line grammar and static dispatch.

This slice owns parsing and inspection only.  Execution deliberately stops at
the typed capability boundary until the scheduler/guard orchestration lands.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from . import agent_assessment, agent_providers, agent_rules, config as config_api
from . import contracts as C
from . import doctor, executability, files, help as help_api, history
from . import init_render
from . import operations, platform, recommendations, scheduler
from . import render
from .adapters import pytest as pytest_adapter
from .runners import adapter_for


_INSPECTION = frozenset({
    "init", "register", "where", "status", "history", "plan",
    "doctor", "guide", "rules",
})
_EXECUTION_VALUE = frozenset({
    "--base", "--workers", "--queue-timeout", "--result-json",
})
_EXECUTION_BOOL = frozenset({"--changed", "--full", "--no-setup", "--shadow"})
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
    result_path: str | None = None
    changed: bool = False
    full: bool = False
    json: bool = False
    prompt: bool = False
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
    children: tuple = ()
    agents: tuple[str, ...] = ()
    agents_explicit: bool = False
    help_topic: str | None = None
    reviewer: str | None = None
    reviewer_explicit: bool = False
    allow_model_review: bool = False
    assessment_json: bool = False
    review_timeout_s: int = 300
    review_timeout_explicit: bool = False
    review_model: str | None = None
    review_model_explicit: bool = False
    review_concurrency: int = 4
    review_concurrency_explicit: bool = False
    offline: bool = False
    doctor_request: bool | None = None


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
        changed=changed, full=full,
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
        index = 0
        while index < len(args):
            token = args[index]
            if token == "--dry-run":
                dry_run = True
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
            elif token == "--assessment-json":
                raise _problem("invalid-config", "assessment output is only available for doctor")
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
                          doctor_request=doctor_request)
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
        json_output = prompt = False
        reviewer = None
        reviewer_seen = allow_seen = assessment_seen = timeout_seen = False
        model_seen = concurrency_seen = False
        allow_model_review = assessment_json = offline = False
        offline_seen = False
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
            elif token == "--prompt":
                prompt = True
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
            elif token == "--assessment-json":
                if assessment_seen:
                    raise _problem("invalid-config", "option cannot be repeated")
                assessment_seen = True
                assessment_json = True
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
            if json_output or prompt:
                raise _problem("invalid-config", "doctor probe cannot combine output modes")
            if (reviewer_seen or allow_seen or assessment_seen or timeout_seen
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
        if json_output and prompt:
            raise _problem("invalid-config", "doctor output modes cannot be combined")
        if offline and (json_output or prompt):
            raise _problem("invalid-config", "doctor output modes cannot be combined")
        if assessment_seen and (json_output or prompt or offline):
            raise _problem("invalid-config", "assessment output cannot combine with static modes")
        if (reviewer_seen or allow_seen or timeout_seen or model_seen
                or concurrency_seen) and (json_output or prompt or offline):
            raise _problem("invalid-config", "review options cannot combine with static modes")
        return ParsedArgs(command=command, json=json_output, prompt=prompt,
                          scope=scope, reviewer=reviewer,
                          reviewer_explicit=reviewer_seen,
                          allow_model_review=allow_model_review,
                          assessment_json=assessment_json,
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
                message=("Vitest runs as one exclusive command (node "
                         "node_modules/vitest/vitest.mjs run); ptest does "
                         "not own Vitest workers, selection or per-test "
                         "results"),
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
        print(render.terminal_text(problem), file=sys.stderr)
    if problem.code in {"review-timeout", "execution-timeout"}:
        return 124
    if problem.code in {"review-cancelled", "cancelled"}:
        return 130
    return 2 if problem.code not in {"coordinator-unavailable", "queue-timeout"} else 75


def _interactive_review() -> bool:
    """TTY review is interactive only outside CI, even if stdin is a TTY."""
    return sys.stdin.isatty() and "CI" not in os.environ


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
        raw = (Path(cache_root) / f"{provider}.json").read_bytes()
    except OSError:
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
    return model


def _write_cached_review_model(cache_root: Path, provider: str,
                               cli_version: str | None, model: str) -> None:
    """Persist a pick result; cache failures never fail the review."""
    if cli_version is None:
        return
    try:
        root = Path(cache_root)
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{provider}.json").write_text(
            json.dumps({"cli_version": cli_version, "model": model},
                       sort_keys=True),
            encoding="utf-8")
    except OSError:
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
            entries = agent_providers._discover_model_entries(adapter)
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
    if sys.stderr.isatty():
        # The collecting spinner line has no trailing newline; terminate
        # it before the consent text, as the success/error paths do.
        print(file=sys.stderr)
    provider = render.terminal_text(adapter.name[:120])
    project = render.terminal_text(resolution.root.name[:120])
    disclosure = (
        f"Model review disclosure: {provider} may receive bounded source text "
        f"from project {project} using your existing account. Provider or "
        "account costs may apply. ptest cannot perfectly detect secrets in "
        "source. Excluded from review: secrets/private files, agent "
        "instructions/configuration, dependency environments, caches, "
        "coverage/build outputs, generated/minified files, and .ptest private "
        "runtime state. Use --offline for the static doctor instead."
    )
    if calls is not None:
        if isinstance(calls, bool) or not isinstance(calls, int) \
                or calls < 0:
            raise TypeError("calls must be a nonnegative int or None")
        if isinstance(concurrency, bool) or not isinstance(concurrency, int):
            raise TypeError("concurrency must be int")
        chosen = (render.terminal_text(str(model)[:128])
                  if model else "")
        if chosen:
            disclosure += (
                f" This review makes {calls} model calls "
                f"({concurrency} at a time) with model {chosen}.")
        else:
            disclosure += (
                f" This review makes {calls} model calls "
                f"({concurrency} at a time) with a model chosen after "
                "consent from the provider's model list (one extra call "
                "that sends only the model list).")
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


def _execution_facts(resolution: C.ConfigResolution) -> dict[str, dict]:
    """Map each project to its public executability fact for review children."""
    return {item.project: item.to_public()
            for item in executability.check_resolution(resolution)}


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
                    f"Bounded evidence omitted {packet.excluded_count} entries and "
                    f"truncated {packet.truncated_count} files or excerpts."),
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
                         execution: dict | None = None) -> dict:
    score = assessment.score
    child = {
        "project_id": assessment.project_id,
        "scope": assessment.scope,
        "packet_sha256": assessment.packet_sha256,
        "rows": [{
            "id": row.id, "status": row.status,
            "rationale": row.rationale, "label": row.label,
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
    return child


def _run_doctor_review(parsed: ParsedArgs, resolution: C.ConfigResolution,
                       domain: C.DomainPaths, *, adapter, interactive: bool,
                       preconsented: bool = False) -> bool:
    """Collect, per-item review, revalidate, then publish one review.

    Returns true for a declined offline result; all review paths fail
    closed. One focused model call runs per (project, checklist item);
    deterministically skipped items take no call.
    """
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
        detail = " | ".join((
            render.terminal_text(phase),
            "provider=" + render.terminal_text(provider),
            "project=" + render.terminal_text(project),
            f"elapsed={max(0, int(elapsed_s))}s",
        ))
        if sys.stderr.isatty():
            spinner = "|/-\\"[int(max(0, elapsed_s)) % 4]
            print(f"\r{spinner} doctor review: {detail}",
                  end="", file=sys.stderr, flush=True)
        else:
            print("doctor review: " + detail, file=sys.stderr, flush=True)
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
        plans = [agent_assessment.plan_item_reviews(packet)
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
                            time.monotonic() - started))
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
            assessments.append(agent_assessment.assemble_child(
                packet, reviews, tuple(replies)))
            ensure_deadline()

        reviewed_rows = [(review, row)
                         for reviews, assessment in zip(plans, assessments)
                         for review, row in zip(reviews, assessment.rows)
                         if review.request is not None]
        if reviewed_rows and all(row.rationale.startswith(
                agent_assessment.FAILED_PREFIX) for _, row in reviewed_rows):
            raise _problem("provider-failed",
                           "review provider did not return a valid assessment")

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

        facts = _execution_facts(resolution)
        child_data = []
        initialization_blocker = _initialization_required_limitation(resolution)
        for packet, assessment in zip(packets, assessments):
            child_limitations = _assessment_limitations((packet,))
            if initialization_blocker is not None and packet.declaration == ".":
                child_limitations.insert(0, dict(initialization_blocker))
            child_data.append(_child_assessment_data(
                packet, assessment, child_limitations,
                execution=facts.get(packet.declaration)))
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
        if parsed.assessment_json:
            sys.stdout.buffer.write(render.render_json(document))
        else:
            sys.stdout.write(render.render_agent_assessment(
                child_data, workspace, report_path=publication.path,
                publication_status=publication.status))
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
    if parsed.prompt:
        sys.stdout.write(render.repair_prompt(
            workspace.aggregate, workspace=workspace))
    elif parsed.json:
        sys.stdout.buffer.write(render.render_doctor_json(
            workspace.aggregate, domain=_domain_public(domain)))
    else:
        sys.stdout.write(render.render_doctor(
            workspace.aggregate, workspace=workspace))


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
            if parsed.json:
                sys.stdout.buffer.write(_document("init", payload))
            else:
                sys.stdout.write(init_render.render_init(
                    result, applied if applied is not None else plan,
                    dry_run=parsed.dry_run, agents=agents,
                    repo_name=root.name, color=sys.stdout.isatty()))
            if parsed.reveal_command:
                print("unredacted-command-disclosure: explicit preview requested",
                      file=sys.stderr)
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
            }
            if parsed.json:
                sys.stdout.buffer.write(_document("status", payload, domain=domain))
            else:
                print(f"queued: {len(payload['queued'])}\nactive: {len(payload['active'])}")
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
            if not (parsed.offline or parsed.json or parsed.prompt):
                declined = _run_review_entry(
                    parsed, resolution, domain,
                    interactive=_interactive_review(),
                )
                if declined:
                    return 0
                return 0
            _doctor_static_output(parsed, resolution, domain)
            return 0
    except C.Problem as problem:
        return _emit_error(
            problem, kind=command or "where",
            json_output=(parsed.json or
                         (command == "doctor" and parsed.assessment_json)),
        )
    raise _problem("invalid-config", "unknown command")


def _init_agents(parsed: ParsedArgs, *, json_output: bool = False) -> tuple[str, ...]:
    if parsed.agents_explicit or json_output or not sys.stdin.isatty():
        return parsed.agents
    print("Install repository-local ptest guidance for which agents? "
          "[none/claude,codex,opencode,gemini/all] (default: none):",
          file=sys.stderr)
    try:
        choice = input().strip().lower()
    except EOFError:
        return ()
    if not choice or choice == "none":
        return ()
    if choice == "all":
        return agent_rules.SUPPORTED_AGENTS
    names = tuple(part.strip() for part in choice.split(","))
    if (any(not name for name in names)
            or any(name not in agent_rules.SUPPORTED_AGENTS for name in names)):
        raise _problem("unsupported-capability", "agent integration is not supported")
    return tuple(dict.fromkeys(names))


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
        if resolution.monorepo is not None:
            from . import monorepo
            children = monorepo.preflight_children(resolution.root, resolution.monorepo)
            domain = platform.domain_paths(parsed.fixture_domain)
            if parsed.full:
                def run_full(child):
                    result = operations.execute(
                        domain, child.config,
                        C.RunRequest(mode=C.Mode.FULL, workers=parsed.workers,
                                     queue_timeout_s=parsed.queue_timeout_s,
                                     no_setup=parsed.no_setup,
                                     result_path=parsed.result_path,
                                     fixture_domain=parsed.fixture_domain),
                    )
                    for reason in result.reasons:
                        print(render.terminal_text(f"{reason.code}: {reason.message}"), file=sys.stderr)
                    return result.exit_code
                return monorepo.execute_full(children, run_full)
            routed = monorepo.route_scopes(parsed.runner_argv, children)
            result = operations.execute(
                domain, routed.target.config,
                C.RunRequest(mode=C.Mode.SCOPED, argv=routed.scopes,
                             workers=parsed.workers, queue_timeout_s=parsed.queue_timeout_s,
                             no_setup=parsed.no_setup,
                             shadow=parsed.shadow, result_path=parsed.result_path,
                             fixture_domain=parsed.fixture_domain),
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
        )
        result = operations.execute(domain, resolution.config, request)
        for reason in result.reasons:
            print(render.terminal_text(f"{reason.code}: {reason.message}"), file=sys.stderr)
        return result.exit_code
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
