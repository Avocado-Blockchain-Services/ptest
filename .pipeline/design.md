# Design — worktree-safe ptest config (committed config only, no hidden fallback)

Date: 2026-09-29. Base: `feature/worktree-safe-config` @ 73f0707 (0.3.5).
Spec: `.pipeline/brief.md`. This document is authoritative over
`.pipeline/context-pack.md` where they differ; the differences are listed in
"Deviations from the context pack" at the end.

## 1. Decisions

D1. **No fallback.** When a checkout has no nearest `.ptest.toml`, ptest never
reads the main checkout's config to *run*. It only checks that the file exists
there so it can name it in the error. `ptest init --from-main` is the one
explicit path that reads main config bytes, and it only copies them. It never
parses them to execute anything.

D2. **Static linked-worktree detection** lives in the new module
`src/ptest/worktree.py`. It follows `.git` file → `gitdir:` → `<gitdir>/commondir`
→ common dir named `.git` → main root = parent of the common dir. It also checks
the back-link `<gitdir>/gitdir` == `<root>/.git`. All reads are bounded
`files.read_regular` reads. Every path component goes through the lstat
no-symlink walk. Detection never raises: any doubt returns `None`, so behavior
falls back to today's `initialization-required`. This fail-closed default keeps
bare repos, submodules (no `commondir`), broken or forged `.git` files, symlinked
layouts and non-git directories unchanged. No git subprocess is used for
detection.

D3. **Same relative path, nearest-first.** The main-checkout lookup repeats
`_find_config`'s walk: for each directory from cwd up to the worktree root,
nearest first, it checks `<main_root>/<same relative dir>/.ptest.toml`. The
first regular file wins. Unreadable, symlinked or oversize main files count as
absent.

D4. **Problem code `config-uncommitted`**, phase `config`, exit status 2. It
has one exact message (§3.3). The message does not mention `--from-main`,
because the agent instruction must be simply "stop, tell the user, never run
ptest init". The `--from-main` stopgap is documented for humans in help/README.

D5. **Where the code surfaces.** `resolve_config` returns it instead of
`initialization-required`, so every existing `raise resolution.problem` site
(bare run, run with runner args, `plan`, `history`, `doctor --fix`,
`doctor --probe`, and `doctor` through `inspect_workspace`) now emits it with no
per-site change. Explicit changes are limited to these:
- **path reroute:** the "missing" branch raises it instead of
  "run ptest init there".
- **init:** refuses before prompting.
- **register:** refuses through init's dry-run.
- **doctor:** refuses before the consent prompt. This replaces the old
  "run `ptest init`" limitation in this situation.
- **where:** exits 0 and emits a `warnings` entry plus a stderr line.
- **status:** does not read config, so it is out of scope.

D6. **Doctor in that situation fails hard (exit 2)** instead of scanning with a
limitation. A partial doctor scan would surface `executability`'s
"run ptest init from the repository root" fix text, which is exactly the advice
this feature must remove. `executability.py` is not owned by any task.

D7. **Tracked means "in HEAD's tree".** Check it with one `git ls-tree -r -z
--name-only HEAD -- <paths>` call through the existing hardened
`source._git`, with a 2 s `_Scan` deadline. Staged but uncommitted files still
count as uncommitted, which is correct because a new worktree will not have
them. An unborn HEAD, git failure, timeout or non-git directory returns `()`
and prints no warning. The check is best-effort and never raises.

D8. **`InitResult.commit_paths`** is the single source of truth for the commit
reminder. `config.init_project` fills it with the config files it *created*,
relative to the git root, and only inside a git checkout. `cli.py` extends it
with agent-rule files that `agent_rules.apply` created or updated, again only
inside a git checkout. The human footer (T3) and `init --json` both read
`result.commit_paths`. It is empty for preview, existing and `--from-main`
results: the copy must not be committed on the worktree branch, and the fix is
a commit on the base branch.

D9. **`init --json` gains optional `commit_paths`.** It follows the additive
`where.domain_root` precedent: the property appears in the schema but is not in
`required`, the validator accepts it if present, and the projector always emits
it (default `[]`). The schema file is generated from `contracts.PUBLIC_SCHEMAS`
by `scripts/export-schemas.py`, so its owner is the contracts owner (T1).

D10. **Run warning:** one stderr line per executing run, sent through
`progress.emit(..., quiet=parsed.quiet)`. It covers only `.ptest.toml` files:
the root one and, for a v2 manifest, the declared children. Agent-rule files
are not included. It is emitted only when a config or monorepo resolved, and it
is wrapped in `try/except Exception`.

