# Design: startup update check and `ptest update`

Date: 2026-09-29. Base: `feature/update-check` @ 72bca43 (ptest 0.3.7).
Spec (authoritative for intent):
`/tmp/claude-1000/-home-ingmar-code-tools-ptest/e8898983-6043-4619-a933-8d5abd5c42c4/scratchpad/update-check-brief.md`.
Context pack: `.pipeline/context-pack.md`. This design is authoritative over
the context pack where they differ. The differences are listed in section 1.

Two tasks run in parallel from the same base in separate worktrees:

- **T1** writes all the code: `update.py`, CLI wiring, help, the contract/schema, and tests.
- **T2** writes the docs: README, installation, changelog, the agent guide, and the guide-hash entry.

Neither task imports anything the other one writes. The only link between
them is user-visible text: T2's docs quote strings that T1's code prints.
Those strings are frozen in section 3.

---

## 1. Corrections to the context pack

These corrections are binding. The file ownership lists in section 6 already include them.

1. **The agent-guide row belongs in `src/ptest/resources/repository-agent-guide.md`,
   not `agent-guide.md`.** `docs/ptest-agent.md` is a byte-for-byte copy of
   `repository-agent-guide.md`: `ptest init` writes it and
   `agent_rules._guide()` reads it. `agent-guide.md` is the `ptest guide`
   repair text and has no output table. T2 owns
   `repository-agent-guide.md` and leaves `agent-guide.md` untouched.
2. **A change to the guide requires adding the old guide's hash** to
   `agent_rules._PREVIOUS_GUIDE_SHA256S`. Without it,
   `test_every_shipped_guide_version_hashes_into_previous_set` fails once the
   change is committed, and every repository that holds the 0.3.6/0.3.7 guide
   gets `already-exists` from `ptest rules`/`init`. The precedent is commit 5159175. T2 owns
   `src/ptest/agent_rules.py`, but only for that one entry, plus
   `tests/ng/test_agent_rules.py` for one regression test.
3. **The guide is exactly 100 lines, and tests cap it at `<= 100`**
   (`test_resources.py`, `test_agent_rules.py`, `test_init_changed.py`). T2
   adds one table row and joins two lines elsewhere (section 5.1), so the
   total stays at 100.
4. **`tests/ng/conftest.py` goes to T1.** The autouse `isolated_env` fixture strips every
   `PTEST_*` variable, so the suite cannot inherit an opt-out. The startup
   check would otherwise reach the network from hundreds of in-process
   `cli.main()` tests. T1 adds the opt-out and a network deny guard there
   (section 4.6).
5. **`scripts/install.py` goes to T1, as a single edit.** Its smoke check runs the
   *new* bundle's `ptest guide`. `guide` is not exempt from the check. With a
   TTY, and with an older `--version` or a pinned `get.sh` install, the check would
   prompt, or reach the network, in the middle of an install. T1 adds
   `PTEST_NO_UPDATE_CHECK=1` to the environment of the three smoke
   `subprocess.run` calls. `test_install.py`'s fakes accept `**kwargs`,
   so nothing breaks.
6. **`src/ptest/runtime/protocol-v1.json` should not change.**
   `PROTOCOL_V1_DESCRIPTOR` does not list public kinds. T1 still runs
   `scripts/export-schemas.py`. The only expected new file is
   `docs/schemas/v1/update.json`.
7. **The JSON schema is T1's work, even though the brief's triage note put it in the docs task.**
   The schema is generated from `contracts.py` and drift-checked there. T1 owns help,
   contract and schema, which matches the triage the orchestrator produced.

---

## 2. Decisions

- **One module.** `src/ptest/update.py` owns version parsing, latest-version resolution,
  the cache, the network, verification, extraction, running the installer,
  layout detection, the startup check, rendering, and the document payload.
  `cli.py` only parses arguments and calls it. It follows the precedent of `uninstall.py` and `_run_uninstall`.
- **Layout detection reuses `uninstall.py` helpers**, which are imported and
  not copied, so there is a single source of truth: `uninstall._root_from_binary`,
  `uninstall._is_bundle_dir` and `uninstall._public_link_in_bundles`.
  `uninstall.py` itself does not change.
- **Reuse the installer instead of reimplementing it.** Run the extracted `ptest-<v>/install.sh`
  directly, the same way `get.sh` does. `PTEST_PYTHON` comes from
  `uv python find --system '>=3.11,<3.15'`. Always pass `--dest <install root>`,
  where the root is the realpath of the running install. Omitting `--dest` would let a changed
  `$HOME` install somewhere else. For the default root, the path matches
  `$HOME/.local/ptest` as text, so `install.sh` also refreshes
  `~/.local/bin/ptest`, exactly as with `get.sh`. `install.py` already
  publishes the new bundle side by side and swaps `<root>/ptest` atomically
  with `os.replace`. It never deletes old bundles.
- **Network access happens only through a `Transport` seam** (section 3.2). The real
  transport uses urllib with an allowlist check on every redirect. Tests
  inject fakes. No environment variable can change the base URL.
  Standard proxy variables are still honoured, because TLS to `github.com` is
  verified end to end through a CONNECT tunnel, so the destination cannot be
  redirected.
