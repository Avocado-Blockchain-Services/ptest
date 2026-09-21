# ptest NG Doctor Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans, secure-by-spec, and the repository's test-first workflow. Implementation begins only after the orchestrator's Claude Opus High plan gate. Every test runs through `ptest`; never invoke a raw runner.

Created: 2026-09-18 UTC

Author: architect-agent

Inspected baseline: `f26dd56397f6f056c74247829d2641dcd9f76e0d`

Review amendment: resolves Claude Opus High findings B1/H1 on plan commit `1dfea81`. The inspected baseline above records source research, not the future implementation base; the worker must record its actual assigned base SHA before editing.

**Goal:** Make static doctor repair guidance self-contained and honest about scan coverage, add bounded cache-hazard spellings, and state the scoped/full verification sequence explicitly.

**Architecture:** Retain `inspect(domain, config, limits, scope) -> DoctorReport` and the pure `repair_prompt(report) -> str` boundary. Existing structured reports supply trusted scan metadata; escaped finding and limitation records remain delimited untrusted evidence. Timing ingestion is explicitly deferred because the existing interface cannot safely supply attributable observations within this slice.

**Tech Stack:** Existing Python standard library, typed contracts, bounded lexical scan, bundled Markdown guide, existing deterministic NG tests. No new dependency.

**Spec:** `docs/specs/2026-09-17-ptest-ng-product.md` sections 6–7, D1–D3, A8/A10/A13; `docs/designs/2026-09-17-ptest-ng-design.md` sections 6 and 10; `docs/plans/2026-09-17-ptest-ng.md` Task 9; `.pipeline/context.md`. This brief narrows implementation scope; it does not claim completion of M3 timing or probes.

## Global constraints

- Static inspection executes no repository code, native config, subprocess, test, package manager, network request, model, or service probe; it writes neither repository nor state.
- No external database/cache/service, remote backend, model API, TUI integration, installation, legacy behavior, dependency, configuration, public schema, scheduler, or execution change.
- Preserve user files and unrelated controller dirt. Implementation belongs in an isolated worktree under `/home/ingmar/worktrees/ptest/cx-ng-doctor-hardening/ptest`; do not share `.venv` or `node_modules`.
- Existing scan defaults and maxima, the fixed 4096-character lexical-line bound, deadline checks, no-follow file reads, and `MAX_PROMPT_BYTES=65536` remain authoritative.
- Findings are hypotheses. No-findings, a clean snippet, or an ownership-looking name must not upgrade parallel readiness from unknown.
- Duration guidance never changes test status, exit status, assertions, inventory, coverage, or execution policy.
- Run scoped `ptest` during implementation; the orchestrator runs one final integrated `ptest --full` after the remaining slices are integrated. Specification-only work runs no tests and installs nothing.
- No commit outside assigned ownership, no main merge, push, publication, deployment, or replacement of the installed CLI.

## Questions and decisions

`E(X,Q)`: X is this static-doctor hardening slice; Q covers timing trust, prompt authority, lexical coverage, boundaries, and verification.

| Question | Answer and rationale |
|---|---|
| Can existing history safely produce `timing.slow-test` here? | No. Keep timing unknown and do not add a history call or synthetic observation input. See the evidence and future gate below. |
| Which prompt fields belong before untrusted evidence? | Exact relative scope/root convention, every supplied readiness entry in its original order, effective scan limits, recorded usage/skips/truncation, and a fixed static-hypothesis caveat. Values must be typed, escaped, bounded data; missing areas are not invented and duplicate areas are not reconciled. |
| How broad is cache detection? | Recognize Redis/Valkey flush spellings and explicit cache-named receiver clear methods using bounded lexical rules. Avoid matching arbitrary `.clear()`, key deletion, or similarly named non-cache methods. |
| Does general cache support prove shared-resource misuse? | No. A cache-named receiver may refer to an owned local instance; retain medium-confidence static evidence and require caller/ownership review. |
| What user-requested guidance already exists? | One database per worker per run; expensive setup once per run/worker; per-test records/factories; cache namespaces; files, ports, fixtures, child processes, time, and network. Preserve this text and executable ownership examples. |
| What needs changing in the guide? | Explicit scoped checks during repair, then one `ptest --full` final gate; preserve test inventory and coverage; add duration guidance and the current unavailable-timing limitation. |

