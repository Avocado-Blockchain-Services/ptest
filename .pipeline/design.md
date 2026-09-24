# Design: parallel pytest (xdist) under ptest, init/doctor output redesign, fewer doctor unknowns

Author: architect (Claude Opus 5.5), 2026-09-24.
Chain worktree: `/home/ingmar/worktrees/ptest/cc-parallel-and-output/ptest`, branch `feature/parallel-and-output`,
base `main` 3f399fb. Authoritative spec: `docs/superpowers/specs/2026-09-24-parallel-and-output-requirements.md`
(§P, §O, §U, §D). This design amends it and the three earlier specs; §9 lists the amendments, and the shared-file
content appends them to the specs verbatim.

Waves: T1, T2, T3, T4 run in parallel off the same base, with disjoint files. **T5 is a barrier (wave 2): it
starts only after T1–T4 are merged into the chain branch, and it is MANDATORY.** T5 imports names that T2, T3, and
T4 create. A T5 that starts before the merge must return BLOCKED; it must not stub those names.

---

## 0. Ground truth (read from the code, not assumed)

| Fact | Where |
|---|---|
| Today init writes `args = ["-n","0"]` whenever the pytest addopts activate xdist. `check_config` marks an xdist project *not runnable* unless the ptest args carry a serial spelling. | `config._fresh_config` L689; `executability.check_config` L1020 |
| Native pytest always requests exactly 1 slot. `prepare()` refuses `grant.slots != 1`. `prepare_advanced` already appends `-n <slots>` when `slots > 1`. | `operations.execute` L1938; `adapters/pytest.prepare` L286/L378 |
| The scheduler already grants `min(requested, max_slots)` and queues until that many slots are free (a memory budget without a per-worker estimate forces 1). | `scheduler._actual_slots` L759 |
| The bridge already reads `PTEST_GRANT_WORKERS` (1–64). When `workers > 1` it already expects `tx == ["popen"]*workers`, `numprocesses == workers`, and `maxprocesses ∈ {None, workers}`, and it checks gateway specs in `pytest_xdist_setupnodes`. What is missing: the xdist plugin exemption for `workers > 1`, worker observation, and cross-worker reconciliation. | `pytest_bridge.OwnedPlugin._validate` L1016–1124, L1329 |
| xdist 3.8.0 controller: `DSession.pytest_collection` returns True, so the controller never collects, never calls `pytest_collection_modifyitems`/`pytest_collection_finish`, and never runs `pytest_runtest_protocol`. It forwards every worker report through `pytest_runtest_logreport`/`pytest_collectreport`. It sets `session.testscollected = len(ids)` and calls `pytest_xdist_node_collection_finished(node, ids)`. A crash emits a synthetic failed report (`when="???"`) plus `pytest_testnodedown(node, error)`, then restarts the worker unless restarts are disabled. | persea `api/.venv/.../xdist/dsession.py` (read-only) |
| xdist workers are `execnet` popen children (plain `subprocess.Popen`, same process group). They receive `sys.path = xdist.plugin._sys_path` (the controller's `sys.path`, frozen when xdist is imported) and the controller's `invocation_params.args`. The worker returns `config.workeroutput` in its `workerfinished` event, which the controller sees as `node.workeroutput`. | xdist `remote.py`, `workermanage.py` L325–349 |
| The guard `setsid`s and kills and reaps its whole process group (`_cancel_and_reap`, `_group_needs_cleanup`, `_predecessor_quiescent`). This already covers execnet workers. | `guard.py` L279–363 |
| The private report field set is exact (`reports._FIELDS`), and its profile is `basic_serial` or `advanced`. **Neither report format changes in this design.** | `reports.py` L31–41 |
| The agent-assessment validator accepts unknown extra keys (`_check_required_keys`), and projection drops them from JSON. This precedent is `rows[].dropped_citations`. Satisfied, gap, and not-applicable rows need ≥ 1 citation. Findings need ≥ 1 citation and prose that passes `C.aa_prose_is_untrusted` (so no `ptest --x`, no `uv run`, no backticks, no `%`). | `contracts.py` L2238, L2995–3040, L2851 |
| The persea `api/.venv` has pytest 9.1.1, pytest-xdist 3.8.0, pytest-cov 7.1.0, and pytest-timeout 2.4.0. The persea `api/.ptest.toml` already has `args = ["-n","0"]` and `[selection] enabled = false`. Its addopts are `-n 4 --dist=loadgroup -m "not extended_migration"`. | persea (read-only) |
| The ptest test extra pins `pytest-xdist==3.8.0` (`uv sync --locked --extra test`). No dependency change is needed. | `pyproject.toml` L13 |

## 1. Architecture and seams

```
                 checked-in pytest config (addopts)          project env (.venv dist-info)
                                 \                               /
T2  executability.parallel_request(config) ──► ParallelRequest (qualifies? N, dist, reason)
        │                         │                                  │
        │ check_config ──► Executability(+parallel/setup/full_suite fields).facts()  ──► T5 cli ──► T3 renderers
        │                                                                    (init, doctor child["facts"], recommendations.md)
        ▼
T2  operations.execute: requested_slots = N (auto → max_slots) ──► scheduler grant (min(N, max_slots), queues)
T2  adapters/pytest.prepare: argv += ("-n", granted) | ("-n","0") ; env PTEST_GRANT_WORKERS=granted
        ▼  (guard: setsid group, kill+reap on timeout/Ctrl-C; unchanged)
T1  pytest_bridge.run (controller): exempt qualified xdist, observe forwarded reports, node collections,
        worker records; reconcile collected-vs-reported; verdict from own counts; one private report (unchanged format)
T1  pytest_bridge (loaded in each worker via -p pytest_bridge): per-worker identity env, the same hook/narrowing
        gates as serial, record → config.workeroutput["ptest_bridge"]

T4  deterministic_items.answers_for(domain, resolution, packet) ──► plan_item_reviews(packet, answers=…)
        (SELECT-001, TIMING-001 need no model call; code-signal routing; sharper prompts)
T5  cli: wires facts, the init header/footer, deterministic answers, the short disclosure, help/README, and the integration tests
```

## 2. Decisions

### 2.1 Parallel tier (§P) — T1 bridge, T2 admission

1. **Qualification is split by source:**
   - **Config-level:** the checked-in pytest addopts. Both the executability check (init, doctor) and every run decide it.
   - **Environment-level:** the xdist version in the project environment. Every run decides it again.
   - **Runtime:** the bridge is the source of truth. It re-verifies everything and fails closed.
2. **Worker count:** N is the project's `-n N` or `--numprocesses N`. `-n auto` and `-n logical` request the
   machine's `max_slots` (`scheduler.effective_limits(domain).max_slots`). `ptest --workers W` caps N at W.
   `[runner] workers` in `.ptest.toml` is ignored for pytest xdist.
3. **Slots:** ptest requests N slots. The scheduler grants `min(N, max_slots)` and queues until that many are free.
   This is deterministic and needs no scheduler change. A grant of ≥ 2 slots runs `-n <granted>`. A grant of 1 runs
   serially with `-n 0`. When the grant is below the request, the run prints one reason line
   (§3.5): `parallel-workers: 2 xdist workers (4 requested, 2 granted)`.
4. **Supported `--dist` modes:** load, loadscope, loadfile, loadgroup, and worksteal. `no` also works, because
   xdist maps it to load when `-n` is set. `xdist_group` works with loadgroup natively. `--dist each` falls back to
   serial, because it runs every test on every worker.
5. **Fallback:** ptest itself generates `-n 0` whenever an xdist-active project runs serially. Two consequences:
   - The old not-runnable rule ("pytest addopts enable xdist … add `-n 0`") is **deleted**.
   - Init writes `-n 0` only for config-level reasons (unsupported `--dist`, `--cov`, `--maxprocesses`).
     Environment reasons can change with `uv sync`, so they are never written into config.
   - Remote or rsync xdist (`--tx`, `--rsyncdir`, `--px` in addopts) stays not runnable, as today, and now has a
     static reason.
6. **Coverage (§P.5) is out of scope:** `--cov` in the addopts or runner args gives a config-level fallback with
   "coverage (--cov) under xdist is out of scope; ptest runs serially". The advanced (coverage-catalog) profile stays
   serial (`parallel_identity=False`), and `prepare_advanced` also generates `-n 0` for xdist-active projects.
7. **No new profile or tier:** `ExecutionTier`, the report format, `reports.py`, and `history.py` are unchanged.
   The private profile label stays `basic_serial`, meaning "the bridge-observed basic profile". The worker count
   is the public `granted_workers`. This keeps the change small and leaves every public schema untouched except
   one additive reason code (§3.5).
8. **Workers are observed (§P.3):**
   - Loading:
     - When `workers ≥ 2`, the bridge appends its own runtime directory to `sys.path` before `pytest.main`.
     - It prepends the internal controls `-p pytest_bridge --max-worker-restart=0` to the native argv. Every worker
       imports the bridge as a plugin, and a crashed worker is never silently replaced.
     - The worker half registers only when `config.workerinput` exists. It verifies that its own file is the
       executor's bridge (the realpath equals the `pytest_bridge.py` beside the `PTEST_BRIDGE_PROTOCOL` descriptor),
       so a project module named `pytest_bridge` fails closed.
   - Checks in the worker:
     - It applies the same serial gates: hook ownership, full-mode narrowing and redirect refusals, and refused
       report-rewriting hooks.
     - It tracks its protocol items and counts.
     - Its record goes to `config.workeroutput["ptest_bridge"]`.
   - Checks on the controller:
     - It observes the forwarded reports and the per-node collections.
     - It requires one valid record from every started worker, with no refusals. All node collections must be
       identical.
     - In full mode, every collected id must have at least one report.
     - Anything unverifiable makes the run `bridge-refused` (incomplete), never PASSED.
9. **Identity (§P.2):**
   - xdist sets `PYTEST_XDIST_WORKER=gwK`.
   - The worker half sets `PTEST_WORKER_ID=w{K:03d}`.
   - It also sets `PTEST_RESOURCE_PREFIX`: the controller prefix with its trailing `w000` replaced.
   - The run, checkout, and attempt ids are inherited from the controller environment.

### 2.2 Output (§O) — T3 renders, T2 produces the facts, T5 wires

1. **Facts:** the plain-language project facts come from one frozen dict per project (§3.1).
   - T2 builds the dict from the same `parallel_request` that admission uses, so the doctor or init says exactly
     what `ptest` will do.
   - T3 only lays the dict out, and never imports `Executability`.
2. **Width and wrapping:**
   - Width is `shutil.get_terminal_size((80, 24)).columns`, clamped to [60, 110]. Every renderer takes an explicit
     `width` override for tests.
   - Wrapping is atom-based: labels, paths, commands, and fact segments are unbreakable atoms. Prose wraps only at
     spaces. Continuations use hanging indents.
   - An atom wider than the line overflows rather than breaking.
3. **Init layout:**
   - The fixed box and the generic next steps are deleted.
   - Only the wordmark stays (amendment M8). It is colored on a TTY and plain under `NO_COLOR` or on a non-TTY.
4. **Doctor layout:** the §O.4 shape. The `--offline` static doctor is unchanged (M12).
5. **Model-review disclosure:** at most three lines before the prompt. The full legal text moves to
   `ptest doctor --help` and the README.

### 2.3 Unknowns (§U) — T4

1. **Deterministic answers:** SELECT-001 (the spec's "SELECTION-001") and TIMING-001 get answers from ptest's own
   facts, with no model call.
   - Each answer is an `ItemReview` with `request=None, skip_reason=None, answer=<DeterministicAnswer>`.
   - The row rationale starts with `Answered by ptest: `.
   - Satisfied, gap, and not-applicable answers cite the project's `.ptest.toml` excerpt. If the packet lacks it,
     the answer degrades to unknown with the reason.
2. **Code-signal routing:** new text signals for RESOURCE-001, NETWORK-001, and PROCESS-001. Conftest excerpts that
   define fixtures used by the routed tests rank first.
3. **Prompts:** sharper prompts. A gap needs a concrete cited violation. Satisfied needs the guaranteeing mechanism.
   Otherwise the answer is unknown with one sentence naming the missing evidence.

## 3. Frozen interfaces (exact; no task may invent an alternative)

### 3.1 Project facts: `Executability` fields and `facts()` (T2 implements; T3 renders plain dicts; T5 wires)

```python
# src/ptest/executability.py  (T2)
FACT_KEYS: tuple[str, ...] = (
    "project", "runner", "runs", "runs_reason", "runs_fix",
    "parallel", "parallel_short", "parallel_fix",
    "setup", "full_suite", "full_blocked",
)

@dataclass(frozen=True, slots=True)
class Executability:
    project: str
    runner: str
    status: str
    caveats: tuple[str, ...]
    reason: str | None
    fix: str | None
    full: bool
    example: str | None
    # new, defaulted, appended AFTER the existing fields (positional constructors keep working)
    parallel: str | None = None        # text after "parallel: " (long form); None = omit
    parallel_short: str | None = None  # text after "parallel: " for one-line summaries; None iff parallel is None
    parallel_fix: str | None = None    # actionable fix when parallel is off only because of the ptest config
    setup: str | None = None           # " ".join(config.setup.argv); None = no [setup]
    full_suite: str | None = None      # text after "full suite = "; None = omit the line
    full_blocked: str | None = None    # text after "full suite: not available — "; None unless full is False

    def facts(self) -> dict:           # keys exactly FACT_KEYS, in that order
        not_runnable = self.status == STATUS_NOT_EXECUTABLE
        return {"project": self.project, "runner": self.runner, "runs": not not_runnable,
                "runs_reason": self.reason if not_runnable else None,
                "runs_fix": self.fix if not_runnable else None,
                "parallel": self.parallel, "parallel_short": self.parallel_short,
                "parallel_fix": self.parallel_fix, "setup": self.setup,
                "full_suite": self.full_suite, "full_blocked": self.full_blocked}
```

Value types: `runs` is a bool. Every other value is `str` (1–512 characters, no control characters) or `None`.
`project` is `"."` or a declaration such as `"api"`. `runner` is one of `pytest`, `vitest`, `command`, or `unknown`.
`to_public()` keeps its exact shape `{"status","detail","fix"}`.

**Exact texts (T2 produces these; T3 and T5 tests may use them as literals).** `{cfg}` is `.ptest.toml` for project
`.` and `{project}/.ptest.toml` otherwise, the existing `_cfg`.

| Case | `parallel` | `parallel_short` | `parallel_fix` |
|---|---|---|---|
| pytest, tier qualifies, `-n N` | `f"{N} workers (xdist, --dist {dist})"` | `f"{N} workers"` | None |
| pytest, tier qualifies, `-n auto`/`logical` | `f"one worker per granted slot (xdist -n auto, --dist {dist})"` | `"auto"` | None |
| vitest | `"inside vitest (its own workers)"` | `"inside vitest"` | None |
| command runner | None | None | None |
| pytest, xdist not active | `"no — xdist is not enabled in your pytest config"` | `"no"` | None |
| pytest, fallback reason R (below) | `f"no — {R}"` | `"no"` | None |
| pytest, otherwise qualified but ptest args carry `-n 0`/`-n0`/`--numprocesses=0` | `f"no — {cfg} sets -n 0"` | `"no"` | `f'remove "-n", "0" from [runner] args in {cfg} to run {N} workers'` (auto: `… to run in parallel`) |

`{dist}` is the effective mode: the declared `--dist`, or `load` when it is unspecified or `no`.

Fallback reasons R, in precedence order (first match wins; `config_level` marks the ones init writes `-n 0` for):

| # | R (exact) | config_level | runs |
|---|---|---|---|
| 1 | `remote xdist workers (--tx, --rsyncdir, --px) are not supported` | False | **False** (reason = R, fix = `remove --tx, --rsyncdir and --px from your pytest addopts`) |
| 2 | `f"--dist {mode} is not supported; ptest runs serially"` | True | True |
| 3 | `coverage (--cov) under xdist is out of scope; ptest runs serially` | True | True |
| 4 | `--maxprocesses is not supported; ptest runs serially` | True | True |
| 5 | `your pytest config asks for 1 worker` | False | True |
| 6 | `f"ptest cannot verify pytest-xdist for launcher {name}; use an absolute interpreter or a uv launcher to run in parallel"` | False | True |
| 7 | `pytest-xdist is not installed in the project environment yet; ptest runs serially until setup installs it` | False | True |
| 8 | `more than one pytest-xdist install in the project environment; ptest runs serially` | False | True |
| 9 | `f"pytest-xdist {version} is not qualified (ptest supports 3.8.0); ptest runs serially"` | False | True |

The ptest-args `-n 0` row is checked after rows 1–9: an actionable fix is shown only when removing `-n 0` would
really give parallel runs.

- **`full_suite`:** set when `full` is True and the static filter scan finds narrowing. The value is
  `f"your pytest config: {', '.join(parts)}"`.
  - `parts` holds the allowlisted checked-in narrowing tokens as `option value`, with the value in double quotes
    when it contains whitespace. Example: `-m "not extended_migration"`.
  - Add `conftest.py hooks` once when any conftest collection or sessionfinish hook exists.
  - Persea example: `your pytest config: -m "not extended_migration", conftest.py hooks`.
- **`full_blocked`:** set when `full` is False. It is the existing text after `ptest --full unavailable: `, for
  example `test_roots is "."`.
- **`setup`:** example `uv sync --locked`.
- **`verdict()`** keeps feeding the init notes `f"{project} · {runner} · {verdict}"`. Its new wording:
  - Not runnable: `f"runs: no — {reason} → {fix}"`.
  - Otherwise: `"runs: yes"` followed by `"; " + caveat` for each caveat.
- **Caveats** are the plain lines below, in this order. Status is `caveat` iff there are any caveats.
  - `f"parallel: {parallel}"`
  - `f"setup: {setup} (ptest runs it when needed)"`
  - `f"full suite = {full_suite}"`
  - `f"full suite: not available — {full_blocked}"`
- The strings `ready with caveats`, `expected:`, `fingerprint`, and `serial: xdist disabled under ptest (-n 0)`
  disappear from executability output.

### 3.2 Parallel request (T2; consumed by T2 internally and optionally by T5 `cli._summary`)

```python
# src/ptest/executability.py  (T2)
XDIST_QUALIFIED_VERSIONS = frozenset({"3.8.0"})   # mirror of pytest_bridge.QUALIFIED_XDIST_VERSIONS
XDIST_DIST_MODES = frozenset({"load", "loadscope", "loadfile", "loadgroup", "worksteal"})  # mirror of pytest_bridge.PARALLEL_DIST_MODES

@dataclass(frozen=True, slots=True)
class ParallelRequest:
    active: bool          # checked-in pytest config activates xdist (existing _xdist_active semantics)
    workers: int | None   # N from -n N / --numprocesses N; None when auto/logical or inactive
    auto: bool            # -n auto / -n logical
    dist: str             # effective --dist ("load" when unspecified or "no")
    reason: str | None    # None <=> the parallel tier qualifies (config + environment); else R from §3.1
    config_level: bool    # reason is config-level (init writes -n 0)
    runs: bool            # False only for R1 (remote/rsync)

def parallel_request(config: C.Config, *, project: str = ".") -> ParallelRequest: ...
def xdist_environment_version(config: C.Config) -> tuple[str | None, str | None]:
    """(version, None) when found; (None, R6|R7|R8 text) otherwise. Static, bounded, no symlink follow."""
```

Environment probe (exact rules):
- **Launcher to venv:**
  - `("uv","run","--locked","--no-sync","python")` → `<project root>/.venv`.
  - `("uv","run","--locked","--no-sync","--project",P,"python")` → `Path(P)/.venv`.
  - A single absolute interpreter `X` → `Path(X).parent.parent`. Do not resolve symlinks, and require `pyvenv.cfg`
    there.
  - Anything else → R6, with `name = Path(launcher[-1]).name`.
- **Dist-info lookup:** list `<venv>/lib/python3.*/site-packages/pytest_xdist-*.dist-info` (at most 8 python dirs,
  lstat, real directories only).
  - Zero hits → R7. More than one → R8.
  - Version = the text between `pytest_xdist-` and `.dist-info`. Outside `XDIST_QUALIFIED_VERSIONS` → R9.

### 3.3 Adapter → bridge contract (T2 produces, T1 consumes)

- **argv (basic profile):** `launcher + (bridge_path,) + runner.args + (runner.full_args + test_roots | plan.files) + generated`.
  - `generated = ("-n", str(grant.slots))` iff `grant.slots >= 2`. This requires that `parallel_request(config).reason`
    is None; otherwise `admission-invalid`.
  - `generated = ("-n", "0")` iff `grant.slots == 1`, `request.active`, and the ptest args carry no serial spelling.
  - Otherwise `generated = ()`.
  - `prepare_advanced` applies the same `-n 0` rule (it never gets > 1 slot).
- **env:** unchanged from base. `PTEST_GRANT_WORKERS=str(grant.slots)`, `PTEST_WORKER_ID=w000`, and
  `PTEST_RESOURCE_PREFIX=pt_{checkout[:8]}_{run_id}_{attempt}_w000`. No new variables.
- **Summary:** `summary.generated_options = ("pytest-xdist.workers=%d" % slots,)` when `slots >= 2`, else unchanged.
- **Serial compatibility:** with `workers == 1`, the bridge must behave exactly as at base. Every existing serial
  test must pass unchanged.

### 3.4 Bridge constants and worker contract (T1; T2 mirrors the constants; T5 asserts equality)

```python
# src/ptest/runtime/pytest_bridge.py  (T1)
QUALIFIED_XDIST_VERSIONS = frozenset({"3.8.0"})
PARALLEL_DIST_MODES = frozenset({"load", "loadscope", "loadfile", "loadgroup", "worksteal"})
_WORKER_CONTROLS = ("-p", "pytest_bridge", "--max-worker-restart=0")   # prepended to native argv iff workers >= 2
```

Refusal messages, exact (T5 may assert them on stderr through the `ptest-bridge-refusal:` marker):
- `f"pytest-xdist {version} is not qualified for parallel runs"` (code `unsupported-capability`). Also used when the
  distribution is missing, with version `missing`.
- `f"pytest xdist --dist {mode} is not supported in parallel runs"`
- `"a parallel worker was not observed by the bridge"`
- `"parallel workers collected different tests"`
- `"full pytest run left collected items unrun"` (existing text, now also used in parallel)
- `"native exit hides observed test failures"` (existing)
- `"unqualified xdist scheduling or crash hook is not owned by the parallel grant"` (a non-xdist implementation of
  `pytest_xdist_make_scheduler`, `pytest_xdist_getremotemodule`, or `pytest_handlecrashitem`)

### 3.5 Run reason for parallel (T2)

A new additive reason code `"parallel-workers"`. It is added to `C.REASON_CODES` and to every
`docs/schemas/v1/*.json` enum that lists reason codes, except `agent-assessment.json`. It is appended to
`RunResult.reasons` of pytest runs for xdist-active projects **only when** the grant is below the request, or when the
run is serial because of a fallback. The cli prints it as `parallel-workers: <message>`. Messages, exact:
- `f"{g} xdist workers ({r} requested, {g} granted)"` when 2 ≤ g < r
- `f"serial ({r} requested, 1 granted)"` when g == 1 < r
- `f"serial: {R}"` when fallback reason R applies (§3.1, rows 2–9 and the `-n 0` row)

### 3.6 Init rendering (T3) and init flow (T5)

```python
# src/ptest/init_render.py  (T3)
def render_init(result: C.InitResult, rules: object = None, *, dry_run: bool = False,
                agents: tuple[str, ...] = (), repo_name: str = "", color: bool = False,
                facts: Sequence[Mapping[str, object]] = (), width: int | None = None) -> str
    # wordmark + "ptest <version>" line, header, project lines, grouped file actions, warnings. No smoke, no next steps.
def render_init_footer(result: C.InitResult, rules: object = None, *, dry_run: bool = False,
                       smoke: Sequence[object] = (), plans: Sequence[object] = (),
                       facts: Sequence[Mapping[str, object]] = (), width: int | None = None) -> str
    # smoke line (only if smoke), actionable next steps (only if any), one restart line (only if a skill was created/updated). May be "".

# src/ptest/init_smoke.py  (T3)
@dataclass(frozen=True, slots=True)
class SmokeResult: ...existing fields...; setup_argv: tuple[str, ...] | None = None   # appended last; set by skip_result for setup-pending skips
def format_smoke(results: tuple[SmokeResult, ...], *, width: int | None = None) -> str  # compact block; "" when empty
```

- **Header** (exact phrases kept; `test_init.py` asserts one of them):
  `f"{phrase} · {repo_name}"`, where the phrase is `ptest initialized`, `ptest init preview`,
  `ptest init needs attention`, or `ptest already configured`. When `repo_name` is empty, the phrase stands alone.
- **Project line:**
  `"  {project}  {runner}  runs: yes · parallel: {parallel_short} · setup: {setup}"`. Omit a segment whose value is
  None. Put `full suite = {full_suite}` (or `full suite: not available — {full_blocked}`) on a hanging line under
  `runs`. A not-runnable project shows `runs: no — {runs_reason} → {runs_fix}`.
- **Without facts:** when `facts` is empty (dry-run), the project lines come from the init notes' first two
  ` · ` fields (`project runner`) only.
- **File actions** are grouped by action, one line per action:
  - Config line: `config     unchanged  .ptest.toml, api/.ptest.toml`.
  - Guidance line: `guidance   created    docs/ptest-agent.md, ptest skill for claude, codex, opencode, gemini`.
  - Skill paths map to `ptest skill for <agent>`: `.claude/skills/ptest/SKILL.md`→claude,
    `.agents/skills/ptest/SKILL.md`→codex, `.opencode/skills/ptest/SKILL.md`→opencode,
    `.gemini/skills/ptest/SKILL.md`→gemini.
- **Smoke cells:**
  - Passed: `f"{project} ✓ {s:.1f}s"`, or `f"{project} [ok] {s:.1f}s"` under `NO_COLOR`.
  - Failed: `f"{project} ✗ exit {code}"`, with at most 5 indented detail lines below.
  - Skipped: `f"{project} – {reason}"`.
- **Next steps** (exact substrings, one per line; shown only when there is at least one):
  - `f"{project}  not runnable → {runs_fix}"`
  - `f"{project}  parallel off → {parallel_fix}"`
  - `f"{project}  setup pending → run: {display_command} (runs {setup} first)"`. This comes from `plans` with
    `setup_argv` whose project had no passed smoke. `display_command` is `init_smoke.display_command(project, candidate)`.
  - `f"{project}  smoke failed → see the runner output above"`
- **Restart line** (exactly one, only when a skill target in `rules.details` has action `created` or `updated`):
  `"Restart your coding agents to load the new ptest skill."`, or `"… the updated ptest skill."` when every skill
  action was `updated`. Never shown for previews.

**T5 init flow:**
1. Write `render_init(…, facts=facts)`.
2. Build `plans = init_smoke.plan_resolution(...)`, best-effort (`()` on error).
3. Get the smoke results through the refactored `_init_smoke_results(parsed, cwd, plans, domain)`, which may print
   live runner output.
4. Write `render_init_footer(…, smoke=results, plans=plans, facts=facts)`.
5. Offer the review as today.

`facts = tuple(item.facts() for item in executability.check_resolution(config_api.resolve_config(cwd)))`,
best-effort, `()` for `--dry-run` and on error. `--json` stays byte-compatible (no facts, no footer).

### 3.7 Doctor child `facts` (T5 writes, T3 reads)

- **T5:** `cli._child_assessment_data(packet, assessment, limitations, *, execution=None, facts=None)` sets
  `child["facts"] = facts` (exact `FACT_KEYS` dict) when facts is not None.
- **Validator and projection:** `contracts` tolerates the extra key, and the projection drops it, so
  `--assessment-json` output is unchanged. T5 tests this.
- **T3:** `recommendations._normalize_run` keeps a validated copy (keys ⊆ `FACT_KEYS`, types as in §3.1; invalid →
  None). `render_agent_assessment` reads `child.get("facts")`. When facts are missing or invalid, both fall back to
  `child["execution"]`: `runs: yes`, or `runs: no — {detail} → {fix}`.

### 3.8 Doctor terminal and report (T3)

```python
# src/ptest/render.py  (T3)
def render_agent_assessment(children, workspace, *, report_path: str, publication_status: str,
                            width: int | None = None) -> str
PTEST_ANSWER_PREFIX = "Answered by ptest: "   # literal mirror of agent_assessment.PTEST_ANSWER_PREFIX (T5 asserts equality)
# src/ptest/project_facts.py  (T3, new) — pure helpers used by init_render, render, recommendations
MIN_WIDTH, MAX_WIDTH = 60, 110
def terminal_width(width: int | None = None) -> int
def check_facts(value: object) -> dict | None
def summary_atoms(facts: Mapping[str, object]) -> list[str]        # ["runs: yes", "parallel: 4 workers", "setup: uv sync --locked"]
def detail_lines(facts: Mapping[str, object]) -> list[str]         # ["full suite = …"] or ["full suite: not available — …"] or []
def long_lines(facts: Mapping[str, object]) -> list[str]           # report: runs / parallel (long) / setup "(ptest runs it when needed)" / full suite
def wrap_atoms(atoms: Sequence[str], width: int, *, indent: str = "", hang: str | None = None,
               sep: str = " · ") -> list[str]
def wrap_words(text: str, width: int, *, indent: str = "", hang: str | None = None) -> list[str]
```

**Header line:**
`f"{scope}  {runner} · {ok} ok · {gap} gap · {unknown} unknown"`. Add `+ " · {n} n/a"` when n > 0. Add
`"   (partial evidence)"` when there is a partial-evidence limitation, and
`"   (checklist only: ptest cannot run this project yet)"` when the project is not runnable.

**Body lines:**
- The summary-atoms line and the detail lines, indented 2.
- A blank line.
- Satisfied rows compacted into width-fitting columns (`✓ Label`, keeping `_dropped_suffix`).
- One line per gap: `✗ Label`. Under it, the wrapped `finding.summary` (indent 6), then `→ suggested_change`.
- One line per unknown: `? Label  <reason>`. The reason is:
  - the rationale with `PTEST_ANSWER_PREFIX` stripped;
  - for a `FAILED_PREFIX` rationale, `review failed: <reason>`;
  - otherwise the model's first sentence.
  The reason is word-cut to the width.
- One line per n/a: `– Label  <reason>`, with the SKIP_PREFIX or PTEST prefix stripped.

**Trailer:** one line, `f"Report: {path} ({status}) — citations, fixes and verification steps."`. The 256 KiB bound
is kept.

**Report (`recommendations.md`):**
- The "Execution:" line is replaced by the `long_lines(facts)` bullets.
- Unknown rows print `Status: unknown.` then `Reason: <rationale without prefix>`. PTEST rows say
  `(answered by ptest without a model call)`.
- All other content is kept.

### 3.9 Deterministic items (T4; T5 wires)

```python
# src/ptest/deterministic_items.py  (T4, new)
DETERMINISTIC_ITEM_IDS: tuple[str, ...] = ("SELECT-001", "TIMING-001")

@dataclass(frozen=True, slots=True)
class DeterministicAnswer:
    item_id: str                           # one of DETERMINISTIC_ITEM_IDS
    status: str                            # "satisfied" | "gap" | "unknown" | "not-applicable"
    reason: str                            # one plain sentence, no prefix, no backticks
    evidence_paths: tuple[str, ...] = ()   # packet excerpt paths to cite (root-relative, as in packet.excerpts)
    finding_summary: str | None = None     # gap only; passes C.aa_prose_is_untrusted() is False
    finding_change: str | None = None      # gap only; same rule

def answers_for(domain: C.DomainPaths, resolution: C.ConfigResolution, packet) -> dict[str, DeterministicAnswer]:
    """Never raises. {} when the packet's child config does not resolve."""

# src/ptest/agent_assessment.py  (T4)
PTEST_ANSWER_PREFIX = "Answered by ptest: "
def plan_item_reviews(packet: EvidencePacket, answers: Mapping[str, object] | None = None) -> tuple[ItemReview, ...]
@dataclass(frozen=True, slots=True)
class ItemReview:  # existing fields unchanged, plus:
    answer: object | None = None   # DeterministicAnswer; exactly one of request / skip_reason / answer is set
```

- **Row from an answer** (built in `assemble_child`, which takes `None` as the reply, like a skip):
  - `rationale = PTEST_ANSWER_PREFIX + answer.reason`.
  - `evidence` holds whole-excerpt citations of `evidence_paths`.
  - A gap adds `Finding(id, finding_summary, finding_change, recipe_id=None, evidence=same citations)`.
- **Degrade:** if a satisfied, gap, or not-applicable answer has no citable excerpt, it becomes unknown. Its
  rationale is `PTEST_ANSWER_PREFIX + reason + " (the ptest config is not in the review evidence)"`.
- The model is told never to start a rationale with this prefix. `_validate_one_row` rejects one that does.

Exact deterministic reasons (T3 and T5 tests use them as literals):

| Item | Condition | status | reason |
|---|---|---|---|
| TIMING-001 | no completed full run in history (or no history) | unknown | `no timing history yet: run ptest --full once` |
| TIMING-001 | history unreadable | unknown | `ptest history is unavailable, so timing cannot be read` |
| TIMING-001 | full runs exist, no per-test timings | unknown | `f"ptest has whole-run timing only (last full run {s:.1f} s); per-test timings are not recorded for this runner"` |
| TIMING-001 | last clean baseline has per-test `call_s` | satisfied | `f"ptest recorded per-test timings for {n} tests in the last clean full run; {k} take over 3 s (slowest {m:.1f} s)"` |
| SELECT-001 | runner vitest or command | not-applicable | `f"ptest has no automatic test selection for {runner}; every run is scoped or full"` |
| SELECT-001 | pytest, `[selection] enabled = false` | gap | `f"selection is disabled in {cfg}"` |
| SELECT-001 | pytest, enabled, not `closed_inputs` or no `input_roots` | gap | `f"selection inputs are not declared closed in {cfg}"` |
| SELECT-001 | pytest, enabled, closed, input_roots nonempty | satisfied | `f"selection is enabled with closed inputs ({i} input roots, {t} full triggers); unknown input widens to the full suite"` |

TIMING never answers gap (M9). The finding texts for the SELECT gaps are T4's choice. They must pass
`aa_prose_is_untrusted() is False` and name the concrete fix:
- Enable `[selection]` with `closed_inputs`, `input_roots`, and `full_triggers` in `{cfg}`.
- For pytest without the coverage catalog profile (`adapters.pytest.qualified_profile(config) is None`), first add
  `--cov` and `--cov-report` to `[runner] args`.

History access is read-only: `history.read_history` and `history.read_history_summaries` inside try/except. It must
never call `scheduler.initialize`. The checkout identity uses the `cli._checkout` derivation
(`sha256(realpath(root))[:32]`).

**T5 wiring in `_run_doctor_review`:**
`plans = [agent_assessment.plan_item_reviews(packet, answers=deterministic_items.answers_for(domain, resolution, packet)) for packet in packets]`.
The disclosure call count needs no other change (`review.request is not None`).

### 3.10 Disclosure (T5)

At most three lines, then the prompt:

```
Model review disclosure: {provider} gets bounded source excerpts from {project}: {calls} calls, {concurrency} at a time, model {model}.
Secrets, private files, agent instructions, dependency folders, caches and build output are never sent. Provider costs may apply.
Full disclosure: ptest doctor --help. Static review without a model: ptest doctor --offline.
Run this review once? [y/N]:
```

- **Unknown model:** when the model is unknown before consent, the tail of line 1 is `model chosen from the
  provider list after consent (one extra call sends only that list)`.
- **No call count:** when `calls is None`, line 1 ends after `{project}.`.
- **Fixed prefix:** line 1 always starts with `Model review disclosure: {provider} ` (`test_init.py` and
  `test_agent_doctor_acceptance.py` rely on it).
- **Newline:** a newline is printed before the disclosure whenever stderr is a TTY. Every other interactive prompt
  after the spinner does the same.

### 3.11 Names that must survive (no task may rename or remove them)

- **executability:** `check_config`, `check_resolution`, `commands`, `pytest_xdist_active`,
  `full_project_filter_text`, `full_project_filter_label`, `_pytest_addopts`, `STATUS_*`, `Executability.verdict`,
  `to_public`.
- **adapters/pytest:** `prepare`, `prepare_advanced`, `reject_unowned_controls`, `require_python_launcher`,
  `qualified_profile`, `compound_support`, `inspect_capability`.
- **agent_assessment:** `FAILED_PREFIX`, `SKIP_PREFIX`, `build_packets`, `plan_item_reviews` (packet stays the first
  positional argument), `assemble_child`, `ItemReview`, `AssessmentRow`, `ChildAssessment`.
- **render:** `terminal_text`, `render_agent_assessment`, `_dropped_suffix`, `_word_cut`, `render_doctor`,
  `render_json`.
- **init_render:** `render_init`.
- **init_smoke:** `plan_resolution`, `run_plan`, `run_setup`, `skip_result`, `setup_advice`, `display_command`,
  `parse_consent`, `SMOKE_QUESTION`, `SETUP_QUESTION`, `SmokePlan`, `SmokeResult`, `format_smoke`.
- **recommendations:** `render_recommendations`, `publish_recommendations`, `PublishedIdentity`.
- **pytest_bridge:** `run`, `cluster_narrow_name`, `short_redirect_cluster`, `full_redirect_name`,
  `full_ini_refusal_name`, `full_narrowing_text`, `full_refusal_name`, `OwnedPlugin`, `AdvancedPlugin`,
  `BridgeRefusal`.

## 4. Per-task acceptance criteria

Every task runs `uv sync --locked --extra test` in its own worktree first. It never shares a `.venv`. It commits
inside its worktree, never pushes or merges, and runs tests only through `ptest`. Every task deletes code it makes
dead.

### T1: parallel bridge (owns `src/ptest/runtime/pytest_bridge.py`, `src/ptest/runtime/protocol-v1.json`, `tests/ng/test_pytest_parallel_subprocess.py` (new), `tests/ng/fixtures/pytest/parallel/` (new), `tests/ng/test_pytest_full_subprocess.py`, `tests/ng/test_pytest_scoped_subprocess.py`)

1. **Constants:** the §3.4 constants exist with the exact values.
2. **Serial path unchanged:** with `PTEST_GRANT_WORKERS=1`, behavior is byte-identical to base.
   `test_pytest_full_subprocess.py`, `test_pytest_scoped_subprocess.py`, and (read-only for T1)
   `test_pytest_xdist_serial_subprocess.py` pass without edits.
3. **Pre-flight when `workers >= 2` (basic profile):**
   - `run()` verifies `importlib.metadata.version("pytest-xdist") ∈ QUALIFIED_XDIST_VERSIONS` before `pytest.main`
     (§3.4 message).
   - It appends the bridge directory to `sys.path` and prepends `_WORKER_CONTROLS` to the native argv.
   - Advanced with `workers > 1` stays refused (unchanged).
4. **Controller:**
   - Exempts plugins whose module root is `xdist` only when all of these hold: `numprocesses == workers`,
     `tx == ["popen"]*workers` (after expansion), `dist ∈ PARALLEL_DIST_MODES`, `maxprocesses ∈ {None, workers}`,
     and no px, rsync, looponfail, or rsyncdirs.
   - Refuses non-xdist `pytest_xdist_make_scheduler`, `pytest_xdist_getremotemodule`, and `pytest_handlecrashitem`.
   - Registers `pytest_xdist_node_collection_finished` and `pytest_testnodedown` for the basic profile.
   - Records node collections (all must be identical) and every reported nodeid.
   - Sets the collected count from the node collections: an empty collection gives derived status 5.
   - Counts failures from the forwarded reports (crash reports included).
5. **Worker half (loaded by `-p pytest_bridge`; active only with `config.workerinput`):**
   - Verifies its own file identity (§2.1.8).
   - Sets `PTEST_WORKER_ID` and `PTEST_RESOURCE_PREFIX` (§2.1.9). Requires `gwK` with K < workers.
   - Applies the serial `_validate` gates for the execution mode, skipping only the controller-only
     tx/numprocesses/maxprocesses checks and exempting xdist-module plugins.
   - Tracks the protocol-seen count, failures, and full-mode accepted conftest hooks and notes.
   - Writes `config.workeroutput["ptest_bridge"]` in a tryfirst `pytest_sessionfinish`.
   - Detects post-modifyitems drops by item identity, not nodeid text. loadgroup rewrites nodeids to
     `id@group` in a worker modifyitems hook, so string comparisons across that hook are wrong.
   - Factor the hookimpl marking into one helper that `run()` and the worker bootstrap share.
6. **Parallel reconciliation in `run()`:**
   - Refuse (bridge-refused, report `terminal_complete=false`, exit 4 if native was 0 or 5) when any of these holds:
     - a started worker lacks a valid, unrefused record;
     - collections differ;
     - in full mode, a collected id was never reported while native is 0;
     - native 0 or 5 hides observed failures.
   - `project_narrowing` is the controller's ini narrowing plus the union of the worker records' conftest hooks and
     notes. The report format is unchanged.
7. **Real-xdist subprocess twins** in `tests/ng/test_pytest_parallel_subprocess.py`:
   - They run the bridge file directly as a subprocess, with `sys.executable`, a scrubbed env (like
     `_clear_native_pytest_environment`), `PTEST_BRIDGE_PROTOCOL`, the §3.3 env, and a private 0700 report dir with
     a valid report binding. They parse the JSON report and the stderr marker. Each twin has a timeout ≤ 60 s.
   - Required cases:
     - (a) Parallel pass with 4 workers. The report is complete; 4 distinct `PYTEST_XDIST_WORKER` and
       `PTEST_WORKER_ID` values (w000–w003) and 4 distinct resource prefixes are observed by tests (written to
       files under tmp).
     - (b) Parallel fail: native 1, report complete, `problem="native-failure"`.
     - (c) Worker crash (`os.kill(os.getpid(), SIGKILL)` in one test): never exit 0. The report is incomplete, or
       native is nonzero.
     - (d) `loadgroup` with `xdist_group`: tests of one group ran on one worker.
     - (e) Ctrl-C: SIGINT the bridge's session (`start_new_session=True`) after every worker has written a start
       marker. The process exits within 15 s, and no process of that session survives (`os.killpg(pgid, 0)` raises
       `ProcessLookupError`, checked after a bounded poll).
     - (f) A worker-only conftest drops items: a deep `tests/sub/conftest.py` modifyitems deselect runs labelled,
       with the conftest path in `project_narrowing.conftest_hooks`. A `pytest_collection_finish` drop is refused.
     - (g) Verdict forgery: a controller-visible conftest `pytest_sessionfinish` wrapper that forces exit 0 is
       refused. A worker-only `pytest_runtest_makereport` that rewrites failures to passes is refused (full mode).
     - (h) The persea-shaped fixture (`tests/ng/fixtures/pytest/parallel/`: addopts
       `-n 4 --dist=loadgroup -m "not slow"`, a conftest with `pytest_configure_node`, `xdist_group` tests, one slow
       test) runs with 4 workers. `project_narrowing.narrowing == "-m not slow"` and the conftest hooks are listed.
     - (i) An unqualified or missing xdist version is refused before collection. Simulate it with a stub
       `pytest_xdist-0.0.0.dist-info` on a tmp `sys.path` prefix, or skip if not reproducible (document why).
     - (j) `--dist each` is refused.
8. **Scoped check:**
   `ptest tests/ng/test_pytest_parallel_subprocess.py tests/ng/test_pytest_full_subprocess.py tests/ng/test_pytest_scoped_subprocess.py tests/ng/test_pytest_xdist_serial_subprocess.py`
   passes.

### T2: admission, qualification, init (owns `src/ptest/adapters/pytest.py`, `src/ptest/operations.py`, `src/ptest/scheduler.py`, `src/ptest/guard.py`, `src/ptest/executability.py`, `src/ptest/config.py`, `src/ptest/contracts.py`, `docs/schemas/v1/` except `agent-assessment.json`, `src/ptest/resources/repository-agent-guide.md`, `src/ptest/resources/agent-guide.md`, `pyproject.toml`, `uv.lock`, and tests `test_pytest_adapter.py`, `test_operations.py`, `test_scheduler.py`, `test_guard.py`, `test_executability.py`, `test_config.py`, `test_init.py`, `test_contracts.py`, `test_resources.py`, `test_agent_rules.py`, `test_pytest_xdist_serial_subprocess.py`, `test_task_11d_pytest.py`, `test_task_11f_pytest.py`)

1. **Interfaces:** §3.1 (fields, `facts()`, `FACT_KEYS`, texts, caveats, `verdict()`) and §3.2 (`ParallelRequest`,
   `parallel_request`, `xdist_environment_version`, the mirror constants) are implemented exactly.
   - Tests cover every row of the parallel table and the reasons table, and the precedence order.
   - The fixtures are tmp projects with stub `pytest_xdist-<v>.dist-info` directories in a tmp venv with
     `pyvenv.cfg`.
2. **Executability rules:**
   - `check_config` no longer marks an xdist project not runnable for a missing `-n 0`.
   - R1 (remote/rsync) is not runnable with the §3.1 fix.
   - A vitest project gets `parallel="inside vitest (its own workers)"`.
   - The persea-shaped fixture (addopts `-n 4 --dist=loadgroup -m "not extended_migration"`, conftest hooks, uv
     launcher, qualified stub dist-info) gives `parallel_short == "4 workers"` and the exact `full_suite` example.
3. **Init:** `config._fresh_config` writes `("-n","0")` iff `parallel_request(...).config_level`.
   - Tests: `--dist each` gives `["-n","0"]`; the persea shape gives `[]`; the fake-xdist fixture with a bare
     `python` launcher gives `[]`.
   - `test_pytest_xdist_serial_subprocess.py` is updated: the init assertion becomes `[]`. The scoped and full
     twins still pass serially through the generated `-n 0`, with the same label.
4. **Operations and adapter:**
   - `operations.execute` requests N slots for a qualified native pytest (non-advanced). `auto` requests
     `scheduler.effective_limits(domain).max_slots`, capped by `request.workers`. Otherwise it requests 1.
   - `adapters/pytest.prepare` accepts `grant.slots >= 1` under §3.3 (refusing `slots > 1` without a qualified
     request) and generates `-n` exactly as §3.3. `prepare_advanced` generates `-n 0` under the same rule.
   - `inspect_capability` limitation wording mentions the parallel tier plainly.
   - The §3.5 reason code is added to contracts and schemas (additive enum only), and emitted exactly as §3.5.
   - Tests use a fake 2- and 4-slot fixture domain and assert argv, env, `granted_workers`, the command summary
     `workers` (requested), and the reason messages.
5. **Guard:** a test in `test_guard.py` shows that a runner spawning 4 popen children in its group has no survivors
   after an execution timeout and after a SIGINT cancel. Use a fake runner script, not xdist. `guard.py` changes
   only if that test finds a gap.
6. **Scheduler:** no behavior change expected. A test pins that a 4-slot request on a 2-slot machine is granted 2,
   and that a 4-slot request on a busy 4-slot machine queues.
7. **Resources:** the resource guides and README-facing agent guides (`resources/*.md`) describe the parallel tier
   and its fallback in plain words. Remove the "-n 0 serial" guidance.
8. **Must not edit:** T2 must not edit `test_pytest_full_subprocess.py` or `test_pytest_scoped_subprocess.py`
   (T1's). Both must still pass with T2's code:
   `ptest tests/ng/test_pytest_full_subprocess.py tests/ng/test_pytest_scoped_subprocess.py`.
9. **Scoped check:**
   `ptest tests/ng/test_pytest_adapter.py tests/ng/test_operations.py tests/ng/test_scheduler.py tests/ng/test_guard.py tests/ng/test_executability.py tests/ng/test_config.py tests/ng/test_init.py tests/ng/test_contracts.py tests/ng/test_resources.py tests/ng/test_agent_rules.py tests/ng/test_pytest_xdist_serial_subprocess.py tests/ng/test_task_11d_pytest.py tests/ng/test_task_11f_pytest.py`
   passes.

### T3: output redesign (owns `src/ptest/init_render.py`, `src/ptest/init_smoke.py`, `src/ptest/render.py`, `src/ptest/recommendations.py`, `src/ptest/project_facts.py` (new), and tests `test_init_render.py`, `test_init_smoke.py`, `test_render.py`, `test_recommendations.py`, `test_project_facts.py` (new))

1. **Interfaces:** `project_facts.py` implements §3.8 exactly. All three renderers use it: one wrapping
   implementation, one width rule [60, 110].
2. **Fixed-width code deleted:** remove `_WIDTH=64`, the box characters, `_HINTS`, `_split_caveats`,
   `_split_verdict`, `_bullet_lines`, the generic `run:` next steps, and the per-agent restart paragraphs.
3. **Init rendering** follows §3.6 exactly.
   - A golden test at width 80 reproduces the spec's target shape for a persea-shaped input. That input is two
     facts dicts, config records `unchanged` × 3, guidance `created` docs plus 4 skills, `updated` AGENTS.md and
     CLAUDE.md, and smoke api ✓ 2.2 s and web ✓ 1.8 s. Only the wordmark and version lines precede it.
   - Tests:
     - no restart line on a re-run with all guidance `unchanged`, and exactly one on creation;
     - no next-steps block when nothing is actionable;
     - each of the four next-step kinds;
     - width 60 and 110 never split an atom (label, path, command, fact segment);
     - `NO_COLOR` and non-TTY output contains no ANSI bytes;
     - `render_init(result)` without facts still renders the header phrase.
4. **Doctor rendering** follows §3.8 exactly.
   - Golden test at width 90 with the spec's O.4 example: 5 ok, 1 gap, 5 unknown, partial evidence.
   - Every unknown row shows a reason: model, deterministic (prefix stripped), or `review failed:`.
   - The round-19 dropped-citation suffix is kept.
   - Fallback to `execution` when `facts` is missing.
   - Byte bound kept.
5. **Report:** `recommendations.md` uses `long_lines(facts)` and prints unknown reasons (§3.8). It validates
   `facts` defensively; invalid facts fall back to `execution`. All other existing content is kept.
6. **Smoke:** `init_smoke.format_smoke` gives the compact block. `SmokeResult.setup_argv` is set by `skip_result`
   for setup-pending plans.
7. **Scoped check:**
   `ptest tests/ng/test_init_render.py tests/ng/test_init_smoke.py tests/ng/test_render.py tests/ng/test_recommendations.py tests/ng/test_project_facts.py`
   passes.
   - T3 never imports `Executability`, `deterministic_items`, or `agent_assessment.PTEST_ANSWER_PREFIX`. It uses
     dict literals and its own `PTEST_ANSWER_PREFIX` literal.

### T4: fewer unknowns (owns `src/ptest/agent_assessment.py`, `src/ptest/checklist.py`, `src/ptest/deterministic_items.py` (new), `docs/schemas/v1/agent-assessment.json`, and tests `test_agent_assessment.py`, `test_agent_assessment_contract.py`, `test_checklist.py`, `test_deterministic_items.py` (new), `tests/ng/fixtures/agent_assessment/`)

1. **Interfaces:** §3.9 is implemented exactly, including the `ItemReview.answer` invariant, `assemble_child` rows
   and findings, the degrade rule, and the prompt/validation guard for `PTEST_ANSWER_PREFIX`.
   - A contract test runs a child that holds a deterministic gap plus a deterministic satisfied row through
     `C.encode_public_document("agent-assessment", ...)`, and it validates.
   - The SELECT finding prose passes `aa_prose_is_untrusted() is False`.
2. **`deterministic_items.answers_for`:**
   - Covers every row of the §3.9 table with tmp domains: an empty history, a history with a basic full run, a
     baseline with `call_s`, and an unreadable history.
   - Also covers configs with selection disabled, not closed, and closed; vitest; and command.
   - Never raises, and never initializes the scheduler.
3. **Routing (U.2):**
   - RESOURCE-001 text signals: `tmp_path`, `tempfile`, `mkdtemp`, `mkstemp`, `socket`, `bind(`, port constants
     (`PORT\s*=\s*\d`, `port=\d`), `/tmp`, lock files (`\.lock\b`, `filelock`, `flock`).
   - NETWORK-001: `httpx`, `requests`, `aiohttp`, `respx`, `responses`, `pytest[-_]socket`, `socket.socket`
     patches, `disable_socket`, network-deny fixtures, `vcr`.
   - PROCESS-001: `subprocess`, `asyncio.create_subprocess`, `multiprocessing`, `Popen`, `os.fork`, `.join(`,
     `.terminate(`, `.kill(`, `.wait(`.
   - Conftest excerpts that define a fixture whose name appears as a parameter in the item's routed test excerpts
     rank first.
   - Packet admission may add signal-bearing files within the existing byte and file caps.
   - Tests show each signal routes, and that a referenced conftest fixture outranks a scanner hit.
4. **Prompts (U.3):**
   - `_ITEM_INSTRUCTION` and each catalog `prompt` say: gap only with a cited concrete violation; satisfied only
     when the evidence shows the guaranteeing mechanism (for example a session-wide network block or per-worker
     port allocation); otherwise unknown with one sentence naming the missing evidence; absence of code is unknown,
     never a guess.
   - Tests pin the key sentences.
5. **Schema:** `agent-assessment.json` changes only if needed, and only additively. The expectation is no change.
6. **Scoped check:**
   `ptest tests/ng/test_agent_assessment.py tests/ng/test_agent_assessment_contract.py tests/ng/test_checklist.py tests/ng/test_deterministic_items.py`
   passes.

### T5: wave 2, MANDATORY: CLI wiring and integration (barrier: starts after T1–T4 are merged into the chain)

Owns `src/ptest/cli.py`, `src/ptest/help.py`, `README.md`, and the tests `test_cli.py`, `test_help.py`,
`test_doctor_init_integration.py`, and `test_parallel_output_cli.py` (new). Post-merge only, T5 may also edit these
wave-1 tests to update output-string assertions, and nothing else in them: `test_init.py`,
`test_agent_doctor_acceptance.py`, `test_doctor_smoke.py`, `test_acceptance.py`, and `test_install.py`.

1. **Init wiring:** exactly §3.6 flow. Delete the old smoke-text concatenation (`_init_smoke_text` becomes
   `_init_smoke_results`).
2. **Doctor wiring:**
   - `_child_assessment_data(..., facts=...)` fills `child["facts"]` (§3.7) from the `check_resolution` facts.
   - Deterministic answers are wired (§3.9).
   - `render_agent_assessment(...)` is called with the default width.
   - The disclosure follows §3.10.
   - `cli._summary` may report the requested worker count for qualified pytest projects (optional; value change
     only).
3. **Help and README:**
   - `help.py`: `ptest doctor --help` carries the full legal disclosure text that used to be in
     `_render_review_disclosure`, word for word, plus a one-paragraph description of the parallel tier.
   - `README.md`:
     - the parallel tier: how N is chosen, slots, the fallback reasons, and that `-n 0` in `.ptest.toml` opts out;
     - coverage under xdist is out of scope;
     - the new init and doctor output, and the short disclosure plus a pointer to the full text.
4. **Integration tests** in `tests/ng/test_parallel_output_cli.py`, through `cli.main` or `case.invoke`, with no
   monkeypatching of ptest modules. Fake provider executables only (the `test_doctor_init_integration` pattern).
   - (a) **Parallel end to end:**
     - tmp git project, launcher `[sys.executable]`, addopts `-n 4 --dist=loadgroup -m "not slow"`, `xdist_group`
       tests, a conftest modifyitems hook;
     - `cli.main(("init","--no-doctor","--agents","none"))` writes `args = []`; stdout contains
       `parallel: 4 workers` and none of `┌`, `ready with caveats`, `expected:`, or `fingerprint`;
     - `case.invoke(domain(slots=4), root, "--full")` exits 0 with `granted_workers == 4`, a `project-filtered`
       reason with `-m not slow`, and 4 distinct worker ids recorded by the tests;
     - with `domain(slots=2)`: `granted_workers == 2` and stderr contains
       `parallel-workers: 2 xdist workers (4 requested, 2 granted)`.
   - (b) **Fallback:** `--dist each` → init writes `["-n","0"]`, init stdout shows
     `parallel: no — --dist each is not supported; ptest runs serially`, and `--full` passes serially.
   - (c) **Ctrl-C through the guard:**
     - `ptest --full` subprocess on a suite whose tests write a start marker then sleep 30 s;
     - after all 4 markers appear, SIGINT the ptest process;
     - ptest exits within 20 s with a non-success code, and psutil finds no surviving process whose environ carries
       that run's `PTEST_RUN_ID`, or whose cmdline contains the tmp root.
   - (d) **Doctor on a persea-shaped monorepo** (uv-style api with the stub-qualified dist-info and `-n 0` in
     `api/.ptest.toml`; vitest web):
     - human output has `api  pytest · ` with counts;
     - `runs: yes · parallel: no · setup:` lines; `web … parallel: inside vitest`;
     - `? Test timing  no timing history yet: run ptest --full once`;
     - a SELECT gap line `✗ Test selection`;
     - every `?` row has a reason;
     - the provider call count equals the planned requests (TIMING and SELECT take no call);
     - the disclosure is ≤ 3 lines before the prompt;
     - `recommendations.md` contains the plain facts and `Reason:` lines;
     - `--assessment-json` has no `facts` key and validates.
   - (e) **Init on the same monorepo:** grouped `config     unchanged` line, exactly one `Restart your coding
     agents` line on first init with agents, none on re-run, and `api  parallel off → remove "-n", "0" from [runner]
     args in api/.ptest.toml to run 4 workers`.
   - (f) **Mirror equality:**
     - `pytest_bridge.QUALIFIED_XDIST_VERSIONS == executability.XDIST_QUALIFIED_VERSIONS`;
     - `pytest_bridge.PARALLEL_DIST_MODES == executability.XDIST_DIST_MODES`;
     - `render.PTEST_ANSWER_PREFIX == agent_assessment.PTEST_ANSWER_PREFIX` (and the recommendations literal, if
       separate).
5. **Suite health:**
   - Fix every test broken only by the merged output changes, in the files listed above.
   - Run the scoped command of all five tasks and the context-pack coverage command:
     `ptest tests/ng/test_pytest_parallel_subprocess.py tests/ng/test_pytest_adapter.py tests/ng/test_executability.py tests/ng/test_init_render.py tests/ng/test_render.py tests/ng/test_agent_assessment.py`.
   - Also run `ptest tests/ng/test_cli.py tests/ng/test_help.py tests/ng/test_doctor_init_integration.py tests/ng/test_parallel_output_cli.py tests/ng/test_init.py tests/ng/test_agent_doctor_acceptance.py tests/ng/test_doctor_smoke.py tests/ng/test_acceptance.py`.
   - The controller runs `ptest --full` once afterwards.

## 5. Test approach

- **TDD per task:** tests come first, through `ptest <files>` only (never `pytest` or `uv run pytest`). A missing
  test path silently gives "no tests ran", so create the file first. The trailing "changed-during-run … path
  classes: ignored" line is benign.
- **Real xdist:** real xdist 3.8.0 comes from the test extra. T1 twins run the bridge directly. The e2e tests
  through ptest's admission and guard are in T5, because only T5 sees T1 and T2 together.
- **Other fakes:** providers are fake executables or vendored fixtures. Never launch claude, codex, or opencode.
  Stub xdist versions use tmp `pytest_xdist-<v>.dist-info` directories. Never modify persea.
- **Timeouts:** subprocess timeouts ≤ 60 s. Sleeping tests are interrupted by the test, never waited out.
- **Coverage:** no coverage gate (none configured). The scoped coverage command from the context pack is run by T5.

## 6. File ownership and new files

| Task | Files (exclusive in its wave) |
|---|---|
| T1 | `src/ptest/runtime/pytest_bridge.py`, `src/ptest/runtime/protocol-v1.json`, `tests/ng/test_pytest_parallel_subprocess.py`*, `tests/ng/fixtures/pytest/parallel/`*, `tests/ng/test_pytest_full_subprocess.py`, `tests/ng/test_pytest_scoped_subprocess.py` |
| T2 | `src/ptest/adapters/pytest.py`, `src/ptest/operations.py`, `src/ptest/scheduler.py`, `src/ptest/guard.py`, `src/ptest/executability.py`, `src/ptest/config.py`, `src/ptest/contracts.py`, `docs/schemas/v1/*.json` except `agent-assessment.json`, `src/ptest/resources/repository-agent-guide.md`, `src/ptest/resources/agent-guide.md`, `pyproject.toml`, `uv.lock`, tests listed in §4 T2 |
| T3 | `src/ptest/init_render.py`, `src/ptest/init_smoke.py`, `src/ptest/render.py`, `src/ptest/recommendations.py`, `src/ptest/project_facts.py`*, tests listed in §4 T3 (`test_project_facts.py`*) |
| T4 | `src/ptest/agent_assessment.py`, `src/ptest/checklist.py`, `src/ptest/deterministic_items.py`*, `docs/schemas/v1/agent-assessment.json`, tests listed in §4 T4 (`test_deterministic_items.py`*), `tests/ng/fixtures/agent_assessment/` |
| T5 (wave 2) | `src/ptest/cli.py`, `src/ptest/help.py`, `README.md`, `tests/ng/test_cli.py`, `tests/ng/test_help.py`, `tests/ng/test_doctor_init_integration.py`, `tests/ng/test_parallel_output_cli.py`*, plus the post-merge assertion-only edits listed in §4 T5 |
| transcription | the spec amendment appends (shared-file content) |

\* marks new files. The exhaustive new-file list:
- `src/ptest/project_facts.py` (T3)
- `tests/ng/test_project_facts.py` (T3)
- `src/ptest/deterministic_items.py` (T4)
- `tests/ng/test_deterministic_items.py` (T4)
- `tests/ng/test_pytest_parallel_subprocess.py` (T1)
- `tests/ng/fixtures/pytest/parallel/pyproject.toml.txt` (T1)
- `tests/ng/fixtures/pytest/parallel/conftest.py.txt` (T1)
- `tests/ng/fixtures/pytest/parallel/test_groups.py.txt` (T1)
- `tests/ng/test_parallel_output_cli.py` (T5)

Files no task touches: `reports.py`, `history.py`, `agent_rules.py`, `doctor.py`, `runners.py`, `selection.py`,
`agent_providers.py`, `monorepo.py`, `files.py`, `platform.py`, `storage.py`, and `source.py`.

## 7. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Worker plugin not loaded (sys.path or `-p` mechanics differ) | Parallel runs refuse | The controller requires a record from every worker, so this fails closed. T1 twin (a) proves the loading on real xdist 3.8.0. |
| A project module named `pytest_bridge` shadows the worker plugin | Forged worker records | File-identity check against the executor's protocol-descriptor sibling (§2.1.8). |
| loadgroup `@group` nodeid rewrite breaks reconciliation | False refusals | Reconcile on the ids the controller receives; detect worker drops by item identity (§4 T1.5). |
| Worker crash triggers xdist restarts and slow storms | Slow or unclear runs | `--max-worker-restart=0`. A lost worker record makes the run incomplete. |
| Parallel scoped runs of one file cost worker startup | Slower tiny runs | Accepted: it matches the project's own `pytest` behavior. Declining it needs a `-n 0` opt-out in `.ptest.toml`. |
| Existing configs (persea) keep `-n 0` | No speed-up until edited | Plain `parallel off →` next step in init and doctor. Init stays non-destructive (M5). |
| T5 starts before the merge | BLOCKED integration | Stated barrier. T5's first step verifies `Executability.facts`, `render_init_footer`, and `deterministic_items` exist, else returns BLOCKED. |
| Output-string tests in unowned files break after the merge | Red suite | T5 owns the post-merge assertion updates in the listed files. |

## 8. Open questions (non-blocking; defaults chosen)

- Should init offer to remove its own earlier `-n 0` from existing configs? Default: no, only the next step (M5).
- Should `worksteal` be qualified? Default: yes (the controller sees exactly one report stream). The controller can
  drop it from both constant sets.
- Should the wordmark stay? Default: yes (M8). Deleting it is a one-function change in `init_render`.

## 9. Amendments

The shared-file content appends these amendments to
`docs/superpowers/specs/2026-09-24-parallel-and-output-requirements.md` (M1–M12), plus pointer sections to the three
earlier specs. They are summarized in §2 and repeated verbatim in the shared-file content.

### A-user-1 (2026-09-24, user decision via controller): checklist item PARALLEL-001 "Parallel execution"
Source: requirements §U.5 (added after this design was written). Binding for T3, T4 and T5.
- **T4** adds canonical checklist entry `PARALLEL-001`, label "Parallel execution", to `src/ptest/checklist.py`. It is
  additive to the catalog, to `docs/schemas/v1/agent-assessment.json` and to the drift guards. It is answered
  **deterministically** in `deterministic_items.py` (no model call) from the project facts in §3.1/§3.2:
  - satisfied: `parallel: N workers (xdist, --dist <mode>)`, or `inside vitest (its own workers)`;
  - gap: parallel is configured but runs serially under ptest (P.4 reason plus fix);
  - gap: not configured. Suggest `pytest-xdist` plus `-n auto` (or the vitest pool settings) only when the
    parallel-safety items (FIX-002, DB-001, DB-002, CACHE-001, RESOURCE-001, NETWORK-001, PROCESS-001, TIME-001) have
    no gap; otherwise the fix is "resolve the parallel-safety gaps first";
  - unknown: only with a stated reason.
  The item is ordered last in the catalog, so existing IDs and orders are unchanged.
- **T3** renders the eight isolation items as a visible **"parallel safety"** group, followed by PARALLEL-001, in the
  doctor terminal output and in `recommendations.md`.
- **T5** wires the facts T4 needs, and its integration test asserts PARALLEL-001 through `cli.main` for a
  persea-shaped fixture (4 xdist workers means satisfied) and for a serial-fallback fixture (gap with the reason).
