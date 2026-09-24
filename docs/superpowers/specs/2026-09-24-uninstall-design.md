# `ptest uninstall` — design note (U1)

Status: design for the spec in `2026-09-24-uninstall-requirements.md`.
One new module `src/ptest/uninstall.py` plus CLI/help/README wiring and one
additive public JSON document kind (`uninstall`).

## 1. Exact state locations found (do not guess)

Repo-local (anchored at `config.repository_root`, the same Git-root anchor
`init` uses, so a subdirectory invocation cleans the whole checkout):

- `.ptest.toml` at the root (v1 project or v2 dispatcher) plus every declared
  monorepo child `<child>/.ptest.toml`. Parsed with `tomllib`; removed only
  when it parses as a ptest config (`version` 1, or 2 with a valid
  `monorepo.children` manifest via `monorepo.parse_monorepo_manifest`).
  Configs are always removed, even user-tuned — the spec says so.
- Guidance (all detection reused from `agent_rules`, never duplicated):
  - `docs/ptest-agent.md` — managed iff bytes equal `agent_rules._guide()`
    (current) or sha256 equals `agent_rules._BASE_GUIDE_SHA256` (previous).
  - Blocks in `AGENTS.md`, `CLAUDE.md` (spec) and `GEMINI.md` (same init
    mechanism — init appends to any pre-existing one, so round-trip needs it).
  - Skills `.claude|.agents|.opencode|.gemini/skills/ptest/SKILL.md` plus the
    legacy `.codex/skills/ptest/SKILL.md` — managed iff
    `agent_rules._provider_target` reports `current`/`legacy`/`previous`
    (exact bytes incl. the two older templates); `already-exists` means
    user-edited → keep. Empty parent dirs (`skills/ptest`, `skills`,
    tool dir) are pruned with `rmdir` only, so a non-empty dir (e.g.
    `.opencode/package.json`, `node_modules`, foreign skill files) survives.
  `docs/` is likewise removed only when the guide deletion leaves it empty
  (a `docs/` dir with any other content survives).
- `recommendations.md` at the root and in each declared child — removed only
  when `recommendations._split_marker` validates AND the recorded sha256
  matches the body (`_marker_for` recomputation). Edited → keep + report.

Machine-level (`platform.domain_paths`, normal or `--fixture-domain`):

- Per-checkout dir `domain.root/checkouts/<checkout_id>/` — holds
  `history.sqlite3` (incl. the `baselines` table), `history-*.json` markers,
  `qualified-native-profile.json`, and `operations`' `setup-fingerprint.json`.
  Removing this one directory clears history, baselines and fingerprints.
- Shared ledger `domain.root/coordinator.sqlite3` — `jobs` rows (plus
  `job_resources`/`observations` via `ON DELETE CASCADE`) filtered by
  `checkout_id` are deleted; every other row is untouched.
- Deliberately KEPT: `domain.root/review-models/<provider>.json` (keyed by
  provider + CLI version, not by checkout — a shared record other checkouts
  depend on), `machine.toml`, the domain marker, other checkouts' dirs.

## 2. How "this checkout" is keyed

`checkout_id = sha256(realpath(repo_root))[:32]` — the exact formula in both
`cli._checkout` and `history._validated_checkout_root`, so uninstall, history
and operations agree. `project_id` comes from the parsed root (v1) config;
monorepo children share the `checkout_id` (root-derived), and the ledger has
no `project_id` column, so one `checkout_id`-scoped delete covers the whole
checkout and can never match another checkout.

## 3. Managed-detection reuse from `agent_rules`

`_guide`, `_BASE_GUIDE_SHA256`, `_provider_target` (+ the three exact skill
templates), `_block`, `_MARKER_START/_END`, `_managed_state` (raises on
unbalanced/duplicated markers → file untouched + reported), and `_replace`
for the atomic block rewrite (temp file + rename in the same directory,
mode preserved, `expect_bytes` fails closed on plan/apply races).

## 4. Marker-block removal format (derived from init's insertion code)

Init appends `text.rstrip("\n") + "\n\n" + block` where
`block = _block(name)` (`"<!-- ptest-agent-rules:start -->\n<ref>\n<!-- ...end -->\n`,
`<ref>` = full sentence for `AGENTS.md`, `@docs/ptest-agent.md` otherwise),
or creates the file with exactly `block`. Removal is the exact inverse:

- content == `block` → the file was created by init → unlink it;
- content ends with `"\n\n" + block` → `_replace` with `content[:-(len)] + "\n"`,
  restoring the single-trailing-newline original byte-for-byte (the case init
  normalises to; covered by the round-trip test);
- anything else (edited around the block, unbalanced/duplicated markers,
  symlink) → keep/skip + report, never write through a symlink.

## 5. `--self` install-root detection

Candidates: `~/.local/ptest` (home from the passwd record, as `platform`
does — `$HOME` is ignored), or the ancestor above `/.ptest-bundles/` in
`realpath(sys.argv[0])` when the running binary lives in an install layout.
A root qualifies only if it looks like `install.sh` made it: a real
`.ptest-bundles/` dir holding at least one bundle dir with a `complete.json`
`{"version": 1, "bundle_id": <dirname>, ...}`. Every existing candidate is
checked before any refusal, so a stale candidate never vetoes a valid
install root; otherwise refuse and remove nothing. Removal walks bottom-up with `O_NOFOLLOW` (unlink files/symlinks
as links, never following; `rmdir` dirs), confined to the root fd. A `ptest`
symlink found via `PATH` (`shutil.which`) is removed only when its resolved
target sits inside the install root. When the running binary is inside the
root, removal still completes (Unix unlinks the running file harmlessly)
and the final line `ptest was uninstalled` is printed last.

## 6. Abuse cases (secure-by-spec step 1; every one gets a twin test)

- Tenancy: second checkout's history dir + ledger rows survive; shared
  `review-models` cache, `machine.toml`, marker untouched.
- Tenancy: active run (any non-terminal scheduler lease for our
  `checkout_id` via read-only `reconcile`) refuses before any mutation.
- Input: symlinked `.ptest.toml` / skill / `docs/` / `AGENTS.md` reported
  and left alone; block rewrite never writes through a symlink.
- Input: unbalanced/duplicated markers → file untouched + reported.
- Input: hostile monorepo children (`../evil`, absolute paths, symlinked
  child dirs) never escape the root; unparseable configs are skipped.
- Input: oversized/non-regular/unreadable entries fail closed (skipped).
- State: plan→apply re-verification (`_replace` `expect_bytes`, ledger
  active re-check, per-file lstat before unlink); second run prints
  `nothing to remove`, exit 0; init→uninstall→init is a clean first init.
- State: TTY decline/EOF changes nothing; non-TTY without `--yes` prints
  the plan + hint, exit non-zero, changes nothing; `--dry-run` never writes.
- Exposure: `--json` goes through the allowlisted `uninstall` validator +
  projector (unknown fields dropped); human output and errors use
  `render.terminal_text` and never echo file bytes or hostile input.
- Self: non-install-looking root refused; PATH symlink pointing elsewhere
  kept; nothing outside the install root is touched.

## 7. Gates (secure-by-spec step 4)

Repo-mandated wrapper: tests only via `ptest`. Secrets/SAST/deps/property:
no repo-configured scanners were found, and standing up new tooling
mid-feature is out of scope — reported as a gap, not installed. Coverage
rests on the twin tests above plus the existing contract/schema drift gates
(`export-schemas.py --check`, frozen `docs/schemas/v1/*.json` incl. the new
`uninstall.json`).
