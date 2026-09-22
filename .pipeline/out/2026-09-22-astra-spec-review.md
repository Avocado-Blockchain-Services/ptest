VERDICT: APPROVE WITH CHANGES
Reviewer: current session, following the user's switch to Astra.
Scope: init-onboarding spec d2d541b versus current cli.py, config.py,
contracts.py, agent_rules.py and init/agent_rules CLI regression tests.
Coverage: contracts, compatibility, upgrade behavior, filesystem safety,
report truthfulness, and end-to-end discovery requirements. No tests run.

Confirmed findings and resolutions for implementation:

1. agent_rules._provider_target accepts only exact current template bytes.
   Adding front matter would reject every pre-existing generated Claude skill.
   Permit upgrades only of exact released ptest templates in canonical paths;
   preserve any user-edited skill and preserve the legacy Codex file entirely.
2. config.init_project returns no per-child action details; CLI discards
   RulesResult. Record internal config actions at the decision/write sites and
   carry actual rules actions/unchanged targets to the renderer. Keep the public
   init JSON allowlist unchanged; never infer creation from post-init existence.
3. cli._init_agents still prompts for init --json on a TTY; parser rejects
   --agents all although the spec includes it. Suppress JSON interaction and
   accept all through the same closed provider mapping as the interactive choice.
4. Existing invalid config returns an EXISTING result plus warnings. Render an
   attention-needed state instead of an initialized/configured success claim.
5. The spec's no-overwrite sentence conflicts with managed AGENTS/CLAUDE updates.
   It means preserve user content; only recognized ptest content may change.
   Test an existing CLAUDE file, alias AGENTS -> CLAUDE, legacy skill upgrade,
   edited skill conflict, dry-run, and a repeated invocation.
6. Multi-file guidance writes currently have no rollback. Preflight all paths;
   roll back owned changes on an ordinary mid-write failure without clobbering
   concurrent edits. Config transactionality is outside this feature's scope.
7. Filesystem assertions cannot prove skill discovery. Launch separate Claude
   and Codex low-effort sessions in isolated initialized repos and retain native
   catalog/tool evidence of loading ptest, with no SKILL path in the question.

The earlier gpt-5.6-astra subprocess was a failed model-ID attempt and did not
review this spec. The current-session review above fulfills the requested one
Astra spec review; implementation audits use Terra.
