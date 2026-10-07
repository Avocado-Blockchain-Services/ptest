"""T1: source_index — per-content AST index, fingerprints, resolver, cache.

Unit scope (tmp_path trees plus in-memory raws); no store needed — a
dict-backed fake ParseCache is enough. Every test names the design rule
it pins (design.md sections 2.1/2.3, T1 acceptance).
"""
from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest import impact as I
from ptest import source_index as S
from support import init_git_repo


def _digest(key: bytes, raw: bytes) -> str:
    return C.selection_file_digest(key, raw)


class DictCache:
    """Dict-backed ParseCache fake with call counts (design 2.3: one get_many,
    at most one put_many per build)."""

    def __init__(self, blobs=None):
        self.blobs = dict(blobs or {})
        self.get_calls: list[list[str]] = []
        self.put_calls: list[dict[str, bytes]] = []

    def get_many(self, digests):
        digests = list(digests)
        self.get_calls.append(digests)
        return {d: self.blobs[d] for d in digests if d in self.blobs}

    def put_many(self, blobs):
        self.put_calls.append(dict(blobs))
        self.blobs.update(blobs)


def _config(test_roots=("tests",)):
    return C.Config(
        runner=C.RunnerConfig(kind=C.RunnerKind.PYTEST, launcher=("uv",),
                              test_roots=test_roots),
        setup=None,
        resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(enabled=True, closed_inputs=False),
        project_id="ab" * 16,
    )


def _scopes(index):
    return {s.qualname: s for s in index.scopes}


def _classes(index):
    return {c.qualname: c for c in index.classes}


# --- index_source: scopes, classes, statements, imports (2.3) ---

def test_index_source_sample_module_tables():
    raw = (b"import os\n"
           b"import pkg.mod as m\n"
           b"from pkg.other import thing as t\n"
           b"from pkg.star_src import *\n"
           b"VALUE = 1\n"
           b"def f(x, y=2):\n"
           b"    return os.path.join(str(x), str(y))\n"
           b"class K(Base, metaclass=Meta):\n"
           b"    '''docs'''\n"
           b"    ATTR = thing\n"
           b"    def method(self):\n"
           b"        return self.ATTR\n")
    index = S.index_source(raw)

    assert index.parsed is True
    kinds = [st.kind for st in index.statements]
    assert kinds == ["import", "import", "import", "import", "assign",
                     "def", "class"]
    star = index.statements[3]
    assert star.kind == "import" and star.bound == ("*",)
    plain = index.statements[0]
    assert plain.bound == ("os",) and plain.refs == ()
    assert index.statements[4].bound == ("VALUE",)

    scopes = _scopes(index)
    assert set(scopes) == {"f", "K.method"}
    assert scopes["f"].refs == (("os", "path", "join"), ("str",),
                                   ("x",), ("y",))
    assert scopes["K.method"].refs == (("self", "ATTR"),)

    classes = _classes(index)
    assert set(classes) == {"K"}
    assert ("thing",) in classes["K"].refs

    by_local = {imp.local: imp for imp in index.imports
                if imp.statement >= 0}
    assert by_local["os"].module == "os"
    assert by_local["os"].name is None
    assert by_local["os"].aliased is False
    assert by_local["m"].module == "pkg.mod"
    assert by_local["m"].aliased is True
    assert by_local["t"].module == "pkg.other"
    assert by_local["t"].name == "thing"


def test_index_source_comment_whitespace_line_shift_is_no_change():
    a = (b"VALUE = 1\n\n\ndef f(x):\n"
         b"    # a comment\n"
         b"    return x + 1\n")
    b = (b"# header comment\nVALUE=1\ndef f(x):\n    return x+1\n")
    c = (b"\n\n\nVALUE = 1\ndef f(x):\n    return x + 1\n")
    ia, ib, ic = S.index_source(a), S.index_source(b), S.index_source(c)

    assert ia.parsed and ib.parsed and ic.parsed
    assert ([(s.body, s.skeleton) for s in ia.scopes]
            == [(s.body, s.skeleton) for s in ib.scopes]
            == [(s.body, s.skeleton) for s in ic.scopes])
    assert ([st.fingerprint for st in ia.statements]
            == [st.fingerprint for st in ib.statements]
            == [st.fingerprint for st in ic.statements])


