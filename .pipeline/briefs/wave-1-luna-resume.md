# User-authorized temporary Luna xhigh implementation

The user explicitly said: "if muse cant go. move with luna xtra high for a while".
Muse exhausted its quota; you are Codex gpt-5.6-luna, reasoning effort xhigh.
This routing overrides ALL older Muse-only/medium/no-substitution language in
task launch files, role examples and task-local context. Use Dan's engineering
instructions explicitly, not the fixed-medium native Dan agent preset. Do not
launch Muse, Claude, other agents or other models. Root owns independent reviews.

Your launch supplies one task number N and its existing isolated worktree.
Read .pipeline/briefs/task-N-brief.md first; it is your bounded requirements.
Then read .pipeline/briefs/task-N-launch.md, wave-1-launch-common.md,
muse-dan-common.md, AGENTS.md, approved design and relevant spec/plan sections.
Follow their scope, security, tests, ownership and three-pass review requirements.
The Muse CLI reference is historical background, not instructions to invoke it.
All source/test edits use apply_patch. You are not alone; do not revert others.
No changes to shared contracts, CLI, fixtures or another task's worktree.
Commit only explicit owned paths inside your worktree. Never push, merge main/dev,
deploy, install over live ptest, alter global configuration or access real DBs.

Approved task base: 60deb4e66bb97bb6e83429c4460877419b137f0c.
Task0 final Opus verdict approved_with_notes, blocking empty; root119scoped passed.
Each task is already isolated; do not create another worktree or restart Task0.
T1 contains untracked partial Muse config.py/tests/config fixtures: inspect and
preserve this work, complete it carefully. Its prior collection ImportError was
NOT behavioral RED. Capture real baseline failures and sensitivity evidence;
report inherited work separately and do not claim it was all test-first.
T2/T3 source are clean at the base. Environments may already exist; use only your
own .venv and UV_CACHE_DIR=.pipeline/local/uv-cache. uv sync --locked if needed.

PTEST_BOOTSTRAP=/home/ingmar/.local/bin/.ptest-bundles/bundle.U1mnf3/ptest
Expected SHA256: 3cab209115a4f7e62be18996db77448288c1bec32218ba10f6d55bf4842fde68.
Run scripts/ptest-bootstrap where, then only your scoped task tests through that
bootstrap. Never raw runners or --full; root owns final integrated full testing.
T1: scripts/ptest-bootstrap tests/ng/test_config.py tests/ng/test_init.py
T2: scripts/ptest-bootstrap tests/ng/test_platform.py
T3: scripts/ptest-bootstrap tests/ng/test_history.py
Save raw logs locally and capture actual test exit, not the exit from tail/grep.
Private temporary fixture resources only; no normal coordination state writes.

Write the detailed owned .pipeline/out/task-N.json report (actual Luna identity,
base/head, tests/negative evidence/durations, inherited vs newly implemented,
three passes, implemented/verified/unverified/deferred/discovered split).
Run graphify update . (AST only), git diff --check and ownership verification.
Commit explicit owned files and return the output-schema summary only. If an
interface gap blocks implementation, report NEEDS_CONTEXT with concrete evidence
instead of inventing fields or weakening a requirement. Do not claim product done.