The alternatives were (a) attach history now, (b) add a speculative observations parameter, or (c) keep the existing truthful interface and finish bounded prompt/cache/guide gaps. Choose (c): (a) crosses identity/state contracts and (b) creates an unused API without a trusted producer.

## Existing behavior and timing decision

- `src/ptest/doctor.py` already emits `_TIMING_MISSING` in limitations and timing readiness, preserves unknown readiness in every area, and exposes the pure `timing_bucket(duration_s)` helper. It does not ingest timing observations.
- Its existing `cache.global-flush` expression, `\bflush(?:all|db)\s*\(`, is case-sensitive and misses JavaScript `flushAll()`/`flushDb()`. It has no general cache-wide clear rule.
- `src/ptest/render.py:repair_prompt` already delimits JSON evidence and bounds total UTF-8 output, but omits scope/readiness/limits/usage from its trusted preamble. It currently includes only code/path/remediation/verification for findings.
- `src/ptest/resources/agent-guide.md` already carries the user's DB/factory/cache/resource guidance. Its final paragraph requests only the scoped rerun. The renderer separately says “then the full gate,” without the exact `ptest --full` command.
- `history.read_history(domain: DomainPaths, checkout: CheckoutIdentity) -> HistoryView` requires a validated checkout that `inspect` does not receive. `HistoryView` contains a baseline, obligations, disabled state, and limitations; a baseline inventory can contain `TestRecord.setup_s/call_s/teardown_s`, but it is not a complete attributed observation stream.
- `history.read_history_summaries(domain, checkout, limit=None) -> tuple[dict, ...]` and `read_history_payload(...) -> dict` expose public run summaries/obligations, not an exact per-test/attempt timing API. Run and phase elapsed time must not be presented as test duration.
- Both history read paths can call `_write_disabled_marker` on corruption. Their “read” names do not satisfy doctor's stronger no-state-write promise. Using history internals or copying its identity logic into doctor is forbidden.

**Current interface decision:** keep all public signatures and JSON fields unchanged; preserve timing `unknown` plus an explicit absent-validated-input limitation; emit no `timing.slow-test`. Do not remove the existing bucket helper. The bundled guide may explain timing budgets without implying that doctor measured them.

| Observed duration, if supplied by a future validated producer | Guidance |
|---|---|
| `0 <= d < 0.5` seconds | healthy |
| `0.5 <= d < 2` | inspect, especially repeated ordinary tests |
| `2 <= d <= 3` | optimize |
| `d > 3` | investigate or record integration justification |

Existing bucket tests must continue rejecting booleans, strings, negative values, NaN, and infinities. Static comments, sleep literals, environment variables, JSON dropped in the repository, and suite elapsed time cannot become observations.

**Separate future timing gate, not implementation work in this plan:** an independently reviewed interface must bind explicit domain/project/checkout, runner and compatibility/source identity, run/attempt, test ID/path/outcome, supported phase attribution and unknown components. It must expose finite byte/record/deadline bounds, preserve partial/missing/unsupported evidence, reject foreign/stale/malformed data, and prove no state creation or mutation even on corrupt/absent history. A failed run can contain valid timing; run success alone is neither required nor sufficient evidence of timing validity. No missing phase may silently become zero, and no attempt may disappear through averaging. Only after that producer and call-site contract exists may a bounded observed `timing.slow-test` finding be designed. A history filename, matching basename, environment redirect, or caller-created duration tuple is insufficient identity authority.

## Owned implementation files and contracts

