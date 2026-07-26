"""Load the extensionless `ptest` script as an importable module."""
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "ptest"


@pytest.fixture(scope="session")
def ptest():
    loader = importlib.machinery.SourceFileLoader("ptest_mod", str(SCRIPT))
    spec = importlib.util.spec_from_loader("ptest_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def repo(tmp_path):
    """Build a fake repo tree from a list of relative paths."""
    def make(paths):
        for rel in paths:
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("")
        return tmp_path
    return make


@pytest.fixture
def persea_shaped(repo):
    """A tree with persea-api's proportions: 221 api / 61 db / 23 crawler / 3 unit."""
    paths = (
        [f"tests/api/test_a{i}.py" for i in range(221)]
        + [f"tests/db/test_d{i}.py" for i in range(61)]
        + [f"tests/crawler/test_c{i}.py" for i in range(23)]
        + [f"tests/unit/test_u{i}.py" for i in range(3)]
    )
    return repo(paths)
