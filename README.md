# ptest NG

ptest is a local-first test coordinator. It requires no cloud account, model
API, or remote service. Run `ptest init`, then use `ptest <scoped paths>` or
`ptest --full`.

For normal use, download a verified release archive and run `./install.sh`; see
[docs/installation.md](docs/installation.md). The installer validates bundled
wheels before atomically switching the local command. Its explicit
`--dest --wheelhouse --manifest` form remains available for offline and
enterprise installation. Security gate instructions are in [docs/security.md](docs/security.md).

The old implementation is retained only in Git history; see
[docs/legacy-index.md](docs/legacy-index.md).