D11. **Doctor report:** the file list includes agent-rule files. The
`config.uncommitted` `C.Finding`s (one per file) appear in the offline grid's
"Static findings". `cli.py` adds one mention line after doctor's terminal
output, like `_fix_mention`, so the online review also shows it.
`doctor --json` is unchanged: the agent-assessment schema carries no static
findings, and this avoids a schema change. The finding does not change
readiness.

D12. **`--from-main` copies bytes verbatim**, so the `project_id` is the same.
It copies the matched config and, if it is a v2 manifest, every declared
child's config that exists in main. It only uses `create_exclusive`, so it
never overwrites. Children whose directory is missing in the worktree are
skipped with a warning. A child config that already exists in the worktree is
skipped ("already present"). Main is only ever opened read-only.

## 2. Ordering (read this first)

Tasks run in parallel off the same base, but there is exactly one barrier:

- **T1 is a BARRIER for T2 and T3.** T2 and T3 import `ptest.worktree` and
  use `InitOptions.from_main`, `InitResult.commit_paths`,
  `REASON_CODES ∋ "config-uncommitted"`, `FINDING_CODES ∋ "config.uncommitted"`,
  `config.config_uncommitted` and `config.git_root`, all of which T1 creates.
  The orchestrator integrates T1 onto the chain branch first. T2 and T3 then
  branch from that tip.
- **T4 has no dependency and runs in wave 1 alongside T1.**
- **T2 and T3 are independent of each other.** T2 never calls a new
  `init_render` parameter; T3 reads `result.commit_paths` directly. Both run in
  wave 2.
- If T2 or T3 starts on a base without `src/ptest/worktree.py`, it must stop
  immediately and report `BLOCKED: T1 barrier not integrated`. It must not stub
  or re-implement T1's API.

Wave 1: T1, T4. Wave 2 (after T1 is integrated): T2, T3.

## 3. Frozen interfaces

### 3.1 `src/ptest/worktree.py` (new, T1)

```python
"""Static linked-worktree detection and committed-state checks for ptest files."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = ".ptest.toml"
# Agent-rule files ptest init may write; the three instruction files count
# only when they contain the managed marker below.
AGENT_RULE_FILES: tuple[str, ...] = (
    "docs/ptest-agent.md",
    "AGENTS.md", "CLAUDE.md", "GEMINI.md",
    ".agents/skills/ptest/SKILL.md", ".claude/skills/ptest/SKILL.md",
    ".gemini/skills/ptest/SKILL.md", ".opencode/skills/ptest/SKILL.md",
)
MANAGED_MARKER = "<!-- ptest-agent-rules:start -->"

@dataclass(frozen=True, slots=True)
class LinkedWorktree:
    root: Path       # linked worktree top: the directory holding the `.git` file
    main_root: Path  # main worktree top: parent of the common `.git` directory

@dataclass(frozen=True, slots=True)
class MainConfig:
    worktree: LinkedWorktree
    relative: str               # "." or posix "a/b": config directory relative to BOTH roots
    path: Path                  # main_root / relative / ".ptest.toml" (absolute, regular file)
    children: tuple[str, ...]   # v2 manifest declarations whose config exists in main; () if standalone

def linked_worktree(root: Path) -> LinkedWorktree | None: ...
def main_config(cwd: Path, root: Path) -> MainConfig | None: ...
def uncommitted_config_files(root: Path, *, include_agent_rules: bool = False) -> tuple[str, ...]: ...

__all__ = ["CONFIG_NAME", "AGENT_RULE_FILES", "MANAGED_MARKER", "LinkedWorktree",
           "MainConfig", "linked_worktree", "main_config", "uncommitted_config_files"]
```

Behavior (normative):

- `linked_worktree(root)`: `root` is the static git boundary (the directory
  that contains `.git`). Steps:
  1. `os.lstat(root/".git")` must be a regular file, not a symlink.
  2. Read it with `read_regular(root, ".git", 4097)`. More than 4096 bytes
     returns `None`.
  3. Decode as strict UTF-8. The content must be exactly one line
     `gitdir: <value>` (trailing `\n`/`\r\n` allowed), with no NUL or C0
     controls in `<value>`.
  4. `gitdir = normpath(value if absolute else root/value)`.
  5. Read `commondir` with `read_regular(gitdir, "commondir", 4097)`. If it is
     missing, return `None` (the submodule case).
  6. `common = normpath(gitdir/text)` if relative, else `text`.
  7. Back-link: `read_regular(gitdir, "gitdir", 4097)`, stripped and
     normalized (relative values resolve against `gitdir`), must equal
     `root/".git"`.
  8. `common.name == ".git"`, and `common` is a real directory. Otherwise
     (bare repo) return `None`.
  9. `main_root = common.parent`, with `main_root != root`.
  10. `gitdir`, `common` and `main_root` must all pass a no-symlink lstat walk
      from `/`. Reuse the logic of `files._check_no_symlink_prefixes` and catch
      its `Problem`.
  11. Any exception (`C.Problem`, `OSError`, `UnicodeError`, `ValueError`)
      returns `None`.
