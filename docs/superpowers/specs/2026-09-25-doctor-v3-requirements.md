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
- Legacy Codex *location* (`_LEGACY_CODEX_SKILL`, `_legacy_codex_note`,
  uninstall's `_LEGACY_SKILL_REL` entry, migration/preservation messages
  and refusals) deleted. BUT audit A1 (HIGH) showed the pre-8cd2b54 short
  template bytes were also the canonical-path content at
  `.claude|.agents|.opencode|.gemini/skills/ptest/SKILL.md`, still
  ptest-managed there: `_legacy_provider_text` and the `"legacy"` upgrade
  kind plus the uninstall managed entry were restored (location handling
  stays deleted). Kept: `_PREVIOUS_GUIDE_SHA256S`, `"previous"` skill kind.
- `docs/legacy-index.md` deleted; only live-doc `legacy` mentions swept
  (historical specs/designs under docs/ keep their wording).

Section F (`doctor --fix`, new module `src/ptest/doctor_fix.py`):
- Planning reuses init's own logic rather than duplicating it: the stale
  `-n 0` call is the exact `parallel_request` "sets -n 0" outcome (every
  other tier input qualifies), and the setup baseline comes from
  `config._fresh_config`. Only the three specified change sets are ever
  planned; a missing `[setup]` table is not added.
- Setup extras apply only to an `uv sync` argv naming no group/extra when
  exactly one optional extra or dependency group carries pytest while the
  main dependencies omit it; otherwise fail closed with no change.
- The selection draft fires on `--cov`/`--cov-report` present in the
  effective runner args, drafts `input_roots` from test roots plus `src/`
  and `full_triggers` from files present on disk, and is marked by a
  separate DRAFT section in the diff output (never written to the file).
  Per audit A1 (LOW), an existing table only flips `enabled` false→true
  and fills *absent* keys; hand-tuned values are kept and show as kept
  context in the diff. Per audit A1 (NOTE), quoted headers/keys
  (`["selection"]`, `"args"`) patch in place, and patched bytes are
  re-parsed — refusal (`invalid-config`) instead of a duplicate table.
- Consent/safety mirror existing precedents: TTY
  `Apply these changes to <files>? [y/N]`, `--yes`, `--dry-run`, atomic
  temp+rename keeping the exact mode, no symlink following, fail closed
  per file on planning-time vs write-time identity/bytes mismatch (no
  cross-file atomicity), byte-identical unmanaged settings via line
  surgery, idempotent second run (`config is up to date`).
- F5 mention (`config is out of date: N changes — run ptest doctor --fix
  to review them`, N = field changes) is appended to both human text
  outputs only; planning failures there stay silent so doctor output
  never breaks, while `doctor --fix` itself reports refusals loudly.