| File | Responsibility/change |
|---|---|
| `src/ptest/doctor.py` | Extend only the cache lexical catalog; retain bounded traversal, accounting, timing and readiness contracts. |
| `src/ptest/render.py` | Add trusted bounded scan context and complete allowlisted finding fields to `repair_prompt`; preserve pure rendering. |
| `src/ptest/resources/agent-guide.md` | Exact scoped/final commands, duration guidance, and the current timing limitation. |
| `tests/ng/test_doctor.py` | Positive/negative cache matrix and timing/no-state regression evidence. |
| `tests/ng/test_render.py` | Scan metadata, full finding context, delimiter, bounds, and pure-renderer regressions. |
| `tests/ng/test_resources.py` | Bundled guide assertions for the exact scoped/final sequence and guidance. Preserve existing ownership tests. |
| `.pipeline/out/doctor-hardening.json` | New task-owned implementation evidence: actual assigned base SHA, final head SHA/range, exact commands/outcomes, durations, skips, limitations, reviewer result. |

Do not modify `contracts.py`, history/storage/platform, CLI/operations/adapters, schemas, configs, recipes, fixture ownership helpers, dependencies, or lockfiles. Parent owns integration and gate orchestration. A discovered need for another production owner is a plan amendment, not permission to expand this task.

### Prompt contract

Keep `repair_prompt(report: C.DoctorReport) -> str`, `_doctor_data(report)`, `render_doctor_json`, and the public report schema unchanged. Reuse `_doctor_data` for field projection instead of creating a second public codec.

Trusted prefix order:

1. Fixed repair constraints: verify the suspected behavior/callers, preserve assertions/inventory/coverage, respect resource ownership, scoped `ptest` during repair, one `ptest --full` final gate, and existing forbidden repairs.
2. The actual bundled guide, still loaded through `_guide()`; missing package data remains `state-unavailable`.
3. Fixed caveat: “Static findings are hypotheses. No findings does not certify parallel safety. Scan limits and missing evidence constrain this advice.”
4. One machine-readable physical line beginning `Scan context: ` followed by ASCII-escaped JSON with exactly `scope`, `readiness`, `limits`, and `usage`.
5. Fixed explanation that `scope=[]` means the resolved repository root, nonempty scope lists exact inspected relative targets, and null limits/usage mean unavailable metadata. Readiness entries are copied in order; missing areas are not assessed and repeated areas are not reconciled. It must distinguish scan `usage.truncated` from later prompt evidence truncation.
6. `BEGIN UNTRUSTED DOCTOR EVIDENCE` and bounded records, then the existing closing delimiter.

`scope` preserves `list(report.scope)` exactly; do not invent an absolute root, treat an empty list as “no scan,” or truncate a target into a different path. `readiness` is exactly `[{"area": r.area, "state": r.state} for r in report.readiness]`: preserve order, partial tuples, duplicate areas, and even conflicting states on duplicate entries. An empty tuple becomes `[]`; do not fill missing areas with unknown, sort, deduplicate, select a winning state, or infer an overall readiness judgment. The static inspector continues producing its existing four unknown areas; the pure renderer does not strengthen the wider `DoctorReport` contract. Copy numerical/bool limits and usage exactly from the typed report, including skipped count and truncation; do not recompute or reinterpret existing `file_bytes` accounting. Readiness reason text and limitation/path text remain inside the untrusted evidence section, never executable instructions in the prefix. Caller-selected path strings in metadata remain JSON data, escaped on a single physical line.

Finding records retain all nine existing public fields: code, severity, confidence, path, line, evidence_type, consequence, remediation, verification. Include limitations and readiness reasons as escaped data records after findings. Associate each readiness-reason record with its zero-based `readiness_index`, `area`, and `state` so reasons remain attributable when areas repeat; this is prompt-only data, not a public schema change. Do not include source snippets, native commands, environment values, or unrelated file contents. Prefix metadata does not promote any evidence string to trusted instructions.

