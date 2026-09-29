# ptest

**Run only the tests your change reaches — locally, in parallel, safely next to
other runs.**

ptest is a local-first test coordinator for pytest and Vitest projects, and
runs Go (`go test ./...`) and Cargo (`cargo test --tests`) suites too. It sits in front of your test runner and:

- **runs the tests your edit reaches**, found from `git diff` and a static
  import graph — no coverage run, no baseline, no setup step;
- **understands monorepos**: a Python API, a React app and a CLI in one
  repository, each with its own runner and config;
- **coordinates concurrent runs** (you, your editor, three coding agents) with
  a machine-wide slot scheduler, so parallel test runs queue instead of
  fighting over CPU, ports and databases;
- **refuses to lie**: a run that cannot prove its result (sources changed
  mid-run, a plugin took over execution) ends `incomplete`, never `passed`;
- **teaches coding agents** how to test: `ptest init` installs short guidance
  for Claude Code, Codex, OpenCode and Gemini.

No cloud account, model API or remote service is needed to run tests.

```text
$ ptest
ptest: server · changed since last green run (93843fe, 23s ago): tests/fullon_credentials/unit/test_backend_switch.py → 1 of 608 test files (1 direct · 0 via importers)
============================== 7 passed in 2.43s ===============================
ptest: passed · 7 tests · 6.6s
ptest: api · no changes
ptest: web · no changes
ptest: total · passed · 6.6s · next: ptest --full before handoff
```

---

## Install

### One line (macOS and Linux)

```sh
curl -fsSL https://raw.githubusercontent.com/Avocado-Blockchain-Services/ptest/main/get.sh | sh
```

The script detects your OS and CPU, downloads the matching release bundle,
verifies its SHA-256, and installs it to `~/.local/ptest` with a `ptest`
command in `~/.local/bin`. The bundle carries every wheel it needs; nothing
else is fetched from PyPI.

Requirements:

