# Runner capability research — 2026-09-17

Status: design input, not an adapter implementation or support certification.
The approved product spec remains authoritative. This stage used official documentation,
installed source/package metadata, and tool version commands only: **no tests, runner
collection, project-config evaluation, installs, provider calls, or global edits**.
The actually runtime-tested matrix is empty. Proposed mechanisms below require fixtures.

## Smallest useful acceptance matrix

Freeze exact tuples initially; publish broader ranges only after compatibility evidence.
These are candidate acceptance targets, not claims that combinations already passed.

| Component | Observed local version / candidate | Initial capability target |
|---|---|---|
| Python + pytest | Python 3.12.12 and 3.13.11; pytest 9.1.1 | Native-config bridge; serial with no xdist installed; exact node-ID reports |
| Optional pytest parallelism | pytest-xdist 3.8.0 | Local `popen` workers only; explicit finite grant; no remote/proxy transport |
| Python coverage | pytest-cov 7.1.0; coverage 7.15.0 | Preserve configured thresholds and full-run exit; separately test xdist combination |
| Node + inspected Vitest reference | Node 26.8.2; Vitest / coverage-v8 3.2.6 | Reference only, not a release support claim |
| Vitest release acceptance candidate | Vitest / coverage-v8 3.2.7 | One project, standard `forks`, runtime tests, no browser/typecheck/watch/API; not locally installed |
| Python launcher | uv 0.9.26 | Recognized `uv run` prefix; explicit setup policy, then owned Python bridge |
| JavaScript launcher | npm 12.0.2 | Direct Node bridge preferred; arbitrary npm scripts remain generic/exclusive |
| Go compatibility | Go 1.27.1-X:nodwarf5 | Conservative standard test command, bounded native controls; no selection claim |
| Rust compatibility | cargo / rustc 1.98.1 | Standard libtest targets only for a bounded profile; no selection claim |

