# Doctor accuracy design

Created: 2026-09-25
Authors: Astra architect and frontend-architect; consolidated by Codex
Base: `988616f7aef871994ecf33cc8fb41bd89b586120`. User authorized execution via `single-repo-feature-muse` on 2026-09-25. Status: APPROVED by Sol xhigh specification audit, round 2 (2026-09-25). Target: ptest only; Persea remains read-only.

This combined spec/design and two-milestone implementation plan is the current task brief. It amends the 2026-09-23 one-call-per-item contract only for the disclosed bounded follow-up, and supersedes absence-based DB/cache N/A shortcuts. It preserves the 2026-09-25 doctor-v3 amendment: no extra consent question and no changes to --fix behavior.

## Outcome and decisions

Make the existing cheap-model review useful by sending the runner's actual context, requiring narrower evidence-backed conclusions, and measuring errors with a small offline fixture corpus. Keep the provider, model-selection policy, execution runner, consent boundary, public assessment schema, and existing packet caps.

Question space E(X,Q), where X is doctor review accuracy:

| Question | Decision |
|---|---|
| What should improve? | Fewer false gaps and unsupported confirmations; missing context is visible and leads to unknown. |
| Which scope is authoritative? | The configured ptest runner, selected child/directory, and statically provable runner configuration. |
| Where does supporting code come from? | Configured setup/fixtures and their bounded local import relationships, ahead of sampled tests. |
| Can the model obtain more context? | Once per item, from omitted excerpts of the same immutable collected packet only. |
| How is accuracy measured? | Draft synthetic source cases with recorded label provenance, captured response input and explicit abstention metrics; measured model accuracy requires recorded human label adjudication and real responses. No automatic live reviews. |
| What is not being built? | No monorepo `--changed`, model ranking service, automatic model switch, JavaScript evaluator, full program analysis, or autonomous provider tools. |

Approaches considered: prompt-only changes cannot recover omitted setup; larger packets increase cost and preserve bad ordering; runner-aware bounded context plus private proof fields addresses the observed failures within current caps. Choose the third.

## Evidence from the current source

- `agent_assessment.py:_build_one_packet` caps each child at 64 files/512 KiB, each file at 64 KiB, candidate reads at 256 files/2 MiB. Tier ordering is largely alphabetical after named configs; configured setup imports are not resolved before tests consume the budget.
- `_route_excerpts` separately caps an item at 24 files/256 KiB. A runner config can miss an item entirely even when admitted to the child packet. `_encode_item_request` omits the packet's runner kind and truncation counts.
- `_skip_reason` infers DB/CACHE N/A from absence in admitted evidence, including incomplete packets. `_validate_one_row` verifies citation identity/ranges, not whether the cited code proves a conclusion.
- `executability.py` already contains a non-executing JS lexer and bounded glob expansion for Vitest smoke candidate filtering. Reuse these low-level helpers; its higher-level best-effort filtering unions all discovered Vitest config candidates and ignores unknown expressions. There is no existing single-config selector to reuse as proof of effective configuration.
- `deterministic_items.py:_select_answer` incorrectly says disabled selection makes every run full. `recommendations.py:_shell_scope` emits invalid whole-child commands such as `ptest api`; `monorepo.route_scopes` requires a path below a child. Generic regression snippets call nonexistent functions and label deterministic findings as reviewer conclusions.
- Supplied Persea evidence: API 53 files/523376 bytes/1739 truncated; web 62/523508/1994. Web's configured `src/test/setup.ts` was missing while four excluded e2e files appeared; DB-001 omitted its main Vitest config. These are motivating observations supplied by the orchestrator, not tests or model comparisons performed by this design task.

## 1. Runner-aware evidence assembly

Add focused `src/ptest/review_context.py`; leave secure file walking, admission accounting, excerpt identities, and final publication in their existing owners. The new module holds immutable context metadata and pure classification/routing helpers. Move only shared JS token/glob primitives needed by both callers to this module, with executability wrappers/imports preserving current smoke behavior. Do not create a second parser or install a parser dependency.

Conceptual internal interface:

```python
@dataclass(frozen=True)
class ReviewContext:
    runner_kind: str
    config_paths: tuple[str, ...]
    active_roots: tuple[str, ...]
    roles: tuple[tuple[str, str], ...]  # config/setup/fixture/helper/test/source
    relations: tuple[tuple[str, str, str], ...]  # from, to, local-import/fixture-use
    known_excluded: tuple[str, ...]   # safe suite paths only; bounded
    missing: tuple[tuple[str, str], ...]  # safe path/role and reason
    config_status: str  # resolved/partial/unavailable
```

