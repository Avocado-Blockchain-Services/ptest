# Pipeline ledger

## 2026-09-17 — setup

- User approved the adapted single-repo workflow and aggressive independent parallelism.
- Final audit order: Opus integrated audit, Astra xhigh integrated audit, then Sol
  repair/re-review if necessary.
- Chain worktree: /home/ingmar/worktrees/ptest/cx-ng-product/ptest, branch cx-ng-product.
- Base: bae8863. Main clean; provisional cx-ng-foundation branch/worktree preserved.
- Current stage: specification and parallel compatibility/runtime-preflight research.
- No implementation, suite runs, dependency setup, live installation or main merge
  authorized for this stage. Next human gate: reviewed product specification.

## Runtime preflight

- Astra xhigh spec author and an independent Astra xhigh TUI-compatibility researcher
  dispatched concurrently in separate spec/compat worktrees; docs-only ownership.
- Claude Fable requested as `claude-fable-5-1` with `--effort high`; isolated
  no-tools READY probe succeeded, modelUsage confirms claude-fable-5-1.
- Claude Opus requested via `opus` alias with high effort; no-tools READY probe
  succeeded, current alias resolved to claude-opus-5.
- Muse no-shell/no-write/no-web READY probe succeeded; configured Meta model is
  muse-spark-1.3. Untrusted workspaces skip AGENTS.md, so actual assigned worktrees
  must explicitly use --trust-workspace. No role inheritance is assumed.
- All probes exited zero. No product tests were run; these are tool-availability
  probes, not product verification. No credentials were read or printed.
- Follow-up trusted Muse probe completed with the exact heading of the assigned
  AGENTS.md despite no tools: `# ptest NG product workflow`. Rule ingestion is
  verified in that mode. This does not verify the future product or native role
  overlays. Selected results: .pipeline/out/runtime-preflight.json; raw trusted
  trace retained locally (ignored) in .pipeline/out/muse-trust-preflight.jsonl.
- Fable spec audit brief and structured verdict schema prepared. Runtime primary
  aliases are pinned in evidence; Fable will run read/search tools only, no shell.

## Parallel specification inputs completed

- TUI compatibility research: worker commit 17e44ed, integrated as 6059a7b. Six
  families covered using primary documentation and bounded local Muse evidence.
- Product specification: Astra xhigh worker commit feb2b680, integrated as
  05736da. Owns docs/specs/2026-09-17-ptest-ng-product.md and author manifest.
- Documentation validation passed (JSON parsing, whitespace and ownership checks).
  No product tests, dependency installations, or runtime edits in this stage.
- Fable 5.1 high specification audit launched against this immutable draft with
  only Read/Glob/Grep tools. Output: .pipeline/out/spec-review-fable.json.
- Human gate remains pending: reviewed product spec approval. The Linux/macOS
  preference question is optional; proposed default remains Linux+macOS v1.

## Fable specification review — round 1

- Completed with requested claude-fable-5-1/high; 23 turns, no permission denials,
  exit 0. Raw structured result retained at .pipeline/out/spec-review-fable.json.
- Verdict label: approved_with_notes. Required-correction array contains F1–F3,
  so the spec gate is NOT treated as passed yet.
- F1: admission-domain identity / nested ptest fixture deadlock.
- F2: changed-during-run/full-gate semantics and separating outputs from inputs.
- F3: define implementable command redaction and honest residual limitations.
- Astra xhigh author resumed to repair/disposition the findings and actionable
  clarification notes; no code or tests. Suggested fixes are not automatically
  accepted when they weaken correctness (notably exempting tracked snapshots or
  returning a misleading full-gate success on changed source).
- Next: scoped Fable re-review of the repair, then present spec for user approval.

## Astra specification repair

- Worker commit 1370334 integrated as e87e58c; exact dispositions in
  .pipeline/out/spec-repair-astra.json. Only specification/author-report files changed.
- F1: canonical host/account domain; immediate same-domain nested execution error;
  explicit isolated synthetic/miniature fixture domain, never silent env activation.
- F2: changed relevant input invalidates automatic/full evidence (child 0 -> final
  70); original child failure codes retained; only non-input outputs are exempt.
  Snapshot/source/expectation updates require a subsequent unchanged full gate.
