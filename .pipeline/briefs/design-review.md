Review the ptest NG detailed design and implementation plan against the approved
product specification and actual repository constraints. Use Claude Opus at high
effort; no delegation or model substitutions. Follow audit-spec by reading
/home/ingmar/.agents/skills/audit-spec/SKILL.md. This is a read-only design gate:
no file edits, tests, dependency installation, shell execution, or external writes.

Read AGENTS.md, .pipeline/context.md, README.md, the full approved
docs/specs/2026-09-17-ptest-ng-product.md, then:
- docs/designs/2026-09-17-ptest-ng-design.md
- docs/plans/2026-09-17-ptest-ng.md
- .pipeline/out/design-author.json
- docs/research/2026-09-17-runner-capabilities.md
- docs/research/2026-09-17-scheduler-feasibility.md
- docs/research/2026-09-17-agent-interoperability.md

Inspect existing source/install/test conventions where relevant. Legacy cloud
architecture is migration input, not a requirement to preserve cloud runtime.
Product scope is settled; do not relitigate local-only, platform support tiers,
no model APIs, ordinary CLI interoperability, or user-approved model routing.
The prototype is candidate reuse, not authority. User approved implementation
after this design gate, not live installation/main merge/publication.

Check concrete feasibility and contracts: argv/worker limits including config/env
precedence and serial without optional plugins; canonical shared domain and safe
owned-process recovery; fail-closed capacity under crashes; fixture-domain/nested
tests; correct baseline/input identity and durable failure obligations; bounded
read-only doctor and repair evidence; portable initialization/migration/install;
schema/CLI/default/version precision; resource caps; no false gate or unsupported
platform claims; negative tests and evidence for A1-A14.

Check task decomposition is executable: exact disjoint file ownership, frozen
helpers/shared registration points, runnable bootstrap barrier, realistic tests
through installed legacy ptest without replacing it, audit/integration ordering,
and honestly blocked release evidence. Identify duplicated/obsolete logic and
unnecessary abstractions with concrete consequences, not taste preferences.

Return the supplied review JSON schema. Put required corrections in `blocking`
(severity reflects consequence). An empty list is valid. Every finding needs a
specific location, traced failure scenario, and actionable fix; try to refute it
first. Distinguish suspected risks from confirmed contradictions. Do not invent
problems for audit volume. State what was not checked; no tests run at docs gate.