`EvidencePacket` gains an internal context value with a compatibility default for existing constructors. Its canonical hash includes context. This is not a public JSON schema change. Existing packet counters retain their meanings; add precise bounded reasons in context rather than repurposing `truncated_count`.

Collection order, stable within each priority:

1. Child `.ptest.toml`, effective runner configuration, small dependency declarations. Runner configuration must be available to **every model item**, regardless of that item's regex matches.
2. Configured setup/global setup; applicable ancestor `conftest.py`; local `pytest_plugins`; their direct helper dependencies. Follow import/fixture links before filling the packet with unrelated tests.
3. A bounded sample of relevant active tests, paired with their fixture/helper definitions and applicable callers. A use-site alone must not outrank its guaranteeing or violating mechanism. Existing scanner hits remain routing hints, never conclusions.
4. Other related source and CI within remaining budget. Raw lockfile bodies are not review excerpts; retain existing deterministic dependency/lock presence facts and distinguish unadmitted content from missing files. Raw locks must not consume setup/test evidence allowance.

Preserve 64 files/512 KiB per child, 64 KiB per file, 256 candidate reads/2 MiB, 20,000 walk entries, prompt bounds and deadlines. Read configured context first and debit **all** reads to one existing per-child ledger, including parser and relation discovery reads; reuse cached bounded bytes for admission. Bound context traversal to 32 files, depth 3, and 16 outgoing links per file within those overall limits. At each cap, record an unresolved dependency; do not silently declare the closure complete. Reserve admitted configured context ahead of generic tests; if required context itself exceeds the item allowance, retain the highest-priority context and state incompleteness rather than enlarging limits.

### Runner parsing and suite scope

- Pytest: use resolved ptest config and existing addopts source selection. Read static test roots, applicable conftests, literal local `pytest_plugins`, fixture definitions/parameters, Python imports, and literal `--ignore`/`--ignore-glob` constraints. Marker/keyword filters and collection hooks are reported as project filters, not treated as static membership proofs. Parse Python through bounded AST/static parsing only; never import it. Handle relative local imports and `src/` layout; unresolved aliases/dynamic fixtures are missing context.
- Vitest: add an explicit single-config selector. For each effective scoped/full argv, honor a unique safely representable literal `--config`; without it, use the sole supported Vitest config candidate (or the sole Vite candidate only when no Vitest candidate exists). Multiple candidates, conflicting config flags or unresolved selection make that profile partial/unknown; never union their patterns into authoritative configuration. Derive scoped/full argv using existing adapter composition and inspect both `runner.args` and `runner.full_args` for all membership-changing flags, including positional filters, root/project/workspace/config selection, include/exclude and test-name filters. Unsupported or ambiguous membership-changing argv makes membership unresolved. Record differing scoped/full profiles separately; a path is conclusively excluded only if exclusion is established for every reviewed profile. Unknown argv must never classify a potentially active path as excluded. Parse literal `test.include`, `test.exclude`, `test.setupFiles`, `test.globalSetup`, local imports, and literal aliases only when unambiguous. Resolve exact existing suffixes as well as extensionless JS/TS paths. Recognize the known `configDefaults.exclude` spread without executing it. Config functions, environment conditions, arbitrary spreads, interpolation, imported computed config and unsupported globs make the relevant field partial; never treat strings inside calls or nested `coverage`/`typecheck` blocks as literal test configuration. Do not union alternate/dynamic configurations and call the result authoritative.
- A file conclusively excluded by a resolved active-suite rule is represented by a bounded path/reason inventory, not sent as an ordinary active test. A known Playwright-only test is separate from Vitest evidence. A file with unresolved membership remains `unknown-scope`; absence of parse support never proves exclusion. A helper imported by an active test can still be admitted as a helper even if its directory also contains another suite.
- Command/custom runner: retain configured scope and mark suite/config semantics unresolved; do not infer Vitest or pytest from filenames alone.
- Preserve the existing selected-directory privacy contract. A review scoped to `tests/unit` must not read/upload root setup, sibling children, or external files to complete its graph. Record `outside-selected-scope`; the normal whole-child review can collect them. No path traversal, symlink following, package import, launcher execution, dependency installation, or config execution.

