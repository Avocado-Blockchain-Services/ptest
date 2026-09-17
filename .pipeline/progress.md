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

## Design gate passed; bootstrap handoff

- Focused Opus high review completed successfully in 30 turns, primary runtime
  claude-opus-5, verdict approved_with_notes, blocking=[]; structured evidence in
  `.pipeline/out/design-rereview-opus.json`. All NG1–9 and P1/P2 are closed.
- Astra is clarifying residual shared-parent creation, capability tier enum,
  memory unknown translation, prompt limit constant, T2 read-only ownership and
  adoption baseline traceability before T0. No scope change or runtime edits.
- Muse 1.3.0 (1.3.0-R3233.1) read-only preflight verified actual muse-spark-1.3
  and access to the 16-line Muse reference and 143-line Dan role source. Reported
  the three required passes correctly. No shell/write/web tools enabled. Trace:
  `.pipeline/out/muse-role-read-preflight.jsonl` (ignored).
- Frozen bootstrap executable resolves to
  `/home/ingmar/.local/bin/.ptest-bundles/bundle.U1mnf3/ptest`, SHA256
  `3cab209115a4f7e62be18996db77448288c1bec32218ba10f6d55bf4842fde68`.
  Installed CLI and global configuration remain untouched.

- Astra clarification a001145 integrated: explicit shared-parent creation helper,
  finite execution tier, single unknown-memory translation, bounded prompt default,
  read-only platform ownership and honest adoption-baseline traceability. No new
  product choice; Opus's passed gate stands. Regenerated T0 brief from final plan.
- Task0 Muse implementation authorized in its own task-0 worktree. Bootstrap is
  the frozen legacy target above; dependency setup is now within the passed gate.
  Root owns mechanical verification, Opus task review and chain integration.

- Task0 launch attempt1 exited2 before work: Muse requires --workspace and
  --worktree-existing to differ. Corrected launcher uses chain as origin and
  task-0 as selected worktree. Attempt2 confirms workspace task-0, actual runtime
  muse-spark-1.3; session `01a0b122-ed82-7e21-b633-78f2177449fe`, base042dde1.
  Trace `task-0-muse-run2.jsonl`; attempt1 retained, no silent retry overwrite.

- Attempt2 wrote only its untracked pyproject.toml using Muse's direct writer,
  violating the requested patch-only workflow. Root interrupted owned Muse PID
  1222435 with SIGINT; exit130, file preserved, no task tests/implementation gate.
  Muse headless session-message ingress was unavailable (external_agent_ingress_closed).
  Restart uses --disable-write so local edits must use shell apply_patch, same
  model/effort/task/base. All attempts retained; no silent substitution or reset.

- Muse attempt3 completed exit0 at c545671 (task-0 only, not integrated). Root
  scoped verification49passed/0.05s, all ten slowest<0.005s; standalone schema drift
  check exit0;26owned committed paths/clean diff check. Worker graph update exit0.
- Mechanical gate passed but root observed potential acceptance gaps; independent
  Opus Task0 audit receives exact base/head diff, root evidence, report and
  provisional notes. No wave1 dispatch until required findings are closed.

- Opus Task0 audit completed44turns, exit0, primary claude-opus-5/high, rejected:
  T0-1 path vocabulary/macOS blocker; T0-2 successful-walk FD leak; T0-3 wrong
  serialized run mode and vacuous privacy test; T0-4 incomplete frozen public
  subrecords; T0-5 untyped nesting crashes; T0-6 raw exception text exposure;
  T0-7 suspected default helper export-parent failure. Full structured report
  saved as task-0-review-opus.json. One unavailable Bash call was read-only and
  rejected; no reviewer shell/test/file mutation occurred.
- Muse repair1 receives all seven plus closely related owned protocol/storage/
  artifact/evidence notes. Regression-first, same task worktree/model/effort,
  patch-only tools. No design reopening, full suite, or wave1 dispatch yet.

- Repair1 pre-fix scratch probe at /tmp/t0r1-red/test_red.py ran through bootstrap,
  exit1:5failed/1passed. Root read raw /tmp/t0r1-red/red.log and red.exit:
  Application Support/spaced-bracketed paths fail,300nested reads leak300FDs,
  result mode is incorrectly automatic, and exception text leaks a synthetic
  sentinel. The chosen2000-depth frame probe already passed; that is NOT RED
  evidence for parser-depth failure. Require a real recursion-limit case later.
