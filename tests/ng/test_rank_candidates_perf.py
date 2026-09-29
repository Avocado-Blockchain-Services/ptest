"""Ranking performance contract: identical selection, fewer operations (T2).

Base-captured goldens (commit 09f6f40, captured with
``uv run --locked --extra test python /tmp/capture_goldens.py`` and
``/tmp/capture_packets.py`` on unmodified code) pin
``rank_candidates`` / ``rank_item_candidates`` / ``item_source_chains``
outputs and online ``packet_sha256`` values. Differential oracles keep the
old helpers verbatim as references. Operation-count tests prove the
reductions deterministically (no wall-clock assertions).
"""
from __future__ import annotations

import re


def _perf_fixture():
    from ptest import review_context as RC

    roles = []
    relations = []
    candidates = []
    texts = {}
    for i in range(30):
        path = f"src/mod{i:02d}.py"
        roles.append((path, "source"))
        candidates.append(path)
        texts[path] = (
            f"import sqlite3\nDB_URL = 'sqlite:///t{i}.db'\n\n"
            f"def handle_{i}(request):\n    conn = sqlite3.connect(DB_URL)\n"
            f"    cur = conn.cursor()\n    cur.execute('SELECT 1')\n"
            f"    return cur.fetchall()\n"
        )
    for i in range(10):
        path = f"src/comp{i:02d}.js"
        roles.append((path, "source"))
        candidates.append(path)
        texts[path] = (
            "import { useState } from 'react';\n"
            f"export function Comp{i}(props) {{\n"
            "  const [x, setX] = useState(0);\n"
            f"  return fetch('/api/{i}');\n}}\n"
        )
    roles.append(("tests/conftest.py", "fixture"))
    texts["tests/conftest.py"] = (
        "import pytest\n@pytest.fixture\ndef db():\n    return object()\n")
    candidates.append("tests/conftest.py")
    for i in range(6):
        path = f"tests/test_mod{i:02d}.py"
        roles.append((path, "test"))
        candidates.append(path)
        texts[path] = (
            f"def test_handle_{i}(db):\n"
            f"    from src.mod{i:02d} import handle_{i}\n"
            f"    assert handle_{i}(db) is not None\n"
        )
        relations.append((path, f"src/mod{i:02d}.py", "fixture-use"))
        relations.append(
            (f"src/mod{i:02d}.py", path, "local-import"))
    roles.append((".ptest.toml", "config"))
    texts[".ptest.toml"] = 'version = 1\nproject_id = "aa"\n'
    candidates.append(".ptest.toml")
    roles.append(("src/helpers.py", "helper"))
    texts["src/helpers.py"] = "def _retry(fn):\n    return fn()\n"
    candidates.append("src/helpers.py")
    roles.append(("src/setup_env.py", "setup"))
    texts["src/setup_env.py"] = "SETUP_TOKEN = 'x'\n"
    candidates.append("src/setup_env.py")
    context = RC.ReviewContext(
        runner_kind="pytest", roles=tuple(roles),
        relations=tuple(relations), config_status="resolved")
    assert len(roles) > 30
    return context, candidates, texts


GOLDEN_RANKED = [
    ".ptest.toml",
    "tests/conftest.py",
    "src/mod00.py",
    "src/mod01.py",
    "tests/test_mod00.py",
    "tests/test_mod01.py",
    "src/comp00.js",
    "tests/test_mod02.py",
    "tests/test_mod03.py",
    "tests/test_mod04.py",
    "tests/test_mod05.py",
    "src/mod02.py",
    "src/mod03.py",
    "src/comp01.js",
    "src/comp02.js",
    "src/comp03.js",
    "src/comp04.js",
    "src/comp05.js",
    "src/comp06.js",
    "src/comp07.js",
    "src/mod04.py",
    "src/mod05.py",
    "src/comp08.js",
    "src/comp09.js",
    "src/helpers.py",
    "src/mod06.py",
    "src/mod07.py",
    "src/mod08.py",
    "src/mod09.py",
    "src/mod10.py",
    "src/mod11.py",
    "src/mod12.py",
    "src/mod13.py",
    "src/mod14.py",
    "src/mod15.py",
    "src/mod16.py",
    "src/mod17.py",
    "src/mod18.py",
    "src/mod19.py",
    "src/mod20.py",
    "src/mod21.py",
    "src/mod22.py",
    "src/mod23.py",
    "src/mod24.py",
    "src/mod25.py",
    "src/mod26.py",
    "src/mod27.py",
    "src/mod28.py",
    "src/mod29.py",
    "src/setup_env.py",
]

