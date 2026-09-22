# Current specification review

VERDICT: APPROVE WITH CHANGES (listed corrections applied to the draft).
Scope: written agent-doctor spec against current source and explicit user intent.
Coverage: CLI/parser modes, existing public contracts, checklist, renderer, safe
publication precedent, provider help, test ownership, consent and process boundaries.
No product execution, provider qualification, tests, or security scans: spec-only.
No git history or historical pipeline was read.

## Corrected findings

- HIGH, confirmed: the initial Bounded evidence packet section made any 64-file
  or 512-KiB collection cap a failed review, preventing useful reports on typical
  large repos. Separate partial source coverage from incomplete agent execution.
- HIGH, confirmed: Strict assessment initially required provider stdout itself
  to be one assessment object, inconsistent with local Claude JSON, Codex final
  message and OpenCode JSON-event protocols. Normalize native envelopes first;
  reject tool attempts/error envelopes/duplicate final results.
- HIGH, confirmed: task 1 added a PublicDocument kind without ownership of
  contracts.py, which owns PUBLIC_KINDS and validator/projection/schema maps
  (current source lines 27, 2746, 2976, 3028). Add ownership and legacy drift tests.
- MEDIUM, confirmed: initial command matrix omitted init --dry-run and explicit
  failure exit semantics. Freeze both; no diagnostic launch or report on preview.
- MEDIUM, confirmed: initial capability table omitted the user's parallel,
  selection and timing columns. Restore those while separating review from proof.
- MEDIUM, confirmed: nonTTY progress only reported phase transitions; a slow
  provider would be silent for minutes. Add bounded 15-second heartbeat.
- MEDIUM, confirmed: CLI ownership had init wiring in a task that did not own
  cli.py; shared orchestrator signature absent. Put wiring in task 5 and freeze it.
- MEDIUM, confirmed: marker/read/recheck/rename cannot provide an absolute CAS
  guarantee against a noncooperating same-user editor. Add cooperating-publisher
  serialization and disclose the residual POSIX window rather than overclaim.
- Clarifications: floor score with numerator/denominator; only deterministic
  facts prove execution blockers; unknown evidence never gets invented citations;
  README claim updated; real smoke source paths verified under code/scammeter.

## Remaining gates (not completed)

Written user approval; Sol-medium contract/qualification design; Muse code/tests;
all-three-provider adverse-behavior qualification; focused RED/GREEN; integrated
full suite; security gates; graph update; built-artifact copied-repo smoke.
The spec is not a claim that provider containment or the feature already works.