A lock present on disk but not uploaded remains a presence fact, not a citable proof of its contents. Continue reporting unsupported/uninspectable dependency observations honestly. Remove only obsolete raw-lock admission and absence-based skip helpers once callers/tests are updated.

## 2. Item context, narrow verdicts, and one follow-up

Add focused `src/ptest/review_protocol.py` for request metadata, private schema/proof validation and follow-up planning. Keep public row assembly/scoring in `agent_assessment.py` and provider process control in `agent_providers.py`.

Every item request carries:

- authoritative `runner_kind`, child declaration, selected directory, configured roots and config resolution status;
- admitted config/setup/fixture relationships relevant to the item;
- packet file/byte counts and exclusions/truncations, item omissions and cut excerpts separately;
- bounded missing-evidence reasons (`not-collected`, `outside-selected-scope`, `excluded-suite`, `dynamic-config`, `unresolved-import`, `read-limit`, `item-limit`);
- role and completeness for each excerpt; omission means unknown, not absence;
- an opaque-ID inventory of eligible packet excerpts omitted from the first request (safe paths/roles/byte sizes only).

The first request uses at most **20 files/192 KiB**, reserving space within the existing final 24-file/256-KiB item cap. Mandatory runner config and configured setup/helper closure precede sampled use sites. Metadata is included in the existing prompt-input cap; if JSON encoding requires further trimming, update item omissions before hashing/dispatch.

Keep the four public statuses and existing public evidence/finding shapes. Extend the **private one-row protocol** with:

```text
proof: at most 4 {role, citation_index, quote} entries
  role = applicability | mechanism | violation | counterevidence
  citation_index indexes the response's existing evidence array
  quote is <=512 UTF-8 bytes copied exactly from that citation's lines
needs: at most 4 distinct opaque IDs from the offered omitted-excerpt inventory
```

A satisfied row needs applicability and mechanism proof. A gap needs applicability and violation proof, with the violating citation also in finding evidence. N/A needs affirmative applicability proof of why the item cannot apply. Unknown may omit proof and must name the missing fact. Invalid proof/IDs, out-of-range indexes, quotes not present in the cited line slice, stale hashes or required proof citations dropped by validation yield unknown. Model prose and quotes cannot create trusted ptest prefixes, commands, scores or execution claims. Quotes stay internal; render only existing sanitized prose/citations.

This validates a traceable argument structure and exact quoted text, **not semantic correctness**. Do not add regex claims that pretend to prove arbitrary code semantics. Preserve the evaluator/human-review distinction.

Rewrite the nine model-item prompts around explicit proof obligations:

| Item | Required distinction |
|---|---|
| FIX-001 | Freshness and assertions from a factory **and its caller**; factory naming alone proves nothing. |
| FIX-002 | Mutable lifetime and reset boundary; module-scoped immutable objects are not a shared-state gap. |
| DB-001 | Actual expensive initialization invoked per test versus per worker/run; importing a DB library or resetting records is not repeated server/schema creation. |
| DB-002 | Trace the database name/namespace helper through callers to teardown; a hardcoded prefix does not prove collision if a helper adds run/worker identity. |
| CACHE-001 | Prefix and cleanup target, including any proven disposable-service exclusivity; client construction alone is not unowned deletion. |
| RESOURCE-001 | Actual writable path/bind plus allocation and release; fixture-provided temporary paths and port 0 count as mechanisms. |
| NETWORK-001 | Active client's interception/denial boundary and configured setup; an external URL under a global mock is not proof of live traffic. |
| PROCESS-001 | Actual spawn and wait/cancel/reap path; provider/runner worker declarations alone do not prove test-created orphan processes. |
| TIME-001 | Whether a wall-clock delay/read controls assertions or synchronization and whether fake timers/setup intercept it; sleep-like names alone are not gaps. |

Prompts explicitly require checking admitted shared setup and counterevidence before a gap, and qualify satisfied conclusions to the evidence's assessed scope. Missing setup or incomplete coverage cannot establish suite-wide satisfaction/N/A. Remove DB/CACHE N/A shortcuts based solely on no matches in the packet; these items can use the model or remain unknown. This amends the v2 absence-based skip example while preserving deterministic SELECT/TIMING/PARALLEL answers.

### Follow-up state machine