def test_index_source_default_changes_skeleton_only_body_change_body_only():
    base = b"def f(x, y=2):\n    return x + y\n"
    skeleton = S.index_source(b"def f(x, y=3):\n    return x + y\n")
    body = S.index_source(b"def f(x, y=2):\n    return x - y\n")
    one = _scopes(S.index_source(base))["f"]
    two = _scopes(skeleton)["f"]
    three = _scopes(body)["f"]

    assert two.skeleton != one.skeleton
    assert two.body == one.body
    assert three.body != one.body
    assert three.skeleton == one.skeleton


def test_index_source_method_body_edit_keeps_class_body():
    base = (b"class K:\n"
            b"    ATTR = 1\n"
            b"    def method(self):\n"
            b"        return 1\n")
    changed = (b"class K:\n"
               b"    ATTR = 1\n"
               b"    def method(self):\n"
               b"        return 2\n")
    one, two = S.index_source(base), S.index_source(changed)

    assert _classes(one)["K"].body == _classes(two)["K"].body
    assert (_scopes(one)["K.method"].body
            != _scopes(two)["K.method"].body)


def test_index_source_nested_defs_and_lambdas_fold_into_outer_scope():
    raw = (b"def outer(x):\n"
           b"    def inner(y):\n"
           b"        return y + 1\n"
           b"    fn = lambda z: z + x\n"
           b"    return inner(x) + fn(x)\n")
    index = S.index_source(raw)
    scopes = _scopes(index)

    assert set(scopes) == {"outer"}
    assert (("inner",),) not in scopes.values()


def test_index_source_duplicate_qualnames_merge_covering_both():
    raw = (b"import sys\n"
           b"if sys.version_info >= (3, 12):\n"
           b"    def f():\n"
           b"        return 1\n"
           b"else:\n"
           b"    def f():\n"
           b"        return 2\n")
    index = S.index_source(raw)
    scopes = _scopes(index)

    assert set(scopes) == {"f"}
    first = S.index_source(b"def f():\n    return 1\n")
    second = S.index_source(b"def f():\n    return 2\n")
    merged = scopes["f"]
    assert merged.body != _scopes(first)["f"].body
    assert merged.body != _scopes(second)["f"].body


def test_index_source_kind_table_doc_effect_star():
    raw = (b'"""module docstring"""\n'
           b"import os\n"
           b"from pkg import *\n"
           b"X = 1\n"
           b"if True:\n"
           b"    print(X)\n"
           b"for i in range(3):\n"
           b"    print(i)\n"
           b"with open('f') as fh:\n"
           b"    print(fh)\n"
           b"try:\n"
           b"    print('t')\n"
           b"except OSError:\n"
           b"    raise\n"
           b"print('call')\n"
           b"if TYPE_CHECKING:\n"
           b"    from pkg import Thing\n"
           b"class C:\n"
           b"    pass\n"
           b"def g():\n"
           b"    pass\n")
    index = S.index_source(raw)
    kinds = [st.kind for st in index.statements]

    assert kinds == ["doc", "import", "import", "assign", "effect",
                     "effect", "effect", "effect", "effect", "effect",
                     "class", "def"]
    assert index.statements[2].bound == ("*",)


@pytest.mark.parametrize("raw", [
    b"def broken(:\n",
    "VALUE = 'héllo'\n".encode("latin-1"),
    b"VALUE = 1\n" + b"x" * (I.MAX_FILE_BYTES + 1),
])
def test_index_source_unparseable_is_not_parsed(raw):
    index = S.index_source(raw)

    assert index.parsed is False
    assert index.imports == ()
    assert index.scopes == ()
    assert index.classes == ()
    assert index.statements == ()