Budget the complete UTF-8 prefix, closing delimiter, and truncation marker before admitting evidence. Never truncate trusted context into an ambiguous value. If exact context plus bundled guide and reserved suffix/marker cannot fit `MAX_PROMPT_BYTES`, return `C.Problem(code="invalid-bound", phase="render")`; no output fragment. The evidence loop admits only complete JSON records, stops at the first omitted record, and shows the existing truncation marker. Every successful result has one opening and one closing delimiter as whole lines and is at most 65536 bytes. `usage.truncated=false` must not hide independent prompt truncation.

### Cache lexical contract

Use only the existing `cache.global-flush` code for both Redis/Valkey flush calls and general cache-wide clear calls. `C.FINDING_CODES`, the public doctor schema enum, the design catalog, and their parity tests remain unchanged. Both detection branches retain the existing high severity, medium confidence, and `static-pattern` evidence: severity describes possible destructive impact, while confidence and explicit hypothesis wording acknowledge uncertain receiver type/ownership. Do not introduce another finding code or lower existing flush severity.

The flush branch matches `flushall`, `flushdb`, `flushAll`, `flushDb`, `flushDB`, and uppercase variants using a bounded case-insensitive alternative with the existing identifier boundary and call parenthesis. The clear branch recognizes a dot-method call whose immediate receiver is a bounded identifier with an explicit cache token, followed by one of `clear`, `clearAll`, `clear_all`, `invalidateAll`, or `invalidate_all` and a call parenthesis. Match cache tokens only at an entire identifier or snake/camel token boundary: `cache`, `caches`, `shared_cache`, `cache_client`, `cacheClient`, `memoryCache`, `applicationCache`, and `obj.cache` qualify; `cacheable`, `cachet`, `showcase`, and `cachedValue` do not. No import/type resolution, alias inference, bracket-property parser, multiline parser, AST rewrite, or new package. Document these lexical limits; not finding an alias remains unknown.

Retain a single `_RULES` row for `cache.global-flush` and its shared consequence/remediation/verification fields. Extend only its matching predicate: `bool(pattern.search(line)) or (code == "cache.global-flush" and _cache_clear_call(line))`, where the private `_cache_clear_call(line: str) -> bool` uses one compiled call expression and bounded receiver tokenization. This preserves one catalog entry and one finding per code per line even when both branches match; no duplicate-code catalog rows or second scanner. Tokenization operates only on the already bounded line/receiver and splits snake case and lower-to-upper camel transitions. Match the token `cache` or `caches` case-insensitively. Keep all existing finding/output/deadline accounting.

Use common catalog prose that truthfully covers both branches. Consequence: a cache-wide flush or clear may erase another worker/run's entries. Remediation: verify receiver lifetime and ownership; use checkout/run/worker key namespaces and delete only owned keys, or demonstrate an exclusively owned disposable cache. Verification: preserve a neighboring run/worker sentinel during cleanup. Broad clear on an owned cache remains a review hypothesis, never an automatic refactor instruction.

| Positive examples | Required negative examples |
|---|---|
| `redis.flushAll()`, `valkey.flushDb()`, `client.flushDB()`, `client.FLUSHALL()` | `client.flushAllMetrics()`, `flushall_pending`, `flush_database()` |
| `cache.clear()`, `shared_cache.clear_all()`, `memoryCache.clearAll()` | `items.clear()`, `new Set().clear()`, `form.clear()`, `console.clear()` |
| `cacheClient.invalidateAll()`, `obj.cache.clear()` | `cache.delete(key)`, `cache.deleteMany(ownedKeys)`, `cache.clearKey(key)`, `cache.clearOwnedNamespace(prefix)` |
| `applicationCache.clear()` | `cacheable.clear()`, `cachedValue.clear()`, `cachet.clear()` |

This remains lexical evidence: strings/comments may resemble calls in non-Python files, and a receiver name is not type evidence. Do not claim full JS parsing or reliable comment exclusion. Python comment-only lines remain excluded by the existing syntax-line filter. Tests must disclose the lexical false-positive limit instead of silently broadening claims.

## Implementation phases and acceptance

### Task 1: Cache coverage with preserved unknown readiness

Files: `doctor.py`, `test_doctor.py`. Complexity: small. Dependency: reviewed plan.

