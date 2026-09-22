"""Single source of truth for static task-oriented help text.

Pure string constants only: no config inspection, no filesystem writes, no
subprocess, no network, no model APIs. Every ``ptest help`` route renders
from here so the overview, ``ptest help <topic>``, and
``<command> --help`` can never drift apart.
"""
from __future__ import annotations

_OVERVIEW = """ptest: local-first test coordinator. No cloud account, model API, or remote service required.

Getting started:
  ptest init [--runner pytest|vitest|go|cargo] [--agents codex,claude] [--dry-run]
  ptest help agents               # self-contained agent workflow

Running tests from the repository root:
  ptest tests/test_example.py     # scoped: smallest relevant scope
  ptest --full                    # integrated gate once the change lands
  ptest -- -k slow                # literal runner tail, standalone v1 only (see ptest help run)

Inspect (static, read-only unless noted):
  ptest where                     # checkout identity and runner summary
  ptest status                    # queued/active leases and limits
  ptest plan                      # full-execution preview; single-project v1 only
  ptest history                   # committed run summaries; single-project v1 only
  ptest register                  # registration preview
  ptest doctor                    # static scan; --probe EXECUTES tests/setup (probe: single-project v1 only)
  ptest guide                     # render bundled repair guide text
  ptest rules                     # preview agent guidance

Machine output:
  --json on init/register/where/status/history/plan/doctor; guide is text-only.

Discover commands:
  ptest help <topic>              # init register where status history plan doctor guide rules run agents
  ptest <inspection-command> --help  # e.g. ptest doctor --help
  ptest --help | -h               # this overview

Examples:
  ptest init --agents codex,claude --json
  ptest tests/test_example.py
  ptest --full
  ptest doctor"""

_INIT = """ptest init: create .ptest.toml after bounded validation. Writes config (and guidance unless --dry-run).

Syntax:
  ptest init [--runner pytest|vitest|go|cargo]
             [--child NAME --runner KIND]...
             [--agents none|all|claude,codex,opencode,gemini]
             [--dry-run] [--reveal-command] [--json]

Notes:
  --dry-run previews without writing. --json is non-interactive (never
  prompts) and emits the init document. Without --agents/--json on a TTY,
  init asks which agents to install guidance for. --child pairs declare
  monorepo children; each --child requires a following --runner. Omit
  --runner to autodetect (including multi-child monorepos). command is not
  a usable init --runner choice: it requires an explicit pre-authored
  configuration."""

_REGISTER = """ptest register: static registration preview. Read-only, never writes.

Syntax:
  ptest register [--json]

Reports initialized versus preview, the proposed runner, and required actions."""

_WHERE = """ptest where: static checkout identity, runner capability, and command summary. Read-only.

Syntax:
  ptest where [--json] [--reveal-command]

--reveal-command prints the literal launcher command to stderr only, labelled
unredacted-command-disclosure; it is never persisted and never appears on stdout."""

_STATUS = """ptest status: queued/active lease counts and effective limits. Read-only.

Syntax:
  ptest status [--json]"""

_HISTORY = """ptest history: committed run summaries and obligations. Read-only; requires a single-project v1 configuration.

Syntax:
  ptest history [--json] [--limit 1..200 (default 20)]

Unavailable on a root v2 dispatcher. Test/file filters are not supported
in this slice; a filter argument fails closed instead of reporting an
empty result."""

_PLAN = """ptest plan: static full-execution preview. Read-only; requires a single-project v1 configuration.

Syntax:
  ptest plan [--json] [--base VALUE]

Static preview only: unavailable on a root v2 dispatcher. Selection stays
disabled pending a verified execution identity."""

_DOCTOR = """ptest doctor: bounded static scan (hypotheses, read-only) or a live probe (executes).

Static syntax (default; never executes, never writes):
  ptest doctor [--json | --prompt] [--scope PATH]
               [--max-entries N] [--max-files N]
               [--max-file-bytes N] [--max-total-bytes N]

Probe syntax (EXECUTES tests and setup; not a static inspection; single-project v1 only):
  ptest doctor --probe --scope S [--repeat 1..5 (default 2)]
               [--workers 1..64 (default 2)]
               [--attempt-timeout 1..120 (default 30)]
               [--no-setup] [--result-json PATH]

Notes:
  --json and --prompt are mutually exclusive output modes. --prompt grants
  assessment text only, never repair authority. The probe requires --scope
  and a single-project v1 configuration, cannot combine output modes or
  static scan limits, and probe options require --probe. A root monorepo
  supports static doctor, not live --probe. Probing executes configured
  setup/tests, may use configured services/network, and requires
  deliberate authorization with safe isolation. Never infer readiness from
  an unknown or incomplete static scan: a clean or truncated scan is never
  a pass."""

_GUIDE = """ptest guide: print the bundled local repair guide. Read-only except for --write.

Syntax:
  ptest guide [--write PATH]

Text only: guide accepts no --json. --write exports to one exclusive new
file resolved from the project root; existing files are never overwritten."""

