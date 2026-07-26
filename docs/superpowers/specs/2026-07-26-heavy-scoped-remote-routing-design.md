# Heavy scoped runs route to the remote backend

Date: 2026-07-26
Status: approved, not yet implemented

## Problem

ptest routes on a binary: `--full` goes to the configured backend, everything
else runs on this machine at the worker cap. That binary is too coarse at the
top end.

In `persea-api` the suite is 334 test files / 5295 test functions. `tests/api`
alone is 221 files / 3771 functions — 66% of the files, 71% of the tests.
`ptest tests/api` therefore passes every scoping check ptest has (there are
1000+ test functions outside it, so it is genuinely not the full suite) and then
runs roughly seven tenths of the suite locally, capped to 2 workers, for many
minutes of wall clock. Meanwhile the project has a verified Cloud Run backend
with 8 dedicated vCPU sitting idle, reachable only by asking for the *whole*
suite.

The user wants a third tier: a scoped run that is large enough to be worth
shipping off-box goes to the backend; a genuinely narrow run stays local, where
the ~2 minutes of pack/upload/cold-start would dominate.

## Prerequisite: the installed copy has drifted from this repo

`~/.local/bin/ptest` is 873 lines. This repo's `ptest` is 725. The installed
copy holds five functions that were never committed here:

    all_test_files  refuse_disguised_full  full_suite_targets
    root_targeting_args  has_narrowing_filter

That is the entire "refuse a disguised full suite" feature — what makes
`ptest tests`, `ptest .` and bare `ptest` exit 2 instead of quietly collecting
everything. It is live on this machine and absent from git.

It is also the foundation this change builds on: `all_test_files` is how
coverage gets counted, `has_narrowing_filter` is how `-k` keeps a run local.

**The first commit of this work is that drift, committed verbatim, on its own.**
Building on top of uncommitted live code, or implementing against the repo's
older `main()`, would silently revert working behaviour on install.

## The routing rule

A new pure function reports how much of the suite a scoped invocation covers:

    heavy_scoped_files(passthrough, root) -> int

It resolves each path-like argument against `all_test_files(root)` and returns
the size of the union of test files underneath them. A file named twice counts
once.

Non-path arguments are ignored, and a flag's *value* must never be mistaken for
a path. Note that `root_targeting_args` does **not** currently implement this:
it skips only tokens starting with `-`, and its docstring's claim that the
exists-on-disk test protects a `-k` value holds only while that value is not
itself a real path. `--rootdir tests` is the counter-example — `tests` exists,
so it is read as a selected path today.

So this work extracts one shared helper, `path_like_args(passthrough)`, that
skips both flags and the values of flags known to take a separate value, and
routes **both** `heavy_scoped_files` and `root_targeting_args` through it.
One implementation, not two.

An argument that does not resolve to an existing path under `root` contributes
zero and is not an error: ptest is not a path validator, and the underlying
runner already reports a bad path better than ptest could. The consequence is
that a typo'd path routes local, which is the safe direction.

In `main()`, after the existing refusal checks and before the local run, a
scoped invocation routes to the backend when **all** of these hold:

1. `pcfg["backend"] == "cloudrun"`
2. `--local` was not passed
3. `has_narrowing_filter(passthrough)` is false
4. `heavy_scoped_files(...) >= remote_scoped_min_files`

Condition 3 is deliberate: a filter means the caller is hunting a specific
failure and wants the fastest possible loop, not the largest machine.
`ptest tests/api -k keyword_language` stays local.

Resulting behaviour in `persea-api` at the default threshold of 40:

| invocation | files covered | routes |
|---|---|---|
| `ptest tests/api` | 221 | remote |
| `ptest tests/db` | 61 | remote |
| `ptest tests/crawler` | 23 | local |
| `ptest tests/llm` | 17 | local |
| `ptest tests/unit` | 3 | local |
| `ptest tests/api/test_keywords.py` | 1 | local |
| `ptest tests/api -k foo` | 221 | local (filter) |
| `ptest --local tests/api` | 221 | local (forced) |

### Refusals still come first

`ptest tests`, `ptest .` and bare `ptest` continue to exit 2 via
`refuse_disguised_full`. They are **not** silently upgraded into a remote full
run. Asking for the whole suite is spelled `--full`, and that stays true — the
refusal exists to teach the correct command, and a silent upgrade would remove
the lesson while making `--full` and its coverage gate skippable by accident.

## What runs remotely

The project's `scoped` command, not `full`. A heavy scoped run is still a
scoped run: same test selection, **no coverage gate**, just executed on the
container instead of this laptop.

