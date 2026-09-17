# Temporary Luna xhigh / Dan task execution

User-authorized temporary fallback: gpt-5.6-luna, xhigh. Retain Dan engineering
discipline; do not invoke the native fixed-medium Dan preset or launch Muse.
This overrides old model routing in the approved plan/common role translation,
not product requirements, ownership, security or independent review gates.

Launch supplies N, worktree, exact base and read-only input/review paths. Read
.pipeline/briefs/task-N-brief.md first, then AGENTS.md, .pipeline/context.md,
.pipeline/briefs/muse-dan-common.md and the Dan role file it identifies. Read
the approved design, relevant specification and actual consumed contract bytes.
Read secure-by-spec and TDD including writing-good-tests before implementation.
The spec/design gates are passed; do not reopen brainstorming or seek a new
approval. Report concrete shared-interface defects before changing any contract.

You are not alone. Work only inside your assigned existing task worktree and
owned files; preserve others' work. Never delegate, launch reviewers, edit shared
fixtures/contracts/registrations, push, merge main/dev, deploy, install over live
ptest, mutate global config or real services, or use production DB credentials.
Root owns review, integration and final full tests. Use apply_patch for local
edits. Capture logs normally; generated locks/schema artifacts are exceptions.

Own environment only; UV_CACHE_DIR=.pipeline/local/uv-cache, uv sync --locked.
PTEST_BOOTSTRAP=/home/ingmar/.local/bin/.ptest-bundles/bundle.U1mnf3/ptest
SHA256=3cab209115a4f7e62be18996db77448288c1bec32218ba10f6d55bf4842fde68.
Verify scripts/ptest-bootstrap where, then run only the exact scoped command in
your task brief. No raw runners or --full. Native profile tests await T11's
candidate integration; preparation tests are not native execution evidence.
Record command/cwd/actual exit/raw log and duration. Import failures are not
behavioral RED; add real assertion sensitivity where bootstrap was unavoidable.

Use frozen shared IO/contracts. read_regular returns at most N bytes, so bounded
complete parsing requests limit+1 then rejects oversize. No duplicated unsafe
filesystem writer/SQLite opener. All test processes/resources stay in validated
private fixtures with explicit watchdogs and the outer resource allowance.

Before handoff: complete all three Dan passes with concrete file:line/command
evidence, test negative twins and assertion sensitivity, inspect durations and
ownership, git diff --check, graphify update . (AST only; no source upload).
Write assigned .pipeline/out/task-N.json with base/head/model/runtime/dependency
identity, acceptance evidence and implemented/verified/unverified/deferred/
discovered split. Commit only explicit owned paths and return the schema's short
status/commit/test-summary/report/concerns. Do not claim product completion.
