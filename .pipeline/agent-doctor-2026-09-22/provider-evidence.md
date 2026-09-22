# Provider capability evidence (local help + official docs, 2026-09-22)

This is pre-design evidence, not proof of containment. No live audit was launched.

- codex-cli 0.155.1: `codex exec --help` supports --ignore-user-config (auth
  retained), --ignore-rules, --ephemeral, --output-schema, -o, read-only sandbox,
  --skip-git-repo-check, -C, config overrides. Top-level `codex --ignore-user-config
  features list` is INVALID; --ignore-user-config belongs to exec.
- `codex features list` confirms shell_tool, unified_exec, hooks, apps, plugins,
  multi_agent, browser_use, computer_use, code_mode_host, image_generation,
  view_image, skill_search, skill_mcp_dependency_install exist. Disabling shell
  alone is NOT proof that every other tool/source of configuration is disabled.
- Official config reference documents features.shell_tool, project_doc_max_bytes,
  project trust controlling local config/hooks/rules, web_search, and per-plugin
  controls: https://learn.chatgpt.com/docs/config-file/config-reference
- Claude Code 2.1.280 local help: --print, --output-format json, --json-schema,
  --tools "" disables built-in tools; --strict-mcp-config with explicit empty MCP
  config; --disable-slash-commands; --no-session-persistence; --safe-mode disables
  CLAUDE.md/skills/plugins/hooks/MCP/customizations while retaining normal auth
  (admin policy still applies); --restricted omits code tools and ignores normal
  settings but is not alone a no-tools guarantee. --bare disables OAuth/keychain
  and therefore must NOT be used as a subscription-auth fallback.
- OpenCode 1.18.32 local help: run --pure --format json --dir PATH --agent NAME,
  --file PATH; --pure means no external plugins, NOT read-only by itself.
- Official config docs: inline OPENCODE_CONFIG_CONTENT overrides runtime config;
  share disabled, snapshot false, autoupdate false are supported. Permissions
  default permissive and per-agent permissions override global: use explicit
  owned agent denial, not assumed plan-mode safety. Config is merged, arrays may
  retain settings; empty overrides are NOT proven isolation of MCP or plugins.
  https://opencode.ai/docs/config/
  https://opencode.ai/docs/permissions/

Recommended design direction: bounded source evidence packet reviewed via an
installed CLI in a fresh task-owned scratch directory with effective no-tools
policy and no repository/user instructions/hooks/MCP. Use normal account auth,
never print/copy credentials. Qualification must demonstrate refusal of hostile
file-read/write/command/MCP requests; unsupported combinations fail closed before
repository data is sent. Do not promise OS sandboxing based on prompts/readonly
labels. All THREE providers are the requested target; do not silently ship only
one or mark feature complete while others remain unqualified.

The native headless provider protocol and exact isolation overrides must be
frozen/qualified at the contract barrier before any feature consumer implements
them. If installed provider cannot satisfy mandatory boundary, report the exact
limitation and seek a design decision; do not invent flags or downgrade goals.

## Candidate-profile check during implementation

Current `agent_providers.py` task draft has intentionally unqualified candidates,
but its Claude `--no-slash-commands` is absent from installed `claude --help`
(the CLI advertises `--disable-slash-commands`), and `--allowedTools ""` is not
the documented `--tools ""` no-built-in-tools switch. Codex exec help does not
advertise `--no-browser` or `--no-shell`; it has `--ignore-user-config`,
`--ignore-rules`, `--ephemeral`, and read-only sandbox. OpenCode's drafted
`--agent ptest-locked-denied` requires an actual isolated agent definition;
`--pure` alone only disables external plugins. These are blockers for real
qualification, not grounds to loosen the boundary. Synthetic fake-executable
tests do not demonstrate any real CLI profile works.
