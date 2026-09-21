# Reproducible off-table coverage fixture

This directory is a deliberate negative fixture. It is not a supported
qualification profile: the bridge's frozen advanced tuple is
`pytest-cov==7.1.0` with `coverage==7.15.0`.

Provision it outside the checkout, without mutating a test fixture or letting
the test suite install dependencies:

```sh
off_table_env=$(mktemp -d /tmp/ptest-qpy-off-table-XXXXXX)
UV_PROJECT_ENVIRONMENT="$off_table_env" uv sync --locked \
  --project tests/ng/fixtures/pytest/off-table-9.1.1
PTEST_TEST_PYTHON_OFFTABLE_COV="$off_table_env/bin/python" \
  ptest tests/ng/test_pytest_scoped_subprocess.py \
  -k test_real_off_table_coverage_tuple_refuses_before_tests -q
```

The probe must print `9.1.1 7.0.0 7.16.1`; the test then proves that the
bridge refuses before the fixture test body runs. The positive recipe is the
same shape with a separate temporary environment and
`--project tests/ng/fixtures/pytest/9.1.1`; its lock pins `pytest==9.1.1`,
`pytest-cov==7.1.0`, and `coverage==7.15.0`, with
`PTEST_TEST_PYTHON_9_1_1_COV` set to the preprovisioned interpreter. Neither
recipe creates a `.venv` in the checkout.
