# ptest init monorepo bootstrap v0.1

## Objective

Make this command work from a repository root without requiring the user to
change directories or hand-author ptest configuration:

```text
ptest init
```

The command must initialize either a single project or a monorepo, preserve
existing child authority, and offer repository-local agent guidance without
silently editing user files or installing global software.

This specification changes bootstrap behavior only. Once configuration exists,
the existing v1/v2 runtime dispatch contract remains authoritative: runtime
execution performs no discovery or glob expansion.

## User-visible behavior

`ptest init` invoked at the repository root operates there. It is idempotent:
an existing valid root or child configuration is reported as existing and is
not rewritten. `--dry-run` reports the planned files and choices without
writing. Existing child-local initialization behavior remains unchanged when
the command is invoked inside a child rather than at the root.

If the root has exactly one unambiguous native runner and no monorepo children,
init creates the existing v1 `.ptest.toml` exactly as today.

If root runner evidence is absent or ambiguous, init performs bounded bootstrap
inspection of immediate, non-symlink child directories only. A directory is a
candidate only when it contains exactly one kind of native evidence already
used by ptest's runner detection. It never recursively searches, globs,
follows symlinks, or treats arbitrary descendants as projects. All unambiguous
candidates are selected automatically; if a candidate has ambiguous evidence,
the user must provide explicit `--child PATH --runner KIND` pairs (or resolve
the ambiguity interactively).

For a selected set of two or more children, init:

1. validates every selected path and runner choice before writing anything;
2. creates missing child v1 configs using the existing fresh-config serializer;
3. preserves valid existing child configs byte-for-byte;
4. writes the root `.ptest.toml` only after all child plans validate:

   ```toml
   version = 2

   [monorepo]
   children = ["api", "web"]
   ```

5. optionally offers `ptest rules --apply` integration for the selected agent
   ecosystems.

There is no nested ptest CLI process. Bootstrap calls internal config and rules
seams directly.

## Agent guidance and skill integration

Before changing agent files, init reads existing root `AGENTS.md`, `CLAUDE.md`,
`GEMINI.md`, and equivalent supported guidance files as bounded local inputs.
Existing text remains authoritative. The canonical packaged ptest guide and its
managed reference block are added only when absent, using the existing safe
`rules --apply` semantics; conflicting or malformed managed content aborts
before any write.

After configuration planning, init asks which repository-local integrations to
enable. Supported choices are Claude, Codex, OpenCode, Gemini, or none. The
default is none in non-interactive mode. Integration writes are limited to the
repository and the provider's documented project-local skill/instruction path;
init never writes a home-directory skill, changes trust settings, changes
permissions, launches an agent, or installs packages. Existing provider files
are preserved and conflicts are reported for manual review. The first version
may implement provider integrations as managed instruction references to the
canonical guide; it must not duplicate the guide body per provider.

The prompt is skipped when `--agents none` is supplied. `--agents` accepts a
comma-separated explicit set for automation and rejects unknown providers.

## Safety contracts

### Positive contracts

- Invocation at the Git root initializes the root; invocation inside an
  uninitialized child retains the existing child-local behavior.
- Single-project initialization retains current v1 output and runner behavior.
- Monorepo initialization creates one v2 dispatcher and only missing child v1
  configs.
- Existing child configurations and existing guidance bytes are preserved.
- A failed validation produces no partial writes.
- Repeating a successful initialization produces no duplicate guidance or
  configuration changes.
- Runtime commands continue to require the already-declared root v2 manifest;
  init is the only bounded discovery phase.

### Negative contracts

- Never scan outside the Git root or recursively discover projects.
- Never follow a symlink for a candidate child, child config, or provider
  integration target. The existing in-repository `AGENTS.md -> CLAUDE.md`
  alias remains the sole documented agent-file exception.
- Reject absolute, traversal, backslash, empty, duplicate, overlapping, and
  mixed-root child paths before writing.
- Reject ambiguous runner evidence unless the user selects a runner explicitly.
- Never overwrite an existing config, guide, agent file, or provider skill.
- Never execute a runner, package manager, shell command, network request, or
  nested ptest process during init.
- Never install a skill globally or alter agent trust/permission settings.
- Never write any file when a later child, agent, or provider validation fails.

## CLI shape

Existing invocations remain valid:

```text
ptest init [--runner KIND] [--dry-run] [--reveal-command]
```

Add:

```text
ptest init [--child PATH --runner KIND]... [--agents LIST|none]
```

`--child` and `--runner` occur as pairs, each child appears once. Explicit
pairs override bounded automatic candidate selection. A single `--runner` with
no children retains single-project behavior. JSON output must describe the
root action without including repository file contents.

## Test-first acceptance criteria

Focused tests must prove:

- root invocation creates the root dispatcher while child-local initialization
  remains compatible;
- an unambiguous single runner creates the current v1 config unchanged;
- unambiguous immediate children are selected automatically when root runner
  evidence is absent or ambiguous; ambiguous child evidence is rejected without
  writes unless explicit child/runner pairs resolve it;
- explicit child/runner pairs create a root v2 manifest and missing child v1
  configs;
- existing child configs remain byte-identical;
- candidate inspection is immediate, bounded, non-symlink, and non-recursive;
- unsafe, duplicate, overlapping, undeclared, and outside-root child paths are
  rejected before writes;
- later validation failure leaves all target files unchanged;
- dry-run and `--agents none` are pure;
- agent guidance is reviewed, idempotent, and preserves existing text;
- provider integration choices are explicit, repository-local, conflict-safe,
  and never install globally;
- the current v1 init, rules, runtime dispatch, and path-hardening tests remain
  green.

All tests run through `ptest`; source changes require `graphify update .`, and
the final verification is one integrated `ptest --full`.

## Scope and implementation ownership

Expected files:

- `src/ptest/config.py`: bootstrap planning and Git-root resolution seams;
- `src/ptest/cli.py`: closed init grammar and prompt/JSON wiring;
- `src/ptest/agent_rules.py` and packaged resources: reuse or extend safe
  repository-local guidance integration;
- `tests/ng/test_config.py`, `tests/ng/test_cli.py`, and
  `tests/ng/test_agent_rules.py`: focused red/green coverage;
- documentation and changelog entries for the new command behavior.

Do not modify runner adapters, execution orchestration, Terraform handling, or
the existing v2 runtime dispatcher except where a regression test proves an
integration seam is required.
