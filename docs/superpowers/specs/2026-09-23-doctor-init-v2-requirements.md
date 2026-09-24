# Doctor and init v2 — requirements

Status: requirements approved by the user on 2026-09-23 ("now go"). The design task turns this document into one
gated design. Amend `2026-09-22-agent-doctor-design.md` where it conflicts, and record every amendment.

Base: branch `cc-reviewer-choice` (interactive reviewer menu, commit e591c18, plus this document). Its audit left
three LOW test findings, which are part of this work:
1. Add a test for TTY `init --doctor --allow-model-review` with no concrete reviewer and two fake reviewers
   installed. The menu must be shown, answering "2" must launch codex, and no y/N prompt may appear. Add a
   `("doctor", "--reviewer", "auto")` case to one menu test.
2. `tests/ng/test_init.py:652-676`: move the `.ptest.toml` existence check into the fake `qualification_status`,
   and delete the resolver that can no longer be reached.
3. Delete the duplicate `test_non_tty_auto_never_shows_menu`, or fold its `input` ban into the existing
   parametrized non-TTY test.

Real-world target: `/home/ingmar/code/persea_content_maker_unified` (monorepo; children `api/` pytest, `web/`
vitest). Every outcome below must hold on that repository.

## A. ptest must actually run real projects

Observed today: in persea, `ptest api/tests/common/test_logger.py` fails with exit 4, "pytest xdist is not owned by
the serial grant", because `api/pyproject.toml` has `addopts = "-n 4 --dist=loadgroup -m \"not
extended_migration\""`. `ptest web/src/.../keyword-filters.test.ts` fails with exit 2, "native profile execution is
deferred", because init wrote `kind = "vitest"`, which ptest cannot execute.

1. **pytest with xdist:** a project whose pytest configuration activates xdist (`-n`, `--dist`, `-p xdist`,
   `numprocesses`) must run under ptest. The minimum is serial: init generates runner args that disable xdist and
   drop only the xdist flags, preserving every other addopts element (for example the `-m` filter). Real parallel
   ownership of xdist workers is out of scope. Init and doctor must say that execution is serial under ptest.
2. **vitest:** `ptest <test file>` and `ptest --full` must execute a vitest child. The design chooses the simplest
   correct route: native vitest execution, or a generated `command` profile. If it chooses a command profile, it
   must resolve the "setup execution is deferred for command profiles" restriction, or keep setup outside the
   profile with a clear next step.
3. **Executability check:** init must never silently write a profile ptest cannot execute. After writing, init
   runs a deterministic check (no model, no test execution) per project and reports one of: executable; executable
   with a caveat (for example "serial: xdist disabled under ptest"); or not executable, with the reason and the
   exact fix.

## B. A much better init

- **Concise, correct output.** Today "created .ptest.toml" appears twice, and the next steps are generic. Show
  per project: runner, execution status (from A.3), and what was created or updated. Show next steps as concrete
  commands for *this* repository that A.3 has verified will run.
- **Guidance files** (`docs/ptest-agent.md`, the AGENTS.md/CLAUDE.md include, and the `.claude|.agents|.opencode|
  .gemini/skills/ptest/SKILL.md` skills) must be accurate for the new behaviour. They must stay short and must not
  duplicate content. Remove or merge any that are redundant.
- Repeated init stays idempotent and never clobbers user edits (existing contract).

## C. Doctor v2 — per project, per checklist item

1. **Evidence admission priority:** manifests and locks, then test roots (`tests/`, `__tests__`, `*.test.*`), then
   test configuration (`conftest.py`, pytest/vitest config), then CI configuration, then source the tests import.
   **Exclude agent and pipeline artifacts by default**: `.superpowers/`, `.pipeline/`, `.claude/`, `.agents/`,
   `.opencode/`, `.gemini/`, `*.diff`, `*.patch`, and review, audit, and report markdown under those trees. The
   real persea review cited `api/.superpowers/sdd/*.diff` and missed `api/tests/conftest.py:447`, which caused a
   false DB-002 finding. That must not recur.
2. **One focused review per (project, checklist item).** Each checklist entry gets:
   - a short human label (for example "Database isolation");
   - an item-specific prompt (the question, what counts as evidence, and when N/A applies; absence is `unknown`);
   - item-specific evidence routing, seeded by the static scanner's hits plus per-item file patterns.
   Each call returns one row (status, rationale, citations, optional finding) against a one-row schema.
   Deterministic skip rules mark an item N/A with **no model call** when it clearly cannot apply (for example, no
   database library or configuration means DB-* is N/A, and the skip reason is shown).
3. **Bounded parallel fan-out** (default 4 concurrent, configurable). A failed item becomes `unknown (review
   failed: <reason>)` without failing the whole report. ptest alone computes scores and assembles the document.
   The public `ptest.agent-assessment/v1` schema changes only additively, if at all.
4. **Cheap model selection, without hardcoded model names:**
   - Discover models from the provider CLI: Codex via `codex debug models` (drop hidden entries), and Claude via
     aliases, since it has no listing.
   - One first provider call chooses the cheapest adequate model **from that real list**. ptest accepts the answer
     only if it matches a listed id exactly.
   - Fallbacks: Claude uses the `haiku` alias; if discovery fails, use the provider default model. Never guess.
   - A user override via `--review-model` or configuration always wins.
   - Cache the choice per provider and CLI version.
   - Qualification: the frozen argv gains only `--model <chosen>` / `-m <chosen>`. Record that the tool-denial
     canary must re-run whenever the chosen model changes (the controller runs the real canaries, not tasks).
5. **Terminal display:** one block per project, and one line per checklist item with its human label and an icon
   (✓ satisfied, ✗ gap, ? unknown, – n/a, with a NO_COLOR fallback of `[ok] [gap] [?] [n/a]`). The finding appears
   directly under its gap line. Citations appear only in `recommendations.md` (or behind `--verbose`). Fix the HTML
   entity leak (`&#40;45%&#41;` in terminal output). Word the score honestly, as "N of M checks confirmed from
   evidence".
6. **Facts first:** the deterministic executability result (A.3) heads each project block. Dependency wording
   distinguishes "not admitted to the packet" from "missing".
7. The reviewer menu (base branch) and consent/disclosure rules stay as they are. One disclosure covers the whole
   fan-out, and it states the number of calls and the chosen model.

## E. Init proves it works with a smoke run (added by the user, 2026-09-23)

After writing the configuration and passing the executability check (A.3), init confirms to the user that testing
actually works by running a small real test through ptest, per project.

- **ptest runs the test, never the model.** Execution goes through ptest's normal scoped runner, with the same
  isolation, timeouts, and exit-code contract as `ptest <path>`. No provider gets tools.
- **Choosing the smoke test:** deterministic by default. Prefer a small test file under the project's test roots
  that imports no database, network, or service fixtures, as the static scanner sees them. If a qualified reviewer
  is available and the user already consented to model use in this init, a cheap model (the C.4 selection; Claude
  `haiku` alias) may choose instead, from ptest's own list of candidate files. Accept only an exact listed path, and
  send no file contents beyond names and sizes.
- **Consent:** running tests executes project code. On a TTY, ask once per init: "Run a quick smoke test to confirm
  ptest works? [Y/n]", naming the files. Non-interactive init runs it only with an explicit flag (for example
  `--smoke`). `--no-smoke` skips it.
- **Report per project:** passed (with the command and duration), failed (with the command, exit code, and the
  first useful lines), or skipped (with the reason). Setup that has not been done (no `.venv` or `node_modules`) is a
  skip with the exact setup command, never a silent install. A smoke failure never rolls back the written
  configuration, and init's exit status stays configuration-based. Show the smoke result clearly.
- Must pass on persea: `api` with its serial xdist handling (A.1), and `web` with vitest execution (A.2).

## F. What `ptest --full` means (user decision, 2026-09-23)

Real projects narrow their own suite in checked-in configuration. For example, persea api has
`addopts = "-m \"not extended_migration\""` and a `conftest.py` `pytest_collection_modifyitems` hook. ptest used to
refuse `--full` for such projects.

- `--full` runs **the project's own full suite as its checked-in configuration defines it**. Narrowing that comes from
  the project's own pytest config (addopts marker/keyword filters, `-p`, `testpaths`) or from its `conftest.py`
  collection hooks is allowed in full mode.
- The result is **clearly labelled**, never silent. The human output and the result record state the filters, for
  example `full (project-filtered: -m not extended_migration; conftest collection hook)`. Init's executability line
  says the same.
- Narrowing supplied at invocation time (CLI args, `-k`/`-m`/`-x`/`--lf` passed to ptest, redirects such as `--rootdir`
  or `-c`, or `PYTEST_ADDOPTS`) is **still refused** in full mode, so a narrowed run can never pass as full.
- Unowned execution plugins, xdist, and other serial-grant rules are unchanged.
- Scope boundary: §F labels narrowing of the collected **inventory**. Outcome changes that pytest itself reports —
  skips from empty parametrize, skip marks added in hooks, `pytest.skip` in setup, `xfail` — are ordinary pytest
  outcomes, visible in pytest's summary, not project filters. `xfail`-strict manipulation needs
  `pytest_runtest_makereport`, which stays refused in full mode.
- Known limit (round 16, extended round 18): scoped mode still allows a conftest `pytest_runtest_makereport`,
  `pytest_runtest_logreport`, or `pytest_collectreport`, which can flip individual outcomes (or hide a collection
  error) before the bridge's own report counting sees them. The bridge refuses a native exit that
  hides an *observed* failure, but a rewritten report is never observed as failed, so a scoped reporting-hook flip
  stays undetected by design. Full mode refuses all four reporting hooks (`pytest_runtest_makereport`,
  `pytest_report_teststatus`, `pytest_runtest_logreport`, `pytest_collectreport`) from any plugin that is not
  `_pytest.*`, the bridge itself, or an approved module.

Guarantee boundary: ptest guarantees labelled inventory narrowing and unforged pytest reports and exit status. Project code
that replaces test bodies (for example `item.runtest = ...`) or monkeypatches pytest or bridge internals is outside that
guarantee; it is indistinguishable from editing the tests themselves.

## D. Constraints

- Follow `secure-by-spec` and `audit-spec`. All tests go through `ptest` only. Tasks never launch real
  claude/codex/opencode (use fake executables and the vendored real-reply fixtures).
- Keep code simple (explicit user preference): prefer deleting and merging code to adding layers.
- No push, no merge to main, no deploy. The controller runs the final real-provider and persea validation.
