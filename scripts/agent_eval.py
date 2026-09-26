#!/usr/bin/env python3
"""Repeatable eval: can LLMs use ptest from its guidance?

Builds a scratch monorepo in a temp dir from the CURRENT package resources
(the guide plus skill templates via ``ptest.agent_rules`` -- the same bytes
``ptest init`` installs), writes the 13-scenario planning prompt, runs each
subject command given on the CLI, extracts the JSON answers, scores them
against ``evals/agent-usage/scenarios.toml`` and prints a compact table.

Subject templates substitute ``{prompt}``, ``{prompt_file}`` and ``{repo}``,
for example::

    claude -p --model haiku {prompt}
    muse-stable exec --workspace {repo} --prompt-file {prompt_file}

Templates are split with ``shlex`` and run WITHOUT a shell. Canned answer
files can be scored without any model via ``--answers-file NAME=PATH``.
``--dry-run`` prints the prompt and repo path without calling any model.

Exit status is 0 only if every subject passes every scenario.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve()
_CHECKOUT = _HERE.parent.parent  # ptest checkout: evals/ + src/ live here
sys.path.insert(0, str(_CHECKOUT / "src"))
from ptest import agent_rules  # noqa: E402  (current checkout, same bytes init installs)

DEFAULT_SCENARIOS = _CHECKOUT / "evals" / "agent-usage" / "scenarios.toml"
SCENARIO_IDS = [f"S{i}" for i in range(1, 14)]

# Runner-misuse that is wrong in every scenario (scenario-local forbids such
# as --full or cd where not expected live in scenarios.toml).
GLOBAL_FORBID = [
    r"pytest",
    r"vitest",
    r"npm\s+test",
    r"(?<![\w-])npm(?![\w-])",
    r"go\s+test",
    r"cargo\s+test",
    r"git\s+commit",
]

PROMPT_HEADER = (
    "You are a coding agent working in the repository at {repo}. Before answering,\n"
    "read this repository's agent instructions exactly as you normally would when starting work here "
    "(AGENTS.md/CLAUDE.md, the ptest skill under .claude/skills or .agents/skills, and anything they "
    "point to). Use ONLY this repository's files — no other knowledge about ptest.\n"
    "IMPORTANT: this is a planning exercise. Do NOT execute any test command or ptest itself, and do not "
    "modify files. For each scenario answer: the exact command(s) you would run next (or \"none\"), and in "
    "one sentence what you would do/tell the user.\n"
    "Print ONLY a JSON object of the form {{\"S1\": {{\"commands\": [\"...\"], \"action\": \"...\"}}, ...}} "
    "as your final message, with all 13 scenario ids S1..S13."
)

BILLING_PY = "def charge(amount):\n    return round(amount * 1.19, 2)\n"
TEST_BILLING_PY = (
    "from app.billing import charge\n\n"
    "def test_charge():\n"
    "    assert charge(100) == 119.0\n"
)
AGENT_POINTER = "# Agent instructions\n\nSee docs/ptest-agent.md for testing.\n"


def build_scratch_repo() -> Path:
    """Create the scratch monorepo; guidance bytes come from agent_rules."""
    repo = Path(tempfile.mkdtemp(prefix="ptest-agent-eval-"))
    (repo / "api" / "app").mkdir(parents=True)
    (repo / "api" / "tests").mkdir(parents=True)
    (repo / "web" / "src").mkdir(parents=True)
    (repo / "api" / "app" / "billing.py").write_text(BILLING_PY, encoding="utf-8")
    (repo / "api" / "tests" / "test_billing.py").write_text(
        TEST_BILLING_PY, encoding="utf-8")
    (repo / "web" / "src" / "app.test.js").write_text(
        "import { test, expect } from 'vitest';\n"
        "test('placeholder', () => { expect(1).toBe(1); });\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text(AGENT_POINTER, encoding="utf-8")
    (repo / "CLAUDE.md").write_text(AGENT_POINTER, encoding="utf-8")
    agent_rules.apply(repo, agents=("claude",))
    return repo


def build_prompt(scenarios: dict, repo: Path) -> str:
    lines = [PROMPT_HEADER.format(repo=str(repo)), ""]
    for sid in SCENARIO_IDS:
        text = scenarios[sid]["prompt"].replace("<repo>", str(repo))
        lines.append(f"{sid}. {text}")
    return "\n".join(lines) + "\n"


def load_scenarios(path: Path) -> dict:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    scenarios = data.get("scenarios", {})
    missing = [sid for sid in SCENARIO_IDS if sid not in scenarios]
    if missing:
        raise ValueError(f"{path} is missing scenarios: {', '.join(missing)}")
    return scenarios


def normalize_commands(raw) -> list[str] | None:
    """Effective commands; 'none' entries (any case) mean no command."""
    if not isinstance(raw, list):
        return None
    out = []
    for item in raw:
        text = str(item).strip()
        if text.lower() == "none" or not text:
            continue
        out.append(text)
    return out


@dataclass
class ScenarioResult:
    scenario: str
    passed: bool
    reason: str = ""


def score_answer(scenarios: dict, sid: str, entry) -> ScenarioResult:
    spec = scenarios[sid]
    if not isinstance(entry, dict):
        return ScenarioResult(sid, False, "answer is not an object")
    commands = normalize_commands(entry.get("commands"))
    if commands is None:
        return ScenarioResult(sid, False, "commands is not a list")
    action = entry.get("action", "")
    action_text = action if isinstance(action, str) else str(action)

    for pattern in list(GLOBAL_FORBID) + list(spec.get("forbid", [])):
        hit = next((c for c in commands if re.search(pattern, c)), None)
        if hit is not None:
            return ScenarioResult(
                sid, False,
                f"command {hit!r} matches forbidden {pattern!r}")

    expected = list(spec.get("expect_commands", []))
    if spec.get("expect_none", False):
        if commands:
            return ScenarioResult(
                sid, False,
                f"expected no command, got {commands!r}")
    elif not commands:
        if expected and not spec.get("no_command_ok", False):
            return ScenarioResult(
                sid, False,
                f"expected one of {expected!r} but gave no command")
    elif expected and not any(
            re.search(p, c) for p in expected for c in commands):
        return ScenarioResult(
            sid, False,
            f"commands {commands!r} match none of {expected!r}")

    for pattern in spec.get("action_all", []):
        if not re.search(pattern, action_text, re.IGNORECASE):
            return ScenarioResult(
                sid, False,
                f"action does not mention {pattern!r}")
    any_of = list(spec.get("action_any", []))
    if any_of and not any(
            re.search(p, action_text, re.IGNORECASE) for p in any_of):
        return ScenarioResult(
            sid, False,
            f"action mentions none of {any_of!r}")
    return ScenarioResult(sid, True)


def score_answers(scenarios: dict, answers: dict) -> list[ScenarioResult]:
    return [score_answer(scenarios, sid, answers.get(sid))
            for sid in SCENARIO_IDS]


def extract_json(text: str) -> dict:
    for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S):
        try:
            obj = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    decoder = json.JSONDecoder()
    best = None
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            best = obj
    if best is None:
        raise ValueError("no JSON object found in subject output")
    return best


def run_subject(template: str, prompt: str, prompt_file: Path, repo: Path,
                timeout: int) -> tuple[str, dict | None]:
    argv = [token.replace("{prompt_file}", str(prompt_file))
            .replace("{repo}", str(repo)).replace("{prompt}", prompt)
            for token in shlex.split(template)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"subject command failed: {exc}", None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else "no output"
        return f"subject exited {proc.returncode}: {tail}", None
    try:
        return "", extract_json(proc.stdout)
    except ValueError as exc:
        return str(exc), None


@dataclass
class SubjectReport:
    name: str
    results: list[ScenarioResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(r.passed for r in self.results)


def print_table(reports: list[SubjectReport]) -> None:
    width = max([len(r.name) for r in reports] + [7])
    header = f"{'subject':<{width}} " + " ".join(f"{s:>3}" for s in SCENARIO_IDS) + "  total"
    print(header)
    for report in reports:
        cells = " ".join("  ✓" if r.passed else "  ✗" for r in report.results)
        total = f"{sum(r.passed for r in report.results)}/{len(report.results)}"
        print(f"{report.name:<{width}} {cells}  {total}")
    failed = [(r.name, res) for r in reports for res in r.results
              if not res.passed]
    if failed:
        print()
        for name, res in failed:
            print(f"{name} {res.scenario}: {res.reason}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Eval whether LLMs use ptest from its guidance.")
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument("--timeout", type=int, default=300,
                        help="per-subject seconds (default 300)")
    parser.add_argument("--subject", action="append", default=[],
                        metavar="NAME=TEMPLATE",
                        help="subject command template (repeatable)")
    parser.add_argument("--answers-file", action="append", default=[],
                        metavar="NAME=PATH",
                        help="score a canned answers JSON without a model")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the prompt and repo path, call no model")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    scenarios = load_scenarios(args.scenarios)
    repo = build_scratch_repo()
    prompt = build_prompt(scenarios, repo)
    if args.dry_run:
        print(f"repo: {repo}")
        print(prompt)
        return 0

    prompt_file = repo / "eval-prompt.md"
    prompt_file.write_text(prompt, encoding="utf-8")
    reports: list[SubjectReport] = []
    for spec in args.subject:
        name, _, template = spec.partition("=")
        if not name or not template:
            print(f"bad --subject {spec!r}: want NAME=TEMPLATE",
                  file=sys.stderr)
            return 2
        reason, answers = run_subject(template, prompt, prompt_file, repo,
                                      args.timeout)
        if answers is None:
            reports.append(SubjectReport(
                name, [ScenarioResult(s, False, reason) for s in SCENARIO_IDS]))
        else:
            reports.append(SubjectReport(name, score_answers(scenarios, answers)))
    for spec in args.answers_file:
        name, _, path = spec.partition("=")
        if not name or not path:
            print(f"bad --answers-file {spec!r}: want NAME=PATH",
                  file=sys.stderr)
            return 2
        try:
            answers = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            reports.append(SubjectReport(
                name, [ScenarioResult(s, False, f"canned file: {exc}")
                       for s in SCENARIO_IDS]))
            continue
        reports.append(SubjectReport(name, score_answers(scenarios, answers)))
    if not reports:
        print("nothing to score: pass --subject, --answers-file or --dry-run",
              file=sys.stderr)
        return 2
    print_table(reports)
    return 0 if all(r.passed for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
