# One combined design / implementation specification

Act as the Sol-high specification worker. Write ONLY
docs/superpowers/specs/2026-09-22-agent-doctor-design.md with apply_patch. Return
the requested JSON summary. No code/tests/install/commits/children. No pipeline
history, graph query, git log/blame, or memory reads. Read ONLY this task's
context.json, provider-evidence.md, progress.md and relevant current source as
needed (maximum six focused additional reads). Do not load .pipeline/context.md.
Never push, merge main/dev, deploy, cloud ops, Terraform, live migrations, or
replace global CLI. Tests later must exclusively use ptest. Existing linked
worktree baseline c8cccf9 is the correct base; main lacks existing features.

Write one implementable spec containing decisions, numbered acceptance criteria
and negative twins, helper signatures/public shape, literal CLI/help/shared
registration edits, exact owned files per ordered task and test commands. No
separate plan. Status: awaiting user written-spec approval; do not imply approved.
Use concise tables/contract sketches, ~2500-4000 words maximum, avoid speculative
frameworks. Call out any capability qualification blocking implementation honestly.

## Actual user requirements

Default `ptest doctor` launches installed Claude, Codex or OpenCode CLI to audit
tests per configured project using canonical checklist, rather than merely emitting
static regex hypotheses. `ptest init` offers the SAME in-process diagnostic after
normal init, including existing config. Do not conflate choosing guidance agents
with consent to paid/cloud-backed review. First-use prompt and explicit automation
flags must disclose cost/account/source-sharing. No silent cloud launch in CI.
Normal test execution remains offline/model-independent. No nested ptest CLI.

Output per-child capability table FIRST, then checklist satisfied/gap/unknown/N/A,
then findings and specific improvement guidance. Percentage = satisfied/applicable,
unknown counts against numerator, N/A must be justified. Zero applicable => no
score. Explicitly not coverage, performance prediction, safety, or proven runtime.
Critical actual blockers dominate headline irrespective of percentage. Say e.g.
'80% of applicable checklist satisfied by review; see recommendations.md for the
remaining items.' 'ptest will work great' must never overclaim execution success.
Disabled selection means optimization disabled (not ptest unusable). Keep distinct
declared execution capability, observed dependency prerequisites, agent-reviewed
quality, and execution-verified behavior. Existing pytest is serial-only: xdist
installation alone does not imply parallel support. Implementing worker
parallelism or monorepo root probes is outside scope, not hidden by readiness UI.

Write root recommendations.md. Every improvement has exact file evidence+reason,
specific change, applicable canonical recipe, preserved assertions/coverage/inventory,
failing regression before repair, exact root-based ptest argv + cwd + prerequisites
+ expected outcomes, measurable completion criteria, required record of actual
command/cwd/exit/output, final ptest --full, unresolved stays unverified. Include
cross-worker/cross-run sentinel proof for DB/Redis/file isolation, not just a naming
convention. If current ptest cannot execute needed parallel proof, mark unsupported
and describe test-harness proof through supported ptest, not nonexistent flags.
Advice is not repair authority. Fixed footer asks user's LLM to read, verify and
implement recommendations; reports distinguish proposed verification from observed.

Slow work shows attached phase, elapsed time, provider/project. TTY spinner vs
readable nonTTY updates on stderr. No fake progress percentages/background daemon.
Cancel/timeout/output exhaustion terminate/reap ONLY owned process group, preserve
partial state as incomplete not success. Explicit bounded sequential child fanout.

Init needs real attractive ptest wordmark, version, repository name, GitHub URL
https://github.com/Avocado-Blockchain-Services/ptest plus exact created/updated/
unchanged/conflicted paths (already current init behavior to preserve), plain and
NO_COLOR support, terminal-escaped bounded names, no banner in JSON.

Deps: distinguish project manifests/locks and actual local env metadata, never
ptest private env. No installation, imports executing project code, tests or
repairs during audit. Missing/unsupported/uninspectable clearly distinct. Respect
uv/npm etc; dependency advice must preserve authoritative lockfiles. Arbitrary
launcher commands cannot be run as 'inspection'; unresolved => unknown.

## Architecture / negative contracts

Reuse existing canonical checklist, scan admission boundaries, root/child routing,
typed errors, pure rendering and safe-write patterns. Do not make regex matches
certificates, code absence N/A, or an agent response trusted executable input.
Choose bounded sanitized evidence packets rather than unrestricted agent workspace
access. No tools/read/write/shell/network browsing/MCP by audit agent; selected
provider model request is the disclosed network use. CLI normal auth is allowed;
don't access/print auth files yourself. No custom repository-provided argv/models/
hooks or instruction execution. Validate provider capabilities before sharing data;
all3 adapters mandatory qualification gate, don't claim flags alone guarantee it.
Bound source selection and prompt/output; exclude .git, .pipeline, graph outputs,
agent instruction/config files, private/generated paths, symlinks and secrets;
include coverage limitations. Not a perfect secret detector: disclose source-sharing.
No automatic source upload on a repeated init or CLI presence alone.

Strict versioned assessment JSON: canonical IDs once per declared child; unknown
IDs/duplicates/missing rows rejected, evidence must refer to collected root-relative
paths/line bounds/content identities. Model can't supply score or execution proof.
Sanitize terminal and Markdown control/link/HTML injection; advice cannot become
arbitrary shell commands. Deterministic command templates from validated scopes.
Preserve doctor --json legacy static schema and --prompt offline semantics; new
agent JSON opt-in needs explicit versioned contract. Existing --probe remains
separate explicit live authority. Choose concrete flag matrix with incompatible
modes rejected before any launch. init JSON contract must remain coherent.

Reports: do not overwrite custom/user-edited recommendations.md. Atomic nofollow
publish with ownership/content match and race protection; conflict returns actionable
status without clobber. Choose one simple repeat-run ownership strategy, not complex
state framework. Concurrent audits and failures must preserve prior complete report.
Handle interruption during write and changes to source between scan/result.

## Workflow and verification

Use $single-repo-feature: Sol-medium public contract barrier next, then Muse coding
with /home/ingmar/.agents/imported/dan-jefferies-agent.md, 3-pass actual observations,
criterion file:line evidence and raw ptest outputs. No old agent memories. Separate
task worktrees only for parallel workers; sequential file owners minimize overhead.
Focused tests first (observed RED), then GREEN, scoped audits, integrated final full
suite once after all source changes, graphify update . AST-only, secrets/SAST/deps
gates already in repo if applicable. Spec-only: no tests/install/graph refresh.
Smoke on COPIES of several repos under /home/ingmar/code (at least standalone pytest,
standalone Vitest and v2 monorepo, can use relevant child snapshots), excluding .git,
secrets/env/deps/generated and preserving originals byte-identically. Install built
artifact in task-local prefix/env, NOT global. Real each-provider smoke plus mocked
deterministic negative tests; record account-unavailable provider as unverified, not
complete. End-to-end init offer, repeatedinit, doctor, table, report self-verification,
JSON, no-provider and cancellation. No real tests/services launched in audit smoke.

Do not run tests for this spec. Do not split spec/plan or add release/global-install
tasks from completed old requests. Final handoff is local worktree only.
