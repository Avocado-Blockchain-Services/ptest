# Design — doctor and init v2

Author: architect (Claude Opus 5.5), 2026-09-23. Revision 2: barrier B1 plus integration task T7 (§1, §4 T7);
cross-file deletions and pinned tests assigned to their owners (T1, T2, T4, T5); new-file list §7.1.
Chain worktree: `/home/ingmar/worktrees/ptest/cc-doctor-init-v2/ptest`, branch `feature/doctor-init-v2`, base `193346e`.
Authoritative spec: `docs/superpowers/specs/2026-09-23-doctor-init-v2-requirements.md`. This design amends
`docs/superpowers/specs/2026-09-22-agent-doctor-design.md`; section 10 lists every amendment, and a pointer section
was appended to that spec.

## 0. Ground truth established before designing (reproduced, not assumed)

A scratch uv project reproduced persea `api/` exactly: pytest 9.1.1 with pytest-xdist 3.8.0, pytest-timeout, and
pytest-asyncio; `addopts = "-n 4 --dist=loadgroup -m \"not slow\""`; `timeout = 300`; a conftest that defines
`pytest_configure_node` without `optionalhook`, like `api/tests/conftest.py:90`.

| Attempt | Result |
|---|---|
| base ptest, `args = []` | refused: `pytest xdist is not owned by the serial grant` (the persea symptom) |
| `-p no:xdist -o addopts=...` | refused: `pytest execution-control plugin is not owned by the serial grant` (pytest-timeout) |
| `-p no:xdist -p no:timeout -o addopts=...` | INTERNALERROR `PluginValidationError: unknown hook 'pytest_configure_node'`, because blocking xdist removes its hookspecs and pytest `check_pending()` then rejects the conftest hook. **Blocking xdist breaks persea.** |
| `args = ["-n", "0"]` plus the bridge patch in section 2.1 | **2 passed, 1 deselected.** The `-m` filter from addopts still applies, the conftest hook loads, timeout and asyncio work, and there is no worker process |
| same, `ptest --full`, addopts without `-m` | runs serially (slow test executes) |
| same, `ptest --full`, addopts with `-m` | refused: `full pytest plans cannot accept addopts narrowing` (existing inventory policy) |

Persea `api/tests/conftest.py` also defines `pytest_collection_modifyitems` and `pytest_sessionfinish`, which the
bridge refuses in full mode. Persea api `ptest --full` therefore stays unavailable, and the executability check must
say so (section 2.3). The following were also verified: persea `web/node_modules/vitest/vitest.mjs` exists (bin
entry of vitest 3.x); persea `docs/ptest-agent.md` and the `.claude`/`.agents` skills are byte-identical to the
base templates (sha256 `72f2a5bb…` for the guide).

`codex debug models` prints `{"models":[{"slug","display_name","description","visibility":"list"|"hide",
"priority",…}]}` (≈440 KiB because of embedded `model_messages`). `claude --help` documents `--model <alias|name>`
and has no listing. `codex exec` accepts `-m, --model <MODEL>`.

## 1. Architecture and seams

```
 WAVE 1 (parallel, disjoint files)                                   WAVE 2 (after barrier B1)
            config.py ──uses──► executability.py (T1, NEW)            vitest adapter (T2)
                 │ InitResult.details notes (grammar §3.2)              operations/runners (T2)
                 ▼
 cli.py ──► init_render.render_init (T3)   [no signature change; reads notes]
    │
    ├─ doctor review (T7): build_packets ─► plan_item_reviews ─► launch_reviews ─► assemble_child
    │                      (agent_assessment, T4)                (agent_providers, T6)   (T4)
    ├─ _execution_facts(resolution) (T7) ─top-level import─► executability.check_resolution (T1)
    └─► render.render_agent_assessment / recommendations.render_recommendations (T5)
        [signatures unchanged; new data rides in the public child dicts, §3.6]
```

**Two waves and one barrier.** Wave 1 runs T1–T6 in parallel on disjoint files. **Barrier B1**: the controller
merges T1, T2, T3, T4, T5, T6 into the chain branch (in that order), and only then cuts the T7 worktree from the
chain HEAD. Wave 2 is T7 alone: it wires the new T1/T4/T6 functions into `cli.py` and owns the post-merge
integration tests. Because T7 runs alone and after every wave-1 merge, T7 may edit files that a wave-1 task owned;
that overlap is sequential, not parallel.

**No shared-file transcription task exists in this run.** `sharedFileContent` is empty. **No wave-1 task imports,
calls, or monkeypatches a file or function that another wave-1 task creates.** In particular, T6 in wave 1 does not
reference `executability`, `plan_item_reviews`, `assemble_child`, or `ItemReview` in any form (no lazy import, no
`monkeypatch.setattr(..., raising=False)`). Every cross-task dependency is one of these five kinds:

1. **Data riding existing types.** T1 → T3 uses `C.ActionRecord` notes. T5 renders plain dicts in the public
   assessment document; T7 produces them.
2. **Frozen literals.** Each side writes the literal from this document: vitest entry path, messages, labels, and
   rationale prefixes.
3. **Barrier B1.** Everything that consumes a function or file created by another task lives in T7 (wave 2):
   the top-level `from . import executability` in cli, `cli._execution_facts`, the per-item doctor flow calling
   `agent_assessment.plan_item_reviews` / `assemble_child` and `agent_providers.launch_reviews` / `with_model` /
   `cli_version` / `discover_models`, and the integration test through `cli.main` with the real functions (§4 T7).
4. **Unchanged signatures.** `render_agent_assessment(children, workspace, *, report_path, publication_status)`,
   `render_recommendations(document)`, `render_init(result, rules=None, *, dry_run, agents, repo_name, color)`, and
   `launch_review(adapter, packet, schema, timeout_s, progress)` keep their call shapes. T6 adds only a
   keyword-only `cancel=None` to `launch_review`.
5. **Deletions whose consumers live in another task's file are assigned to the consumer's owner** (§4 T2
   criterion 6, T4 criterion 9, T5 criterion 4). Deletions whose consumers are in cli wait for T7: T4 keeps
   `encode_review_request` / `parse_assessment` working in wave 1, and T7 deletes them (§3.5).

Post-merge integration evidence: T4's chain test (real `launch_review` plus a fake provider executable through
plan → assemble); **T7's `tests/ng/test_doctor_init_integration.py`, which drives `cli.main` with the real T1, T3,
T4, T5, and T6 code and only fake provider executables**; `ptest --full` run by the controller after T7; and the
controller's real-provider canaries plus persea validation.

## 2. Decisions

### 2.1 Pytest with xdist runs serially (T1)

* **Mechanism: `-n 0` appended by ptest-generated runner args.** This neutralizes xdist rather than removing it.
  The xdist flags stay in the project's addopts but become inert: xdist's own `pytest_cmdline_main` sets
  `dist="no"` and `tx=[]` when `numprocesses == 0`. Every other addopts element is preserved natively by pytest,
  with no re-quoting. `-p no:xdist` is rejected because it breaks conftests that implement xdist hooks (see §0).
  `-o addopts=` is rejected because full mode refuses `-o`, and it would require re-serializing user quoting.
* Init (`config._fresh_config`, PYTEST) writes `args = ["-n", "0"]` **iff** the static pytest configuration
  activates xdist (§3.3). Otherwise it writes `args = []`, as today.
* `adapters/pytest.py::_reject_unowned_controls` accepts exactly these serial spellings and nothing else from the
  parallel family: `-n 0`, `-n0`, `--numprocesses 0`, `--numprocesses=0`. `-n 2`, `-nauto`, `--dist=…`,
  `--maxprocesses`, `--tx`, `-f`, and `@argfile` remain rejected.
* `runtime/pytest_bridge.py::OwnedPlugin._validate`, under a one-slot grant, treats **loaded but inactive xdist** as
  allowed. Inactive means `numprocesses in (None, 0, "0")`, `tx` empty, and no `looponfail`, `px`, or `rsyncdir`.
  All plugins whose module's top-level package is `xdist` join the exempt id set, as blocked xdist already does. Any
  active xdist state still refuses with the existing messages.
* Bridge basic-serial approves the additive hook modules `pytest_asyncio` and `pytest_timeout`: module constant
  `_BASIC_APPROVED_HOOK_MODULES = ("pytest_asyncio", "pytest_timeout")`, used as the default for
  `_approved_hook_modules`. `"timeout"` is removed from the execution-control `executors` set; `forked`,
  `parallel`, `rerunfailures`, `repeat`, and `loop` stay refused. `AdvancedPlugin._approved_hook_modules` becomes
  `("pytest_cov",) + _BASIC_APPROVED_HOOK_MODULES`.

  Security rationale: neither plugin distributes, reorders, or re-runs tests. A pytest-timeout
  `timeout_method = "thread"` expiry calls `os._exit`, which loses the bridge report, and ptest already records that
  as incomplete (fail closed).
* The full-inventory policy is unchanged. Full still refuses addopts narrowing and unqualified conftest collection
  hooks. The executability check reports both (§2.3) instead of weakening full.

### 2.2 Vitest executes as an exclusive literal command (T2)

`kind = "vitest"` stays the config kind, so the existing persea `web/.ptest.toml` becomes executable without edits.
The adapter no longer uses the prepared-only bridge. It builds a literal exclusive command through the project-local
Vitest CLI:

```
scoped: launcher + ("node_modules/vitest/vitest.mjs", "run") + runner.args          # effective args already end with the caller's scope argv
full:   launcher + ("node_modules/vitest/vitest.mjs", "run") + runner.args + runner.full_args
```

* The launcher must be `("node",)` or one absolute path named `node` (`_require_node_launcher`, unchanged).
  cwd = project root. `env_updates = ()`, and operations adds the `PTEST_*` identity as for every run.
  `selected` plans raise `unsupported-capability`. `plan.files` must be empty; scope arrives through the effective
  args, as for command profiles.
