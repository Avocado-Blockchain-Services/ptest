# v0.2 monorepo specification task

You are the Sol-high specification author. Work in this task worktree only.

Read the repository AGENTS.md, the existing v0.1 source/tests, and the user
request supplied in the parent prompt. Produce one complete, implementation-
ready design/specification document at
`docs/specs/2026-09-21-ptest-monorepo-v02.md`, then commit only that document.
Do not modify runtime code, tests, configs, agent files, dependencies, or any
other file. Do not run tests or install dependencies; this is specification
only. Do not push, merge to dev/main, publish, deploy, run Terraform, or alter
the installed CLI.

The design must freeze:

- exact root `.ptest.toml` v2 grammar and validation bounds;
- how resolution distinguishes an explicit root v2 config from an unchanged
  child v1 config;
- exact scope normalization/routing/rebasing and rejection behavior for empty,
  mixed-child, undeclared, absolute, `..`, backslash, and symlink-escape input;
- root `--full` preflight ordering, sequential child invocation without nested
  ptest processes, literal output/exit behavior, first-nonzero aggregation,
  and no discovery/globbing/Terraform runner;
- how `ptest rules --apply` and the packaged repository guidance direct agents
  to run from the monorepo root while preserving child v1 authority;
- frozen helper/function seams and disjoint owned files for implementation tasks;
- focused positive and negative tests first, all run through ptest, followed by
  the required final full suite and graphify update;
- abuse cases and negative contracts for path traversal, symlink replacement,
  malformed manifests, fan-out/resource bounds, partial preflight, output
  leakage, and child process launch.

The least-blast-radius design must avoid broad refactors and retain the
existing v1 parser/executor as the only child authority. Include acceptance
criteria, explicit non-goals, implementation sequencing, and unresolved risks.
Return a structured summary naming the document and owned files.
