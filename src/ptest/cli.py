"""Closed NG command-line grammar and static dispatch.

This slice owns parsing and inspection only.  Execution deliberately stops at
the typed capability boundary until the scheduler/guard orchestration lands.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from . import config as config_api
from . import contracts as C
from . import doctor, files, history, platform, scheduler
from . import render
from .runners import adapter_for


_INSPECTION = frozenset({
    "init", "register", "where", "status", "history", "plan",
    "doctor", "guide",
})
_EXECUTION_VALUE = frozenset({
    "--fixture-domain", "--base", "--workers", "--queue-timeout",
    "--result-json",
})
_EXECUTION_BOOL = frozenset({"--changed", "--full", "--no-setup", "--shadow"})


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


def _parse_execution(args: Sequence[str], *, command: str | None = None) -> ParsedArgs:
    mode = C.Mode.AUTOMATIC
    changed = command == "changed"
    full = False
    workers = None
    base = None
    fixture = None
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
        if token == "--fixture-domain":
            if fixture is not None:
                raise _problem("invalid-config", "option cannot be repeated")
            path = Path(value)
            if not path.is_absolute():
                raise _problem("unsafe-path", "fixture domain must be an absolute path")
            fixture = path
        elif token == "--base":
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
        command=command, mode=mode, runner_argv=tail, fixture_domain=fixture,
        base=base, workers=workers, queue_timeout_s=queue_timeout,
        no_setup=no_setup, shadow=shadow, result_path=result_path,
        changed=changed, full=full,
    )


def _parse_inspection(command: str, args: Sequence[str]) -> ParsedArgs:
    if command == "init":
        runner = None
        dry_run = reveal = False
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
            elif token == "--json":
                # Init's frozen grammar omits --json, but accepting it is
                # harmless only when it is explicitly requested by automation.
                pass
            else:
                raise _problem("invalid-config", "unknown inspection option")
            index += 1
        return ParsedArgs(command=command, runner=runner, dry_run=dry_run,
                          reveal_command=reveal, json="--json" in args)
    if command == "register":
        if any(token not in {"--json"} for token in args):
            raise _problem("invalid-config", "unknown inspection option")
        return ParsedArgs(command=command, json="--json" in args)
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
        scope = None
        limits: dict[str, int] = {}
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
            elif token == "--probe":
                raise _problem("unsupported-capability", "doctor probes are not qualified in this slice")
            else:
                raise _problem("invalid-config", "unknown inspection option")
            index += 1
        if json_output and prompt:
            raise _problem("invalid-config", "doctor output modes cannot be combined")
        return ParsedArgs(command=command, json=json_output, prompt=prompt,
                          scope=scope, **limits)
    if command == "guide":
        write = None
        index = 0
        while index < len(args):
            token = args[index]
            if token == "--write":
                write, index = _value(args, index, token)
                continue
            if token != "--json":
                raise _problem("invalid-config", "unknown inspection option")
            index += 1
        return ParsedArgs(command=command, json="--json" in args, write=write)
    raise _problem("invalid-config", "unknown command")


def parse_argv(argv: Sequence[str] | None = None) -> ParsedArgs:
    """Parse ptest's closed prefix grammar without inspecting runner tails."""
    if argv is None:
        argv = tuple(sys.argv[1:])
    args = tuple(argv)
    if any(not isinstance(token, str) for token in args):
        raise _problem("invalid-config", "arguments must be strings")
    if args[:1] == ("--fixture-domain",):
        value, index = _value(args, 0, "--fixture-domain")
        path = Path(value)
        if not path.is_absolute():
            raise _problem("unsafe-path", "fixture domain must be an absolute path")
        parsed = parse_argv(args[index:])
        return replace(parsed, fixture_domain=path)
    if args and args[0] in _INSPECTION:
        command = args[0]
        return _parse_inspection(command, args[1:])
    if args and args[0] == "changed":
        return _parse_execution(args[1:], command="changed")
    if args and args[0] in {"--help", "-h"}:
        return ParsedArgs(command="help")
    if args and args[0] == "--version":
        return ParsedArgs(command="version")
    # --fixture-domain is a leading global option and is consumed by the same
    # prefix parser as execution options.
    return _parse_execution(args)


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
    scoped = C.summarize_command(
        config.runner.kind, C.Mode.SCOPED,
        config.runner.launcher + config.runner.args,
        workers=config.runner.workers, provenance=("config",),
    )
    full = C.summarize_command(
        config.runner.kind, C.Mode.FULL,
        config.runner.launcher + config.runner.args + config.runner.full_args,
        workers=config.runner.workers, provenance=("config",),
    )
    return C.summarize_config(config, scoped=scoped, full=full)