- `main_config(cwd, root)`:
  1. Return `None` unless `cwd == root` or `root in cwd.parents`, and
     `linked_worktree(root)` is not `None`.
  2. Walk `cursor` from `cwd` up to and including `root`. Set
     `rel = cursor.relative_to(root).as_posix()`, and name =
     `".ptest.toml"` if `rel == "."` else `f"{rel}/.ptest.toml"`.
  3. Use the first `main_root/name` that `read_regular(main_root, name,
     256*1024+1)` reads successfully with ≤ 256 KiB.
  4. `children`: if `tomllib` parses the file as `version == 2`, run
     `monorepo.parse_monorepo_manifest(raw, path)` (a lazy import). Keep each
     declared child whose `main_root/rel/child/.ptest.toml` is readable the
     same way. A parse `Problem` means `()`.
  5. Never raises; returns `None` on any error.
  6. Read-only.
- `uncommitted_config_files(root, *, include_agent_rules=False)`:
  1. Candidates are `".ptest.toml"` if it is a regular file (lstat, no
     symlink) under `root`.
  2. If it parses as a v2 manifest, add `f"{child}/.ptest.toml"` for each
     declared child whose file is a regular file.
  3. When `include_agent_rules`, add each entry of `AGENT_RULE_FILES` that
     exists as a regular file (`read_regular`, 256 KiB bound). `AGENTS.md`,
     `CLAUDE.md` and `GEMINI.md` count only if their bytes contain
     `MANAGED_MARKER`.
  4. If there are no candidates, return `()` without running git.
  5. Otherwise make exactly one call:
     `source._git(root, source._Scan(deadline=time.monotonic() + 2.0), "ls-tree",
     "-r", "-z", "--name-only", "HEAD", "--", *candidates)` (a lazy import).
     Output names are NUL-separated and relative to `root`.
  6. Return the candidates not in the output, in candidate order.
  7. Any exception, including `source._Unavailable`, returns `()`. Never
     raises and never writes.

### 3.2 `src/ptest/contracts.py` (T1)

Exact edits:

```python
# REASON_CODES: add one member (keep the frozenset otherwise unchanged)
    "config-uncommitted",
# FINDING_CODES: add one member
    "config.uncommitted",

@dataclass(frozen=True, kw_only=True)
class InitOptions:
    runner: RunnerKind | None
    dry_run: bool
    reveal_command: bool
    children: tuple = ()
    agents: tuple[str, ...] = ()
    from_main: bool = False          # NEW; validated with _check_bool("init.from_main", ...)

@dataclass(frozen=True, kw_only=True)
class InitResult:
    action: InitAction
    target: Path
    exists: bool
    config: ConfigSummary | None
    warnings: tuple = ()
    details: tuple = ()
    commit_paths: tuple = ()         # NEW; _as_str_tuple("init.commit_paths", ...);
                                     # each entry repo-relative posix, no leading "/", no ".." part (ValueError)

def serialize_init_result(result: InitResult) -> dict:
    return {
        "action": result.action.value,
        "target": str(result.target),
        "exists": result.exists,
        "warnings": [_reason_dict(item) for item in result.warnings],
        "config": _config_summary_dict(result.config),
        "commit_paths": list(result.commit_paths),
    }

# _validate_init_payload: append
    if "commit_paths" in data:
        _check_str_list(_need_list(data, "commit_paths"), "init.commit_paths")

# _project_init_payload: add key
        "commit_paths": list(data.get("commit_paths", [])),

# PUBLIC_SCHEMAS["init"] data properties: add (NOT in "required")
            "commit_paths": {"type": "array", "items": {"type": "string"}},
```

Regenerate the schemas with `uv run python scripts/export-schemas.py`. Only
`docs/schemas/v1/init.json` may change. Its exact expected bytes are in the
sharedFileContent field, also listed in §6. Afterwards,
`uv run python scripts/export-schemas.py --check` must print nothing and exit 0.

### 3.3 `src/ptest/config.py` (T1)

```python
CONFIG_UNCOMMITTED = "config-uncommitted"

def config_uncommitted(cwd: Path | str) -> C.Problem | None:
    """config-uncommitted Problem when cwd sits in a linked worktree with no
    nearest .ptest.toml and the main checkout has one at the same relative
    path (nearest-first); else None. Never raises, never loads main config."""

def git_root(cwd: Path | str) -> Path | None:
    """Static git boundary containing cwd (no subprocess), or None for
    non-git / unsafe / unavailable. Never raises."""
```

