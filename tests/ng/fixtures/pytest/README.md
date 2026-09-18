# Pytest candidate fixtures and scoped execution evidence

These four manifests pin the enumerated pytest candidates and exclude xdist.
They are dependency metadata, not provisioned environments. Tests never install
them. `test_pytest_scoped_subprocess.py` uses an already provisioned controller
interpreter or an explicitly supplied `PTEST_TEST_PYTHON_8_4_2` (and corresponding
version) interpreter. Absent tuples skip with an explicit unqualified reason.
The actual controller run, versions, failures and skips are recorded in
`.pipeline/out/task-11d-astra-repair.json`, with the nested-conftest correction
recorded in `.pipeline/out/task-11d-nested-hook-repair.json`.

Current `native_cli` tests in `test_pytest_adapter.py` assert the Task 11D boundary:
FULL, AUTOMATIC and setup are typed unsupported before admission or dependency
execution. This does not qualify any candidate tuple. Scoped subprocess tests
exercise candidate ptest, the real guard and scheduler, native pytest, private
terminal reports and cleanup. Runtime checks precede pytest import where possible;
initial plugin qualification precedes test collection, after initial
conftest/plugin imports. Qualification repeats after all collection-finish
implementations to reject executors registered there, then again at each test
body's call boundary after setup hooks and fixtures. Nested conftest imports,
test-module collection and setup registration side effects can already have run
at these points. This is local execution, not a sandbox; the controls prevent
ordinary unowned executors from certifying a false pass, but do not claim to
contain hostile project code that catches or forges internal failures. The
current critical-hook gate also refuses unqualified plugin wrappers, including
active pytest-cov `--cov` runs; preserving coverage arguments does not qualify
those runs.

Pending full-tier acceptance (preserved from Task 5): basic serial automatic/full
pass and native failure across 8.4.2, 9.0.3, 9.1.0 and 9.1.1; candidate-owned
locked dependency setup; full narrowing through ini/env/argfiles; full native
stdout, exit and baseline restrictions. These require a later authorized tier;
they are not silently promoted or represented as passing native execution here.

Still to author/execute at the native qualification stage: bounded xdist worker
instrumentation/inventory, full pytest 9 TOML precedence, coverage thresholds,
custom reporter preservation/corruption, collection/setup/teardown errors, unknown
critical hooks and the interpreter/runner/plugin compatibility rejection matrix.
This fixture subset is not the entire Task 5 acceptance matrix.