GOLDEN_DB001 = [
    ".ptest.toml",
    "src/mod00.py",
    "src/mod01.py",
    "src/mod02.py",
    "src/mod03.py",
    "src/mod04.py",
    "src/mod05.py",
    "src/mod06.py",
    "src/mod07.py",
    "src/mod08.py",
    "src/mod09.py",
    "src/mod10.py",
    "src/mod11.py",
    "src/mod12.py",
    "src/mod13.py",
    "src/mod14.py",
    "src/mod15.py",
    "src/mod16.py",
    "src/mod17.py",
    "src/mod18.py",
    "src/mod19.py",
    "src/mod20.py",
    "src/mod21.py",
    "src/mod22.py",
    "src/mod23.py",
    "src/mod24.py",
    "src/mod25.py",
    "src/mod26.py",
    "src/mod27.py",
    "src/mod28.py",
    "src/mod29.py",
    "tests/conftest.py",
    "tests/test_mod00.py",
    "tests/test_mod01.py",
    "tests/test_mod02.py",
    "tests/test_mod03.py",
    "tests/test_mod04.py",
    "tests/test_mod05.py",
    "src/comp00.js",
    "src/comp01.js",
    "src/comp02.js",
    "src/comp03.js",
    "src/comp04.js",
    "src/comp05.js",
    "src/comp06.js",
    "src/comp07.js",
    "src/comp08.js",
    "src/comp09.js",
    "src/helpers.py",
    "src/setup_env.py",
]

GOLDEN_CHAINS = [
    ["tests/test_mod00.py", "src/mod00.py", "tests/conftest.py"],
    ["tests/test_mod01.py", "src/mod01.py", "tests/conftest.py"],
    ["tests/test_mod02.py", "src/mod02.py", "tests/conftest.py"],
    ["tests/test_mod03.py", "src/mod03.py", "tests/conftest.py"],
    ["tests/test_mod04.py", "src/mod04.py", "tests/conftest.py"],
    ["tests/test_mod05.py", "src/mod05.py", "tests/conftest.py"],
]

GOLDEN_STANDALONE_SHA = (
    "44dc3183db4c1654f5991fdd4cc37127d6682ca3e6b1d451fc3e0f80396e9255")
GOLDEN_V2_SHA = (
    "e9914910a10c88d18262c2aac2e551fbf8267b5941e3f16fff3831008f2d2be3")


def test_rank_candidates_matches_base_golden():
    from ptest import review_evidence as RE
    from ptest.checklist import CATALOG

    context, candidates, texts = _perf_fixture()
    assert list(RE.rank_candidates(
        context, candidates, texts, CATALOG)) == GOLDEN_RANKED


def test_rank_item_candidates_matches_base_golden():
    from ptest import review_evidence as RE

    context, candidates, texts = _perf_fixture()
    assert list(RE.rank_item_candidates(
        context, candidates, texts, "DB-001")) == GOLDEN_DB001


def test_item_source_chains_matches_base_golden():
    from ptest import review_evidence as RE

    context, candidates, texts = _perf_fixture()
    chains = RE.item_source_chains(
        "DB-001", texts, context, tuple(candidates))
    assert [list(chain) for chain in chains] == GOLDEN_CHAINS


def test_online_packet_sha256_matches_base_golden(tmp_path):
    from ptest import agent_assessment as AA
    from ptest import contracts as C
    from ptest import doctor
    from factories_agents import (
        agent_domain as _domain,
        agent_resolution as _resolution,
        doctor_assessment_project,
    )

    doctor_assessment_project(tmp_path, "aa" * 8)
    resolution = _resolution(tmp_path)
    workspace = doctor.inspect_workspace(
        _domain(tmp_path), resolution, C.DEFAULT_SCAN_LIMITS, None)
    packet = AA.build_packets(workspace, resolution)[0]
    assert packet.packet_sha256 == GOLDEN_STANDALONE_SHA