def test_index_source_fingerprints_use_ast_dump_without_attributes():
    raw = b"VALUE = 1\n"
    index = S.index_source(raw)
    node = ast.parse(raw.decode("utf-8")).body[0]
    expected = hashlib.sha256(
        ast.dump(node, include_attributes=False).encode()).hexdigest()

    assert index.statements[0].fingerprint == expected


# --- N11: indexing never imports or executes project code ---

def test_index_source_has_no_side_effects_and_imports_nothing(tmp_path):
    probe = tmp_path / "probe.txt"
    raw = (b"import sys\n"
           b"print('loud')\n"
           b"open(r'" + str(probe).encode() + b"', 'w').write('touched')\n"
           b"raise SystemExit(3)\n")
    before = set(sys.modules)

    index = S.index_source(raw)

    assert index.parsed is True
    assert set(sys.modules) == before
    assert not probe.exists()


def test_build_project_index_has_no_side_effects(tmp_path):
    init_git_repo(tmp_path, files={
        "pkg/__init__.py": "",
        "pkg/evil.py": ("import sys\nprint('loud')\n"
                        "raise SystemExit(3)\n"),
        "tests/test_e.py": "def test_e():\n    assert True\n",
    })
    before = set(sys.modules)

    index = S.build_project_index(tmp_path, _config(), key=None,
                                  cache=None)

    assert set(sys.modules) == before
    assert "pkg/evil.py" in index.files


# --- encode_index / decode_index (deterministic, versioned) ---

def _sample_index():
    return S.index_source(
        b"import os\n"
        b"from pkg.other import thing\n"
        b"VALUE = thing\n"
        b"def f(x, y=2):\n"
        b"    return os.path.join(str(x), str(y))\n"
        b"class K(Base):\n"
        b"    ATTR = thing\n"
        b"    def method(self):\n"
        b"        return self.ATTR\n")


def test_encode_decode_round_trip_is_equal():
    index = _sample_index()

    assert S.decode_index(S.encode_index(index)) == index


def test_encode_is_deterministic():
    assert S.encode_index(_sample_index()) == S.encode_index(_sample_index())


