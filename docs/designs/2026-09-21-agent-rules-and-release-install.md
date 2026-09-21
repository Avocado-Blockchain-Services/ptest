# Agent rules and release-install design

## Goal

Make ptest easy to adopt in a repository without silently changing user-owned
agent configuration, and make a published ptest release installable without
requiring users to construct a wheelhouse by hand.

## Repository rules

`ptest rules` is read-only by default and prints a bounded preview. `ptest rules
--apply` is the sole mutation path. It writes one canonical repository-local
guide at `docs/ptest-agent.md` and adds an idempotent, delimited reference to
each existing root `AGENTS.md`, `CLAUDE.md`, and `GEMINI.md`. If none exists, it
creates only `AGENTS.md`; it never creates provider settings, changes trust or
permissions, or invokes a coding agent.

The canonical guide is packaged static content. It requires ptest for test
execution, scoped TDD followed by one full gate, factories, bounded test
duration, run/worker-owned databases and cache namespaces, and deterministic
file, port, process, time, and network boundaries. It explicitly says that
Doctor evidence is a hypothesis and repair must preserve test semantics,
assertions, and coverage.

The command rejects symlinks, non-regular files, an existing guide with
different content, malformed managed delimiters, and destinations outside the
resolved repository root. It validates every target before writing anything;
repeating a successful apply produces no duplicate block. A preview exposes
only target paths and stable action descriptions, not repository contents.

## Release installation

The existing Python installer remains the verification and atomic-cutover
primitive. `install.sh` gains a convenience mode for a release archive that
contains `wheelhouse/manifest.json` and its two verified wheels. In that mode a
user runs `./install.sh` or `./install.sh --dest /absolute/path`; the destination
defaults to `~/.local/ptest`. Explicit `--wheelhouse` and `--manifest` retain
the existing offline/enterprise interface and must still be paired.

The convenience path never downloads or executes a remote script. The release
publisher creates the archive locally with `scripts/build-release-bundle.py`,
which builds/copies only the expected ptest-ng and pinned psutil wheels and
writes the versioned manifest. The archive's installer validates hashes before
creating a venv; failed validation or installation leaves the published ptest
symlink unchanged.

## Acceptance and negative contracts

- A default rules preview writes no files; `--apply` never overwrites an
  existing guide or agent file and is idempotent after its own managed block.
- Symlink, malformed-block, and non-regular target cases make no writes.
- The generated guide contains the local/parallel safety rules and provider
  references use each tool's documented import form.
- A source checkout without bundled release artifacts fails with an actionable
  instruction, never falls back to a network request.
- A release bundle installs with only `./install.sh`, validates the exact two
  wheels, and keeps an existing installation on failure.