- Process deviation retained honestly: Muse used a shell heredoc to create that
  scratch probe despite patch-only instructions; subsequent source repairs use
  apply_patch correctly. Do not claim perfect patch-only compliance. Durable
  regressions and complete retained evidence remain required for acceptance.

- Repair1 ced109b completed, root independently68passed/0.12s, slowest0.02s;
  standalone schema drift check exit0. Not integrated/approved: worker correctly
  identified remaining missing public summary shapes rather than inventing them.
- Astra confirmed true omissions for ConfigSummary, EffectiveLimits, InitAction
  and history summary shape; a narrow author addendum is underway. Existing
  Capability/LeaseView/etc were already specified and repaired, not new scope.
- Split remaining T0 work into disjoint repair worktrees before the combined gate:
  original task-0 will own public contracts/schemas/test_contracts/report;
  task-0-helper (baseced109b) owns only support.py, helper tests in test_files.py
  and its unique helper-repair report. It fixes explicitly required watchdogs and
  bounded-while-reading capture/export parsing. No downstream consumers launched.

- Astra public-summary addendum2162cf2 integrated as7220fba on chain and071d4af
  in Task0. Exact ConfigSummary/EffectiveLimits/InitAction/history reuse and
  recursive public known-field projection are now explicit authority.
- Helper attempt1 was root-interrupted (ownedPID1257715,exit130), edits preserved,
  to add verified prefix-only argument ordering/result-option regression. Same
  Muse medium restarted as task-0-helper-muse-run2.jsonl; independent public
  repair has disjoint source/test/report ownership.
