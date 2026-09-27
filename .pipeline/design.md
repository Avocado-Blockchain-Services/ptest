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
| D6 | The config GENERATOR default stays `enabled = false`. Only ptest's own `.ptest.toml` flips to `enabled = true` (T3). | Measured: flipping `_fresh_config` breaks test_init (1) and test_init_changed (4) and changes the meaning of `init --changed-setup no`; out of KISS scope. The full-suite reason tells the user the exact one-line fix. Follow-up, see Open questions. |
| D7 | Impact `full` → `C.Mode.FULL` for every runner, `base=None` (pytest FULL rejects a base). No git evidence → full "git changes are unavailable". | Fail closed, one mapping, no legacy branch. |
| D8 | Nothing runs → no `--result-json` document is written, exit 0. | Nothing executed; KISS. |
| D9 | Monorepo: every child is planned against the same repo-level changed set. A path outside a child counts for that child only if it sits in an ancestor directory of the child AND its basename is a trigger basename (root `.ptest.toml`, root `uv.lock`, …). | Replaces the deleted `_classify` outside-trigger rule with one simple rule. |
| D10 | Deleted as dead: in `monorepo.py` → `ChangedChild`, `_COMMIT_RE`, `_child_baseline_head`, `child_baseline_heads`, `_committed_since`, `_run_all`, `select_changed_children`, `_classify`, `_vitest_base`, `child_changed_request` (and the `_matches` import if unused). Kept: `_git_blob`, `_repo_path`, `_nul_paths`, `worktree_changed_files`. `progress.format_changed_selected / format_changed_start / explain_changed_full_reason` and operations' AUTOMATIC start-line branch are KEPT (still reachable through `operations.execute` AUTOMATIC). | Spec: delete dead code, don't layer; but do not rip out the still-used engine. |
| D11 | `init --changed-setup` (coverage + baseline setup) is untouched. | Not asked; still configures the AUTOMATIC/--shadow engine. Wording follow-up in Open questions. |

## 3. Task split (re-scoped so all three are parallel-safe)

The triage's T1 (impact) and T2 (wiring) cannot run in parallel: the wiring imports `ptest.impact`
and its tests need the real graph. They are merged into **T1**. **T2** is re-scoped to the CLI
help text + README; **T3** keeps the agent guide / skill / own config. No task imports anything
another task writes; there is no shared-file edit.

| Task | Owns (exclusive) |
|---|---|
| **T1** impact + wiring | NEW `src/ptest/impact.py`, NEW `tests/ng/test_impact.py`, NEW `tests/ng/test_changed_default.py`; edits `src/ptest/cli.py`, `src/ptest/operations.py`, `src/ptest/progress.py`, `src/ptest/contracts.py`, `src/ptest/monorepo.py`, and the legacy tests the routing change breaks: `tests/ng/test_monorepo_changed.py`, `tests/ng/test_changed_explain.py`, `tests/ng/test_natural_loop.py`, `tests/ng/test_run_output.py`, `tests/ng/test_operations.py`, `tests/ng/test_pytest_adapter.py`, `tests/ng/test_pytest_scoped_subprocess.py`, `tests/ng/test_monorepo_snapshot.py`, `tests/ng/test_shadow.py`, `tests/ng/test_files.py`, `tests/ng/test_parallel_output_cli.py`, `tests/ng/test_cli.py` (only if a routing assertion breaks) |
| **T2** help + README | `src/ptest/help.py`, `README.md`, `tests/ng/test_help.py` |
| **T3** guide + skill + own config | `src/ptest/resources/repository-agent-guide.md`, `src/ptest/agent_rules.py`, `.ptest.toml`, `tests/ng/test_agent_rules.py`, `tests/ng/test_resources.py` |

Nobody touches `src/ptest/config.py`, `init_changed.py`, `tests/ng/test_config.py`,
`tests/ng/test_init.py`, `tests/ng/test_init_changed.py` (D6, D11).

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

## 5. Acceptance criteria

