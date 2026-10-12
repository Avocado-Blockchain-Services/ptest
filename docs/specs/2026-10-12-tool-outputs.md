# ptest 0.6.4: tool outputs no test reads, and `doctor --fix` for gitignored files

Status: approved by the user on 2026-10-12 ("lets do your suggestions": built-in
list, `doctor --fix` drafts the rest, then finish the reuse trial). The approved
proposal had `.env` edits keep rerunning the tests; that is impossible together
with a worktree's symlinked `.env` (T5), so `.env` is left to the user.

## Problem

In the full mode a gitignored file that is not declared counts as a test input. Until
it is declared in `[selection] non_input_outputs` (tests never read it) or
`ignored_inputs` (tests read it, so it is fingerprinted), the tree is never clean and
no baseline is recorded. The 0.6.3 reuse trial on specvm passed twice (362 tests,
1m30s each) and recorded nothing: `graphify-out/` (+55 files) blocked it. muse-msp has
`graphify-out/` and `.ruff_cache/`; fullon2's children add `.mypy_cache/`,
`.hypothesis/`, `.env` files and editor state. The same undeclared paths also send the
changed-mode static planner to the full suite ("ignored input is not declared").

## Contract

T1. **Built-in outputs.** `graphify-out`, `.ruff_cache` and `.mypy_cache` at the
project root (a monorepo child's own root) are non-input outputs without any
configuration, for every runner kind. They apply only to untracked and ignored
files, exactly like a declared `non_input_outputs` entry: a tracked file under them
stays an input. Nested copies (`pkg/.mypy_cache`) are not exempt.

T2. **The config wins.** A built-in entry is dropped whenever it overlaps an
`input_roots`, `ignored_inputs`, `full_triggers`, `always`, `no_tests` or group
entry, or a test root. Declaring `ignored_inputs = [".mypy_cache"]` keeps it
fingerprinted. Built-ins are never written into `.ptest.toml` and never make a
user's config invalid.

T3. **One source of truth.** Every place that consults `non_input_outputs` (input
snapshot, static planner, unmapped-change check, static impact walk) uses the same
effective list: the declared entries plus the surviving built-ins.

T4. **Identity.** The selection policy digest is unchanged (built-ins are not policy
fields). A baseline recorded before 0.6.4 had no undeclared ignored files, so it
stays valid; the input digest lists the files it covers, so a later change to the
built-in list can only cause a rerun, never a false "already verified".

T5. **`doctor --fix` drafts the rest** (pytest and vitest projects). It lists the
project's gitignored files that ptest would count as undeclared inputs, collapses
them to the shortest directory that hides no other file, and drafts:
- a directory carrying a valid `CACHEDIR.TAG` (exact signature line, regular file,
  no symlink): `non_input_outputs`;
- a `.env` or `.env.*` file: nothing; the note names it. Tests may read it, so
  declaring it an output would let an edit keep "already verified", and
  `ignored_inputs` refuses a worktree's `.env`, which is a symlink. The note gives
  the output route with its cost (`ptest --full --again` after an edit);
- anything else: `ignored_inputs`. A wrong input entry only costs a visible
  rerun; a wrong output entry would keep stale proof. Never an unknown path as an
  output. (`.hypothesis` at the root is already a pytest byproduct and never
  reaches doctor.)
- a symlink that is not a `.env` file: nothing (it cannot be an input); the note
  names it.
Existing entries are kept; a drafted entry that would make the policy invalid is
dropped. The run-start "can improve this config" notice ignores these drafts, because
they depend on the working tree, not on the ptest version.

T6. **The run says how.** The "gitignored paths count as test inputs" reason and the
"no baseline recorded" note name `ptest doctor --fix` first.

## Abuse cases

| Case | Expected |
|---|---|
| tracked `graphify-out/x.json` edited | still a change; no reuse |
| `ignored_inputs = [".mypy_cache"]`, cache file edited | input changed; full gate runs |
| `input_roots = ["graphify-out"]` | built-in dropped; files are inputs; config valid |
| `pkg/.mypy_cache/x` ignored | still an undeclared input |
| `graphify-out` is a symlink, ignored | exempt like a declared output with a final symlink |
| vitest nested `node_modules` | unchanged |
| `doctor --fix`: unknown gitignored `data/fixture.json` | drafted as `ignored_inputs`, never as an output |
| `doctor --fix`: gitignored `.env` | not declared; named with both choices |
| `doctor --fix`: fake `CACHEDIR.TAG` (wrong signature, or a symlink) | treated as unknown: `ignored_inputs` |
| `doctor --fix`: symlinked unknown path | not drafted; named in the note |
| `doctor --fix`: entry overlapping a declared input | not drafted |
| run-start notice | unaffected by tree-dependent drafts |

## Out of scope

Comparing `.env` contents across checkouts, symlinked `ignored_inputs`, and nested
cache directories below the project root.