- **Latest-version resolution** requests `/releases/latest` *without following
  the redirect*. It reads `Location`, resolves it against the request
  URL, and requires an exact match of
  `https://github.com/Avocado-Blockchain-Services/ptest/releases/tag/v<X.Y.Z>`.
- **A hard 2 s bound** for the startup fetch comes from a daemon thread plus `join(2.0)`.
  Socket timeouts alone cannot bound DNS lookups or a slow trickle of data. If the thread
  is still running at 2 s, it is abandoned and treated as a failed fetch.
- **Claim-then-fetch cache.** When the cache is stale, the check first
  writes `checked_at = now` and keeps the old `latest`, then fetches. Concurrent
  agents see a fresh claim and skip the fetch. A failed fetch backs off for 24 h, so being
  offline never costs 2 s on every command. **The fetch happens only if the claim write
  succeeded.** If there is no usable state area, the check never fetches.
- **`-q` turns off the whole check**, not just the output line. That means no output and no fetch.
  The prompt never appears under `-q`.
- **`--json` with a TTY prints the one-line notice on stderr and never prompts.**
- **The source layout never prompts.** It prints the notice line with
  the source hint, both on and off a TTY.
- **`ptest update` itself never prompts.** An explicit command is
  consent. This is what agents run.
- **`--check` and `--version` together are rejected** as `invalid-config`.
- **Source layout refuses every form of `ptest update`, including `--check`.** It returns
  `not-install-layout` with exit 2, before any network access.
- **Offline or unreachable during `ptest update`** returns `update-unavailable`,
  retryable, with exit 75. Every other update failure is `update-failed`, exit 2.
- **An EOF at the prompt means decline.** The prompt reads a line with `sys.stdin.readline()`,
  not `input()`, because `input()` writes to stdout. An empty
  string (EOF) declines. `"\n"`, `y` and `yes` accept. Anything else declines.
- **Ctrl-C at the prompt, or during a prompted update,** prints a newline and
  raises `SystemExit(130)`. The user cancelled, and the command never ran.
- **A failed prompted update records a decline** for that version for 24 h, so a
  broken release cannot nag on every command.
- **No re-exec loop guard through the environment.** Changing the environment would leak into runner
  children. After a successful update the new process runs the target version,
  and the cache says `latest == target`, so it prints nothing.
