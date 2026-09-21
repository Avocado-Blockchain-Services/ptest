# ptest NG support matrix

Support labels are evidence levels, not guesses:

| Area | Current level | Boundary |
| --- | --- | --- |
| Local CLI, non-TTY use | Implemented in the NG runtime | Verify with the integrated chain gate |
| Pytest/Vitest basic serial profiles | Enumerated implementation candidates | Each exact runner tuple needs positive and negative native evidence |
| Advanced selection and baseline promotion | Unverified | Requires advanced-capability adoption evidence |
| Independent pytest adoption | Blocked-unverified | Public immutable checkout and dependency setup still need an authorized run |
| Independent Vitest adoption | Blocked-unverified | Public immutable checkout, Yarn availability, and lifecycle disclosure still need an authorized run |
| Linux local filesystem | Observed platform scope | Only the measured runner/launcher tuple is supported |
| macOS | Unverified | Repeat the install/lifecycle/adapter matrix on actual macOS |
| Windows, hosted services, cloud APIs | Out of scope for v1 | ptest remains local-only |
| Coding-agent TUIs | Generic CLI compatibility only | No provider API or agent launcher is required or implied |

An unavailable or failed check stays visible in the acceptance JSON. It is not
silently upgraded to “supported” by a version string or a static pattern.