Output is not identical, though. `summarize_remote_logs` reduces the
container's stdout to a digest before printing it — on a green run only the
summary/coverage lines survive, so `ptest tests/db -v` routes remote and the
`-v` per-test listing never reaches the terminal. Failure tracebacks do
survive (the digest keeps the whole FAILURES section on a red run), which is
why the trade is acceptable, but it is a real difference from a local run's
verbatim output, not just a change of location. `ptest --local <path>` keeps
the run — and its full, unsummarized output — on this machine.

A heavy scoped run also uploads the working tree to the bucket, same as
`--full` does: same packing (`git ls-files`) and same `.env` stripping, so
there is no new secret-exposure class — but a command that previously never
left this machine now does.

Parallelism is the one deliberate difference. Local runs are capped at
`workers` (2) because several agents share this box; the container is dedicated,
so capping it there would discard the entire benefit. A remote scoped command is
built with container parallelism instead of the cap:

| kind | flag appended | why |
|---|---|---|
| pytest | `-n auto` | pytest-xdist is opt-in; without it pytest is serial and the remote run would be *slower* than local |
| vitest / npm | *(none)* | defaults to all available cores already |
| go | *(none)* | parallelises across packages by default |
| cargo | *(none)* | defaults to all available cores |

pytest is the only runner needing an explicit flag, and omitting it is the
failure mode worth guarding against: `uv run pytest tests/api` in the container
with no `-n` runs ~3771 tests single-threaded.

Caller arguments are appended after this flag, so an explicit flag always wins:
`ptest tests/api -n 0` runs serially, remotely, exactly as asked.

## Failure semantics

Mirrors `--full` as implemented in `run_cloudrun`, so there is one rule to
remember rather than two:

| condition | behaviour |
|---|---|
| job at `max_concurrent_remote_runs` | **exit 75**, nothing runs |
| daily remote cap spent | local at 2 workers, one warning |
| `gcloud` missing / not configured | local at 2 workers, one warning |
| upload failed / execution error | local at 2 workers, one warning |

The distinction is *busy* versus *broken*. Busy means other heavy runs are
genuinely in flight and adding local load on top is the harm the tool exists to
prevent — so it refuses, and exit 75 keeps meaning "nothing ran, retry later",
never "tests failed". Broken means the remote path is unavailable through no
fault of load, and degrading to the local run the caller would have got
yesterday is strictly better than bricking the command.

### Accepted consequence

Heavy scoped runs and `--full` runs now compete for the same 6-slot job
concurrency. A busy `tests/api` loop can therefore return exit 75 for someone
else's `--full`. This was raised and accepted: the 6-slot ceiling is a
runaway-loop brake set well above normal use, and exit 75 is already a
well-understood, non-destructive outcome with a documented reaction.

## Configuration

    [defaults]
    remote_scoped_min_files = 40      # 0 disables the tier

Overridable per project. Absolute file count, not a percentage: the number that
matters is how long the run takes, which tracks test count, not the ratio to a
suite that grows underneath it.

**40 is an estimate, not a tuned value.** It cleanly separates *file counts* —
`tests/db` (61) from `tests/crawler` (23) in the repo that motivated this — but
the tier's premise is that remote is *faster* above the line, and that has
never been measured. One live remote run (`ptest tests/db` against persea-api,
2026-07-25) proved correctness — the job executed, Postgres came up, tests
passed — not timing. `tests/db` sits barely above the boundary at 61 files and
is the likeliest case to be a net loss once ~2:45 of pack/upload/cold-start is
priced in against however long 61 files actually take locally. The open
question that would settle this: time `ptest tests/db` against
`ptest --local tests/db` on persea-api and compare. Not done here, deliberately
— see Out of scope.

Projects with `backend = "local"` are unaffected regardless of threshold.

## Observability

A routed run says so before it goes, because a command that silently takes a
different path for 2 minutes reads as a hang:

    [ptest] tests/api covers 221/334 test files — routing to the remote backend

## Verification

ptest has no unit tests today; `smoke.sh` is a live GCP round trip, too slow and
too stateful to gate a routing rule on.

Both new pieces are pure functions over a list of paths, so they are testable
with no network and no GCP. Add `tests/test_routing.py` to this repo, building a
tmpdir of fake test files and asserting the verdict for each row of the
behaviour table above, plus:

- threshold boundary: exactly at, and one below, `remote_scoped_min_files`
- `remote_scoped_min_files = 0` disables routing
- `backend = "local"` never routes however large the path
- two args covering overlapping trees are counted as a union, not summed
- `-k` whose *value* looks like a path (`-k tests/api`) is not counted as a path
- the pytest `-n auto` flag appears in the remote command, and a caller's
  `-n 0` follows it

The end-to-end proof that a real heavy scoped run reaches Cloud Run is one
manual `ptest tests/db` against `persea-api` after install — not automated.

## Out of scope

- Changing what `--full` does, or its coverage gate
- Turning existing refusals into remote runs
- Any change to the container images, entrypoint, or Terraform
- Auto-tuning the threshold from measured runtimes
