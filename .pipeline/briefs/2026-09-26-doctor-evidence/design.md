# Doctor automatic evidence accuracy implementation plan

> For the assigned Muse worker: implement this approved design in one isolated task worktree using the existing single-repo-feature-muse workflow, secure-by-spec and TDD. No new user approval is required for the agreed sequence. Audit gates remain required.

Created: 2026-09-26 UTC. Author: architect-agent.

**Goal:** Make ordinary consented doctor reviews automatically collect the configured suite's setup → actual caller → cleanup/cancellation evidence, return source-ID judgments, and verify every initial judgment once with a stronger default reviewer.

**Architecture:** Retain `review_context` for static suite/relationship resolution, `agent_assessment` for bounded immutable packets and public assembly, `review_protocol` for the private model contract, and the existing CLI/provider pipeline for consent, execution and publication. Add one small `review_evidence.py` module for pure resource ranking and source-unit selection; do not add a retrieval service, evaluator, dependency or model requirement to normal ptest execution.

**Tech stack:** Existing Python standard library, frozen Python/JS source parsing, existing Claude/Codex CLI adapters.

**Spec inputs:** Parent's approved sequence; [2026-09-25 smoke](../../cx-doctor-smoke-20260925/run/results.md), its `audited-results.json`, current source at `a64774c`, and [CLI UX constraints](ux.md). The smoke's 19 OK / 5 findings / 2 N/A / 1 unresolved result used curated retrieval and is not a product accuracy score or required verdict distribution.

## Question space and decisions

X is the ordinary doctor review workflow. Q and its answers:

| Question | Decision |
|---|---|
| What is the reviewed suite? | Resolve each child's effective scoped and full configuration before selecting active test seeds. Include script tests selected by Vitest; excluded Playwright tests cannot establish Vitest behavior. Ambiguity remains explicit. |
| What is selected? | Resource mechanisms and actual use chains, with contrary and timeout paths represented; paths are discovered generically from configuration, calls, fixtures and imports. |
| Who owns citations? | ptest assigns item/packet-bound source IDs and materializes the existing public citation objects. Models never author coordinates, hashes or proof quotes. |
| How is an initial OK checked? | One combined evidence/verification pass for every valid initial model answer, regardless of status or `needs`. No recursive third call. |
| What reviewer? | Flag > `PTEST_REVIEW_MODEL` > explicit stronger provider default. Codex defaults to listed `gpt-6-sol`; Claude defaults to `opus`. No cheap picker or historical cheap-model cache. |
| What is unchanged? | Public v1 document, four statuses, score, per-row reasons, citation shape, guarded publication, deterministic rows, offline behavior and one invocation consent. |
| How to prove it? | Synthetic retrieval/protocol/security regressions plus an automatic live smoke using production collection/planning/assembly on current Persea API, Persea Vitest and ptest, with no target writes. |

Alternative considered: only switch models. Rejected because the smoke showed retrieval and bookkeeping failures independent of model choice. A general agentic repository browser is also out of scope; the frozen bounded packet is the simpler authorization and reproducibility boundary.

## Global constraints

- No tests/dependencies/source edits during this design task. Implementation tests run only through `ptest`; use scoped TDD runs and one final integrated `ptest --full`.
- One Muse implementation task owns the affected files in `/home/ingmar/worktrees/ptest/cx-doctor-evidence-20260926/repo-t1`. It is not alone; preserve others' changes. Fresh dependencies belong to that worktree; use `uv` and never share `.venv`/`node_modules`.
- Never merge main, push, publish, deploy, replace the installed CLI, execute target configuration/source, migrate databases, or alter Persea. The orchestrator integrates an audited commit onto the chain only.
- Do not read `.pipeline/context.md` or pipeline/history artifacts before 2026-09-22. Do not run broad graph/history queries. After source changes perform the required local structural `graphify update .` without source transmission or historical semantic extraction.
- No project-specific paths, expected verdicts, curated read manifests, live model fixtures in ordinary tests, or increased provider payload/concurrency/deadline limits.

## Design

### 1. Resolve suite membership before resource selection