* The capability is `ExecutionTier.EXCLUSIVE_COMMAND`, `selection=False`, with one limitation
  `Reason("unsupported-capability", VITEST_EXCLUSIVE_NOTE)` (§3.4).
* Registry: `RunnerAdapter(C.RunnerKind.VITEST, vitest_adapter.prepare, _exclusive=True, automatic_full=True)`,
  with no qualified profile and no advanced preparation. Delete `vitest_adapter.qualified_profile`,
  `compound_support`, `prepare_advanced`, `_scoped_files_binding`, and `_bridge_path`.
* `operations.execute`: the non-native gate becomes
  `elif config.runner.kind not in (C.RunnerKind.COMMAND, C.RunnerKind.VITEST): raise … "native profile execution is deferred"`.
  Go and Cargo stay deferred. The restriction `if not native_runner and config.setup is not None: raise … "setup
  execution is deferred for command profiles"` is **deleted**. The existing generic `_setup_prepared` path runs the
  declared setup (for example `npm ci`) for command and vitest profiles. `_setup_prepared` summarizes with
  `config.runner.kind` instead of the hard-coded `C.RunnerKind.PYTEST`.
* The deleted functions have consumers outside T2's source files in exactly one test file,
  `tests/ng/test_compound_profiles.py` (L15 import, L119–124 `compound_support`, L342–352 `prepare_advanced`).
  T2 owns that file and updates it (T2 criterion 6). Its parametrized
  `test_compound_support_is_not_inferred_from_runner_kind[VITEST]` keeps passing through the registry default
  `runners._unsupported_support`.
* `src/ptest/runtime/vitest_bridge.mjs` is **left unchanged**. `scripts/install.py` (owned by no task) asserts the
  file exists. Deleting both is a recorded follow-up.
* Vitest keeps its own worker pool. Exclusive admission accounts for the whole command. ptest claims no worker
  ownership and no per-test results; the vitest exit code is the outcome.

### 2.3 Deterministic executability check (T1, new `src/ptest/executability.py`)

The check is pure static inspection: no model, no subprocess, no imports of project code, and only no-follow bounded
reads (`files.read_regular`, `os.lstat`). It runs per project: `.` for a standalone project, or each declared child
in manifest order. The rule table is frozen in §3.3.

* `config.init_project` and `config._existing_result` append executability notes to `InitResult.details` for
  created, preview (dry-run: checks the planned in-memory config), and existing results (§3.2). Init output
  therefore always shows the check. Re-running `ptest init` in persea reports `api` as not runnable, with the exact
  `-n 0` fix, until the user applies it; existing configs are never rewritten.
  `_existing_result` appends notes **only when `resolution.problem is None`**. An existing root config that is
  invalid or unresolvable keeps its warnings and gets no notes, so `details == ()` stays true for that case
  (`tests/ng/test_init.py:604`).
* The notes change exact `details` tuples that existing tests pin in `tests/ng/test_init.py` (L462–463,
  L586–593). T1 owns `test_init.py` in wave 1 and updates those assertions (T1 criterion 6).
* Doctor (T7, wave 2) calls `executability.check_resolution(resolution)` through `cli._execution_facts` and puts
  `Executability.to_public()` into each child dict as `execution` (§3.6).

### 2.4 Init output and guidance (T3)

* `render_init` drops the duplicate historical line `created: .ptest.toml`. The Configuration section lists each
  config record once as `<action padded to 15> <path>`. The root `.ptest.toml` line is synthesized from
  `result.action` when no detail record names it. This is the existing-standalone case; the action is always one of
  created/updated/unchanged/would create.
* A new **Projects** section, between Configuration and Guidance, is built from executability notes (§3.2), one
  block per project: `project  runner  verdict`. It is followed by that project's config action line when one
  exists.
* **Next steps** lists only the `run: ` notes (verified commands), followed by the unchanged agent restart hints.
  When a project is not runnable, its `fix` is listed as `fix <project>: <fix>`. The generic `ptest --full  run the
  integrated gate…` line and the invented `ptest <child>/tests/<scope>.py` line are deleted.
* Guidance: skills become minimal pointers (front matter plus four lines). `docs/ptest-agent.md` (bundled
  `repository-agent-guide.md`) is the single place for rules, is accurate for this release, and stays at 45 lines
  or fewer. The AGENTS.md/CLAUDE.md managed block is unchanged. Upgrades are recognized: the base guide bytes
  (sha256 `72f2a5bbfcafc9b74cc2d1a7e621fe6784f67f701315d0503eef06e866989e68`) and the base `_provider_text(p)` bytes
  for all four providers count as *previous managed* content and are updated in place (action `updated`). Anything
  else still raises `already-exists` (user edits are never clobbered).

### 2.5 Doctor v2: evidence, per-item review, display (T4, T5, T6; wired by T7)

* **Admission priority** (T4, `_build_one_packet`): the candidate order is `(tier, path)` with the frozen tiers in
  §3.5 instead of the path alone. Test configuration is ranked **before** test files (amendment A3), so the 64-file
  cap can never starve `conftest.py`, which is the persea DB-002 failure.
