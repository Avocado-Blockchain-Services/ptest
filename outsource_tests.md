# Outsourcing a test suite: parallel-safe databases, worktree isolation, and remote execution on GCP

A complete, reproducible description of a test setup that solves three problems at once:

1. **A test suite that runs in parallel against a real database** without tests
   corrupting each other.
2. **Several checkouts of the same repo** (git worktrees, one per concurrent
   agent/developer) sharing one Postgres without destroying each other's data.
3. **Full-suite runs that don't consume the developer machine** — dispatched to
   Cloud Run Jobs, with a database living in RAM inside the container.

The concrete stack here is Python 3.12 / pytest / SQLAlchemy 2 async / Postgres 15
/ Vitest / GCP. The *ideas* port to any language with a parallel test runner and
any cloud with a batch-job primitive. Where something is genuinely
stack-specific, it is called out.

Written to be replicable by someone (or some model) who has never seen this
repo. Every non-obvious decision includes the failure it prevents, because the
failures are what make the design look arbitrary until you have hit them.

---

## Table of contents

- [Part 0 — Why](#part-0--why)
- [Part 1 — The local test rig](#part-1--the-local-test-rig)
  - [1.1 One database per worker](#11-one-database-per-worker)
  - [1.2 The worktree tag](#12-the-worktree-tag-the-part-everyone-gets-wrong)
  - [1.3 Per-test isolation: savepoint rollback](#13-per-test-isolation-savepoint-rollback)
  - [1.4 The escape hatch: tests that really commit](#14-the-escape-hatch-tests-that-really-commit)
  - [1.5 The drift guard](#15-the-drift-guard)
  - [1.6 Safety rails](#16-safety-rails)
- [Part 2 — The dispatcher](#part-2--the-dispatcher)
  - [2.1 Why a wrapper at all](#21-why-a-wrapper-at-all)
  - [2.2 Worker caps and runner detection](#22-worker-caps-and-runner-detection)
  - [2.3 Config format](#23-config-format)
- [Part 3 — Remote execution](#part-3--remote-execution)
  - [3.1 Architecture](#31-architecture)
  - [3.2 The container: Postgres in RAM](#32-the-container-postgres-in-ram)
  - [3.3 The entrypoint contract](#33-the-entrypoint-contract)
  - [3.4 Packing the source](#34-packing-the-source)
  - [3.5 Runaway protection](#35-runaway-protection)
- [Part 4 — Terraform](#part-4--terraform)
- [Part 5 — Hard-won gotchas](#part-5--hard-won-gotchas)
- [Part 6 — Replication checklist](#part-6--replication-checklist)
- [Part 7 — Measured results](#part-7--measured-results)

---

## Part 0 — Why

Three forces collide:

**Real databases beat mocks.** A repository-layer test against a mock proves the
mock works. Testing against real Postgres catches constraint violations, cascade
behaviour, transaction semantics, and migration drift. So: real database.

**Parallel test runners auto-detect the machine.** `pytest -n auto`, `vitest`,
`jest`, `ava`, `go test` all default to roughly "every core". That is correct for
a CI box that owns its hardware and catastrophic on a shared workstation. With
three concurrent sessions each auto-detecting 16 cores, load average passes 40
and the machine swaps itself to death.

**Concurrent work means concurrent checkouts.** Multiple agents or developers
working in parallel each want an isolated worktree. If test-database names are
derived from anything they share, they collide — and the collision is *silent
data destruction*, not a clean error.

The solution has three layers, each independently useful:

| Layer | Solves |
|---|---|
| Per-worker databases, keyed by checkout | Parallel safety within and across checkouts |
| A dispatcher that caps workers | Shared-machine survival |
| Remote execution for full runs | Full suites cost the workstation nothing |

---

## Part 1 — The local test rig

Everything here lives in one root `tests/conftest.py`.

### 1.1 One database per worker

**Exactly one database per xdist worker.** Not per test, not per module.

```
serial run:  test_<worktree_tag>_serial
xdist run:   test_<worktree_tag>_gw0, test_<worktree_tag>_gw1, …
```

```python
def _get_test_db_name(request) -> str:
    worker_id = getattr(request.config, "workerinput", {}).get("workerid", "") or "serial"
    return f"test_{_WORKTREE_TAG}_{worker_id}"
```

`workerinput` is injected by `pytest-xdist` into each worker's config and is
absent in a serial run — that is the whole detection mechanism.

Schema is created once per worker with `Base.metadata.create_all`, **not** by
running migrations. Migrations are exercised by their own dedicated tests
(§1.6). Using `create_all` keeps worker startup to well under a second; running
a long migration chain per worker would dominate the suite.

Why not per-module databases? Because per-test isolation comes from transaction
rollback (§1.3), not from a fresh database. Once rollback is doing the isolation,
extra databases buy nothing and cost a CREATE + full schema build each.

### 1.2 The worktree tag (the part everyone gets wrong)

This is the subtle one, and getting it wrong produces failures that look like
flaky tests for weeks.

**The problem.** Two checkouts of the same repo run their suites at the same
time against the same Postgres. Both compute worker id `gw0`. Both want
`test_gw0`. Then:

- Run B's `DROP DATABASE IF EXISTS test_gw0` fails or blocks while run A holds
  connections; or worse,
- Run B's setup terminates run A's live backends and drops the database **out
  from under a running test**.

The symptom is a test failing with a connection error in a module that has
nothing to do with anything. It is not reproducible alone. It looks like flake.

**The fix.** Namespace every database by *which checkout it came from*:

```python
def _worktree_tag(root: str) -> str:
    path = Path(root)
    slug = re.sub(r"[^a-z0-9]+", "_",
                  f"{path.parent.name}_{path.name}".lower()).strip("_")[:28]
    return f"{slug}_{hashlib.sha1(root.encode()).hexdigest()[:8]}"

_WORKTREE_TAG = _worktree_tag(_REPO_ROOT)   # computed once, at import
```

Four decisions, each load-bearing:

**(a) Derived from the absolute path of `conftest.py` itself.** Not from an
environment variable (agents forget to set it), not from the branch name (two
worktrees can share a branch), not from the CWD (you can invoke pytest from
anywhere).

**(b) The last TWO path components, not the basename.** Both
`worktrees/<feature>/api` and `worktrees/<feature>-api` layouts are common. With
a bare basename, every worktree in the first layout is called `api` and they all
collide. Taking the parent directory too keeps them distinct.

**(c) A path digest appended.** The slug is truncated to 28 chars and is blind to
anything above those two components. `sha1(abs_path)[:8]` guarantees uniqueness
that a truncated human-readable slug cannot. Keep the slug anyway — when you are
staring at `\l` in psql, `persea_api_3f2a9c1b` tells you which checkout owns it
and a bare hash does not.

**(d) Deterministic, NOT random.** Tempting to use a per-session UUID. Don't. If
a run is `kill -9`'d, its finalizer never runs and the database is never dropped.
A deterministic name is reclaimed by the next run from that same checkout,
because creation does `DROP DATABASE IF EXISTS` before `CREATE`. A random name
would strand that database on disk forever, and you would slowly accumulate
hundreds of them.

> **Port this idea, not the code.** Any language: hash the absolute path of the
> test-root file, combine with the runner's worker id, use it as the database (or
> schema, or keyspace) name. In Jest, `process.env.JEST_WORKER_ID`; in Go,
> derive from `-parallel` shard; in Vitest, `process.env.VITEST_POOL_ID`.

### 1.3 Per-test isolation: savepoint rollback

Each test runs inside an outer transaction that is rolled back at teardown:

```python
async with engine.connect() as conn:
    trans = await conn.begin()
    session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
    yield session
    await session.close()
    await trans.rollback()          # undoes everything the test did
```

`join_transaction_mode="create_savepoint"` (SQLAlchemy 2.0) is the key. Inside
the test:

- Inserts and queries hit the real database and real constraints fire.
- `session.commit()` becomes `RELEASE SAVEPOINT` — it does **not** actually
  commit.
- The outer `rollback()` at teardown undoes the lot.

Result: full isolation with no truncation and no re-created schema between tests.
It is fast, and tests can run in any order.

**The trap this creates.** Because `session.commit()` is neutered, *the test rig
cannot detect a missing real commit in production code.* If your request handler
forgets to commit, every test still passes and production silently rolls back
writes.

Mitigation: own the commit boundary in exactly one place — a
request-scoped dependency that commits on clean return and rolls back on
exception — and cover *that* with an integration test that uses a real session
rather than a fake repository. Know this hole exists; it is the price of the
speed.

### 1.4 The escape hatch: tests that really commit

Some tests must genuinely commit: races over a limited resource, advisory locks,
lifecycle transitions where concurrent commits are the thing under test. They
drive their own engine and connection, so their `COMMIT` escapes the savepoint
and survives the outer rollback — leaking rows into the worker's database and
poisoning subsequent tests.

Those modules declare themselves:

```python
pytestmark = pytest.mark.real_commits
```

and the `db` fixture switches strategy: instead of rollback, it `TRUNCATE`s every
table after each test in that module.

```python
await conn.execute(sa_text(
    f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
```

Two details:

- Run the TRUNCATE **outside** the connection block that the test used. TRUNCATE
  takes `ACCESS EXCLUSIVE`; issuing it while the test's own connection is still
  open can deadlock against it.
- They still share the worker's single database. Giving them their own would
  reintroduce the per-module-database cost for a handful of modules.

### 1.5 The drift guard

A module that *should* be marked `real_commits` but isn't will commit rows that
never get cleaned, and the damage shows up in an unrelated test later. Relying on
reviewers to catch this does not work.

So there is a test that fails the build:

```python
# tests/test_conftest_db_naming.py
def test_modules_driving_own_engines_are_marked_real_commits():
    for path in TEST_FILES:
        src = path.read_text()
        drives_own_engine = ("create_async_engine" in src
                             or "async_sessionmaker" in src
                             or "psycopg" in src)
        marked = "pytest.mark.real_commits" in src
        assert not (drives_own_engine and not marked), (
            f"{path} builds its own engine but is not marked real_commits")
```

**Make sure CI actually runs it.** In the original repo, CI enumerated specific
test paths and this file sat outside them — the guard existed and had never once
executed. A guard you do not run is worse than no guard, because you believe you
are covered. Either run the whole tests tree, or assert the guard file is in your
path list.

### 1.6 Safety rails

**Refuse to touch anything not named `test_*`:**

```python
def _validate_test_db_name(db_name: str) -> None:
    if not db_name.startswith("test_"):
        raise RuntimeError(f"SAFETY ABORT: refusing to create/drop {db_name!r}")
```

Called in both create and drop. The suite connects as a Postgres **superuser**
(it must `CREATE DATABASE`), so a bug in name computation could otherwise drop a
real database. Cheap insurance.

**Never `str()` a SQLAlchemy URL to rebuild a connection string:**

```python
# WRONG — masks the password to the literal "***"
url = str(make_url(base).set(database=name))

# RIGHT
url = make_url(base).set(database=name).render_as_string(hide_password=False)
```

SQLAlchemy's `__str__` hides the password. Rebuilding a URL that way produces a
connection string that authenticates with the literal string `***`. In the
source repo this broke CI in six places and went undiagnosed for **199
consecutive red runs**, because the error surfaces as an authentication failure
that looks like bad credentials rather than bad code.

**Migration tests stand apart.** They never take the `db` fixture; they create
and drop their own databases and run the migration tool against them. That keeps
`create_all` (fast, for the suite) and the migration chain (correct, for
production) both under test without either slowing the other.

---

## Part 2 — The dispatcher

A small wrapper — call it `ptest` — that every agent and human uses instead of
invoking runners directly.

### 2.1 Why a wrapper at all

Because "please remember to pass `-n 2`" is not a control. The wrapper is the
single place that knows: which runner this repo uses, what the worker cap is,
whether full runs go remote, and what the daily budget is. Agents are told
*never* to call `pytest`/`vitest`/`npm test` directly.

```
ptest <paths / -k args>    scoped run — always LOCAL, workers capped
ptest --full               whole suite — remote if configured, else local
ptest where                what resolved for this directory
ptest doctor               config + backend health
ptest register             print a config stanza for the repo you are in
```

The scoped/full split matters: scoped runs are the TDD inner loop and must stay
local and instant; full runs are the expensive thing worth outsourcing.

**Worktrees need no configuration.** The wrapper asks git which repository a
directory belongs to (`git rev-parse --git-common-dir`), resolves the config
stanza from *that*, and then runs in the **current** directory. Never `cd` to the
configured root — a run from a worktree would silently test the original
checkout, which is the most confusing possible failure.

### 2.2 Worker caps and runner detection

The cap is only meaningful if it reaches the runner. Two traps:

**Trap 1 — run-scripts swallow arguments.** `npm test --maxWorkers=2` passes the
flag to *npm*, not to the test runner. You need `npm test -- --maxWorkers=2`.
Insert the `--` only for npm/yarn/pnpm-shaped commands that lack one; never for a
direct invocation like `npx vitest run`, where it would change argument parsing.

**Trap 2 — the runner is not knowable from the command.** `npm test` says nothing
about what it runs. Read `package.json`'s `scripts.test` and detect the runner
from it.

Then cap **only runners that are parallel by default**:

| Runner | Flag | Note |
|---|---|---|
| vitest | `--maxWorkers=N --minWorkers=1` | parallel by default |
| jest | `--maxWorkers=N` | parallel by default |
| ava | `--concurrency=N` | parallel by default |
| playwright | `--workers=N` | defaults to ~half the cores |
| node:test | `--test-concurrency=N` | parallel by default |
| tap | `--jobs=N` | parallel by default |
| **mocha** | **(nothing)** | **serial by default** |
| **unknown** | **(nothing)** | do not invent a flag |
| pytest | `-n N` (or `-p no:xdist` when N=1) | |
| go | `-p N` | |
| cargo | `--jobs N` | |

**The governing principle: the goal is to REDUCE parallelism, never to introduce
it.** Mocha runs serially unless told otherwise, so "capping" it with `--parallel
--jobs 2` would *double* the load. An unrecognised script is most likely serial,
and inventing a flag risks passing something the runner rejects — turning a
worker cap into a broken test run.

**Strip before you set.** If the configured command already carries
`--maxWorkers=16` or `-n auto`, remove it and then append your cap. Do not append
a second flag and rely on which duplicate the runner honours.

### 2.3 Config format

One TOML file, `~/.config/ptest/config.toml`:

```toml
[defaults]
workers                    = 2   # per-agent cap for LOCAL runs. Never auto-detect.
daily_remote_runs          = 60  # per project, per day
max_concurrent_remote_runs = 6   # ceiling, not a mutex — see §3.5
region                     = "us-central1"
account     = "you@example.com"          # NEVER inherit `gcloud config set account`
gcp_project = "your-test-project"
bucket      = "your-test-project-ptest"

[projects.myapp-api]
root    = "/abs/path/to/repo"
kind    = "pytest"
workers = 2
scoped  = "uv run pytest"
full    = "uv run pytest -n auto --cov=pkg --cov-fail-under=85 tests"
backend = "cloudrun"
job     = "ptest-myapp-api"

[projects.myapp-front]
root    = "/abs/path/to/front"
kind    = "vitest"
workers = 2
scoped  = "npm test --"
full    = "npm test --"      # the `--` forwards the cap to vitest
backend = "cloudrun"
job     = "ptest-myapp-front"
```

Note `full` uses `-n auto` **because it is written for the remote container**,
which owns dedicated vCPU. The dispatcher rewrites that down to the local cap if
the run ever falls back to local (§3.5).

---

## Part 3 — Remote execution

### 3.1 Architecture

```
  developer machine                         GCP
  ─────────────────                         ───
  ptest --full
    │
    ├─ git ls-files → tar.gz  ──────────▶  GCS bucket (3-day lifecycle)
    │                                          │
    └─ gcloud run jobs execute --wait ──▶  Cloud Run Job
                                               │  PTEST_SRC = gs://…
                                               │  PTEST_CMD = "pytest -n auto …"
                                               ▼
                                          container:
                                            fetch tarball
                                            initdb → Postgres (RAM)
                                            install deps
                                            run PTEST_CMD
                                            exit with its code
```

Cloud Run **Jobs** (not Services): jobs are run-to-completion, have a task
timeout, and bill only for execution time. Any equivalent batch primitive works —
AWS Batch / ECS RunTask, Azure Container Instances, a Kubernetes Job.

**Why not build an image per run?** Because a source tarball to GCS is seconds
and an image build is minutes. The image holds only the *toolchain*; the source
arrives at execution time as an env var pointing at a blob.

### 3.2 The container: Postgres in RAM

**On Cloud Run the container filesystem is already memory-backed** — writes count
against the memory limit. So `PGDATA` on the normal filesystem *is* tmpfs. No
volume configuration, no `--mount type=tmpfs`. On other platforms, mount a tmpfs
at `PGDATA` explicitly.

Because the database dies with the container, durability is pure waste:

```conf
fsync = off
synchronous_commit = off
full_page_writes = off
max_connections = 300        # one pool per xdist worker adds up
shared_buffers = 512MB
listen_addresses = 'localhost'
```

> **Never copy this config anywhere real.** It trades crash safety for speed and
> is only correct because the data is disposable.

Postgres refuses to run as root; Cloud Run gives you root. Create `PGDATA` owned
by the `postgres` account and drop privileges just for the server. The tests
themselves can stay root — that avoids a permissions dance over the unpacked
source tree.

The suite needs a **superuser** (it issues `CREATE DATABASE`). `initdb -U <user>`
makes that user the bootstrap superuser. Assert it rather than assume:

```bash
psql -U "$PGUSER" -d postgres -tAc \
  "SELECT usesuper FROM pg_user WHERE usename = '$PGUSER'" | grep -qx 't' \
  || die "role $PGUSER is not a superuser — the temp-DB rig cannot work"
```

### 3.3 The entrypoint contract

Two environment variables, set per execution:

| Var | Meaning |
|---|---|
| `PTEST_SRC` | `gs://bucket/object.tar.gz` — the packed source tree |
| `PTEST_CMD` | the exact test command to run |

Plus `PTEST_KIND` (`pytest` \| `vitest`) baked into the **image**, not passed per
execution — so a job can never be pointed at an image lacking the tools it is
about to invoke.

**Exit-code discipline is the most important part of this file.** A reader — very
often an LLM — must be able to distinguish "the suite is red" from "the runner
broke", because those demand opposite reactions and the second must never be
reported as a passing build.

```
exit 0  → tests passed
exit 1  → tests failed  (pytest's own 2 = "interrupted" is remapped to 1)
exit 2  → THE RUNNER BROKE (fetch failed, initdb failed, deps failed)
```

Fetching the source without installing a cloud SDK (~1 GB) — one authenticated
GET against the metadata server:

```bash
TOKEN=$(curl -sf -H 'Metadata-Flavor: Google' \
  'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')

curl -s -w '%{http_code}' -o /tmp/src.tar.gz \
  -H "Authorization: Bearer $TOKEN" \
  "https://storage.googleapis.com/storage/v1/b/${BUCKET}/o/${OBJECT_URLENCODED}?alt=media"
```

The object name goes into a URL **path segment**, so slashes must be
percent-encoded (`/` → `%2F`).

### 3.4 Packing the source

**Ask git what the source is. Do not maintain an exclude list.**

```bash
git ls-files -c -o --exclude-standard -z
```

That is tracked files **plus** untracked files that `.gitignore` does not cover —
so a test file written seconds ago still ships, which a plain `git archive` would
silently drop.

A hand-maintained exclude list always drifts. In the source repo it did not know
about 416 MB of gitignored media fixtures and 199 MB of a generated knowledge
graph: **447 MB uploaded on every run**. Switching to git's own view: **4.8 MB**.
Ninety-three times smaller, and it can never drift again.

Two corrections on top:

**(a) Lockfiles are build INPUT — ship them even if gitignored.** If the repo
gitignores `uv.lock` / `package-lock.json`, git omits it, and then your installer
falls back from *syncing* to *resolving*. Resolution walks the entire dependency
graph including private optional extras that a normal run never installs, and
fails with an **authentication error naming your git host** — sending you to hunt
for credentials when the real cause is a missing lockfile. Re-add:
`uv.lock`, `poetry.lock`, `Pipfile.lock`, `package-lock.json`, `yarn.lock`,
`pnpm-lock.yaml`, `Cargo.lock`, `go.sum`.

**(b) Strip secrets — but not committed templates.** Exclude anything matching
`.env*`, **except** suffixes `.example`, `.sample`, `.template`, `.dist`.
Stripping `.env.example` — tracked, secret-free, and asserted on by tests that
verify documented environment variables — broke 15 tests in a way that looked
like an application regression and was pure runner damage.

### 3.5 Runaway protection

An agent in a retry loop can otherwise submit jobs indefinitely. Six layers:

| # | Control | Why |
|---|---|---|
| 1 | `--max-retries=0` | **Cloud Run Jobs default to 3.** One runaway becomes four billed executions. |
| 2 | `--task-timeout=1800s` | Server-side hard kill; survives a wedged client. |
| 3 | Concurrency **ceiling** | Bounds fan-out. |
| 4 | Daily cap per project | Bounds the day. |
| 5 | Bucket lifecycle (3d) | Tarballs cannot accumulate. |
| 6 | Budget alert | Backstop. |

Three design lessons, each learned the hard way:

**A ceiling, not a mutex.** The first version allowed exactly one execution per
job. That was wrong. Remote executions cost the local machine nothing — that is
the entire point of going remote — and separate worktrees legitimately need
separate full runs simultaneously. Blocking at 1 serialised independent work and
made agents spin in 30-second poll loops. Set the ceiling well above normal use
(6); it still stops a loop fanning out dozens.

**Fail closed.** The check must block when it cannot verify. The first version
used an invalid filter expression, the CLI errored, and the code treated "cannot
determine" as "nothing running" — so the guard silently never fired and three
concurrent full suites went through. If you cannot list executions, refuse.

**Blocking must NOT fall back to local.** This is the subtle one. If your
dispatcher's convention is "return None → run locally", then a blocked *full* run
executes the entire suite on the workstation — with several agents blocked at
once, that is exactly the meltdown the tool exists to prevent. **The guard would
have been worse than no guard.** Return a distinct exit code instead:

```
exit 75   # EX_TEMPFAIL — "busy, retry later", distinct from any test-runner code
```

and document loudly that **75 is not a test failure**, or every agent that sees
it will report your green suite as broken.

**And if a full run does legitimately fall back to local, re-cap it.** The `full`
command says `-n auto` because it was written for a dedicated container. Strip
that and force the local cap before running, and warn that you did.

---

## Part 4 — Terraform

The image must exist before the jobs can reference it, and Terraform does not
build images. Order: **build image → `terraform apply`**.

```hcl
# providers.tf
terraform {
  required_version = ">= 1.5"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
```

Complete, `terraform validate`-clean files live in [`terraform/`](terraform/) —
`providers.tf`, `variables.tf`, `main.tf`, `outputs.tf`. The essentials are
reproduced below; use the directory, not the excerpt.

```bash
cd terraform
terraform init
terraform apply \
  -var project_id=your-test-project \
  -var bucket_name=your-test-project-ptest \
  -var ar_repo=your-ar-repo \
  -var 'operator_members=["user:you@example.com"]'
```

`terraform output config_stanza` prints the block to paste into
`~/.config/ptest/config.toml`.

```hcl
# main.tf
locals {
  image_pytest = "${var.region}-docker.pkg.dev/${var.project_id}/${var.ar_repo}/ptest-runner:latest"
  image_node   = "${var.region}-docker.pkg.dev/${var.project_id}/${var.ar_repo}/ptest-runner-node:latest"
}

resource "google_project_service" "required" {
  for_each = toset([
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "storage.googleapis.com",
  ])
  service            = each.key
  disable_on_destroy = false
}

# ── Source drop ────────────────────────────────────────────────────────────
resource "google_storage_bucket" "src" {
  name                        = var.bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true   # tarballs are disposable

  # Without this, every run leaves a tarball behind forever and storage
  # quietly becomes the most expensive part of the setup.
  lifecycle_rule {
    action    { type = "Delete" }
    condition { age = var.src_retention_days }
  }
}

# ── Runner identity ────────────────────────────────────────────────────────
# Least privilege: read the source drop, write logs. Nothing else. This SA must
# never be able to reach production buckets, secrets, or databases.
resource "google_service_account" "runner" {
  account_id   = "ptest-runner"
  display_name = "ptest remote test runner"
}

resource "google_storage_bucket_iam_member" "runner_read" {
  bucket = google_storage_bucket.src.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.runner.email}"
}

resource "google_project_iam_member" "runner_logs" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.runner.email}"
}

# ── Jobs ───────────────────────────────────────────────────────────────────
resource "google_cloud_run_v2_job" "pytest" {
  name     = "ptest-myapp-api"
  location = var.region

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.runner.email
      # CRITICAL: Cloud Run Jobs default to 3 retries. One runaway loop would
      # become four billed executions.
      max_retries = 0
      timeout     = var.task_timeout

      containers {
        image = local.image_pytest
        resources {
          limits = { cpu = var.cpu, memory = var.memory }
        }
        # Placeholders; the dispatcher overrides both per execution with
        # `gcloud run jobs execute --update-env-vars`.
        env { name = "PTEST_SRC" value = "unset" }
        env { name = "PTEST_CMD" value = "unset" }
      }
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_cloud_run_v2_job" "vitest" {
  name     = "ptest-myapp-front"
  location = var.region

  template {
    task_count  = 1
    parallelism = 1
    template {
      service_account = google_service_account.runner.email
      max_retries     = 0
      timeout         = var.task_timeout
      containers {
        image = local.image_node
        resources { limits = { cpu = var.cpu, memory = var.memory } }
        env { name = "PTEST_SRC" value = "unset" }
        env { name = "PTEST_CMD" value = "unset" }
      }
    }
  }

  depends_on = [google_project_service.required]
}
```

```hcl
# outputs.tf
output "bucket"       { value = google_storage_bucket.src.name }
output "runner_sa"    { value = google_service_account.runner.email }
output "pytest_job"   { value = google_cloud_run_v2_job.pytest.name }
output "vitest_job"   { value = google_cloud_run_v2_job.vitest.name }
```

**The human running this** also needs `roles/storage.objectAdmin` on the bucket
(to upload tarballs) and `roles/run.developer` (to execute jobs). Grant to a
group, not an individual, if more than one person will use it.

**Build the images first:**

```bash
gcloud builds submit . --config=cloudbuild.pytest.yaml \
  --substitutions=_IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/ptest-runner:latest"
```

> **Never pipe `gcloud builds submit` into `tail`/`head`.** The pipe's exit
> status masks a failed build as success. Check `PIPESTATUS` or do not pipe.
> (The same mistake was made in this project twice — once on a deploy, once on
> a test run that was reported green while it had actually failed.)

---

## Part 5 — Hard-won gotchas

Ordered by how much time each cost.

**1. The CLI's active account is not yours.** If your dispatcher shells out to
`gcloud` without `--account`, it inherits whatever `gcloud config set account`
last was — on a machine that touches multiple clients, that is somebody else's
identity. It 403s, the code falls back to local, and *the remote backend silently
never runs*. Put the account in config and pass it explicitly, every call. Same
for `--billing-project` when the quota project is unrelated.

**2. A soft fallback hides a dead feature.** "Remote if configured, else local
with a warning" is good design and a great way to never notice remote has been
broken for months. Add a `doctor` command that prints what actually resolved, and
verify a real remote execution end-to-end before declaring it working.

**3. IAM is eventually consistent.** A service account can be created
successfully and still be invisible to `add-iam-policy-binding` for tens of
seconds. The error says `does not exist`, accompanied by an unrelated hint about
condition syntax. Retry with backoff.

**4. Cloud Run Jobs default to 3 retries.** Set `max_retries = 0` explicitly.

**5. Cold start is ~10 seconds, not minutes.** Do not optimise the image before
measuring. A long-looking progress spinner is usually the *run*, not the pull.
Slimming a 250 MB layer bought nothing here; parallelising the suite bought 5.4×.

**6. Validate config after writing it.** A sloppy string replacement left
dangling text in the TOML. The dispatcher printed `config unreadable — falling
back to local defaults` and quietly routed **every** project to local. Always
re-parse (`tomllib.load`) after editing.

**7. Align the full command with CI — but be a superset, not a copy.** Matching
CI exactly makes a green local run mean something. But if CI enumerates paths, it
may be skipping files; inherit that and you inherit its blind spot. Run the whole
tests tree so your pre-push gate is *stricter* than CI.

**8. Don't put fresh randomness in test data collected at import time.** Under
xdist, different workers collect independently — `uuid.uuid4()` in a class body or
`parametrize` argument produces *different* ids per worker, and assertions that
compare across them fail only in parallel. Generate inside a fixture or the test
function.

---

## Part 6 — Replication checklist

**Local rig**

- [ ] One database per worker, named `test_<checkout_tag>_<worker_id>`
- [ ] Checkout tag = slug of last two path components + `sha1(abs_path)[:8]`, deterministic
- [ ] Schema via `create_all`; migrations tested separately
- [ ] Per-test isolation by outer-transaction rollback + savepoint join mode
- [ ] `real_commits` marker → TRUNCATE strategy, executed outside the test's connection
- [ ] Drift guard test asserting own-engine modules are marked — **and CI runs it**
- [ ] `test_*` name guard on create and drop
- [ ] URL rebuild uses `render_as_string(hide_password=False)`

**Dispatcher**

- [ ] Scoped runs always local and capped; full runs may be remote
- [ ] Resolve project via `git rev-parse --git-common-dir`; run in CWD, never `cd`
- [ ] Detect runner from `package.json`; cap only parallel-by-default runners
- [ ] Insert `--` for npm/yarn/pnpm run-scripts; never for direct invocations
- [ ] Strip existing parallelism flags before setting yours
- [ ] Explicit cloud account/project on every CLI call
- [ ] `where` and `doctor` commands

**Remote**

- [ ] Batch job primitive, `max_retries=0`, explicit task timeout
- [ ] Least-privilege runner identity: read source bucket + write logs only
- [ ] Bucket lifecycle deletes tarballs
- [ ] Source packed via `git ls-files -c -o --exclude-standard`
- [ ] Lockfiles force-included; `.env*` excluded except `.example`/`.sample`/`.template`/`.dist`
- [ ] Database in RAM with durability disabled; superuser asserted
- [ ] Exit codes: 0 pass / 1 fail / 2 runner-broke / 75 busy
- [ ] Concurrency ceiling that fails closed and does NOT fall back to local
- [ ] Daily per-project cap
- [ ] End-to-end smoke on ONE test file before trusting a full run

---

## Part 7 — Measured results

Real numbers from the reference implementation. Suite: ~5,400 tests, Python,
real Postgres, 92% coverage gate. Container: 8 vCPU / 16 GiB.

| Metric | Value |
|---|---|
| Cold start (execution created → started) | **~10 s** |
| Container setup (fetch, unpack, initdb, Postgres, dep install) | **~7 s** |
| Dependency install (103 packages, warm network) | **4.9 s** |
| Suite, serial | **880 s (14:40)** |
| Suite, `-n auto` on 8 vCPU | **163 s (2:43)** — **5.4× faster** |
| End-to-end `ptest --full` | **283 s (4:43)** |
| Source tarball, hand-maintained excludes | 447 MB |
| Source tarball, `git ls-files` | **4.8 MB** |
| Cost per run (approx.) | **~$0.04** |

Takeaways: parallelism is the entire win; the tarball fix is second; the venv
(4.9 s) and cold start (10 s) are noise. Set the task timeout at roughly 10× the
observed runtime — at ~185 s of container work, 1800 s is comfortable, whereas it
was killing legitimate serial runs at 880 s.

---

## Licence / provenance

Extracted from a working private setup. No proprietary code is reproduced — the
snippets are the load-bearing patterns, rewritten to be generic. Adapt freely.
