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
  ptest --changed                 # default loop: only what the change touches
  ptest --full                    # integrated gate once the change lands
  ptest -- -k slow                # literal runner tail, standalone v1 only (see ptest help run)

Inspect and review:
  ptest where                     # checkout identity and runner summary
  ptest status                    # queued/active leases and limits
  ptest plan                      # full-execution preview; single-project v1 only
  ptest history                   # committed run summaries; single-project v1 only
  ptest register                  # registration preview
  ptest doctor                    # consented CLI review; use --offline for static-only
  ptest guide                     # render bundled repair guide text
  ptest rules                     # preview agent guidance
  ptest uninstall                 # remove what ptest set up (see ptest help uninstall)

Local state:
  PTEST_STATE_DIR=/absolute/path  # config, coordination, history, review cache
  Parent must exist and be owned/non-writable by others; existing state must be 0700.
  Use one value across projects to share limits. Explicit --fixture-domain wins.

Machine output:
  --json on init/register/where/status/history/plan/doctor; guide is text-only.

Discover commands:
  ptest help <topic>              # init register where status history plan doctor guide rules run agents uninstall
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
             [--doctor | --no-doctor]
             [--reviewer auto|claude|codex|opencode]
             [--allow-model-review] [--review-timeout SECONDS]
             [--review-model MODEL] [--review-concurrency 1..8]
             [--smoke | --no-smoke]
             [--changed-setup now|later|no]

Notes:
  --dry-run previews without writing. --json is non-interactive (never
  prompts) and emits the init document. Without --agents/--json on a TTY,
  init asks which agents to install guidance for (default: all). --child pairs declare
  monorepo children; each --child requires a following --runner. Omit
  --runner to autodetect (including multi-child monorepos). command is not
  a usable init --runner choice: it requires an explicit pre-authored
  configuration. --agents installs guidance only and is separate from
  model-review consent. After successful initialization, --doctor requests
  the review offer and --no-doctor suppresses it. Review flags cannot be
  combined with --json or --dry-run. --review-model (or PTEST_REVIEW_MODEL)
  overrides the cheap-model choice; --review-concurrency 1..8 (default 4)
  bounds parallel model calls. After init, --smoke runs one small real
  test per project through the scoped runner, --no-smoke skips it, and a
  TTY asks once naming the files. --dry-run and --json never run smoke;
  a smoke failure keeps the written config and the exit status. Declared
  setup never runs silently: a TTY is asked once per project before its
  smoke test, and non-interactive smoke skips with the setup command.
  After the smoke step, init offers ptest --changed setup once per
  pytest project whose environment holds the frozen pytest-cov/coverage
  pair: now writes --cov/--cov-report plus the [selection] draft and
  runs ptest --full for the baseline, later writes the same draft for
  a later baseline, no leaves selection off. --changed-setup
  now|later|no answers non-interactively (default: later); existing
  configs are never rewritten (see ptest doctor --fix)."""

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

_DOCTOR = """ptest doctor: consented bounded CLI review by default, offline static inspection, or an explicit live probe.

Review syntax (default mode):
  ptest doctor [--reviewer auto|claude|codex|opencode]
               [--allow-model-review] [--json]
               [--review-timeout 10..900] [--scope PATH]
               [--review-model MODEL] [--review-concurrency 1..8]

Offline static syntax (never launches a provider or writes a report):
  ptest doctor --offline [--json] [--scope PATH] [--max-entries N]
               [--max-files N] [--max-file-bytes N] [--max-total-bytes N]

Fix syntax (never runs a model review; static plan plus guarded write):
  ptest doctor --fix [--offline] [--dry-run]

Probe syntax (EXECUTES tests and setup; not a static inspection; single-project v1 only):
  ptest doctor --probe --scope S [--repeat 1..5 (default 2)]
               [--workers 1..64 (default 2)]
               [--attempt-timeout 1..120 (default 30)]
               [--no-setup] [--result-json PATH]

