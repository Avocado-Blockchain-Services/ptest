# Pytest candidate fixtures and scoped execution evidence

These four manifests pin the enumerated pytest candidates. Basic serial
execution tolerates an xdist module being present but refuses an active xdist
executor through the named bridge control; the explicit refusal test uses a
preprovisioned xdist interpreter. The adapter preserves the configured argv
literally, including any project-plugin controls; the ordinary fixture helper
adds `-p no:xdist` explicitly when it needs a serial fixture, while its
`allow_xdist` negative path deliberately omits that option.

The manifests are dependency metadata, not shared environments. The real
Q-PY-SELECT fixture (`9.1.1`) is exercised only through an explicitly supplied
preprovisioned `PTEST_TEST_PYTHON_9_1_1_COV` interpreter. The bridge accepts
that interpreter only when it reports pytest 9.1.1, pytest-cov 7.1.0, and
coverage 7.15.0; absent or off-table tuples skip/refuse before tests. No test
path runs uv synchronization, installs dependencies, or mutates a fixture
environment. Other candidate interpreters use an already provisioned
controller or the explicitly supplied `PTEST_TEST_PYTHON_8_4_2` (and
corresponding version) override.
The actual controller run, versions, failures and skips are recorded in
`.pipeline/out/task-11d-luna-final-repair.json`; earlier repair evidence remains
in `.pipeline/out/task-11d-astra-repair.json`, `.pipeline/out/task-11d-nested-hook-repair.json`
and `.pipeline/out/task-11d-sol-repair.json`.

ptest's declared-setup boundary is guard-owned: explicit FULL and setup-scoped
requests execute the exact declared setup under the guard before native pytest,
while automatic, selected, shadow and probe requests remain typed unsupported.
`--no-setup` refuses missing or stale declared dependencies before admission to
a child. The locked-dependency fixture tests in
`test_pytest_adapter.py` retain an invalid machine-budget/setup refusal and
assert that no dependency environment is provisioned; the explicit no-setup
FULL path is exercised separately by `test_pytest_full_subprocess.py` on the
already provisioned controller tuple.
Scoped subprocess tests exercise candidate ptest, the real guard and scheduler, native pytest, private
terminal reports and cleanup. Runtime checks precede pytest import where possible;
initial plugin qualification precedes test collection, after initial
conftest/plugin imports. Qualification repeats after all collection-finish
implementations, immediately before the test loop, before each item protocol,
and at each test body's call boundary after setup hooks and fixtures. A final
post-`pytest.main` qualification runs before a complete terminal report can be
written, catching registrations from the last item's teardown. Boundary and
terminal refusals produce incomplete ptest-origin evidence; a genuine native
nonzero observed before a late refusal remains nonzero. Nested conftest imports,
test-module collection and setup registration side effects can already have run
at these points. This is local execution, not a sandbox; the controls prevent
ordinary unowned executors from certifying a false pass, but do not claim to
contain hostile project code that catches or forges internal failures. The
current critical-hook gate refuses unqualified plugin wrappers. A locked
fixture tuple admitted by the closed advanced catalog may load the approved
pytest-cov hooks, but it still needs measured checkout files and an observed
terminal reporter before qualification.

This scoped tier preserves native Pytest's ordinary partial-selection semantics
and deliberately publishes no inventory, count, source-validity, baseline, or
history claim. Native collection/setup-only modes, deselection in project code,
and native outcome rewriting are therefore not proof that every discoverable
test body ran; they are outside this executor-ownership control. A late bridge
refusal always makes the result incomplete with ptest origin. If Pytest had
already observed a genuine nonzero exit, ptest preserves that nonzero code for
diagnosis while withholding a certified native result.

The current evidence executes the provisioned Linux CPython 3.13.11 /
pytest 9.1.1 / pluggy 1.6.0 tuple. The real advanced SELECT fixture is locked
under `tests/ng/fixtures/pytest/9.1.1` with pytest-cov 7.1.0 and coverage
7.15.0; it runs a full baseline and then exact-file SELECT through the
explicit interpreter override. Pytest 8.4.2, 9.0.3 and 9.1.0,
an installed xdist tuple and an unsupported pytest runtime remain explicit
unqualified skips unless matching interpreters are supplied through the
documented environment overrides; no unqualified tuple promotes them.

Task 11F implements explicit Pytest `--full` basic-serial admission on top of
the scoped lifecycle. Full mode retains configured `runner.args`, appends
`runner.full_args` and the exact configured roots, grants one slot regardless
of the requested worker count, freezes mode/roots before project import, and
authenticates the existing private terminal report. It rejects native
narrowing, redirect/configuration controls, executor hooks, dot roots,
automatic/selected/shadow/probe requests and `--base`; declared setup is
accepted only through the guard-owned setup lifecycle. The safe
`--strict`/`--strict-config`/`--strict-markers` flags are preserved (pytest 9
expresses them as exact strict-true override-ini entries); every other
`-o`/`--override-ini` value remains refused. Static `where`/`register`
summaries report the enforced serial worker count (1) for Pytest while generic
commands keep their configured count. Allowed terminal/log/
cleanup hooks remain additive; collection-finish item mutation and setup-time
skips are explicitly cooperative limitations, so inventory, counts, source
validity and full-gate claims remain unavailable.

The full-only source domain excludes only the exact checkout-root Pytest cache
files and regular assertion-rewrite/CPython bytecode with an included source
module. Tracked, declared-input, malformed, sourceless, nested/custom-cache,
symlink and raced outputs remain inputs. Equal execution-only digests can carry
an authenticated native outcome despite missing compatibility identity;
missing/non-Git/over-budget evidence makes a zero run incomplete/70 while
preserving an observed native nonzero.

The implementation evidence in `.pipeline/out/task-11f-pytest-full.json` records
the exact controller command and the final repair regression: 751 passed and 8
explicitly unqualified tuples skipped in 109.14s on Linux CPython 3.13.11 /
pytest 9.1.1 / pluggy 1.6.0. Real full-subprocess coverage now includes plain
plus wrapper forms of collection-modifyitems, runtest-makereport,
report-teststatus and sessionfinish, a live pytest_-prefixed specname alias and
late registration (each refused with exit 4 and ptest origin before any test
body runs), preserved safe strict controls, and a sparse-file scan-limit case
that ends incomplete/70. Pytest 8.4.2, 9.0.3 and 9.1.0, xdist, coverage,
macOS and independent-repository adoption remain explicitly unqualified.

Residual native qualification outside this slice includes bounded xdist worker
instrumentation/inventory, coverage thresholds, collection/setup/teardown error
matrix expansion, and the broader interpreter/runner/plugin compatibility matrix.
This fixture subset is not the entire Task 5 acceptance matrix.

Task 12b adds a separate advanced bridge/report contract with strict native
inventory, runtime-digest, coverage/reporter, and worker-identity evidence;
basic serial remains execution-only. The closed adapter registry does not
promote advanced support from configuration or a stub: the real locked
Q-PY-SELECT tuple is cataloged and exercised by
`test_q_py_select_real_coverage_baseline_then_exact_selected_file`, while
Q-PY-PROBE and all Vitest Q profiles remain explicitly unsupported. The
cataloged SELECT tuple must explicitly carry `--cov` and `--cov-report`; the
bridge then requires measured files inside the checkout and an observed
terminal reporter, not merely configured flags. Missing, changed, duplicate,
or unknown evidence is rejected rather than converted into a selection claim.
