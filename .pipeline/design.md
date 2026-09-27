# Design — changed-by-default (KISS)

Base: `feature/changed-by-default` @ fb1b8f6. Authoritative over `.pipeline/context-pack.md`
where they differ (differences are listed in "Deviations" at the end).

## 1. What changes, in one paragraph

Bare `ptest` and `ptest --changed` (no scope, no `--shadow`, no `--probe`) stop sending an
AUTOMATIC request to the history/baseline engine. Instead the CLI asks a new pure module
`ptest.impact` "what changed vs the branch base, and which tests does that reach?", then
sends an ordinary SCOPED (selected files, or `vitest --changed <sha>`) or FULL request to the
unchanged `operations.execute`, with a one-line blast-radius note for the start line and a
next-step hint on the end line. The AUTOMATIC engine stays as is (still used by `--shadow`, the
Python API and its tests); only the monorepo baseline-head plumbing that fed bare/`--changed`
is deleted.

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | Changed set = `git diff <base> HEAD` + `git diff HEAD` + untracked (non-ignored), via the existing `monorepo.worktree_changed_files(top, sha)`. | Reuse; already validated/no-hook. |
| D2 | Base: `--base REF` → merge-base(HEAD, REF), label = REF text. Else default ref = target of `refs/remotes/origin/HEAD` (e.g. `origin/dev`), else first existing local `main`, `master`, `dev`. If the current branch short name equals the default ref's branch name → `Base(None, "HEAD")` (uncommitted only). Else `Base(merge-base(HEAD, ref), ref)`. No default ref / unborn HEAD / merge-base failure → `Base(None, "HEAD")`. | Spec (1). Explicit REF also uses merge-base so an advanced `main` never shows its own commits as "changed". |
| D3 | Import graph: FILE level, static `ast` scan, name-keyed reverse index, BFS from the changed file's module names. Only test files' import chains count (conftest/fixture-mediated dependencies are NOT edges). | Spec (2) literally; KISS. The end-line hint `next: ptest --full before handoff` is the safety net. |
| D4 | Graph is built lazily: only when at least one relevant changed file is a non-test `.py`. Test-file counting always walks (cheap). | Keep bare `ptest` fast when only tests changed. |
| D5 | `[selection] enabled = false` → full for pytest ("selection is off …, set [selection] enabled = true"). Vitest ignores `enabled` and always delegates. Other runners → full on any relevant change. | Spec (2). |
| D6 | The config GENERATOR writes `[selection] enabled = true` for new pytest configs (§4.8.1). An existing config with `enabled = false` stays full-suite with the §4.2 reason. `ptest doctor --fix` flips `enabled` to true without `--cov` only for a generator-shaped table (§4.8.2). `init --changed-setup` only configures the optional coverage engine (`--shadow`) (§4.8.4). Owned by T3, except the `doctor --fix` next-step line, which lives in `cli.py` (T1, §4.8.3). | User requirement: changed is the default, even with no parameters. Measured ripple is 5 assertions, all in T3's files (§5 T3). |
| D7 | Impact `full` → `C.Mode.FULL` for every runner, `base=None` (pytest FULL rejects a base). No git evidence → full "git changes are unavailable". | Fail closed, one mapping, no legacy branch. |
| D8 | Nothing runs → no `--result-json` document is written, exit 0. | Nothing executed; KISS. |
| D9 | Monorepo: every child is planned against the same repo-level changed set. A path outside a child counts for that child only if it sits in an ancestor directory of the child AND its basename is a trigger basename (root `.ptest.toml`, root `uv.lock`, …). | Replaces the deleted `_classify` outside-trigger rule with one simple rule. |
| D10 | Deleted as dead: in `monorepo.py` → `ChangedChild`, `_COMMIT_RE`, `_child_baseline_head`, `child_baseline_heads`, `_committed_since`, `_run_all`, `select_changed_children`, `_classify`, `_vitest_base`, `child_changed_request` (and the `_matches` import if unused). Kept: `_git_blob`, `_repo_path`, `_nul_paths`, `worktree_changed_files`. `progress.format_changed_selected / format_changed_start / explain_changed_full_reason` and operations' AUTOMATIC start-line branch are KEPT (still reachable through `operations.execute` AUTOMATIC). | Spec: delete dead code, don't layer; but do not rip out the still-used engine. |
| D11 | `init --changed-setup` behaviour (coverage argv + `[selection]` draft + optional baseline run, gated in `cli._run_changed_setup` / `_report_existing_changed`) is unchanged. Only its strings change (§4.8.4). | It still configures the AUTOMATIC/`--shadow` engine. Graph selection needs none of it. |

## 3. Task split (re-scoped so all three are parallel-safe)

The triage's T1 (impact) and T2 (wiring) cannot run in parallel: the wiring imports `ptest.impact`
and its tests need the real graph. They are merged into **T1**. **T2** is re-scoped to the CLI
help text + README; **T3** keeps the agent guide / skill / own config. No task imports anything
another task writes; there is no shared-file edit.

