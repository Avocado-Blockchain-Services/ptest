# Opus public-contract Task0 re-review

Read-only/no shell/tests/edits/delegation. Read audit-spec and common task review
contract completely; audit actual submitted bytes, not the worker's claims.
Skill /home/ingmar/.agents/skills/audit-spec/SKILL.md
Common /home/ingmar/worktrees/ptest/cx-ng-product/ptest/.pipeline/briefs/task-review-common.md

Cwd /home/ingmar/worktrees/ptest/cx-ng-product/task-0, original base042dde1,
reviewed HEAD dbac887a90c2c8f10ba03fc96f246378baf162de. Scope contracts.py,
generated public schemas/protocol, test_contracts.py, task0 report and original
T0-1..T0-6 foundation repairs (files/storage/tests unchanged sinceced109b).
Helper support.py/test_files invoke repairs run independently; EXCLUDE that
helper implementation and T0-7 from this review, do not report its old bytes as
a new failure. Root combines both independently reviewed disjoint parts later.

Read original findings:
/home/ingmar/worktrees/ptest/cx-ng-product/ptest/.pipeline/out/task-0-review-opus.json
Read current approved design, including author public-summary addendum2162cf2,
plan T0, actual contracts and report .pipeline/out/task-0.json (addendum_round).
Root ran scoped test_contracts/test_files/test_storage,78passed/0.13s,exit0,
raw .pipeline/local/root-public-scoped.log; standalone schema export --check0.
These green tests are NOT sufficient evidence that the boundaries are correct.

Root observed concrete candidate failures; confirm/refute and inspect related
boundaries so another piecemeal fix is not accepted:
- encode_public_document (around1889) STILL inserts caller data/domain dicts
  verbatim despite its allowlist docstring. Tests exercise encode -> decode ->
  re-encode then assert no sentinel; the FIRST encode leaks additive fields.
  Addendum requires producers emit onlyallowlist, consumers validate known fields
  then recursively project unknowns. Inspect this as a producer boundary, not
  merely a caller responsibility deferred to T11.
- Worker explicitly retained run.command argv/env rejection while addendum says
  same-major additive unknowns drop, including argv. Check consistency across
  all public nested records and private strict codecs. No raw exception/text leaks.
- Schema versus actual-validator parity: e.g. RunResult IDs are hex32 in schema
  but _validate_run_payload begins with _need_str only; check wrong types/bools,
  bounds/nullability/enums and unknown fields returning safe typed Problems,
  including unhashable enum inputs. Distinguish real catalog rules from wishes.
- Manual validators/projectors/generated descriptors should share one authority,
  not merely claim so in comments. Identify concrete drift/failure, not aesthetic
  demands or a speculative new framework.

Review all frozen ConfigSummary/EffectiveLimits/InitAction/history reuse semantics,
real serializers/privacy negatives, known-field requiredness vs unknown additions,
private protocol strictness and priorT0-1..T0-6 dispositions. Capture honest checked
vs not checked, no realmacOS/downstream integration claims. Return supplied JSON;
only genuine required findings, do not reopen product choices or require T1+code.
