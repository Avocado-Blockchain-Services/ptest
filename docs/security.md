# Local security gates

Provision the pinned official release once per checkout, then run the developer
gate with the locked environment:

```sh
uv run --locked python scripts/security-checks.py --provision-gitleaks
uv run --locked python scripts/security-checks.py
```

Bandit, pip-audit, and Gitleaks must be installed, detect their synthetic
fixture, and pass their owned scope. An unavailable detector or failed
sensitivity check is explicitly unpassed. pip-audit may contact package
advisory metadata; source scanning remains local.

Provisioning downloads only the official Gitleaks 8.30.1 Linux x86_64 archive
and its release checksum file, verifies both pinned SHA-256 values, and stores
the executable under `.tools/gitleaks`; scans never trust `PATH` or download
artifacts.