`config_uncommitted` works as follows. First,
`physical = _absolute_directory(cwd)`. It then returns `None` if either of these
holds:
- `_find_config(physical)` found a config, or reported a problem other than
  `initialization-required`;
- `_git_boundary(physical)` is `None`.

Otherwise it returns `found = worktree.main_config(physical, boundary)`, and if
that is not `None`:

```python
C.Problem(code="config-uncommitted", phase="config", retryable=False, message=(
    f"this is a linked git worktree without {name}; the main checkout has {found.path}. "
    f"Worktrees only receive committed files. Ask the user to commit {name} on the base "
    f"branch. Do not run ptest init here."))
```

Here `name = ".ptest.toml" if found.relative == "." else f"{found.relative}/.ptest.toml"`.
Rendered through `_emit_error`, the human line is
`config-uncommitted: this is a linked git worktree without .ptest.toml; the main checkout has /abs/main/.ptest.toml. Worktrees only receive committed files. Ask the user to commit .ptest.toml on the base branch. Do not run ptest init here.`

`resolve_config`: wherever it currently returns the `initialization-required`
problem, it first tries `config_uncommitted(physical_cwd)` and returns that
problem instead when it is not `None`. The resolution keeps `root=physical_cwd`,
`path=None`, `config=None` and `monorepo=None`. No other resolution changes.

`init_project(cwd, options)`:
1. `resolution = resolve_config(...)`. An existing local config still returns
   `_existing_result` unchanged, and `from_main` is ignored, so re-running
   after a copy is idempotent.
2. If `resolution.problem.code == "config-uncommitted"` and not
   `options.from_main`, raise that problem. This also applies to `dry_run`,
   which is how `register` refuses.
3. If `options.from_main` and the problem is anything else, raise
   `Problem("invalid-config", "--from-main only works in a linked git worktree whose main checkout has .ptest.toml at this path; nothing was copied")`.
4. If `options.from_main` and `config-uncommitted`, copy from main:
   1. `found = worktree.main_config(...)`.
   2. Set `dest = found.worktree.root` when `relative == "."`, else
      `_safe_init_child(found.worktree.root, found.relative)`. If the directory
      is missing in the worktree, raise `state-unavailable`.
   3. Read the root bytes with `read_regular(found.worktree.main_root, name, 256 KiB+1)`.
   4. For each child in `found.children`:
      - If `_safe_init_child(dest, child)` fails, skip it and add the warning
        `C.Reason("config-uncommitted", f"did not copy {child}/.ptest.toml: that directory is missing in this worktree", ())`.
      - If the child config already exists, the detail is "already present".
      - Otherwise read the main bytes and queue a create.
   5. `dry_run` returns `PREVIEW` (`exists=False`, `config=None`) with
      "would create" details.
   6. Otherwise run `create_exclusive(..., private=False)`, children first and
      then the root. If the root returns `already-exists`, return
      `_existing_result`.
   7. Return `CREATED`, `target=dest/.ptest.toml`, `exists=True`, and
      `config=_summary(resolve_config(dest).config)` if it is not `None`, else
      `None`.
   8. Details are `_config_detail(".ptest.toml", "created")` plus the child
      details.
   9. `warnings = (stopgap, *skip_warnings)` with
      `stopgap = C.Reason(code="config-uncommitted", paths=(), message=f"copied {name} from the main checkout {found.path}; this copy is a temporary stopgap that may go stale. The fix is to ask the user to commit {name} on the base branch.")`.
   10. `commit_paths=()`.
5. The normal create paths (standalone and monorepo) set
   `commit_paths = tuple(<created config paths relative to boundary, posix>)`
   when `_git_boundary(physical_cwd)` is not `None`, else `()`. The standalone
   path gives `(".ptest.toml",)` relative to the boundary. Monorepo gives the
   children created, then the root. PREVIEW and EXISTING give `()`.

### 3.4 `src/ptest/cli.py` (T2) — texts frozen

- `ParsedArgs.from_main: bool = False`. The `init` parser accepts `--from-main`
  (a bool flag; repeats are harmless like `--dry-run`). Combining it with
  `--runner` or `--child` raises
  `_problem("invalid-config", "--from-main copies the main checkout's config; it cannot be combined with --runner or --child")`.
- In the `init` branch, before `_init_agents(...)`: if not `parsed.from_main`
  and `(p := config_api.config_uncommitted(cwd)) is not None`, raise `p`. The
  result is exit 2, `code: message` on stderr, or the JSON error document with
  `--json`. Nothing is written and no prompt appears. Pass
  `from_main=parsed.from_main` into `C.InitOptions`.
- After `agent_rules.apply`, when `config_api.git_root(cwd) is not None` and
  `applied is not None`:
  `result = dataclasses.replace(result, commit_paths=result.commit_paths + tuple(d.target for d in applied.details if d.source == "guidance" and d.action in ("created", "updated") and d.target not in result.commit_paths))`.
  The payload and renderers then use this `result`. With `--from-main`, rule
  paths are appended the same way, but config paths stay out (D8).