`review_context.py` remains the membership authority. Separate config/profile resolution from seed traversal so excluded test files cannot become setup/import seeds merely because they sort first. Preserve the current two Vitest profiles, ambiguity handling, literal parsing and no-execution rule. For pytest incorporate effective config precedence, literal `testpaths`/`python_files`/`norecursedirs`, explicit runner paths and supported `--ignore`/`--ignore-glob` arguments for both scoped/full profiles. Unsupported collection flags or dynamic configuration produce partial membership; do not infer absence from them. Configured ptest test roots are hints unless actual runner/config semantics establish collection roots.

Extend existing pytest fixture traversal to parameters of fixture definitions, imported fixture aliases and activated local plugins, preserving nearest-applicable conftest ownership and recording ambiguity. A fixture or helper being defined/imported is not evidence that an active test calls it. Keep local import edges and add an explicit static caller/fixture-use relation where syntax establishes it. An excluded test can contribute a helper only when an active call/import makes that helper reachable; it must not become an active test.

The new pure `review_evidence.py` owns:

- `rank_candidates(context, candidates, texts, catalog) -> tuple[str, ...]`: use discriminative resource operations from the existing catalog/scanner, config/setup roles and resolved relations. Round-robin the nine model items and distinct active test subtrees; prioritize concrete operation + caller/cleanup chains and contrary/timeout branches before generic directory matches. Lexical order is only a stable tie-breaker. Every item gets an opportunity before any item fills the shared packet.
- `source_units(excerpt, item_id, context) -> tuple[SourceUnit, ...]`: produce contiguous original line spans, never stitched or synthesized text. Python AST locations select complete relevant functions/classes plus imports and referenced module declarations; `finally`, fixture yield teardown, context exit and assertions stay with their owner. For JS/TS, whole complete bounded modules are acceptable initially; reuse existing static token primitives for safe boundaries if smaller units are needed. A partial parse or oversized function is recorded missing, not offered as a complete mechanism.
- `select_item_sources(packet, item_id) -> (initial, reserve, missing)`: combine relevant configuration, setup, real callers, normal cleanup and exception/cancellation paths. Do not seed every unrelated helper into every item. Give distinct concrete exceptions space even when a good helper exists.

`SourceUnit` is private and carries `{path,start_line,end_line,source_sha256,text,role}`. `source_sha256` is the owning immutable `SourceExcerpt.sha256`, preserving existing public citation and guarded source-proof semantics. Packet excerpts continue to retain original file bytes; selected model units are views into those bytes. No change to `recommendations` source-proof byte-prefix hashing is needed.

Collection scans bounded complete source candidates before final packet admission, so a relevant late-name test can outrank filler. Raise only local candidate defaults from 256 files/2 MiB to **1024 files/16 MiB**, shared by context, inventory and admission; cache bounded reads and debit failed/invalid reads too. Keep 64 KiB source read/admission cap, 20,000 walk entries, 64 admitted files/512 KiB per child. Context traversal may touch at most 64 files, depth 3 and 16 outgoing links per file, inside the same ledger. Keep explicit candidate/coverage counts and a digest of consulted inventory in packet identity for deterministic revalidation. Existing custom smaller limits remain honored.

These bounds are a reasoned tradeoff: current API tests are about 704 files/10 MiB, while ptest tests alone exceed the old 2 MiB candidate budget. Reading more candidate bytes does not authorize sending them all. Any unscanned, over-budget, unreadable, incomplete or unresolved source remains a named coverage limitation. Oversized files must not contribute an arbitrary prefix as an apparently complete function; unaffected complete units inside a provable prefix may be used only with explicit partial-source metadata and no absence claim.

### 2. Private v3 source-ID contract

Replace the v2 quote/citation-index proof machinery with one exact private JSON object:

```text
{status, rationale, evidence: [source_id],
 finding: null | {summary, suggested_change, evidence: [source_id]},
 needs: [offered_source_id]}
```

Keep existing statuses and prose limits, at most 16 evidence IDs and four needs. `source_id(packet_sha256, item_id, source_unit) -> str` derives an opaque `src-<24 lowercase hex>` token from packet, item, path, owning hash and span bounds. Duplicate evidence IDs are rejected; unknown/foreign/stale/unoffered IDs are item-local protocol errors. Positive, gap and N/A rows require evidence; gaps require nonempty finding evidence. The model may request offered reserve IDs for any initial status. Final `needs` must be empty. Remove `proof`, quote roles and old fallback validation; reject mixed-version responses rather than carrying two production protocols.

