# Task0 helper repair2 — Muse / Dan, implementation authorized

Worktree /home/ingmar/worktrees/ptest/cx-ng-product/task-0-helper, current HEAD
4fe6d2701bc52c297af04860feed32bacc7e8278. Same muse-spark-1.3, medium effort,
no delegation/model substitution. Own ONLY tests/ng/support.py, helper tests in
tests/ng/test_files.py and .pipeline/out/task-0-helper-repair.json. Another Muse
owns public contracts in a separate tree; do not touch those files or integrate.

This is IMPLEMENTATION, not read-only. --disable-write disables non-shell writes
only. Shell apply_patch is write-enabled and required for ALL source/test/scratch
edits. Bare @@ update hunks; Add File uses +lines without @@. No cat/Python source
writes, whole-file replacement workarounds, or direct writer. Generated logs okay.

Read AGENTS.md, .pipeline/context.md, original task0 launch/brief, muse-dan-common,
approved design section4 and plan shared helper interface, your existing helper
report and real bytes. Read complete filesystem inputs (not Muse skill aliases):
/home/ingmar/.codex/agents/dan-jefferies-agent.toml
/home/ingmar/.codex/agent-memory/dan-jefferies-agent/reference-muse-code-cli.md
/home/ingmar/.agents/skills/secure-by-spec/SKILL.md
/home/ingmar/.codex/plugins/cache/openai-curated-remote/superpowers/6.3.0/skills/receiving-code-review/SKILL.md
/home/ingmar/.codex/plugins/cache/openai-curated-remote/superpowers/6.3.0/skills/systematic-debugging/SKILL.md
/home/ingmar/.codex/plugins/cache/openai-curated-remote/superpowers/6.3.0/skills/test-driven-development/SKILL.md
/home/ingmar/.codex/plugins/cache/openai-curated-remote/superpowers/6.3.0/skills/test-driven-development/writing-good-tests.md
/home/ingmar/.codex/skills/graphify/SKILL.md

Opus reviewed4fe6d27,31turns, primaryclaude-opus-5. Verdict approved_with_notes
BUT two required blocking findings remain, so root has NOT accepted the helper.
Root77passed/3.33s is in .pipeline/local/root-helper-scoped.log. Exact findings:

H-1 MEDIUM CONFIRMED: MINI_BIG_MAIN writes1.5MiB stdout FIRST; parent kills at
the stdout cap before stderr is written. Current raises(match='bound') covers
only stdout, contrary to both-stream requirement. Parameterize separate stdout
and stderr writers, assert correct named stream. Include exactly1MiB success and
cap+1 failure to pin off-by-one behavior, not just1.5MiB. Retain test-only targets.

H-2 MEDIUM CONFIRMED: _read_export_file's absolute-path branch uses blocking
open(), after the process watchdog ended. An absolute FIFO hangs indefinitely;
symlinks are followed; approved CLI accepts project-relative REL_FILE only.
Remove the special branch and route ALL reads through F.read_regular(root,name,
limit). Reject absolute paths without opening them, as shared reader does. Add
regular/symlink/FIFO negative regressions with bounded diagnostics; never hang a
test to demonstrate the bug. No new path writer or reader clone.

Closely related root acceptance corrections within the SAME owned helper:
- Respect every genuine leading wrapper option when finding --result-json. The
  frozen grammar lists value flags --fixture-domain/--base/--workers/
  --queue-timeout/--result-json and boolean flags --changed/--full/--no-setup/
  --fresh/--local/--shadow. Literal table + tiny prefix walker is sufficient, not
  a second general parser. Stop at unknown/native or --, never inspect tail.
  Prove --shadow --result-json out.json and --workers 1 --result-json out.json
  export/read-back correctly without duplicated override, while native values
  remain verbatim. Trace grammar for exact aliases, don't invent options.
- env= is an explicit fixture-test override, not inherited contamination. Purge
  control variables from inherited os.environ FIRST, then apply explicit env.
  This permits the plan's deliberate override-rejection negative tests without
  changing the helper signature or weakening production validation. Add separate
  inherited-purge and explicit-override cases; update the old conflicting test.
- Completed.result is dict|None: malformed/scalar/list exports cannot be returned
  as dict. Oversize export must raise a clear bounded incomplete-evidence failure,
  not silently look like a missing file. Preserve None for legitimately absent or
  rejected unsafe exports; document distinction and test it. Use bounded reader.
- If select/read fails, don't return normal success with missing output. Raise a
  clear fixture error with existing owned-group/FD cleanup. Fault injection is
  appropriate; no new production guard/scheduler. Preserve bounded retained-pipe
  behavior; reviewer refuted need to treat quiet grandchild retention as failure.
- Tighten timing assertions so regression to waiting whole15s fails reliably.
  Use short justified watchdogs/barriers; inspect2.01s test for practical reduction
  without flaky sub-millisecond thresholds. No unrelated process signaling.
- Remove unused module imports sys/Path in test_files (string target imports do
  not count) and duplicate inner select import if still unused.

Do NOT claim Opus's skill-absence note is true: it searched the WRONG root.
The explicit superpowers paths above exist and are readable. Read them fully and
correct your prior skill_notes honestly; preserve original attempts/limitations.
Theoretical post-reap PID reuse remains a disclosed test-helper residual; don't
invent a production identity supervisor in this repair.

Write durable behavioral regressions FIRST, run scoped scripts/ptest-bootstrap,
observe intended failures, then fix and GREEN. Use own existing venv and local
UV_CACHE_DIR. Capture FULL raw logs AND immediate $? to distinct .exit artifacts;
then inspect logs and return captured exit, no masked grep/tail status. Keep all
prior evidence, report exact paths/commands/cwd/exits and per-finding dispositions.
Root's audit raw/structured traces live in chain; findings above are embedded
because Muse shell cannot read that origin directory. No full/raw runners/global
config/live ptest install/main merge/push. Frozen PTEST_BOOTSTRAP supplied.

Re-read all owned bytes, all three Dan passes, durations, graphify update . AST-
only, git diff --check; commit explicit owned files and report SHA/evidence/issues.
Root will combine and run focused Opus final Task0 gate; do not launch auditors.
