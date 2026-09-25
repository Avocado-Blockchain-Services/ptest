# Doctor v3: remove legacy, one --json, and `ptest doctor --fix` — requirements

Status: requested by the user on 2026-09-25 ("if we have legacy shit, remove it. no one uses this"; "ptest doctor should
change the project config … especially after recommendations to be parallel are applied"). ptest has a single user; there
are no external consumers of old flags or formats. Prefer deleting code.

## L. Remove legacy (do this first, as its own commit)
1. Doctor output modes collapse to one: `ptest doctor --json` emits the versioned review/assessment document
   (today's `--assessment-json` document). `ptest doctor --offline --json` emits the same document kind built from
   deterministic/static facts only (items ptest cannot decide offline are `unknown` with a reason). Delete the static legacy
   `--json` document, `--assessment-json`, and `--prompt` (flag, code paths, schema files if only they used them — keep the
   drift guard green; removing a public schema is fine here), their tests, help and README text.
2. Delete the legacy Codex skill location handling (`.codex/skills/ptest`: `_LEGACY_CODEX_SKILL`, the "legacy" state,
   migration/preservation messages and refusals) from init/agent_rules. uninstall may drop its `.codex` entry too.
3. Delete docs/legacy-index.md and its README link; sweep help/README/docs for "legacy" wording that describes removed
   behaviour. Keep `_PREVIOUS_GUIDE_SHA256S` (it recognises ptest's own previously written guide/skill bytes).
4. Update the shipped agent guide/skill only if they mention removed flags (register old bytes' hashes if they change).

## F. `ptest doctor --fix`
1. Doctor computes the config ptest would write today for each project (reuse init's generator; do not duplicate it) plus
   deterministic fixes from ptest-owned facts, and shows a unified diff against the existing `.ptest.toml`(s):
   - drop a stale `-n 0` / xdist-disabling args written by older init versions when the parallel tier qualifies;
   - fix `[setup] argv` when the project needs extras/groups the current argv omits (e.g. `uv sync --locked` vs the extras
     the test deps live in), when this is determinable from the lockfile/pyproject;
   - when `--cov`/`--cov-report` is present (or being added) and the runner is pytest, propose enabling `[selection]` with
     `closed_inputs = true`, `input_roots`, `full_triggers` (lockfiles, conftest.py, pyproject/pytest config, .ptest.toml) and
     a drafted mapping from the source layout — clearly marked as a draft in the diff output;
   - anything else the deterministic doctor items already know the exact fix for.
   Model-review findings about test code are NEVER applied automatically.
2. Consent: TTY asks `Apply these changes to <files>? [y/N]`; non-interactive requires `--yes`; `--dry-run` shows the diff
   only. `--fix` never runs a model review by itself (it can combine with `--offline`).
3. Safety: atomic write (temp + rename, keep mode), no symlink following (reuse files.py helpers), fail closed if the file
   changed between planning and writing, keep every setting ptest does not manage byte-identical, idempotent (second run:
   "config is up to date"). Monorepo: root dispatcher + each child.
4. After a successful fix, print one line per file changed and suggest the next step (e.g. `run ptest --full once to record a
   baseline` when selection was enabled).
5. Normal `ptest doctor` output mentions `ptest doctor --fix` when it would change something ("config is out of date: N
   changes — run ptest doctor --fix to review them").

## Tests (strict TDD, through ptest only)
Legacy removal: removed flags give a clear unknown-option error; `doctor --json` decodes as the assessment document;
offline --json likewise. Fix: stale `-n 0` dropped; setup extras fixed; selection draft proposed with --cov; unmanaged
settings preserved byte-for-byte; symlinked .ptest.toml refused; concurrent change refused; dry-run writes nothing;
non-TTY without --yes writes nothing and exits non-zero; idempotent second run; monorepo children; doctor mentions --fix.

## Design notes (implementation, 2026-09-25)

Section L (output-mode collapse):
- `ParsedArgs.prompt` / `ParsedArgs.assessment_json` deleted; `--json` takes
  the old `--assessment-json` role. Online `doctor --json` runs the normal
  consented review entry and emits the agent-assessment document; offline
  `doctor --offline --json` builds the same document kind without launching
  a provider or writing recommendations.md.
- Offline rows reuse the existing seams: `agent_assessment.build_packets` +
  `_plan_item_reviews` (deterministic answers) + `assemble_child` with a
  `str` reply per requested item, which already maps to an `unknown` row
  (`FAILED_PREFIX + reason`), then `_assemble_with_parallel` and
  `_child_assessment_data`. No new assembly layer.
- Additive contract additions for the offline document: provider name
  `offline` (profile `ptest-offline-v1`) and publication status `skipped`
  (path stays `recommendations.md`, sha256 of empty input). The legacy
  `doctor` public schema is removed entirely (spec explicitly allows this).
- Legacy Codex location (`_LEGACY_CODEX_SKILL`, `_legacy_codex_note`,
  `_legacy_provider_text`, the `"legacy"` skill kind, uninstall's
  `_LEGACY_SKILL_REL` entry) deleted as one concept: the old released
  template bytes have no other consumer, and there are no external users
  to protect. Kept: `_PREVIOUS_GUIDE_SHA256S`, `"previous"` skill kind.
- `docs/legacy-index.md` deleted; only live-doc `legacy` mentions swept
  (historical specs/designs under docs/ keep their wording).

Section F (`doctor --fix`):
- Fix planning reuses init's config generator (`init_render`/`config`
  resolution path) to compute the config ptest would write today; the fix
  diffs that against each existing `.ptest.toml` and appends deterministic
  item fixes (stale `-n 0`, setup argv extras, selection draft under --cov).
- Consent/safety mirror existing precedents: TTY `[y/N]`, `--yes`,
  `--dry-run`, `files.publish_atomic`-style temp+rename without symlink
  following, fail closed on concurrent change, byte-identical unmanaged
  settings, idempotent second run.