* **Exclusions** (T4): `agent_assessment._EXCLUDED_DIRS` gains `.superpowers`, and `doctor._SKIP_DIRS` gains
  `.pipeline`, `.superpowers`, `.claude`, `.agents`, `.codex`, `.opencode`, and `.gemini`. Files ending in `.diff`
  or `.patch`, and `recommendations.md` (ptest's own report), are never admitted to packets and never scanned by
  the static doctor.
* **One focused call per (project, item)** (T4): `plan_item_reviews(packet)` returns 11 `ItemReview`s in catalog
  order. Each is one of two kinds:
  * deterministically resolved: `request is None` and a skip rationale; or
  * a request built from the item prompt, the item's routed evidence subset (at most 24 excerpts and 256 KiB), and
    the one-row response schema.

  `assemble_child(packet, reviews, replies)` validates each one-row reply against its item subset. It turns each
  failure into an `unknown` row with `Review failed: <reason>`, and computes the score with the existing `score()`.
* **Fan-out** (T6 builds `launch_reviews`; T7 wires it into the doctor flow): `agent_providers.launch_reviews` runs
  the non-skipped requests with bounded concurrency. The
  default is 4; `--review-concurrency 1..8`. The review targets one model and uses one disclosure. If every
  reviewed item fails, the review fails with `provider-failed` (exit 2) and no report is published. Otherwise
  failed items become `unknown` rows and the report is published.
* **Cheap model** (T6): §3.9.
* **Display** (T5): per-project blocks, facts first, one icon line per item with its label, and the finding directly
  under its gap line. Citations appear only in `recommendations.md`. There are no Markdown tables and no HTML
  entities in terminal output. Score wording is "N of M checks confirmed from evidence". `--verbose` is **not**
  added (the spec allows "or behind --verbose"); citations stay in the report.

## 3. Frozen interfaces (exact; do not invent alternatives)

### 3.1 `src/ptest/executability.py` (T1 creates in wave 1; T7 consumes through `cli._execution_facts` after B1)

```python
STATUS_EXECUTABLE = "executable"
STATUS_CAVEAT = "caveat"
STATUS_NOT_EXECUTABLE = "not-executable"

@dataclass(frozen=True, slots=True)
class Executability:
    project: str                 # "." or the root-relative child declaration
    runner: str                  # C.RunnerKind value, or "unknown" when no config resolved
    status: str                  # STATUS_EXECUTABLE | STATUS_CAVEAT | STATUS_NOT_EXECUTABLE
    caveats: tuple[str, ...]     # plain sentences from §3.3, no trailing period; () unless status == caveat
    reason: str | None           # set iff status == not-executable
    fix: str | None              # set iff status == not-executable
    full: bool                   # True iff `ptest --full` can execute this project (False when not-executable)
    example: str | None          # project-relative path of one existing test file for a scoped run, or None

    def verdict(self) -> str:
        # "ready" | "ready with caveats: " + "; ".join(caveats) | f"not runnable: {reason} — fix: {fix}"
    def to_public(self) -> dict:
        # {"status": status,
        #  "detail": reason if not-executable else ("; ".join(caveats) or "ready"),
        #  "fix": fix}            # fix is None unless not-executable

def check_config(config: C.Config, *, project: str = ".") -> Executability
def check_resolution(resolution: C.ConfigResolution) -> tuple[Executability, ...]
    # standalone: (check_config(resolution.config, project="."),)
    # v2 monorepo: one entry per resolution.monorepo.children declaration, manifest order; each child resolved with
    #   config.resolve_config(root / declaration); a child problem yields
    #   Executability(project=decl, runner="unknown", status=not-executable,
    #                 reason="child configuration is missing or invalid",
    #                 fix="run ptest init from the repository root", full=False, example=None, caveats=())
    # no configuration: one "." entry, runner "unknown", reason "no ptest configuration",
    #   fix "run ptest init from the repository root"
def commands(items: tuple[Executability, ...]) -> tuple[str, ...]
    # verified next-step commands, in order: for each runnable item with an example:
    #   "ptest <prefix><example>" (prefix = "" for ".", else "<project>/"); then "ptest --full" iff items is
    #   nonempty and every item.full is True. Deduplicated, max 8.
```

### 3.2 Init executability notes in `InitResult.details` (T1 writes and updates the pinned `test_init.py` tuples; T3 renders; T6/T7 tests read rendered text)

These are appended after all existing config detail records, as `C.ActionRecord(target=…, action="note",
source="config")`. They are appended for created and preview results always, and for existing results only when
`resolution.problem is None` (§2.3):

1. One **project note** per project, in project order: `target = f"{project} · {runner} · {verdict}"`. The
   separator is exactly `" · "` (space, U+00B7, space); `verdict` is `Executability.verdict()`.
2. Then one **run note** per verified command: `target = "run: " + command`, where command comes from
   `executability.commands(...)`.

T3 parses project notes with `target.split(" · ", 2)` when that yields exactly three parts. It parses run notes by
the prefix `"run: "`. Any other config note is rendered as it is today. Notes are never serialized; init JSON is
unchanged.

### 3.3 Executability rule table (T1; texts are exact)

Evaluate in order. The first not-executable rule wins. Caveats accumulate in rule order. `<cfg>` is the
root-relative path of the project's `.ptest.toml`.

| Kind | Condition | Effect |
|---|---|---|
| pytest | `_require_python_launcher` fails | not-executable; reason `pytest launcher is not a supported Python interpreter launcher`; fix `set [runner] launcher = ["uv", "run", "--locked", "--no-sync", "python"] or ["python"] in <cfg>` |
| pytest | `_reject_unowned_controls(args+full_args, full=False)` fails on token T | not-executable; reason `runner args contain a parallel, remote or argfile control (T)`; fix `remove T from [runner] args in <cfg>` |
| pytest | pytest config activates xdist, has no `no:xdist`, and args lack a serial spelling (§2.1) | not-executable; reason `pytest addopts enable xdist, which ptest runs serially`; fix `add "-n", "0" to [runner] args in <cfg>` |
| pytest | a scanned conftest defines a scoped-refused hook H (`pytest_cmdline_main`, `pytest_collection`, `pytest_runtestloop`, `pytest_runtest_protocol`, `pytest_runtest_call`, `pytest_pyfunc_call`) | not-executable; reason `<conftest path> defines H, which ptest refuses`; fix `move H out of conftest.py into an installed plugin, or configure a command profile` |
| pytest | xdist active and a serial spelling present | caveat `serial: xdist disabled under ptest (-n 0)` |
| pytest | `"."` in test_roots | caveat `ptest --full unavailable: test_roots is "."`; full=False |
| pytest | addopts contain a full-narrowing token (`-k`, `-m`, `-x`, `--exitfirst`, `--deselect`, `--lf`, `--last-failed`, `--ff`, `--failed-first`, `--sw`, `--stepwise`, `--ignore`, `--ignore-glob`, `--maxfail`≠0, or the `-kEXPR`/`-mEXPR` forms) | caveat `ptest --full unavailable: pytest addopts narrow the inventory (<tokens space-joined, option names only>)`; full=False |
| pytest | a scanned conftest defines a full-refused hook H (`pytest_collection_modifyitems`, `pytest_ignore_collect`, `pytest_runtest_makereport`, `pytest_report_teststatus`, `pytest_sessionfinish`) | caveat `ptest --full unavailable: <conftest path> defines H` (first hook of the first conftest only); full=False |
| vitest | launcher not `("node",)`, and not one absolute path named `node` | not-executable; reason `vitest launcher must be node`; fix `set [runner] launcher = ["node"] in <cfg>` |
| vitest | `node_modules/vitest/vitest.mjs` absent (lstat regular) and no setup declared | not-executable; reason `node_modules/vitest/vitest.mjs is missing`; fix `install dependencies, or declare [setup] argv = ["npm", "ci"] in <cfg>` |
| vitest | always | caveat `exclusive: Vitest runs as one command and manages its own workers` |
| command | always | caveat `exclusive: runs as one literal command` |
| go / cargo | always | not-executable; reason `native <kind> execution is not available in this release`; fix `configure kind = "command" with an explicit launcher in <cfg>` |
| any | setup declared and any `required_paths` entry absent | caveat `first run executes setup: <setup argv space-joined>` (appended last) |

* **Pytest config source.** The first file in this order that has a pytest section is used: `pytest.ini
  [pytest]`, `pyproject.toml [tool.pytest.ini_options]` or `[tool.pytest]`, `tox.ini [pytest]`, `setup.cfg
  [tool:pytest]`. `addopts` is a string split with `shlex.split` (malformed quoting means treat as no addopts) or a
  list of strings. Files are read with the existing bounded no-follow reader (256 KiB).
* **xdist activation tokens:** `-n`, `-n<X>`, `--numprocesses`, `--numprocesses=<X>`, `--dist`, `--dist=<X>`,
  `--maxprocesses`, `--maxprocesses=<X>`, `-p xdist`, `-pxdist`, `-p xdist.plugin`, `-pxdist.plugin`. `no:xdist`
  suppresses activation.
* **Conftest scan:** `conftest.py` at the project root and under each literal test root down to depth 3. Scan at
  most 64 files at 256 KiB each, sorted, and skip `node_modules`, `.venv`, `venv`, dot-directories, and
  `__pycache__`. A hook counts as defined by the regex `^(?:async\s+)?def\s+(pytest_[a-z_]+)\s*\(` per line.
  `<conftest path>` is project-relative.
* **Example test file:** a sorted, bounded walk (2,000 entries, depth 6, same skips) under the first test root. For
  pytest, the first `test_*.py` or `*_test.py`. For vitest, the first name matching
  `\.(test|spec)\.(ts|tsx|js|jsx|mts|cts|mjs|cjs)$`. If none is found, `example=None`.
* **Full per kind.** Pytest full is True unless a rule above sets it False. Vitest and command full are True. Go
  and cargo full are False.

### 3.4 Vitest execution literals (T2 defines; T1 and T6 duplicate verbatim)

```python
VITEST_ENTRY = "node_modules/vitest/vitest.mjs"            # adapters/vitest.py; T1 uses the same literal
VITEST_EXCLUSIVE_NOTE = ("Vitest runs as one exclusive command (node node_modules/vitest/vitest.mjs run); "
                         "ptest does not own Vitest workers, selection or per-test results")
```

T2 uses `VITEST_EXCLUSIVE_NOTE` as the prepared capability limitation. T6 `cli._where_payload`'s VITEST branch
becomes `Capability(execution=C.ExecutionTier.EXCLUSIVE_COMMAND, selection=False,
lifecycle="cooperative-process-group", limitations=(C.Reason(code="unsupported-capability",
message=VITEST_EXCLUSIVE_NOTE),))`, with the literal inlined in cli.py.

### 3.5 Checklist catalog, evidence admission, per-item API (T4 creates in wave 1; T7 consumes after B1)

`checklist.ChecklistEntry` gains fields. The final field order is frozen, and all construction is by keyword:

```python
@dataclass(frozen=True, slots=True)
class ChecklistEntry:
    id: str
    label: str
    criterion: str
    evidence: str
    recommendation: str
    example: str
    verification: str
    recipe: str | None
    prompt: str                      # item question + what counts as evidence + when N/A applies; absence is unknown
    path_patterns: tuple[str, ...]   # regexes matched against excerpt paths (routing)
    text_patterns: tuple[str, ...]   # regexes matched against excerpt text (routing)
    scanner_codes: tuple[str, ...]   # doctor rule codes whose hits seed routing
    skip: str | None                 # None | "no-database" | "no-cache"
```

**Labels** (exact; T5 tests and T3 docs use these):

| ID | label | skip |
|---|---|---|
| FIX-001 | Test data factories | None |
| FIX-002 | Fixture state isolation | None |
| DB-001 | Database setup reuse | no-database |
| DB-002 | Database isolation | no-database |
| CACHE-001 | Cache isolation | no-cache |
| RESOURCE-001 | Files and ports | None |
| NETWORK-001 | Network isolation | None |
| PROCESS-001 | Child processes | None |
| TIME-001 | Deterministic time | None |
| SELECT-001 | Test selection | None |
| TIMING-001 | Test timing | None |

**Admission tiers** (T4; `_build_one_packet` sorts candidates by `(tier, path)`; the order within a tier is path
order):

| Tier | Admits |
|---|---|
| 0 | root manifests and locks: basename in {pyproject.toml, package.json, Cargo.toml, go.mod, setup.py, requirements.txt, uv.lock, poetry.lock, pdm.lock, package-lock.json, pnpm-lock.yaml, yarn.lock, Cargo.lock, go.sum}, or basename matching `requirements*.txt` |
| 1 | test configuration: basename conftest.py, pytest.ini, tox.ini, setup.cfg, or basename starting with vitest.config., vite.config., jest.config., vitest.setup., vitest.workspace., or setupTests. |
| 2 | test files: any path component `tests`, `test`, or `__tests__`; or basename `test_*.py`, `*_test.py`, or `*.test.*`/`*.spec.*` |
| 3 | CI: path under `.github/workflows/`, `.circleci/`, or `.buildkite/`, or basename .gitlab-ci.yml, azure-pipelines.yml, Jenkinsfile, or cloudbuild*.yaml/yml |
| 4 | imported source: resolved from **admitted** tier-2 texts. Python `import a.b` / `from a.b import …` resolves to `a/b.py`, `a/b/__init__.py`, `src/a/b.py`, or `src/a/b/__init__.py`. JS/TS relative imports `from './x'`/`'../x'` and `require('./x')` resolve with extensions `.ts .tsx .js .jsx .mjs .cjs` and `/index.*`. Resolution stays inside the child, and only files already in the candidate list are used |
| 5 | everything else |

The existing caps are unchanged: 64 files, 512 KiB per child, 64 KiB per file, and the candidate-read budgets.

**Dependency facts** use disk presence (lstat at the child root), not admission. The status stays in the existing
closed set. The detail wording is frozen:
- `"<name> is present but was not admitted to the review packet"` uses status `uninspectable`.
- `"<name> is missing"` uses status `missing`, and applies only when the file is absent on disk.

**Per-item API** (`src/ptest/agent_assessment.py`):

```python
ITEM_MAX_FILES = 24
ITEM_MAX_BYTES = 256 * 1024
SKIP_PREFIX = "Skipped without a model call: "
FAILED_PREFIX = "Review failed: "

@dataclass(frozen=True, slots=True)
class ItemReview:
    item_id: str                    # checklist id
    label: str                      # checklist label
    scope: str                      # packet.scope
    request: bytes | None           # provider stdin bytes; None iff resolved without a model call
    schema: bytes                   # one-row response JSON schema bytes (identical for every item)
    excerpt_paths: tuple[str, ...]  # routed excerpt paths, in request order
    skip_reason: str | None         # full row rationale (starts with SKIP_PREFIX) iff request is None

def plan_item_reviews(packet: EvidencePacket) -> tuple[ItemReview, ...]
    # len == len(CATALOG), catalog order; pure (no filesystem); every request <= PROMPT_INPUT_MAX_BYTES - len(schema)

def assemble_child(packet: EvidencePacket, reviews: tuple[ItemReview, ...],
                   replies: tuple[bytes | str | None, ...]) -> ChildAssessment
    # replies aligned with reviews. bytes = normalized provider payload; str = failure reason from §3.7;
    # None iff reviews[i].request is None. A reply that fails validation becomes an unknown row
    # FAILED_PREFIX + "invalid reply". Per-item problems never raise. It raises TypeError/ValueError only for
    # misaligned inputs, and C.Problem("stale-evidence") when reviews were planned from another packet.
```

`AssessmentRow` gains a fifth field, `label: str` (nonempty), declared after `evidence`. Findings produced by
`assemble_child` have `id` equal to the item id and `recipe_id` equal to the catalog recipe, and appear in gap-row
order. This satisfies the existing contract rule that findings match the gap rows.

`encode_review_request`, `parse_assessment`, the `_RAW_*` whole-assessment shapes, `_raw_output_shape`, and
`_REVIEW_INSTRUCTION` are deleted **by T7 after B1**, together with cli's `_raw_assessment_schema` and every test
that references them (`tests/ng/test_agent_assessment.py`, `tests/ng/test_cli.py`). In wave 1, T4 keeps them
working because the base cli still calls them: `parse_assessment` fills the new `AssessmentRow.label` from the
catalog. After T7, cli must not reference any of them.

**One-row reply** (model output, exact keys): `{"status", "rationale", "evidence": [citation…≤16],
"finding": null | {"summary", "suggested_change", "evidence": [citation…1..16]}}`, where citation is
`{"path", "start_line", "end_line", "sha256"}`. Validation (T4):
- Extra, missing, or forbidden keys are invalid.
- `status` must be in the 4-value enum.
- Prose is checked with `C.aa_prose_is_untrusted` and must be at most 2,048 bytes.
- Citations must bind to the item subset excerpts (path, sha, line range).
- `satisfied`, `gap`, and `not-applicable` need at least one citation.
- `not-applicable` needs a rationale with at least 24 non-whitespace characters.
- `gap` requires a finding; non-gap requires `finding: null`.
- A model rationale starting with `SKIP_PREFIX` or `FAILED_PREFIX` is invalid.

**Deterministic skip.**
- `no-database` applies iff all of the following hold:
  - at least one tier-0 manifest excerpt is admitted; and
  - no manifest text matches the DB library regex; and
  - no packet excerpt text matches the DB usage regex; and
  - no `doctor.match_rules(text)` hit has a code starting with `db.`.
- `no-cache` applies with the same structure, using the cache regexes and the `cache.` code prefix.
- The skip row cites every admitted root manifest excerpt over its full line range.
- The rationale is `SKIP_PREFIX + "no database library in <manifest names comma-joined> and no database
  configuration or usage in the admitted evidence."` (or `…cache library…`/`…cache configuration…`).
- The regexes are conservative. Any hit means the item is reviewed.

`doctor.match_rules(text: str) -> frozenset[str]` (T4 adds it to doctor.py) is pure. It applies `_RULES` line by
line, with each line bounded to `_LINE_CHARS`, and returns the matched rule codes.

### 3.6 Public assessment child dict (T6 validates in contracts; T7 builds in cli; T5 renders; T4 unaffected)

Additive to `ptest.agent-assessment/v1`. Both fields are optional in the validator and JSON schema, and projection
keeps them when present:

```python
rows[i]["label"]      # str, 1..64 bytes, plain text (no controls), when present
child["execution"]    # {"status": "executable"|"caveat"|"not-executable",
                      #  "detail": str 1..512 bytes, "fix": str 1..512 bytes | None}; exactly these keys when present
```

`provider.profile` becomes `f"ptest-item-review-v1 model={model or 'provider-default'}"`, which is at most 128
bytes; for a longer model id, fall back to `"ptest-item-review-v1"`. `provider.cli_version` is the discovered CLI
version when known, else `"unreported"`.

### 3.7 Failure reasons (T7 maps `ProviderResult` → str in cli; T4 formats; T5 displays)

| ProviderResult | reply str |
|---|---|
| `timed_out` | `timed out` |
| `truncated` / `error == "output-exhausted"` | `output exceeded its bound` |
| `error == "tool-attempt"` | `provider attempted a tool` |
| `error == "provider-failed"` or `exit_code != 0` | `provider exited with an error` |
| `error == "invalid-assessment"` | `invalid reply` |
| launch raised `C.Problem("provider-unavailable")` for one item | `provider unavailable` |

The row rationale is `FAILED_PREFIX + reason`, so the terminal shows `unknown (review failed: <reason>)`.
`cancelled` is never per-item: Ctrl-C cancels the whole review (`review-cancelled`, exit 130).

### 3.8 Renderer signatures stay unchanged (T5 keeps; cli calls, T7 wires the new data)

```python
render.render_agent_assessment(children, workspace, *, report_path: str, publication_status: str) -> str
recommendations.render_recommendations(document: C.PublicDocument) -> bytes
init_render.render_init(result, rules=None, *, dry_run=False, agents=(), repo_name="", color=False) -> str
```

`render_agent_assessment` must contain every child `scope` and the substring `recommendations.md`. It must never
contain `&#`, `&lt;`, `&gt;`, or `&amp;`. Icons are `✓` satisfied, `✗` gap, `?` unknown, and `–` not-applicable.
When the `NO_COLOR` environment variable is set, they become `[ok]`, `[gap]`, `[?]`, and `[n/a]`. The offline
`render_doctor`, `repair_prompt`, and `render_guide` outputs are unchanged, including the "review not yet
performed" worksheet and "Findings: N total".

### 3.9 Provider runtime (T6 builds in wave 1; T7 wires into the doctor flow; public names frozen for help, README, and tests)

```python
# agent_providers.py
def launch_review(adapter, packet: bytes, schema: bytes, timeout_s: int, progress, *,
                  cancel: threading.Event | None = None) -> ProviderResult
    # existing contract; when cancel is set, stop the owned group and return error="cancelled", cancelled=True
def launch_reviews(adapter: ReviewerAdapter, requests: Sequence[tuple[bytes, bytes]], timeout_s: int, *,
                   concurrency: int = 4,
                   on_done: Callable[[int, ProviderResult], None] | None = None) -> tuple[ProviderResult, ...]
    # results aligned with requests; ThreadPoolExecutor(max_workers=concurrency), concurrency in 1..8;
    # KeyboardInterrupt in the waiting thread sets the shared cancel event, joins every worker, then raises
    # C.Problem("review-cancelled")
def with_model(adapter: ReviewerAdapter, model: str) -> ReviewerAdapter
    # claude: argv + ("--model", model); codex: argv + ("-m", model); other providers raise provider-unqualified.
    # model must match ^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$
def discover_models(adapter: ReviewerAdapter) -> tuple[str, ...]
    # codex only: run [executable, "debug", "models"] (sanitized env, no shell, 20 s timeout, 4 MiB stdout bound,
    # owned group); parse {"models":[…]}; keep entries whose visibility == "list"; return their slugs in
    # catalog order. Any failure returns ().
def cli_version(adapter: ReviewerAdapter) -> str | None
    # [executable, "--version"], 10 s, first stdout line stripped, ≤128 chars; None on failure
```

Model resolution helpers (T6 adds them to `cli.py` in wave 1 and unit-tests them; T7 calls them from the flow):

```python
# cli.py (T6)
def _declared_review_model(provider: str, override: str | None,
                           environ: Mapping[str, str]) -> str | None
    # pre-consent and subprocess-free: steps 1, 2 and 4 below; None means "decide after consent"
def _resolve_review_model(adapter: ReviewerAdapter, cache_root: Path,
                          declared: str | None) -> tuple[str | None, str | None]
    # post-consent: returns (model, cli_version); declared wins and is not cached; otherwise steps 3, 5 and 6
```

Model resolution order (cli):
1. `--review-model MODEL`.
2. The environment variable `PTEST_REVIEW_MODEL`.
3. The cache for (provider, CLI version).
4. **claude:** alias `haiku` (no discovery, no pick call).
5. **codex:** `discover_models`, then one pick call through `launch_review`. The pick request carries only the
   listed models (slug, display name, description) and asks for the single cheapest adequate slug. The reply
   `.strip()` must equal a listed slug exactly.
6. **Fallback:** the provider default model (no model flag).

Never guess. Cache a pick result under `files.ensure_private_dir(domain.root, "review-models")` /
`<provider>.json` = `{"cli_version": "...", "model": "..."}`. Overrides and fallbacks are not cached. **No provider
executable runs before consent**: version, discovery, and pick all happen after the disclosure is accepted.

CLI flags, valid exactly where `--reviewer` is valid (doctor review mode, and init with review; rejected with
`--offline`/`--json`/`--prompt`/`--probe`, `init --json`, and `init --dry-run`):

```
--review-model MODEL          # override; also PTEST_REVIEW_MODEL
--review-concurrency N        # 1..8, default 4
```

The disclosure keeps its existing first sentence (`Model review disclosure: <provider> may receive bounded source
text from project <project>…`) and adds:

`This review makes <N> model calls (<C> at a time) with model <model>.` or `…with a model chosen after consent from
the provider's model list (one extra call that sends only the model list).`

## 4. Per-task acceptance criteria

Commands: every task runs only `ptest tests/ng/<its test files>` (never pytest directly), with the prewarmed
`uv sync --locked` in its own worktree. After changing source, run `graphify update .`. Commit in the task
worktree. Report integration seams in the task report.

### T1: xdist serial and executability (owns `config.py`, `adapters/pytest.py`, `runtime/pytest_bridge.py`, `executability.py`, `test_config.py`, `test_pytest_adapter.py`, `test_executability.py`, `test_pytest_xdist_serial_subprocess.py`, `test_init.py`, `fixtures/pytest/xdist_addopts/`)

1. `_reject_unowned_controls` accepts the four serial spellings from §2.1 and rejects all other parallel/remote
   controls (parametrized tests, including `-n 2`, `-nauto`, `--numprocesses=2`, `--dist=load`, `-n` followed by
   `1`).
2. Bridge: `OwnedPlugin(1)` accepts a plugin manager listing an `xdist` plugin, and `xdist.plugin`/`xdist.looponfail`
   modules with their hookimpls, when options are inactive. It still refuses `numprocesses=2`, a nonempty `tx`,
   `looponfail`, and `px`. The old `test_loaded_xdist_is_refused_even_when_native_options_are_inactive` is replaced
   by this pair.
3. Bridge: hookimpls from modules `pytest_asyncio.plugin` and `pytest_timeout` are accepted in scoped and full
   basic-serial. A plugin named `timeout` is no longer an executor. `rerunfailures`, `repeat`, `forked`,
   `parallel`, `loop`, and conftest-defined `pytest_runtest_protocol` are still refused.
4. `config._fresh_config` writes `args = ["-n", "0"]` exactly when §3.3 detects xdist activation. Test the
   pyproject string and list forms, pytest.ini, tox.ini, setup.cfg, `-p xdist`, `--numprocesses=auto`, and
   `no:xdist` (no args). All other generated config bytes are unchanged (existing `test_config` goldens still
   pass).
5. `executability.py` implements §3.1 and §3.3 exactly. Each table row gets a unit test with the exact text. The
   check performs no subprocess or import; a monkeypatched `subprocess.run` / `importlib.import_module` must never
   be called.
6. `init_project` (created, preview, and monorepo) and `_existing_result` append notes in the §3.2 grammar. The
   test asserts the exact `details` tuple for (a) a standalone pytest project with xdist addopts and `tests/`, and
   (b) an existing persea-shaped monorepo: an `api` pytest child with addopts `-n 4 --dist=loadgroup -m "…"`,
   `args=[]`, and a conftest defining `pytest_sessionfinish`, plus a `web` vitest child with setup `npm ci` and no
   `node_modules`. Expected notes:
   - `api · pytest · not runnable: pytest addopts enable xdist, which ptest runs serially — fix: add "-n", "0" to [runner] args in api/.ptest.toml`
   - `web · vitest · ready with caveats: exclusive: Vitest runs as one command and manages its own workers; first run executes setup: npm ci`
   - one run note `run: ptest web/<example>`, and no `run: ptest --full`.

   `_existing_result` for an invalid or unresolvable root config appends no notes.
   **Pinned tests in `tests/ng/test_init.py` (T1 owns this file in wave 1).** T1 runs `ptest tests/ng/test_init.py`
   and updates exactly the assertions that the notes change, keeping their original intent:
   - `test_monorepo_dry_run_reports_would_create_without_writing` (L462–463): the two assertions apply to the
     records with `action != "note"` and keep their current expected values. A new assertion pins the exact note
     records that follow them (§3.2 grammar: one project note each for `api` and `web`, then the run notes that
     `executability.commands` yields for that tree).
   - `test_single_init_details_carry_root_config_action` (L586–593): the expected lists become the existing config
     tuple followed by the exact note tuples for that project (`(".", "pytest", <verdict>)` project note, then its
     run notes), for both the created and the preview case.
   - `test_existing_invalid_config_keeps_warnings_for_attention_header` (L604): unchanged; it must still pass
     (`details == ()`).
   - Assertions that look up targets by key or check `source == "config"` stay unchanged. Notes use
     `source="config"`, so they keep passing.

   T1 changes nothing else in `test_init.py`.
7. **Fixture `tests/ng/fixtures/pytest/xdist_addopts/`.** It holds exactly the five files below. All are
   non-collectable `.txt` files, which the test copies into a tmp project under the listed target name:

   | Fixture file | Materialized as | Content |
   |---|---|---|
   | `pyproject.toml.txt` | `pyproject.toml` | `[tool.pytest.ini_options]` with `addopts = '-p xdist.plugin -n 2 --dist=loadgroup -m "not slow"'`, `markers = ["slow: slow test"]`, `testpaths = ["tests"]` |
   | `xdist_init.py.txt` | `xdist/__init__.py` | a one-line docstring naming it a test double of pytest-xdist |
   | `xdist_plugin.py.txt` | `xdist/plugin.py` | `pytest_addoption` registering `-n/--numprocesses`, `--dist`, `--tx`, and `--maxprocesses`; `pytest_addhooks` registering a hookspec for `pytest_configure_node`; a `tryfirst` `pytest_cmdline_main` that mirrors real xdist (`numprocesses == 0` → `dist = "no"`, `tx = []`) and otherwise fails the session with "fake xdist would distribute" |
   | `conftest.py.txt` | `tests/conftest.py` | defines `pytest_configure_node(node)` as a no-op |
   | `test_sample.py.txt` | `tests/test_sample.py` | one passing test and one `@pytest.mark.slow` test |

   The subprocess test lives in the new file `tests/ng/test_pytest_xdist_serial_subprocess.py`. It uses the same
   harness style as `test_pytest_scoped_subprocess.py`, with the ptest venv interpreter as launcher, and the tmp
   project root must be importable so that `-p xdist.plugin` resolves to the fake. The test runs
   `ptest init --no-doctor --agents none`, then a scoped ptest run of `tests/test_sample.py`. Expected: the
   generated args are `["-n", "0"]`, the run exits 0, and 1 test is deselected by `-m`.

### T2: vitest executes (owns `operations.py`, `runners.py`, `adapters/vitest.py`, `adapters/simple.py`, `runtime/vitest_bridge.mjs`, `test_vitest_adapter.py`, `test_operations.py`, `test_simple_adapters.py`, `test_compound_profiles.py`, `fixtures/vitest/`)

1. `vitest_adapter.prepare` returns the §2.2 argv for scoped (with caller argv already in effective args) and full,
   with `ExecutionTier.EXCLUSIVE_COMMAND` and the `VITEST_EXCLUSIVE_NOTE` limitation. It rejects `selected`,
   non-empty `plan.files`, and non-node launchers.
2. The registry marks VITEST exclusive and automatic-full. `adapter_for(VITEST).prepare_advanced` raises
   `unsupported-capability`.
3. `operations.execute` runs a vitest config end to end with a fake `node` executable (absolute path named `node`
   in tmp; records argv and cwd; the exit code is controlled by env):
   - scoped `("src/a.test.ts",)` → argv `[node, "node_modules/vitest/vitest.mjs", "run", "src/a.test.ts"]`, cwd =
     project root, exit 0 → passed and 1 → failed;
   - full → `[node, …, "run"] + full_args`;
   - admission is exclusive.
4. The command and vitest profiles with declared `[setup]` run setup first when required paths are missing. A fake
   setup creates the path; the test asserts the setup runs once, then the test command runs. The "setup execution
   is deferred for command profiles" message no longer exists. The setup summary kind equals the config kind.
5. Go and Cargo still raise `native profile execution is deferred`. Pytest paths are untouched: existing
   `test_operations` pytest tests pass.
6. `vitest_bridge.mjs` is untouched. Tests that exercised the bridge preparation are deleted or rewritten for the
   command route. This includes `tests/ng/test_compound_profiles.py`, which T2 owns:
   - delete `test_vitest_compound_support_requires_an_exact_local_profile` (L119–124) and
     `test_vitest_advanced_prepare_refuses_before_node_launch` (L342–352). Criterion 2 covers the second one's
     intent through `adapter_for(VITEST).prepare_advanced`;
   - delete the `from ptest.adapters import vitest as vitest_adapter` import (L15) once it has no users;
   - keep `test_compound_support_is_not_inferred_from_runner_kind[VITEST]` passing unchanged.

   `ptest tests/ng/test_compound_profiles.py` passes in T2's worktree. A repo-wide
   `grep -rn "vitest_adapter\.\(compound_support\|prepare_advanced\|qualified_profile\)" src tests` returns
   nothing.

### T3: init output and guidance (owns `init_render.py`, `agent_rules.py`, `resources/agent-guide.md`, `resources/repository-agent-guide.md`, `README.md`, `test_init_render.py`, `test_agent_rules.py`, `test_resources.py`)

1. `render_init` has no `created: .ptest.toml` / `unchanged: .ptest.toml` colon lines. The root config appears
   exactly once as `<action> .ptest.toml`, matching `^\s*(created|updated|unchanged|would create)\s+\.ptest\.toml$`,
   including for an existing standalone config with empty `details`.
2. The Projects section renders §3.2 project notes: project, runner, and verdict, then the matching config line. The
   section order is Configuration < Projects < Guidance < Warnings < Next steps. The header strings are unchanged:
   `ptest initialized`, `ptest already configured`, `ptest init needs attention`, `ptest init preview`.
3. Next steps shows the commands from `run: ` notes verbatim (`ptest …`), then `fix <project>: <fix>` for
   not-runnable projects, then agent hints. It shows no invented command, and the generic `ptest --full` line
   exists only when a `run: ptest --full` note does.
4. Hostile project names and verdicts are terminal-escaped and bounded, and the box width is unchanged. JSON is
   untouched (no render involvement).
5. Guidance:
   - `repository-agent-guide.md` is 45 lines or fewer and states the following:
     - run all tests via `ptest` from the repo root with child prefixes;
     - `ptest --full` once at the end;
     - pytest runs serially under ptest, xdist is disabled with `-n 0`, and `-n`/`--dist` must not be added to ptest
       args;
     - Vitest runs as one exclusive `vitest run` command, and declared setup runs first;
     - `ptest doctor` asks consent, then sends one cheap-model call per checklist item, and `--offline` is static;
     - the existing isolation and assessment-authority rules.
   - It contains no text duplicated in the skills.
   - `agent-guide.md` keeps these phrases verbatim: `one database per worker per run`, `never use global flush`,
     `under 0.5 seconds is healthy`, `assessment authority only` (`test_install.py`/`test_render.py` rely on them).
     Its first paragraph describes doctor v2 (consented per-item review; `--offline` static).
6. Skill text is front matter (`name: ptest`, description) plus at most 4 body lines pointing at
   `docs/ptest-agent.md` and "run tests only through ptest from the repository root". The merge/graphify lines move
   out of the skills and stay only in the guide.
7. Upgrade: a repository containing the base guide bytes (sha256 `72f2a5bb…`) and base skills for all four
   providers is updated in place (`updated` actions). A user-edited guide or skill still raises `already-exists`.
   The second `init` is idempotent (all `unchanged`).
8. README describes the new init output (per-project status and verified next steps), pytest serial `-n 0`,
   exclusive vitest execution, doctor v2 (per-item review, cheap model selection, `--review-model`,
   `PTEST_REVIEW_MODEL`, `--review-concurrency`), and that citations live in `recommendations.md`.

### T4: evidence priority, per-item catalog, routing, skips (owns `agent_assessment.py`, `checklist.py`, `doctor.py`, `resources/recipes/`, `test_agent_assessment.py`, `test_agent_assessment_contract.py`, `test_doctor.py`, `test_checklist.py`, `fixtures/agent_assessment/`, `fixtures/doctor/`)

1. The catalog carries the §3.5 fields. Labels match the table exactly. Every `prompt` states the question, what
   counts as evidence, and when N/A applies, and says absence is `unknown`. `test_checklist.py` pins ids, labels,
   recipes, skip rules, and prompt invariants. The contracts-derived constants still equal the catalog.
2. Admission test with a persea-shaped tmp tree:
   - `api/.superpowers/sdd/x.diff` containing `drop_database(`;
   - `api/.pipeline/review.md`;
   - `api/recommendations.md`;
   - 80 test files under `api/tests/`;
   - `api/tests/conftest.py` of 500 lines with owned DB naming at line 447;
   - `api/pyproject.toml` and `uv.lock`.

   Expected: conftest.py is admitted with lines 1–500; nothing under `.superpowers`/`.pipeline` and no `.diff` or
   `recommendations.md` is admitted; the admitted order starts with tier 0 then tier 1; the DB-002 item's routed
   `excerpt_paths` includes `api/tests/conftest.py`.
3. `doctor.inspect` (static) does not scan `.superpowers`, `.pipeline`, `.claude`, `.agents`, `.codex`,
   `.opencode`, `.gemini`, `*.diff`, or `*.patch`: a finding planted there is absent.
4. `plan_item_reviews`:
   - returns 11 reviews in catalog order;
   - each request stays within the byte bound and carries only routed excerpts (at most 24, at most 256 KiB);
   - `no-database`/`no-cache` skips fire with the exact rationale and manifest citations for a pure-library fixture;
   - skips do **not** fire when `sqlalchemy` is declared, `redis://` appears, a `db.` scanner hit exists, or no
     manifest is admitted.
5. `assemble_child` behaviour:
   - valid replies yield rows with labels, findings in gap order with catalog recipe ids, and the `score()`
     result;
   - invalid JSON, extra keys, citations outside the subset, a gap without a finding, an injected
     `FAILED_PREFIX` rationale, or untrusted prose each yields `unknown` with `Review failed: invalid reply` for that
     item only;
   - str replies yield `Review failed: <reason>`.

   The resulting dicts, built exactly as §3.6 minus `execution`, pass `C.PublicDocument` validation.
6. Real-reply regression: each vendored real full reply in `fixtures/agent_assessment/claude-e2e-raw-assessment-*.json`
   and `codex-e2e-raw-assessment-1.json` is split per row into one-row replies. Each is re-bound to its packet
   subset, or dropped to `invalid reply` when it cites outside the subset. Assembled statuses equal the original
   statuses for every row whose citations fall in the subset.
7. Chain test: a fake `claude` executable (tmp PATH; answers from stdin by echoing a canned one-row reply wrapped
   in Claude's result envelope) goes through the **real** `agent_providers.launch_review` for every planned request.
   `assemble_child` then yields a valid child. There are no real providers and no network.
8. Dependency facts: a lock present on disk but not admitted yields `…is present but was not admitted to the review
   packet`, and a lock absent on disk yields `…is missing`.
9. `test_doctor.py` no longer asserts agent-assessment terminal layout, since layout is T5's and T5 deletes
   `render_agent_checklist_table` (T5 criterion 4). T4 deletes all seven layout tests in L1305–1520:
   - `test_agent_checklist_table_orders_rows_sanitizes_cells_and_bounds_output` (L1305)
   - `test_agent_checklist_table_escapes_html_and_markdown_in_every_cell` (L1357)
   - `test_agent_checklist_table_does_not_render_any_rationale` (L1380)
   - `test_agent_checklist_table_does_not_render_model_rationale` (L1397)
   - `test_agent_checklist_table_neutralizes_bare_urls_in_every_untrusted_cell` (L1419)
   - `test_agent_assessment_human_output_keeps_capability_claims_separate` (L1442, parametrized)
   - `test_agent_assessment_capability_line_without_dependency_codes` (L1484)

   After this, `grep -n "render_agent_checklist_table\|render_agent_assessment" tests/ng/test_doctor.py` returns
   nothing. T5 criterion 3 ports the safety intent of these tests to `test_render.py`.
   `encode_review_request`, `parse_assessment`, and their tests stay in wave 1. `parse_assessment` fills
   `AssessmentRow.label` from the catalog, and the existing tests keep passing. T7 deletes them after B1 (§3.5).

### T5: terminal display and report wording (owns `render.py`, `recommendations.py`, `test_render.py`, `test_recommendations.py`)

1. `render_agent_assessment` output per child, in this order:
   - a header `<scope> (<runner>)`;
   - `ptest: <verdict>` from `child["execution"]` (`ready` / `ready with caveats: <detail>` / `not runnable:
     <detail> — fix: <fix>`), omitted when `execution` is absent;
   - the score line `<satisfied> of <applicable> checks confirmed from evidence`, plus `; <k> not applicable` and
     `; partial evidence` when they apply, or `no applicable checks` when the score is null;
   - 11 item lines `<icon> <label>`: `label` falls back to `id` when absent; `unknown` rows show `— unknown` or
     `— unknown (review failed: <reason>)` when the rationale starts with `Review failed: `; `n/a` rows show
     `— n/a: <reason text>` with `SKIP_PREFIX` stripped, bounded to 160 chars;
   - under each gap line, an indented `finding: <summary> Suggested change: <change>`.

   Then a Dependencies section (limitation messages verbatim, sanitized), then
   `Report: recommendations.md (<status>). Citations, suggested changes and verification steps are there.` and
   `Execution verification: not run.`
2. When `execution.status == "not-executable"`, the score line reads `Checklist review only: N of M checks
   confirmed from evidence (ptest cannot run this project yet)`.
3. The icons and the `NO_COLOR` fallback are exactly as §3.8. There are no citations (`path:line`) and no `&#`,
   `&lt;`, `&gt;`, or `&amp;` in the output. A hostile summary with `<script>`, a Markdown link, a URL, `|`, or
   bidi controls is rendered inert, porting the safety assertions from the deleted T4 tests. The output is bounded
   (existing byte caps), and "pytest passed"-style claims never appear.
4. `render_agent_checklist_table`, `_agent_checklist_table_cell`, and the capability table are deleted; the
   markdown table cell escaping moves to report-only code or is deleted. Outside `render.py`, their only consumers
   are the five `test_agent_checklist_table_*` tests in `tests/ng/test_doctor.py` (L1305–1440). T4 owns that file
   and deletes those tests (T4 criterion 9), so T5 does not edit `test_doctor.py`. After the merge,
   `grep -rn "render_agent_checklist_table\|_agent_checklist_table_cell" src tests` returns nothing.
5. `recommendations.md`:
   - each item heading shows `<ID> <label>`;
   - each project section starts with the execution fact;
   - the score is worded as "N of M checks confirmed from evidence";
   - skip rows read "not applicable (skipped without a model call)" and failed rows read "unknown (review failed:
     <reason>)";
   - citations stay here.

   The footer, publication marker, guarded publication, and injection defenses are unchanged, and the existing
   recommendations tests still pass after wording updates.
6. The offline `render_doctor`, `repair_prompt`, and `render_guide` outputs are byte-identical to the base for the
   existing tests.

### T6: review runtime, contracts, flags, LOW fixes (wave 1; owns `agent_providers.py`, `cli.py`, `help.py`, `contracts.py`, `docs/schemas/v1/`, `test_agent_providers.py`, `test_cli.py`, `test_help.py`, `test_contracts.py`, `test_agent_doctor_acceptance.py`, `test_doctor_smoke.py`, `fixtures/agent_providers/`)

In wave 1, T6 does not change the doctor review flow in `cli.py` and does not reference `executability`,
`plan_item_reviews`, `assemble_child`, or `ItemReview` in any form. That wiring is T7's (§1).

1. **Contracts.** The additive §3.6 fields are validated when present, kept by projection, and appear in
   `docs/schemas/v1/agent-assessment.json`, regenerated with `scripts/export-schemas.py`. Legacy documents without
   them still decode. Bad `execution.status`, extra keys, and over-long labels are rejected.
2. **`launch_reviews` and `cancel`:**
   - with fake executables, concurrency is bounded (never more than N live children, observed through timestamps
     written by the fakes);
   - results are aligned;
   - a per-item timeout marks only that item;
   - KeyboardInterrupt (raised in the waiting thread with a monkeypatched `wait`) stops every owned group and raises
     `review-cancelled`;
   - `launch_review` without `cancel` behaves exactly as before (existing tests pass).
3. **`with_model`, `discover_models`, `cli_version`:**
   - argv gains only `--model m` (claude) or `-m m` (codex); invalid ids and flag-like ids are rejected;
   - `discover_models` parses `fixtures/agent_providers/codex-debug-models.json` (trimmed real output, §6) to
     exactly the 5 `list` slugs `gpt-6-astra, gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5`, and returns `()`
     for a nonzero exit, a timeout, malformed JSON, or over-bound output.
4. **Model resolution helpers** (`cli._declared_review_model`, `cli._resolve_review_model`, §3.9), unit-tested
   directly with fake provider executables:
   - the override flag beats the env var, which beats the cache;
   - `_declared_review_model("claude", None, {})` is `"haiku"`, and resolving it runs no discovery or pick call;
   - `_declared_review_model` never starts a subprocess (monkeypatched `subprocess.Popen`/`run` must not be
     called);
   - a codex pick reply that is not an exact listed slug (for example `"gpt-5.6-luna."` or a hidden slug) falls back
     to `None` (no `-m`);
   - the cache hits only for the same CLI version.

   The flow-level "no provider subprocess before consent" test is T7's.
5. *(moved to T7 criterion 2: doctor flow order)*
6. *(moved to T7 criterion 1: seams)*
7. **Flags** `--review-model`, `--review-concurrency`, and env `PTEST_REVIEW_MODEL`:
   - they parse with the same validity matrix as `--reviewer`, and invalid combinations exit 2 before any
     scan/launch. In wave 1 the parsed values are validated and kept on the parsed arguments. T7 passes them into
     the flow;
   - `help.py` doctor/init topics document them plus per-item review, cheap model selection, the canary note ("the
     tool-denial qualification must be re-run when the chosen model changes"), and "citations are in
     recommendations.md";
   - `test_help.py` pins the new lines.
8. **`where`** for vitest reports `exclusive_command` with the §3.4 message.
9. **LOW fixes:**
   - (a) new test: TTY `init --doctor --allow-model-review`, no concrete reviewer, two fake qualified reviewers →
     the menu is shown, answering `2` launches codex, and no `[y/N]` prompt appears. Also add
     `("doctor", "--reviewer", "auto")` as a case of one existing menu test.
   - (b) *(moved to T7 criterion 5, because `test_init.py` is T1's in wave 1)*
   - (c) delete the duplicate `test_non_tty_auto_never_shows_menu`, folding its `input` ban into the existing
     parametrized non-TTY test.
10. **Render-tolerant assertions** in `test_cli.py` and `test_agent_doctor_acceptance.py` (these must pass against
    both the base renderers and T3/T5's):
    - init text checks use `re.search(r"created:?\s+\.ptest\.toml", out)` and
      `re.search(r"unchanged:?\s+\.ptest\.toml", out)`;
    - tests expecting `ptest --full` in init output create `tests/` before `init`;
    - review-output checks assert scope names and `recommendations.md`, never `Project | Execution`.

    `test_init.py` needs no render-tolerance change: it asserts only the header strings, which are unchanged.

### T7: doctor v2 wiring and post-merge integration (wave 2, after barrier B1; owns `cli.py`, `agent_assessment.py` (deletions only), `test_cli.py`, `test_init.py`, `test_agent_assessment.py` (deletions only), `test_agent_doctor_acceptance.py`, `test_doctor_smoke.py`, `test_doctor_init_integration.py`)

**Precondition (barrier B1).** The T7 worktree is cut from the chain HEAD after the controller has merged T1–T6.
Before editing, T7 checks that the chain contains `src/ptest/executability.py`,
`agent_assessment.plan_item_reviews`, `agent_assessment.assemble_child`, `agent_providers.launch_reviews`, and
`cli._declared_review_model`. If any is missing, T7 stops and reports instead of stubbing.

1. **Seams.** cli imports `executability` at module top level (`from . import executability`), with no lazy
   import. `cli._execution_facts(resolution) -> dict[str, dict]` maps `project → Executability.to_public()` from
   `executability.check_resolution(resolution)`. cli calls `agent_assessment.plan_item_reviews` / `assemble_child`
   and `agent_providers.launch_reviews` / `with_model` directly. It maps `ProviderResult` to the §3.7 reason
   strings, and builds `provider.profile` / `provider.cli_version` per §3.6.
2. **Doctor flow order:**
   - consent check (non-TTY);
   - menu;
   - local collection and `plan_item_reviews`;
   - disclosure with call count, concurrency, and model text (§3.9, from `_declared_review_model`);
   - post-consent `_resolve_review_model` and `with_model`;
   - `launch_reviews` for the non-skipped requests only, with `--review-concurrency`;
   - `assemble_child`;
   - all-failed → `provider-failed` (exit 2, no report written);
   - stale-evidence revalidation (unchanged);
   - child dicts with `label` and `execution` (from `_execution_facts`);
   - publish and render.

   If there are zero planned calls, there is no disclosure and no launch, and the report is still produced.
3. **Deletions (§3.5).** Delete `encode_review_request`, `parse_assessment`, the `_RAW_*` shapes,
   `_raw_output_shape`, `_REVIEW_INSTRUCTION`, cli's `_raw_assessment_schema`, and every test that references
   them in `test_agent_assessment.py` and `test_cli.py`. After the deletions,
   `grep -rn "encode_review_request\|parse_assessment\|_raw_assessment_schema\|_raw_output_shape\|_REVIEW_INSTRUCTION" src tests`
   returns nothing.
4. **Integration test `tests/ng/test_doctor_init_integration.py` (new).** It drives `cli.main` with the real
   `config`, `executability`, `init_render`, `agent_assessment`, `render`, `recommendations`, `contracts`, and
   `agent_providers` code. It does **not** monkeypatch any of those modules' functions. Its only fakes are provider
   executables on a tmp `PATH` (a `claude`-shaped fake that reads the item id from its stdin request, echoes a
   canned one-row reply in Claude's result envelope, appends its argv to a log file, and exits nonzero for one
   item id selected by env), plus `stdin.isatty`/`input` where a TTY is needed. It uses no network and no real
   provider, and each fake finishes in well under a second. Cases:
   - (a) **init, persea-shaped monorepo**: `api` pytest child with addopts `-n 4 --dist=loadgroup -m "not slow"`,
     `args=[]`, and a `tests/conftest.py` defining `pytest_sessionfinish`; `web` vitest child with setup
     `npm ci`. Running `ptest init --no-doctor --agents none` on the existing configs prints a Projects line for
     `api` with the exact §3.2 `not runnable … — fix: add "-n", "0" to [runner] args in api/.ptest.toml` verdict,
     a `fix api:` next step, and no `ptest --full` line.
   - (b) **doctor review through the real per-item path**: `ptest doctor --reviewer claude --allow-model-review`
     on a tmp standalone pytest repo with a DB-using conftest. The fake's argv log shows exactly as many launches
     as there are non-skipped `ItemReview`s, and each launch has `--model haiku`. The terminal output has the
     scope header, a `ptest: …` execution line from the real `_execution_facts`, 11 labelled item lines, and no
     `|`-table header. The published `recommendations.md` has `<ID> <label>` headings. The public assessment JSON
     written by the flow passes `C` validation and contains `rows[].label` and `children[].execution`.
   - (c) **per-item failure**: with the fake failing one item, that row renders
     `unknown (review failed: provider exited with an error)` and the report is published. With the fake failing
     every item, the exit code is 2 (`provider-failed`) and no `recommendations.md` is written.
   - (d) **no launch before consent**: when the TTY disclosure is declined, the fake's argv log is empty
     (no `--version`, no discovery, no review launch).
5. **LOW fix (b)** in `test_init.py` ~L652–676: the `.ptest.toml` existence assertion moves into the fake
   `qualification_status`, and the unreachable `resolve_reviewer` fake is deleted.
6. `ptest tests/ng/test_cli.py tests/ng/test_init.py tests/ng/test_agent_assessment.py
   tests/ng/test_agent_doctor_acceptance.py tests/ng/test_doctor_smoke.py tests/ng/test_doctor_init_integration.py`
   passes. Any post-merge failure in those files is T7's to fix, preserving each test's intent.

## 5. Test approach

* TDD per task, scoped: `ptest tests/ng/test_<owned>.py …`. There is no coverage tooling or gate.
* There are no real providers, no network, and no persea access in tasks. Use fake executables in tmp PATH, the
  vendored fixtures, and tmp repos. Fixtures that look like tests use `.txt` names so ptest's own suite never
  collects them.
* Barrier B1 (controller): merge T1–T6 into the chain branch, then launch T7 from the chain HEAD.
* Post-merge (controller, after T7 is merged):
  - run `ptest --full` once;
  - run real canaries: tool-denial qualification for `claude --model haiku` and for the codex model chosen by the
    pick. This must be re-run whenever the chosen model changes;
  - validate in persea:
    - `ptest init` shows api not runnable with the `-n 0` fix; apply the fix by hand, with user consent, in the
      persea checkout;
    - `ptest api/tests/common/test_logger.py` passes serially;
    - `ptest web/<one test file>` passes;
    - `cd web && ptest --full` executes vitest;
    - `ptest doctor --reviewer claude --allow-model-review` produces per-project blocks with no DB-002 false
      finding sourced from `.superpowers`.

## 6. Fixture content frozen for T6 (new file `tests/ng/fixtures/agent_providers/codex-debug-models.json`, created by T6)

This is a trimmed capture of the real `codex debug models --bundled` output from codex-cli 0.155.1, taken on
2026-09-23:

```json
{
  "models": [
    {"slug": "gpt-6-astra", "display_name": "GPT-6-Astra", "description": "Our most capable model for complex, demanding work.", "visibility": "list", "priority": 1},
    {"slug": "gpt-5.6-sol", "display_name": "GPT-5.6-Sol", "description": "Latest frontier agentic coding model.", "visibility": "list", "priority": 6},
    {"slug": "gpt-5.6-terra", "display_name": "GPT-5.6-Terra", "description": "Balanced agentic coding model for everyday work.", "visibility": "list", "priority": 7},
    {"slug": "gpt-5.6-luna", "display_name": "GPT-5.6-Luna", "description": "Fast and affordable agentic coding model.", "visibility": "list", "priority": 8},
    {"slug": "gpt-daybreak-blue-latest", "display_name": "Daybreak Blue", "description": "Latest frontier agentic coding model for broad defensive cybersecurity work.", "visibility": "hide", "priority": 10},
    {"slug": "gpt-daybreak-red-latest", "display_name": "Daybreak Red", "description": "Cyber-permissive variant of our latest frontier agentic coding model for advanced, authorized cybersecurity research.", "visibility": "hide", "priority": 11},
    {"slug": "gpt-5.5", "display_name": "GPT-5.5", "description": "Frontier model for complex coding, research, and real-world work.", "visibility": "list", "priority": 12},
    {"slug": "gpt-5.4", "display_name": "GPT-5.4", "description": "Strong model for everyday coding.", "visibility": "hide", "priority": 16},
    {"slug": "codex-auto-review", "display_name": "Codex Auto Review", "description": "Automatic approval review model for Codex.", "visibility": "hide", "priority": 43}
  ]
}
```

## 7. File ownership and new files

Wave 1 (T1–T6) ownership is disjoint. Wave 2 (T7) runs alone after barrier B1, so its overlap with wave-1 owners is
sequential.

| Task | Wave | Owns |
|---|---|---|
| T1 | 1 | src/ptest/config.py, src/ptest/adapters/pytest.py, src/ptest/runtime/pytest_bridge.py, src/ptest/executability.py (new), tests/ng/test_config.py, tests/ng/test_pytest_adapter.py, tests/ng/test_executability.py (new), tests/ng/test_pytest_xdist_serial_subprocess.py (new), tests/ng/test_init.py, tests/ng/fixtures/pytest/xdist_addopts/ (new directory; files listed in §7.1) |
| T2 | 1 | src/ptest/operations.py, src/ptest/runners.py, src/ptest/adapters/vitest.py, src/ptest/adapters/simple.py, src/ptest/runtime/vitest_bridge.mjs (unchanged), tests/ng/test_vitest_adapter.py, tests/ng/test_operations.py, tests/ng/test_simple_adapters.py, tests/ng/test_compound_profiles.py, tests/ng/fixtures/vitest/ |
| T3 | 1 | src/ptest/init_render.py, src/ptest/agent_rules.py, src/ptest/resources/agent-guide.md, src/ptest/resources/repository-agent-guide.md, README.md, tests/ng/test_init_render.py, tests/ng/test_agent_rules.py, tests/ng/test_resources.py |
| T4 | 1 | src/ptest/agent_assessment.py, src/ptest/checklist.py, src/ptest/doctor.py, src/ptest/resources/recipes/, tests/ng/test_agent_assessment.py, tests/ng/test_agent_assessment_contract.py, tests/ng/test_doctor.py, tests/ng/test_checklist.py (new), tests/ng/fixtures/agent_assessment/, tests/ng/fixtures/doctor/ |
| T5 | 1 | src/ptest/render.py, src/ptest/recommendations.py, tests/ng/test_render.py, tests/ng/test_recommendations.py |
| T6 | 1 | src/ptest/agent_providers.py, src/ptest/cli.py, src/ptest/help.py, src/ptest/contracts.py, docs/schemas/v1/, tests/ng/test_agent_providers.py, tests/ng/test_cli.py, tests/ng/test_help.py, tests/ng/test_contracts.py, tests/ng/test_agent_doctor_acceptance.py, tests/ng/test_doctor_smoke.py, tests/ng/fixtures/agent_providers/ (including the new codex-debug-models.json) |
| T7 | 2 | src/ptest/cli.py, src/ptest/agent_assessment.py (deletions only), tests/ng/test_cli.py, tests/ng/test_init.py, tests/ng/test_agent_assessment.py (deletions only), tests/ng/test_agent_doctor_acceptance.py, tests/ng/test_doctor_smoke.py, tests/ng/test_doctor_init_integration.py (new) |

Nobody touches `scripts/install.py`, `tests/ng/test_install.py`, or `pyproject.toml`/`uv.lock` (no new
dependencies).

### 7.1 Brand-new files (exhaustive; each created by exactly one task)

| Path | Task | Needed by |
|---|---|---|
| src/ptest/executability.py | T1 | config init notes (T1), cli `_execution_facts` (T7) |
| tests/ng/test_executability.py | T1 | T1 criterion 5 |
| tests/ng/test_pytest_xdist_serial_subprocess.py | T1 | T1 criterion 7 |
| tests/ng/fixtures/pytest/xdist_addopts/pyproject.toml.txt | T1 | T1 criterion 7 |
| tests/ng/fixtures/pytest/xdist_addopts/xdist_init.py.txt | T1 | T1 criterion 7 |
| tests/ng/fixtures/pytest/xdist_addopts/xdist_plugin.py.txt | T1 | T1 criterion 7 |
| tests/ng/fixtures/pytest/xdist_addopts/conftest.py.txt | T1 | T1 criterion 7 |
| tests/ng/fixtures/pytest/xdist_addopts/test_sample.py.txt | T1 | T1 criterion 7 |
| tests/ng/test_checklist.py | T4 | T4 criterion 1 |
| tests/ng/fixtures/agent_providers/codex-debug-models.json | T6 | T6 criterion 3 (content frozen in §6) |
| tests/ng/test_doctor_init_integration.py | T7 | T7 criterion 4 |

No other new files. T2, T3, and T5 create none: T2's fake `node` and setup scripts are written into `tmp_path` at
test time.

## 8. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Cross-task seams (T1/T4/T6 functions consumed by cli) | Integration bug only visible after merge | Removed structurally: all consumers live in T7 after barrier B1; T7's integration test drives `cli.main` with the real code; T4 chain test; controller `ptest --full` and real persea validation |
| Chain between B1 and T7 still runs the old single-call doctor flow | Doctor v2 behaviour absent at that point | Expected; T4 keeps the old functions working until T7 deletes them, so the chain stays green at B1 |
| `haiku` alias unavailable in the installed Claude CLI | Every item fails → `provider-failed` | `--review-model`/`PTEST_REVIEW_MODEL` override; controller canary confirms the alias |
| Cheap model emits prose the untrusted-prose filter rejects | More `unknown (review failed: invalid reply)` rows | Per-item failure is contained; the prompt repeats the prose rules |
| Allowing pytest-timeout/asyncio hooks in basic-serial | Policy relaxation | Neither distributes or reruns; thread-timeout `_exit` loses the report → incomplete (fail closed) |
| Persea api `ptest --full` still unavailable | User expectation | Executability reports the exact cause (addopts `-m`, conftest `pytest_collection_modifyitems`/`pytest_sessionfinish`); relaxing full-inventory policy is out of scope |
| Hoisted `node_modules` (workspaces) lack `vitest.mjs` in the child | Vitest not runnable | Executability reports it with a fix; command profile remains available |

## 9. Open questions (non-blocking; defaults chosen)

- Should a future release relax full-mode policy for project-declared marker filters (`-m` in the project's own
  addopts)? Default: no; this release reports it as a caveat.
- Delete `runtime/vitest_bridge.mjs` together with its `scripts/install.py` assertion in a follow-up.

## 10. Amendments to `2026-09-22-agent-doctor-design.md`

- **A1 Fan-out.** The sequential one-call-per-child fan-out becomes one call per (project, checklist item) with a
  one-row schema. Up to 4 calls run in parallel (`--review-concurrency 1..8`). A per-item failure becomes an
  `unknown` row. When all items fail, the review fails with `provider-failed` and no report is published.
- **A2 Human output.** The capability, checklist, and Markdown tables are replaced by per-project blocks in this
  order: facts first, one icon line per labelled item, the finding under its gap. Citations appear only in
  `recommendations.md`. No `--verbose` flag.
- **A3 Evidence admission.** Tiered priority. Test configuration is ranked before test files, deviating from the
  requirement's literal order so the 64-file cap cannot starve `conftest.py`. `.superpowers`, `*.diff`, `*.patch`,
  and `recommendations.md` are excluded, and the static scanner skips agent and pipeline trees.
- **A4 Model selection.**
  - The frozen argv gains only `--model`/`-m`.
  - Claude uses the `haiku` alias without a pick call.
  - Codex uses `codex debug models` plus one exact-match pick call.
  - "Configuration" override means the env var `PTEST_REVIEW_MODEL`; `.ptest.toml` gets no new key.
  - The choice is cached per provider and CLI version.
  - The tool-denial canary must be re-run whenever the chosen model changes.
- **A5 Disclosure timing.** Local collection happens before the disclosure so it can state the call count. No
  provider executable runs before consent, including the model listing.
- **A6 Additive schema.** `rows[].label` and `children[].execution` are added to `ptest.agent-assessment/v1`.
- **A7 Pytest.** "Drop only the xdist flags" is realized as `-n 0` (neutralizes xdist; addopts are preserved
  natively), because `-p no:xdist` breaks xdist-hook conftests and `-o addopts=` is refused by full mode.
  "Pytest's declared capability remains basic-serial" still holds; loaded-but-inactive xdist and the
  pytest-asyncio/pytest-timeout hooks are allowed under the serial grant.
- **A8 Vitest.** It executes as an exclusive literal command (`node node_modules/vitest/vitest.mjs run`). Declared
  setup now runs for command and vitest profiles.
- **A9 Init.** Init output carries per-project executability and verified next steps (notes in
  `InitResult.details`; JSON unchanged).
