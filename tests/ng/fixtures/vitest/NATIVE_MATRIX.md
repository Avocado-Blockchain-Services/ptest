# Vitest execution matrix

`kind = "vitest"` executes as one literal exclusive command through the
project-local Vitest CLI:

- scoped: `launcher + ("node_modules/vitest/vitest.mjs", "run") + runner.args`
  (the caller scope is already appended to the effective args)
- full: `launcher + ("node_modules/vitest/vitest.mjs", "run") + runner.args + runner.full_args`

The launcher is `("node",)` or one absolute path named `node`. `plan.files`
must stay empty; `selected` plans refuse. The capability is
`exclusive_command` with selection disabled: Vitest keeps its own worker
pool, ptest claims no worker ownership and no per-test results, and the
vitest exit code is the outcome. Declared `[setup]` (for example `npm ci`)
runs first when its required paths are missing, as for command profiles.

No native Vitest tuple is qualified for selection or inventory: there is no
per-test result record, no advanced preparation, and no qualified profile.
The `basic-*` / `advanced-*` fixture trees and `stub-node.mjs` in this
directory are leftover doubles from the retired prepared-only bridge route
(`src/ptest/runtime/vitest_bridge.mjs` is kept in place only because
`scripts/install.py` asserts it exists; deleting both is a recorded
follow-up). They are not executed by any test.

Project config and hooks are trusted local code, not a sandbox; the adapter
refuses non-node launchers and public plan files but does not claim to
contain the command's side effects.
