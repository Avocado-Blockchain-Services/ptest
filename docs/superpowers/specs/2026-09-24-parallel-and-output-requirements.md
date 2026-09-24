# ptest: parallel pytest (xdist) and output redesign — requirements

Status: approved by the user on 2026-09-24 ("do all we just talked … and this one … go"). The design task turns this into
one gated design. Copy this file into the chain as `docs/superpowers/specs/2026-09-24-parallel-and-output-requirements.md`,
and record any amendments to the earlier specs there:
- `docs/superpowers/specs/2026-09-22-agent-doctor-design.md`
- `docs/superpowers/specs/2026-09-23-doctor-init-v2-requirements.md` (§A–F)
- `docs/superpowers/specs/2026-09-23-init-smoke-amendment.md`

Base: `main` at 3f399fb (doctor/init v2, already released). Real-world target, read-only for tasks:
`/home/ingmar/code/persea_content_maker_unified` (api: pytest with `addopts = "-n 4 --dist=loadgroup -m \"not
extended_migration\""`; web: vitest). The controller validates there afterwards; tasks must not modify it.

## P. Real xdist parallelism under ptest (highest priority)

Today init writes `args = ["-n", "0"]` for xdist projects and ptest runs pytest one test at a time, under a one-slot
"basic-serial" grant. The "advanced" profile in `adapters/pytest.py` and `runtime/pytest_bridge.py` is also serial:
`AdvancedPlugin` says parallel worker instrumentation awaits "a future supported xdist hook", and the bridge already reads
a `PTEST_GRANT_WORKERS` worker grant (1–64). persea's API suite is built for `-n 4 --dist=loadgroup`; under ptest it
runs about 4× slower.

1. **A parallel pytest tier:** when a project's own checked-in config activates xdist (`-n N` / `numprocesses`, including
   `-n auto`), ptest runs it with xdist, **owned** by ptest:
   - **Workers:** N comes from the project's config and is bounded by the machine scheduler's slots. `-n auto` resolves
     to the slots granted.
   - **Slots:** ptest leases that many slots for the run. When fewer are available it queues, or, if the design finds
     that simpler and deterministic, runs with fewer workers. Either way, the output says so ("4 requested, 2 granted").
   - **Distribution:** `--dist` modes the project declares (at least `load`, `loadscope`, `loadfile`, `loadgroup`, and
     `no`) keep working, and so does `xdist_group`.
