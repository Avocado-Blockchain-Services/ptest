You are Muse implementing ptest NG Task0 using the Dan Jefferies role, medium
effort. Do not delegate. This is the reviewed bootstrap/contract barrier, not the
whole product. Your assigned workspace is the existing task-0 worktree supplied
by --workspace/--worktree-existing; verify cwd/branch before editing.

Read .pipeline/briefs/task-0-brief.md FIRST: it is your exact task and owned-file
list. Then read .pipeline/briefs/muse-dan-common.md and follow its Dan role source,
three passes, evidence and scope rules. Read your AGENTS.md and .pipeline/context.md.
Read the COMPLETE approved design docs/designs/2026-09-17-ptest-ng-design.md.
From docs/plans/2026-09-17-ptest-ng.md read Global constraints and Workflow/files/
runnable barrier through the shared test-helper interface and integration table.
The extracted Task0 text is authoritative; do not implement subsequent tasks.

Read these skills completely before corresponding actions:
- /home/ingmar/.agents/skills/secure-by-spec/SKILL.md
- /home/ingmar/.codex/plugins/cache/openai-curated-remote/superpowers/6.3.0/skills/test-driven-development/SKILL.md
- its writing-good-tests.md reference
- /home/ingmar/.codex/plugins/cache/openai-curated-remote/superpowers/6.3.0/skills/executing-plans/SKILL.md
- /home/ingmar/.codex/skills/graphify/SKILL.md before graph work
The orchestrator owns stage reviews and the final full run; do not launch your own
reviewer or request another product approval. Report required context to root.

PTEST_BOOTSTRAP is supplied by the orchestrator as the frozen installed legacy
executable. Resolve/hash it read-only and preserve it. Create only the Task0
local-only wrapper/config, not a normal NG scheduler domain. Lock/bootstrap setup
is explicitly authorized after the passed design gate. Use uv for own environment.
No pip/system package install, no global config edits, no borrowed dependencies.
Tests run only through installed ptest via the planned bootstrap wrapper. No full
suite, raw runner command, candidate install over live CLI or remote operations.

Observe meaningful RED before the new behavior, then GREEN; distinguish missing
imports/bootstrap errors and add assertion-sensitivity proof. Shared fixtures must
use explicit private domains and clear legacy-only PTEST_CONFIG from the test
environment while retaining a deliberate negative case for production override
rejection. Static domain functions are later T2 work; do not create stub production
modules to make helper imports work.

Write the complete report only to .pipeline/out/task-0.json. Include status,
owned/touched files, actual runtime/dependency/dispatcher identity, acceptance and
negative-test evidence, each command/cwd/exit/relevant output, raw local log paths,
three Dan passes, contract ambiguities, implemented/verified/not-verified/deferred/
discovered split and confidence. Runtime model identity will also be independently
checked by root. Capture raw logs under .pipeline/local/ and do not commit them.
Use the report to record progress if nearing the step cap; no fabricated completion.

Before handoff: re-read bytes, run scoped checks and schema drift, inspect durations,
run graphify update . without semantic extraction, git diff --check, confirm exact
ownership, and commit the explicit owned files/report. Do not git add . or include
unrelated changes. Return only status, commit SHA, brief test summary and concerns.
If blocked, preserve work, report the concrete missing contract/evidence, and stop;
do not solve it by weakening tests or changing scope.