- **The update-check cache survives `ptest uninstall`.** It is shared machine state, like
  `machine.toml`. This follows the existing uninstall contract ("shared records are never
  touched").
- **Spec kept as written: `uninstall` is not exempt from the check.** A human running
  `ptest uninstall --self` at a TTY may be asked to update first. That wastes time but does no
  harm. This is recorded as an open question, not implemented as a deviation.

---

## 3. Frozen interfaces

### 3.1 User-visible strings

T1 prints these strings byte for byte, and T2 quotes them. `X` is the newer or target
version and `Y` is the running version, both validated `N.N.N`. The long dash is
U+2014 (`—`).

| Id | Where | Exact text |
|---|---|---|
| S1 prompt | stderr, no newline, then flush | `ptest X is available (you have Y). Update now? [Y/n] ` |
| S2 agent notice | stderr, one line | `ptest: update available: X (installed Y) — run: ptest update` |
| S3 source notice | stderr, one line | `ptest: update available: X (installed Y) — installed from source; update it with git pull` |
| S4 source hint (`SOURCE_HINT`) | Problem message | `installed from source; update it with git pull` |
| S5 up to date | stdout (`ptest update`) | `ptest is up to date (Y)` |
| S6 check available | stdout (`ptest update --check`) | `ptest X is available (installed Y) — run: ptest update` |
| S7 updated | stdout (`ptest update`) | `ptest updated to X (was Y)` |
| S8 prompted update ok | stderr (startup) | `ptest: updated to X (was Y)` |
| S9 re-exec impossible | stderr (startup) | `ptest: updated to X; the update takes effect on the next run` |
| S10 prompted update failed | stderr (startup) | `ptest: update failed: <problem.message>; continuing with Y` |
| S11 download progress | stderr (`ptest update`, prompted) | `ptest: downloading ptest X for <os>-<arch>` |

Refusals go through the existing `_emit_error` and print as
`<code>: <message>` on stderr, or as an error document with `--json`:

| Code | Exit | Message (exact where given) |
|---|---|---|
| `not-install-layout` | 2 | S4 |
| `update-unavailable` (retryable) | 75 | `could not reach the ptest releases on GitHub` |
| `update-failed` | 2 | `checksum mismatch for ptest-X-<suffix>.tar.gz; the current install is unchanged` (other `update-failed` messages are free text but must end with `; the current install is unchanged` when nothing was switched) |
| `invalid-config` | 2 | `--version must be a release number like 0.3.7` / `--check and --version cannot be combined` / `unknown inspection option` |

The `update-failed` message for a missing release: `no ptest X release for <suffix>`.

### 3.2 `src/ptest/update.py` public surface (T1)

```python
REPOSITORY = "Avocado-Blockchain-Services/ptest"
RELEASES_URL = "https://github.com/Avocado-Blockchain-Services/ptest/releases"
LATEST_URL = RELEASES_URL + "/latest"
ALLOWED_HOSTS = frozenset({"github.com", "objects.githubusercontent.com",
                           "release-assets.githubusercontent.com"})
CHECK_INTERVAL_S = 24 * 3600
CHECK_TIMEOUT_S = 2.0            # startup wall-clock bound (thread join)
RESOLVE_TIMEOUT_S = 10.0         # explicit `ptest update`
DOWNLOAD_TIMEOUT_S = 300.0       # total per file, monotonic deadline
INSTALL_TIMEOUT_S = 900
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_SHA_BYTES = 4096
MAX_EXTRACT_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 10_000
CACHE_NAME = "update-check.json"
CACHE_MAX_BYTES = 4096
SOURCE_HINT = "installed from source; update it with git pull"

@dataclass(frozen=True, slots=True)
class Transport:
    # timeout_s -> raw Location header of LATEST_URL (redirect NOT followed)
    latest_location: Callable[[float], str]
    # (url, sink, max_bytes, timeout_s) -> None; writes the body to sink.
    # Real impl: HTTPS + ALLOWED_HOSTS on the URL and on every redirect
    # (max 5), no userinfo, port None/443, Content-Length and streamed byte
    # cap, monotonic deadline. Raises C.Problem or OSError.
    download: Callable[[str, BinaryIO, int, float], None]

@dataclass(frozen=True, slots=True)
class Layout:
    root: Path        # realpath of the install root (holds .ptest-bundles and ptest)
    bundle_id: str    # running bundle dir name

@dataclass(frozen=True, slots=True)
class UpdateResult:
    running_version: str
    target_version: str
    action: str       # "up-to-date" | "available" | "updated"
    check_only: bool
    install_root: Path | None

def default_transport() -> Transport: ...
def valid_version(text: object) -> str | None:
    """str matching re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", text, re.ASCII),
    len <= 32, else None. No leading 'v', no whitespace, no unicode digits."""
def is_newer(candidate: str, current: str) -> bool:   # int-tuple compare
def platform_suffix() -> str:
    """os.uname(): Linux->linux, Darwin->macos; x86_64/amd64->x86_64;
    arm64/aarch64 -> arm64 on macos, aarch64 on linux. Else raises
    C.Problem update-failed. Same table as get.sh."""
def detect_layout(module_file: str | None = None) -> Layout | None:
    """module_file defaults to this file. Bundle layout iff realpath lies in
    <root>/.ptest-bundles/<id>/..., uninstall._is_bundle_dir(<root>/.ptest-bundles, id)
    and uninstall._public_link_in_bundles(root). Anything else -> None (source)."""
def resolve_latest(transport: Transport | None = None, *, timeout_s: float) -> str:
    """Raises C.Problem update-unavailable (retryable) on any failure or on a
    Location that does not match the exact tag URL / valid_version."""
def run_update(*, requested: str | None, check_only: bool,
               transport: Transport | None = None,
               layout: Layout | None = None,
               log: TextIO | None = None) -> UpdateResult:
    """Explicit `ptest update`. layout None -> detect_layout(); still None ->
    C.Problem not-install-layout (SOURCE_HINT) before any network. Raises
    C.Problem only. log defaults to sys.stderr (resolved at call time)."""
def startup_check(argv: Sequence[str], *, quiet: bool, json_output: bool,
                  fixture: bool, transport: Transport | None = None,
                  now: float | None = None) -> None:
    """Best-effort pre-command hook. Never raises (except SystemExit(130) on
    Ctrl-C at the prompt/prompted update). Never writes stdout. May
    _execv(<root>/ptest, [<root>/ptest, *argv]) and not return."""
def render_text(result: UpdateResult) -> str:        # S5 / S6 / S7 + "\n"
def document_data(result: UpdateResult) -> dict:     # section 3.4 payload
```

Private seams that T1 tests may monkeypatch (names are frozen inside T1):
`update._execv` (defaults to `os.execv`), `update.default_transport`, and
`update.CHECK_TIMEOUT_S`. Tests may lower `CHECK_TIMEOUT_S` to about 0.2 s so they never wait 2 s.

### 3.3 Startup-check algorithm (T1; T2 documents it)

1. Return without doing anything if any of these holds: `fixture`, `quiet`,
   `_truthy(PTEST_NO_UPDATE_CHECK)`, or `_truthy(CI)`.
   `_truthy(v) = v is not None and v.strip().lower() not in {"", "0", "false", "no", "off"}`.
   The command gate is in cli (`help`, `version`, `update` are exempt).
2. `domain = ptest.platform.domain_paths(None)`. The cache is
   `domain.root / CACHE_NAME`, where `domain.root` is the coordination dir and
   respects `PTEST_STATE_DIR`. Any exception makes the check return silently.
3. Read with `files.read_regular(domain.root, CACHE_NAME, CACHE_MAX_BYTES + 1)`.
   A missing, oversized, symlinked, non-JSON or schema-invalid file counts as an empty cache.
   Cache schema, frozen:
   `{"schema": 1, "checked_at": float, "latest": str|null, "declined": str|null, "declined_at": float|null}`.
   Versions inside the cache are re-validated with `valid_version`.
4. The cache is stale if `checked_at` is missing, `now - checked_at >= CHECK_INTERVAL_S`,
   or `checked_at > now + 300` (clock skew). When stale: write the claim with
   `files.publish_atomic` (0600). If the claim write fails, return. Then run
   `resolve_latest(timeout_s=CHECK_TIMEOUT_S)` in a daemon thread and
   `join(CHECK_TIMEOUT_S)`. On success, write the cache with the new
   `latest`. On failure or timeout, keep the claim and stay silent.
5. If `latest` is missing, or `not is_newer(latest, C.PTEST_VERSION)`, return.
6. `layout = detect_layout()`. If it is None, print S3 and return.
7. Interactive means `sys.stdin.isatty() and sys.stderr.isatty() and not json_output`.
   If not interactive, print S2 and return.
8. If `declined == latest` and `now - declined_at < CHECK_INTERVAL_S`, return silently.
9. Print prompt S1 and read one line. On decline, record `declined`/`declined_at` and return.
10. Accept: run `run_update(requested=latest, check_only=False, layout=layout, log=sys.stderr)`.
    If it raises Problem or Exception, print S10, record a decline, and return.
    On success, print S8, flush stdout and stderr, and call
    `_execv(str(layout.root / "ptest"), [str(layout.root / "ptest"), *argv])`.
    On OSError, print S9 and return, so the command runs on the current version.

### 3.4 `update` public document (T1)

`contracts.PUBLIC_KINDS` gains `"update"`, **appended last**, because
`test_agent_assessment_contract` pins `PUBLIC_KINDS[:8]`. The payload is
exactly this:

```python
_UPDATE_ACTIONS = ("up-to-date", "available", "updated")

def _validate_update_payload(data: dict) -> None:
    _need_str(data, "running_version")
    _need_str(data, "target_version")
    if data.get("action") not in _UPDATE_ACTIONS:
        raise _invalid("report-invalid", "update.action is unknown")
    _need_bool(data, "check_only")
    _need_str(data, "install_root", allow_none=True)

def _project_update_payload(data: dict) -> dict:
    return {"running_version": data["running_version"],
            "target_version": data["target_version"],
            "action": data["action"],
            "check_only": data["check_only"],
            "install_root": data["install_root"]}

# PUBLIC_SCHEMAS["update"]
_envelope_schema("update", {
    "type": "object",
    "properties": {
        "running_version": {"type": "string"},
        "target_version": {"type": "string"},
        "action": {"type": "string", "enum": list(_UPDATE_ACTIONS)},
        "check_only": {"type": "boolean"},
        "install_root": {"type": ["string", "null"]},
    },
    "required": ["running_version", "target_version", "action",
                 "check_only", "install_root"],
})
```

Register the validator in `_PAYLOAD_VALIDATORS` and the projector in `_PROJECTORS`.
`docs/schemas/v1/update.json` is generated with
`uv run --locked --no-sync python scripts/export-schemas.py`. It is never hand-written.

### 3.5 CLI grammar and wiring (T1)

- `_INSPECTION` gains `"update"`. `_COMMAND_ALIASES` gains `"upgrade": "update"`.
- `ParsedArgs` gains `update_check: bool = False` and `update_version: str | None = None`.
- Grammar: `ptest update [--check] [--version X.Y.Z] [--json]`. Each option may
  appear at most once. `--version` takes its value through `_value`, which rejects a missing value
  or one that starts with `--`, and then `update.valid_version`. `--check` combined with
  `--version` is rejected. `--help` / `-h` go through the existing generic route
  (`help update`).
- In `main()`, right after `parsed = _parse_args(raw_args, prefix)`:
  ```python
  if parsed.command not in _UPDATE_CHECK_EXEMPT:   # frozenset({"help", "version", "update"})
      update_api.startup_check(raw_args, quiet=parsed.quiet,
                               json_output=parsed.json,
                               fixture=parsed.fixture_domain is not None)
  ```
  Import it as `from . import update as update_api`.
- `_static_dispatch`: `if command == "update": return _run_update(parsed)`.
  `_run_update` calls `run_update(requested=parsed.update_version,
  check_only=parsed.update_check)`. With `--json`, it writes
  `_document("update", update_api.document_data(result))` to stdout. Otherwise
  it writes `update_api.render_text(result)` to stdout. Errors go through
  `_emit_error(problem, kind="update", json_output=parsed.json)`.
- `_emit_error` adds `"update-unavailable"` to the exit-75 set.
- Installer output never reaches stdout (section 3.6), so `--json` stdout is
  exactly one document.

### 3.6 `ptest update` pipeline (T1)

`run_update` works in this order:

1. Detect the layout. If it is source, raise `not-install-layout` with no network access.
2. Compute `platform_suffix()`.
3. Set `target = requested or resolve_latest(timeout_s=RESOLVE_TIMEOUT_S)`.
4. Pick the action:
   - `requested is None and not is_newer(target, running)`: `up-to-date`.
   - `requested == running`: `up-to-date`.
   - `check_only`: `available`.
   - Otherwise install. A requested older version installs only because `--version` named it.
     Without `--version` there is never a downgrade.
5. Install:
   1. `tmp = tempfile.mkdtemp(prefix="ptest-update-")`. Assert that it is owned and mode 0700.
      Delete it with `shutil.rmtree` in `finally` on **every** path.
   2. Download `ptest-X-<suffix>.tar.gz` (max `MAX_BUNDLE_BYTES`) and `.sha256` (max
      `MAX_SHA_BYTES`) from `RELEASES_URL/download/vX/...` into
      `O_CREAT|O_EXCL|O_NOFOLLOW` 0600 files.
   3. Parse `.sha256`: the first token must be 64 lowercase hex characters. The second
      token, if present, must equal the asset name or `*` + the asset name.
      Compare with `hmac.compare_digest`. A mismatch is `update-failed`, raised before
      any extraction.
   4. Extract by hand with `tarfile.open(mode="r:gz")`, iterating members and never calling `extractall`.
      Only regular files and directories are allowed. Symlinks, hard links, character or block
      devices, FIFOs and other types are rejected. Absolute names, `..` parts, NUL
      and empty parts are rejected. Every name must be `ptest-X` or start with `ptest-X/`.
      There may be at most `MAX_MEMBERS` members, and the summed sizes may be at most
      `MAX_EXTRACT_BYTES`. Duplicates are rejected (`O_EXCL`). Directories are created 0700.
      Files are 0700 if any exec bit is set in the member, else 0600.
   5. Require `ptest-X/install.sh` to be a regular file with an exec bit.
   6. Find the interpreter: `shutil.which("uv")`, then
      `uv python find --system '>=3.11,<3.15'` (60 s timeout). The output must be an absolute
      existing file. Otherwise raise `update-failed`.
   7. Run `[install.sh, "--dest", str(layout.root)]` with `cwd=tmp`,
      `stdin=DEVNULL`, and `stdout=PIPE, stderr=STDOUT`. Keep the output bounded to the last 64 KiB.
      Use `timeout=INSTALL_TIMEOUT_S`. The environment is `os.environ` minus
      `PTEST_INSTALL_FAULT`, plus `PTEST_PYTHON=<found>` and
      `PTEST_NO_UPDATE_CHECK=1`. On a non-zero exit or timeout, write the last 20 or fewer output
      lines to `log` and raise `update-failed`.
   8. Post-verify: `realpath(<root>/ptest)` must be inside
      `<root>/.ptest-bundles/<id>/`, `_is_bundle_dir` must hold, and
      `complete.json.ptest_version == X`. Otherwise raise `update-failed`
      (`the installer did not switch the launcher to X`).
6. Return `UpdateResult(running_version=C.PTEST_VERSION, target_version=target, ...)`.

---

## 4. Task T1: update check and `ptest update`

**Owns:** `src/ptest/update.py` (new), `src/ptest/cli.py`,
`src/ptest/help.py`, `src/ptest/contracts.py`,
`docs/schemas/v1/update.json` (new, generated),
`src/ptest/runtime/protocol-v1.json` (regenerated, expected unchanged),
`scripts/install.py` (smoke environment only), `tests/ng/test_update.py` (new),
`tests/ng/test_cli.py`, `tests/ng/test_help.py`, `tests/ng/test_contracts.py`,
`tests/ng/conftest.py`.

Do not touch `uninstall.py`, `get.sh`, `install.sh`, `agent_rules.py`, or any
file T2 owns.

### 4.1 Help (`help.py`)

- Add `_UPDATE` to `_TOPIC_TEXTS["update"]`, after `uninstall`. `TOPICS` and `HINT`
  follow automatically.
- In the overview, add the line `  ptest update                    # install the latest release (see ptest help update)`
  under "Inspect and review". Add `update` to the `ptest help <topic>` list.
  Add `update` to the `--json on ...` line.
- `_UPDATE` must contain the syntax line `ptest update [--check] [--version X.Y.Z] [--json]`
  and strings S2, S4 and S5. It must name `PTEST_NO_UPDATE_CHECK=1`, truthy `CI`,
  `--fixture-domain` and `-q` as opt-outs, the 24 h cache, the 2 s bound,
  `PTEST_STATE_DIR`, `Update now? [Y/n]`, and the fact that running processes keep their
  version. It must not contain the banned terms `fingerprint`,
  `ready with caveats`, or `expected:`.

### 4.2 Contracts

Apply section 3.4 verbatim, then regenerate the schemas.

### 4.3 `scripts/install.py`

On the three smoke `subprocess.run` calls (`--version`, `guide`,
`python -c`), add `env={**os.environ, "PTEST_NO_UPDATE_CHECK": "1"}`. Change nothing else.

### 4.4 Abuse cases and negative contracts (secure-by-spec step 1)

| Axis | Abuse case | Must not happen | Test (in `test_update.py` unless noted) |
|---|---|---|---|
| Input | hostile `--version` (`1.2`, `1.2.3.4`, `../1.2.3`, `1.2.3/x`, `v1.2.3`, `1.2.3\n`, full-width digits, 40-char) | reaching any URL or path | `test_hostile_version_rejected_before_network` (cli parse and `valid_version`) |
| Input | `Location` pointing to another host/repo/scheme, or a bad tag | a version accepted from it | `test_latest_location_must_be_exact_tag_url` |
| Input | redirect to `http://`, a foreign host, userinfo, port 8443 | following it | `test_redirect_outside_allowlist_refused` (unit test on the real redirect handler and URL check, no socket) |
| Input | body over the cap (streamed and via Content-Length) | unbounded disk/memory | `test_download_over_cap_fails_and_cleans_temp` (bounded-copy helper with a small cap) |
| Input | `.sha256` wrong, malformed, not hex, naming another file | installer runs | `test_checksum_mismatch_never_runs_installer_and_keeps_launcher` |
| Input | tar with absolute path, `..`, symlink, hard link, device/FIFO, member outside `ptest-X/`, duplicate | writing outside the extraction root or running anything | `test_unsafe_archive_member_rejected[...]` (parametrized) |
| State | installer exits non-zero | launcher moves or old bundle is removed | `test_failed_install_keeps_old_bundle_and_launcher` |
| State | installer "succeeds" but does not switch | success reported | `test_post_verify_rejects_unswitched_launcher` |
| State | every failure path | a leftover `ptest-update-*` temp dir | assert that `TMPDIR` is empty after each path |
| State | `PTEST_BASE_URL` or other env set | download URL changes | `test_env_cannot_redirect_download_urls` |
| State | two stale-cache runs in a row, network down | a 2 s cost on each run | `test_failed_fetch_backs_off_via_claim` |
| State | transport hangs | the command is delayed beyond the bound | `test_startup_fetch_is_wall_clock_bounded` (`CHECK_TIMEOUT_S`=0.2, released event) |
| State | cache is a symlink, oversized, bad JSON, or has a future `checked_at` | crash, follow, or trust | `test_corrupt_cache_is_a_miss_never_followed` |
| Exposure | notice or prompt on stdout, or under `--json` | stdout or JSON bytes change | `test_notice_is_stderr_only_and_json_stdout_unchanged` (cli) |
| Exposure | check changes exit status | changed exit code | `test_startup_check_never_changes_exit_status` (cli, with raising transport) |
| Identity/layout | source/editable run of `ptest update` / `--check` | network access or install | `test_source_layout_refuses_without_network` (real `detect_layout` plus deny guard) |
| Opt-out | `PTEST_NO_UPDATE_CHECK=1`, `CI=true`, `--fixture-domain`, `-q` | transport called | `test_opt_outs_never_touch_transport[...]`; also `CI=0` and `CI=false` **do** check |
| Opt-out | `help`, `version`, `update` | `startup_check` called | `test_exempt_commands_skip_startup_check` (cli) |
| TTY | EOF at the prompt | treated as Yes | `test_prompt_eof_declines` |
| TTY | decline | re-prompt within 24 h | `test_decline_remembered_for_24h_then_asks_again` |
| TTY | accept | wrong argv on re-exec | `test_accept_updates_then_reexecs_with_literal_argv` (patched `_execv`) |
| TTY | `execv` fails | lost command | `test_reexec_failure_continues_with_next_run_notice` |
| TTY | prompted update fails | nag loop / exit change | `test_prompted_update_failure_continues_and_records_decline` |
| Install | smoke run of the new bundle | nested update prompt | `test_install_smoke_runs_disable_update_check` (loads `scripts/install.py` like `test_install.py` does, fakes `subprocess.run`, asserts env) |
| Install | installer environment | leaked `PTEST_INSTALL_FAULT` / missing `PTEST_PYTHON` / missing `--dest` | asserted in `test_success_installs_side_by_side_and_switches` (the fake `install.sh` records argv and env) |

Positive tests: up to date (S5, exit 0); `--check` available (S6, exit 0,
nothing downloaded); success (S7, the old bundle dir still exists, the new link points at
the new bundle); explicit older `--version` installs; latest lower than running is
up to date (no downgrade); non-TTY notice S2 exact; source notice S3 exact;
`ptest update --json` round-trips through `C.decode_public_document` with
exactly the five data keys; `ptest update --help` shows the topic.

**Fixture approach.** Build tarballs in memory with `tarfile`. The fake
`ptest-X/install.sh` is a `#!/bin/sh` script. It records `"$@"` and the relevant
environment variables into a file under tmp. It creates
`<dest>/.ptest-bundles/X-test/{complete.json,venv/bin/ptest}` with a valid
marker (`{"version":1,"bundle_id":...,"ptest_version":X}`) and swaps
`<dest>/ptest` with `ln -s` + `mv -f`. A failing variant runs `exit 1`. `uv` is a
PATH shim that prints an absolute fake python path. A fake `Transport`
serves bytes from a dict keyed by URL and records calls. The install root and
`TMPDIR` live under `tmp_path`. The real install is never touched, and the
session guard in conftest enforces that.

### 4.5 Changes to existing tests

- `test_contracts.py`:
  - Add an `"update"` payload to `_full_payloads()`, which covers `test_nine_public_documents_parse`
    (the name stays).
  - Add an `"update"` entry to `_PUBLIC_DATA_KEYS` and `_dirty_payloads` with a smuggled field.
  - Add an `update.json` file-equals-descriptor test modelled on
    `test_init_schema_file_matches_descriptors`.
  - Add a rejection test for an unknown `action`.
- `test_help.py`: add `update` to `VALID_TOPICS`, to the topic-contents map
  (`"--check", "--version", "--json", "PTEST_NO_UPDATE_CHECK"`) and to the
  banned-terms topic loop.
- `test_cli.py`: add parse tests for the grammar in section 3.5 (valid forms, repeated
  flags, `--check --version`, missing or hostile value, `upgrade` suggestion).

### 4.6 `tests/ng/conftest.py`

In `isolated_env`, after the environment is built, add
`monkeypatch.setenv("PTEST_NO_UPDATE_CHECK", "1")`. Subprocess `invoke()`
inherits it through `os.environ`. Add an autouse fixture
`_deny_update_network` that monkeypatches `ptest.update.default_transport` to
return a `Transport` whose two callables call
`pytest.fail("update network access in tests")`. `pytest.fail` raises a
BaseException, so it escapes the startup check's `except Exception`. Tests
in `test_update.py` that exercise the check call `monkeypatch.delenv("PTEST_NO_UPDATE_CHECK")`
and pass or patch their own transport.

### 4.7 T1 acceptance criteria

- [ ] Every row in 4.4 has a test. Each abuse test was observed failing for its
      intended reason before the implementation existed (red, then green). Record this in the T1 report.
- [ ] Strings S1 to S11 and the codes and exits in section 3.1 are byte-exact and asserted in tests.
- [ ] `uv run --locked --no-sync python scripts/export-schemas.py --check` is clean.
      `docs/schemas/v1/update.json` exists. `protocol-v1.json` has no diff.
- [ ] Scoped gate, passing:
      `ptest --workers 2 --queue-timeout 1800 tests/ng/test_update.py tests/ng/test_cli.py tests/ng/test_help.py tests/ng/test_contracts.py tests/ng/test_install.py tests/ng/test_uninstall.py`
      (`test_install`/`test_uninstall` are added because `install.py` and the layout helpers are shared).
      The coverage table shows `ptest/update.py` at 90% or more.
- [ ] No test takes 3 s or longer. No test uses a timeout under 20 s with `invoke()`. No real
      network access. No real install root touched.
- [ ] SAST: `uv run --locked --no-sync bandit -q -r src/ptest/update.py` has no
      unexplained findings. Any `# nosec` carries a reason, following the style of the existing `install.py`
      comments.
- [ ] `graphify update .` has been run. Commit only in the T1 worktree.

---

## 5. Task T2: docs

**Owns:** `README.md`, `docs/installation.md`, `docs/changelog.md`,
`src/ptest/resources/repository-agent-guide.md`, `docs/ptest-agent.md`,
`src/ptest/agent_rules.py` (one hash entry only), `tests/ng/test_agent_rules.py`
(one test). `src/ptest/resources/agent-guide.md` is **not** touched.

### 5.1 Agent guide (`repository-agent-guide.md`; `docs/ptest-agent.md` is a byte-for-byte copy)

Append this row as the **last row** of the `## Reading ptest output` table, after
the `unsafe-path` row:

```
| `ptest: update available: X (installed Y) — run: ptest update` | a newer ptest release exists | Run `ptest update`, then continue; running jobs keep their version. If the line says `installed from source`, tell the user instead. |
```

Replace these two lines:

```
Untracked config: `ptest: .ptest.toml is not committed` — tell the user;
do not commit it yourself unless asked.
```

with this single line:

```
Untracked config: `ptest: .ptest.toml is not committed` — tell the user; do not commit it yourself unless asked.
```

The result is 100 lines. It must not contain `baseline`, `coverage`, `graphify`,
`fast-forward`, `fingerprint`, or `expected:`. Then run
`cp src/ptest/resources/repository-agent-guide.md docs/ptest-agent.md`.

### 5.2 `agent_rules.py`

Append this entry as the last element of `_PREVIOUS_GUIDE_SHA256S`, before `})`:

```python
    # 1d9ef59 (0.3.6-0.3.7): guide before the update-available row.
    "2639d68b4727643280c02301e1f21c6813dbcce4596d04b5666f2d0fb6b081c2",
```

### 5.3 `test_agent_rules.py`

Add `test_previous_hashes_cover_pre_update_row_guide`. It asserts that the hash above is in
`_PREVIOUS_GUIDE_SHA256S`, that the shipped guide contains `ptest: update available:` and `run \`ptest update\``,
and that `len(guide.splitlines()) <= 100`.

### 5.4 README

- Rename `### Pin a version, upgrade, remove` to `### Update, pin a version, remove`.
  The code block becomes:
  ```sh
  ptest update                   # install the latest release (verified, side by side)
  ptest update --check           # only report whether a newer release exists
  ptest update --version 0.3.3   # a specific version, older ones included

  # or with the one-liner (pin with PTEST_VERSION)
  curl -fsSL https://raw.githubusercontent.com/Avocado-Blockchain-Services/ptest/main/get.sh | PTEST_VERSION=0.3.3 sh

  ptest --version
  ptest uninstall --self     # remove the installation (run ptest uninstall in a repo first to clean it)
  ```
  Follow it with this paragraph:
  "ptest looks for a newer release at most once a day (2 s network limit,
  cached in the state directory; offline means no notice). At a terminal it
  asks `ptest 0.3.8 is available (you have 0.3.7). Update now? [Y/n]`, then
  runs your command on the new version. Without a terminal (agents, CI,
  pipes) it prints `ptest: update available: 0.3.8 (installed 0.3.7) — run: ptest update`
  and carries on. Running ptest processes keep their version, so updating is safe
  mid-work. `PTEST_NO_UPDATE_CHECK=1`, a truthy `CI`, and `-q` turn the check
  off. From a source checkout ptest says `installed from source; update it with git pull`."
