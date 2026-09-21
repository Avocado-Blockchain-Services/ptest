# Local security gates

Run the developer gate with the locked environment:

```sh
uv run --locked python scripts/security-checks.py
```

Bandit, pip-audit, and Gitleaks must be installed, detect their synthetic
fixture, and pass their owned scope. An unavailable detector or failed
sensitivity check is explicitly unpassed. pip-audit may contact package
advisory metadata; source scanning remains local.
