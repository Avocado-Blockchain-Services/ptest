### Task 5: Native pytest bridge and optional-xdist profile

**Own:** `src/ptest/adapters/pytest.py`, `src/ptest/runtime/pytest_bridge.py`, `tests/ng/test_pytest_adapter.py`, `tests/ng/fixtures/pytest/`, `.pipeline/out/task-5.json`. No registry/CLI changes.

**Consumes:** PreparedRun and generated protocol-v1.json, config/plan/grant/attempt. **Produces:** `prepare(config,plan,grant,attempt)->PreparedRun` and runnable stdlib-only/pytest bridge in the selected **CPython3.11–3.14 project interpreter**, checked under admission before bridge import. Load T0's generated protocol descriptor alongside the trusted bridge; do not require ptest/psutil installed in the project interpreter or duplicate its schema. T5 may generate/commit fixture uv.lock files with explicit reported `uv lock` metadata resolution; fixture environments are installed by candidate ptest at T11.

- [ ] Write preparation assertion and native fixtures for serial and hostile flags:

```python
def test_serial_without_xdist_adds_no_xdist_flag(case):
    prepared = prepare(case.config(runner_kind="pytest", workers=1),
                       full_plan(case), grant(case, 1), attempt(case))
    assert "-n" not in prepared.argv
    assert "no:xdist" not in prepared.argv
    assert prepared.summary.workers == 1
```

`full_plan`, `grant`, `attempt` are local typed fixture constructors. The native fixture excludes xdist from its own uv environment, not merely disables it in the package dev environment.

- [ ] Run `scripts/ptest-bootstrap tests/ng/test_pytest_adapter.py -k serial_without`; implement native pytest.main, owned additive plugin, guarded effective config/critical-hook profile, remote/proxy rejection before gateways, and worker instrumentation import path. Do not handparse all addopts.
- [ ] Write real fixture cases for pytest9 native TOML/INI/env precedence, quoted -k and metacharacters, noxdist valid serial and absent-plugin -n error, excessive worker settings/config hooks, remote px/tx, pytest-cov threshold0/failedthreshold, custom reporters, parametrized IDs/setup/teardown/collection errors, missing worker inventory and reporter corruption. Add parametrized basic_serial cases for8.4.2/9.0.3/9.1.0/9.1.1 with xdist absent: runner exits preserved, grant1/nonexclusive, automatic full, no selection/baseline, optional verified coverage failure still nonzero. Unknown xdist/control plugin, incompatible interpreter and versions outside the enumerated table must fail before tests; do not disable plugins to pass. Unknown test IDs cannot clear prior obligations. Every actual subprocess fixture launches candidate ptest at T11; before then record NOT RUN, never direct pytest.
- [ ] Run preparation/unit scope `scripts/ptest-bootstrap tests/ng/test_pytest_adapter.py -k 'not native_cli'`; commit `feat: add bounded native pytest evidence bridge`. T11 must run all native_cli cases before this profile is promoted.

