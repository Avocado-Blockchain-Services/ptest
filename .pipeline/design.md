# Design: ptest test policy (Tier-1 guidance, doctor Test policy facts, opt-in policy)

Date: 2026-10-08. Author: architect. Branch: feature/test-policy (base bbf1260, 0.5.2).
Worktree: /home/ingmar/worktrees/ptest/cc-test-policy/ptest.
Authoritative request: the workflow task text (parts A, B, C, out-of-scope list, rules).
Precedent: src/ptest/agent_rules.py (bundled resource bytes, previous-version hash set,
fd-based `_create_leaf`/`_replace`, `_rollback`, 256 KiB bound, user-edited copy is never
overwritten). This document fixes decisions, the barrier, frozen interfaces, ownership,
acceptance criteria and tests. Where it is silent, the task text governs.

## 0. Barrier (applied to the chain base BEFORE T1-T3 branch)

Section 12 holds exact edits to existing files. A transcription step applies them to the
chain branch and commits them before the task worktrees branch. Every task depends on
them. No task re-applies, edits, reverts or reformats a barrier hunk. If a task finds the
barrier missing (check: `grep -n "def coverage_instruction_lines" src/ptest/agent_rules.py`
and `grep -n "def _test_policy_outputs" src/ptest/cli.py` both hit) or defective, it stops
and reports BLOCKED with the exact problem. It never patches a barrier hunk locally.

Why a barrier is needed (each item would otherwise make tasks silently sequential or red):

| Barrier item | Without it |
|---|---|
| B1 new guide bytes (`## Writing tests`) in both guide copies | T1 changes the guide; T3's tests pin `current not in _PREVIOUS_GUIDE_SHA256S` and the line cap. Splitting guide and hash across tasks makes one of them red. |
| B2 `_PREVIOUS_GUIDE_SHA256S` gains bbf1260 | same as above |
| B3 line cap 100 -> 109 in three existing tests; `test_init_changed.py:95` "coverage" ban scoped to outside `## Writing tests` | T1's longer guide, and the word "coverage" in its new section, fail tests no task owns |
| B4 `agent_rules.coverage_instruction_lines` + `TEST_POLICY_PATH` | doctor (T2) and the opt-in install (T3) both list conflicting instruction lines; one scanner, one owner of record |
| B5 `checklist.recipe_names()` | T3's `ptest guide <topic>` validates topics against T1's recipe table |
| B6 cli.py `_test_policy_outputs` + doctor call sites | doctor wiring lives in cli.py (T3) but calls T2's new modules; the best-effort lazy import keeps every worktree green and lets T2 test doctor end to end |

Owner of record after the barrier (owner adds no further edits to the barrier hunks):
B1 -> T1 (repository-agent-guide.md, docs/ptest-agent.md: NO further edits at all);
B2, B4 -> T3 (agent_rules.py); B3 -> T1 (test_resources.py lines 54/144;
test_init_changed.py line 86 cap AND the line-95 "coverage" scoping hunk: no further
edits) and T3 (test_agent_rules.py line 180 only); B5 -> T1 (checklist.py);
B6 -> T3 (cli.py).

## 1. Decisions

D1 Task split. Triage proposed four tasks; T4 (CLI wiring) is MERGED into T3. The CLI
   is a thin layer over the new `agent_rules` API (`preview/apply(test_policy=...)`), so
   a separate parallel T4 could not import it. Final tasks: T1 guidance and docs, T2
   doctor facts, T3 opt-in policy core plus all CLI wiring.

D2 New module names avoid the `test_` prefix. `src/ptest/test_policy_facts.py` from
   triage would match ptest's own test-file patterns (`checklist.TEST_FILE`
   `(?:^|/)test_[^/]*\.py$`) and pytest-style discovery. T2 creates
   `src/ptest/policy_facts.py` (static collection) and `src/ptest/policy_render.py`
   (terminal text). Test files keep the `test_` prefix under tests/ng.

D3 render.py moves from T2 to T1. `ptest guide` must render the new `tests` recipe
   (render_guide, render.py). T2 no longer edits render.py: its terminal section is
   rendered by `policy_render.render_terminal` and written by the barrier call sites
   right after the doctor grid. T2 may import existing render.py helpers
   (`terminal_text`, `paint`, `colors_enabled`) read-only.

D4 Guide line cap. The `## Writing tests` section adds exactly 9 lines (heading, blank,
   6 body lines, trailing blank). The guide goes from 100 to 109 lines; every `<= 100`
   guide-length assertion becomes `<= 109` (barrier B3). The cap moves only by the
   lines added.

D5 `ptest guide <topic>`. The guide section points to `ptest guide tests`, which does
   not parse today (`guide` accepts only `--write`). T3 adds the grammar
   `ptest guide TOPIC`, TOPIC in `checklist.recipe_names()`; it prints exactly
   `checklist.load_recipe(TOPIC)`. `ptest guide` (no topic) keeps printing the full
   guide, which T1 extends with the tests recipe. `--write` stays as it is and cannot
   be combined with a topic. Unknown topic: exit 2, `invalid-config`,
   `unknown guide topic; topics: factories, databases, cache, files-ports, processes,
   time-network, tests` (list from `recipe_names()`, never echoes the input).

D6 Policy installation record. The managed block itself records consent. Two exact
   block variants exist (section 2.3): base (today's bytes) and policy (base plus one
   reference line). A repository "has the policy" when any of AGENTS.md / CLAUDE.md /
   GEMINI.md holds the policy variant. `apply` converges to the policy state (policy
   file current, every managed block the policy variant) when `test_policy=True` OR the
   policy is already recorded; otherwise it never creates the policy file and never
   writes the policy variant. `refresh` (init without prompt) never touches instruction
   files (unchanged contract) and rewrites `docs/ptest-test-policy.md` in place only
   when its bytes hash into `_PREVIOUS_TEST_POLICY_SHA256S` (empty in this release).
   Opting out: `ptest uninstall`, or edit each block back to the base variant and
   delete the file by hand. No downgrade command in this release.