All local evidence is Linux x86_64. macOS subprocess, signals, paths, and these tuples
still need acceptance coverage; no invented macOS patch versions. The local Node
version is an observation, not a recommendation to require that major everywhere.
Vitest's current site documents a newer major; use the [v3 API](https://v3.vitest.dev/advanced/api/)
for this profile. [v3.2.7 release notes](https://github.com/vitest-dev/vitest/releases/tag/v3.2.7)
list a browser filesystem-access fix; existing repositories must not be upgraded silently.
[v4 migration](https://v4.vitest.dev/guide/migration) changes pool options, worker environment
variables, and reporter APIs. Do not advertise a single v3–v5 adapter from matching flag names.

## Literal arguments and admission

Store and spawn executable plus argv arrays, never a shell command assembled from test
paths. Preserve argument boundaries, whitespace, metacharacters, and runner `--` semantics;
redacted display text is not executable input. [Python subprocess documentation](https://docs.python.org/3/library/subprocess.html)
supports sequence arguments with `shell=False`. A literal outer invocation does not make
an inner npm script shell-free.

Obtain scheduler admission **before** importing runner/plugin/project config, collection,
or package setup. Static `plan`/`doctor` cannot use these bridges. One-shot bridges must
retain the native terminal reporters/stdout/stderr and final runner status; write owned
structured evidence separately. Coverage failure, collection failure, cancellation, and
reporter failure cannot become success merely because individual tests passed.

## Pytest: native config, optional xdist, guarded parsed options

Do not implement another pytest configuration parser. Pytest 9 recognizes `pytest.toml`,
`.pytest.toml`, INI files, and native `[tool.pytest]` as well as the older
`[tool.pytest.ini_options]`. Configuration files are not merged; `-c` chooses explicitly.
Native and nonempty legacy pyproject sections conflict. Empty pytest-specific config
files can still determine the root/config choice. [Configuration rules](https://docs.pytest.org/en/stable/reference/customize.html)
and [9.1.1 source](https://github.com/pytest-dev/pytest/blob/9.1.1/src/_pytest/config/findpaths.py).

Native argument composition is config `addopts`, then `PYTEST_ADDOPTS`, then explicit
argv; usual duplicate options resolve later values. These config strings are intentionally
parsed by pytest, not by ptest. Plugins can alter effective settings afterward, so appending
`-n N` is not a complete guarantee. [Documented order](https://docs.pytest.org/en/stable/example/simple.html)
and [9.1.1 parsing source](https://github.com/pytest-dev/pytest/blob/9.1.1/src/_pytest/config/__init__.py).

Recommended prototype, to prove before adoption:

1. An owned child bridge calls public `pytest.main(argv_list, plugins=[owned_plugin])`.
   Native parsing preserves config precedence and literal user argv. Serial operation
   without xdist injects **no `-n` option**. If config requests missing-plugin options,
   report that incompatibility; do not silently erase them. [Calling pytest](https://docs.pytest.org/en/stable/how-to/usage.html).
2. With the pinned xdist plugin, a `pytest_cmdline_main` wrapper guards parsed options
   before xdist's `tryfirst` implementation. Normalize `numprocesses`/`maxprocesses`
   against the grant; serial sets zero. Reject remote `tx`, proxy `px`, rsync and custom
   distribution before yielding. xdist normally expands a finite count to local
   `popen` specifications; zero disables distribution. [xdist source](https://github.com/pytest-dev/pytest-xdist/blob/v3.8.0/src/xdist/plugin.py).
3. Revalidate after configuration, before worker creation, using a session-start wrapper;
   a setupnodes wrapper can validate the final local spec count after other setupnodes
   hooks but before ordinary gateways start. **That hook alone is too late for `--px`:**
   NodeManager creates proxy gateways in its constructor. [Worker management source](https://github.com/pytest-dev/pytest-xdist/blob/v3.8.0/src/xdist/workermanage.py).
4. Inspect critical hook owners against the pinned supported profile. Unknown control
   overrides lose first-class capability; never disable user plugins silently. Wrappers
   run around ordinary hooks, but another wrapper/configure hook can mutate settings.
   [Hook ordering](https://docs.pytest.org/en/stable/how-to/writing_hook_functions.html).

A critical-hook allowlist can retain pytest-cov and ordinary fixtures while rejecting
untested orchestration hooks; it is **not a sandbox or proof about arbitrary plugins**.
Include cmdline/configure/session-start and xdist worker-setup hooks in that review.
Autoloaded plugin identities and relevant environment belong in the capability fingerprint.
An object supplied through `plugins=[...]` is not assumed to propagate to xdist workers;
worker instrumentation/bootstrap needs an explicit, tested importable mechanism.

`pytest_load_initial_conftests` sees composed arguments after native preliminary parsing
and plugin loading. Stripping tokens there recreates option-arity problems and cannot
reliably rescue unknown options from an absent plugin. Prefer parsed settings plus a
conservative profile. [Parsing implementation](https://github.com/pytest-dev/pytest/blob/9.1.1/src/_pytest/config/__init__.py).

## Vitest 3: flags alone are insufficient

The inspected 3.2.6 source uses pool-specific `maxForks`/`maxThreads` ahead of global
`maxWorkers`; corresponding minimums also matter. `VITEST_MAX_THREADS`,
`VITEST_MIN_THREADS`, `VITEST_MAX_FORKS`, and `VITEST_MIN_FORKS` are applied later to
pool settings. Normalize the child environment and every supported pool bound, then
validate effective config. `--maxWorkers=N` by itself is not the cap.
[Config resolver](https://github.com/vitest-dev/vitest/blob/v3.2.6/packages/vitest/src/node/config/resolveConfig.ts),
[fork pool](https://raw.githubusercontent.com/vitest-dev/vitest/v3.2.6/packages/vitest/src/node/pools/forks.ts).

For serial, `fileParallelism=false` constrains standard file workers to one, but within-file
`test.concurrent` remains a separate control. Freeze `maxConcurrency=1` for the serial
profile and test it. Preserve isolation: `singleFork`/`singleThread` is not a harmless
synonym for serial execution. Reject custom/VM/browser pools, projects/workspaces,
`poolMatchGlobs`, typechecking, benchmark/watch/standalone modes in the first profile.
Do not secretly change a repository's execution mode to fit that profile.
[v3 configuration](https://v3.vitest.dev/config/), [pool migration](https://v4.vitest.dev/guide/migration).

Use public `parseCLI(argv_array)`, never its space-splitting string input. `createVitest`
constructs the actual instance without running tests; validate its root/project config
on that same instance. A separate `resolveConfig()` preflight is insufficient: later
creation resolves config again and the standalone resolver does not resolve projects.
[Public creation/parsing APIs](https://v3.vitest.dev/advanced/api/).

Public one-shot candidate flow (ordering/parity still needs tests):

```text
admit -> parseCLI(array) -> createVitest('test', guardedOptions, viteOverrides)
      -> validate the actual instance -> globTestSpecifications()
      -> exact-match selected specifications -> init()
      -> runTestSpecifications(specifications, allTestsRun) -> finally close()
```

`init()` initializes reporters/coverage; do not subsequently call `start()`. Set
`allTestsRun=true` only for the actual complete full inventory, preserving coverage
semantics. `globTestSpecifications` provides files; `collectTests` executes file bodies
even though it skips test callbacks. [Instance APIs](https://v3.vitest.dev/advanced/api/vitest).
Creation itself evaluates config/Vite hooks and can start a configured API server in
3.2.6; enforce no-watch/no-API/no-browser controls before creation, not just afterward.
[Creation source](https://github.com/vitest-dev/vitest/blob/v3.2.6/packages/vitest/src/node/create.ts).

Inject an owned Vite plugin through `createVitest`'s `viteOverrides.plugins`. Its public
experimental `configureVitest({vitest})` hook can append an inline reporter to resolved
`vitest.config.reporters`, retaining the existing/default entries before instantiation.
Do not mutate private `ctx.reporters`. Official docs specifically permit this config
mutation. Other untested `configureVitest` owners are unsupported: native hooks are
awaited together, not a safe last-writer ordering. Revalidate after creation. Do not rely
on Vite's parallel `configResolved` hook as a final guard. [Vitest plugin API](https://v3.vitest.dev/advanced/api/plugin),
[Vite hooks](https://vite.dev/guide/api-plugin.html#configresolved).

## Reports, selection, and the full gate

Pytest's owned additive plugin should record exact node IDs, collection failures,
setup/call/teardown outcomes/durations, and session completion. In xdist, use controller
reports plus `pytest_xdist_node_collection_finished` inventories; verify their agreement.
JUnit alone transforms node IDs and is not a lossless parametrized identity format.
[Pytest hooks](https://docs.pytest.org/en/stable/reference/reference.html),
[xdist hooks](https://raw.githubusercontent.com/pytest-dev/pytest-xdist/v3.8.0/src/xdist/newhooks.py),
[JUnit implementation](https://github.com/pytest-dev/pytest/blob/9.1.1/src/_pytest/junitxml.py).

Vitest JSON preserves useful names/results but its inspected assertions lack native task
IDs; duplicate titles cannot be disambiguated by `fullName` alone. An additive reporter
should preserve project, exact file, native task ID, title hierarchy, result, and duration.
Native IDs are not promised stable across edits: unresolved failures retain a broad file
obligation until compatible inventory/full reconciliation. [JSON reporter](https://v3.vitest.dev/guide/reporters),
[reported test cases](https://v3.vitest.dev/advanced/api/test-case).

Use explicit reviewed source-to-test groups plus native file inventory as the initial
selection proof. No custom dependency resolver is needed. Unmapped/dynamic uncertainty,
global config/fixture/dependency changes, incompatible evidence, or unreconciled failures
widen to full. Vitest positional path filters are substrings, not exact paths; run exact
native specifications or check actual selected inventory. Native `related` requires
statically analyzable imports and misses variable dynamic imports, so it is not a sole
safety basis. [v3 selection CLI](https://v3.vitest.dev/guide/cli).

Keep the configured full gate distinct from a selected run, even when most tests were
selected. Preserve coverage options/thresholds and final exit status. pytest-cov can
override coverage settings; Vitest coverage threshold auto-update can edit configuration.
Do not strip gates or treat generated-input edits as a valid unchanged baseline.
[pytest-cov config](https://pytest-cov.readthedocs.io/en/stable/config.html),
[Vitest coverage options](https://v3.vitest.dev/config/).

## Launchers and bounded compatibility

`uv run` may lock and synchronize before execution. `--locked` checks lock consistency;
`--frozen` skips that check; `--no-sync` skips environment synchronization. Treat setup
as explicit admitted work. Preserve recognized launcher cwd/env/options and reject unknown
wrappers; do not invent equivalence between these flags. [uv synchronization](https://docs.astral.sh/uv/concepts/projects/sync/).

`npm run` uses a shell and may execute pre/post scripts; forwarded arguments apply only
to the main script. Worker flags therefore do not bound arbitrary npm lifecycle or compound
commands. Prefer an explicitly adopted direct Node adapter. A recognized simple npm script
needs its own parity fixtures and no unaccounted hooks/workspaces/custom shell; otherwise
generic/exclusive, without silently bypassing hooks. `npm ci` is separate authorized setup.
[npm run semantics](https://docs.npmjs.com/cli/v10/commands/npm-run-script/),
[npm ci](https://docs.npmjs.com/cli/v11/commands/npm-ci/); npm 12.0.2 installed source confirms shell/pre/post behavior.

For Go, combine finite `-p` (concurrent build/test programs), `-parallel` (parallel tests
within a binary), and controlled `GOMAXPROCS`/`-cpu`; the conservative profile uses `-p=1`.
Normalize conflicting `GOFLAGS`/explicit flags, reject unaccounted `-args`/fuzz controls,
and use `-count=1` when evidence must represent execution rather than cached success.
[Go command](https://pkg.go.dev/cmd/go). GOMAXPROCS is not a bound on arbitrary child
processes/native threads. [Go runtime](https://pkg.go.dev/runtime#GOMAXPROCS).

Cargo `--jobs` bounds build concurrency, not libtest threads; `-- --test-threads=N` controls
the standard harness. Custom `harness=false` executables and doctest process parallelism
need separate proof. Do not silently remove doctests from a full gate to fit the profile.
Unsupported commands may use declared exclusive compatibility, not a false hard-CPU-cap
claim. [Cargo test](https://doc.rust-lang.org/cargo/commands/cargo-test.html),
[Cargo build settings](https://doc.rust-lang.org/cargo/reference/config.html).

## Required verification before capability promotion

Test absent/present xdist; all pytest config forms including pytest9 native TOML; config,
environment, duplicate CLI and plugin overrides; dangerous xdist transport; worker bootstrap;
Vitest min/max/pool/env overrides, concurrent tests, rejected projects/custom pools/API/watch;
exact paths with spaces/metacharacters/leading dashes; duplicate/parametrized identities;
collection/import/setup/teardown/coverage failures; missing/truncated reports; zero inventory;
stdout/stderr and exit parity; launcher hooks/setup side effects; cancellation and descendants.
Run real pinned subprocess fixtures through ptest, on Linux and macOS, after design approval.
Control flags are cooperative runner bounds, never a universal malicious-code CPU sandbox.

Local provenance (read only; not shared into this worktree):

- `/home/ingmar/code/persea_content_maker/persea_content_maker_api/.venv/lib/python3.12/site-packages`: `_pytest/config/__init__.py:1510`, `_pytest/config/findpaths.py`, `xdist/plugin.py:303`, `xdist/workermanage.py:44` and package metadata.
- `/home/ingmar/code/persea_content_maker/persea_content_maker_front/node_modules/vitest/dist/chunks`: `coverage.DfSpMS-b.js:2612` pool bounds, `:3719` env overrides; `cli-api.DWGBtMmz.js:9333` plugin hooks, `:9351` reporter creation; `index.VByaPkjc.js:1687` JSON reporter.
- `/usr/lib/node_modules/npm/lib/commands/run.js:87` and nested `@npmcli/run-script/lib/make-spawn-args.js`: lifecycle and shell behavior.

Portable evidence is the primary-source links, not availability of these machine-local
paths in a fresh clone. v3.2.7 upstream `create.ts`/`plugins/index.ts` were additionally
read for creation/plugin ordering; this is source inspection, not runtime verification.

## NG-5 repair addendum: basic serial is a separate capability

The advanced tuple must not become the sole gate for ordinary cooperative execution.
Add an independently tested `basic_serial` capability: one nonexclusive scheduler slot,
native configured scoped/full execution, untouched coverage gates/output/exit, no automatic
narrowing or reusable selection baseline. Report the advanced-capability limitation;
do not require `command`/whole-host exclusivity solely because an advanced tuple differs.
This updates the earlier single-tuple recommendation, not the evidence that tests remain unrun.

Finite **acceptance candidates, not passing support claims**:

| Basic profile | Exact runner fixtures | Conditions |
|---|---|---|
| pytest serial | 8.4.2, 9.0.3, 9.1.0, 9.1.1 | xdist absent; native config/addopts; no untested orchestration hooks; optional pytest-cov 7.1.0 / coverage 7.15.0 cases |
| Vitest serial | 3.1.4, 3.2.6, 3.2.7 | Single project, standard forks, matching coverage-v8 when configured, all effective worker bounds and maxConcurrency=1; no API/watch/browser/custom control hooks |

Use the already declared CPython 3.11–3.14 project-interpreter target explicitly, separate
from ptest's interpreter; build a syntax-compatible stdlib-only bridge. Test fixtures must
record resolved interpreter/Node/Vite versions, not merely runner package versions.
Pytest 8.4.2 requires Python >=3.9; 9.0.3/9.1.0 require >=3.10, so this proposed floor
does not conflict with their metadata. [8.4.2](https://pypi.org/project/pytest/8.4.2/),
[9.0.3 metadata](https://pypi.org/pypi/pytest/9.0.3/json), [9.1.0 metadata](https://pypi.org/pypi/pytest/9.1.0/json).

`pytest.main(argv, plugins)` and native exit propagation exist in both inspected earlier
sources; serial does not intrinsically require xdist or advanced identity instrumentation.
Preserve actual plugin/config behavior rather than disabling coverage to qualify. An
unknown control plugin or unbounded configuration is distinct from an unpromoted evidence
tuple and may still block the bounded path. Do not infer all plugin safety from pytest's
version. [8.4.2 source](https://raw.githubusercontent.com/pytest-dev/pytest/8.4.2/src/_pytest/config/__init__.py),
[9.0.3 source](https://raw.githubusercontent.com/pytest-dev/pytest/9.0.3/src/_pytest/config/__init__.py).

Vitest 3.1.4 already has the discussed creation, configuration-plugin, and run APIs; its
fork pool and environment precedence have the same relevant controls. Basic serial can
use guarded `createVitest` then native `start(filters)` and `close`, preserving native
coverage lifecycle without depending on advanced exact-spec selection. Never call both
`init` and `start`. Successful basic execution still needs trustworthy run completion;
missing required completion evidence is not an excuse to manufacture green. Unknown
per-test identity/timing stays unknown and cannot clear old failure obligations.
[3.1.4 creation](https://raw.githubusercontent.com/vitest-dev/vitest/v3.1.4/packages/vitest/src/node/create.ts),
[core](https://raw.githubusercontent.com/vitest-dev/vitest/v3.1.4/packages/vitest/src/node/core.ts),
[forks](https://raw.githubusercontent.com/vitest-dev/vitest/v3.1.4/packages/vitest/src/node/pools/forks.ts),
[config](https://raw.githubusercontent.com/vitest-dev/vitest/v3.1.4/packages/vitest/src/node/config/resolveConfig.ts).

Do not silently upgrade an unchanged 3.1.4 adoption repository to 3.2.7 merely to fit the
advanced tuple. Additional patches/families require an explicit compatibility policy and
acceptance evidence; this note does not assert every version between the candidates works.
Required regression: a basic-only tuple runs at grant 1 alongside another checkout,
preserves a failing coverage threshold, and reports selection unavailable rather than
refusing execution or reserving the host exclusively.

## Design-pin metadata check

Public registry/release metadata retrieved 2026-09-17; no packages/binaries downloaded,
installed, imported, or executed. Existence is not a vulnerability audit or runtime proof.

| Pin | Metadata result | Primary evidence |
|---|---|---|
| psutil 7.2.2 | Exists, Requires-Python >=3.6; files not yanked; Linux/macOS x64/arm64 wheels listed | [PyPI JSON](https://pypi.org/pypi/psutil/7.2.2/json) |
| Bandit 1.9.4 | Exists, Requires-Python >=3.10; files not yanked | [PyPI JSON](https://pypi.org/pypi/bandit/1.9.4/json) |
| pip-audit 2.10.1 | Exists, Requires-Python >=3.10; files not yanked | [PyPI JSON](https://pypi.org/pypi/pip-audit/2.10.1/json) |
| Gitleaks 8.30.1 | Release exists; Darwin/Linux x64/arm64 archives and checksum file listed; Python requirement not applicable | [Release](https://github.com/gitleaks/gitleaks/releases/tag/v8.30.1) |

Correction to the sensitivity concern: issue 2170 is closed. Comments explain that its
alphabet-sequence fake token matches an intentional global stopword allowlist and show
a changed token being detected. This is not evidence of a confirmed blanket detector
regression. Keep the actual-binary sensitivity gate, using non-allowlisted synthetic
positive and negative samples, and verify release checksums during authorized setup.
[Reproduction explanation](https://github.com/gitleaks/gitleaks/issues/2170#issuecomment-4877877658),
[follow-up analysis](https://github.com/gitleaks/gitleaks/issues/2170#issuecomment-5063891505).
The rendered issue page omitted comments; the public GitHub issue-comments API supplied
them. This stage did not reproduce either the original claim or its correction locally.
