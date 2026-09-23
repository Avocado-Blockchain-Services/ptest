# ptest NG

ptest is a local-first test coordinator. Normal test execution is local and
model-independent; no cloud account, model API, or remote service is required
to run tests. Run `ptest init`, then use `ptest <scoped paths>` or
`ptest --full`.

Doctor has explicit offline static modes (`--offline`, `--json`, and `--prompt`)
and a separately consented CLI review design. Claude and Codex are qualified
reviewers. OpenCode is not supported because its free tier refuses tool-free
runs (HTTP 403 FreeTierError). Review sends bounded source text to the
selected provider using your
existing account; provider or account costs may apply. On a TTY without an
explicit concrete --reviewer, review first asks which qualified installed
reviewer to use (one is used directly; declining shows offline output).
Choosing agents with
`ptest init --agents` installs guidance only and does not authorize model
review. See `ptest help doctor` for details; agents start at `ptest help agents`.

`ptest init` reports per-project status: each configured project shows its
runner and an execution verdict, and Next steps lists only commands the
executability check verified for this repository. Projects that cannot run
show the reason and the exact fix instead. Re-running init is idempotent
and never rewrites user-edited guidance.

Pytest runs serially under ptest. When a project's pytest configuration
enables xdist, ptest adds `-n 0` to the runner args; never add `-n`,
`--dist`, or other parallel controls to ptest args. Vitest executes as one
exclusive `vitest run` command through the project-local Vitest CLI and
manages its own workers; declared `[setup]` (such as `npm ci`) runs first
when its required paths are missing.

`ptest doctor` review sends one cheap-model call per checklist item after
consent. The model is resolved as `--review-model`, then
`PTEST_REVIEW_MODEL`, then a cached pick, then the provider default;
`--review-concurrency` (1-8, default 4) bounds parallel calls.
`ptest doctor --offline` is static and sends nothing. Model citations live
in `recommendations.md`; the terminal shows only the verdict per item.

For normal use, download a verified release archive and run `./install.sh`; see
[docs/installation.md](docs/installation.md). The installer validates bundled
wheels before atomically switching the local command. Its explicit
`--dest --wheelhouse --manifest` form remains available for offline and
enterprise installation. Security gate instructions are in [docs/security.md](docs/security.md).

The old implementation is retained only in Git history; see
[docs/legacy-index.md](docs/legacy-index.md).