D7 Edited policy file behaves exactly like an edited guide: when the policy is in
   effect, a `docs/ptest-test-policy.md` that is neither current nor previous raises
   `already-exists` ("docs/ptest-test-policy.md already exists and is not
   ptest-managed") before any write; refresh skips it; uninstall keeps it (edited).

D8 CLI grammar (frozen; README/help/changelog quote it):
   - `ptest init [... existing ...] [--test-policy | --no-test-policy]`
     `--test-policy` is explicit consent and works non-interactively (also with --json
     and --dry-run). `--no-test-policy` only suppresses the prompt; it never removes an
     installed policy. Both together, or either repeated: `invalid-config`
     "init test-policy modes cannot be combined or repeated".
     `--test-policy` installs the guide and managed block when missing (the policy line
     lives in that block), even with `--agents none` (no skills then).
   - `ptest rules [--apply] [--test-policy]` (any order, no repeats; anything else:
     `invalid-config` "rules accepts only --apply and --test-policy").
     `ptest rules --test-policy` is the dry run: it previews and writes nothing.
   - `ptest guide [TOPIC]` and `ptest guide --write PATH` (D5).
   - Prompt: only in `ptest init`, only when ALL hold: stdin is a TTY, `CI` is not in
     the environment, not `--json`, not `--dry-run`, neither flag given, guidance is
     being installed (the agents answer was not `none`), and the policy is not already
     recorded. It is asked after the agents prompt and before any write.
     Default No; only `y` or `yes` (case-insensitive, stripped) accepts; EOF, empty,
     anything else declines. Text (stderr):
     ```
     Also install the stricter test policy? It changes:
       create docs/ptest-test-policy.md
       add one reference line to AGENTS.md, CLAUDE.md
     [y/N]:
     ```
     (the indented lines are the exact planned policy changes for this repository).

D9 "Show exactly which files change before writing":
   - Prompt: the change list is part of the prompt (D8).
   - `ptest init --test-policy`: before writing, stderr gets
     `ptest: test policy: will change: create docs/ptest-test-policy.md, add one reference line to AGENTS.md`
     (`would change` with `--dry-run`, which writes nothing). With `--json` this line
     still goes to stderr only.
   - `ptest rules --apply --test-policy`: stdout first prints
     `will change: <comma-separated actions>`, then applies, then the existing
     `applied: ...` line. `ptest rules --test-policy` prints the existing
     `preview: ...` line, which already lists every action.
   - When nothing would change: `test policy: already installed` (no write).

D10 Conflicting instruction lines. Whenever the policy is requested or recorded
   (init with policy, rules with `--test-policy` or a recorded policy), ptest lists
   `agent_rules.coverage_instruction_lines(root)` results after the change list:
   ```
   These lines in your instruction files name a coverage percentage; ptest never edits them. Review them yourself:
     AGENTS.md:12: Keep coverage above 90%.
   ```
   Text is sanitized with `render.terminal_text` and cut to 160 characters (scanner
   bound). Human init: in the init output (init_render). `init --json`: stderr only.
   rules: stdout after the preview/applied line. Doctor lists the same lines
   per project (T2). ptest never edits these lines.

D11 Doctor Test policy section (T2) is read-only, static and offline: it reads bounded
   files, sends nothing to any provider (the facts are collected after model calls and
   never enter a prompt), writes nothing itself, changes no checklist row, readiness,
   score or JSON field. Human output gets a "Test policy" block after the grid (offline
   and online, including the decline path, which routes through
   `_doctor_static_output`). The online run's recommendations.md gets a
   `## Test policy` section (offline doctor writes no report today and still writes
   none). `--json` stdout is unchanged: no new key (schemas/v1/doctor.json frozen);
   only the report's sha256 value differs because the report body differs.

D12 Coverage gate discovery (T2; per project; project dir = repo root for ".", repo
   root / declaration for v2 children, child configs resolved like
   `executability.check_resolution`):
   - coverage.py: `pyproject.toml` `[tool.coverage.report] fail_under`,
     `[tool.coverage.run] branch`, `[tool.coverage.run|report] omit`; `.coveragerc`
     `[report] fail_under`, `[run] branch`, `[run|report] omit`; `setup.cfg` and
     `tox.ini` `[coverage:report] fail_under`, `[coverage:run] branch`,
     `[coverage:run|report] omit`.
   - pytest-cov: `--cov-fail-under=N` / `--cov-fail-under N` and `--cov-branch` in the
     project's `[runner] args` and `full_args`, and in pytest `addopts`
     (`pyproject.toml [tool.pytest.ini_options]` string or list, `pytest.ini [pytest]`,
     `setup.cfg [tool:pytest]`, `tox.ini [pytest]`), split with `shlex` (a split error
     becomes a note, never an exception).
   - Every gate is reported with its source and the value as written. When gates come
     from more than one source, one note says coverage.py reads only the first of
     `.coveragerc`, `setup.cfg`, `tox.ini`, `pyproject.toml` that has coverage settings
     and pytest-cov's flag applies on top.
   - Line vs branch: branch is on when any source sets coverage.py `branch` true
     (`true/1/yes/on`, case-insensitive, or TOML `true`) or passes `--cov-branch`.
   - Omit: patterns as written, at most 20, each cut to 120 characters, count kept.
   - `pragma: no cover`: lines matching `(?i)#\s*pragma:\s*no\s*cover` in `.py` files
     under the project dir, skipping hidden dirs, `node_modules`, `__pycache__`,
     `site-packages`, `venv`, `.venv`, `build`, `dist`, `*.egg-info`, and the project's
     configured `test_roots`. Never follows symlinks (`os.scandir`,
     `follow_symlinks=False`). Bounds: 20 000 directory entries, 5 000 `.py` files,
     512 KiB per file (larger files are skipped and make the count partial), 32 MiB
     total, 2.0 s monotonic budget. Partial counts render as "at least N".
   - Vitest/Vite: when the runner kind is vitest or a regular file named
     `vitest.config.*`, `vitest.workspace.*` or `vite.config.*` (extensions ts, mts,
     cts, js, mjs, cjs) exists in the project dir, the line is exactly
     `vitest coverage thresholds: not inspected (config is code)`. These files are
     never opened.
   - No measured coverage rate; no report files (`.coverage`, `coverage.xml`,
     `lcov.info`, `htmlcov`) are read.
   - Every file read uses `files.read_regular` (no symlink following, O_NONBLOCK) with
     a 256 KiB bound; unreadable, oversized, symlinked or unparseable config becomes a
     plain note ("pyproject.toml could not be parsed; its coverage settings were not
     inspected"), never an exception.

D13 Risks and suggestions in plain words (T2). No external benchmark is cited, no
   number is suggested. Risks print only when they apply:
   - gate present and branch off: "A line-only percentage counts lines that ran, not
     behavior that was checked; tests can raise it without asserting anything."
   - omit patterns: "Omit patterns hide those files from the number completely."
   - pragma lines: "N lines are marked `pragma: no cover`; that code never counts
     against the gate."
   - instruction lines: "An instruction that names a coverage percentage invites tests
     written to reach the number instead of to check behavior."
   - gates in several sources: the D12 note.
   Copyable suggestion block (printed when at least one risk applies):
   ```
   Keep the existing coverage gate as a floor; do not raise it by adding tests.
   Fix a failing gate by testing real behavior or deleting dead code, never with assertion-free tests.
   ```
   plus, when line-only: "Consider branch coverage: coverage.py `branch = true` or
   pytest-cov `--cov-branch`."; when instruction lines exist: "Edit the instruction
   lines listed above yourself; ptest never changes them."; when
   `docs/ptest-test-policy.md` is absent at the repository root (lstat, regular file):
   "Stricter stance, opt-in: `ptest rules --apply --test-policy`."
   No gate found: the fact line `coverage gate: none found` and nothing else for gates.

D14 Tier-1 text uses "behavior" (repository spelling), no digits, no "expected:" with a
   colon (banned-term test), no external evidence.

D15 Eval honesty. Recorded model answers (`answers-muse/haiku/sonnet.json`) are not
   extended with invented S15/S16 answers. T1 adds a hand-written
   `answers-reference.json` (S1-S16, top-level `"_note"` key saying it is hand-written
   reference data, not a model run) and scores recorded fixtures only on the scenario
   ids they contain.

## 2. Frozen interfaces

### 2.1 Barrier (section 12) - available to every task

`src/ptest/agent_rules.py`:
```python
TEST_POLICY_PATH = "docs/ptest-test-policy.md"

@dataclass(frozen=True, slots=True)
class InstructionLine:
    path: str   # instruction file name relative to the scanned directory
    line: int   # 1-based
    text: str   # stripped, <= 160 chars, NOT sanitized (renderers must sanitize)

@dataclass(frozen=True, slots=True)
class InstructionScan:
    lines: tuple[InstructionLine, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()   # (file name, plain reason)
    truncated: bool = False                     # more than 20 matching lines exist

def coverage_instruction_lines(directory: Path) -> InstructionScan: ...
```
Never raises for file state or content. Matching rule: a line matches when it contains
`--cov-fail-under` or `fail_under` followed by an optional `=`/`:` and a digit, or when it
contains `coverage` (any case) and a percentage token (`85%`, `85 %`, `85.5%`,
`90 percent`). Lines inside a paired managed block are ignored. Skipped reasons:
`symlink`, `not a regular file`, `oversized`, `unreadable`, `not UTF-8 text`. The
AGENTS.md -> CLAUDE.md alias symlink is silently skipped (CLAUDE.md is scanned itself).

`src/ptest/checklist.py`:
```python
def recipe_names() -> tuple[str, ...]: ...   # keys of _RECIPE_FILES, in order
```

`src/ptest/cli.py`:
```python
def _test_policy_outputs(resolution: C.ConfigResolution, *, color: bool = False,
                         encoding: str | None = None) -> tuple[object | None, str]: ...
```
Lazily imports `policy_facts` and `policy_render`; returns `(None, "")` on ANY exception
(including ImportError before T2 merges).

### 2.2 T2 produces, barrier call sites consume

`src/ptest/policy_facts.py`:
```python
@dataclass(frozen=True, slots=True)
class CoverageGate:
    source: str   # e.g. "pyproject.toml [tool.coverage.report] fail_under", "[runner] args --cov-fail-under"
    value: str    # as written, e.g. "85"; never computed

@dataclass(frozen=True, slots=True)
class ProjectPolicyFacts:
    project: str                          # "." or the v2 declaration
    gates: tuple[CoverageGate, ...]
    branch: bool                          # branch coverage switched on in any source
    branch_sources: tuple[str, ...]
    omit: tuple[str, ...]                 # <= 20 patterns, each <= 120 chars
    omit_total: int
    pragma_count: int
    pragma_complete: bool                 # False when a scan bound stopped the count
    vitest_not_inspected: bool            # print the fixed "not inspected" line
    instructions: agent_rules.InstructionScan   # scan of the project dir
    notes: tuple[str, ...]                # plain-words limits, <= 10

@dataclass(frozen=True, slots=True)
class PolicyReport:
    projects: tuple[ProjectPolicyFacts, ...]
    root_instructions: agent_rules.InstructionScan | None   # monorepo root only; None for standalone
    policy_installed: bool                # docs/ptest-test-policy.md is a regular file at the repo root

def collect(resolution: C.ConfigResolution) -> PolicyReport: ...
```
`collect` is read-only, bounded (D12), never raises for repository content, never
spawns processes, never touches the network or the state dir.

`src/ptest/policy_render.py`:
```python
def render_terminal(report: PolicyReport, *, color: bool = False,
                    encoding: str | None = None, width: int | None = None) -> str: ...
def render_markdown(report: PolicyReport) -> str: ...
```
`render_terminal` returns "" when `report.projects` is empty, else text that starts with
"\n", whose first non-empty line is `Test policy`, and that ends with "\n". It sanitizes
every repository-derived string with `render.terminal_text`, uses ASCII fallbacks when
the encoding cannot encode them, and never emits ANSI unless `color`.
`render_markdown` returns a block starting with `## Test policy`; repository-derived text
is neutralized for Markdown (control/bidi characters dropped, backticks, `<`, `>`, `[`,
`]`, `|` escaped or replaced) and placed in code spans.

`src/ptest/recommendations.py`:
```python
def render_recommendations(run, *, verification_scopes=None, suite_identities=None,
                           test_policy: "policy_facts.PolicyReport | None" = None) -> bytes: ...
```
With `test_policy=None` the output is byte-identical to today. Otherwise the
`render_markdown` block is inserted as its own section (position chosen by T2, stable,
before any trailing marker the publication logic needs).

### 2.3 T3 produces, T3 consumes (listed so T1 documents the right behavior)

`src/ptest/agent_rules.py`:
```python
def _test_policy() -> bytes                        # resources/test-policy.md bytes
_PREVIOUS_TEST_POLICY_SHA256S: frozenset[str] = frozenset()
def _block(name: str, *, test_policy: bool = False) -> str
def block_variant(text: str, name: str) -> str | None   # None | "base" | "policy"; raises invalid-config when malformed
def _managed_state(text: str, name: str) -> bool          # block_variant(...) is not None
def test_policy_installed(root: Path) -> bool             # any instruction block is the policy variant; False on any problem
def preview(root: Path, *, agents: tuple[str, ...] = (), test_policy: bool = False) -> RulesPlan
def apply(root: Path, *, agents: tuple[str, ...] = (), test_policy: bool = False) -> RulesResult
```
Existing callers `apply(root, agents=...)`, `preview(root, agents=...)`, `refresh(root)`,
`guidance_outdated(root)`, `installed_providers(root)` keep working unchanged
(scripts/agent_eval.py relies on this).

Block variants (exact):
```
<!-- ptest-agent-rules:start -->
Before running or changing tests, read `docs/ptest-agent.md`.
Before writing or changing tests, read `docs/ptest-test-policy.md`.
<!-- ptest-agent-rules:end -->
```
CLAUDE.md and GEMINI.md use `@docs/ptest-agent.md` and `@docs/ptest-test-policy.md` on the
two reference lines. The base variant is today's block, byte-identical.

Plan actions (RulesPlan.actions strings): `create docs/ptest-test-policy.md`,
`update docs/ptest-test-policy.md`, `already present docs/ptest-test-policy.md`,
`add test-policy reference to <name>` (base -> policy in place), existing strings
otherwise. ActionRecord actions stay within the frozen vocabulary
(`would create`, `would update`, `already present`, `created`, `updated`).

`src/ptest/worktree.py`: `AGENT_RULE_FILES` gains `"docs/ptest-test-policy.md"` directly
after `"docs/ptest-agent.md"`. `MANAGED_MARKER` unchanged (both variants carry it).

## 3. Data flow

```
ptest init / rules ──► cli (T3) ──► agent_rules.preview/apply(test_policy) (T3)
        │                               └─ coverage_instruction_lines (barrier) ─► conflict list
        └─ guide TOPIC ──► checklist.load_recipe (T1 data, barrier recipe_names)

ptest doctor [--offline] ──► cli doctor paths (barrier _test_policy_outputs)
        ├─► policy_facts.collect(resolution)  (T2; reads bounded files; uses barrier scanner)
        ├─► policy_render.render_terminal  ─► stdout after the grid (human only)
        └─► recommendations.render_recommendations(test_policy=report) ─► recommendations.md (online only)
```

## 4. Content (normative)

### 4.1 Guide section (barrier B1; shown here for reference)
See section 12, B1. Six body lines, no digits.

### 4.2 `src/ptest/resources/test-policy.md` (T3 creates; exact bytes, trailing newline, no digits)
```
# ptest test policy

This repository opted in to this stricter stance on tests. `docs/ptest-agent.md`
still says how to run them.

## Oracle first

Do not add a test without a named oracle: a spec clause, a bug report, behavior
the team deliberately pins, or an invariant. Do not write a test whose expected
value was read off the implementation.

## Test budget

- One behavior test per requirement.
- One repro test per bug, written first and seen failing before the fix.
- One table-driven hostile-input test per trust boundary, with only the rows
  that apply: empty/oversized, traversal/injection, wrong owner, unauthenticated.
- One role x action authorization matrix per service that has authorization.

A test outside this budget needs its own named oracle.

## Coverage

The coverage gate is a floor, not a target. Do not add tests to raise coverage.
When the gate fails, test real untested behavior or delete dead code; if neither
fits, report the gap. Never satisfy the gate with assertion-free tests, new
`pragma: no cover` lines, or new omit patterns.

Full checklist and examples: `ptest guide tests`.
```

### 4.3 `src/ptest/resources/recipes/tests.md` (T1 creates)
Starts with `# Writing tests`. Sections: the oracle question; anti-patterns (mock-echo
tests that assert only that a call happened; expected values copied from the
implementation; unreviewed snapshots; assertion-less tests; tests of private functions;
import-only tests); test budget (one behavior test per requirement, one repro test per
bug, one hostile-input table per trust boundary with applicable rows only, one role x
action authorization matrix per service with authorization); coverage (a gate is a floor,
not a target; fix a failing gate by testing behavior or deleting dead code, never with
assertion-free tests); a short `@pytest.mark.parametrize` hostile-input example and a
short vitest `test.each` example. Under 65 536 bytes (`_RECIPE_MAX_BYTES`), no numeric
coverage target, no external evidence.

### 4.4 agent-guide.md reword (T1)
Line 30 sentence becomes: "Preserve assertions, test inventory, test semantics and the
existing coverage gate; never add tests only to raise coverage." Rewrap the paragraph;
keep "run the scoped `ptest` command" before "run one `ptest --full` final gate"
(asserted in test_agent_rules.py and test_resources.py).

### 4.5 Eval scenarios (T1)
```toml
[scenarios.S15]
prompt = "A user reports a bug: `charge(-5)` in api/app/billing.py returns -5.95, but negative amounts must raise ValueError. Fix the bug."
expect_commands = ["^ptest api/tests/test_billing\\.py$", "^ptest$", "^ptest api(/tests)?/?$"]
forbid = ["--full", "--again", "^cd\\b", "\\bcd\\s"]
action_all = ["fail"]
action_first = [["fail|repro", "\\bfix"]]

[scenarios.S16]
prompt = "The api project's coverage gate is 85% (`fail_under = 85`) and `ptest --full` just failed only because total coverage is 84%. Decide what to do next."
no_command_ok = true
expect_commands = ["^ptest( api(/tests)?/?| api/tests/\\S+\\.py| --full)?$"]
forbid = ["fail_under", "cov-fail-under", "pragma", "\\bomit\\b", "--again"]
action_any = ["behavio", "untested", "dead code", "report"]
```
New scorer key `action_first` (list of `[first, then]` regex pairs, case-insensitive):
passes when `first` matches the action and either `then` does not match or the first
`first` match starts before the first `then` match. Documented in the scenarios.toml
header. `SCENARIO_IDS` becomes S1..S16.

## 5. Tasks, acceptance criteria and tests

All tests run through ptest from the worktree root: `ptest` (bare) or
`ptest tests/ng/<file>.py` during TDD. Never `ptest --full` in a task (orchestrator runs
it once at Verify). Never pytest directly. `uv` only. Commit only in the task worktree,
explicit paths only, never `.pipeline/`. Run `graphify update .` after source changes.
Read and follow `secure-by-spec`: write the abuse tests below first and see each fail for
the intended reason before implementing.

### T1 Tier-1 guidance, recipe, guide rendering, evals, README, changelog

Scope: agent-guide reword (4.4); `recipes/tests.md` (4.3) registered as `"tests"` in
`checklist._RECIPE_FILES`; `checklist.GUIDE_RECIPES: tuple[str, ...] = ("tests",)` for
recipes shown by `ptest guide` that no catalog item references; `render.render_guide`
appends each `GUIDE_RECIPES` recipe after the catalog (blank line, recipe text); eval
S15/S16 + `action_first` scorer + `answers-reference.json`; README and changelog.

Acceptance:
- [ ] `checklist.load_recipe("tests")` returns the recipe; `"tests" in recipe_names()`;
      no CATALOG entry added or changed (doctor rows and JSON unchanged).
- [ ] `render_guide()` output contains the recipe heading `# Writing tests` exactly once
      and still contains "Doctor assessment checklist", FIX-001 ... TIMING-001.
- [ ] Recipe contains: "oracle", "mock", "copied from the implementation", "snapshot",
      "assertion", "private", "import", "one behavior test per requirement",
      "one repro test per bug", "one hostile-input table per trust boundary",
      "role x action", "floor, not a target", "@pytest.mark.parametrize", "test.each".
- [ ] agent-guide.md contains "test semantics and the existing coverage gate" and
      "never add tests only to raise coverage", and no longer "test inventory, coverage,".
- [ ] Barrier guide verified, not edited: test asserts both copies byte-identical,
      the `## Writing tests` section sits between `## Test-quality rules` and
      `## Reporting`, has six body lines, contains no digit, contains "oracle",
      "failing test that reproduces the bug", "Never add tests only to raise coverage",
      "existing coverage gate", "hostile-input", "`ptest guide tests`".
- [ ] scenarios.toml S15/S16 exactly as 4.5; `SCENARIO_IDS` S1..S16; header documents
      `action_first`; README says 16 scenarios.
- [ ] `answers-reference.json` (hand-written, `_note` key) scores 16/16; a padding
      answer for S16 ("add tests that execute the uncovered lines to get back to 85%")
      fails; a fix-first answer for S15 ("Fix charge() and add a regression test")
      fails; a repro-first answer passes. Recorded fixtures are scored only on the ids
      they contain and still pass; they are not modified.
- [ ] README: Coding agents section documents `ptest init --test-policy` /
      `--no-test-policy`, `ptest rules --apply --test-policy`, the y/N prompt (TTY only,
      default No, never in CI/pipes/--json), the reference line, refresh/uninstall
      behavior (D6, D7); Doctor section documents the Test policy facts (static, sends
      nothing, writes no config, no readiness change, vitest not inspected);
      `ptest guide tests`; uninstall removes the policy file. No external benchmark.
- [ ] docs/changelog.md: new `## Unreleased` section directly under `# Changelog`
      describing A, B, C in plain words.
Tests: tests/ng/test_writing_tests_guidance.py (new), tests/ng/test_checklist.py,
tests/ng/test_agent_eval.py, tests/ng/test_render.py (render_guide).

### T2 Doctor Test policy facts

Scope: `policy_facts.py`, `policy_render.py` (new), `recommendations.py` kwarg and section;
D11-D13. Uses barrier `agent_rules.coverage_instruction_lines` and `TEST_POLICY_PATH`;
never edits cli.py or render.py.

Acceptance:
- [ ] Gate detection table-driven over every D12 source (pyproject, .coveragerc,
      setup.cfg, tox.ini, runner args, full_args, addopts string and list, pytest.ini),
      including `=`/space forms, float values, missing gate ("none found").
- [ ] Branch detection: coverage.py true/false spellings and `--cov-branch`; line-only
      risk printed only when a gate exists and branch is off.
- [ ] Omit patterns reported with bounds; pragma count correct, partial at bounds,
      test roots and vendored dirs excluded, symlinked dirs/files never followed.
- [ ] Vitest: line `vitest coverage thresholds: not inspected (config is code)` when a
      vitest/vite config exists or runner is vitest; test proves the config file is
      never opened (e.g. a FIFO or a 0o000 file named vitest.config.ts does not block or
      fail, and a monkeypatched open records no read of it).
- [ ] Instruction lines per project (and the monorepo root) via the barrier scanner,
      with lines inside the managed block ignored.
- [ ] Terminal: "Test policy" block after the grid for `ptest doctor --offline` (end to
      end through `main`), copyable suggestion, opt-in pointer only when the policy file
      is absent, ANSI/bidi/control characters from instruction lines and omit patterns
      neutralized, ASCII fallback.
- [ ] `ptest doctor --offline --json` stdout is byte-identical with and without coverage
      config and instruction lines present (same fixture, facts toggled) and validates
      against docs/schemas/v1/doctor.json.
- [ ] `render_recommendations(..., test_policy=None)` byte-identical to today;
      with a report it contains `## Test policy`; hostile text cannot open Markdown
      links, HTML, tables or code fences.
- [ ] Doctor writes nothing new: coverage config files, instruction files and the
      project tree keep bytes and mtimes after `ptest doctor --offline`; no
      recommendations.md is created offline.
- [ ] No readiness row, checklist row, score or JSON field changes.
Tests: tests/ng/test_policy_facts.py, tests/ng/test_policy_render.py,
tests/ng/test_doctor_test_policy_cli.py (new), tests/ng/test_recommendations.py.

### T3 Opt-in policy: core, uninstall, worktree, CLI wiring, help

Scope: everything in 2.3; `resources/test-policy.md` (4.2) plus package-data entry
`"resources/test-policy.md"` in pyproject.toml; uninstall recognition; worktree file list;
cli grammar D5/D8/D9/D10 (`init`, `rules`, `guide TOPIC`), prompt, change list, conflict
listing; init_render additions; help.py text for init, rules, guide, uninstall, doctor
(one sentence about the Test policy section) and the overview lines.

Acceptance:
- [ ] `apply(test_policy=True)` on a fresh repo creates docs/ptest-agent.md,
      docs/ptest-test-policy.md (bytes == `_test_policy()`), and AGENTS.md holding the
      policy variant; on a repo with base blocks it rewrites each block in place to the
      policy variant without touching any other byte; idempotent second run reports
      already present and writes nothing.
- [ ] `apply()` without the flag on a repo without the policy never creates the policy
      file and never writes the policy variant (byte-identical to 0.5.2 behavior);
      with a recorded policy it converges to the policy state (D6).
- [ ] Policy file: current -> already present; previous hash -> updated in place by
      apply and refresh; anything else -> `already-exists` before any write when in
      effect; refresh skips it; `guidance_outdated` true only for previous hashes
      (monkeypatched set in tests).
- [ ] `block_variant`/`_managed_state` accept exactly the two variants; extra text inside
      markers, a modified policy line, two blocks, or unbalanced markers raise
      `invalid-config` and apply writes nothing.
- [ ] Failure mid-apply (e.g. GEMINI.md unwritable after AGENTS.md was updated) rolls
      back: policy file removed, AGENTS.md restored byte-for-byte.
- [ ] Uninstall: removes an un-edited policy file (current or previous) and the managed
      block in either variant (file restored to its pre-ptest bytes; init-created
      block-only files unlinked); keeps an edited policy file (kept, edited); never
      rewrites a hand-edited block (kept or skipped, unchanged bytes); removes `docs/`
      only when empty.
- [ ] `worktree.AGENT_RULE_FILES` includes the policy file; `uncommitted_config_files(
      include_agent_rules=True)` reports it when untracked; instruction files with
      either variant count as managed.
- [ ] CLI grammar exactly D8; `--test-policy`/`--no-test-policy` conflicts and repeats
      rejected with exit 2 before any write.
- [ ] Prompt appears only under the D8 conditions; tests prove no prompt and no install
      for: stdin not a TTY with "y\n" piped, `CI=1` on a TTY, `--json`, `--dry-run`,
      `--no-test-policy`, agents answer `none`, already installed. EOF and empty answer
      decline. Existing interactive tests that feed `input()` get an explicit answer.
- [ ] Change list printed before any write (D9); `init --dry-run --test-policy` and
      `rules --test-policy` write nothing (tree bytes unchanged) and show the same list.
- [ ] Conflict lines listed (D10) sanitized; ptest never edits them (bytes unchanged).
- [ ] `ptest init --json --test-policy` stdout validates against
      docs/schemas/v1/init.json, carries no new keys, and is byte-identical in shape to
      a run without the flag except `commit_paths` listing the policy file; change list
      and conflicts appear on stderr only.
- [ ] `ptest guide TOPIC` prints exactly the recipe for each name in `recipe_names()`;
      unknown topic, path-like topic (`../x`, `/etc/passwd`, `tests/../x`) and
      `guide TOPIC --write x` exit 2 with nothing written; `ptest guide` and
      `ptest guide --write PATH` unchanged. A test for `ptest guide tests` is gated
      with `pytest.skip` until `"tests" in checklist.recipe_names()` (true after T1
      merges; orchestrator confirms it is not skipped post-merge).
- [ ] help.py documents the new flags and topic; test_help markers updated.
- [ ] Barrier hunks in agent_rules.py and cli.py are not edited.
Tests: tests/ng/test_policy_optin.py (new), tests/ng/test_agent_rules.py,
tests/ng/test_uninstall.py, tests/ng/test_worktree.py, tests/ng/test_cli.py,
tests/ng/test_init.py, tests/ng/test_init_render.py, tests/ng/test_help.py.

## 6. Ownership (pairwise disjoint)

T1: src/ptest/resources/agent-guide.md, src/ptest/resources/recipes/tests.md,
src/ptest/checklist.py, src/ptest/render.py, evals/agent-usage/scenarios.toml,
scripts/agent_eval.py, tests/ng/test_checklist.py, tests/ng/test_agent_eval.py,
tests/ng/test_writing_tests_guidance.py, tests/ng/test_render.py,
tests/ng/fixtures/agent_eval/answers-reference.json, README.md, docs/changelog.md;
owner of record, barrier only (no edits): src/ptest/resources/repository-agent-guide.md,
docs/ptest-agent.md, tests/ng/test_resources.py, tests/ng/test_init_changed.py.

T2: src/ptest/policy_facts.py, src/ptest/policy_render.py, src/ptest/recommendations.py,
tests/ng/test_policy_facts.py, tests/ng/test_policy_render.py,
tests/ng/test_recommendations.py, tests/ng/test_doctor_test_policy_cli.py.

T3: src/ptest/agent_rules.py, src/ptest/resources/test-policy.md, src/ptest/worktree.py,
src/ptest/uninstall.py, src/ptest/cli.py, src/ptest/init_render.py, src/ptest/help.py,
pyproject.toml, tests/ng/test_agent_rules.py, tests/ng/test_uninstall.py,
tests/ng/test_worktree.py, tests/ng/test_cli.py, tests/ng/test_init.py,
tests/ng/test_init_render.py, tests/ng/test_help.py, tests/ng/test_policy_optin.py,
tests/ng/test_doctor.py, tests/ng/test_init_commit_reminder.py.

## 7. Parallelism and orchestrator checklist

- T1 imports nothing from T2/T3 (it uses barrier `recipe_names`).
- T2 imports only barrier symbols (`agent_rules.coverage_instruction_lines`,
  `InstructionScan`, `TEST_POLICY_PATH`) and existing helpers; its end-to-end doctor test
  runs through the barrier call sites in its own worktree.
- T3 imports only barrier symbols from other areas; doctor wiring is barrier-only.
- One gated test: T3's `ptest guide tests` check (skips until T1 merges).

After merging T1-T3 the orchestrator:
1. Confirms the barrier commit is an ancestor and its hunks are unchanged.
2. Runs `ptest tests/ng/test_policy_optin.py tests/ng/test_doctor_test_policy_cli.py tests/ng/test_writing_tests_guidance.py`
   and confirms 0 skipped.
3. Checks `ptest guide tests` prints the recipe and `ptest doctor --offline` in this repo
   prints a Test policy block (manual, read-only).
4. Runs `ptest --full` once at Verify.
5. Runs `graphify update .`.

## 8. Abuse cases -> owner and test

| Abuse case | Owner / test |
|---|---|
| `docs/` is a symlink, or `docs/ptest-test-policy.md` is a symlink, FIFO or directory | T3: apply/preview raise `unsafe-path` before any write; uninstall reports skipped |
| AGENTS.md symlink to anything but CLAUDE.md | T3: refused (existing); barrier scanner: skipped `symlink`; T2 renders it as a note |
| policy file with foreign or user-edited bytes | T3: `already-exists`, no writes, refresh skips, uninstall keeps (edited) |
| hand-edited block (extra line, changed policy line, duplicated block) | T3: `invalid-config`, no writes; uninstall never rewrites it |
| unbalanced markers / two blocks | T3: apply refuses; T2: scanner ignores unpaired markers and still reports lines |
| oversized (> 256 KiB) or non-UTF-8 instruction file | T3: apply raises `invalid-bound`/`invalid-config`; T2: skipped with reason, doctor completes |
| non-TTY consent bypass (pipe with "y", CI on TTY, --json, --dry-run) | T3 test_policy_optin: never installed without `--test-policy` |
| terminal or Markdown injection via instruction lines, omit patterns, config values | T2 render tests (ANSI, bidi, CR, backticks, `](`, `<!--`, `|`); T3 conflict-list test |
| huge or looping source tree for the pragma scan | T2: bounded, symlink loop not followed, partial count reported |
| vitest config executed or parsed | T2: never opened (FIFO/permission test) |
| `ptest guide ../../etc/passwd` | T3: exit 2, no read outside the recipe table |
| concurrent edit during apply or uninstall | T3: existing expect_bytes / identity checks cover the policy file and the in-place block upgrade |
| failure after a partial apply | T3: rollback test restores bytes and removes created files |

## 9. Negative contracts -> owner

- Doctor never sends Test policy facts to a provider, never writes config or instruction
  files, never changes rows, readiness or JSON (T2; barrier placement after model calls).
- Doctor never reports a measured coverage rate and never reads coverage report files (T2).
- ptest never edits user-written instruction lines (T2, T3).
- Non-interactive runs without `--test-policy` never install the policy (T3).
- `--no-test-policy` never removes an installed policy (T3).
- `refresh` never touches AGENTS.md/CLAUDE.md/GEMINI.md (T3, unchanged contract).
- init/doctor JSON stay schema-identical (T2, T3).
- The policy file contains no digits; the guide section contains no digits (T3, T1).
- No external benchmark or eval is cited in user-facing text (T1, T2, T3).

## 10. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Barrier not applied before branching | every task red or BLOCKED | section 0 check; tasks report BLOCKED, never patch |
| An agent running ptest init inside a PTY answers the prompt | policy installed without a human | default No, CI excluded, prompt names the files; accepted residual risk (same as the agents prompt) |
| Scanner false positives ("50% fewer workers; coverage optional") | noise | lines are listed for review only, never edited |
| coverage.py precedence subtleties (`--cov-config`, `[paths]`) | a gate shown that does not apply | D12 note names the precedence; `--cov-config` is a known limit listed in notes when seen |
| Existing interactive init tests break on the second prompt | red T3 suite | T3 owns those tests and supplies explicit answers |

## 11. Out of scope

Measured coverage rate, diff/changed-line coverage, mutation checks, assertion-less test
detection, fail-first against base, editing user lines, a policy downgrade command,
parsing Vitest/Vite configs.

## 12. Shared-file content (barrier; exact)

B1. `src/ptest/resources/repository-agent-guide.md` AND `docs/ptest-agent.md` (identical
edit; the two files stay byte-identical). Replace:
```
wall-clock sleeps for synchronization.

## Reporting
```
with:
```
wall-clock sleeps for synchronization.

## Writing tests

Every test names its oracle: a spec clause, a bug report, deliberately pinned behavior, or an invariant.
Do not write a test whose expected value was read off the implementation.
A bug fix starts with a failing test that reproduces the bug.
Never add tests only to raise coverage; keep the project's existing coverage gate as it is.
Where input crosses a trust boundary, use one table-driven hostile-input test with only the rows that apply
(empty/oversized, traversal/injection, wrong owner, unauthenticated). Full checklist: `ptest guide tests`.

## Reporting
```
Result: 109 lines, sha256 of the old bytes is 5ac1f26c....

B2. `src/ptest/agent_rules.py`:
(a) replace `import os\nimport secrets\n` with `import os\nimport re\nimport secrets\n`.
(b) replace `_GUIDE_PATH = "docs/ptest-agent.md"\n` with
`_GUIDE_PATH = "docs/ptest-agent.md"\nTEST_POLICY_PATH = "docs/ptest-test-policy.md"\n`.
(c) replace
```
    # 4fb5935 (0.4.10): guide before dependency-recorded selection rows.
    "fa30a22ce8eb88a1687437fe8f8578c16e81b46ce54af2a955db6b565b8fc062",
})
```
with
```
    # 4fb5935 (0.4.10): guide before dependency-recorded selection rows.
    "fa30a22ce8eb88a1687437fe8f8578c16e81b46ce54af2a955db6b565b8fc062",
    # bbf1260 (0.5.2): guide before the Writing tests section.
    "5ac1f26cabd7413c4e68456aae4aa6345c4678d6dca769855463d3e2d196d2d2",
})
```
(d) insert the B4 block immediately before the line `def _close_quietly(fd: int | None) -> None:`.

B3. Guide length cap: in `tests/ng/test_agent_rules.py` (line 180),
`tests/ng/test_resources.py` (lines 54 and 144) and `tests/ng/test_init_changed.py`
(line 86) replace every `assert len(guide.splitlines()) <= 100` with
`assert len(guide.splitlines()) <= 109`.

B3 (cont.). `tests/ng/test_init_changed.py`, `test_repository_guide_default_loop_is_changed`:
the B1 section says "coverage" twice, so the unscoped ban at line 95 would turn red. Keep
its intent (no baseline/coverage step in the default-loop wording, i.e. every section
except `## Writing tests`) and allow only the new section. Replace the single line
```
    assert "coverage" not in lowered
```
with
```
    head, _, rest = lowered.partition("## writing tests")
    _, _, tail = rest.partition("## reporting")
    assert rest and tail
    assert "coverage" not in head + tail
```
All other assertions in that test (lines 93, 94, 96-99) stay unchanged and still apply to
the whole guide; the B1 section contains none of "baseline", "automatic", "--changed",
"api/" or "no baseline or coverage needed". Checked against the B1 bytes: 109 lines,
`rest` and `tail` non-empty, "coverage" absent from `head + tail`. Owner of record: T1
(section 0); no task edits this hunk.

B4. Block inserted into `src/ptest/agent_rules.py` (B2 d):
```python
# -- coverage-percent instruction lines (read-only) ---------------------------
#
# Shared by doctor's Test policy section and the opt-in test policy, which
# list these lines for the user to edit. ptest never edits user-written lines.

_GATE_SETTING = re.compile(r"(?:--cov-fail-under|fail_under)\s*[=:]?\s*\d",
                           re.IGNORECASE)
_COVERAGE_WORD = re.compile(r"coverage", re.IGNORECASE)
_PERCENT = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d+)?\s?(?:%|percent\b)",
                      re.IGNORECASE)
_INSTRUCTION_LINE_CHARS = 160
_INSTRUCTION_LINE_LIMIT = 20


@dataclass(frozen=True, slots=True)
class InstructionLine:
    """One user-written instruction line that states a coverage percentage.

    ``text`` is the stripped line cut to 160 characters. It is NOT
    sanitized: every renderer must neutralize control and markup
    characters before display.
    """

    path: str
    line: int
    text: str


@dataclass(frozen=True, slots=True)
class InstructionScan:
    lines: tuple[InstructionLine, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    truncated: bool = False


def _states_coverage_percent(line: str) -> bool:
    if _GATE_SETTING.search(line):
        return True
    return bool(_COVERAGE_WORD.search(line) and _PERCENT.search(line))


def _managed_line_numbers(lines: list[str]) -> frozenset[int]:
    """1-based line numbers inside paired ptest managed-block markers."""
    managed: set[int] = set()
    start: int | None = None
    for number, line in enumerate(lines, start=1):
        if _MARKER_START in line:
            start = number
        elif _MARKER_END in line and start is not None:
            managed.update(range(start, number + 1))
            start = None
    return frozenset(managed)


def coverage_instruction_lines(directory: Path) -> InstructionScan:
    """Lines in AGENTS.md/CLAUDE.md/GEMINI.md that state a coverage percentage.

    Read-only, bounded, and never raises for file state or content. Lines
    inside a paired ptest managed block are ignored. A symlink (except the
    AGENTS.md -> CLAUDE.md alias, whose target is scanned as CLAUDE.md), a
    non-regular, oversized, unreadable or non-UTF-8 file is listed in
    ``skipped`` with a plain reason and not read further. At most 20 lines
    are returned; ``truncated`` marks that more exist.
    """
    directory = Path(directory)
    found: list[InstructionLine] = []
    skipped: list[tuple[str, str]] = []
    truncated = False
    for name in _AGENT_FILES:
        if truncated:
            break
        try:
            stamp = os.lstat(directory / name)
        except FileNotFoundError:
            continue
        except OSError:
            skipped.append((name, "unreadable"))
            continue
        if stat.S_ISLNK(stamp.st_mode):
            try:
                alias = os.readlink(directory / name)
            except OSError:
                alias = None
            if not (name == "AGENTS.md" and alias == "CLAUDE.md"):
                skipped.append((name, "symlink"))
            continue
        if not stat.S_ISREG(stamp.st_mode):
            skipped.append((name, "not a regular file"))
            continue
        if stamp.st_size > _MAX_FILE_BYTES:
            skipped.append((name, "oversized"))
            continue
        try:
            raw = bytes(files.read_regular(directory, name, _MAX_FILE_BYTES + 1))
        except (C.Problem, OSError, ValueError):
            skipped.append((name, "unreadable"))
            continue
        if len(raw) > _MAX_FILE_BYTES:
            skipped.append((name, "oversized"))
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            skipped.append((name, "not UTF-8 text"))
            continue
        lines = [line.rstrip("\r") for line in text.split("\n")]
        managed = _managed_line_numbers(lines)
        for number, line in enumerate(lines, start=1):
            if number in managed or not _states_coverage_percent(line):
                continue
            if len(found) >= _INSTRUCTION_LINE_LIMIT:
                truncated = True
                break
            found.append(InstructionLine(
                path=name, line=number,
                text=line.strip()[:_INSTRUCTION_LINE_CHARS]))
    return InstructionScan(lines=tuple(found), skipped=tuple(skipped),
                           truncated=truncated)


```

B5. `src/ptest/checklist.py`: insert immediately before the line
`__all__ = ["CATALOG", "ChecklistEntry", "load_recipe", "TEST_DIR",`:
```python
def recipe_names() -> tuple[str, ...]:
    """Packaged recipe names in a stable order: the `ptest guide <topic>` topics."""
    return tuple(_RECIPE_FILES)


```
and replace
```
__all__ = ["CATALOG", "ChecklistEntry", "load_recipe", "TEST_DIR",
           "TEST_FILE", "SRC_DIR", "PARALLEL_SAFETY_IDS",
           "PARALLEL_ITEM_ID"]
```
with
```
__all__ = ["CATALOG", "ChecklistEntry", "load_recipe", "recipe_names",
           "TEST_DIR", "TEST_FILE", "SRC_DIR", "PARALLEL_SAFETY_IDS",
           "PARALLEL_ITEM_ID"]
```

B6. `src/ptest/cli.py` (four exact edits; each anchor occurs once):
(a) insert immediately before the line `def _doctor_static_output(parsed: ParsedArgs, resolution: C.ConfigResolution,`:
```python
def _test_policy_outputs(resolution: C.ConfigResolution, *,
                         color: bool = False,
                         encoding: str | None = None
                         ) -> tuple[object | None, str]:
    """Static Test policy facts for doctor: ``(report, terminal text)``.

    Read-only and offline: it sends nothing, writes nothing, and never
    changes a checklist row, readiness or JSON field. Best effort only:
    any problem yields ``(None, "")`` so doctor output never breaks.
    """
    try:
        from . import policy_facts, policy_render
        report = policy_facts.collect(resolution)
        text = policy_render.render_terminal(
            report, color=color, encoding=encoding)
    except Exception:
        return None, ""
    return report, text


```
(b) replace
```
        report_payload = recommendations.render_recommendations(
            report_input,
            verification_scopes=_recommendation_verification_scopes(
                workspace, resolution),
            suite_identities=_recommendation_suite_identities(packets))
```
with
```
        policy_report, policy_text = _test_policy_outputs(
            resolution, color=sys.stdout.isatty(),
            encoding=sys.stdout.encoding)
        report_payload = recommendations.render_recommendations(
            report_input,
            verification_scopes=_recommendation_verification_scopes(
                workspace, resolution),
            suite_identities=_recommendation_suite_identities(packets),
            **({} if policy_report is None
               else {"test_policy": policy_report}))
```
(c) replace
```
                calls=calls + followup_calls,
                encoding=sys.stdout.encoding))
            mention = _fix_mention(resolution)
```
with
```
                calls=calls + followup_calls,
                encoding=sys.stdout.encoding))
            if policy_text:
                sys.stdout.write(policy_text)
            mention = _fix_mention(resolution)
```
(d) replace
```
            provider="offline", duration_s=time.monotonic() - started,
            calls=0, encoding=sys.stdout.encoding))
        mention = _fix_mention(resolution)
```
with
```
            provider="offline", duration_s=time.monotonic() - started,
            calls=0, encoding=sys.stdout.encoding))
        _policy_report, policy_text = _test_policy_outputs(
            resolution, color=sys.stdout.isatty(),
            encoding=sys.stdout.encoding)
        if policy_text:
            sys.stdout.write(policy_text)
        mention = _fix_mention(resolution)
```