| Need | Why | If missing |
| --- | --- | --- |
| [uv](https://docs.astral.sh/uv/) | builds ptest's private virtualenv | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| CPython 3.11–3.14 | ptest's runtime | the installer asks uv for one, and installs 3.13 with uv if none is found (the macOS system `python3` is too old) |
| `curl`, `tar` | download and unpack | preinstalled on macOS and most Linux |

If `~/.local/bin` is not on your `PATH`, add it:

```sh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc   # or ~/.bashrc
```

Supported platforms: Linux x86_64 and aarch64 (glibc 2.28+), macOS arm64
(Apple Silicon) and x86_64. Linux is what ptest is developed and verified on.
The macOS bundles are built and validated from the same sources, but have not
yet been verified end to end on real macOS hardware; please open an issue if
anything misbehaves there. Windows is not supported.

### Pin a version, upgrade, remove

```sh
# a specific version
curl -fsSL https://raw.githubusercontent.com/Avocado-Blockchain-Services/ptest/main/get.sh | PTEST_VERSION=0.3.3 sh

# upgrade: run the one-liner again (the switch is atomic)
curl -fsSL https://raw.githubusercontent.com/Avocado-Blockchain-Services/ptest/main/get.sh | sh

ptest --version
ptest uninstall --self     # remove the installation (run ptest uninstall in a repo first to clean it)
```

### Manual install (air-gapped, audited)

Download `ptest-VERSION-OS-ARCH.tar.gz` and its `.sha256` from the
[releases page](https://github.com/Avocado-Blockchain-Services/ptest/releases),
then:

```sh
sha256sum -c ptest-0.3.3-linux-x86_64.tar.gz.sha256     # macOS: shasum -a 256 -c
tar -xzf ptest-0.3.3-linux-x86_64.tar.gz
./ptest-0.3.3/install.sh                               # or: --dest /absolute/path
```

See [docs/installation.md](docs/installation.md) for the offline
`--wheelhouse/--manifest` form and workspace-local setups.

---

## Quick start

```sh
cd your-repo
ptest init          # detects runners, writes .ptest.toml, offers agent guidance
ptest               # after each edit: runs the tests your change reaches
ptest --full        # once, before you hand the change off
```

Bare `ptest` runs the tests your change reaches:
changes since the last green run (or, before the first green run, since your
branch left the default branch). No baseline or coverage step is needed.
Run `ptest --full` once before handoff.

`ptest init` looks at the repository, detects pytest/Vitest/Go/Cargo projects
(including several in one monorepo), writes a `.ptest.toml` per project, and
reports what it found:

```text
$ ptest init --dry-run
ptest init preview · your-repo

  api  pytest
  web  vitest
```

Run init again at any time; it is idempotent and never rewrites files you
edited. Commit the `.ptest.toml` files.

---

## The five commands

Always run ptest from the repository root.

| You want | Command |
| --- | --- |
| After each edit: the tests your change reaches, whole repo | `ptest` |
| The changed tests under one folder only | `ptest api/tests` |
| One file or one test — always runs it | `ptest api/tests/test_billing.py` · `ptest api/tests/test_billing.py::test_tax` |
| Every test under one folder | `ptest --full api/tests` |
| The integrated gate, once before handoff | `ptest --full` |

Any path routes to the nearest `.ptest.toml`, so every sub-project works the
same way: `ptest web/src`, `ptest --full launchpad`, `ptest server/tests/unit`.

### Examples

```sh
# edited api/app/billing.py
ptest
# ptest: api · changed since last green run (a1b2c3d, 4m ago): app/billing.py → 3 of 212 test files (1 direct · 2 via importers)

# nothing changed since the last green run
ptest
# ptest: no changes vs HEAD — nothing to test · ptest --full runs everything

# only look at one area
ptest api/tests/unit
# ptest: api · no changes under api/tests/unit — nothing to test · ptest --full api/tests/unit runs all of them

# run one test, no matter what changed
ptest api/tests/test_billing.py::test_tax

# the whole web suite
ptest --full web

# the final gate over every project
ptest --full
```

Useful flags:

| Flag | Effect |
| --- | --- |
| `-v` | ptest's scheduling/setup detail, and a verbose runner |
| `-q` | silence ptest's own lines (errors still print) |
| `--again` | with `--full`: rerun even if these exact inputs already passed |
| `--timeout 2700` | run deadline in seconds (default: from history, else estimated from the test count) |
| `--workers N` | cap parallel workers for this run |

---

## How "the tests your change reaches" works

1. **What changed.** Changes since the last green run of that project
   (its commit plus any uncommitted work); before the first green run,
   changes since the merge-base with your default branch.
2. **Noise is ignored.** Docs, build output (`build/`, `dist/`,
   `node_modules/`, caches) and non-code files outside source/test areas do
   not trigger tests.
3. **Who imports it.** For pytest, a static AST import graph maps each
   changed file to the test files that import it, directly or through other
   modules. Package `__init__.py` re-exports count, because Python executes
   them.
4. **When to widen.** Lockfiles, `conftest.py`, pytest/ptest config and
   declared `full_triggers` run the whole project's suite, as does a change
   that reaches more than 70% of the test files (`full_ratio`).
5. **Vitest** projects delegate to `vitest run --changed <base>`.

Untouched projects in a monorepo print `<project> · no changes` and run
nothing.

---

## Monorepos

The root `.ptest.toml` declares the children; each child has its own config:

```toml
# .ptest.toml (repository root)
version = 2

[monorepo]
children = ["api", "web"]
```

```toml
# api/.ptest.toml
version = 1
project_id = "291b6f5e961ca600b86c8a5f688f1a21"

[runner]
kind = "pytest"
launcher = ["uv", "run", "--locked", "--no-sync", "python"]
args = []
full_args = []
test_roots = ["tests"]          # what --full runs: pytest testpaths, or every folder holding tests
workers = 1
lifecycle = "cooperative-process-group"

[setup]                         # runs first when required paths are missing or the lockfile changed
argv = ["uv", "sync", "--locked"]
required_paths = [".venv/bin/python"]
network = true
lifecycle_scripts = true

[selection]
enabled = true                  # graph selection for bare `ptest`
full_triggers = []              # extra files that force the whole suite
non_input_outputs = [".venv"]   # outputs that never count as source changes
full_ratio = 0.7
```

`ptest init` writes these; `ptest doctor --fix` updates older configs (turns
on selection, widens `test_roots` that miss test files, adds missing output
exemptions) without touching values you tuned.

---

## Worktrees and clones

Commit the ptest files (`.ptest.toml` files, `docs/ptest-agent.md`, the
managed block in `AGENTS.md`/`CLAUDE.md`, provider skills): linked git
worktrees and fresh clones only receive committed files.

In a linked worktree whose main checkout holds a `.ptest.toml`
at the same path, ptest refuses with `config-uncommitted` (exit 2) instead
of running: stop, ask the user to commit the config on the base branch, or,
if it is already committed there, to update this branch from it, and
never run `ptest init` there. `ptest init --from-main` copies the main
checkout's config verbatim as a temporary stopgap that may go stale; the
fix is still a commit on the base branch. Delete the copied file(s) before
updating the branch. `--from-main` cannot be combined
with `--runner` or `--child`.

A run warns once on stderr when its config is not committed
(`ptest: .ptest.toml is not committed — new worktrees won't have it`), and
`ptest doctor` lists uncommitted ptest files the same way. A successful
`ptest init` ends by listing the files to commit.

---

## Reading the output

ptest narrates on stderr in `ptest:` lines; your runner's output is untouched.

| Line | Meaning |
| --- | --- |
| `changed: <path> → N of M test files` | the tests your change reaches |
| `changed → full suite: <reason>` | this run *is* the full suite (trigger file, too many affected tests, selection off) |
| `no changes … — nothing to test` | nothing to run (exit 0) |
| `waiting for N slots … in use by …` | queued behind other ptest runs; it starts by itself |
| `setup: uv sync --locked (first run)` | declared setup is running |
| `passed · N tests` / `failed · …` | the verdict |
| `next: ptest --full before handoff` | a changed-mode green; the gate is still to do |
| `incomplete (exit 70)` + reason | ptest could not prove the result; rerun once, report if it repeats |

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | passed (or nothing to test) |
| 1 | tests failed |
| 2 | usage or config error |
| 70 | incomplete: the result could not be proven |
| 75 | queue or coordinator unavailable |
| 124 | timeout (`--timeout` raises it) |
| 130 | cancelled (Ctrl-C prints only `cancelled`) |

---

## Parallel runs and setup

- **pytest + xdist**: when the project's pytest config enables xdist
  (`-n 4`, `-n auto`), ptest requests that many slots and runs the granted
  number of workers; a single slot runs serially. Without xdist, runs are
  serial.
- **Vitest** runs as one exclusive `vitest run` that manages its own workers.
- **Many runs at once**: every ptest on the machine shares one slot budget.
  Extra runs queue (`waiting for …`) instead of oversubscribing the CPU.
- **Setup** runs before the tests when its required paths are missing or its
  lockfile changed, and is skipped otherwise. `ptest init` picks it from the
  project:

  | Project has | Setup |
  | --- | --- |
  | `uv.lock` | `uv sync --locked` |
  | `pyproject.toml` / `setup.py` / `requirements*.txt`, no lock | `uv venv` + `uv pip install` of the project and its test deps (from a requirements file, an extra or dependency group naming pytest, or Poetry dev deps) into `.venv` |
  | `pnpm-lock.yaml` / `yarn.lock` / `bun.lock` / `package-lock.json` | `pnpm install --frozen-lockfile` / `yarn install --frozen-lockfile` (`--immutable` on Yarn 2+) / `bun install --frozen-lockfile` / `npm ci` |

  A bare folder of tests with no package manifest uses the `python` on your
  `PATH`.

---

## pytest plugin compatibility

Supported: pytest 8.x and 9.x, pytest-xdist 3.5 or newer 3.x (parallel runs),
pytest-cov 5 to 7 with coverage 7 (parallel coverage).

Installed pytest plugins run as normal (pytest-django, hypothesis,
pytest-asyncio, pytest-benchmark, pytest-codspeed, schemathesis, ...): they
are the project's own declared test dependencies. Your own `conftest.py` may
filter collection, clean up at session end, and observe tests (timing,
logging), including hooks re-exported from your own helper modules.

Plugins that re-run or distribute tests change what "passed" means and are
refused: pytest-rerunfailures, flaky, pytest-retry, pytest-flakefinder,
pytest-repeat, pytest-forked, pytest-parallel (and pytest-xdist outside the
parallel tier). A hook that comes from neither your checkout nor an installed
package is refused too.

A refusal names the hook and how to run without it:

```text
unqualified pytest execution hook is not owned by the serial grant (pytest_pyfunc_call from pytest_custom.plugin) · add "-p no:custom" to [runner] args in .ptest.toml to run without it
```

---

## Coding agents

```sh
ptest init --agents claude,codex      # or: all, none
```

installs `docs/ptest-agent.md` (the rules: the five commands, every output
line, every exit code, test-quality rules), a managed block in
`AGENTS.md`/`CLAUDE.md`, and a `ptest` skill per agent
(`.claude/skills`, `.agents/skills`, `.opencode/skills`, `.gemini/skills`).
When a new ptest ships newer guidance, `ptest` says
`agent guidance is outdated — run ptest init to update`.

`scripts/agent_eval.py` checks that models actually follow the guidance: it
builds a scratch monorepo with the installed guidance, asks 14 scenarios
(`evals/agent-usage/scenarios.toml`) and scores the answers.

```sh
.venv/bin/python scripts/agent_eval.py --dry-run
.venv/bin/python scripts/agent_eval.py \
  --subject 'haiku=claude -p --model haiku {prompt} --add-dir {repo} --allowedTools Read Glob Grep'
```

---

## Doctor: is the suite safe to run in parallel?

```sh
ptest doctor --offline     # static scan, sends nothing
ptest doctor --fix         # apply config updates (no prompts, no model)
ptest doctor               # asks consent, then a bounded model review
```

Doctor looks for what breaks parallel and repeatable test runs: global cache
flushes, database cleanup without ownership, fixed ports, shared fixture
mutation, live network targets, wall-clock sleeps. It writes cited findings
to `recommendations.md`. The model review uses your own Claude Code or Codex
CLI account (default models Codex `gpt-6-sol`, Claude `opus`; override with
`--review-model`), makes one initial call per checklist item plus at most one
bounded verification, and covers only the cited source. Findings are
hypotheses to verify, not a certificate. `ptest help doctor` has the details.

---

## State, privacy, uninstall

- Per-machine state (scheduler, history, review cache) lives under your user
  state directory. To keep it elsewhere, set an absolute
  `PTEST_STATE_DIR` outside any repository; use the same value everywhere you
  want shared limits.
- `ptest uninstall` removes what ptest set up in a repository (`.ptest.toml`
  files, managed guidance, this checkout's state). Files you edited are kept.
  It prints the plan first; `--dry-run` changes nothing.
- `ptest uninstall --self` removes the installation.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `ptest: command not found` | add `~/.local/bin` to `PATH` |
| `uv is required` during install | install uv, open a new shell, rerun the one-liner |
| `selection is off in .ptest.toml` | `ptest doctor --fix` |
| `--full` runs fewer tests than plain pytest | `ptest doctor --fix` widens `test_roots` |
| `waiting for N slots …` for a long time | `ptest status` shows who holds them |
| `incomplete (exit 70)` / `changed-during-run` | something wrote into the source tree during the run; the message names the path class (tracked, untracked, ignored) |
| a plugin is refused | it re-runs or distributes tests: add `-p no:<name>` to `[runner] args` |
| tests fail with `ModuleNotFoundError` for an optional dependency | ptest installs the test deps only; add the extra to `[setup] argv` (e.g. `--all-extras` or `--extra azure` for `uv sync`) |

Every command has help: `ptest help`, `ptest help run`, `ptest help init`,
`ptest doctor --help`.

---

## Developing ptest

```sh
git clone https://github.com/Avocado-Blockchain-Services/ptest && cd ptest
uv sync --locked --extra test
.venv/bin/ptest                         # the changed tests
.venv/bin/ptest tests/ng/test_impact.py
.venv/bin/ptest --full                  # ~4,400 tests, parallel, ~4 minutes
```

Releasing (maintainers):

```sh
# bump version: pyproject.toml, src/ptest/contracts.py, uv.lock, version tests
.venv/bin/python scripts/export-schemas.py --check
.venv/bin/ptest --full
git tag vX.Y.Z && git push origin main vX.Y.Z
uv build --wheel --out-dir /tmp/dist
scripts/build-release-assets.py --ptest-wheel /tmp/dist/ptest_ng-X.Y.Z-py3-none-any.whl \
    --version X.Y.Z --out /tmp/assets
gh release create vX.Y.Z /tmp/assets/* --title "ptest X.Y.Z" --notes-file notes.md
```

`get.sh` always installs the release marked *latest*.

More: [installation](docs/installation.md) · [security](docs/security.md) ·
[support matrix](docs/support-matrix.md) · [changelog](docs/changelog.md).

## License

[MIT](LICENSE)