def test_decode_rejects_truncated_garbage_wrong_version():
    blob = S.encode_index(_sample_index())

    assert S.decode_index(blob[:len(blob) // 2]) is None
    assert S.decode_index(b"not an index at all!!!!!!!!") is None
    bad_magic = b"\x00" + blob[1:] if blob[:1] != b"\x00" else b"\xff" + blob[1:]
    assert S.decode_index(bad_magic) is None
    # A well-formed header with a foreign version word is also rejected.
    at = len(S._MAGIC)
    bad_version = blob[:at] + b"\x00\x00\x00\x02" + blob[at + 4:]
    assert S.decode_index(bad_version) is None


def _forge(payload: dict) -> bytes:
    """Rebuild a blob with a caller-supplied payload (the on-disk format is
    a fixed magic header plus a JSON object, per source_index docs)."""
    import json as _json
    blob = S.encode_index(_sample_index())
    head = blob[:blob.index(b"{")]
    return head + _json.dumps(payload, sort_keys=True,
                              separators=(",", ":")).encode()


def test_decode_rejects_wrong_type_field():
    assert S.decode_index(_forge({"parsed": 1, "imports": [], "scopes": [],
                                  "classes": [], "statements": []})) is None
    assert S.decode_index(_forge({"parsed": True, "imports": {},
                                  "scopes": [], "classes": [],
                                  "statements": []})) is None


def test_decode_rejects_unknown_statement_kind():
    forged = _forge({"parsed": True, "imports": [], "scopes": [],
                     "classes": [],
                     "statements": [["bogus", [], [], "abc"]]})

    assert S.decode_index(forged) is None


# --- iter_python_files / read_source ---

def test_iter_python_files_matches_impact_walk(tmp_path):
    init_git_repo(tmp_path, files={
        "pkg/__init__.py": "",
        "pkg/a.py": "X = 1\n",
        "tests/test_a.py": "def test_a():\n    assert True\n",
        "build/lib/pkg/b.py": "Y = 2\n",
        ".hidden/c.py": "Z = 3\n",
    })

    assert sorted(S.iter_python_files(tmp_path)) == sorted(
        I._iter_py_files(tmp_path))


def test_read_source_refuses_links_dirs_oversize(tmp_path):
    init_git_repo(tmp_path, files={"pkg/a.py": "X = 1\n"})
    big = tmp_path / "pkg" / "big.py"
    big.write_bytes(b"x" * (I.MAX_FILE_BYTES + 1))
    (tmp_path / "pkg" / "link.py").symlink_to(tmp_path / "pkg" / "a.py")

    assert S.read_source(tmp_path, "pkg/a.py") == b"X = 1\n"
    assert S.read_source(tmp_path, "pkg/big.py") is None
    assert S.read_source(tmp_path, "pkg/link.py") is None
    assert S.read_source(tmp_path, "pkg") is None
    assert S.read_source(tmp_path, "missing.py") is None
    assert S.read_source(tmp_path, "../outside.py") is None


# --- build_project_index: modules, graph, conftest edges (2.3) ---

def _tree():
    return {
        "pkg/__init__.py": "",
        "pkg/b.py": "VALUE = 1\n",
        "pkg/a.py": "from pkg.b import VALUE\n",
        "tests/test_a.py": ("from pkg.a import VALUE\n\n"
                             "def test_a():\n    assert VALUE\n"),
        "tests/test_b.py": "def test_b():\n    assert True\n",
        "tests/test_c.py": "def test_c():\n    assert True\n",
        "tests/test_d.py": "def test_d():\n    assert True\n",
    }


def test_build_empty_project_is_complete_and_empty(tmp_path):
    init_git_repo(tmp_path, files={"README.md": "hi\n"})
    index = S.build_project_index(tmp_path, _config(), key=None,
                                  cache=None)

    assert index.complete is True
    assert index.files == {}
    assert index.test_files == frozenset()
    assert index.unparsed == frozenset()


def test_build_modules_equal_impact_module_names(tmp_path):
    init_git_repo(tmp_path, files=_tree())
    index = S.build_project_index(tmp_path, _config(), key=None,
                                  cache=None)

    for rel, project_file in index.files.items():
        assert project_file.modules == I._module_names(tmp_path, rel)
    assert index.test_files == frozenset(
        rel for rel, pf in index.files.items() if pf.test)
    assert index.unparsed == frozenset()
    assert index.complete is True


def _old_reach(project_root, test_roots, seeds):
    """The 0.4 name-graph reachability, kept as a private helper (design T1):
    reverse name -> importing files, BFS over module names."""
    py_files = [rel for rel in I._iter_py_files(project_root)]
    names_of = {rel: I._module_names(project_root, rel) for rel in py_files}
    reverse: dict[str, set[str]] = {}
    for rel in py_files:
        text = I._read_text(project_root / rel)
        if text is None:
            continue
        for name in I._import_edges(text, I._package_name(rel)):
            reverse.setdefault(name, set()).add(rel)
    queue: list[str] = []
    for seed in seeds:
        queue.extend(I._module_names(project_root, seed))
    seen = set(queue)
    reached: set[str] = set()
    while queue:
        name = queue.pop()
        for rel in reverse.get(name, ()):
            if rel in reached:
                continue
            reached.add(rel)
            for alias in names_of.get(rel, ()):
                if alias not in seen:
                    seen.add(alias)
                    queue.append(alias)
    return {rel for rel in reached
            if I._is_test_file(rel, test_roots)}


def test_build_reverse_matches_old_name_graph(tmp_path):
    init_git_repo(tmp_path, files=_tree())
    index = S.build_project_index(tmp_path, _config(), key=None,
                                  cache=None, conftest_edges=False)

    for seeds in (["pkg/b.py"], ["pkg/a.py"], ["pkg/__init__.py"],
                  ["tests/test_a.py"], ["pkg/b.py", "pkg/a.py"]):
        new = (C.selection_static_reach(index.reverse, seeds)
               & index.test_files)
        # selection_static_reach includes the seeds themselves; the 0.4
        # name walk only returns importers, so the oracle gains seed tests.
        old = _old_reach(tmp_path, ("tests",), seeds) | (
            set(seeds) & set(index.test_files))
        assert new == old, seeds


def test_build_conftest_edges_reach_tests_under_its_directory(tmp_path):
    files = dict(_tree())
    files["tests/conftest.py"] = "from pkg.b import VALUE\n"
    files["tests/sub/test_s.py"] = "def test_s():\n    assert True\n"
    files["other/test_o.py"] = "def test_o():\n    assert True\n"
    init_git_repo(tmp_path, files=files)
    config = _config(test_roots=("tests", "other"))

    plain = S.build_project_index(tmp_path, config, key=None, cache=None,
                                  conftest_edges=False)
    edged = S.build_project_index(tmp_path, config, key=None, cache=None,
                                  conftest_edges=True)

    assert "tests/test_b.py" not in plain.reverse.get(
        "tests/conftest.py", frozenset())
    assert {"tests/test_a.py", "tests/test_b.py", "tests/test_c.py",
            "tests/test_d.py", "tests/sub/test_s.py"} <= edged.reverse[
                "tests/conftest.py"]
    assert "other/test_o.py" not in edged.reverse["tests/conftest.py"]
    # A module imported only by the conftest reaches the whole directory.
    assert (C.selection_static_reach(edged.reverse, ["pkg/b.py"])
            & edged.test_files) >= {"tests/test_a.py", "tests/test_b.py"}
    assert (C.selection_static_reach(plain.reverse, ["pkg/b.py"])
            & plain.test_files) == {"tests/test_a.py"}


# --- build_project_index: name resolution (2.3 table) ---

def _resolve_tree():
    return {
        "pkg/__init__.py": "from pkg.sub import helper\n",
        "pkg/sub.py": "def helper():\n    return 1\n",
        "pkg/b.py": "VALUE = 1\n",
        "pkg/a.py": ("import pkg.sub as sub\n"
                     "from pkg.b import VALUE\n"
                     "from pkg import sub as submod\n"
                     "from .b import VALUE as V2\n"
                     "from pkg.star_src import *\n"
                     "USED = sub.helper()\n"
                     "SEEN = VALUE + V2\n"),
        "pkg/star_src.py": "STARRED = 1\n_private = 2\n",
        "tests/test_a.py": ("from pkg.a import USED\n"
                             "def test_a():\n    assert USED\n"),
    }


def test_build_resolution_aliases_module_attr_from_variants(tmp_path):
    init_git_repo(tmp_path, files=_resolve_tree())
    index = S.build_project_index(tmp_path, _config(), key=None,
                                  cache=None)
    key = C.selection_name_key

    assert index.files["pkg/a.py"].imports >= {
        "pkg/sub.py", "pkg/b.py", "pkg/__init__.py", "pkg/star_src.py"}
    # import pkg.sub as sub; USED = sub.helper() -> the defining module path.
    assert key("pkg/sub.py", "helper") in _all_refs(index, "pkg/a.py")
    # from pkg.b import VALUE used at module level (assign statement refs).
    stmt_refs = index.files["pkg/a.py"].statement_refs
    assert any(key("pkg/b.py", "VALUE") in s for s in stmt_refs)
    # from .b import VALUE as V2 used at module level.
    assert any(key("pkg/b.py", "V2") in s or key("pkg/b.py", "VALUE") in s
               for s in stmt_refs)
    # Unresolved builtins are dropped, never keys.
    assert all(not k.endswith(":len") and not k.endswith(":str")
               for s in stmt_refs for k in s)


def _all_refs(index, path):
    pf = index.files[path]
    out = set()
    for refs in list(pf.scope_refs.values()) + list(pf.class_refs.values()):
        out |= set(refs)
    for refs in pf.statement_refs:
        out |= set(refs)
    return out


def test_build_resolution_reexport_points_at_target_key(tmp_path):
    init_git_repo(tmp_path, files=_resolve_tree())
    index = S.build_project_index(tmp_path, _config(), key=None,
                                  cache=None)
    init_refs = index.files["pkg/__init__.py"].statement_refs
    key = C.selection_name_key

    assert any(key("pkg/sub.py", "helper") in s for s in init_refs)


def test_build_resolution_star_import_expands_bound_names(tmp_path):
    init_git_repo(tmp_path, files=_resolve_tree())
    index = S.build_project_index(tmp_path, _config(), key=None,
                                  cache=None)
    pf = index.files["pkg/a.py"]
    key = C.selection_name_key

    star = [i for i, st in enumerate(pf.index.statements)
            if st.kind == "import" and st.bound == ("*",)]
    assert len(star) == 1
    assert key("pkg/a.py", "STARRED") in pf.statement_bound[star[0]]
    assert not any(k.endswith(":_private")
                   for k in pf.statement_bound[star[0]])


def test_build_resolution_last_binding_wins_in_source_order(tmp_path):
    init_git_repo(tmp_path, files={
        "pkg/__init__.py": "",
        "pkg/b.py": "VALUE = 1\n",
        "pkg/a.py": ("X = 'local'\n"
                     "from pkg.b import VALUE as X\n"
                     "USED = X\n"),
        "tests/test_a.py": "def test_a():\n    assert True\n",
    })
    index = S.build_project_index(tmp_path, _config(), key=None,
                                  cache=None)
    key = C.selection_name_key

    assert key("pkg/b.py", "VALUE") in _all_refs(index, "pkg/a.py")
    assert key("pkg/a.py", "X") not in _all_refs(index, "pkg/a.py")


def test_build_import_statement_refs_are_from_targets_only(tmp_path):
    init_git_repo(tmp_path, files=_resolve_tree())
    index = S.build_project_index(tmp_path, _config(), key=None,
                                  cache=None)
    pf = index.files["pkg/a.py"]
    plain = [i for i, imp in enumerate(pf.index.imports)
             if imp.local == "sub" and imp.statement >= 0]
    assert plain
    assert pf.statement_refs[plain[0]] == frozenset()


# --- build_project_index: parse cache (one get_many, <= one put_many) ---

def test_build_cache_contract_single_get_single_put_then_warm(tmp_path):
    init_git_repo(tmp_path, files=_tree())
    key = b"0" * 32
    cache = DictCache()

    first = S.build_project_index(tmp_path, _config(), key=key,
                                  cache=cache)

    assert len(cache.get_calls) == 1
    assert len(cache.put_calls) == 1
    assert first.complete is True

    cache.get_calls.clear()
    cache.put_calls.clear()
    second = S.build_project_index(tmp_path, _config(), key=key,
                                   cache=cache)

    assert len(cache.get_calls) == 1
    assert len(cache.put_calls) == 0
    assert {r: f.digest for r, f in second.files.items()} == \
        {r: f.digest for r, f in first.files.items()}


def test_build_warm_cache_parses_nothing(tmp_path, monkeypatch):
    init_git_repo(tmp_path, files=_tree())
    key = b"0" * 32
    cache = DictCache()
    S.build_project_index(tmp_path, _config(), key=key, cache=cache)
    calls = []
    real_parse = ast.parse

    def counting_parse(*args, **kwargs):
        calls.append(1)
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(ast, "parse", counting_parse)
    S.build_project_index(tmp_path, _config(), key=key, cache=cache)

    assert calls == []


def test_build_key_none_uses_no_cache(tmp_path):
    init_git_repo(tmp_path, files=_tree())
    cache = DictCache()

    index = S.build_project_index(tmp_path, _config(), key=None,
                                  cache=cache)

    assert cache.get_calls == []
    assert cache.put_calls == []
    assert all(f.digest == "" for f in index.files.values())


def test_build_digests_are_keyed_and_content_addressed(tmp_path):
    init_git_repo(tmp_path, files=_tree())
    key = b"0" * 32
    index = S.build_project_index(tmp_path, _config(), key=key,
                                  cache=None)

    for rel, project_file in index.files.items():
        raw = (tmp_path / rel).read_bytes()
        assert project_file.digest == _digest(key, raw)


def test_build_exceeding_scan_cap_is_incomplete(tmp_path, monkeypatch):
    init_git_repo(tmp_path, files=_tree())
    monkeypatch.setattr(I, "MAX_SCAN_FILES", 1)

    index = S.build_project_index(tmp_path, _config(), key=None,
                                  cache=None)

    assert index.complete is False


# --- regression: star re-export chain propagates names (finding 1) ---

def test_build_star_reexport_chain_refs_propagate(tmp_path):
    init_git_repo(tmp_path, files={
        "pkg/__init__.py": "from .core import *\n",
        "pkg/core.py": "TIMEOUT = 5\nREG = 1\n",
        "tests/test_x.py": ("from pkg import TIMEOUT\n"
                            "def test_x():\n    assert TIMEOUT\n"),
    })
    index = S.build_project_index(tmp_path, _config(), key=None,
                                  cache=None)
    key = C.selection_name_key
    pf = index.files["pkg/__init__.py"]

    star = [i for i, st in enumerate(pf.index.statements)
            if st.kind == "import" and st.bound == ("*",)]
    assert len(star) == 1
    # Bound side still re-exports the names locally ...
    assert key("pkg/__init__.py", "TIMEOUT") in pf.statement_bound[star[0]]
    # ... and the ref side resolves to the defining module so the planner's
    # fixpoint (which only reads statement_refs) can carry the name through.
    assert key("pkg/core.py", "TIMEOUT") in pf.statement_refs[star[0]]
    assert key("pkg/core.py", "REG") in pf.statement_refs[star[0]]
    # The importing test resolves through the chain to the definition.
    assert key("pkg/__init__.py", "TIMEOUT") in _all_refs(index,
                                                          "tests/test_x.py")


# --- regression: multi-level star chains propagate (finding 1, attempt 3) ---

def test_build_star_chain_three_levels_propagate(tmp_path):
    # _Resolver used to drop every star import (local is None), so a name
    # could not pass through more than one star re-export and star-fed
    # scope refs resolved to nothing.
    init_git_repo(tmp_path, files={
        "pkg/__init__.py": "from .mid import *\n",
        "pkg/mid.py": "from .core import *\nOWN = 1\n",
        "pkg/core.py": "TIMEOUT = 5\n",
        "tests/test_x.py": ("from pkg import TIMEOUT\n"
                            "def test_x():\n    assert TIMEOUT\n"),
    })
    index = S.build_project_index(tmp_path, _config(), key=None,
                                  cache=None)
    key = C.selection_name_key

    mid = index.files["pkg/mid.py"]
    mid_star = [i for i, st in enumerate(mid.index.statements)
                if st.kind == "import" and st.bound == ("*",)]
    assert len(mid_star) == 1
    assert key("pkg/core.py", "TIMEOUT") in mid.statement_refs[mid_star[0]]

    top = index.files["pkg/__init__.py"]
    top_star = [i for i, st in enumerate(top.index.statements)
                if st.kind == "import" and st.bound == ("*",)]
    assert len(top_star) == 1
    # TIMEOUT arrives through mid's own star (bound_names(mid) expands it);
    # OWN is mid's own binding. Each level points at its direct target and
    # the planner fixpoint carries the change transitively.
    assert key("pkg/mid.py", "TIMEOUT") in top.statement_refs[top_star[0]]
    assert key("pkg/mid.py", "OWN") in top.statement_refs[top_star[0]]
    assert key("pkg/__init__.py",
               "TIMEOUT") in top.statement_bound[top_star[0]]
    # The importing test resolves to the re-export, which the change
    # fingerprint now reaches through the chain.
    assert key("pkg/__init__.py", "TIMEOUT") in _all_refs(index,
                                                          "tests/test_x.py")


def test_build_star_imported_name_resolves_in_function_scope(tmp_path):
    # A name a module gets from a star import must resolve inside function
    # bodies too (conservative star handling), not be dropped.
    init_git_repo(tmp_path, files={
        "pkg/__init__.py": "",
        "pkg/consts.py": "LIMIT = 10\n",
        "pkg/app.py": ("from pkg.consts import *\n"
                       "def get():\n    return LIMIT\n"),
    })
    index = S.build_project_index(tmp_path, _config(), key=None,
                                  cache=None)
    key = C.selection_name_key

    assert key("pkg/consts.py",
               "LIMIT") in index.files["pkg/app.py"].scope_refs["get"]


# --- regression: effect statements resolve refs (finding 2, attempt 3) ---

def test_build_effect_statements_resolve_refs_and_keep_plain_bound(tmp_path):
    # Mixed-target and mutating assigns are "effect", but their refs must
    # still resolve (design 4.4: upstream changes propagate through AM) and
    # their plain Name targets must stay bound (pre-fix assign behavior).
    init_git_repo(tmp_path, files={
        "pkg/__init__.py": "",
        "pkg/consts.py": "LIMIT = 10\n",
        "pkg/app.py": ("from pkg.consts import LIMIT\n"
                       "d = {}\n"
                       "x = d['k'] = LIMIT\n"
                       "SETTINGS = {}\n"
                       "SETTINGS['limit'] = LIMIT\n"
                       "(a, b.c) = 1, 2\n"),
    })
    index = S.build_project_index(tmp_path, _config(), key=None,
                                  cache=None)
    key = C.selection_name_key
    pf = index.files["pkg/app.py"]
    kinds = [st.kind for st in pf.index.statements]
    assert kinds == ["import", "assign", "effect", "assign", "effect",
                     "effect"]

    mixed = 2
    assert key("pkg/consts.py", "LIMIT") in pf.statement_refs[mixed]
    assert key("pkg/app.py", "x") in pf.statement_bound[mixed]

    mutating = 4
    assert key("pkg/consts.py", "LIMIT") in pf.statement_refs[mutating]
    assert pf.statement_bound[mutating] == frozenset()

    unpack = 5
    assert key("pkg/app.py", "a") in pf.statement_bound[unpack]


# --- regression: mutating assigns are effectful (finding 2) ---

def test_index_source_mutating_assign_targets_are_effect():
    index = S.index_source(
        b"X = 1\n"
        b'REG = {}\n'
        b'REG["a"] = 1\n'
        b"m.attr = 1\n"
        b"m.attr += 1\n"
        b"y += 1\n"
        b"x, y2 = 1, 2\n")
    kinds = [st.kind for st in index.statements]

    assert kinds == ["assign", "assign", "effect", "effect", "effect",
                     "assign", "assign"]
    assert index.statements[2].bound == ()
    assert index.statements[3].bound == ()


# --- regression: docstrings are observable fingerprints (finding 3) ---

def test_index_source_docstring_edit_changes_fingerprint():
    one = _scopes(S.index_source(
        b"def hello():\n    '''Say hello.'''\n    return 1\n"))["hello"]
    two = _scopes(S.index_source(
        b"def hello():\n    '''Say goodbye.'''\n    return 1\n"))["hello"]

    assert one.body != two.body
    assert one.skeleton == two.skeleton

    gone = _classes(S.index_source(b"class K:\n    '''Old help'''\n    X = 1\n"))["K"]
    changed = _classes(S.index_source(
        b"class K:\n    '''New help'''\n    X = 1\n"))["K"]

    assert gone.body != changed.body
    assert gone.skeleton == changed.skeleton
