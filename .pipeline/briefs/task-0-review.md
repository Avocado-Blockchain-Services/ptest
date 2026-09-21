Review completed Task0 only, in the task-0 worktree provided as cwd.
Read the common review contract at
/home/ingmar/worktrees/ptest/cx-ng-product/ptest/.pipeline/briefs/task-review-common.md
and audit-spec completely. Read the extracted task-0 brief, approved current
design and .pipeline/out/task-0.json. Root supplies a review package covering
recorded base042dde1 through the submitted task HEAD and root check evidence.
Do not infer a task HEAD before the launcher supplies the exact value.

This is the shared runnable barrier. In particular verify every frozen type and
enum/default, public allowlisted schema vs private argv-bearing protocol, strict
decode boundaries and generated-schema parity, finite execution tiers and memory
units, fixtures' explicit domain routing, no accidental import of future modules,
and no production scheduler/CLI stub used to fake runnable state.

Trace file primitives for descriptor-relative paths, symlinks/FIFOs/hardlinks,
wrong modes/owners, directory swaps, no-overwrite, cleanup and durability. Check
the distinct shared-parent vs exclusive-private-child rules and static no-create
semantics. Check storage size/identity/corruption/read-only/pragmas without
introducing store schemas prematurely. Read negative test assertions and raw
logs: RED must mean the intended assertion, not only import failure.

Verify the bootstrap truly routes the installed frozen legacy ptest locally at
one worker, refuses full, preserves exit/output, scopes the correct worktree,
and cannot accidentally call a future PATH candidate. No direct raw runner test
commands are allowed. Initial uv lock generation and temporary local environment
setup were authorized after the design gate. Existing root legacy launcher and
cloud code are intentionally retained until T13, not a task0 cleanup failure.

Do not claim downstream integration or macOS runtime proof from fixture mocks.
Do not request a full suite at this task gate. Return exact JSON review schema.