def test_online_v2_child_packet_sha256_matches_base_golden(tmp_path):
    from ptest import agent_assessment as AA
    from ptest import config as config_api
    from ptest import contracts as C
    from ptest import doctor
    from factories_agents import (
        agent_domain as _domain,
        agent_v1_config_text as _v1_config_text,
    )

    root = tmp_path
    (root / ".ptest.toml").write_text(
        'version = 2\n[monorepo]\nchildren = ["api"]\n', encoding="utf-8")
    child = root / "api"
    child.mkdir()
    (child / ".ptest.toml").write_text(
        _v1_config_text("bb" * 16, "pytest"), encoding="utf-8")
    src = child / "src"
    src.mkdir()
    (src / "app.py").write_text(
        "import sqlite3\ndef get_db():\n    return sqlite3.connect('a.db')\n",
        encoding="utf-8")
    tests = child / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text(
        "def test_get_db(db):\n    from src.app import get_db\n"
        "    assert get_db() is not None\n",
        encoding="utf-8")
    resolution = config_api.resolve_config(root)
    workspace = doctor.inspect_workspace(
        _domain(root), resolution, C.DEFAULT_SCAN_LIMITS, None)
    packets = AA.build_packets(workspace, resolution)
    assert [(p.declaration, p.packet_sha256) for p in packets] == [
        ("api", GOLDEN_V2_SHA)]


# --- Differential oracles: old implementations kept verbatim ---------------

def _old_identifier_words(name):
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return re.sub(r"_+", " ", split)


def _old_python_identifier_text(text):
    import io
    import tokenize

    try:
        names = [token.string for token in tokenize.generate_tokens(
            io.StringIO(text).readline) if token.type == tokenize.NAME]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return ""
    return " ".join(_old_identifier_words(name) for name in names)


def _old_relation_score(path, context):
    roles = dict(context.roles)
    outgoing = {source for source, _target, _kind in context.relations}
    incoming = {target for _source, target, _kind in context.relations}
    score = 0
    if path in outgoing:
        score += 2
    if path in incoming:
        score += 3
    role = roles.get(path)
    if role in {"config", "setup", "fixture"}:
        score += 5
    elif role == "helper":
        score += 4
    elif role == "test":
        score += 3
    return score


def _old_source_role(path, context):
    from ptest import review_evidence as RE

    if RE._is_config_path(path, context):
        return "config"
    return dict(context.roles).get(path, "source")


def _old_call_hit(patterns, call_names):
    return any(pattern.search(name) or pattern.search(name + "(")
               for name in call_names for pattern in patterns)


IDENTIFIER_CORPUS = [
    "def handleRequest(db_conn):\n    my_var = 1\n",
    "class HTTPServer:\n    def __init__(self):\n        self.a1B = 1\n",
    "def __dunder__(snake__case_):\n    return snake__case_\n",
    "def café_naïve(über_var):\n    return über_var\n",
    "def broken(:\n    oops = \n",
    "",
    "x = 1\n",
    "async def fetchURLs2JSON(http_client):\n    await http_client.get()\n",
]


def test_python_identifier_text_matches_old_reference():
    from ptest import review_evidence as RE

    for text in IDENTIFIER_CORPUS:
        assert RE._python_identifier_text(text) == \
            _old_python_identifier_text(text), text
    # Would fail for a no-op (empty) implementation.
    assert RE._python_identifier_text(IDENTIFIER_CORPUS[0]) != ""


def test_relation_score_role_config_matches_old_reference():
    from ptest import review_evidence as RE

    context, candidates, texts = _perf_fixture()
    for path in candidates:
        assert RE._relation_score(path, context) == \
            _old_relation_score(path, context), path
        assert RE._source_role(path, context) == \
            _old_source_role(path, context), path
        assert RE._is_config_path(path, context) == (
            path in (".ptest.toml",)), path


