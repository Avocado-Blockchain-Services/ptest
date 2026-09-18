# Task 5 native cases: NOT RUN

These four isolated environments pin the enumerated pytest candidates and exclude
xdist. Task 5 only resolves lock metadata; it does not install or execute them.
The `native_cli` tests in `test_pytest_adapter.py` create projects under the shared
fixture domain and call `case.invoke`, which launches candidate ptest. Candidate
ptest alone runs the declared locked dependency setup and the native runner.

The tests are deliberately not skipped when the candidate CLI is absent or its
pytest profile remains unavailable. T11 must qualify the profile before these
can pass. Green unit tests or lock resolution do not promote runtime capability.

Defined here: basic serial automatic/full pass and failure across 8.4.2, 9.0.3,
9.1.0 and 9.1.1; serial argv/import behavior; full narrowing through ini/env and
argfiles; combined parallel flags and explicit remote/proxy refusal.

Still to author/execute at the native qualification stage: bounded xdist worker
instrumentation/inventory, full pytest 9 TOML precedence, coverage thresholds,
custom reporter preservation/corruption, collection/setup/teardown errors, unknown
critical hooks and the interpreter/runner/plugin compatibility rejection matrix.
This fixture subset is not the entire Task 5 acceptance matrix.