Program validation binds IDs to exactly the units supplied to that request and constructs unchanged public `{path,start_line,end_line,sha256}` citations. The span must lie within its owning excerpt and its text must equal the original lines. A valid ID establishes provenance, not truth of a judgment. Keep `ptest.agent-assessment/v1`, public row/finding keys, deterministic answers, score computation and user-report protection unchanged. Update provider fakes/fixtures to generate valid IDs rather than weakening public validators.

Initial request bounds remain 20 distinct source files / 192 KiB of source; final bounds remain 24 files / 256 KiB; additional evidence is at most four files / 64 KiB. Bound units to 64 per item and the reserve inventory to 64 IDs; embedded schema, metadata and JSON escaping must fit the existing 1 MiB provider input cap. ID inventory contains safe path/role/size metadata only, never excluded names. `ItemReview` carries its immutable source-ID map and reserve units alongside its existing packet authority; all assembly and follow-up lookups use that map rather than model paths or path-only deduplication.

### 3. One rubric and mandatory bounded verification

Keep operational criteria in `checklist.py` as the single item authority, with one shared instruction in `agent_assessment.py`. Incorporate the approved smoke rubric: fresh instances are ownership; SQLite is a database; per-instance maps can be caches; managed temporary roots/context managers count; injected in-process transports can establish sampled network isolation; `subprocess.run` owns/reaps its direct child, not arbitrary descendants; state polling is different from sleep-based correctness. Deliberately bad test examples and mocked calls are not demonstrated live effects. Concrete reachable contrary evidence takes precedence over one good helper. No suite-wide absence or runtime-execution proof is implied.

`plan_followup_review(packet, review, reply)` becomes the combined verifier planner (retain its public-in-module name to minimize orchestration churn):

1. Validate the initial private reply. Malformed/provider failures remain named errors; do not fabricate semantic verification from them.
2. For every valid initial status, schedule exactly one verification call, even with empty `needs` or no reserve units. It carries the initial answer as untrusted draft data, the same criterion/rubric, initial units, and bounded added caller/helper/counterevidence units.
3. Select additions from program-established relations and resource coverage gaps first, then valid requested IDs. The offered inventory is frozen before calls; models cannot open paths, request shell commands or expand scope. Resolve an exact omitted caller/helper once from that inventory. If the required source never fit the frozen packet, retain the missing fact; do not claim it was fetched.
4. The verifier must explicitly check actual caller reachability, the relevant consumer of a setting, ownership and timeout/cancellation paths before accepting a draft. It can keep or change any status using the same criteria. Missing decisive context yields unknown naming the fact; no target distribution, majority vote, confidence threshold or forced status.
5. The final verifier reply is authoritative only after normal validation. Verification provider/protocol failure yields an unknown error row, never a quietly retained unverified OK. No third request or new prompt.

Preserve reason classes using the existing public rationale/limitation channels: semantic missing evidence is ordinary unknown text; a valid request blocked by a bound says which evidence/bound is missing; malformed response starts `Review failed: invalid reply: ...`; provider/timeout errors use existing named failure reasons. Do not map budget exhaustion to provider failure. Final source/config revalidation and publication continue through current guards.

### 4. Model defaults, consent and output