- Reroute: the `main()` condition becomes
  `resolution.problem.code in {"initialization-required", "config-uncommitted"}`.
  On `("missing", typed)`, compute the scope directory the way
  `_nearest_config_dir` does (`start if start.is_dir() else start.parent`). If
  `config_api.config_uncommitted(that_dir)` is not `None`, raise it (exit 2).
  Otherwise print the old "no ptest project for X — run ptest init there" line.
  If the reroute returns `None` (a non-path scope), the problem is raised as
  before.
- In the later `resolution.config is None` block, the
  `unsupported-capability` conversion stays only for `initialization-required`.
  A `config-uncommitted` problem is raised as is.
- Run warning `_warn_uncommitted_config(resolution, *, quiet: bool) -> None` is
  called right after `_warn_stale_guidance(resolution.root)`:
  - It is a no-op unless `resolution.config is not None or resolution.monorepo is not None`.
  - It gets `paths = worktree.uncommitted_config_files(resolution.root)`.
  - Line for one path: `f"ptest: {p} is not committed — new worktrees won't have it"`.
    For more than one:
    `f"ptest: {', '.join(paths[:3])}{f' (+{len(paths)-3} more)' if len(paths) > 3 else ''} are not committed — new worktrees won't have them"`.
  - It emits through `progress.emit(render.terminal_text(line), quiet=quiet)`
    and wraps everything in `try/except Exception: return`.
- `where`: when `resolution.problem` has code `config-uncommitted`, the
  unconfigured payload's `"warnings"` becomes
  `[{"code": "config-uncommitted", "message": problem.message, "paths": []}]`.
  Human `where` prints `render.terminal_text(problem)` to stderr as one line,
  after the stdout lines. Exit 0.
- `doctor` branch: first statement is `if resolution.problem is not None and
  resolution.problem.code == "config-uncommitted": raise resolution.problem`.
  This comes before `--fix`, `--probe`, offline, or any consent prompt.
- Doctor mention `_uncommitted_mention(resolution) -> str | None`:
  - `paths = worktree.uncommitted_config_files(resolution.root, include_agent_rules=True)`.
  - It returns `None` on empty or any exception.
  - Otherwise it returns
    `f"not committed: {', '.join(paths[:5])}{f' (+{len(paths)-5} more)' if len(paths) > 5 else ''} — new worktrees and clones won't have them; commit them on the base branch"`.
  - It is written to stdout (plus `"\n"`) after the offline grid, next to
    `_fix_mention`, and after `_run_review_entry` returns in the online path
    (declined or not).
  - It is never printed for `--json`, `--fix` or `--probe`.

### 3.5 `src/ptest/init_render.py` (T3) — text frozen

`render_init_footer` appends the commit reminder as the very last block, after
the restart line. It is shown only when not `dry_run`, `result.action is not
PREVIEW` and `result.commit_paths` is non-empty. It is separated from earlier
lines by one blank line:

```
Commit these files: .ptest.toml, docs/ptest-agent.md, AGENTS.md
Worktrees and clones only get committed config.
```

The first line is wrapped with `wrap_words(..., width, indent="", hang="  ")`.
Paths go through `terminal_text`. There is no new function parameter, and
`render_init` (the header) is unchanged.

### 3.6 `src/ptest/doctor.py` (T3)

`_config_findings(root: Path) -> tuple[C.Finding, ...]` returns one finding per
path from `worktree.uncommitted_config_files(root, include_agent_rules=True)`:

```python
C.Finding(code="config.uncommitted", severity="medium", confidence="high",
          path=rel, line=None, evidence_type="git-tree",
          consequence="New git worktrees and clones will not contain this ptest file.",
          remediation="Ask the user to commit it on the base branch; do not run ptest init in a worktree.",
          verification="git ls-tree HEAD lists the file and ptest doctor no longer reports config.uncommitted.")
```

- They are admitted only when the requested doctor scope is `None` (whole
  repository). They go through the existing ledger admission (`_admit_finding`,
  or the `_Scan` equivalent in `inspect`), so finding and output bounds still
  hold, and they are admitted **before** the source findings.
- For a standalone project, root = `config.root`. For a monorepo, root =
  `resolution.root`, and children are included by the worktree helper.
- **Readiness must be identical** with and without these findings. Compute
  readiness from the non-config findings.

## 4. Per-task scope and acceptance

Common rules:
- Tests go through ptest only, scoped:
  `ptest --workers 2 --queue-timeout 1800 <your test files>`.
- No test may use a timeout above the default.
- Git fixtures use `support.git` and `support.init_git_repo`, which are
  hermetic.