1. Run the ordinary first item call. Only a valid `unknown` response with nonempty `needs` is eligible; an invalid response is not retried.
2. Resolve IDs against that item's frozen offered inventory. Reject duplicates, already-sent IDs, foreign IDs and stale identities. Add at most four excerpts/64 KiB, and only when the union stays within 24 files/256 KiB and the prompt cap. If none fit or context is not in the packet, finish unknown with the reason.
3. Send exactly one new request containing the original relevant evidence plus the added excerpts and updated limits; require a final row with empty `needs`. This is not a tool invocation or a second collection pass. At least one previously unsent excerpt must be added.
4. A failed/invalid second reply finishes that item unknown. There is no third call, new model selection, recursive request, arbitrary path read, external fetch, or deadline reset.

Disclose `N initial item calls, up to N evidence follow-ups` before the existing single consent (plus the already-disclosed model-list selection call when needed). Use the same adapter/model, qualified tool-denial argv, concurrency cap and remaining overall deadline for both phases. Offline/declined runs make zero calls. Preserve cancellation cleanup and item-local failure handling. Report actual follow-up count in existing limitation text when useful, not by inventing a public schema field.

The immutable child packet permits existing whole-packet revalidation/source-proof publication unchanged. Follow-ups can recover item-routing omissions; they cannot recover files absent from the child packet. That limitation is deliberate and visible.

## 3. Deterministic report corrections

1. Disabled standalone pytest selection: gap text says **automatic changed-input selection is disabled; explicit file/path scopes still work**. Never say every run is full. Advice still names required coverage/closed-input configuration where supported.
2. A monorepo dispatcher cannot automatically select changed tests: the child SELECT-001 answer is N/A to this unavailable dispatcher capability, with the root limitation explicit, regardless of child selection settings. Do not suggest enabling a child table will enable dispatcher `--changed`. Vitest/command unsupported cases remain explicit. Do not change dispatcher execution behavior.
3. Generate root-based verification argv from trusted scope/config facts. A known test file/directory below a child can render `ptest api/tests/...`; for a whole-child finding with no safe narrower target, render root `ptest --full`. Never `ptest api`, invented flags, or `cd` to bypass the dispatcher. Keep display quoting separate from argv validation.
4. Delete `_REGRESSION_SKETCH` and `_sketch_for` once unused. Replace imaginary functions with item-specific verification requirements using catalog `verification` as the single source of truth, plus runner-aware detail where required. Examples: network denial must exercise an unexpected request through the configured setup; ownership cleanup must preserve another owner's sentinel; fixture isolation must mutate one instance and verify the next remains independent; DB-001 must count setup calls across at least two tests without weakening assertions. These are requirements for a real regression, not claimed implemented tests. Keep observed command/cwd/exit/output unfilled and status unverified until actual execution.
5. Render `ptest configuration/history fact` for owned deterministic rows/findings and `model review — not execution proof` for model findings, using the existing guarded owned prefix. No model can assign its provenance. Keep scoring unchanged and unknown in the denominator.

## 4. Offline accuracy evaluation

Add a small fixture corpus under `tests/ng/fixtures/doctor_accuracy/` and pure evaluator `scripts/evaluate-doctor-accuracy.py` with tests in `tests/ng/test_doctor_accuracy.py`. No dependency, provider call, model choice, CLI installation or benchmark service. The script accepts an explicitly supplied directory of saved model responses; it scores files, it does not run project tests or collect private repositories.

Start with 8–10 small synthetic cases whose labels are initially drafts, not human-reviewed: Vitest global mock vs real network gap; excluded Playwright e2e; pytest fixture/helper ownership vs unowned cleanup; reusable DB setup vs per-test setup; truncated/missing setup; dynamic config. Include saturated test-tree variants reproducing the supplied Persea packet failure shapes without copying Persea source. Each case has expected status (or justified allowed set), required citation groups, prohibited conclusion codes described in its rubric, corpus/request identity, `label_review_status` (`draft` or `maintainer-reviewed`), and label provenance (author/type/date). `maintainer-reviewed` requires an actual recorded human adjudication with reviewer, date and review reference; do not generate or infer that status. Cases with decisive evidence must require a decisive status; all-unknown must not count as success.

Report counts and denominators: verdict agreement, false gaps on safe cases, unsupported satisfied/N/A on uncertain cases, missed gaps, abstentions on decisive cases, required-evidence coverage, malformed replies and follow-up use. Use the production request/validator path for normalization. Keep free-prose semantic relevance unmeasured unless an actual recorded human adjudication covers that response; evidence-group checks do not claim to understand prose. Results against draft labels are labeled draft-label agreement, never measured model accuracy.