Preserve override precedence. Claude defaults to the `opus` complex-reasoning alias ([official alias documentation](https://code.claude.com/docs/en/model-config)); this is a capability policy, not a Claude accuracy claim from the Codex smoke. Codex default is `gpt-6-sol` only when post-consent bounded `debug models` lists it. If unavailable/discovery fails, stop with a clear unsupported-default reason and preserve the report; an explicit user override remains permitted. ptest must not choose a smaller or unspecified fallback model itself. Claude or another provider may internally fall back; ptest does not promise to prevent that provider behavior. Disclosure, profile and report wording identify the requested model unless trusted provider metadata verifies the served model. A requested alias is not proof of the served model.

Delete the model-pick call and cease reading/writing the obsolete cheap-model cache; preserve existing cache files and uninstall ownership rules. CLI version discovery remains bounded and after consent. Provider tools, credentials/environment handling, sandbox and process ownership remain unchanged.

For N model-reviewed items disclose `N initial + up to N verification (2N maximum)`, concurrency and requested model before the existing once-only consent. Count every actual attempted item call, including failures, in the final summary. Deterministic-only, offline and declined paths launch no provider/model-discovery process. No new cost prompt for the approved verification phase. Use the existing stderr progress surface for `verifying evidence`; stdout JSON remains one document.

Add one program-owned qualifier beside the terminal grid and in Markdown: model OKs cover the cited mechanisms and representative callers, with static-review limits. Retain per-row reasons, useful source citations, output sanitization, ASCII/narrow-terminal behavior and actionable gap details. Update README, help and both bundled guides to remove cheap-model/picker claims and state the bounded two-phase behavior. Do not add private IDs/source text to public JSON.

## Review focus and acceptance criteria

- [ ] **A1 Automatic suite selection:** synthetic pytest/Vitest fixtures prove excluded e2e findings do not contaminate active suite judgments; script tests, full-profile differences, config precedence, partial/dynamic config and fixture shadowing remain correct.
- [ ] **A2 Automatic evidence selection:** a dangerous caller after 100 irrelevant files, a factory used through a fixture dependency, an imported cleanup helper and a timeout branch all reach the appropriate item pack automatically. Rename/reorder filler and project directory without changing the selected semantic chain. No curated paths in production or live smoke.
- [ ] **A3 Complete units:** setup, caller assertions and `finally`/teardown remain intact; imports and referenced constants are available; oversized/incomplete units record a reason and never prove missing cleanup. Candidate admission cannot exceed custom or default ledgers.
- [ ] **A4 Source IDs:** valid IDs reconstruct exact old-format public citations. Wrong item/child/packet, changed source, duplicate IDs, unoffered reserve IDs, hostile prose and v2 proof fields fail closed with specific item-local reasons. Exact public document schema still validates.
- [ ] **A5 Verification:** fake providers show initially OK, gap, N/A and unknown each get one verification; an initial false OK can become a caller-supported gap, an initial false gap can become OK, and an untraced setting stays unknown. Empty reserves still permit verification, final needs cannot recurse, and invalid/provider replies never become successful reviews.
- [ ] **A6 Defaults/consent:** stronger defaults, flag/env overrides, unsupported Codex catalog, legacy cache ignoring, no picker call, truthful ceiling/actual count, requested-versus-verified-served model labeling, zero provider calls on offline/decline/deterministic-only and unchanged non-TTY consent are exercised.
- [ ] **A7 Preservation/security:** source mutation before publication, symlink/FIFO/traversal inputs, sibling child IDs, bounded floods, timeout/cancellation and user-edited reports retain existing fail-closed behavior. No target test/config imports or execution.
- [ ] **A8 Live proof:** automatic live smoke produces reviewable source-chain/citation evidence for all 27 model items across Persea API, Persea Vitest and ptest, reports which chains remain incomplete, and independently checks substantive findings and representative OKs. No fixed verdict count is a pass criterion. Do not call the automatic-retrieval work complete if missing known chains require hand-fed source paths; fix generic retrieval or clearly retain the unmet acceptance item.

## Secure-by-spec negative contracts

Actors are the local invoking user, untrusted repository/config text, hostile model output and a failing external CLI. Protected resources are excluded/private source, sibling projects, user reports, provider credentials, local processes and finite budgets. Boundaries are filesystem → packet → provider → validator → public report.

| Axis | Intended property and negative twin | Regression evidence |
|---|---|---|
| Identity | Reuse qualified local provider account; missing/revoked login fails as provider error. No credential extraction, new authentication store or secret logging. | Existing provider-error/env tests plus failure during verification; auth failure must not create semantic findings. |
| Authorization | One consent covers disclosed source packs and at most 2N item calls. Offline/decline must launch no provider or discovery; model IDs cannot grant filesystem access. | Zero-call spies and exact call ceiling; hostile `needs` and source instructions cannot widen reads. |
| Tenancy | Child/scope/packet/item IDs are isolated. A valid ID from another child/run/item must not bind; restricted scope must not read ancestor or sibling source. | Foreign-ID and restricted-scope no-follow reader assertions, including follow-up. |
| Input | Only bounded regular UTF-8 sources/config literals and exact private shapes are accepted. No symlink/FIFO/path traversal/config execution, unknown IDs, oversized payloads or prompt-injected commands. | Existing no-follow fixtures plus v3 malformed/oversized/nested payload and poisoned-source tests. |
| State | Frozen inventory, deterministic selection, one verifier and guarded publication. Repeated calls cannot recurse; source/config edits and report races must not publish stale evidence or overwrite user bytes. | Revalidation hash drift, source/parent-symlink swaps, repeat/final-needs and publication sentinel tests. |
| Exposure | Provider receives only admitted safe units under consent; public output has allowlisted v1 keys. No private paths/IDs/source, provider stderr credentials, or excluded names leak. Source can contain undetected secrets; retain honest disclosure. | Exact JSON key assertions and sent-packet exclusion assertions; output sanitizer regressions. |
| Availability | All reads, AST work, candidate fan-out, inventories, bytes, calls and elapsed time have bounds. Floods and verification share ledgers/deadline/concurrency; cancellation stops owned process groups only. | Tight custom budgets, saturated trees/import cycles, oversized function, total-deadline and neighbor-process sentinels. |
| Dependencies | No new runtime dependency/API; static parsing never imports target modules. Provider calls remain fixed argv through existing adapters; no model-provided shell/path/template execution. | Fake executable/config side-effect sentinels, unchanged provider argv/isolation tests, lockfile unchanged. |

Abuse cases made explicit beyond the earlier smoke: stale cheap-model cache bypassing policy; source-ID replay across child/item; an OK avoiding verification; an excluded test seeding active setup; function clipping hiding cleanup; and a valid verification request being mislabeled as provider failure. The orchestrator shares this concise list before implementation. No security axis is N/A.

## Implementation phases: one task, one ownership boundary

The worker owns `src/ptest/{review_context,review_evidence,review_protocol,agent_assessment,cli,checklist,help,render,recommendations}.py`, README, both bundled agent guides, and the corresponding tests/fixtures below. Avoid unrelated cleanup. `agent_providers.py` changes are unnecessary unless a qualification regression proves a required adjustment; preserve its isolation contract. Public `contracts.py` must not change its schema.

1. **Foundation and RED:** Add retrieval cases to `tests/ng/test_review_context.py` and new `tests/ng/test_review_evidence.py`; migrate/add protocol cases in `test_review_protocol.py`. Record observable failing assertions against the baseline. Existing satisfied controls need honest sensitivity evidence rather than invented RED.
2. **Core GREEN:** Implement static suite-before-seed resolution, shared candidate ledger/ranking/units, private source maps and v3 validation. Consolidate obsolete v2 quote proof and cheap-pick/cache code only after caller inspection; remove tests whose only purpose was the retired behavior. Preserve public citation regressions.
3. **Integration RED/GREEN:** Update fake replies in `tests/ng/factories_agents.py`, provider fixtures and `test_agent_assessment.py`; add all-status verification, model/default/consent/count tests in `test_cli.py`, `test_doctor_init_integration.py` and `test_agent_doctor_acceptance.py`. Reuse pipeline helpers; any extracted batch executor must be the production CLI path and the smoke path, not a parallel implementation.
4. **UX and documentation:** Adjust existing rendering/help/guide assertions in `test_render.py`, `test_recommendations.py`, `test_help.py`, `test_parallel_output_cli.py`, `test_resources.py` and `test_agent_rules.py`; leave snapshots of intentionally historical report provenance alone where still valid. Add the positive-scope qualifier without a grid redesign.
5. **Verification/handoff:** Muse runs scoped gates, local structural graph update and security inventory/gates; the orchestrator owns model qualification canaries and automatic live smoke; record commands/exit statuses and preservation evidence in this run directory. Audit and fix task findings, rerun affected scoped gates, and commit in the task worktree. Muse runs scoped gates only. The orchestrator integrates the approved task commit onto the chain, completes the final Sol audit and any resulting fixes, then runs the one final integrated `ptest --full` on the exact final chain tip.

Scoped TDD commands (invoke the task checkout's installed local `ptest`, not the globally installed CLI):

```bash
uv run --locked --no-sync ptest tests/ng/test_review_context.py tests/ng/test_review_evidence.py tests/ng/test_review_protocol.py
uv run --locked --no-sync ptest tests/ng/test_agent_assessment.py tests/ng/test_agent_assessment_contract.py tests/ng/test_cli.py tests/ng/test_doctor_init_integration.py tests/ng/test_agent_doctor_acceptance.py
uv run --locked --no-sync ptest tests/ng/test_render.py tests/ng/test_recommendations.py tests/ng/test_help.py tests/ng/test_parallel_output_cli.py tests/ng/test_resources.py tests/ng/test_agent_rules.py
```

Orchestrator-only final integrated gate, from the final chain checkout:

```bash
uv run --locked --no-sync ptest --full
```

Use individual node IDs for the actual RED/GREEN loops where supported, keeping coverage gates/output/exit codes intact. Ordinary tests use fake providers and need no account or network. The orchestrator owns the final full gate, run once on the chain after all implementation/audit edits; Muse must not run an additional task-level full gate. A new change or failure justifies the needed rerun.

Existing security infrastructure: `scripts/security-checks.py` provides Bandit, pip-audit and pinned Gitleaks sensitivity gates; `tests/ng/test_security_gates.py` tests that unavailable/insensitive gates fail. Run applicable installed scanners and dependency audit with `uv`; report unavailable gates rather than silently marking passed or adding unrelated infrastructure. Do not invoke its full-history Gitleaks command (`--log-opts=--all`) under the current history cutoff; use the equivalent current-checkout scan and record history scanning as excluded by instruction. Property/fuzz tooling is absent from the inspected dependency manifest; use focused adversarial regression fixtures without adding it.

## Automatic live smoke and evidence handoff

Before any source-bearing live smoke, the orchestrator must pass model-specific tool-denial canaries for **both** new defaults: Codex `gpt-6-sol` and Claude `opus`, using the existing qualified provider profiles and model flags. Follow `docs/research/2026-09-22-agent-provider-qualification.md:130`: qualification is per provider profile/model, and the controller reruns the canary whenever it changes an explicit override, selected model or Claude alias target. Canaries send only synthetic decoys, verify tool-use denial and unchanged write sentinels, and record the requested model plus any verified served-model metadata. This is the existing controller release/smoke gate, not a new runtime API phase or an undisclosed call in ordinary doctor reviews. Muse tasks and ordinary tests use fake providers and never run these live canaries. Failed or missing canary evidence blocks the corresponding source-bearing smoke.

The orchestrator owns temporary smoke code/artifacts under `run/live-smoke/`, not production paths. Targets are current `/home/ingmar/code/persea_content_maker_unified/api`, its sibling `web`, and the integrated ptest task/chain checkout. Root paths are harness inputs only; no per-item source paths or verdict labels are supplied to collection.

Drive production config/workspace collection → packet construction → item planning → qualified provider launches → verifier planning → assembly. Use the real stronger default policy and original bounds; skip only report publication for these external targets through a harness seam, with all outputs/state under the owned run directory. Do not invoke `doctor --json` on Persea if it would publish `recommendations.md`. Do not install target dependencies or execute its suites, configs, helpers, migrations or scripts. Set bytecode/cache state outside targets for the harness itself.

Record per target the revision/dirty status and existing report/config hashes before/after; record source unit IDs/maps, exact requests/replies privately, model/call timings, count/budget/missing reasons, and assembled public output. Verify citations against current source, scan for scope leakage, and independently assess the known file/process/network/time failure chains plus a representative OK for each resource. Report raw and audited status separately; a current source change may legitimately change a prior finding. Provider/protocol failure and missing automatic evidence are distinct smoke outcomes. Keep unknown when a consumer or failure path remains untraced. Avoid any claim that live labels, fewer unknowns, or fixture tests establish a measured accuracy rate.

## Risks and success criteria

Static JS configuration and dynamic fixture/caller relationships remain incomplete; explicit partial membership and unknowns bound the claim. A 16 MiB scan still cannot inventory every repository; saturation is visible and per-item source packs remain small. Mandatory verification raises cost/latency to at most twice the item calls; consent states that ceiling. An explicit user-selected smaller model is respected without promising equal accuracy. `opus` resolution is provider-dependent and requires its normal qualification canary.

Success means the automatic chain retrieval works on the smoke targets without manual paths, source bookkeeping no longer depends on model coordinates, every valid initial result receives bounded scrutiny, public/offline/security contracts and required ptest gates pass, and final reporting states actual evidence and remaining uncertainty. No new domain decision blocks this plan. It is ready for the independent Sol design audit.
