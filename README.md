# ptest NG

ptest is a local-first test coordinator. Normal test execution is local and
model-independent; no cloud account, model API, or remote service is required
to run tests. Run `ptest init`, then loop bare `ptest` after each edit (bare
`ptest` runs the changed tests), target one test with `ptest <scoped paths>`,
and finish with `ptest --full`. `--full` skips already-verified inputs
(`--again` forces them), and a duplicate full run joins the running full run
instead of starting a second one.

Doctor has an explicit offline static mode (`--offline`) and a separately
consented CLI review design. `ptest doctor --json` emits the versioned
review document; `ptest doctor --offline --json` emits the same document
built from static facts only. Claude and Codex are qualified
reviewers. OpenCode is not supported because its free tier refuses tool-free
runs (HTTP 403 FreeTierError). Review sends bounded source text to the
selected provider using your
existing account; provider or account costs may apply. On a TTY without an
explicit concrete --reviewer, review first asks which qualified installed
reviewer to use (one is used directly; declining shows offline output).
Choosing agents with
`ptest init --agents` installs guidance only and does not authorize model
review. See `ptest help doctor` for details; agents start at `ptest help agents`.
The installed `docs/ptest-agent.md` lists every ptest output line and exit
code with the action for each; runner internals (parallel tiers, setup,
`-v`/`-q`) are documented here and in `ptest help run`.

`ptest init` reports per-project status: each configured project shows its
runner, whether it runs, its parallel workers, and its setup, and the
footer lists only actionable next steps (not runnable, parallel off,
setup pending, smoke failed) plus one line to restart coding agents when
guidance changed. Projects that cannot run show the reason and the exact
fix instead. Re-running init is idempotent and never rewrites user-edited
guidance.

Pytest runs in parallel under ptest when the project's checked-in pytest
config enables xdist: `-n N` requests N workers, `-n auto` requests one
worker per granted slot, and `ptest --workers W` caps the request. The
scheduler grants the slots that are free; a smaller grant runs fewer
workers and a single slot runs serially with a generated `-n 0`. When
xdist cannot be verified (unsupported `--dist`, an unqualified
pytest-xdist or pytest-cov/coverage pair, or an unverifiable launcher),
ptest falls back to serial and says why. `ptest init` writes `-n 0` only
for config-level reasons; `-n 0` in `[runner] args` opts out of parallel
runs. Coverage (`--cov`) runs in parallel under xdist when the project
environment holds the qualified pytest-cov/coverage pair. Vitest
executes as one exclusive `vitest run` command through the project-local
Vitest CLI and manages its own workers; declared `[setup]` (such as
`npm ci`) runs first when its required paths are missing or the lockfile changed.

While a run executes, ptest narrates itself on stderr with short
`ptest:`-prefixed status lines: a start line, a waiting line when
admission queues, setup lines, and an end line with ptest's own verdict,
counts, and duration. Runner output is untouched. `ptest -v` adds
scheduling and setup detail and also makes pytest/vitest verbose;
`ptest -q` silences ptest's own lines while errors and refusals still
print. Machine documents (`--json`, `--result-json`) never carry status
lines. See `ptest help run`.

`ptest doctor` review makes an initial model call for each checklist item
that needs one after consent, then makes at most one bounded follow-up: an
independent verification of a valid reply, or a fresh source-only recovery
after a completed protocol, schema, or source-ID failure. Recovery reuses the
original selected units and IDs and omits the invalid draft. Provider, tool, deadline,
cancellation, and output-limit failures are not retried; an invalid follow-up
ends as unknown. There is no third call or new source collection. Timing,
selection and parallel execution items use ptest's own facts with no model
call. The
default requested models are Codex `gpt-6-sol` and Claude `opus`; an explicit
`--review-model` or `PTEST_REVIEW_MODEL` overrides that choice. The selected
model must pass the provider's normal qualification. `--review-concurrency`
(1-8, default 4) bounds parallel calls. Before the prompt, ptest prints at
most three short disclosure lines with the requested model and the maximum
initial-plus-one-follow-up call count; full disclosure is in
`ptest doctor --help`.
Reviews cite opaque IDs for automatically selected setup, representative
callers, helpers and cleanup. Findings cover only cited, reachable evidence;
an omitted decisive caller or failure path remains unknown. A review is not a
suite-wide execution certificate. `ptest doctor --offline` is static and
sends nothing. The terminal shows a compact checklist with a reason on every
unknown row.

For normal use, download a verified release archive and run `./install.sh`; see
[docs/installation.md](docs/installation.md). The installer validates bundled
wheels before atomically switching the local command. Its explicit
`--dest --wheelhouse --manifest` form remains available for offline and
enterprise installation. Security gate instructions are in [docs/security.md](docs/security.md).

To keep ptest's configuration, coordinator, history, and review-model cache in
your workspace, set `PTEST_STATE_DIR` to an absolute path outside the repository
before invoking ptest:

```sh
export PTEST_STATE_DIR=/abs/workspace/.ptest-state
```

Use the same value for every project that should share concurrency limits.
See [workspace-local setup](docs/installation.md#workspace-local-setup) for
running from a checkout without a global installation.

`ptest uninstall` reverses what ptest set up in a repository: it removes the
`.ptest.toml` files, ptest-managed guidance, the un-edited
`recommendations.md` report, and this checkout's private state (history,
setup records, scheduler rows). Files you edited are kept and reported;
symlinks and anything outside the repository root are never touched. The
plan prints first (grouped by action); a TTY is asked once, while
non-interactive runs require `--yes` and `--dry-run` changes nothing.
Skipped entries are informational and exit 0. Run uninstall with the same
PTEST_STATE_DIR used for runs, so it inspects the same machine-state
location (the plan names it as `state: ...`).
`ptest uninstall --self` also removes the local installation (only
installer-created entries; anything else inside the root is kept). See
`ptest help uninstall` for details.

## Checking agent guidance

`scripts/agent_eval.py` checks whether LLMs can use ptest from its installed
guidance: it builds a scratch monorepo in a temp dir from the current guide
plus skill bytes (the same bytes `ptest init` installs), prompts each subject
with 13 scenarios (`evals/agent-usage/scenarios.toml`), extracts the JSON
answers, and scores them. It prints a compact subject × scenario table and
exits 0 only if every subject passes every scenario. Stdlib only; `--dry-run`
prints the prompt and repo path without calling any model:

```sh
.venv/bin/python scripts/agent_eval.py --dry-run
```

Live subjects are command templates with `{prompt}`, `{prompt_file}`, and
`{repo}` placeholders (run without a shell); canned answer files can be
scored with no model at all:

```sh
.venv/bin/python scripts/agent_eval.py \
  --subject "haiku=claude -p --model haiku {prompt}" \
  --answers-file "muse=tests/ng/fixtures/agent_eval/answers-muse.json"
```