Test the scorer with deliberately good/bad handcrafted responses labeled `synthetic`, and support separately saved real responses with provider/model/date provenance. A model's measured accuracy requires both actual supplied model responses and recorded human adjudication of the evaluated corpus labels; report draft and reviewed cases separately. This run remains unmeasured. No model ranking or claim that accuracy improved from passing mocked fixtures. CI gates deterministic context/protocol correctness and rubric scoring, not stochastic provider output. Live comparison remains optional future user invocation and is **not run automatically in this feature**.

## 5. Compatibility and security contracts

Public `ptest.agent-assessment/v1` field set, row IDs/statuses, citation structure, score semantics and ordinary test execution stay unchanged. New metadata uses the private request and existing public limitation records. Old saved public reports remain valid. Old private one-row responses lacking proof are not silently treated as fully verified current responses; fixture replay must declare its protocol version or treat them as invalid/unknown. Packet hashes naturally change because the collected evidence/context changes; do not rewrite existing reports except through normal publication. Current v3 flags/fix behavior remains out of scope.

| Security axis | Positive contract and negative twin |
|---|---|
| Identity | Existing qualified adapter/authentication stays unchanged; missing/revoked provider authentication cannot trigger fallback tools/providers or a retry loop. Provider failure becomes existing failure/unknown handling. |
| Authorization | Existing single consent covers the disclosed maximum; offline, refusal and noninteractive missing consent must launch neither first nor follow-up calls. Model IDs never authorize new source collection. |
| Tenancy | Child/worktree/scope identity is bound throughout; a web response cannot request/cite API evidence, sibling worktree state, or a directory outside selected scope. No multi-user tenancy is added. |
| Input | Bounded static parsing and exact schema/quote validation; dynamic/hostile config, deep syntax, traversal, symlink paths, forged IDs, invalid UTF-8 and oversized arrays remain partial/rejected, never executed or silently authoritative. |
| State | One frozen packet, at most one follow-up per item, deterministic catalog-order accounting; repeats cannot cause a third call, concurrent edits fail publication revalidation, cancellation cannot publish a partial stale report. |
| Exposure | Existing secret/instruction/private-file exclusions apply before context discovery and inventory; excluded private names/content never appear in offered IDs, logs or prompts. Only safe suite exclusions are listed; unavailable sources remain unknown. |
| Availability | All discovery/parser reads share existing budgets; relation fan-out/depth and follow-up calls/bytes are bounded; adversarial configs cannot reset deadlines, force unbounded recursion or amplify calls. |
| Dependencies | Filesystem reads reuse no-follow helpers; no config/project imports, shells, new parser dependency, network or installation. Provider uses existing frozen tool-denial process boundary. Existing user reports are only replaced through guarded publication. |

No axis is wholly N/A: even identity inherits the unchanged provider boundary. Database/cloud mutation and runtime multi-user authorization are N/A because the feature only reads local source and uses already-consented provider review.

Inventory existing security gates via `scripts/security-checks.py` and `tests/ng/test_security_gates.py`; do not install unrelated scanners. Reuse applicable existing gate execution through ptest; report unavailable gates honestly. Regression tests must show observed RED/GREEN for newly fixed behaviors, with current controls retained as regression evidence without fabricated RED claims.

## 6. Implementation ownership and acceptance

Use **one Muse implementation task in its own worktree, with two sequential milestones** because both touch `agent_assessment.py`. This one worker owns all files listed below and must preserve concurrent edits elsewhere. All commits remain in its assigned worktree; the orchestrator integrates approved commits to its chain only. Do not inspect or integrate concurrent `cc-doctor-v3` work as part of this task.

### Milestone 1 — context and admission

Own `src/ptest/review_context.py`, context-related `src/ptest/agent_assessment.py`, shared parser extraction in `src/ptest/executability.py`, `tests/ng/test_review_context.py`, related `tests/ng/test_agent_assessment.py` and parser regression tests. Own source fixture portions of `tests/ng/fixtures/doctor_accuracy/`.

Acceptance: main runner config and configured setup are admitted and included in every applicable item before samples; static helper/caller chains outrank generic files; excluded e2e is separate; lock bodies do not starve context; truncated/dynamic/unresolved facts are explicit; regressions cover two conflicting Vitest config candidates, explicit selection among them, membership-changing `full_args`, and unsupported membership argv preserving unknown membership; exact scope and all existing read/citation/publication budgets hold. Scoped test examples: `ptest tests/ng/test_review_context.py tests/ng/test_agent_assessment.py tests/ng/test_doctor_smoke.py tests/ng/test_executability.py`.