| Task | Owns (exclusive) |
|---|---|
| **T1** impact + wiring | NEW `src/ptest/impact.py`, NEW `tests/ng/test_impact.py`, NEW `tests/ng/test_changed_default.py`; edits `src/ptest/cli.py`, `src/ptest/operations.py`, `src/ptest/progress.py`, `src/ptest/contracts.py`, `src/ptest/monorepo.py`, and the legacy tests the routing change breaks: `tests/ng/test_monorepo_changed.py`, `tests/ng/test_changed_explain.py`, `tests/ng/test_natural_loop.py`, `tests/ng/test_run_output.py`, `tests/ng/test_operations.py`, `tests/ng/test_pytest_adapter.py`, `tests/ng/test_pytest_scoped_subprocess.py`, `tests/ng/test_monorepo_snapshot.py`, `tests/ng/test_shadow.py`, `tests/ng/test_files.py`, `tests/ng/test_parallel_output_cli.py`, `tests/ng/test_cli.py` (only if a routing assertion breaks) |
| **T2** help + README | `src/ptest/help.py`, `README.md`, `tests/ng/test_help.py` |
| **T3** guide + skill + config default | `src/ptest/resources/repository-agent-guide.md`, `src/ptest/agent_rules.py`, `src/ptest/uninstall.py` (the `_decide_skill` tuple only), `src/ptest/config.py` (the `_fresh_config` `enabled=` literal only), `src/ptest/init_changed.py` (string constants + module docstring only), `src/ptest/doctor_fix.py` (the §4.8.2 flip only), `.ptest.toml`, `tests/ng/test_agent_rules.py`, `tests/ng/test_resources.py`, `tests/ng/test_init_changed.py`, `tests/ng/test_uninstall.py` (one new test only), `tests/ng/test_init.py`, `tests/ng/test_config.py`, `tests/ng/test_doctor_fix.py` |

`tests/ng/test_init_changed.py` belongs to T3 alone. Its help/README assertions
(`test_getting_started_shows_changed`) pin T2's wording, which T3's worktree never sees, so the
assertions are split: T3 deletes that one test from `test_init_changed.py`, and T2 re-adds the
same checks with the new wording (frozen in §4.7) as a new test in `tests/ng/test_help.py`. Each
task's scoped-green command then runs every assertion that pins the wording that task changes.
Nobody but T3 touches `src/ptest/config.py`, `init_changed.py`, `doctor_fix.py`, `uninstall.py` or
their tests.

The `ptest doctor --fix` next-step line is printed by `cli._run_doctor_fix` (T1). It is split the
same way: T1 changes the printed string and pins it in T1's `tests/ng/test_changed_default.py`
(§4.8.3). T3 deletes the old-string assertion at `tests/ng/test_doctor_fix.py:322` and asserts no
next-step wording, because T3's worktree still prints the old line. T1 edits only that one `print`
in `_run_doctor_fix`. T1 does not touch `_report_existing_changed` or `_run_changed_setup`, and T3
does not change `doctor_fix.selection_enabled_by`, so the merged code prints the new line for both
the `--cov` flip and the new §4.8.2 flip.

## 4. FROZEN INTERFACES

### 4.1 `src/ptest/impact.py` (T1, new)

```python
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from . import contracts as C

MAX_SCAN_FILES = 20000          # more .py files under the project -> full "import graph too large"
MAX_FILE_BYTES = 2 * 1024 * 1024  # larger files are skipped (a changed one -> "could not be parsed")
MAX_SELECTED = 200              # RunRequest.argv caps at 256 tokens

@dataclass(frozen=True, slots=True)
class Base:
    sha: str | None   # None = compare against HEAD (uncommitted work only)
    label: str        # "origin/dev", "main", "HEAD", or the --base REF text as typed

@dataclass(frozen=True, slots=True)
class Impact:
    kind: str                     # "none" | "selected" | "full" | "vitest"
    changed: tuple[str, ...] = () # project-relative; relevant paths first (sorted), then ignored ones (sorted)
    files: tuple[str, ...] = ()   # selected test files, project-relative, sorted (kind "selected" only)
    direct: int = 0               # changed test files among files
    via: int = 0                  # files reached through importers; direct + via == len(files)
    total: int = 0                # test files in the project (kind "selected" only; else 0 allowed)
    reason: str = ""              # kind "full" only; exact words from 4.2

def git_top(start: Path) -> Path | None
    # `rev-parse --show-toplevel` via monorepo._git_blob; None when not a work tree / git unusable.

def resolve_base(top: Path | None, explicit: str | None) -> Base
    # D2. explicit with top None -> C.Problem(code="invalid-config",
    #   message="--base needs a git repository", phase="config", retryable=False).
    # explicit empty / starting "-" / containing NUL / not `rev-parse --verify --quiet REF^{commit}`
    #   / no merge-base -> C.Problem(code="invalid-config",
    #   message="--base is not a commit in this repository", phase="config", retryable=False).

def changed_files(top: Path | None, base: Base) -> tuple[str, ...] | None
    # None when top is None; else monorepo.worktree_changed_files(top, base.sha).

def plan(top: Path | None, project_root: Path, config: C.Config,
         repo_changed: tuple[str, ...] | None) -> Impact
```

