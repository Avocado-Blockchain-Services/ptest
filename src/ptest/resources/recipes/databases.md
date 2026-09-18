# Databases

Initialize expensive database/schema state once per run or worker, not per test.
Give every concurrent worker a run/worker-owned database or schema. Factories make
per-test records explicit; teardown removes only records and namespaces owned by
that worker. Verify with a neighbor database sentinel that survives cleanup.