def _where_payload(resolution: C.ConfigResolution, domain: C.DomainPaths | None,
                   *, reveal: bool = False) -> dict:
    config = resolution.config
    if config is None:
        return {
            "root": str(resolution.root), "config_path": None,
            "initialized": False, "runner_kind": None, "capability": None,
            "commands": [], "effective_limits": {"max_slots": None,
            "max_jobs": None, "memory_mb": None, "repo_workers": None},
            "provenance": list(resolution.provenance),
            "warnings": [_reason for _reason in []],
        }
    adapter_for(config.runner.kind)  # closed registry validation only
    capability = {
        "execution": C.ExecutionTier.UNAVAILABLE.value,
        "selection": False,
        "lifecycle": "cooperative-process-group",
        "limitations": [{
            "code": "unsupported-capability",
            "message": "execution orchestration is not qualified in this slice",
            "paths": [],
        }],
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
        print(str(problem), file=sys.stderr)
    return 2 if problem.code not in {"coordinator-unavailable", "queue-timeout"} else 75


def _static_dispatch(parsed: ParsedArgs, cwd: Path) -> int:
    command = parsed.command
    if command == "help":
        print("ptest [--changed|--full] [--workers N] [-- RUNNER_ARG ...]")
        return 0
    if command == "version":
        print(C.PTEST_VERSION)
        return 0
    if command == "guide":
        text = render.render_guide()
        if parsed.write is not None:
            try:
                files.create_exclusive(cwd, parsed.write, text.encode("utf-8"), private=False)
            except C.Problem as problem:
                return _emit_error(problem, kind="register", json_output=parsed.json)
        if parsed.json:
            # Guide has no public envelope kind; JSON mode is intentionally a
            # strict usage error rather than an invented schema.
            problem = _problem("invalid-config", "guide JSON output is unsupported")
            return _emit_error(problem, kind="register", json_output=True)
        sys.stdout.write(text)
        return 0
    if command == "init":
        try:
            result = config_api.init_project(cwd, C.InitOptions(
                runner=parsed.runner, dry_run=parsed.dry_run,
                reveal_command=parsed.reveal_command,
            ))
            payload = C.serialize_init_result(result)
            if parsed.json:
                sys.stdout.buffer.write(_document("init", payload))
            else:
                print(f"{result.action.value}: {result.target}")
            if parsed.reveal_command:
                print("unredacted-command-disclosure: explicit preview requested",
                      file=sys.stderr)
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
            payload = _where_payload(resolution, domain, reveal=parsed.reveal_command)
            if parsed.json:
                sys.stdout.buffer.write(_document("where", payload, domain=domain))
            else:
                print(f"root: {payload['root']}")
                print(f"initialized: {payload['initialized']}")
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
                        + json.dumps(list(command), ensure_ascii=False),
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
            domain = platform.domain_paths(parsed.fixture_domain)
            if resolution.config is None:
                raise resolution.problem or _problem("initialization-required", "project configuration is required")
            payload = history.read_history_payload(domain, _checkout(resolution.config), parsed.history_limit)
            if parsed.scope is not None:
                payload["summaries"] = [
                    item for item in payload["summaries"]
                    if item["run_id"] == parsed.scope
                    or any(parsed.scope == test.get("file") for test in [])
                ]
            if parsed.json:
                sys.stdout.buffer.write(_document("history", payload, domain=domain))
            else:
                print(f"history: {len(payload['summaries'])} runs")
            return 0
        if command == "doctor":
            domain = platform.domain_paths(parsed.fixture_domain)
            limits = C.ScanLimits(
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
            report = doctor.inspect(domain, resolution, limits, parsed.scope)
            if parsed.prompt:
                sys.stdout.write(render.repair_prompt(report))
            elif parsed.json:
                sys.stdout.buffer.write(render.render_doctor_json(report, domain=_domain_public(domain)))
            else:
                sys.stdout.write(render.render_doctor(report))
            return 0
    except C.Problem as problem:
        return _emit_error(problem, kind=command or "where", json_output=parsed.json)
    raise _problem("invalid-config", "unknown command")


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
    json_requested = _json_prefix_requested(raw_args)
    try:
        parsed = parse_argv(raw_args)
        if parsed.command in _INSPECTION or parsed.command in {"help", "version"}:
            return _static_dispatch(parsed, Path.cwd())
        # No subprocesses are launched in Slice 11A.  Keep this a typed
        # capability error so callers cannot mistake preparation for authority.
        raise _problem("unsupported-capability", "execution orchestration is not qualified in this slice")
    except C.Problem as problem:
        kind = _command_from_args(raw_args) or "run"
        return _emit_error(problem, kind=kind, json_output=json_requested)


def _json_prefix_requested(args: Sequence[str]) -> bool:
    """Recognize --json only in an inspection command's closed option list."""
    values = tuple(args)
    if values[:1] == ("--fixture-domain",):
        values = values[2:]
    if not values or values[0] not in _INSPECTION:
        return False
    allowed = {
        "init": {"--json", "--dry-run", "--reveal-command", "--runner"},
        "register": {"--json"},
        "where": {"--json", "--reveal-command"},
        "status": {"--json"},
        "history": {"--json", "--limit"},
        "plan": {"--json", "--base"},
        "doctor": {"--json", "--prompt", "--scope", "--max-entries",
                    "--max-files", "--max-file-bytes", "--max-total-bytes"},
        "guide": {"--json", "--write"},
    }[values[0]]
    for token in values[1:]:
        if token == "--json":
            return True
        if token.startswith("-") and token not in allowed:
            return False
    return False


def _command_from_args(args: Sequence[str]) -> str | None:
    values = tuple(args)
    if values[:1] == ("--fixture-domain",):
        values = values[2:]
    return values[0] if values[:1] and values[0] in _INSPECTION else None


__all__ = ["ParsedArgs", "parse_argv", "main"]
