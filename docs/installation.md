# Local installation

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