- Troubleshooting table: add the row
  `| \`ptest: update available: …\` | \`ptest update\` (safe while other runs are active) |`.
- Keep `ptest uninstall` and `PTEST_STATE_DIR` in the README, because `test_uninstall.py` asserts them.

### 5.5 `docs/installation.md`

After `## One-line install`, add `## Updating`. Cover:
- `ptest update [--check] [--version X.Y.Z] [--json]`.
- What it verifies: HTTPS to the GitHub release only, SHA-256 before anything runs,
  a 64 MiB cap, safe extraction, and the same bundled `install.sh`.
- Side-by-side bundles, an atomic launcher switch, and a failed update keeping the old one.
- The startup check (24 h cache under the ptest state area / `PTEST_STATE_DIR`, 2 s bound, silent on
  errors), the opt-outs, and the source-checkout refusal (S4).
- Exit codes: 0 success or up to date, 2 refusal or failure, 75 GitHub unreachable.

### 5.6 `docs/changelog.md`

Insert a new `## Unreleased` section above `## 0.3.7`. Cover `ptest update` (flags,
verification, side-by-side install, `update` JSON document plus schema), the
startup check (prompt at a terminal and re-run, the one-line notice otherwise,
the 24 h cache, the 2 s bound, silent offline, the opt-outs), the agent-guide row, and the fact that
the installer smoke check now runs with `PTEST_NO_UPDATE_CHECK=1`.

