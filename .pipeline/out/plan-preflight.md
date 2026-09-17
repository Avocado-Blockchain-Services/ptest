# Orchestrator preflight — design 97b25c9 / integrated e4ba0bd

This is execution coordination, not the independent Opus approval. All tasks are
NOT STARTED. The user approved the product specification, not skipped review gates.

## Task self-consistency and ownership

| Task | Scope checked against its tests and downstream use | Preflight status |
|---|---|---|
| 0 | Contracts/files/bootstrap; strict codecs, no-follow reads, private fixture helpers | Gate must prove real failing assertion, not only missing import. Entry CLI intentionally arrives at T11. |
| 1 | Config/init/migration; no overwrite and literal/native policy preservation | Own tests/modules only; no CLI registration. |
| 2 | Platform/account/group primitives; normal-domain read-only invariance and fixture containment | Actual macOS behavior remains unverified until platform run. |
| 3 | History/baselines/obligations; late passes, skips, pruning and corrupt history | OPEN P1: fixture state domain is not an explicit input to read/publish APIs. |
| 4 | SQLite admission/recovery; all-resource transactions and ownership cases | Guard lifecycle tests explicitly complete in T7/T11, not claimed by unit tests. |
| 5 | pytest adapter/bridge; optional xdist and native config/report semantics | Native CLI tests wait for T11; preparation tests do not promote support. |
| 6 | Vitest adapter/bridge; native specifications, additive reports, pool controls | Private fixture lock owned here; installation through candidate setup after T11. |
| 7 | Guard/control protocol; registration race and signal/quiescence fixtures | Only recorded fixture processes may be killed. |
| 8 | Source/selection; unsafe inputs widen and failures remain mandatory | OPEN P1 also affects domain-private HMAC/environment identity input. |
| 9 | Static doctor/resource guidance; no execution/read escapes and neighbor resources | Recipes here; prompt rendering owned by T11, no overlapping writer. |
| 10 | Go/Cargo/generic profiles; bounded controls and literal behavior | Native subprocess proof postponed explicitly to T11. |
| 11 | CLI/registry/orchestration/render; real profile and public output tests | Single shared registration owner; fixes outside ownership return to component owner. |
| 12 | Probes/shadow; same execution path, all attempts retained | operations/CLI ownership transfers only after T11 review/integration. |
| 13 | Packaging/deletion/security; atomic install and scanner sensitivity | Manifest ownership transfers from T0; review exact deletion inventory before removal. |
| 14 | Adoption/performance/evidence; failed samples retained and real platform limits | Genuine external suites distinct from miniature fixture domain. Root owns final full/audits. |

## Shared producer/consumer checks

| Producer → consumers | Shared interface/file check | Finding |
|---|---|---|
| T0 → T1–T14 | Typed contract catalog, safe file helpers, common fixtures | Every dependent task branches after T0 review; no concurrent contract edits. |
| T0 → T2/T4/T7 | ProcessIdentity, GroupObservation, QuiescenceProof, admission records | Author added missing GroupObservation and typed exception before frozen commit. |
| T0 → T3/T8/T11 | RunResult internal admission sequence/input references | Author froze internal sequence rather than inventing a public argv-bearing serializer. |
| T1 → T5/T6/T8/T9/T10/T11 | ConfigResolution and typed policy/runner config | Wave dependencies supply config before consumers; no duplicated parser ownership. |
| T2 → T4/T7/T11 | Canonical domain/process/group/boot observations | No terminating signal from recovered stored PID; ownership remains guarded. |
| T2 ↔ T3/T8 | Domain path/key resolution versus history/source API inputs | OPEN P1: pass explicit domain context or explain a typed, non-global alternative; cannot infer fixture from env. |
| T3 → T8/T11/T12 | Failure obligations, eligible baseline and publication | Late/incompatible/partial passes cannot erase failures; T12 uses same ledger. |
| T4 → T7/T11 | Durable grant/nonce/CAS and finalization lease | Producer has no process launch; guard cannot launch before registration wins. |
| T5/T6/T10 → T11 | PreparedRun / RunnerAdapter registry | Per-adapter ownership disjoint; only T11 registers and executes real CLI fixtures. |
| T7 → T11/T12 | Manifest/control frames and provisional facts | No second executor for probe/shadow; publication happens after quiescence under lease. |
| T8 → T11/T12 | Pure plan/source identity; queued/running invalidation | No selected result substitutes for coverage/full gate; shadow holds same lease. |
| T9 → T11/T12 | DoctorReport, recipes and installed guide | Renderer owns trusted prompt wrapper; probes are separate execution, never static scan side effect. |
| T11 → T12 | operations.py and cli.py | Sequential ownership transfer; no overlapping worktrees writing these modules. |
| T0 → T13 | pyproject.toml / uv.lock / package entrypoint | Sequential dependency ownership after all integration, then audited packaging. |
| T13 → T14 | Installed candidate / support and migration behavior | Temporary bundle only; release evidence cannot replace live CLI or merge main. |

P1 is a concrete unresolved design-interface question raised to the author while
Opus reviews; no edits requested until review findings are combined. It must be
resolved before the runnable contract barrier, with fixture history/key isolation
tests. This report does not approve implementation or claim any tests passed.