def test_call_names_hit_matches_old_double_loop():
    from ptest import review_evidence as RE
    from ptest.checklist import CATALOG

    call_name_sets = [
        ("connect", "db.execute", "fetch", "cache.get("),
        ("handle_0", "sqlite3.connect", "test_handle_0"),
        ("Comp0", "useState", "fetch"),
        (),
        ("(", "x" * 200),
    ]
    for entry in CATALOG:
        patterns = RE._resource_patterns(entry)
        for names in call_name_sets:
            assert RE._call_names_hit(patterns, names) == \
                _old_call_hit(patterns, names), (entry.id, names)
    crafted = [r"^connect", r"execute$", r"\bconnect\b", r"\s",
               r"\Aconnect", r"con(?<=con)nect", r"con(?=nect)",
               r"fet(?!ch)", r"db\.execute\(", r"missing-zzz"]
    for expr in crafted:
        patterns = (re.compile(expr, re.IGNORECASE),)
        for names in call_name_sets:
            assert RE._call_names_hit(patterns, names) == \
                _old_call_hit(patterns, names), (expr, names)
    assert RE._call_names_hit((), ("connect",)) is False
    assert RE._call_names_hit(
        (re.compile("connect"),), ()) is False


# --- Operation counts (deterministic, no wall clock) ------------------------

def test_context_index_built_once_per_rank_candidates(monkeypatch):
    from ptest import review_evidence as RE
    from ptest.checklist import CATALOG

    real = RE._build_context_index
    calls = []

    def counting(context):
        calls.append(1)
        return real(context)

    monkeypatch.setattr(RE, "_build_context_index", counting)
    context, candidates, texts = _perf_fixture()
    RE.rank_candidates(context, candidates, texts, CATALOG)
    assert len(calls) == 1
    calls.clear()
    RE.rank_candidates(context, candidates, texts, CATALOG,
                       signal_cache={})
    assert len(calls) == 1
    calls.clear()
    RE.rank_item_candidates(context, candidates, texts, "DB-001")
    assert len(calls) == 1


class _CountingPattern:
    """Proxy exposing .pattern/.flags/.search while counting searches."""

    def __init__(self, expr):
        self._inner = re.compile(expr, re.IGNORECASE)
        self.searches = 0

    @property
    def pattern(self):
        return self._inner.pattern

    @property
    def flags(self):
        return self._inner.flags

    def search(self, string, *args, **kwargs):
        self.searches += 1
        return self._inner.search(string, *args, **kwargs)


def test_call_names_hit_prefilters_without_exact_searches(monkeypatch):
    from ptest import review_evidence as RE

    patterns = tuple(_CountingPattern(expr) for expr in
                     ("zz-no-such-call-zzz", r"qqq_missing\(",
                      r"another\.absent\.name"))
    names = ("connect", "db.execute", "fetch", "Comp0", "useState")
    assert RE._call_names_hit(patterns, names) is False
    # No-hit path: zero exact-loop searches on the proxies.
    assert sum(p.searches for p in patterns) == 0

    real_prefilter = RE._prefilter_for
    prefilter_calls = []

    def counting_prefilter(pattern):
        prefilter_calls.append(pattern)
        return real_prefilter(pattern)

    monkeypatch.setattr(RE, "_prefilter_for", counting_prefilter)
    assert RE._call_names_hit(patterns, names) is False
    # One prefilter search per pattern, independent of name count.
    assert len(prefilter_calls) == len(patterns)
    assert sum(p.searches for p in patterns) == 0
    # A hit still reports True through the exact loop.
    hitter = (_CountingPattern("db\\.execute"),)
    assert RE._call_names_hit(hitter, names) is True


class _CountingSplitter:
    def __init__(self):
        self.subs = 0

    def sub(self, repl, string, *args, **kwargs):
        self.subs += 1
        return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|_+",
                      repl, string, *args, **kwargs)


def test_python_identifier_text_single_regex_pass(monkeypatch):
    from ptest import review_evidence as RE

    splitter = _CountingSplitter()
    monkeypatch.setattr(RE, "_IDENTIFIER_SPLIT_RE", splitter)
    text = ("def handleRequest(db_conn):\n"
            "    my_var2X = fetchURLs2JSON(http_client)\n")
    assert RE._python_identifier_text(text) == \
        _old_python_identifier_text(text)
    assert splitter.subs == 1
