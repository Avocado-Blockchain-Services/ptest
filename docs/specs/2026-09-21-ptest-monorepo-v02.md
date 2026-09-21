# ptest v0.2 monorepo-root support

## Objective

Add explicit monorepo-root dispatch while preserving all existing v1 behavior. A root `.ptest.toml` with `version = 2` is a dispatcher manifest only. Every declared child retains an immediate, unchanged, authoritative v1 `.ptest.toml` and executes through the existing `operations.execute(domain, config, RunRequest)` path.

Use one new dispatcher module plus minimal changes to config and CLI code. Do not broadly refactor parsing, execution, runners, or agent-rule application.

## Manifest and path grammar

The only valid v2 shape is:

```toml
version = 2

[monorepo]
children = ["api", "web"]
```

Parsing remains strict:

- `version` must be integer `2`.
- The only top-level keys are `version` and `monorepo`.
- The only `[monorepo]` key is `children`.
- `children` must be an array of 1 through 256 strings.
- A v2 document is never converted into the existing v1 `Config`.
- Existing strict v1 parsing, models, errors, and resolution remain unchanged.

A child declaration has grammar `SEGMENT ("/" SEGMENT)*`. A segment is nonempty and is not `.` or `..`. The complete string must not start or end with `/`, contain `//`, `\`, NUL, an absolute path, or a platform drive/root prefix. Compare normalized POSIX component tuples, not string prefixes.

Declarations must be unique. They must also be non-overlapping: no declared child may equal, contain, or be an ancestor of another declared child.

For root-scoped execution, each scope has grammar `CHILD "/" RELATIVE`, where `CHILD` exactly matches one declared child and `RELATIVE` follows the same segment grammar with at least one segment. The child prefix is removed exactly once before constructing the child request. Thus `api/tests/unit` becomes `tests/unit` for `api`.

Reject a missing or empty scope, a scope equal only to a child, absolute paths, `.` or `..` segments, backslashes, empty segments, undeclared prefixes, and any request whose scopes select more than one child. Validation occurs before calling any execution seam.

## Filesystem validation

Resolve children relative to the directory containing the root v2 manifest; never use discovery or globbing.

For every declared child:

1. Walk each declared path component with non-following metadata checks.
2. Require every component, including the final child, to be a real directory and not a symlink.
3. Confirm the resolved directory remains beneath the monorepo root.
4. Require an immediate `<child>/.ptest.toml`; it must be a regular, non-symlink file.
5. Parse that file through the existing strict v1 parser and require `version = 1`.

Do not search parents or descendants for a substitute config. Validation failure identifies the declaration and prevents the relevant launch. Root `--full` must validate every child before launching any child.

## Design and helper seams

Add `src/ptest/monorepo.py` with small, independently testable seams equivalent to:

- `parse_monorepo_manifest(raw, source_path) -> MonorepoManifest`
- `preflight_children(root_dir, manifest) -> tuple[ChildTarget, ...]`
- `route_scopes(scopes, children) -> RoutedChildRequest`
- `execute_full(children, execute_child) -> int`

`ChildTarget` contains the declaration, validated directory, parsed v1 `Config`, and existing execution domain. `RoutedChildRequest` contains exactly one target and rebased scopes. Data objects should be immutable where practical.

Extend `ConfigResolution` with an explicit discriminated monorepo result, or an equivalent separate result type, so callers must distinguish v1 executable configuration from a v2 dispatcher manifest. Do not weaken `_parse_config` for v1 or populate fake v1 fields for v2.

In `cli.py`, keep argument parsing and static-command behavior stable. After resolving configuration:

- A v1 result follows the current path unchanged.
- A v2 result enters monorepo dispatch.
- Normal execution validates and routes all scopes to one child, rebuilds the existing `RunRequest` with only rebased scopes, and calls `operations.execute` once with that child’s existing domain and v1 config.
- Root execution without scopes fails unless it is `--full`.

## Root full-gate behavior

For `ptest --full` at a v2 root:

1. Preflight all declared children in manifest order before starting execution.
2. Invoke each child’s existing full gate directly through `operations.execute`; construct the same full `RunRequest` that a child-local full invocation would use.
3. Run children sequentially in declaration order.
4. Pass through the process stdout and stderr streams without capturing, rewriting, grouping, or suppressing child output.
5. Run every child even after a failure.
6. Record the first nonzero exit status and return it only after all children finish; return zero if all succeed.

Do not spawn nested `ptest` CLI processes. Do not add or select a Terraform-specific runner. Child v1 configuration remains solely responsible for its execution domain and gate behavior.

## Agent guidance

Update `repository-agent-guide.md` so `rules --apply` installs guidance stating:

- Run `ptest` from the monorepo root.
- Prefix focused scopes with the declared child, for example `ptest api/tests/ng/test_x.py`.
- Use root `ptest --full` for the integrated gate.
- Do not bypass, copy, merge, or rewrite child `.ptest.toml` files; each child v1 config remains authoritative.
- Do not run raw test runners or `cd` into a child to bypass root dispatch.

Preserve existing preview/apply semantics and user-file protections. Change `agent_rules.py` only if required to render the revised packaged guidance; otherwise leave its implementation untouched.

## Negative security contracts

The implementation must never:

- Discover undeclared repositories or expand globs.
- Accept traversal, absolute, backslash-based, ambiguous, or mixed-child scopes.
- follow a symlink in a child path or accept a symlinked child config.
- Route by naive string prefix (`api2` must not match `api`).
- Launch any child during root full preflight.
- Partially execute a normal request before all its scopes are validated.
- Mutate root or child configuration, user files, scopes, or command arguments.
- Capture or normalize child output or replace its exit status.
- Require model APIs, cloud services, a specific TUI, nested CLI execution, or Terraform.

Filesystem mutation concurrent with an already-running command is outside the guarantee; validation must nevertheless be non-following and containment-aware at each preflight.

## Acceptance criteria

Focused tests demonstrate:

- Exact valid v2 parsing and rejection of unknown keys, wrong types, zero or more than 256 children, duplicates, overlaps, and every unsafe path form.
- Existing valid and invalid v1 cases behave unchanged.
- Child preflight accepts only immediate strict v1 configs in safe non-symlink directories.
- One or multiple same-child scopes are rebased exactly and execute once.
- Empty, child-only, mixed-child, undeclared, absolute, traversal, backslash, malformed-segment, and symlink-escape scopes produce no execution call.
- `--full` preflights all children before execution, executes sequentially in declaration order, continues after failures, preserves stream objects, and returns the first nonzero status.
- Preflight failure causes zero child executions.
- Dispatch directly calls the existing operation seam; no subprocess or Terraform path is used.
- v1 CLI execution and static dispatch regressions remain green.
- Rules preview/apply contains the root-command guidance, preserves child authority, and retains existing file-safety behavior.

## Task ownership and implementation order

This task owns only the files listed below. Do not modify unrelated runners, operations, domain implementations, or historical pipeline artifacts.

1. Add failing focused tests first in the three existing NG test files; add no broad snapshot suite.
2. Implement the standalone manifest, path-validation, routing, and full-loop seams in `monorepo.py`.
3. Add strict v2 resolution to `config.py` without changing v1 contracts.
4. Integrate the discriminated result into `cli.py` and direct execution.
5. Update packaged repository-agent guidance and its focused tests; touch `agent_rules.py` only if necessary.
6. During implementation, run scoped tests only through `ptest`.
7. After source changes, run `graphify update .` in the changed checkout.
8. Finish with the integrated `ptest --full` gate and record negative/regression evidence.

This specification-only task itself performs no tests, dependency installation, graph update, or file changes.

## Non-goals

No repository discovery, glob support, nested monorepos, cross-child scope execution, parallel full execution, merged config inheritance, v1 schema changes, child config rewriting, new runner abstraction, Terraform integration, output aggregation, cloud coordination, or general CLI/config refactor.
