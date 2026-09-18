`matrix.json` supplies concrete standard lib/bin/integration, custom-harness and
workspace-member projects. All native cases are NOT RUN; Task11 must copy each
project inside an explicit private fixture-domain and use candidate ptest.
Create `{run_output}` outside the source snapshot; replace placeholders literally
before writing configuration. Trace lines identify timestamp, process, thread
and start/end; assert the expected positive start count and overlap <= grant.
Create an isolated absolute CARGO_HOME with no configuration and explicitly
controlled child environment. Do not share target directories or native caches
between simultaneously active fixture projects.

The preparation profile refuses all discovered `.cargo/config`,
`.cargo/config.toml` and CARGO_HOME configuration (even empty files), ancestor
manifests, workspace/member selection and manifests, custom harnesses, and
examples/benchmarks opted into testing. Unknown Cargo/Rust environment controls,
CLI overrides, wrappers, filters and no-test modes fail closed. A missing,
malformed, oversized, linked, inaccessible or special-file manifest/config path
cannot qualify. Only explicit standard targets are accepted; no doctests are
silently removed from a default command. Libtest passthrough allows display flags
only, behind exactly one separator with ptest's thread limit first.

Task11 still owns executable/version qualification, the environment snapshot,
config/source revalidation and mutation races through launch, guard cancellation,
native exit/signal truth and promotion from `unavailable`. The matrix's exit
values are expectations to verify, not measured results. Cargo maps an inner
custom exit to its own failure code; candidate ptest must retain the Cargo code.
Public counts remain null; no per-test inventory or selection is inferred.
