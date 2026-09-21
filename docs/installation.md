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