### T1 — impact + wiring
- [ ] `impact.py` matches 4.1 exactly (names, fields, order of checks, reason strings 4.2); no new dependency; `from .monorepo import _git_blob, worktree_changed_files` and `from .selection import _matches` are the only git/policy helpers used.
- [ ] `tests/ng/test_impact.py` (real git repos via `support.init_git_repo`/`git`, `tmp_path`) covers: default base on a feature branch (merge-base vs `main`), `origin/HEAD` preferred (`git update-ref refs/remotes/origin/main HEAD` + `git symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/main`), on the default branch → `Base(None,"HEAD")`, explicit `--base`, bad ref → Problem; changed set includes committed-since-base, staged, unstaged, untracked; plan: direct test, transitive via importer (test→a→b, change b), relative import, `src/` layout, package `__init__`, deleted source, function-level import, each trigger family (lockfile, nested `conftest.py`, `.ptest.toml`, ancestor root trigger for a child prefix), test support, unparsable changed file, non-`.py` file, docs/hidden-only → none with changed, full_ratio, `MAX_SELECTED` (monkeypatch small), selection disabled, vitest, command runner, `repo_changed=None`, sibling-child change → none.
- [ ] Bare `ptest` and `ptest --changed` route per 4.6 for standalone and monorepo root; output per 4.5; `--full`, scoped paths, `--shadow`, `--probe` unchanged.
- [ ] `tests/ng/test_changed_default.py` (in-process `ptest.cli.main`, `operations.execute` monkeypatched to capture requests, as in `test_natural_loop.py`) asserts: requests + notes for selected / full / vitest; nothing-changed line + exit 0 + no execute; monorepo per-child lines and total hint; real `_emit_end` hint wording for pass/fail via `progress.format_end(next_step=...)` and `operations._emit_end` with `next_hint`.
- [ ] Dead code in D10 deleted; `grep -rn "child_baseline_heads\|select_changed_children\|child_changed_request" src tests` is empty.
- [ ] Legacy tests keep their intent and assertions: tests that used CLI `--changed` to drive the AUTOMATIC engine switch to in-process `operations.execute(domain, config, C.RunRequest(mode=C.Mode.AUTOMATIC, ...))` (pattern: `test_operations._execute`) and keep every assertion (translated from JSON dict to `RunResult` fields); tests that only needed "run the project" keep bare if still green, else pass `--full`; tests that committed a change on `main` and expect it to count create a feature branch first (on the default branch only uncommitted work counts).
- [ ] Scoped green: `.venv/bin/ptest --workers 2 tests/ng/test_impact.py tests/ng/test_changed_default.py tests/ng/test_monorepo_changed.py tests/ng/test_changed_explain.py tests/ng/test_natural_loop.py tests/ng/test_run_output.py tests/ng/test_operations.py tests/ng/test_pytest_adapter.py tests/ng/test_pytest_scoped_subprocess.py tests/ng/test_monorepo_snapshot.py tests/ng/test_shadow.py tests/ng/test_files.py tests/ng/test_parallel_output_cli.py tests/ng/test_cli.py`.

### T2 — help + README
- [ ] `help.py`: overview line 18 and the execution section say bare `ptest` = `--changed` = tests reached by the change vs the branch base (merge-base with origin/HEAD, else main/master/dev; on the default branch only uncommitted work), no baseline or coverage needed; `--base REF` compares against the merge-base with REF; the loop section drops "The first loop run may run everything to record a baseline" and says `ptest --full` once before handoff. `init --changed-setup` text untouched.
- [ ] `README.md` loop wording matches (no claim that `--changed` needs a baseline or coverage).
- [ ] `tests/ng/test_help.py` updated/extended for the new sentences; `.venv/bin/ptest --workers 2 tests/ng/test_help.py` green.

### T3 — guide + skill + own config
- [ ] `repository-agent-guide.md`: loop table row "After each edit" says bare `ptest` runs the tests the change reaches (git diff vs branch base; no baseline or coverage needed); "Reading ptest output" rows replaced/added for every line in 4.5 and the reasons in 4.2 (what to do: nothing for selected/full/none; `ptest --full` once before handoff after `next:`; fix code after failure); baseline rows stay (they describe `--full`). Exit-code table unchanged.
- [ ] `agent_rules.py`: sha256 of the base-commit guide bytes (`2c9ec366c57d69f24053268632132915938098f1ff540dade0963b8206667fe7`, last changed in `5467c52`) added to `_PREVIOUS_GUIDE_SHA256S` with a comment; the current `_provider_text` bytes frozen into a new `_pre_graph_provider_text(provider)` (same pattern as `_pre_rewrite_provider_text`) and added to the recognised tuple; new `_provider_text` loop line: "`ptest` after each edit runs the tests your change reaches (git diff vs the branch base; no baseline or coverage needed); `ptest <path>` runs one test file; `ptest --full` once before handoff runs the integrated gate." Body stays within fifteen lines.
- [ ] `.ptest.toml` (ptest's own): `[selection] enabled = true`; nothing else.
- [ ] `.venv/bin/ptest --workers 2 tests/ng/test_agent_rules.py tests/ng/test_resources.py` green (including `test_every_shipped_guide_version_hashes_into_previous_set`).

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
- Flip the generator default (`config._fresh_config`) to `enabled = true` and reword `init --changed-setup` ("needs pytest-cov") in a follow-up? Measured ripple: test_init ×1, test_init_changed ×4.

## Deviations from the context pack
1. Tasks re-scoped (T1 absorbs wiring; T2 = help/README; T3 = guide/skill/own config) — the original T1/T2 were silently sequential.
2. Generator default not flipped (D6).
3. `RunRequest` gains two fields (`changed_note`, `next_hint`), not one — children must not print the hint.
4. Explicit `--base` also uses the merge-base (D2); hidden-path and docs ignore list added (step 5); none-with-changes line added.
