# ptest NG

ptest is a local-first test coordinator. Normal test execution is local and
model-independent; no cloud account, model API, or remote service is required
to run tests. Run `ptest init`, then use `ptest <scoped paths>` or
`ptest --full`.

Doctor has explicit offline static modes (`--offline`, `--json`, and `--prompt`)
and a separately consented CLI review design. Provider-backed review is
currently disabled because the Claude, Codex, and OpenCode profiles are
unqualified. All three must pass qualification before review is enabled. If
enabled, review sends bounded source text to the selected provider using your
existing account; provider or account costs may apply. Choosing agents with
`ptest init --agents` installs guidance only and does not authorize model
review. See `ptest help doctor` for details; agents start at `ptest help agents`.

For normal use, download a verified release archive and run `./install.sh`; see
[docs/installation.md](docs/installation.md). The installer validates bundled
wheels before atomically switching the local command. Its explicit
`--dest --wheelhouse --manifest` form remains available for offline and
enterprise installation. Security gate instructions are in [docs/security.md](docs/security.md).

The old implementation is retained only in Git history; see
[docs/legacy-index.md](docs/legacy-index.md).