Notes:
  Without an explicit concrete --reviewer, default review on a TTY lists the
  qualified installed reviewers in a stable order: one is used directly, while
  two or more are offered once by number (an empty, invalid, out-of-range, or
  EOF answer declines the review). It then discloses the
  selected provider and asks for invocation-local consent. It sends bounded
  source text using your existing provider account; provider or account costs
  may apply, and ptest cannot perfectly detect secrets. Declining the offer
  runs offline static output.
  In automation, provide both --reviewer PROVIDER and --allow-model-review;
  --reviewer auto never selects a provider without an interactive TTY.

  Claude and Codex are qualified reviewers. OpenCode is not supported
  because its free tier refuses tool-free runs (HTTP 403 FreeTierError).
  Selecting an unqualified provider, or auto finding only unqualified
  providers, fails closed with provider-unqualified. Normal test execution
  remains local and model-free.
  --json emits the versioned review document. Offline --json emits the
  same document kind built from static facts only: items ptest cannot
  decide offline are unknown with a reason.

  --fix diffs each `.ptest.toml` against the config ptest would write
  today plus deterministic fixes (stale `-n 0`, setup extras/groups,
  a `[selection]` draft under coverage) and applies the diff directly:
  the --fix flag itself is the consent, and --dry-run shows the diff
  only. Writes are atomic, never follow
  symlinks, fail closed on concurrent edits, keep unmanaged settings
  byte-identical, and are idempotent. Model-review findings about test
  code are never applied.

  Each review makes one model call per checklist item that needs one
  (4 at a time by default; --review-concurrency 1..8 bounds
  parallelism), skipping items that do not apply without a call.
  Timing, selection and parallel execution items are answered from
  ptest's own facts with no model call. The model is the
  cheapest adequate one: --review-model (or PTEST_REVIEW_MODEL) wins,
  otherwise claude uses its haiku alias and codex picks from its model
  list with one extra call that sends only the model list; the choice is
  cached per provider and CLI version. The tool-denial qualification must
  be re-run when the chosen model changes. Citations are in
  recommendations.md.

  Full model review disclosure: the selected provider may receive bounded
  source text from the reviewed project using your existing account.
  Provider or account costs may apply. ptest cannot perfectly detect
  secrets in source. Excluded from review: secrets/private files, agent
  instructions/configuration, dependency environments, caches,
  coverage/build outputs, generated/minified files, and .ptest private
  runtime state. Use --offline for the static doctor instead.

  Parallel pytest: a pytest project whose checked-in config enables xdist
  runs one worker per granted slot (``-n N`` requests N slots; ``-n auto``
  requests the machine's slot count; ``ptest --workers W`` caps it). The
  scheduler grants what is free and the run serializes with ``-n 0`` when
  only one slot is granted or xdist cannot be verified; ``-n 0`` in
  [runner] args opts out of parallel runs. Coverage (--cov) runs in
  parallel under xdist when the project environment holds the qualified
  pytest-cov/coverage pair, and serially otherwise.

  --probe requires --scope and a single-project v1 configuration, cannot
  combine output modes or static scan limits, and probe options require
  --probe. A root monorepo supports static doctor, not live --probe. Probing
  executes configured setup/tests, may use configured services/network, and
  requires deliberate authorization with safe isolation. Never infer
  readiness from an unknown or incomplete static scan: a clean or truncated
  scan is never a pass."""

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

_UNINSTALL = """ptest uninstall: remove what ptest set up, keeping your work.

Syntax:
  ptest uninstall [--yes] [--dry-run] [--json] [--self]

Removes the repository `.ptest.toml` files (root and declared monorepo
children), ptest-managed guidance (docs/ptest-agent.md, the managed block
in AGENTS.md/CLAUDE.md, managed provider skills), the un-edited
recommendations.md report, and this checkout's private state (its history,
setup records, and scheduler rows). Files you edited are kept and reported
as kept (edited); symlinks, unbalanced marker blocks, and anything outside
the repository root are never touched and reported as skipped.

The full plan prints first, grouped by action (remove, kept (edited),
skipped). Skipped entries are informational and exit 0; only refusals
(active run, missing --yes, failures) exit non-zero. On a TTY, uninstall
asks once: `Remove these? [y/N]`. Non-interactive runs require --yes;
without it the plan prints and the command exits non-zero. --dry-run
prints the plan and changes nothing. --json emits the versioned
uninstall document; without --yes it prints the plan document with
applied=false and exits non-zero without prompting. A second run
reports nothing to remove and exits 0. --self also removes the local
ptest installation (default ~/.local/ptest) when it looks like an
install.sh layout: only installer-created entries go (the bundle dirs,
the installer symlink, leftover temp links), plus a PATH ptest symlink
only when it resolves into that root. Anything else inside the root is
kept and reported, and the root itself stays when it is not empty.
--self works outside any repository. Run uninstall with the same
PTEST_STATE_DIR used for runs, so it inspects the same machine-state
location; the plan names that location as `state: ...`, with
(PTEST_STATE_DIR) appended when the variable selected it."""

_RUN = """Running tests: scoped iteration and the integrated full gate, from the repository root.

