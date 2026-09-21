# ptest NG

ptest is a local-first test coordinator. It requires no cloud account, model
API, or remote service. Run `ptest init`, then use `ptest <scoped paths>` or
`ptest --full`.

To install into an explicitly owned temporary destination, see
[docs/installation.md](docs/installation.md). The installer accepts only the
explicit `--dest`, `--wheelhouse`, and `--manifest` arguments and is offline by
default. Security gate instructions are in [docs/security.md](docs/security.md).

The old implementation is retained only in Git history; see
[docs/legacy-index.md](docs/legacy-index.md).
