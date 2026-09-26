# Doctor evidence accuracy: CLI UX constraints

Date: 2026-09-26. Read-only assessment of chain checkout `repo` at the supplied base `a64774c`; no tests, dependency installation, source changes, live review, or historical pipeline reads. Only this task-owned document is written. No browser/frontend impact: the user surface is the CLI, `recommendations.md`, and versioned JSON. Preserve the existing grid rather than redesigning it.

## Evidence and intended behavior

The approved 2026-09-25 smoke report supports a stronger reviewer plus bounded verification of initially positive conclusions. Its 27 curated judgments are neither test results nor a demonstrated automatic-retrieval accuracy rate. `gpt-6-sol` is the supported next Codex default from that smoke; fewer unknowns must not be described as higher accuracy. Do not build an evaluator.

Current source still selects Claude `haiku` and asks Codex for the “cheapest adequate” model (`cli.py:1245–1380`); README and help repeat this. Private protocol only permits `unknown` replies to request omitted evidence (`review_protocol.py:191–207`), so plausible false OKs bypass follow-up. The terminal currently counts initial plus follow-up item calls, excluding the separate model-pick call (`cli.py:2224`). The new design must replace these assumptions consistently.

## Constraints

1. **Model choice is explicit and truthful.** Preserve `--review-model` > `PTEST_REVIEW_MODEL` > product default precedence. Prefer an explicit stronger provider default over a model deciding which cheap model is adequate. Do not silently revive an old cheap-model cache or fall back to an unspecified provider default on discovery failure. Name the effective provider/model in disclosure and the existing report profile/header. An explicit weaker override remains the user's choice; do not add another consent prompt. Claude's default and qualification must be an explicit implementation decision; the Codex smoke does not establish comparative Claude accuracy.

2. **Bound verification before consent.** Recommend one initial call and at most one combined evidence/verification call per model-reviewed item, including initially `satisfied`, `gap`, and `not-applicable` answers. Missing callers/helpers and failure/timeout paths can change a plausible initial status. Verification eligibility cannot depend solely on model confidence, `unknown`, or a nonempty model request. Keep one shared deadline and concurrency cap; no recursive calls. If the design needs more phases, disclose its actual larger ceiling rather than retaining `2N` copy.

3. **The disclosure matches the complete call plan.** State initial count, maximum additional calls, total upper bound, concurrency, model, account-cost possibility, bounded source sharing, and offline escape. At most three short disclosure lines plus the existing consent prompt. If model selection consumes a call, count it in both ceiling and final actual count, including failed attempts; ideally explicit defaults remove that call. Example for a two-phase design: `codex / gpt-6-sol receives bounded source excerpts: 9 initial calls + up to 9 verification calls (18 maximum), 4 at a time.` Cost means provider/account usage may apply; do not invent currency estimates or promise “cheap.” Replace an absolute “secrets are never sent” claim with excluded-file categories and the existing honest caveat that source may contain undetected secrets. README, `doctor --help`, `help doctor`, init review help, and disclosure must agree.

4. **Evidence IDs stay private; citations remain useful.** The reviewer selects only program-issued IDs from its own admitted source pack. The program supplies path, line range, and SHA-256 for public citations. Models must not author coordinates/hashes, introduce new paths, borrow another item's IDs, or revive stale IDs. Invalid IDs are an invalid response, not a conclusion that the repository has a problem. Never expose private protocol fields or source text in JSON merely to support this implementation.

5. **Positive results have visible limits.** Keep `ok`, `gap`, `unknown`, and `n/a` cells and the existing score calculation. Near the grid, add one program-owned sentence explaining that model OKs cover the cited mechanisms and representative callers, not every test or execution behavior. The report retains per-row rationale/citations and `model review — not execution proof`; deterministic configuration/history facts remain distinguishable. A source risk is not an observed failure, orphan, or reproduced flake. No “verified suite” or runtime-safety guarantee.