- Public attempt1 was root-interrupted during clean read-only preflight
  (ownedPID1261783,exit130) to fix root's erroneous generated-schema paths in its
  brief. Actual authorized paths are docs/schemas/v1/*.json and the existing
  src/ptest/runtime/protocol-v1.json, not invented schemas/public directories.
  Same Muse medium resumes with corrected brief; no source edits discarded.

- Helper repair4fe6d27 submitted; root77passed/3.33s, slowest2.01s timeout and1.05s
  retained pipe, remaining<=0.04s. Exact raw evidence lives in helper worktree
  .pipeline/local/root-helper-scoped.log. Opus narrow helper review runs in
  parallel with public-contract completion; no unapproved source integration.
- Read-only wave1 readiness check found stale T1/T3 extracts after the author
  addendum; aligned InitAction/config projection and history public-summary reuse.
  T2 unchanged. Launcher now requires exact approved repaired-T0 SHA/current plan
  section and explicitly distinguishes local build instructions from product TUI
  portability/runtime dependencies. No downstream source work launched yet.

- Public repair run2 exited0 with a read-only gap inventory and NO implementation:
  it incorrectly interpreted --disable-write as disabling shell patches. Actual
  `muse exec --help` says only non-shell filesystem writes are disabled. Root
  clarified authorized shell apply_patch and exact readable skill paths, then
  resumed the same model/effort as task-0-public-muse-run3.jsonl. Clean071d4af
  preserved; no model substitution or fabricated successful implementation.

- Helper Opus audit31turns/486449ms actualprimaryclaude-opus-5 completed with
  approved_with_notes but TWO required blocking corrections H-1(stderr cap test
  never reached),H-2(absolute export uses blocking unsafe open outside watchdog).
  Root treats any required blocking item as NOT accepted regardless verdictlabel.
  SameMusemedium helper repair2 launched, including related prefix/env/result-type/
  descriptor-error negatives; no producer source overlap with public branch.
- Review's claim that two skills were absent is REFUTED: it searched only
  /home/ingmar/.agents/skills, not the explicit existing superpowers cache paths.
  Repair brief supplies complete exact paths and requires filesystem reading.
- Public addendum dbac887 submitted; root78passed/0.13s, schema drift0,9ownedpaths,
  clean. Actual producer encode_public_document still passes raw payload/domain
  dicts; original tests only assert privacy after decode/re-encode. Root sent this
  and schema/validator parity hypotheses to Opus public T0-1..T0-6 re-review while
  independent helper repair runs. Overall Task0 barrier remains UNAPPROVED.

- Helper repair2 committed0f41c78; root92passed/2.56s, slowest1.05s post-exitpipe
  and1.01s watchdog, remaining<=0.03s. Exact raw root-helper-repair2.log under
  helper .pipeline/local. Opus focused H-1/H-2/related-diff re-review runs parallel
  to public review. No source changes integrated into chain yet.
- Additional process limitation: helper repair2 used cat for temporary patch
  files /tmp/red_fix.txt and /tmp/rep*.txt, then apply_patch for actual repo edits.
  Retained in raw trace and reviewer brief; not perfect patch-only compliance.
  Do not mistake initial malformed miniature-target RED for product evidence;
  corrected red-repair2b carries the meaningful behavioral failures.

- Public Opus review43turns REJECTED dbac887: T0-8/HIGH first-encode raw dict
  publication, T0-9/HIGH known-field/schema parity holes, T0-10/MEDIUM unhashable
  enum crashes, T0-11/MEDIUM private protocols accepting unknown fields. Original
  T0-1..T0-6 confirmed fixed. SameMusemedium correction launched with exact report
  at task0/.pipeline/local/public-review-1.json; requires independent consumer
  hostile-JSON tests so new producer sanitation cannot make decoder tests vacuous.
- Separate root-only combined-check worktree created under approved root:
  task-0-combined branchcx-ng-product-task-0-combined at5f5e501 (dbac887 plus
  helper4fe6d27->d61b142 and0f41c78->5f5e501). No changes to review worktrees.
  Own uv environment/cache; combined112passed/2.80s, schema drift0, clean diff.
  This proves composition of SUBMITTED bytes only, NOT acceptance; public findings
  remain. Normal chain/main/installed CLI are untouched by this check.

- Helper focused Opus re-review35turns approved_with_notes, blocking=[]:
  H-1/H-2 and authorized related corrections resolved at0f41c78. Root confirms
  exactSHA via git log; no extra self-referential SHA edit needed in workerreport.
  Retained notes: actualT11/macOS integration not proven, test-only post-reapPID
  residual, minor artifact/report hygiene and disclosed patch-file workflow gap.
  Helper half of Task0 is accepted; public correction and combined final gate remain.
- Combined checkout AST graph update completedexit0; two JSONschema files produce
  zero nodes (tool warning), no semantic extraction/provider. Labels/wiki not used.

- Public correction8e3d370 completed; cherry-picked into combined ascd572ec.
  Root119passed/2.72s, schema drift0, diffcheck0, ASTgraphupdate0. Final combined
  Opus gate53turns/452794ms APPROVED_WITH_NOTES, blocking=[]; allT0-8..11 resolved
  and accepted helper composition verified. Task0 is now accepted for integration.
- Nonblocking residual R1 for final integrated/Astra audit and subsequent Sol
  corrections: _run_data_schema granted_workers has minimum1 but omits maximum64;
  runtime/frozenrecord/decoder already enforce1..64. Align generated run/history
  descriptors plus a schema assertion at the next controlled contract repair;
  no runtime broadening. Do not claim 'no other drift' while R1 remains.
- Carry forward later-owner duties: T3/T11 enforce history count/byte bounds;
  T5/T6/T7 bridge codecs enforce private unknown-field rejection; T11 renders
  typed contract failures versus programmer-misuse errors as one JSON document.

- Accepted Task0 merged serially into chain at60deb4e66bb97bb6e83429c4460877419b137f0c.
  Chain fresh own uv environment:119passed/2.85s, schema check0, ASTgraphupdate0,
  clean diff. Main bae8863 and installed ptest unchanged. Merge had no conflicts.
- Wave1 AUTHORIZED and launched concurrently: tasks1/2/3 each start at exact
  base60deb4e66bb97bb6e83429c4460877419b137f0c, branchcx-ng-product-task-N and
  /home/ingmar/worktrees/ptest/cx-ng-product/task-N. Actual Muse model/effort fixed
  muse-spark-1.3/medium; separate local UV caches/venvs, patch-only shell authority.
  Traces .pipeline/out/task-N-muse.jsonl/.stderr. Root exec sessions (unordered
  launch-completion IDs)66311,89618,11923; task identity comes from per-task traces,
  not guessed output ordering. Ownership T1config/init;T2platform;T3history.

- T3 attempt1 (session11923) exited0 with empty terminal text, no tool calls and
  clean base60deb4e. Trace records provider final wire event response.incomplete,
  but runtime labelled stream_succeeded/maincompleted. This is NOT implementation
  success. Retained trace; same model/effort/task restarted as task-3-muse-run2,
  session6389. T1/T2 continue; no lost work or silent substitution.