- F3: default command summaries omit arbitrary argv; explicit local unredacted
  reveal is warned, not retained, and never includes an environment dump.
- Clarified initialization/migration, cwd, signals, guide availability, scan order,
  coverage-failure obligations and side-by-side legacy limits. Smaller arbitrary-
  wrapper reservations remain deliberately deferred with a rationale.
- JSON parsing and whitespace checks passed; scoped ownership inspected.
- Repair-only Fable 5.1/high review launched with read/search tools and streamed
  local trace .pipeline/out/spec-rereview-fable.jsonl; no tests or source changes.

## Specification review gate passed; awaiting human approval

- Focused Fable re-review completed: requested claude-fable-5-1/high, 12 turns,
  exit 0, no permission denials. Verdict approved_with_notes; blocking list empty.
  Portable result: .pipeline/out/spec-rereview-fable.json. Raw trace kept locally.
- F1, F2, F3 each resolved; reviewer explicitly accepted Astra's alternatives.
- All actionable N1–N9 clarifications resolved or deliberately dispositioned;
  the smaller arbitrary-wrapper reservation was a preference and remains parked.
- Two nonblocking wording observations remain: T3 could repeat T4's existing rule
  that failures survive an invalidated run; P2's pre-init command list could repeat
  the guide availability already explicit in section 9/A1. No requirement gap
  was established. Carry those references into design, not another speculative loop.
- Orchestrator updated only the spec's status line to reflect the passed review;
  no substantive contract edits after the reviewed repair.
- Next required action is USER APPROVAL of the reviewed product spec. Do not begin
  detailed design, Muse coding, code removal, installation, main merge, or release
  on the strength of this report alone. Linux+macOS remains the proposed v1 scope;
  native Windows is deferred, and license/publication decisions remain M5 gates.

## Human specification approval; design started

- User explicitly replied "approved!" to the reviewed specification on 2026-09-17.
- Product contract is now approved, including Linux/macOS first-release scope.
- Proceeding with Astra xhigh detailed design/contracts/task plan and independent
  parallel design research, then Opus review before Muse/Dan implementation.
- No runtime edits, tests, dependency installation, or legacy removal at this gate.
- Final integrated review remains Opus then Astra; subsequent corrections use Sol.

## Design dispatch and implementation-runtime preparation

- Astra xhigh architect owns design + plan in `cx-ng-product/design`; independent
  Astra runner and scheduler researchers own documentation in `runners` and
  `scheduler-research` worktrees. All started from approved-spec commit 1ca3223.
- Prepared `.pipeline/briefs/design-review.md` for read-only Opus gate and
  `.pipeline/briefs/muse-dan-common.md` translating the actual Dan role, with
  project-specific test/worktree/ownership rules overriding generic examples.
- Muse 1.3.0-R3233.1 local help and offline schema export inspected. `exec --agents`
  accepts overlays, but an offline negative unknown-field probe was also accepted;
  parsing alone is NOT proof of native role loading. Implementation will receive
  Dan's actual source and explicit translated instructions in its prompt. The
  previously observed trusted AGENTS loading and actual muse-spark-1.3 model
  preflight remain the evidence, not an assumed Codex-role import.
- No live credentials were read; offline probes made no provider requests.
- Legacy checkout contains no Python package/lock or existing `.github` security
  gates; gitleaks/bandit/osv-scanner were absent from PATH in read-only inventory.
  Tool availability: uv 0.9.26, Node 26.8.2, npm 12.0.2, Go 1.27.1, Cargo 1.98.1.
- No feature code, dependency installs, tests, cloud operations, or live CLI edits
  at this documentation gate. Provisional foundation remains untouched.

## Parallel research integrated

- Scheduler author fca24b8 integrated as 46e2301; runner author b673239 integrated
  as b0371c2. Both changes contain only their two owned research/report files.
  JSON parse and whitespace checks passed; no runtime tests were run or claimed.
- Scheduler recommendation uses cooperative foreground groups, a guarded launch
  handshake, and a shared SQLite transaction ledger. Arbitrary unobserved detached
  descendants are explicitly outside portable containment; uncertainty holds slots.