Syntax (ptest options precede scoped/native arguments):
  ptest [--workers 1..64] [-v | --verbose] [-q | --quiet] [<scoped paths>...]
  ptest --changed | --full [--workers 1..64] [-v] [-q]
  ptest [--queue-timeout 1..86400 (default 1800)] [--base X]
        [--timeout 1..86400 (default: from history, else 600)]
        [--no-setup] [--shadow] [--result-json PATH] [-- SCOPES...]
  e.g. ptest --workers 2 tests/test_example.py  # only with verified isolation and adapter support

Notes:
  Standalone v1 only: the scoped form passes runner arguments literally;
  everything from the first native token (or --) reaches the runner
  untouched, e.g. ptest -- -k slow. An explicit -- still preserves
  parsing. -v/--verbose and -q/--quiet are ptest options only before
  the scope: ptest -v tests/a.py is ptest-verbose, while
  ptest tests/a.py -v leaves -v as runner data (pytest verbose only).
  From a monorepo root, scoped paths must be child-prefixed
  paths selecting exactly one child (e.g. api/tests/test_example.py);
  arbitrary runner flags are rejected by root scope validation. --changed
  and --full are mutually exclusive; both reject runner narrowing. --base
  is unavailable with --full. --shadow requires automatic mode. Root
  --full preflights all children, then runs them sequentially with output
  preserved, returning the first nonzero exit after all children finish.
  Exit status mirrors the outcome: 0 passes, nonzero fails; only --full
  completes the change, a scoped green is iteration.

Run output (stderr; runner stdout/stderr stay untouched):
  ptest prints short ptest:-prefixed status lines: a start line naming
  project, runner, workers and scope; a waiting line when admission
  queues (with the active runs, the limit, and the holders when known);
  setup lines naming the setup command and its state; and an end line
  with ptest's own verdict, bridge counts, and duration (one per child
  plus a total for root --full). Refusals keep their code: message line.
  A failure or refusal names ptest -v once per run; a clean pass never
  does. -v adds detail lines (admission and grant, queue position, setup
  state and command, the runner command, per-phase timings, the scope
  decision) and also forwards -v to pytest/vitest. -q suppresses ptest's
  own status lines; errors and refusals still print. --json and
  --result-json documents are unchanged; status lines never go to
  stdout."""

_AGENTS = """Agent workflow (normal test execution needs no model APIs or extra runtime).

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

3. Loop changed, then gate full:
     ptest --changed               # default loop after each edit
     ptest api/tests/test_example.py  # monorepo; standalone example in step 2
     ptest --full                  # once, after the change is integrated
   A scoped or changed green is iteration only; only --full completes the
   change. The first --changed may run everything to record a baseline.
   Concurrency (e.g. --workers N) requires verified isolation and adapter
   support. Standalone v1 passes runner arguments literally: everything
   from the first native token or -- passes through untouched. Exit status
   mirrors the outcome: 0 passes, nonzero fails, and root --full keeps the
   first child failure after all children finish.

4. Review with consent or inspect offline:
     ptest doctor                  # separately consented source review on a TTY
     ptest doctor --offline        # static scan: hypotheses, read-only
     ptest doctor --json           # versioned review document
     ptest doctor --offline --json # same document from static facts only
   Doctor review is separately consented and may send bounded source text to
   the selected provider; costs may apply. Claude and Codex are qualified
   reviewers; OpenCode is not supported because its free tier refuses
   tool-free runs. Use --offline for static output. Never infer readiness
   from an unknown or incomplete static scan: report unknown honestly;
   a clean or truncated scan is never a pass
   and never proves parallel, timing, or execution readiness.
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
    "uninstall": _UNINSTALL,
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
