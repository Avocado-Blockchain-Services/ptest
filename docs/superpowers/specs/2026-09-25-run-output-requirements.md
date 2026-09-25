# ptest run output: say what is happening, and -v for more — requirements

Status: requested by the user on 2026-09-25. `ptest api/tests` in persea printed nothing at all for a long time: the run was
waiting for a scheduler slot (another ptest run held the slots) and ptest printed no status of its own. Runner output
(pytest/vitest) already streams live once the runner starts; the silence is everything ptest does before and around it.

## Default output (no flag)
ptest prints its own short status lines on **stderr**, each prefixed `ptest:`, plain words, terminal width aware
(reuse the existing render/terminal helpers). Runner stdout/stderr stay untouched.
1. **Start:** one line naming the project, runner, workers and scope, e.g.
   `ptest: api · pytest · 4 workers · api/tests` or `ptest: api · pytest · full suite` (monorepo: per child).
2. **Waiting for a slot:** if the run is not granted within about 1 s, say what the run needs vs what is free:
   `ptest: waiting for 4 slots (2 of 4 free) — in use by ptest (pid 13178) · queue timeout 10m`.
   Repeat about every 15 s as `ptest: still waiting for 4 slots (2 of 4 free) · 16.0s` (a TTY may update one line in place;
   non-TTY/NO_COLOR prints a new line at most every 15 s, never a spinner stream). Name what holds the slots when the
   scheduler knows it (project dir name, pid) — never secrets or full argv.
3. **Setup:** before ptest runs setup: `ptest: setup: uv sync --locked (first run | inputs changed)`; after:
   `ptest: setup done (4.2s)` or the failure with its exit code.
4. **End:** one line with the verdict ptest itself derived, counts when the bridge has them, and duration:
   `ptest: passed · 812 tests · 3m12s` / `ptest: failed · 3 failed, 809 passed · 3m15s (exit 1)`; refusals/timeouts keep their
   existing `code: message` line. Monorepo `--full`: one end line per child plus a final one-line total.
5. **Hint:** mention verbosity where it helps, once per run: on the waiting line and on a failure/refusal end line
   (`… run with ptest -v for scheduling and setup details`). Never on a clean pass.

## `-v` / `--verbose` and `-q` / `--quiet`
- `-v`/`--verbose` in ptest's option position (before the scope / runner tail) is a ptest option: ptest adds detail lines
  (admission and grant, slots requested/granted, queue position, setup fingerprint reason and the exact setup argv, the
  runner argv ptest launches, per-phase timings, the selection/scope decision), AND forwards `-v` to the runner so pytest or
  vitest is verbose too. Today `ptest -v …` is parsed as a runner tail; changing it is intended — record it in help.
- `-q`/`--quiet` suppresses ptest's own status lines (errors/refusals still print). Runner output unchanged.
- `-v`/`-q` placed after the scope, or after `--`, remain runner data (e.g. `ptest api/tests -v` = pytest verbose only).
- `--json` / `--result-json` / public documents are unchanged; status lines never go to stdout.

## Constraints
- Keep it simple: one small status/progress helper used by operations/cli, not a framework. Prefer extending the
  existing render helpers.
- Update `ptest --help`/`ptest help run`, README, and the shipped agent guide + skill only where the grammar is described
  (register the new guide/skill hashes in `agent_rules._PREVIOUS_GUIDE_SHA256S` if their bytes change — the old bytes must
  stay recognized as ptest-managed).
- Tests (strict TDD, through ptest only): start/end lines for scoped, full and monorepo full; queued run prints the waiting
  line (fake scheduler that grants after a delay) and a TTY vs non-TTY rate limit; setup lines; failure end line with hint;
  `-v` detail lines + forwarded `-v`; `-q` silences status lines; tail `-v` after scope stays runner-only; stdout unchanged
  in `--json` modes; hostile project names escaped.

## Design notes (implementation, cc-run-output branch)

- One small helper, `src/ptest/progress.py` (~200 lines): pure `format_*`
  builders plus a single `emit()` to stderr. No framework, no color, no
  in-place updates. Existing helpers reused: `render.terminal_text`
  (bounds + escapes every dynamic field, including hostile project names)
  and `project_facts.terminal_width`.
- Width-awareness applies to unbounded caller-controlled segments only
  (scope text, argv). Mandated segments (verdict, counts, holders, hint)
  are never cut: cutting them at 80 columns would drop exactly the content
  the spec mandates. Holder labels are individually bounded by
  `terminal_text`.
- TTY and non-TTY behave identically (newline per emission, throttled to
  first-after-1s then every 15 s). The spec permits in-place updates on a
  TTY ("may") but does not require them; uniform newlines satisfy "never a
  spinner stream" everywhere, and `test_status_lines_are_identical_on_tty_and_non_tty`
  pins the guarantee.
- Verbosity travels on `C.RunRequest.verbose/quiet` (additive contract
  fields, defaults false; not part of the exported JSON schemas, so
  `export-schemas --check` is unaffected). `-v`/`-q` parse as ptest
  options only in option position; after the first native token or `--`
  they stay runner data. `-v` + `-q` combine as quiet ptest + verbose
  runner instead of erroring.
- `-v` is forwarded by appending to the private effective config only
  (pytest/vitest kinds); the committed config is untouched so the
  changed-during-run revalidation still compares clean sources, and
  literal command argv is never rewritten.
- Holders come from a new read-only `scheduler.queue_holders()` (run id,
  owner pid, checkout id — no argv, no secrets). The project dir name is
  resolved best-effort from `/proc/<pid>/cwd` on Linux with a pid-only
  fallback; "when the scheduler knows it" degrades gracefully.
- The hint fires once per `cli.main` execution dispatch (shared flag in
  `progress`): first waiting line wins, else the failure/refusal end line.
  Refusal `code: message` lines are byte-identical apart from the appended
  hint, and machine (`--json`) documents never carry it.
- Out of scope, deliberately: shadow/probe paths emit no status lines
  (their flows predate this spec and have their own output), and the
  `NO_TESTS_NEEDED` early return stays silent (nothing ran).
- Docs: `ptest help run`, README, and the shipped repository agent guide
  (kept within its 45-line cap by reflow; base hash `a9d5171` registered
  in `_PREVIOUS_GUIDE_SHA256S`). The per-provider skill template carries
  no run grammar (it only points at the guide), so it is unchanged.
- Durable tests: `tests/ng/test_run_output.py` (28 tests) through the
  `case.invoke` subprocess seam plus `parse_argv` checks; no fakes of
  ptest modules (the queue test holds a real scheduler slot and releases
  it from a timer). Two pre-existing stderr assertions in
  `test_operations.py` were updated to the new contract (runner bytes
  intact, ptest adds only `ptest:`-prefixed lines) and one
  `operations.execute` stub in `test_cli.py` gained the `counts` field
  every real `RunResult` carries.