- Runner input recommends native parsed-config bridges, additive reporting,
  optional serial pytest without xdist, exact supported profiles and full fallback.
  Locally inspected Vitest 3.2.6 is not promoted to tested support; maintenance
  candidate 3.2.7 needs actual acceptance evidence.
- Focused independent adoption search found MIT `marshmallow-code/apispec` at
  bfac55c9bfbc4edbdde505d29d8c20c62133fc10, with uv.lock pytest 9.1.1. Proposed setup:
  `uv sync --locked --no-default-groups --group tests --extra yaml --extra marshmallow`.
  Its no-xdist environment is useful serial-path evidence, not parallel evidence.
- Node fallback `dcastil/tailwind-merge` v3.3.1 at
  71218a58edc9e5ad36e5a3233b51c893782a411d has meaningful pure unit tests but uses
  Yarn 1.22.22 and locked Vitest/coverage 3.1.4, so it is NOT qualified for the
  proposed frozen adapter matrix. Matching Vitest adoption remains unresolved.
  No candidate was cloned, installed, executed, or represented as passing.

## Frozen design submitted to Opus

- Astra author commit 97b25c9 integrated as e4ba0bd: design, 15-task plan and
  structured handoff only. JSON/whitespace/owned-file checks passed; no tests.
- Root preflight caught and Astra resolved changed/history CLI grammar, auxiliary
  record fields, exception/sequence/group-observation types, explicit PTEST path
  override errors, and fixture root/identity containment. These are contract
  clarifications, not a change to the approved product requirements.
- Opus high design audit launched read-only against this frozen integrated tree,
  using `.pipeline/briefs/design-review.md` and the existing JSON schema. Trace:
  `.pipeline/out/design-review-opus.jsonl`. Runtime source remains legacy while
  review runs; no implementation task is authorized before required corrections.

## Opus design gate — corrections required

- claude-opus-5/high completed25 turns, exit0, no permission denials; incidental
  Haiku metadata usage is not the primary reviewer. Verdict rejected with NG-1–9
  required findings (3HIGH,6MEDIUM). Full result and extracted findings are in
  `.pipeline/out/design-review-opus.json` and `design-review-findings.json`.
- Required: legacy state-root collision; default unknown-memory serialization and
  missing mixed-runner benchmark; shared write/default/control-frame contracts;
  register JSON alias/schema; execution-vs-selection version degradation; private
  installation/dependency provisioning; queue default; invalid example ID; a
  vacuous doctor assertion. Root stat/length checks confirm concrete NG-1/NG-8 facts.
- Root preflight additionally records P1 explicit domain routing for history/source
  and P2 literal runner-value parsing. Combine these with reviewer findings for one
  Astra repair pass; do not patch runtime code or bypass review.
- Reviewer notes on lock generation, adoption nesting, project Python versions,
  doctor benchmark completion, package-data/ownership, shared storage helpers and
  explicit watchdogs also require dispositions. Alternatives must preserve the
  approved safety contract; untested versions cannot simply be called bounded.

## Design repair integrated; focused re-review running

- Astra repair44c7a9e integrated as e1e4a2b; owned design/plan/report only.
  Dispositions in `.pipeline/out/design-repair-astra.json`; repair-only diff in
  `.pipeline/out/design-repair.diff`. JSON/whitespace checks passed, no tests.
- Research2f6c5e7 integrated as e386c8b: finite basic-serial qualification candidates
  and verified package metadata. Candidate API inspection is not runtime support.
  Gitleaks issue2170 was closed with an intentional placeholder-token allowlist
  explanation; removed an unsupported implication of a blanket scanner regression.
- Repair covers NG1–9 plus P1 explicit state domains and P2 prefix-only parsing.
  Native capability checks remain mandatory; compatibility candidates still need
  real negative tests. Default memory estimates remain unknown, no fake measurements.
- Shared private write/SQLite/default/frame contracts now belong to T0. Task domains,
  constructor examples, package-data ownership, lock generation and independent
  adoption-vs-nested-fixture execution are explicit.
- Opus high focused re-review launched with `.pipeline/briefs/design-rereview.md`;
  trace `.pipeline/out/design-rereview-opus.jsonl`. T0 remains NOT STARTED until
  the required review corrections are closed.