2. **Ownership and containment are unchanged:**
   - **Process ownership:** xdist worker processes (execnet popen) are part of the run's owned process tree. Timeout,
     Ctrl-C, and teardown kill and reap every worker; no worker may outlive the run.
   - **Identity:** per-run and per-worker identity (`PYTEST_XDIST_WORKER` = gw0…, plus ptest's run/checkout identity)
     reaches tests, so persea's per-worker database naming keeps working.
3. **Every guarantee from the serial bridge must hold in parallel mode.** These were built and audited in rounds 9–18 of
   doctor/init v2; see the §F text in the v2 requirements and `runtime/pytest_bridge.py`:
   - the verdict is derived from the bridge's own outcome counts, never trusting pytest's returned code;
   - collected-versus-run reconciliation: every collected item must be run or reported, now across workers;
   - labelled project-filter narrowing (`full (project-filtered: …)`);
   - refused report- and outcome-rewriting hooks in full mode;
   - conftest ownership rules for collection and sessionfinish hooks.
   The bridge must observe reports on the xdist **controller** (which aggregates worker reports) and must validate the
   workers too, for example by loading the bridge in workers or refusing configurations it cannot observe. Anything
   unverifiable in parallel must fail closed, never pass silently.
4. **Fallback:** if the parallel tier cannot be qualified for a project, ptest falls back to serial exactly as today and
   says why in one plain line. The possible reasons include an unsupported xdist version, an unsupported `--dist` mode,
   and remote/rsync workers. Init writes `-n 0` only in that case.
5. **Coverage:** `pytest-cov` under xdist (a combined report) must either work under the existing coverage policy or be
   explicitly out of scope, with a clear message.
6. **Tests:** subprocess twins with real xdist (already in ptest's dev environment, or added through `uv` as a test
   dependency). They must cover:
   - parallel pass and fail;
   - a worker crash (a worker killed mid-run means the run is incomplete or failed, never passed);
   - `loadgroup` with `xdist_group`;
   - a Ctrl-C/timeout that leaves no survivors;
   - a conftest dropping items in parallel (refused or labelled);
   - verdict forgery in parallel (refused);
   - a persea-shaped fixture running with 4 workers and the correct label.

## O. Output redesign: init and doctor (plain language, terminal width, only what matters)

The user called the current output "ugly": a fixed 64-column box that wraps mid-phrase, generic "Next steps", four
"restart your agent" paragraphs, and jargon such as
`ptest: ready with caveats: serial: xdist disabled under ptest (-n 0); expected: full (project-filtered: …); setup runs when required paths or its fingerprint are missing: uv sync --locked`.

1. **Width:** use the terminal width (`shutil.get_terminal_size`, sensible min/max, e.g. 60–110). No fixed 64-column box.
   Never break inside a label, a path, or a command; wrap only at word boundaries, with hanging indents.
2. **Plain-language project facts, one short line each:**
   - **Runs:** `runs: yes` / `runs: no — <reason> → <fix>`.
   - **Parallel:** `parallel: 4 workers (xdist, --dist loadgroup)`, `parallel: no — <reason>`, or
     `parallel: inside vitest (its own workers)`.
   - **Setup:** `setup: uv sync --locked (ptest runs it when needed)`.
   - **Full suite:** `full suite = your pytest config: -m "not extended_migration", conftest.py hooks`.
   No "ready with caveats", no "expected:", no "fingerprint".
3. **Init output**, target shape:
   ```
   ptest initialized · persea_content_maker_unified

     api   pytest  runs: yes · parallel: 4 workers · setup: uv sync --locked
                   full suite = your pytest config: -m "not extended_migration", conftest.py hooks
     web   vitest  runs: yes · parallel: inside vitest · setup: npm ci

     config     unchanged  .ptest.toml, api/.ptest.toml, web/.ptest.toml
     guidance   created    docs/ptest-agent.md, ptest skill for claude, codex, opencode, gemini
                updated    AGENTS.md, CLAUDE.md

     smoke      api ✓ 2.2s   web ✓ 1.8s

   Restart your coding agents to load the new ptest skill.
   ```
   - **Next steps:** shown only when something is actionable, for example
     `web  setup pending → run: ptest web/src/i18n-node.test.ts (runs npm ci first)`, or a not-runnable fix.
     Never show generic example commands.
   - **Restart line:** exactly one, shown only when a skill was created or updated in this init.
   - **Grouping:** file actions grouped by action, never one line per unchanged file.
4. **Doctor output**, target shape per project:
   ```
   api  pytest · 5 ok · 1 gap · 5 unknown   (partial evidence)
     runs: yes · parallel: 4 workers · setup: uv sync --locked
     full suite = your pytest config: -m "not extended_migration", conftest.py hooks

     ✓ Test data factories      ✓ Fixture state isolation   ✓ Database setup reuse
     ✓ Database isolation       ✓ Cache isolation
     ✗ Deterministic time
         Template init waits with real asyncio.sleep; no injected clock.
         → Inject a sleep/clock function and use a test seam for the wait.
     ? Files and ports          no port or temp-dir allocation in the evidence
     ? Test timing              no timing history yet: run `ptest --full` once
   ```
   - **Passing rows** are compacted into columns that fit the width.
   - **Every unknown shows a short reason:** the model's one-line reason, or ptest's deterministic reason. A review
     failure shows as `? <label>  review failed: <reason>`.
   - **Dropped citations** keep the round-19 suffix.
5. **Disclosure before a model review:** at most three short lines — what is sent, to which provider and model, how many
   calls, and how to use `--offline`. Keep the full legal text in `ptest doctor --help` and the README. Always print a
   newline after the spinner before any prompt.
6. `NO_COLOR` and non-TTY output get a clean fallback. `--json` and every public schema stay unchanged unless additive.
   `recommendations.md` keeps its content, adopting the plain-language facts.

## U. Fewer unknowns in doctor

Real persea evidence: 5 of 11 api items were unknown. The model's own reasons were "no port/temp allocation evidence",
"spawn/teardown code not in the excerpts", "no global network block", "no ptest timing history", and "selection
inputs not declared".
1. **Deterministic items, with no model call, where ptest owns the facts:**
   - **TIMING-001:** answer from ptest's own run/timing history (satisfied, gap, or an honest
     `unknown: no timing history yet — run ptest --full once`).
   - **SELECTION-001:** answer from ptest's own `[selection]` config and capability (for example
     `gap: selection is disabled in api/.ptest.toml`, with the concrete fix).
