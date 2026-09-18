# Pytest candidate fixtures and scoped execution evidence

These four manifests pin the enumerated pytest candidates and exclude xdist.
They are dependency metadata, not provisioned environments. Tests never install
them. `test_pytest_scoped_subprocess.py` uses an already provisioned controller
interpreter or an explicitly supplied `PTEST_TEST_PYTHON_8_4_2` (and corresponding
version) interpreter. Absent tuples skip with an explicit unqualified reason.
The actual controller run, versions, failures and skips are recorded in
`.pipeline/out/task-11d-luna-final-repair.json`; earlier repair evidence remains
in `.pipeline/out/task-11d-astra-repair.json`, `.pipeline/out/task-11d-nested-hook-repair.json`
and `.pipeline/out/task-11d-sol-repair.json`.

Current `native_cli` tests in `test_pytest_adapter.py` assert the Task 11D boundary:
FULL, AUTOMATIC and setup are typed unsupported before admission or dependency
execution. This does not qualify any candidate tuple. Scoped subprocess tests
exercise candidate ptest, the real guard and scheduler, native pytest, private
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
current critical-hook gate also refuses unqualified plugin wrappers, including
active pytest-cov `--cov` runs; preserving coverage arguments does not qualify
those runs.

This scoped tier preserves native Pytest's ordinary partial-selection semantics
and deliberately publishes no inventory, count, source-validity, baseline, or
history claim. Native collection/setup-only modes, deselection in project code,
and native outcome rewriting are therefore not proof that every discoverable
test body ran; they are outside this executor-ownership control. A late bridge
refusal always makes the result incomplete with ptest origin. If Pytest had
already observed a genuine nonzero exit, ptest preserves that nonzero code for
diagnosis while withholding a certified native result.

The current evidence executes only the provisioned Linux CPython 3.13.11 /
pytest 9.1.1 / pluggy 1.6.0 tuple. Pytest 8.4.2, 9.0.3 and 9.1.0, an installed
xdist tuple and an unsupported pytest runtime remain explicit unqualified skips
unless matching interpreters are supplied through the documented environment
overrides; no fixture installation promotes them.

Task 11F implements explicit Pytest `--full` basic-serial admission on top of
the scoped lifecycle. Full mode retains configured `runner.args`, appends
`runner.full_args` and the exact configured roots, grants one slot regardless
of the requested worker count, freezes mode/roots before project import, and
authenticates the existing private terminal report. It rejects native
narrowing, redirect/configuration controls, executor hooks, dot roots, setup,
automatic/selected/shadow/probe requests and `--base`. Allowed terminal/log/
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
the exact controller command and the current qualification prerequisite. The
assigned worktree does not currently contain the provisioned pytest tuple, so
no candidate runtime/version is claimed qualified until the mandated controller
bootstrap can execute. Pytest 8.4.2, 9.0.3, 9.1.0 and 9.1.1, xdist, coverage,
macOS and independent-repository adoption remain explicitly unqualified.

Still to author/execute at the native qualification stage: bounded xdist worker
instrumentation/inventory, full pytest 9 TOML precedence, coverage thresholds,
custom reporter preservation/corruption, collection/setup/teardown errors, unknown
critical hooks and the interpreter/runner/plugin compatibility rejection matrix.
This fixture subset is not the entire Task 5 acceptance matrix.