- Commit inside your own worktree only.
- Run `graphify update .` after source changes.
- Do not edit files you do not own. If an unowned test breaks, report it in
  your final message; the orchestrator fixes it at integration.

Frozen test helper. Copy it verbatim into each test module that needs it:

```python
from support import git, init_git_repo, write_ptest_toml

def _linked_worktree(tmp_path, *, commit_config=False, **toml):
    """main checkout (committed README) + linked worktree at tmp_path/wt."""
    main = init_git_repo(tmp_path / "main", files={"README.md": "x\n"})
    write_ptest_toml(main, **toml)
    if commit_config:
        git(main, "add", ".ptest.toml")
        git(main, "commit", "-q", "-m", "config")
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "wt", str(wt))
    return main, wt
```

### T1 — core (BARRIER for T2 and T3)

Owns:
- `src/ptest/worktree.py` (new)
- `src/ptest/config.py`
- `src/ptest/contracts.py`
- `docs/schemas/v1/init.json` (regenerated)
- `tests/ng/test_worktree.py` (new)
- `tests/ng/test_config.py`
- `tests/ng/test_init.py`
- `tests/ng/test_contracts.py`

Acceptance:
1. `linked_worktree` works on a real `git worktree add` and returns
   `(wt, main)`. It returns `None` for:
   - the main checkout
   - a non-git directory
   - a bare-repo worktree (`git init --bare m.git` + `worktree add`)
   - a submodule-style `.git` file (no `commondir`)
   - a `.git` symlink
   - a `.git` file over 4096 bytes
   - garbage or multi-line content
   - a back-link mismatch
   - a gitdir path that crosses a symlink

   None of these cases raises.
2. `main_config` does a nearest-first walk and finds the same relative path
   (`wt/api/x` finds `main/api/.ptest.toml` before `main/.ptest.toml`). It
   lists v2 children, ignores a symlinked main config, and returns `None` when
   `cwd` is outside `root`.
3. `uncommitted_config_files` behaves as follows:
   - it returns `(".ptest.toml",)` for an untracked or staged-only config;
   - it returns `()` once the config is committed, in a non-git directory, and
     in an unborn-HEAD repo;
   - monorepo children are listed;
   - agent-rule files appear only with `include_agent_rules=True`, and an
     `AGENTS.md` without the marker is not listed;
   - with git absent from PATH (monkeypatch PATH) it returns `()`.
4. `resolve_config(wt)` returns `config=None`, `monorepo=None` and problem
   code `config-uncommitted`, with the exact §3.3 message naming the main
   path. It never reads main content. Prove this by making the main config
   invalid TOML: the code is still `config-uncommitted`, not `invalid-config`.
5. `resolve_config` is unchanged for:
   - a normal checkout
   - a worktree with a committed config
   - a bare-repo worktree
   - a non-git directory
   - a worktree where main also has no config (`initialization-required`)
6. `init_project` in `wt`:
   - It raises `config-uncommitted`, also with `dry_run=True`.
   - With `from_main=True` it copies byte-identical files (same
     `project_id`), including v2 children, and skips missing child
     directories with a warning. The result is `CREATED`, with the stopgap
     warning and `commit_paths == ()`.
   - `dry_run` + `from_main` gives `PREVIEW` and writes nothing.
   - A second `from_main` run gives `EXISTING`.
   - `from_main` outside a worktree raises `invalid-config` with the frozen
     message.
   - The main checkout tree (paths, bytes, mtimes) is identical before and
     after every one of these cases.
7. A normal init inside a git repo gives `commit_paths == (".ptest.toml",)`.
   Monorepo init lists the children, then the root. Init in a non-git
   `tmp_path` gives `()`. PREVIEW and EXISTING give `()`.
8. Contracts:
   - `InitOptions(from_main="x")` raises `TypeError`.
   - `InitResult` rejects absolute or `..` entries in `commit_paths`.
   - `serialize_init_result` includes `commit_paths`.
   - An encode/decode roundtrip keeps it, and a decoded old document without
     it projects `[]`.
   - `config-uncommitted` is in `REASON_CODES` and `config.uncommitted` is in
     `FINDING_CODES`.
   - The existing key-set assertions (`test_contracts.py` ~1234, ~1475) are
     updated.
   - A new test asserts that `docs/schemas/v1/init.json` equals
     `json.dumps(C.PUBLIC_SCHEMAS["init"], indent=2, sort_keys=True) + "\n"`.
9. `git_root` and `config_uncommitted` never raise, including on missing,
   symlinked or relative inputs.
10. Scoped run: `ptest --workers 2 --queue-timeout 1800 tests/ng/test_worktree.py tests/ng/test_config.py tests/ng/test_init.py tests/ng/test_contracts.py`
    is green. Coverage for `worktree.py` is at least 90%, and `config.py` does
    not regress.

