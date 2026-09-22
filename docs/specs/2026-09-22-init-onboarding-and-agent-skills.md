# ptest init onboarding and agent-skill discovery

## Purpose

Make `ptest init` a clear, trustworthy setup experience. A person should be
able to see what configuration and agent guidance were actually created,
updated, left unchanged, or deliberately not requested. A selected Codex or
Claude integration must be placed where that product discovers repository-local
skills.

This corrects the current Codex mismatch: ptest writes a Codex skill to
`.codex/skills/ptest/SKILL.md`, while Codex discovers repository skills from
`.agents/skills/*/SKILL.md`. The canonical Codex target is therefore
`.agents/skills/ptest/SKILL.md`. Claude remains
`.claude/skills/ptest/SKILL.md`.

## Scope

This change owns the human terminal rendering of successful `ptest init`, the
provider-target model used by `ptest init --agents ...` and `ptest rules`, the
generated skill metadata/content, and focused tests. It does not change
`.ptest.toml` inference, runner selection, child configuration authority,
doctor, execution, or the versioned JSON document emitted by `ptest init
--json`.

## Provider targets

| Selected provider | Canonical repository-local target |
|---|---|
| Codex | `.agents/skills/ptest/SKILL.md` |
| Claude | `.claude/skills/ptest/SKILL.md` |
| OpenCode | Preserve its existing supported ptest target |
| Gemini | Preserve its existing supported ptest target |

Every generated `SKILL.md` has valid YAML front matter with a stable `name:
ptest` and a concise provider-appropriate `description`. Its instructions point
to `docs/ptest-agent.md`, require root-based ptest commands, show child-prefixed
scopes, and require the root full gate.

`init --agents codex`, `init --agents claude`, and `init --agents all` use this
same single provider-target model. `rules --apply` remains idempotent and uses
the same model wherever it accepts provider selection.

## Legacy Codex artifact

ptest must never delete, rewrite, move, or follow an existing
`.codex/skills/ptest/SKILL.md`. If it contains the previous ptest-managed Codex
skill, a successful selected-Codex init creates the canonical `.agents` skill
when safe and reports a non-fatal migration note naming the legacy file. If it
is user-managed or unsafe, the existing safe conflict behavior remains in
force. ptest must not claim that the legacy file is active or removed.

## Successful human output

For non-JSON successful init, render a fixed, terminal-safe Unicode box banner
after every configuration and agent-rule write succeeds. The banner uses only
ptest-owned labels, status glyphs, and terminal-sanitized paths. It has these
ordered sections:

1. Header: `ptest initialized` for a creation or completed setup; `ptest
   already configured` when configuration already existed and no requested
   guidance changed; `ptest init preview` for `--dry-run`.
2. Configuration summary: root config action and every declared child action,
   using `created`, `already present`, or `would create` exactly as applicable.
3. Guidance summary: every action returned by the validated agent-rule plan,
   preserving the actual relative target. Examples include `created
   docs/ptest-agent.md`, `updated AGENTS.md`, `updated CLAUDE.md`, and `created
   .agents/skills/ptest/SKILL.md`. Existing unchanged managed files are shown as
   `already present`; a non-selected provider is not represented as installed.
4. Next steps: a child-prefixed focused command when this is a monorepo,
   `ptest --full`, and provider-specific discovery help. Codex says skills are
   automatically detected and directs the person to restart Codex or run
   `/skills` only if it is not visible. Claude gives an equivalent restart or
   skill-list refresh hint appropriate to its local-skill behavior.

The previous one-line result (`created: .ptest.toml` or `existing: .ptest.toml`) remains
inside the banner as the configuration result so existing terminal readers keep
the essential action and path. `init --json` remains byte-for-byte governed by
the existing v1 public document codec: no banner, ANSI, prompt, or extra field.

## Failure, safety, and truthfulness contracts

The banner is emitted only after `init_project` and requested `agent_rules.apply`
return successfully. Any invalid provider selection, unsafe path, conflict,
validation failure, or write failure returns the existing error and emits no
success banner. A dry run reports only planned actions and never says `created`
or `updated`.

Paths and action text are bounded and terminal-sanitized before rendering. User
file contents never enter the banner. The renderer must use the validated
`RulesPlan`/`RulesResult` rather than infer writes from provider choices. It
cannot report an AGENTS/CLAUDE/docs/skill modification that did not occur.

## Acceptance tests

- Codex selected during init creates a valid, discoverable
  `.agents/skills/ptest/SKILL.md`; it does not create the obsolete `.codex`
  skill path.
- Claude selected during init creates a valid `.claude/skills/ptest/SKILL.md`.
- Each generated skill has required front matter and root/child/full-gate
  instructions.
- Human successful output has the ordered banner, exact created/updated/already
  present action list, and suitable next-step hints for selected providers.
- Existing configuration plus newly applied guidance reports both facts; a fully
  idempotent repeat accurately reports no new writes.
- `--dry-run` reports proposed actions but does not write; `--json` preserves
  the frozen document shape and contains no banner text.
- Unsafe provider directories, symlinks, conflicting user-owned skills, and
  agent-rule write failures emit no success banner and do not create partial
  guidance.
- A legacy `.codex/skills/ptest/SKILL.md` is preserved and is called out only
  as a migration note; it is never removed or claimed as the active Codex
  skill.
- All changed behavior is exercised through scoped `ptest`; the final code
  change runs one root `ptest --full` and `graphify update .`.

## Out of scope

No global/user-level skill is installed. User-authored content is preserved.
No automatic migration is performed. No color scheme, terminal capability
probing, external network call, LLM launch, dependency installation, or tool
trust-setting change is added to init.

## Review resolutions — 2026-09-22

The current-session Astra review and Terra review clarified these executable
contracts before implementation:

- `init --json` is non-interactive even on a TTY; absent explicit `--agents`
  means no agent selection. Explicit `--agents all` uses the same closed list
  as the interactive all choice. The JSON codec/key sets remain unchanged.
- `rules` keeps its existing no-argument/`--apply` CLI grammar. It manages base
  guidance; provider selection stays on `init` and shared Python helpers.
- Internal per-target outcomes carry config and guidance states to the banner.
  Record child creation at write sites, unchanged states during validation,
  and planned states during dry-run. Do not infer writes from final existence.
- Recognize legacy skill content by exact bytes from the released
  `_provider_text` template (heading `# ptest skill for PROVIDER`, followed by
  its two original instruction paragraphs). At canonical Claude/OpenCode/Gemini
  paths, upgrade only this exact generated template to the valid new format.
  Edited content conflicts. The old Codex path is always preserved. Inspect
  it with bounded no-follow reads; reject unsafe or user-edited legacy entries
  before any init writes when Codex is selected.
- Preflight all guidance writes and restore owned guidance mutations on an
  ordinary mid-apply failure. Preserve original bytes and permissions for
  updated files; remove only files/directories created by this invocation.
  Never overwrite a concurrent edit during rollback. If safe restoration is
  impossible, return an explicit failure explaining incomplete restoration.
  No crash-atomic multi-file transaction or config rollback is promised.
- Invalid existing config warnings render an attention-needed header rather
  than an initialized/configured success claim; do not silently hide warnings.
- Skill bodies identify guide paths relative to the repository root, not the
  skill directory. Give standalone scopes when the root config is v1; use
  child-prefixed scopes only with declared monorepo children.
- Native smoke uses a wheel installed in a fresh venv, isolated initialized
  Git repositories, and new Claude/Codex low-effort sessions. Retain the host's
  catalog or actual skill-load tool evidence. A model assertion alone is not
  sufficient. These model calls are verification only, never part of init.