`plan` algorithm (order matters; first full wins, iterating paths sorted):

1. `top is None` or `repo_changed is None` or `project_root` not inside `top` → `Impact("full", reason="git changes are unavailable")`.
   `prefix` = project_root relative to top as posix (`""` for the root).
2. `changed` = paths under prefix, made project-relative. Outside paths are only checked for the
   ancestor-trigger rule (D9): dirname is `""` or an ancestor of prefix, and basename is a trigger
   basename → full `"<repo-relative path> is a full trigger"`.
3. Nothing under prefix and no ancestor trigger → `Impact("none")`.
4. Trigger (any runner): basename in `{uv.lock, poetry.lock, Pipfile.lock, pyproject.toml, setup.py,
   setup.cfg, pytest.ini, tox.ini, conftest.py, package.json, package-lock.json, pnpm-lock.yaml,
   yarn.lock, .ptest.toml}`, or basename matching `requirements*.txt`, or basename starting with
   `vitest.config.` / `vite.config.` / `jest.config.`, or `selection._matches(path,
   config.selection.full_triggers)` → full `"<path> is a full trigger"`.
5. Ignored (not relevant): any path component starting with `.`; suffix `.md` `.rst` `.txt`;
   basename in `{LICENSE, CODEOWNERS}`; `selection._matches` against `selection.no_tests`,
   `selection.ignored_inputs`, `selection.non_input_outputs`. No relevant path left →
   `Impact("none", changed=...)`.
6. Runner vitest → `Impact("vitest", changed=...)`.
7. Runner not pytest → full `"<runner kind value> has no import graph"` (e.g. `command has no import graph`).
8. `config.selection.enabled is False` → full `"selection is off in .ptest.toml — set [selection] enabled = true"`.
9. Classify each relevant path:
   - not `.py` → full `"<path> is outside the import graph"`;
   - **test file** = basename `test_*.py` or `*_test.py` AND under a test root
     (`path == root or path.startswith(root + "/")`; root `"."` matches everything): existing → direct; deleted → skip;
   - **test support** = other `.py` under a test root (root `"."` counts only for paths with a
     `tests` or `test` directory component) → full `"<path> is test support"`;
   - existing file unreadable / > MAX_FILE_BYTES / `SyntaxError` / `ValueError` from `ast.parse` → full `"<path> could not be parsed"`;
   - otherwise **source** (existing or deleted) → seed for the graph.
10. Graph (only when there are seeds, D4): walk `project_root` (no symlink follow; skip dir names
    starting `.`, `node_modules`, `__pycache__`, `site-packages`); > MAX_SCAN_FILES `.py` → full
    `"import graph too large"`. Unchanged files that fail to read/parse are skipped silently.
    - Module names of a file `a/b/c.py` (or `a/b/__init__.py` → `a/b`): dotted path from
      project root; the same without a leading `src.`; and the dotted path from its package root
      (first ancestor directory without `__init__.py`).
    - Edges from every `ast.Import` / `ast.ImportFrom` anywhere in the file (`ast.walk`, so
      function-level imports count): `import a.b.c` → names `a`, `a.b`, `a.b.c`;
      `from a.b import c` → `a`, `a.b`, `a.b.c`; relative `from ..x import y` resolved against the
      file's package-root name (an `__init__.py` is its own package).
    - Reverse index: name → importing files. BFS from the seeds' names; each reached file adds
      its own names to the queue; reached test files (definition above) are selected `via`
      unless already `direct`.
11. `files` = sorted(direct ∪ via). Empty → `Impact("none", changed=...)`.
    `len(files) >= full_ratio * total` → full `"<n> of <total> test files reaches full_ratio <ratio:g>"`.
    `len(files) > MAX_SELECTED` → full `"<n> test files exceed the 200-file scoped limit"`.
    Else `Impact("selected", changed, files, direct, via, total)`.

`total` = count of existing test files under the test roots (walk, same skips).

### 4.2 Impact reason strings (exact; T2/T3 quote them in docs)

`git changes are unavailable` · `<path> is a full trigger` · `<runner> has no import graph` ·
`selection is off in .ptest.toml — set [selection] enabled = true` · `<path> is outside the import graph` ·
`<path> is test support` · `<path> could not be parsed` · `import graph too large` ·
`<n> of <total> test files reaches full_ratio <ratio>` · `<n> test files exceed the 200-file scoped limit`

### 4.3 `contracts.RunRequest` (T1) — two new trailing fields

```python
    changed_note: str | None = None   # start-line text after "ptest: <project> · "
    next_hint: bool = False           # append the next-step hint to this run's end line
```
Validation in `__post_init__`: `changed_note` → `_check_str("request.changed_note", ...)` when not
None; `next_hint` → `_check_bool("request.next_hint", ...)`.

### 4.4 `progress.py` (T1) — additions

