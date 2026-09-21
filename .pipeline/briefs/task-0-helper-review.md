# Opus narrow helper review while public-contract repair runs

Read-only, no edits, shell/tests/installs/delegation. Follow audit-spec completely:
/home/ingmar/.agents/skills/audit-spec/SKILL.md
Common contract:
/home/ingmar/worktrees/ptest/cx-ng-product/ptest/.pipeline/briefs/task-review-common.md

Cwd /home/ingmar/worktrees/ptest/cx-ng-product/task-0-helper.
Base ced109bea3d91a15c942de26247b8fa2f187b5a9, reviewed HEAD
4fe6d2701bc52c297af04860feed32bacc7e8278. Own changes ONLY tests/ng/support.py,
helper tests in tests/ng/test_files.py, .pipeline/out/task-0-helper-repair.json.
Review those complete files and relevant unchanged files.read_regular helper.
Read current design/plan helper interface (T11 owns actual CLI) and exact assignment
/home/ingmar/worktrees/ptest/cx-ng-product/ptest/.pipeline/briefs/task-0-helper-repair.md.
Do not audit the concurrently changing contracts branch or require T11 now.

Root ran scripts/ptest-bootstrap tests/ng/test_files.py tests/ng/test_contracts.py
--durations=12 in this cwd, exit0,77passed/3.33s. Full raw root evidence:
.pipeline/local/root-helper-scoped.log. Slowest2.01s timeout,1.05s retained pipe;
remaining <=0.04s. Read worker RED/GREEN logs named by its report. Legacy dispatcher
hash pinned; normal ptest/domain untouched. No final full requested at task gate.

Root hypotheses to REFUTE or confirm from bytes, not assume as findings:
1. _read_export_file uses ordinary blocking open for absolute names, bypassing
   shared no-follow/nonblocking regular-file reader; FIFO may exceed invoke's
   watchdog after process exit, and symlinks/oversize silently yield None.
2. _prefix_result_json recognizes only two value flags, stops at --shadow etc
   although these are real wrapper flags; explicit --result-json after a real
   leading wrapper option may be duplicated/read from wrong path. No second full
   CLI parser should be invented; identify smallest contract-safe resolution.
3. _drain_bounded breaks on post-exit grace/deadline or select/read error then
   returns successful Completed, potentially representing incomplete output as
   complete. Check cleanup identity/PGID scope and descriptor failures too.
4. Worker report says skill aliases unavailable, but did not show filesystem
   fallback for receiving-code-review/systematic-debugging. Treat as evidence
   limitation, not invented runtime bug; report accurately.

Check positive and negative sensitivity, BOTH stdout/stderr cap, explicit timeout,
no unbounded memory/disk/export, no orphan fixture or unrelated job signaling,
prefix-only native-tail byte preservation and exact declared result read-back.
No production second scheduler/guard or dependencies. Return required JSON schema
with real required findings only, honest coverage and reviewed_files. An empty
blocking array is valid. Root will combine this with public contracts for the
overall Task0 re-review after all required repairs.