2. **Evidence routing by code signals per item**, seeding each item's evidence from grep-level signals across the
   project plus the fixtures those tests use:
   - **RESOURCE-001:** `tmp_path`, `tempfile`, `mkdtemp`, `socket`, `bind(`, port constants, `/tmp`, lock files.
   - **NETWORK-001:** `httpx`, `requests`, `aiohttp`, `respx`, `responses`, `pytest-socket`, `socket.socket` patches,
     network-deny fixtures, `vcr`.
   - **PROCESS-001:** `subprocess`, `asyncio.create_subprocess`, `multiprocessing`, `Popen`, `os.fork`, and their
     teardown and join paths.
   - **The rest:** the existing seeds.
   Fixture definitions referenced by the selected tests (conftest chains) come first.
3. **Sharpened per-item prompts:** return `gap` when the evidence shows a concrete violation, and `satisfied` when it
   shows the mechanism that guarantees the property (for example a session-wide network block or per-worker port
   allocation). Otherwise return `unknown` with a one-sentence reason naming what evidence is missing. Absence of code
   is still `unknown`, never a guess.
4. **Report the unknown reason** in the terminal (O.4) and in `recommendations.md`.
5. **New checklist item "Parallel execution" (PARALLEL-001)**, added by the user on 2026-09-24. The checklist checks
   whether tests are *safe* to run in parallel, through the isolation items, but never whether they *do*. Add an item
   answered **deterministically by ptest** (no model call) from its own config, executability, and run facts:
   - **satisfied:** parallel is configured and runs in parallel under ptest, e.g.
     `✓ Parallel execution: 4 workers (xdist, --dist loadgroup)`, or `inside vitest (its own workers)` for vitest.
   - **gap, runs serially:** parallel is configured but runs serially under ptest, with the plain reason and the fix
     (the P.4 fallback reasons).
   - **gap, not configured:** no parallel runner is configured. Suggest adding `pytest-xdist` with `-n auto` (or the
     vitest pool/workers settings), but only when the parallel-safety items (fixture state, database, cache, files and
     ports, network, processes, time) show no gaps. Otherwise the fix is "resolve the parallel-safety gaps first".
   - **unknown:** only when ptest genuinely cannot tell, with the reason.
   In the doctor output, visually group the isolation items as **parallel safety**, the prerequisites of this item.
   Extend the public checklist catalog, schema, and report **additively** (a new canonical ID), following the existing
   drift guards and the item-label and prompt conventions.

## D. Constraints

- Follow `secure-by-spec` and `audit-spec`. Tests go through `ptest` only. Tasks never launch real claude/codex/opencode
  and never modify persea.
- Keep code simple: prefer deleting and merging code to adding layers.
- **Lesson from the previous run:** a design with a barrier and a wave-2 integration task must actually run the wave-2
  task. The final CLI wiring and an integration test through `cli.main` are mandatory deliverables.
- No push, no merge to main, no deploy. The controller runs the final real validation on persea, then merges, pushes, and
  reinstalls.