### T2 — CLI (wave 2, after T1)

Owns:
- `src/ptest/cli.py`
- `tests/ng/test_cli.py`
- `tests/ng/test_init_smoke.py` (only the init JSON key-set assertion at ~323)

Acceptance:
1. `main(())` and `main(("tests",))` in `wt` exit 2 with stderr containing
   `config-uncommitted:` and "Do not run ptest init here". The runner never
   executes: monkeypatch `operations.execute` to fail. The text "run ptest
   init" does not appear.
2. Path reroute: in `wt`, `main(("api/tests",))`, where main has only
   `api/.ptest.toml` untracked, raises `config-uncommitted` naming
   `main/api/.ptest.toml`. In a non-worktree directory with no config, the old
   "no ptest project for … — run ptest init there" line is unchanged.
3. `main(("init",))` in `wt` exits 2, does not prompt (monkeypatch `input` to
   fail) and writes nothing. `("init", "--json")` returns an error document
   with code `config-uncommitted`. `("init", "--from-main", "--agents",
   "none")` copies the file, exits 0, prints the stopgap warning and no commit
   reminder. The `--json` form gives `commit_paths == []` and a warning with
   code `config-uncommitted`. `--from-main --runner pytest` exits 2 with
   `invalid-config`.
4. `register --json`, `plan --json` and `history --json` in `wt` return error
   documents with code `config-uncommitted`. `where --json` exits 0 with the
   warnings entry, and human `where` shows the stderr line. `doctor --offline`,
   `doctor --fix` and `doctor --json` in `wt` exit 2 with `config-uncommitted`
   and no consent prompt.
5. Run warning:
   - A git repo with an untracked `.ptest.toml` (command runner `echo`) prints
     exactly one matching stderr line.
   - `-q` suppresses it.
   - The exit code and runner stdout are identical to the committed case.
   - Once the config is committed there is no line.
   - A non-git `tmp_path` has no line.
   - A monorepo with an untracked child prints one line listing it.
   - If `worktree.uncommitted_config_files` is monkeypatched to raise, the run
     is still unaffected.
6. `init --json` in a git repo gives `commit_paths` with `.ptest.toml` plus
   the created agent-rule paths. In a non-git `tmp_path` it gives `[]`. Update
   `test_init_json_is_non_interactive_and_byte_exact` and the
   `test_init_smoke.py` key sets to include `commit_paths`.
7. The doctor mention line appears after offline doctor output when there are
   uncommitted files, and is absent for `--json` and when everything is
   committed.
8. Scoped run: `ptest --workers 2 --queue-timeout 1800 tests/ng/test_cli.py tests/ng/test_init_smoke.py`
   is green.

### T3 — doctor finding and init reminder (wave 2, after T1)

Owns:
- `src/ptest/doctor.py`
- `src/ptest/init_render.py`
- `tests/ng/test_doctor.py`
- `tests/ng/test_init_render.py`

Acceptance:
1. `render_init_footer` shows the two frozen lines last, when `commit_paths`
   is non-empty. It shows nothing for empty `commit_paths`, PREVIEW or
   `dry_run`. Wrapping works at width 40, and control characters in paths are
   sanitized. Existing footer tests pass, updated only where the new tail
   appears.
2. `doctor.inspect_workspace` on a git repo with an untracked `.ptest.toml` and
   `docs/ptest-agent.md` gives `config.uncommitted` findings for both. When
   they are committed there are none. A scoped request gives none. The
   findings bound is respected (limit=1 admits at most one). Readiness is
   equal to the committed-repo readiness. A monorepo lists the child config.
3. Scoped run: `ptest --workers 2 --queue-timeout 1800 tests/ng/test_doctor.py tests/ng/test_init_render.py`
   is green.

### T4 — docs, guide, help (wave 1, no dependency)

Owns:
- `src/ptest/resources/repository-agent-guide.md`
- `src/ptest/agent_rules.py`
- `src/ptest/help.py`
- `README.md`
- `docs/changelog.md`
- `tests/ng/test_agent_rules.py`
- `tests/ng/test_help.py`

It no longer owns `docs/schemas/v1/init.json` (moved to T1, D9).

Acceptance:
1. The guide's "Reading ptest output" table gains this row, placed before the
   `unsafe-path` row:
   `` | `config-uncommitted: …` | this linked git worktree lacks the committed `.ptest.toml` that the main checkout has | Stop and tell the user to commit `.ptest.toml` on the base branch; never run `ptest init` here. | ``
   It also gets one line in "Reporting" or right after the table:
   "Untracked config: `ptest: .ptest.toml is not committed` — tell the user;
   do not commit it yourself unless asked."
