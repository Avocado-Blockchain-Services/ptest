# Local installation

## Published release archive

Download the release archive and its SHA-256 value from the matching GitHub
release, verify the archive locally, then extract and install it:

```sh
sha256sum -c ptest-VERSION-linux-x86_64.tar.gz.sha256
tar -xzf ptest-VERSION-linux-x86_64.tar.gz
cd ptest-VERSION
./install.sh
```

That installs an isolated, atomically switchable `ptest` at
`~/.local/ptest/ptest`. Add `~/.local/ptest` to `PATH`, or link that executable
from a directory already on `PATH`. Choose another explicitly owned location
with `./install.sh --dest /absolute/path`. The release archive contains the
exact verified ptest-ng and psutil wheels; the installer does not download or
execute remote code.

Release maintainers create the archive with
`scripts/build-release-bundle.py`, passing the built ptest wheel, the pinned
psutil wheel, compatible wheel tags, and an output `.tar.gz` path.

## Workspace-local setup

From a source checkout, create a virtual environment inside the checkout and
keep uv's download cache alongside it in the workspace:

```sh
cd /absolute/workspace/ptest
UV_CACHE_DIR="/absolute/workspace/.ptest-tools/uv-cache" uv sync --locked
export PTEST_STATE_DIR="/absolute/workspace/.ptest-state"
cd /absolute/workspace/your-project
/absolute/workspace/ptest/.venv/bin/ptest doctor --offline
```

No global installation or shell profile changes are needed. Export the variable
again in each new shell, or prefix individual commands with
`PTEST_STATE_DIR=/absolute/workspace/.ptest-state`. A `.env` file is not loaded
automatically.

The override puts `machine.toml` directly in that directory and the coordinator
database, run history, reports, and review-model cache under `coordination/`.
Read-only commands do not create it. Test execution or a consented doctor review
creates missing state directories with mode `0700`; private files use `0600`.
The parent must already exist, belong to you, and be non-writable by group/other.
An existing state directory must belong to you with mode `0700`. Symlinks,
relative paths, parent traversal, and unsafe ancestors are rejected; ptest does
not repair existing permissions. Use a supported local filesystem.

Keep state outside source repositories: a state directory at or under a
repository root is refused before admission, even if Git ignores it. Each distinct
state directory has independent concurrency limits and history: use one shared
absolute value for projects that must coordinate. Run every ptest command,
including a future `ptest uninstall`, with the same PTEST_STATE_DIR. Unsetting
the variable restores the default account locations without moving or deleting
state. An explicit `--fixture-domain` takes precedence over this variable.

This setting controls ptest storage. Project files such as `.ptest.toml` and
`recommendations.md` stay in the project; temporary scratch files use the OS
temporary directory. Claude/Codex login and cache locations follow their own
CLI settings.

## First repository setup

From the repository root, run:

```sh
ptest init
```

Initialization anchors at the Git root. A single project receives the normal
v1 `.ptest.toml`; a repository with multiple immediate native projects receives
a root v2 dispatcher and missing child v1 configs. Existing child configs are
preserved. Interactive setup can add repository-local guidance for Claude,
Codex, OpenCode, or Gemini; it never installs global skills or packages.
Claude skills land in `.claude/skills/ptest/SKILL.md` and Codex skills in
`.agents/skills/ptest/SKILL.md`, each with valid `name: ptest` front matter.
A legacy `.codex/skills/ptest/SKILL.md` is never modified or removed. Human
`ptest init` prints a boxed summary banner; `ptest init --json` emits only the
frozen v1 document and never prompts.

## Offline or enterprise installation

The installer is intentionally explicit and local-only. Prepare a wheelhouse and
version-1 manifest containing exactly one `ptest-ng` wheel and the pinned
`psutil==7.2.2` wheel, then run:

```sh
./install.sh --dest /absolute/owned/path --wheelhouse /absolute/wheelhouse --manifest /absolute/manifest.json
```

Provisioning is offline by default. `--allow-network` is reserved for an
official, hash-verified psutil wheel download. The private virtual environment
is created at its final bundle path; `complete.json` is written before the
same-directory `ptest` symlink is replaced. Failed upgrades retain the old
target. The installer never changes system Python or a live installation.