- [ ] Add parametrized positive and negative cases from the exact matrix above, using `_resolution`, `case.domain()`, and `case.project(domain)` already in the test module. Put JS snippets in temporary `tests/cache.test.js`; Python snippets in valid temporary Python source. Every positive emits `cache.global-flush` with high severity/medium confidence/`static-pattern`; assert exact path/line and parallel/timing unknown by area. Put a flush and clear on one line and assert exactly one finding with safe shared remediation/verification. Existing catalog parity assertions must remain valid without editing the closed code list or schemas.
- [ ] Example first RED regression:

```python
def test_cache_hardening_camelcase(case):
    domain = case.domain()
    root = case.project(domain)
    tests = root / "tests"
    tests.mkdir()
    (tests / "cache.test.js").write_text("redis.flushAll();\n", encoding="utf-8")
    report = inspect(domain, _resolution(case, root), C.DEFAULT_SCAN_LIMITS, None)
    assert [(f.code, f.path, f.line) for f in report.findings] == [
        ("cache.global-flush", "tests/cache.test.js", 1)
    ]
    assert next(r for r in report.readiness if r.area == "parallel").state == "unknown"
```

- [ ] Run `ptest tests/ng/test_doctor.py -k cache_hardening --durations=20` and record expected RED for currently missed spellings and general clear.
- [ ] Implement the bounded lexical changes. Keep fixed catalog prose; never copy matched source into a finding. Add helper tests for snake/camel token boundaries only if a helper is introduced.
- [ ] Run the same scoped command GREEN. Then run `ptest tests/ng/test_doctor.py --durations=20` for catalog, byte/entry/file/output/deadline limits, unsafe-path and timing regressions.
- [ ] Extend the existing `test_static_markers_do_not_claim_observed_timing_or_read_state` only where needed, retaining its fake source timing marker, normal-state read sentinel, environment redirect, and unchanged-domain assertions. Add history API failure sentinels or other genuinely missing negative evidence in that test; do not duplicate its coverage in a new parallel test. Scan emits no `timing.slow-test`, timing stays unknown, and state remains untouched. Keep existing exact bucket boundary/invalid-value tests; do not manufacture RED for unchanged correct behavior.

Acceptance: supported spellings produce concrete hypotheses, clean negatives do not produce cache findings, readiness stays unknown, and bounds/static behavior remain intact.

### Task 2: Trusted scan context and bounded untrusted records

Files: `render.py`, `test_render.py`. Complexity: medium. Dependency: reviewed prompt contract; no dependency on Task 1 output.

- [ ] Extend `_hostile_report()`/`dataclasses.replace` fixtures to set scope `("tests/unit",)`, non-default limits, usage with skipped entries and truncation, four area states, and an actual finding location. Assert context appears before the opening delimiter and decode the JSON line to compare exact metadata.
- [ ] Concrete assertion pattern:

```python
prompt = repair_prompt(report)
before, evidence = prompt.split("BEGIN UNTRUSTED DOCTOR EVIDENCE\n", 1)
line = next(line for line in before.splitlines() if line.startswith("Scan context: "))
context = json.loads(line.removeprefix("Scan context: "))
assert context["scope"] == list(report.scope)
assert context["usage"]["truncated"] is report.usage.truncated
assert context["limits"]["files"] == report.limits.files
assert context["readiness"] == [
    {"area": r.area, "state": r.state} for r in report.readiness
]
```