```python
NEXT_FULL = "next: ptest --full before handoff"
NEXT_FIX = "fix the code under test, then rerun ptest"

def next_step(status: C.Status, narrowed: bool) -> str | None
    # FAILED -> NEXT_FIX; PASSED/NO_TESTS_NEEDED and narrowed -> NEXT_FULL; else None
def format_impact(project: str, note: str, *, color: bool = False) -> str
    # f"{_prefix(color)} {_project(project, color)} · {note}"
def format_nothing_changed(label: str, *, color: bool = False) -> str
    # f"{_prefix(color)} no changes vs {label} — nothing to test · ptest --full runs everything"
def format_end(status, *, counts, duration_s, exit_code, hint=False, lead=None,
               color=False, next_step: str | None = None) -> str
    # after "(exit N)": if next_step: " · {next_step}" (the -v HINT is then omitted); elif hint: " · {HINT}"
```
Notes are built in `cli.py` (private `_impact_note(impact, label) -> str`), escaped with
`render.terminal_text` per path/label.

### 4.5 Output lines (exact)

| Case | Line (stderr) |
|---|---|
| selected | `ptest: api · changed: services/credits.py (+2 files) → 34 of 704 test files (3 direct · 31 via importers)` |
| full | `ptest: api · changed → full suite: uv.lock is a full trigger` |
| vitest | `ptest: web · changed: src/a.ts (+1 file) → vitest --changed origin/dev` |
| none, some changes (docs/unimported) | `ptest: api · changed: README.md → no tests affected · ptest --full runs everything` |
| none, monorepo child untouched | `ptest: web · no changes` (existing `format_no_changes`) |
| nothing changed anywhere | `ptest: no changes vs origin/dev — nothing to test · ptest --full runs everything` (exit 0) |
| end, narrowed pass | `ptest: passed · 12 tests · 1.2s · next: ptest --full before handoff` |
| end, failure | `ptest: failed · 1 failed, 11 passed · 1.2s (exit 1) · fix the code under test, then rerun ptest` |
| end, changed-mode full pass / explicit `--full` | unchanged end line, no hint |

`(+N files)`: omitted when exactly one changed path; `+1 file` singular. The shown path is
`impact.changed[0]`. Project = `checkout.root.name` in operations (start line); declaration or
`resolution.root.name` for lines cli prints itself.

### 4.6 Request mapping (T1, cli)

Route when `parsed.mode is C.Mode.AUTOMATIC and not parsed.shadow and parsed.probe is None`
(single project) / the existing `parsed.changed or parsed.mode is C.Mode.AUTOMATIC` branch
(monorepo root). Base and changed set are computed ONCE per invocation from
`impact.git_top(resolution.root)`.

| Impact.kind | mode | argv | base | changed_note | next_hint |
|---|---|---|---|---|---|
| selected | SCOPED | `impact.files` | None | note | True single / False child |
| vitest | SCOPED | `("--changed", base.sha or "HEAD")` | None | note | True single / False child |
| full | FULL | `()` | None | `"changed → full suite: " + reason` | True single / False child |
| none | not executed | — | — | — | — |

All other request fields carry over from `parsed` as today (workers, queue_timeout_s, timeout_s,
no_setup, result_path, fixture_domain, verbose, quiet). Monorepo total line:
`next_step(worst_status, narrowed)` where narrowed = any child ran SCOPED or was `none` with
changes; children never carry the hint (`next_hint=False`). If every child is `none` with no
changes → only `format_nothing_changed`, no per-child lines, no total, exit 0.

operations (T1): `_emit_start` — when `request.changed_note is not None` emit
`progress.format_impact(project, request.changed_note)` (+ the existing `-v plan` line) and return.
`_emit_end` — `next = progress.next_step(result.status, request.mode is C.Mode.SCOPED) if request.next_hint else None`; claim the `-v` hint only when `next` is None.

### 4.7 Frozen doc wording (the exact substrings the tests assert)

