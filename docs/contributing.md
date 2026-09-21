# Contributing to ptest NG

Keep the product local-first: no model API, cloud credential, hosted test
backend, or provider-specific TUI integration belongs in the runtime. Ordinary
CLI invocation is the interoperability contract.

Use the repository's `ptest` command for every test invocation. During TDD,
run a scoped command; reserve one `ptest --full` for the integrated chain gate.
Use `uv` for Python environments and never install with `pip`.

Acceptance and benchmark scripts are root tools. They must use literal argv,
finite subprocesses, temporary owned fixture/artifact directories, and must
retain every failed or capped attempt. Do not collect them as tests or launch
substantial external suites under a fixture domain. Do not add a fallback that
turns an unavailable capability into a pass.

Evidence roots are created exclusively with mode 0700 and artifacts with mode
0600; an existing path or symlinked parent is refused. Benchmark promotion
requires a candidate-bound typed workload profile. Arbitrary commands remain
diagnostic only.

Changes to runtime behavior need positive and negative regression evidence and
must preserve exit status, output, coverage, and user files. Run
`graphify update .` after source changes. Keep platform and runner claims tied
to actual recorded evidence; document blocked or unverified work explicitly.