- [ ] Add null usage/limits, empty-root-scope, and readiness tuples with zero entries, one area, reordered areas, and duplicate parallel entries with different states/reasons. Use the exact list-comprehension assertion above for every case: preserve entries/order/states with no fill-in, sorting, deduplication, or conflict resolution. Verify complete finding fields and indexed readiness-reason records occur inside the untrusted section, preserving duplicate-entry association.
- [ ] Run `ptest tests/ng/test_render.py -k 'scan_context or prompt' --durations=20` and record RED for omitted context/fields.
- [ ] Implement the prefix and whole-record evidence budget using existing projection, ASCII JSON escaping and package resource loader. Reserve suffix and truncation marker before evidence admission; retain original truncation behavior.
- [ ] Add hostility cases: newlines/CR/ESC/NUL/bidi controls/Unicode separators in scope, paths, reasons and remediation; forged delimiter lines; multibyte long strings; an oversized exact context; many records; scan truncation independently true/false. Assert no physical delimiter injection, no terminal controls, no raw sentinel source/env/argv, explicit truncation or `invalid-bound`, and no over-limit successful prompt.
- [ ] For oversized context/guide, assert failure before returning text. For bounded data, assert exactly one whole-line opening/closing delimiter and successful JSON decoding of each admitted record.
- [ ] Patch process/network/history/domain-resolution entrypoints with failure sentinels while calling the pure renderer with a report. Bundle reads remain permitted; repository/state reads and writes are forbidden.
- [ ] Run the focused command GREEN, then `ptest tests/ng/test_render.py tests/ng/test_doctor.py --durations=20`.

Acceptance: a recipient can see exact scan reach and uncertainty without the originating command, and untrusted strings cannot acquire instruction authority or break the cap.

### Task 3: One bundled verification workflow

Files: `agent-guide.md`, `test_resources.py`, prompt constraint prose in `render.py` if still necessary. Complexity: small. Dependency: Task 2 prefix budget must be rerun after guide changes.

- [ ] Extend `test_agent_guide_contains_local_nonexecuting_repair_workflow` to assert scoped `ptest` during repair precedes one exact `ptest --full` final gate; preserve existing database/factory/cache statements and forbid any claim that a static scan measured timings.
- [ ] Run `ptest tests/ng/test_resources.py -k agent_guide --durations=20` and record RED for the missing final command.
- [ ] Replace the guide's final paragraph with:

```markdown
Preserve assertions, test inventory, coverage, and test semantics. During repair,
run the scoped `ptest` command for the affected behavior. After the repairs are
integrated, run one `ptest --full` final gate. A text-pattern change alone is not
proof that isolation works.

When validated test timings are available, under 0.5 seconds is healthy;
0.5–2 seconds merits inspection, especially for repeated ordinary tests;
2–3 seconds merits optimization; over 3 seconds needs investigation or an
integration justification. Exactly 2 seconds starts optimization; exactly 3
seconds remains in that band. These budgets are guidance, never automatic test
failures. This static doctor currently has no validated per-test history timing
input and reports timing as unknown; do not infer durations from source.
```

- [ ] Keep only the minimal command-sequence summary in the renderer's fixed constraints; the bundled guide stays the detailed source of repair guidance. Do not duplicate the guide into recipes or another TUI file.
- [ ] Run `ptest tests/ng/test_resources.py -k agent_guide --durations=20` GREEN, then `ptest tests/ng/test_doctor.py tests/ng/test_render.py tests/ng/test_resources.py --durations=20`. Record the reported durations and investigate any deterministic regression over 3 seconds; use an affected scoped `ptest ... --durations=0` run if complete per-test timing detail is needed. Never call the runner directly for timing output.

Acceptance: installed guide and generated prompt both request the same scoped/final workflow; DB-per-worker/run, factories, all cache types and other parallel blockers remain visible.

### Task 4: Evidence, documentation and independent gate

- [ ] Before editing, record `git rev-parse HEAD` from the actual assigned implementation worktree as `base_sha` in the new task-owned evidence file; do not copy this plan's historical inspected baseline. On handoff record `head_sha`, the actual `base_sha..head_sha` review range, RED/GREEN commands and exit codes, changed files, actual test counts, durations, skips, timing deferral and remaining lexical limitations. Do not report an unrun service/probe/native integration as tested.
- [ ] Run `git diff --check`, scoped regression checks needed after any repair, and `graphify update .` in each source-changed checkout before handoff. No graph refresh for this plan-only commit. Do not run semantic extraction.
- [ ] Have Claude Opus High audit the plan before implementation and the final owned range after implementation, using `audit-spec`. Record the actual model identifier/reasoning setting and verdict; do not silently substitute a model.
- [ ] Require no unresolved correctness/security findings, no out-of-scope file changes, meaningful RED before GREEN, and all negative contracts below. Opus must specifically check history non-use, full-prefix byte budgeting, forged delimiters in metadata, exact readiness copying including partial/duplicate entries, unchanged inspector unknown readiness, use of the existing cache finding code without schema/catalog drift, case/token boundary false positives, and exact `ptest --full` guide wording.
- [ ] If rejected, repair only the bounded findings, rerun affected scoped checks, and obtain a fresh focused review. Parent integrates the approved commit serially, then runs one final integrated `ptest --full`; task-only GREEN is not the final product gate.