6. **Unknown is informative.** Preserve genuine unresolved facts when configured scope, consumer, caller, setup, teardown, or timeout behavior cannot be established within bounds. Distinguish missing evidence/budget exhaustion from provider timeout and invalid protocol. Use the existing unknown rationale and limitation fields; no fifth public status. Reason text should put the missing fact first because `render.py:_unknown_reason` currently shows only the first model sentence. Do not group distinct uncertainty reasons under a generic “review failed.”

7. **Preserve public and offline contracts.** Keep `ptest.agent-assessment/v1`, the four status tokens, public row/finding shape, `{path,start_line,end_line,sha256}` citations, program-computed score, publication behavior, and existing JSON envelope. Use existing supported limitation codes for coverage/verification bounds. Private protocol versioning may change independently. Offline and declined reviews make zero provider/model/discovery calls. Normal tests remain model-independent. Preserve TTY reviewer selection, existing single invocation consent, automation's explicit reviewer plus `--allow-model-review`, `--fix` behavior, stdout-only machine document, stderr progress/disclosure, source revalidation, and user-edited report protection.

8. **Keep the established terminal behavior.** No information conveyed only by color. Preserve NO_COLOR/non-TTY/TERM=dumb behavior, ASCII fallback, narrow-terminal stacking, sanitized untrusted text, per-gap actionable detail, and one useful next command. Progress may say `reviewing`, `verifying evidence`, and `validating`; “verified” must not imply executed tests. All reviewer phases remain inside the original authorization.

## Acceptance checks for the implementation plan

- Assert explicit/default/environment model resolution, rejection of stale cheap-default cache policy, actual chosen-model profile, and no hidden cheaper fallback. Assert disclosure counts for multiple children, deterministic-only review, overrides, partial failures, and any retained selection call.
- A synthetic initially OK row with a missing or contradictory caller reaches bounded verification; a timeout/cancellation flaw can become a supported gap. Initial N/A and gaps receive the same evidence scrutiny. Exhausted or missing verification evidence becomes qualified unknown, never an unsupported OK.
- A valid program-issued ID produces the original exact public citation. Foreign, duplicate where forbidden, stale, fabricated, or unoffered IDs fail closed with an item-local protocol reason. Existing public JSON validates without private IDs/proof/needs leaking.
- Disclosure maximum matches the orchestration ceiling; actual header count includes every attempted provider call and never exceeds it. Follow-up shares concurrency/deadline, has no third call, and never prompts again.
- Offline/decline/zero-model-item paths start no provider process or model discovery. JSON remains a single machine document on stdout; progress and consent stay on stderr. Cancellation does not publish a misleading complete report or overwrite a user-edited report.
- Terminal and Markdown qualify positive conclusions, distinguish static risks from observed failures, identify missing evidence separately from malformed/provider replies, and retain safe useful citations. Rendering checks cover narrow/ASCII/plain output and hostile prose without redesigning the grid.
- Synthetic regressions verify mechanism and contract behavior only. Do not claim measured model accuracy or reproduce the smoke percentages as an implementation success metric. The orchestrator owns the scoped and final integrated ptest gates; this assessment runs none.

## Source pointers

- `README.md` doctor overview/model policy; `src/ptest/help.py:149–198` consent and review contract.
- `src/ptest/cli.py:1245–1435` model selection/disclosure; `1984–2104` initial/follow-up orchestration; `2224` actual count; `2240–2256` offline rendering.
- `src/ptest/review_protocol.py` proof/needs validation and opaque inventory; `src/ptest/agent_assessment.py` source bounds and unknown normalization.
- `src/ptest/contracts.py:2838–2996` public schema/status/citation contracts.
- `src/ptest/render.py:441–451,1008–1098` unknown reason and grid; `src/ptest/recommendations.py:610–671` non-gap provenance and citations.
- `/home/ingmar/worktrees/ptest/cx-doctor-smoke-20260925/run/results.md` approved evidence and scope limitations.

Next: incorporate these constraints in the current implementation brief, especially the amended model default, private-ID response shape, all-status verification policy, and honest call ceiling before builder work.