_RULES = """ptest rules: preview (default) or install repository-local agent guidance.

Syntax:
  ptest rules [--apply]

Without --apply nothing is written. --apply adds references to existing
instruction files, creating AGENTS.md only if none exist (plus
docs/ptest-agent.md); it does not install per-agent skills. Use
init --agents for provider skills; failures roll back guidance writes.
Rules accepts no --json."""

_RUN = """Running tests: scoped iteration and the integrated full gate, from the repository root.

Syntax (ptest options precede scoped/native arguments):
  ptest [--workers 1..64] [<scoped paths>...]
  ptest --changed | --full [--workers 1..64]
  ptest [--queue-timeout 1..86400 (default 1800)] [--base X]
        [--no-setup] [--shadow] [--result-json PATH] [-- SCOPES...]
  e.g. ptest --workers 2 tests/test_example.py  # only with verified isolation and adapter support

Notes:
  Standalone v1 only: the scoped form passes runner arguments literally;
  everything from the first native token (or --) reaches the runner
  untouched, e.g. ptest -- -k slow. An explicit -- still preserves
  parsing. From a monorepo root, scoped paths must be child-prefixed
  paths selecting exactly one child (e.g. api/tests/test_example.py);
  arbitrary runner flags are rejected by root scope validation. --changed
  and --full are mutually exclusive; both reject runner narrowing. --base
  is unavailable with --full. --shadow requires automatic mode. Root
  --full preflights all children, then runs them sequentially with output
  preserved, returning the first nonzero exit after all children finish.
  Exit status mirrors the outcome: 0 passes, nonzero fails; only --full
  completes the change, a scoped green is iteration."""

_AGENTS = """Agent workflow (self-contained; no model APIs or extra runtime required).

1. Set up once, noninteractively, from the repository root (monorepo root
   when applicable):
     ptest init --agents codex,claude --json
   --agents accepts none|all|claude,codex,opencode,gemini (comma-separated).
   --json never prompts and emits the typed init document. Omitting
   --runner autodetects, including multi-child monorepos; for an explicit
   monorepo use --child NAME --runner KIND pairs.

2. Always run from the repository root (monorepo root when applicable;
   the directory holding the root .ptest.toml). Prefix scopes with the
   owning child; never copy or merge child configs.
   Standalone: ptest tests/test_example.py
   Monorepo:   ptest api/tests/test_example.py
   From a monorepo root, scoped paths must be child-prefixed scopes for
   exactly one child; arbitrary runner flags are rejected by root scope
   validation.

3. Iterate scoped, then gate full:
     ptest api/tests/test_example.py  # monorepo; standalone example in step 2
     ptest --full                  # once, after the change is integrated
   A scoped green is iteration only; only --full completes the change.
   Concurrency (e.g. --workers N) requires verified isolation and adapter
   support. Standalone v1 passes runner arguments literally: everything
   from the first native token or -- passes through untouched. Exit status
   mirrors the outcome: 0 passes, nonzero fails, and root --full keeps the
   first child failure after all children finish.

4. Diagnose statically first:
     ptest doctor                  # static scan: hypotheses, read-only
     ptest doctor --prompt         # assessment text for repair planning only
     ptest doctor --json           # typed findings document
   --prompt and --json are mutually exclusive. Never infer readiness from an
   unknown or incomplete static scan: report unknown honestly; a clean or
   truncated scan is never a pass and never proves parallel, timing, or
   execution readiness.
   ptest doctor --probe --scope tests/test_example.py EXECUTES tests and
   setup (may use configured services/network): requires deliberate
   authorization with safe isolation; use it only to reproduce, never to
   inspect. history, plan, and doctor --probe require single-project v1;
   a root monorepo supports static doctor, not live --probe. That gap is
   a known limitation.

5. Guidance and isolation:
     ptest guide
   Findings are hypotheses; verify callers before repairing. Use factories,
   keep one owned database per worker per run, namespace caches by run and
   worker, keep tests/coverage/assertions, and never global-flush,
   blanket-drop, or use fixed paths, fixed ports, detached processes, live
   network targets, or wall-clock sleeps."""

_TOPIC_TEXTS = {
    "init": _INIT,
    "register": _REGISTER,
    "where": _WHERE,
    "status": _STATUS,
    "history": _HISTORY,
    "plan": _PLAN,
    "doctor": _DOCTOR,
    "guide": _GUIDE,
    "rules": _RULES,
    "run": _RUN,
    "agents": _AGENTS,
}

TOPICS = tuple(_TOPIC_TEXTS)

HINT = "topics: " + " ".join(TOPICS)


def overview() -> str:
    """Return the task-oriented help overview."""
    return _OVERVIEW


def topic(name: str) -> str | None:
    """Return the help text for a known topic, else None (fail closed)."""
    return _TOPIC_TEXTS.get(name)


__all__ = ["TOPICS", "HINT", "overview", "topic"]