2. `agent_rules._PREVIOUS_GUIDE_SHA256S` gains the entry below, with this
   comment:
   `# 80392ca (0.3.3-0.3.5): guide before the config-uncommitted row.`
   `"4fd66f8dea3d1fa6bdb691daa0fe329ec54dd43670a0e2d88bb297552e621f9d",`
   `test_every_shipped_guide_version_hashes_into_previous_set` and the
   upgrade-in-place tests must pass.
3. `help.py`:
   - The `_INIT` syntax adds `[--from-main]`.
   - Its notes explain that init refuses in a linked worktree whose main
     checkout has an uncommitted `.ptest.toml` (`config-uncommitted`), that
     `--from-main` copies the main checkout's config verbatim as a temporary
     stopgap that may go stale, and that the fix is committing on the base
     branch.
   - The notes say init ends by listing the files to commit.
   - The `agents` topic gets one sentence: `config-uncommitted` means stop and
     tell the user; never run ptest init.
4. `README.md` gets a short "Worktrees and clones" section covering: commit
   the ptest files; `config-uncommitted`; `--from-main` is a stopgap; the
   run warning and the doctor line. It also updates the `ptest init` flag list.
5. `docs/changelog.md` gets a new top section `## Unreleased`. The release
   step renames it to 0.3.6. It has bullets for the five behaviors.
6. `test_user_facing_text_has_no_banned_terms` passes. New `test_help`
   assertions cover the init and agents topic texts. A new `test_agent_rules`
   assertion checks that the shipped guide contains `config-uncommitted` and
   "never run `ptest init`".
7. Scoped run: `ptest --workers 2 --queue-timeout 1800 tests/ng/test_agent_rules.py tests/ng/test_help.py`
   is green.

## 5. Negative contracts (regression evidence each task must show)

| Contract | Test owner |
|---|---|
| No config from outside the checkout is ever executed or parsed for a run | T1 (item 4: invalid main config still gives config-uncommitted), T2 (item 1: execute never called) |
| Main checkout never modified | T1 (item 6 tree snapshot), T2 (item 3 snapshot around `--from-main`) |
| No git subprocess for detection | T1: `linked_worktree` and `main_config` pass with `subprocess.Popen` monkeypatched to fail |
| Unchanged behavior for normal checkout, committed worktree, bare repo, non-git | T1 (item 5), T2 (item 2 second half, item 5 non-git) |
| Warning never alters output or exit status, never fails a run | T2 (item 5) |
| `--from-main` never overwrites | T1: pre-existing child config bytes unchanged |

## 6. Shared-file content

`docs/schemas/v1/init.json`: the full, exact final bytes are in this design's
`sharedFileContent` return field. They equal the current file plus a
`commit_paths` property inserted between `action` and `config`. T1 generates it
with `scripts/export-schemas.py`, and a transcription pass must be a
byte-identical no-op. No other shared files (routers, `__init__`, exports) are
touched. `ptest/__init__.py` does not re-export modules.

## 7. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Unowned tests assert exact stderr for runs in uncommitted-config git fixtures | integration failures in `ptest --full` | Most fixtures commit everything (`init_git_repo`, `ptest_project(git=True)`). The orchestrator fixes stragglers at integration, and T2 reports any it sees. |
| Unowned tests assert exact human init footer in git repos | same | The reminder only appears inside git repos, and most init tests use a non-git `tmp_path`. |
| `git ls-tree` cost on every run | ~5 ms | One call, 2 s deadline, skipped when there are no candidates. |
| Monorepo root committed but a child config uncommitted, in a worktree | the run fails with the existing "monorepo child is unavailable" (no init advice) | Out of scope. The main-checkout run warning lists the child, which prevents this. Noted as a follow-up. |
| `executability` still says "run ptest init from the repository root" for a missing child config | misleading only in the out-of-scope case above | Follow-up; not reachable in the config-uncommitted situation because D6 makes doctor and init fail early. |

## 8. Deviations from the context pack

- Task labels follow the triage (T1 core, T2 CLI, T3 doctor + init_render,
  T4 docs). The pack's contract section labelled them differently.
- `main_config_paths(root, relative)` became `main_config(cwd, root) -> MainConfig | None`
  (a nearest-first walk, with the matched relative path and children).
  `uncommitted_ptest_files` was renamed `uncommitted_config_files`.
- Tracked means "in HEAD's tree" (`ls-tree`), not "in the index".
- `docs/schemas/v1/init.json` moved from T4 to T1, because it is generated from
  `contracts.py`.
- Doctor in the config-uncommitted situation fails with exit 2 instead of
  showing a limitation (D6).
- The commit reminder text and placement are frozen here (§3.5). T3 reads
  `result.commit_paths`, and there is no new renderer parameter.