T2 (asserted by T2's new test in `tests/ng/test_help.py`):
- `help.overview()` contains `default loop: tests your change reaches (= --changed)` (overview line 18,
  keep the `#` column alignment).
- `help.topic("agents")` contains `bare ptest runs the tests your change reaches`.
- `README.md`, whitespace-normalised (`" ".join(text.split())`, so line wrapping does not matter),
  contains `` `ptest` runs the tests your change reaches ``.

T3 (asserted by T3 in `tests/ng/test_init_changed.py`):
- Guide row, exact line: ``| After each edit | `ptest` (bare `ptest` runs the tests your change reaches: git diff vs the branch base, no baseline or coverage needed) |``
- Guide keeps ``| Integrated change, before handoff | `ptest --full` once |``, keeps `baseline recorded`, stays ≤ 100 lines, and no longer contains `the first run records a baseline`.
- `_provider_text("claude")` contains `` `ptest` after each edit runs the tests your change reaches `` and `` `ptest --full` once before handoff `` and `docs/ptest-agent.md`.

T2 (asserted by T2 in `tests/ng/test_help.py`):
- `help.topic("init")` (the paragraph at help.py:84-90), whitespace-normalised, contains
  `Graph selection (bare ptest) is on in every new config and needs neither.` and no longer contains
  `no leaves selection off`.

### 4.8 Config default, `doctor --fix` flip, and setup wording

#### 4.8.1 Generator (T3, `config._fresh_config`)
Only this literal changes: `enabled=False` becomes `enabled=kind is C.RunnerKind.PYTEST`. `closed_inputs`
stays `False`, and the parse default for a missing `[selection]` table or `enabled` key stays `False`
(config.py:402/482 unchanged). Vitest, go and cargo configs keep `enabled = false`. Only pytest reads
the flag for graph selection (D5).

#### 4.8.2 `doctor --fix` flip (T3, `doctor_fix.plan_project`)
- The existing `--cov` branch (doctor_fix.py:958-980) is unchanged. `--cov` present plus a table that
  still needs closing gives the enabled flip plus the closed_inputs/input_roots/full_triggers/groups draft,
  exactly as today.
- New, placed after that branch: if the runner is PYTEST, no `("selection", "enabled")` change is planned
  yet, and `_generator_default_off(parsed.get("selection"))` is true, then append
  `FieldChange("selection", "enabled", True)` (`draft=False`). Nothing else is appended: there is no cov
  gate and no draft. A generated table has every key already, so `_apply_changes` rewrites the
  `enabled = false` line in place.
- Discriminator, a new private function `_generator_default_off(selection: object) -> bool`. It is true
  exactly when:
  1. `selection` is a dict whose key set is EXACTLY `{enabled, closed_inputs, input_roots,
     ignored_inputs, environment, full_triggers, always, no_tests, non_input_outputs, full_ratio}`,
     the ten keys `config._serialize_fresh` writes;
  2. `enabled is False` and `closed_inputs is False`;
  3. `input_roots`, `ignored_inputs`, `environment`, `full_triggers`, `always` and `no_tests` are each `[]`;
  4. `non_input_outputs` is `[]` or `[".venv"]`;
  5. `full_ratio == 0.7`.
- Anything else counts as an explicit user choice, and doctor proposes nothing for it. That covers a
  missing `[selection]` table, a table missing any of the ten keys (for example a hand-written
  `[selection]\nenabled = false`), any extra key (for example `groups`), and any non-default value (for
  example `full_ratio = 0.5`).
- Accepted limitation, stated in the function docstring: a user who deliberately typed `enabled = false`
  into an otherwise untouched generated table cannot be told apart from a generator default. For such a
  table, `ptest doctor` reports "config is out of date" and `doctor --fix` flips it. Changing or deleting
  any other `[selection]` line makes the choice durable.
- `selection_enabled_by`, `_selection_needs_close`, `_selection_key_is_default` and `_has_cov` are unchanged.

#### 4.8.3 `doctor --fix` next-step line (T1, `cli._run_doctor_fix`)
When `doctor_fix.selection_enabled_by(plan)` is true after the apply step, the line
`run a parallel full baseline once to record a baseline` is replaced by exactly:
`selection enabled: bare ptest now runs the tests your change reaches (no baseline needed); run ptest --full once before handoff`
Nothing else in `_run_doctor_fix` changes.

#### 4.8.4 `init --changed-setup` strings (T3, `init_changed.py` constants; placeholders, `CHOICES`, `DEFAULT_CHOICE`, `COV_ARGV` and all functions unchanged)
| Constant | Exact new value |
|---|---|
| `QUESTION` | `Set up the optional coverage engine (ptest --shadow) for {project}? Bare ptest already runs the tests your change reaches without it. Adds coverage to test runs; needs one full run as a baseline. [now/later/no] (default: later)` |
| `NEEDS_COV_LINE` | `{project}: the optional coverage engine (ptest --shadow) needs pytest-cov; bare ptest does not` |
| `LATER_LINE` | `{project}: coverage engine drafted; its baseline is recorded on the first passing --full on a clean tree` |
| `NOW_LINE` | `{project}: coverage engine drafted; running ptest --full for the baseline` |
| `EXISTING_LINE` | `{project}: selection is off in this config — set [selection] enabled = true, or run ptest doctor --fix` |
| `DRY_RUN_LINE` | `would set up the coverage engine for {project}: write --cov/--cov-report plus the [selection] draft; the baseline is recorded on the first passing --full on a clean tree` |

None of these strings contains `ptest --changed`.

## 5. Acceptance criteria

### T1 — impact + wiring
- [ ] `impact.py` matches 4.1 exactly (names, fields, order of checks, reason strings 4.2); no new dependency; `from .monorepo import _git_blob, worktree_changed_files` and `from .selection import _matches` are the only git/policy helpers used.
- [ ] `tests/ng/test_impact.py` (real git repos via `support.init_git_repo`/`git`, `tmp_path`) covers: default base on a feature branch (merge-base vs `main`), `origin/HEAD` preferred (`git update-ref refs/remotes/origin/main HEAD` + `git symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/main`), on the default branch → `Base(None,"HEAD")`, explicit `--base`, bad ref → Problem; changed set includes committed-since-base, staged, unstaged, untracked; plan: direct test, transitive via importer (test→a→b, change b), relative import, `src/` layout, package `__init__`, deleted source, function-level import, each trigger family (lockfile, nested `conftest.py`, `.ptest.toml`, ancestor root trigger for a child prefix), test support, unparsable changed file, non-`.py` file, docs/hidden-only → none with changed, full_ratio, `MAX_SELECTED` (monkeypatch small), selection disabled, vitest, command runner, `repo_changed=None`, sibling-child change → none.
- [ ] Bare `ptest` and `ptest --changed` route per 4.6 for standalone and monorepo root; output per 4.5; `--full`, scoped paths, `--shadow`, `--probe` unchanged.
- [ ] `tests/ng/test_changed_default.py` (in-process `ptest.cli.main`, `operations.execute` monkeypatched to capture requests, as in `test_natural_loop.py`) asserts: requests + notes for selected / full / vitest; nothing-changed line + exit 0 + no execute; monorepo per-child lines and total hint; real `_emit_end` hint wording for pass/fail via `progress.format_end(next_step=...)` and `operations._emit_end` with `next_hint`.
- [ ] `cli._run_doctor_fix` prints the §4.8.3 line in place of `run a parallel full baseline once to record a baseline` (that one `print` is the only `_run_doctor_fix` change). `tests/ng/test_changed_default.py` pins it in-process. It monkeypatches `cli.doctor_fix.plan_all` to return a `doctor_fix.FixPlan` whose single `FileFix` carries `FieldChange("selection", "enabled", True)`, `cli.doctor_fix.render_diff` to return `""`, and `cli.doctor_fix.apply_plan` to return `(".ptest.toml",)`. It runs `main(("doctor", "--fix"))` with cwd set to a `tmp_path` holding a minimal pytest config (`from support import write_ptest_toml`) and asserts exit 0, the exact §4.8.3 line in stdout, and `record a baseline` not in stdout. `grep -rn "record a baseline" src/ptest/cli.py` is empty.
- [ ] Dead code in D10 deleted; `grep -rn "child_baseline_heads\|select_changed_children\|child_changed_request" src tests` is empty.
- [ ] Legacy tests keep their intent and assertions: tests that used CLI `--changed` to drive the AUTOMATIC engine switch to in-process `operations.execute(domain, config, C.RunRequest(mode=C.Mode.AUTOMATIC, ...))` (pattern: `test_operations._execute`) and keep every assertion (translated from JSON dict to `RunResult` fields); tests that only needed "run the project" keep bare if still green, else pass `--full`; tests that committed a change on `main` and expect it to count create a feature branch first (on the default branch only uncommitted work counts).
- [ ] Scoped green: `.venv/bin/ptest --workers 2 tests/ng/test_impact.py tests/ng/test_changed_default.py tests/ng/test_monorepo_changed.py tests/ng/test_changed_explain.py tests/ng/test_natural_loop.py tests/ng/test_run_output.py tests/ng/test_operations.py tests/ng/test_pytest_adapter.py tests/ng/test_pytest_scoped_subprocess.py tests/ng/test_monorepo_snapshot.py tests/ng/test_shadow.py tests/ng/test_files.py tests/ng/test_parallel_output_cli.py tests/ng/test_cli.py`.

### T2 — help + README
- [ ] `help.py`: overview line 18 and the execution section say bare `ptest` = `--changed` = tests reached by the change vs the branch base (merge-base with origin/HEAD, else main/master/dev; on the default branch only uncommitted work), no baseline or coverage needed; `--base REF` compares against the merge-base with REF; the loop section drops "The first loop run may run everything to record a baseline" and says `ptest --full` once before handoff. In the `init` topic, the `--changed-setup` paragraph (help.py:84-90) describes it as the optional coverage engine for `ptest --shadow` and replaces `no leaves selection off` per §4.7 T2. The syntax line `[--changed-setup now|later|no]` and the `now`/`later` descriptions stay.
- [ ] `README.md` loop wording matches (no claim that `--changed` needs a baseline or coverage).
- [ ] Wording matches §4.7 (T2) exactly.
- [ ] `tests/ng/test_help.py` updated/extended for the new sentences, including a new `test_getting_started_shows_graph_default` that asserts the three §4.7 T2 substrings and a new `test_init_help_changed_setup_is_optional` that asserts the §4.7 T2 `init`-topic substring and absence (it replaces `test_init_changed.py::test_getting_started_shows_changed`, which T3 deletes; T2 does not edit `test_init_changed.py`). `.venv/bin/ptest --workers 2 tests/ng/test_help.py` green.

### T3 — guide + skill + config default
- [ ] `repository-agent-guide.md`: loop table row "After each edit" says bare `ptest` runs the tests the change reaches (git diff vs branch base; no baseline or coverage needed); "Reading ptest output" rows replaced/added for every line in 4.5 and the reasons in 4.2 (what to do: nothing for selected/full/none; `ptest --full` once before handoff after `next:`; fix code after failure); baseline rows stay (they describe `--full`). Exit-code table unchanged.
- [ ] `agent_rules.py`: sha256 of the base-commit guide bytes (`2c9ec366c57d69f24053268632132915938098f1ff540dade0963b8206667fe7`, last changed in `5467c52`) added to `_PREVIOUS_GUIDE_SHA256S` with a comment; the current `_provider_text` bytes frozen into a new `_pre_graph_provider_text(provider)` (same pattern as `_pre_rewrite_provider_text`) and added to the recognised tuple; new `_provider_text` loop line: "`ptest` after each edit runs the tests your change reaches (git diff vs the branch base; no baseline or coverage needed); `ptest <path>` runs one test file; `ptest --full` once before handoff runs the integrated gate." Body stays within fifteen lines.
- [ ] `uninstall._decide_skill`: `agent_rules._pre_graph_provider_text(provider)` added to its managed tuple (it keeps its own list, separate from `agent_rules._provider_target`). New test in `tests/ng/test_uninstall.py`: a repo whose `.claude/skills/ptest/SKILL.md` holds `_pre_graph_provider_text("claude")` bytes → `ptest uninstall` plans that file as REMOVE "managed skill" (not KEPT "edited"), and it is gone after `--yes`.
- [ ] `tests/ng/test_init_changed.py`: `test_repository_guide_default_loop_is_changed` and `test_skill_template_defaults_to_changed` assert the §4.7 T3 wording (the `the first run records a baseline` assertion is dropped because that row is replaced); `test_getting_started_shows_changed` is deleted (T2 re-adds it in `test_help.py`); every other assertion is kept, apart from the default-flip and string updates listed below.
- [ ] Generator (§4.8.1): `config._fresh_config` changes only its `enabled=` literal. The five measured assertions that break change as follows (measured in a throwaway worktree at 8f2640b with the literal flipped for every runner plus a prototype of §4.8.2: `ptest --full --workers 2` failed on exactly these five and nothing else, and prototypes of the (b)/(c) tests below passed):
  1. `test_init.py::test_init_creates_one_fresh_config_exclusively`: `selection.enabled is False` becomes `is True`.
  2. `test_init_changed.py::test_changed_setup_no_leaves_selection_off`: rename it to `test_changed_setup_no_writes_no_coverage`. `_selection(...).get("enabled") is not True` becomes `is True`. The `--cov` absence and `calls == []` assertions stay.
  3. and 4. `test_init_changed.py::test_changed_setup_preview_matches_real_run[True-later]` and `[True-now]`: in the `existing` branch, after the first `init --changed-setup no`, rewrite `enabled = true` to `enabled = false` in `.ptest.toml` (this simulates a 0.2.x config) and only then capture `before`. All assertions stay, including `EXISTING_LINE` for preview and real run and the byte-identical config.
  5. `test_init_changed.py::test_changed_setup_existing_config_points_to_doctor_fix`: the same `enabled = false` rewrite before `before`. The `doctor --fix` and byte-identical assertions stay.
  Also add `enabled = true` for a fresh pytest config and `enabled = false` for a fresh vitest config, as assertions in `tests/ng/test_config.py` or `test_init.py`.
- [ ] Strings (§4.8.4): `init_changed.py` constants equal §4.8.4 byte-for-byte. `test_init_changed.py` string assertions follow them: lines 140/170 assert `Set up the optional coverage engine (ptest --shadow) for`; line 226 asserts the exact new `NEEDS_COV_LINE` text for project `.`; line 251 asserts `Set up the optional coverage engine` not in stderr; `_CHANGED_MARKERS` becomes `("coverage engine", "selection is off in this config", "would set up", "baseline")`. A new test asserts that no constant in `(QUESTION, NEEDS_COV_LINE, LATER_LINE, NOW_LINE, EXISTING_LINE, DRY_RUN_LINE)` contains `ptest --changed`.
- [ ] `doctor --fix` flip (§4.8.2) in `tests/ng/test_doctor_fix.py`:
  (a) `test_fix_proposes_selection_draft_with_cov`: delete only the line-322 assertion `run a parallel full baseline once to record a baseline` (T1 pins the new line). All other assertions stay.
  Both new tests below define a module constant `_GENERATED_SELECTION`: the ten `[selection]` lines exactly as 0.2.x `_serialize_fresh` writes them for a uv pytest project (`enabled = false`, `closed_inputs = false`, six `= []` lists, `non_input_outputs = [".venv"]`, `full_ratio = 0.7`). They write it with `_write_config(root, args=("-n", "0"), extra_lines=("", "[selection]", *lines))` on `_write_pytest_project(...)`, the same setup as `test_fix_second_run_reports_up_to_date`. The assertions check the parsed `selection` table, not the whole file, so unrelated runner/setup fixes cannot mask the result.
  (b) Generator-shaped, no `--cov`: `doctor --fix --dry-run` output contains `enabled = true` and does not contain `closed_inputs = true`, and the file bytes are unchanged. After `doctor --fix`, parsed `selection["enabled"] is True`, the other nine keys equal their written values, and there is no `groups` key. A second `doctor --fix` prints `config is up to date`.
  (c) Explicit choices, parametrised, no `--cov`: a hand-written `[selection]` + `enabled = false` only; `_GENERATED_SELECTION` with `full_ratio = 0.5`; `_GENERATED_SELECTION` plus `groups = []`; `_GENERATED_SELECTION` without its `always = []` line. After `doctor --fix`, the parsed `selection` table equals the table before the run (`enabled` stays `False`).
  (d) The existing `test_fix_preserves_unmanaged_settings_byte_for_byte` stays green unchanged, so it still asserts `enabled = false` is kept.
- [ ] `.ptest.toml` (ptest's own): `[selection] enabled = true`; nothing else.
- [ ] `.venv/bin/ptest --workers 2 tests/ng/test_agent_rules.py tests/ng/test_resources.py tests/ng/test_init_changed.py tests/ng/test_uninstall.py tests/ng/test_config.py tests/ng/test_init.py tests/ng/test_doctor_fix.py` green (including `test_every_shipped_guide_version_hashes_into_previous_set`).

## 6. Test approach
- Every task: `uv sync --locked --extra test` in its own worktree first; run only through that worktree's `.venv/bin/ptest`, always `--workers 2` (the machine was saturated: an 8-minute slot wait was observed while designing). Never `--full` inside a task.
- Fixtures: `tmp_path` repos, `support.init_git_repo(branch="main")`, `support.git`; no network, no sleeps, no fixed ports; ≥ 4 test files in graph fixtures so `full_ratio` 0.7 does not mask selection.
- Controller, after merge: one `.venv/bin/ptest --full --workers 2`, then the persea smoke test.

## 7. Risks
| Risk | Mitigation |
|---|---|
| Fixture-/conftest-mediated dependencies are not edges → under-selection (e.g. FastAPI `client` fixture) | End line always says `next: ptest --full before handoff`; guide states it. |
| Persea smoke: `.ptest.toml`, `api/.ptest.toml`, `web/.ptest.toml` are UNTRACKED there, so every bare run is "changed → full suite: .ptest.toml is a full trigger" | Controller: smoke in a scratch persea worktree (`~/worktrees/persea/cc-changed-smoke/...`) with those configs committed on a scratch branch, touch one service file, run bare `ptest`. Never commit in the user's checkout. |
| AST of newer Python syntax than ptest's interpreter | Changed file → full "could not be parsed"; unchanged file skipped. |
| Legacy test churn in T1 | Explicit ownership list; intent-preserving migration rules in T1 criteria. |

## 8. Open questions (for the controller, not blocking)
- None. The generator flip and the `--changed-setup` rewording are decided in D6/§4.8.

## Deviations from the context pack
1. Tasks re-scoped (T1 absorbs wiring; T2 = help/README; T3 = guide/skill/own config) — the original T1/T2 were silently sequential.
2. Generator default flipped for pytest, and `doctor --fix` flips generator-shaped `enabled = false` tables without `--cov` (D6, §4.8).
3. `RunRequest` gains two fields (`changed_note`, `next_hint`), not one — children must not print the hint.
4. Explicit `--base` also uses the merge-base (D2); hidden-path and docs ignore list added (step 5); none-with-changes line added.

## Controller amendment (folded in)
The controller reversed the original D6: changed-by-default must hold even with no parameters. The
amendment is now spelled out in D6, D11, §3, §4.7 (T2 `init` topic), §4.8 and the §5 T1/T2/T3 criteria,
and nothing outside those sections restates it.

## Revision 1 (review findings)
- `tests/ng/test_init_changed.py` is owned by T3 alone (§3). Its help/README test moves to T2's `test_help.py`
  (§5 T2), and every doc substring the tests assert is frozen in §4.7, so each task's scoped green covers
  the wording that task changes.
- T3 also owns `src/ptest/uninstall.py` (`_decide_skill` tuple) and one new test in `tests/ng/test_uninstall.py`,
  so a 0.2.5 `SKILL.md` (`_pre_graph_provider_text`) is still removed by `ptest uninstall`.

## Revision 2 (review findings)
- The "generator-default" discriminator is now concrete (§4.8.2 `_generator_default_off`). Tables with no
  `--cov` that pass it get only the `enabled` flip, with no draft. The `--cov` branch is unchanged, and
  anything else is treated as the user's choice. The accepted limitation is stated.
- The `--changed-setup` strings are frozen (§4.8.4), and so is the help `init` paragraph (§4.7 T2). The five
  broken assertions are named, with their exact updates, in §5 T3. The contradictions in D6, Deviation 2,
  §8 and T3 are removed.
- The `doctor --fix` next-step line is now owned by T1 (§4.8.3, `cli._run_doctor_fix`) and pinned in T1's
  `test_changed_default.py`. T3 only drops the old-string assertion at `test_doctor_fix.py:322`. This keeps
  ownership disjoint, and the merged output no longer tells users to record a baseline.