### Milestone 2 — verdicts, follow-up, reports and evaluation

Own `src/ptest/review_protocol.py`, remaining `src/ptest/agent_assessment.py`, `src/ptest/checklist.py`, `src/ptest/cli.py`, `src/ptest/deterministic_items.py`, `src/ptest/recommendations.py`, necessary `src/ptest/help.py`/README wording, `scripts/evaluate-doctor-accuracy.py`, accuracy labels/responses and tests. Change `agent_providers.py` only if an existing API seam cannot express reuse of current launch controls; default expectation is no changes there.

Acceptance: all nine prompts have proof obligations; quote/role validation degrades invalid claims; one useful bounded follow-up without tools/new source reads; refusal/offline/cancellation/cross-child negatives; selected model unchanged; disabled/unsupported selection wording correct; every suggested command passes the same dispatcher route validation; no imaginary regression functions or model attribution for deterministic facts; offline scorer distinguishes verdict mistakes from abstention, synthetic from real outputs, and draft from actually reviewed labels; tests forbid measured-accuracy claims without recorded human adjudication. Scoped test examples: `ptest tests/ng/test_agent_assessment.py tests/ng/test_deterministic_items.py tests/ng/test_recommendations.py tests/ng/test_agent_doctor_acceptance.py tests/ng/test_doctor_accuracy.py tests/ng/test_cli.py tests/ng/test_help.py`.

### Integration gate

Run targeted public-contract/schema drift tests through ptest, existing relevant security gate tests, then one final `ptest --full` on the integrated tip. Do not rerun solely after a fast-forward of that exact tested commit. Record exact cwd/argv/output/exit and observed RED/GREEN; no real provider comparison is part of this gate. After source changes update Graphify in changed checkouts without semantic extraction/upload, then perform the required scoped audit. No push/deploy/install replacement, cloud operation or real database migration.

## Risks and limits

- Static JS/Python parsing cannot prove arbitrary runtime behavior. Partial config/relations must remain visible; do not broaden execution or upload boundaries to eliminate unknowns.
- Packet-only follow-up cannot rescue context that exceeded collection caps. Config/setup-first admission addresses the observed omission; unresolved cases remain useful unknowns.
- Mandatory context can consume an item's sample budget; report that trade-off rather than pretending the entire suite was reviewed.
- Structured quotes improve traceability, not semantic proof. Cheap-model accuracy remains unmeasured until real responses are evaluated against labels with recorded human adjudication.
- Removing absence-based N/A shortcuts can increase initial calls. This is a deliberate correctness change; disclose actual maximum and retain current model/provider choice.
- Existing `agent_assessment.py` and CLI are large. Extract only the new context/protocol responsibilities and remove replaced dead helpers; avoid unrelated module reorganization.

## Consolidated CLI/report UX requirements

No frontend UI changes. Retain current per-project checklist, score arithmetic, reason per unknown, and detailed citations in recommendations.md. Scope limitations must say which suite was assessed and distinguish safe suite exclusions from evidence-budget omissions; never disclose names of secret/private excluded paths. Emit a concise progress event only when an additional-evidence call is actually dispatched. Deterministic findings use ptest-owned provenance; model findings say source assessment, not execution proof.

Fix all three existing selection misstatements: disabled automatic selection still permits explicit scopes; inputs not declared closed cause full fallback, not inability to widen; a configured selection policy is not execution verification. Do not implement automatic monorepo selection. Replace the blanket report statement that worker parallelism is unsupported with the specific limitation that root-monorepo live probe permutations are unsupported; use the current execution capability source.

No fabricated scoped command: validate a trusted below-child target through existing routing or fall back to root `ptest --full`. Keep missing observed execution fields and unverified status. Remove runnable-looking placeholder regression snippets; catalog verification requirements must be concrete and not demand unsupported ptest permutations.

A shared mechanism plus verified configuration attachment can support a scope-limited conclusion. Sampled passing examples cannot certify a whole-scope criterion when omitted evidence is material. Existing public rationale/limitation fields carry this information; no public schema expansion. Offline evaluator synthetic fixtures test scorer behavior only. Initial corpus labels are draft synthetic labels with explicit provenance. Draft-label agreement is not measured model accuracy. Measured accuracy requires real saved responses and recorded human adjudication of labels; this run remains unmeasured.