### 5.7 T2 acceptance criteria

- [ ] The quoted strings match section 3.1 byte for byte (U+2014 dash).
- [ ] `docs/ptest-agent.md` is byte-identical to `repository-agent-guide.md`.
      The guide is 100 lines or fewer.
- [ ] Scoped gate, passing:
      `ptest --workers 2 --queue-timeout 1800 tests/ng/test_agent_rules.py tests/ng/test_resources.py tests/ng/test_init_changed.py tests/ng/test_help.py tests/ng/test_uninstall.py`.
      This covers `test_every_shipped_guide_version_hashes_into_previous_set` *after
      committing*, plus the banned-terms check on the README and guide.
- [ ] No reference to `ptest help update` outputs beyond S1 to S11 (T2 cannot run T1's code).
- [ ] `graphify update .` has been run. Commit only in the T2 worktree.

---

## 6. File ownership (disjoint; no shared-file edits)

| File | Task | New? |
|---|---|---|
| `src/ptest/update.py` | T1 | new |
| `tests/ng/test_update.py` | T1 | new |
| `docs/schemas/v1/update.json` | T1 | new (generated) |
| `src/ptest/cli.py`, `src/ptest/help.py`, `src/ptest/contracts.py` | T1 | edit |
| `src/ptest/runtime/protocol-v1.json` | T1 | regenerate, expected unchanged |
| `scripts/install.py` | T1 | edit (smoke env) |
| `tests/ng/test_cli.py`, `tests/ng/test_help.py`, `tests/ng/test_contracts.py`, `tests/ng/conftest.py` | T1 | edit |
| `README.md`, `docs/installation.md`, `docs/changelog.md` | T2 | edit |
| `src/ptest/resources/repository-agent-guide.md`, `docs/ptest-agent.md` | T2 | edit |
| `src/ptest/agent_rules.py`, `tests/ng/test_agent_rules.py` | T2 | edit |

No file is edited by both tasks, so there is **no shared-file content** to
transcribe. Merge order does not matter. After both merge, the orchestrator runs one
integrated `ptest --full`. Integration risk is limited to `test_help.py`'s
banned-terms check reading T2's README and guide, which T2 already runs.

---

## 7. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| The suite reaches the network through the startup check | flaky and unsafe tests | conftest env opt-out, a `default_transport` deny guard that escapes `except Exception`, and subprocess `invoke()` always using `--fixture-domain` |
| A guide change breaks managed-guide recognition | `already-exists` in user repositories | the section 5.2 hash plus the section 5.3 test |
| The guide goes over 100 lines | three tests fail | the section 5.1 line join |
| The install smoke run prompts for an update | a hung or recursive install | the section 4.3 env; `ptest update` also sets it for `install.sh` |
| GitHub moves asset redirects to a new CDN host | `ptest update` fails closed | allowlist in one constant; fails as `update-unavailable`/`update-failed`, never unsafe |
| Two concurrent `ptest update` runs | two new bundles, last swap wins | both bundles are valid, and `install.py` publishes atomically. Accepted; no lock |
| Python 3.11.0-3.11.3 has no tar `filter` | unsafe extraction | manual per-member extraction, never `extractall` |

## 8. Open questions (not blocking)

- Should `uninstall` be exempt from the startup check? The spec lists only
  `help`, `version` and `update`, so it stays checked for now.
- Should `ptest update --check` exit non-zero when an update exists? Chosen: 0.
  The JSON `action` field carries the answer.
