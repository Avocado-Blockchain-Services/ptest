`matrix.json` is the Task11 acceptance input; every native case is NOT RUN.
`project/` contains two packages and a shared trace probe with parallel subtests.
Copy the project under a private explicit fixture-domain and invoke only candidate
ptest. Resolve `{run_output}` to a run-owned path outside the source snapshot.
Supply a fresh trace for each attempt (or partition appended lines); both repeated
Go runs must add eight starts and eight ends, proving execution despite warm cache.
Order trace events by timestamp and assert only one test process runs at once,
at most two subtests overlap, and every GOMAXPROCS observation is two.

Task10 only verifies preparation; Go remains `unavailable`. Native qualification
must also validate the chosen executable/version, persisted `go env` controls,
and identical environment/config snapshots from preparation through launch.
The conservative profile owns `-p=1`, `-parallel=grant`, `-cpu=grant`, `-count=1`,
and `GOMAXPROCS=grant`. Unknown/forwarded/filter/no-test options and file plans
are refused. Package roots are explicit local directories/patterns.
Inner exit 23 or SIGTERM is expected to become the **go CLI's** exit 1; it must
not be confused with a signal delivered to the top-level runner. Task11 must
verify that distinction and guard cancellation separately. Fixture traces are
acceptance evidence only; public test counts remain unknown, not text-derived.