## Required security negative contracts

| Boundary | Required evidence |
|---|---|
| Identity/tenancy | Explicit fixture domain remains isolated; normal-state sentinel and another checkout's state untouched. No inferred checkout ID/history path or borrowed timing. |
| Authorization | Imported source/config/comments do not execute; no subprocess/package install/network/model/TUI/probe invocation from scan or prompt rendering. |
| Input/exposure | Traversal/symlink-swap/FIFO/private-file fixtures remain rejected; no source/argv/env/dotenv sentinel in output; hostile metadata/evidence remains escaped data. |
| Availability | Existing entry/file/bytes/depth/AST/line/deadline/finding limits hold; repeated near-match cache tokens stay within fixed line work; whole prompt stays within 65536 bytes or fails explicitly. |
| State | No history-corruption marker, directory, DB, key, guide export, config write, or cleanup as a side effect. Existing DB/cache/file neighbor sentinels survive owned cleanup examples. |
| Result truth | Scan and prompt truncation are distinct and visible; no-findings never means safe; source duration literals never mean measured; no hard speed-fail gate or rewritten test outcome. |
| Dependencies | No new parser, service, installation, lockfile, schema, native integration, or legacy lookup. Missing bundled guide fails rather than substituting instructions. |

## Risks and success criteria

| Risk | Mitigation |
|---|---|
| Receiver names overstate cache ownership/type knowledge | Medium-confidence static hypothesis, precise token matrix, explicit caller/lifetime verification and documented lexical limits. |
| Metadata consumes evidence room or carries injected instructions | Typed projection, one-line ASCII JSON, exact-context fail-closed budgeting, whole-line delimiter adversarial cases. |
| History appears reusable because its API says “read” | Explicitly forbidden in this slice; source evidence records corruption-marker writes and missing attribution contract. |
| Guide change silently breaks prompt cap | Rerun renderer bounds cases after editing the bundled guide. |
| Scope broadens into M3 timing/probes or TUI work | Fixed owned-file list and separate future timing gate; Opus rejects unreviewed contract expansion. |

Success means cache hazards in the positive matrix are visible, negatives remain clean, scope/readiness/limits/usage precede untrusted prompt evidence safely, guide and prompt name scoped `ptest` then one `ptest --full`, timing remains explicitly unknown, and every static/no-write/no-execution boundary still holds. This does not certify general cache analysis, observed parallel isolation, usable timing history, probes, or full product completion.

## Plan verification

The writing-plans and graphify workflows informed this source-grounded brief. The graph query located doctor/history/render ownership; inspected source is authoritative for the timing decision. Self-review maps all four requested audit findings to explicit decisions/tasks, preserves existing DB/factory/resource guidance, and keeps absent timing support visible. No tests, dependency setup, production edits, or graph rebuild were performed for this specification-only change.

The review amendment resolves B1 by keeping one existing `cache.global-flush` catalog entry for both match branches with consistent high severity/medium confidence and common safe guidance. It resolves H1 by copying readiness entries exactly, including partial/duplicate lists, with explicit tests and indexed reason attribution. It also adds scoped `ptest` duration flags, extends the named existing timing/no-state regression rather than duplicating it, and requires the actual implementation base/head range in evidence. All prior scope and no-write constraints remain in force; Opus re-review is still required before implementation.
