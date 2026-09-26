"""Automatic evidence selection: ranking, source units, item packs (TDD).

Covers the new ``ptest.review_evidence`` module from the approved
design: pure resource ranking over the shared candidate ledger, exact
contiguous source units (never stitched text), and per-item packs that
combine configuration, setup, real callers, normal cleanup, and
exception/cancellation paths.

Abuse twins (secure-by-spec): oversized or partially parsed source is
recorded missing and never offered as an apparently complete unit;
lexical order is only a stable tie-break so filler renames cannot
change the selected semantic chain; no curated production paths.
"""
from __future__ import annotations


def _excerpt(path="src/db.py", start=1, text="x = 1\n", sha=None):
    from ptest import agent_assessment as AA

    lines = text.splitlines()
    end = start + max(len(lines), 1) - 1
    digest = sha or ("ab" * 32)
    return AA.SourceExcerpt(path, start, end, digest, text)


def _context(roles=(), relations=(), runner_kind="pytest"):
    from ptest import review_context as RC

    return RC.ReviewContext(
        runner_kind=runner_kind, roles=tuple(roles),
        relations=tuple(relations), config_status="resolved")


def test_source_id_is_stable_and_opaque():
    from ptest import review_evidence as RE

    unit = RE.SourceUnit("src/db.py", 3, 9, "ab" * 32, "x = 1\n", "source")
    first = RE.source_id("11" * 32, "DB-001", unit)
    assert first == RE.source_id("11" * 32, "DB-001", unit)
    assert first.startswith("src-") and len(first) == 28
    assert all(char in "0123456789abcdef" for char in first[4:])


def test_source_id_binds_packet_item_span_and_content():
    from ptest import review_evidence as RE

    unit = RE.SourceUnit("src/db.py", 3, 9, "ab" * 32, "x = 1\n", "source")
    base = RE.source_id("11" * 32, "DB-001", unit)
    moved = RE.SourceUnit("src/db.py", 4, 10, "ab" * 32, "x = 1\n", "source")
    assert RE.source_id("22" * 32, "DB-001", unit) != base
    assert RE.source_id("11" * 32, "DB-002", unit) != base
    assert RE.source_id("11" * 32, "DB-001", moved) != base
    changed = RE.SourceUnit(
        "src/db.py", 3, 9, "cd" * 32, "x = 1\n", "source")
    assert RE.source_id("11" * 32, "DB-001", changed) != base


def test_source_unit_rejects_stitched_or_empty_spans():
    from ptest import review_evidence as RE

    import pytest

    with pytest.raises((TypeError, ValueError)):
        RE.SourceUnit("src/db.py", 9, 3, "ab" * 32, "x = 1\n", "source")
    with pytest.raises((TypeError, ValueError)):
        RE.SourceUnit("src/db.py", 1, 1, "ab" * 32, "", "source")
    with pytest.raises((TypeError, ValueError)):
        RE.SourceUnit("src/db.py", 1, 1, "ab" * 32, "x = 1\n", "excluded")


def test_python_units_keep_setup_caller_and_finally_intact():
    from ptest import review_evidence as RE

    text = (
        "import sqlite3\n"
        "DB_URL = 'sqlite:///t.db'\n"
        "\n"
        "def make_db():\n"
        "    conn = sqlite3.connect(DB_URL)\n"
        "    try:\n"
        "        yield conn\n"
        "    finally:\n"
        "        conn.close()\n"
        "\n"
        "def test_writes(make_db):\n"
        "    make_db.execute('INSERT INTO t VALUES (1)')\n"
        "    assert make_db.execute('SELECT COUNT(*) FROM t').fetchone()\n"
    )
    excerpt = _excerpt("tests/test_db.py", text=text)
    units = RE.source_units(
        excerpt, "DB-001", _context(roles=(("tests/test_db.py", "test"),)))
    by_start = {unit.start_line: unit for unit in units}
    assert 4 in by_start  # make_db spans yield and finally together
    maker = by_start[4]
    assert "finally" in maker.text and "yield" in maker.text
    assert maker.end_line == 9
    caller = by_start[11]
    assert "assert" in caller.text  # caller assertions stay with owner
    import_names = [unit.text for unit in units if unit.start_line == 1]
    assert import_names and "import sqlite3" in import_names[0]
    constants = [unit for unit in units if "DB_URL" in unit.text]
    assert constants  # referenced module declarations are available


def test_pytest_runner_declarations_survive_function_item_filtering():
    from ptest import review_evidence as RE

    text = (
        "import pytest\n"
        "pytestmark = pytest.mark.real_commits\n"
        "pytest_plugins = ('shared_fixtures',)\n"
        "\n"
        "def test_database_write():\n"
        "    conn.execute('INSERT INTO records VALUES (1)')\n"
    )
    excerpt = _excerpt("tests/test_db.py", text=text)
    units = RE.source_units(
        excerpt, "DB-002", _context(roles=(("tests/test_db.py", "test"),)))

    assert any("pytestmark =" in unit.text and "real_commits" in unit.text
               for unit in units)
    assert any("pytest_plugins =" in unit.text and "shared_fixtures" in unit.text
               for unit in units)


def test_large_conftest_keeps_module_autouse_fixture_without_item_signal():
    from ptest import review_evidence as RE

    text = (
        "import pytest\n"
        "@pytest.fixture(autouse=True)\n"
        "def configure_process_environment():\n"
        "    previous = os.environ.get('APP_MODE')\n"
        "    os.environ['APP_MODE'] = 'test'\n"
        "    try:\n"
        "        yield\n"
        "    finally:\n"
        "        if previous is None:\n"
        "            os.environ.pop('APP_MODE', None)\n"
        "        else:\n"
        "            os.environ['APP_MODE'] = previous\n"
        + "# inert filler beyond the bounded whole-module unit\n" * 1700
        + "def unrelated_helper():\n    return 1\n"
    )
    excerpt = _excerpt("tests/conftest.py", text=text)
    units = RE.source_units(
        excerpt, "DB-002", _context(roles=(("tests/conftest.py", "fixture"),)))

    autouse = next(unit for unit in units
                   if "def configure_process_environment" in unit.text)
    assert "@pytest.fixture(autouse=True)" in autouse.text
    assert "finally:" in autouse.text and "os.environ.pop" in autouse.text
    assert not any("def unrelated_helper" in unit.text for unit in units)


def test_cache_identifier_and_used_local_import_form_one_item_chain():
    from ptest import review_evidence as RE

    caller_path = "api/tests/test_cache.py"
    alternate_caller_path = "api/tests/test_cache_alternate.py"
    helper_path = "api/tests/fixtures/ownership.py"
    caller = (
        "from fixtures.ownership import StoreCache\n"
        "\n"
        "def test_cache_owner_is_scoped():\n"
        "    cache = StoreCache('run-a')\n"
        "    cache.put('key', 'value')\n"
        "    assert cache.get('key') == 'value'\n"
    )
    helper = (
        "class StoreCache:\n"
        "    def __init__(self, owner):\n"
        "        self.owner = owner\n"
        "        self.values = {}\n"
        "\n"
        "    def put(self, key, value):\n"
        "        self.values[(self.owner, key)] = value\n"
        "\n"
        "    def get(self, key):\n"
        "        return self.values.get((self.owner, key))\n"
    )
    texts = {caller_path: caller,
             alternate_caller_path: caller.replace(
                 "test_cache_owner_is_scoped", "test_cache_alternate_owner"),
             helper_path: helper}
    context = _context(roles=(("tests/test_cache.py", "test"),
                              ("tests/test_cache_alternate.py", "test")))

    chains = RE.item_source_chains(
        "CACHE-001", texts, context, set(texts), declaration="api")

    assert chains and chains[0] == (caller_path, helper_path)
    assert (alternate_caller_path, helper_path) in chains
    helper_excerpt = _excerpt(helper_path, text=helper)
    helper_units = RE.source_units(
        helper_excerpt, "CACHE-001", _context(), whole_module=True)
    assert len(helper_units) == 1
    assert "class StoreCache" in helper_units[0].text
    assert "self.values" in helper_units[0].text


def test_used_imported_package_symbol_resolves_to_sibling_submodule():
    from ptest import review_evidence as RE

    caller_path = "api/tests/test_cache.py"
    package_init = "api/src/content_maker_api/__init__.py"
    cache_module = "api/src/content_maker_api/cache.py"
    texts = {
        caller_path: (
            "from content_maker_api import cache\n\n"
            "def test_cache_isolation():\n"
            "    client = cache.CacheClient()\n"
            "    client.clear()\n"
            "    assert client.get('key') is None\n"),
        package_init: "from . import cache\n",
        cache_module: (
            "class CacheClient:\n"
            "    def clear(self):\n"
            "        self.cache.clear()\n"
            "    def get(self, key):\n"
            "        return self.cache.get(key)\n"),
    }
    context = _context(roles=(("tests/test_cache.py", "test"),))

    chains = RE.item_source_chains(
        "CACHE-001", texts, context, set(texts), declaration="api")

    assert chains
    assert chains[0][0] == caller_path
    assert package_init in chains[0]
    assert cache_module in chains[0]


def test_caller_core_keeps_a_directly_called_same_file_helper():
    from ptest import review_evidence as RE

    text = (
        "def test_http_client_uses_request():\n"
        "    requests = _build_app()\n"
        "    response = requests.get('https://example.invalid')\n"
        "    assert response is not None\n"
        "\n"
        "def _build_app():\n"
        "    return make_session()\n"
        "\n"
        "def unrelated_helper():\n"
        "    return 'not part of the caller chain'\n")
    excerpt = _excerpt("tests/test_network.py", text=text)

    units = RE.source_units(
        excerpt, "NETWORK-001",
        _context(roles=(("tests/test_network.py", "test"),)),
        caller_core=True)

    assert any("def test_http_client_uses_request" in unit.text
               for unit in units)
    assert any("def _build_app" in unit.text for unit in units)
    assert not any("def unrelated_helper" in unit.text for unit in units)


def test_optional_test_callers_keep_their_own_direct_helpers():
    from ptest import review_evidence as RE

    text = (
        "def test_requests_client_uses_request():\n"
        "    requests = _build_requests_client()\n"
        "    response = requests.get('https://example.invalid')\n"
        "    assert response is not None\n"
        "\n"
        "def test_httpx_client_uses_request():\n"
        "    httpx = _build_httpx_client()\n"
        "    response = httpx.get('https://example.invalid')\n"
        "    assert response is not None\n"
        "\n"
        "def _build_requests_client():\n"
        "    return make_requests_session()\n"
        "\n"
        "def _build_httpx_client():\n"
        "    return make_httpx_client()\n"
        "\n"
        "def unrelated_helper():\n"
        "    return 'not part of either caller'\n")
    excerpt = _excerpt("tests/test_network.py", text=text)

    units = RE.source_units(
        excerpt, "NETWORK-001",
        _context(roles=(("tests/test_network.py", "test"),)))
    joined = "\n".join(unit.text for unit in units)

    assert "def test_requests_client_uses_request" in joined
    assert "def _build_requests_client" in joined
    assert "def test_httpx_client_uses_request" in joined
    assert "def _build_httpx_client" in joined
    assert "def unrelated_helper" not in joined


def test_python_partial_parse_offers_no_unit():
    from ptest import review_evidence as RE

    excerpt = _excerpt("src/broken.py", text="def broken(:\n    pass\n")
    assert RE.source_units(excerpt, "DB-001", _context()) == ()


def test_oversized_function_is_missing_never_clipped():
    from ptest import review_evidence as RE

    body = "    x = %d\n" % 1
    text = "def huge():\n" + body * 2000 + "    return 1\n"
    excerpt = _excerpt("src/huge.py", text=text)
    units = RE.source_units(excerpt, "DB-001", _context())
    assert all("def huge" not in unit.text for unit in units)
    assert all(
        unit.end_line - unit.start_line + 1 <= RE.MAX_UNIT_LINES
        for unit in units)


def test_js_module_unit_is_whole_and_bounded():
    from ptest import review_evidence as RE

    text = ("import {createClient} from 'redis';\n\n"
            "const cache = createClient();\n"
            "await cache.flushDb();\n")
    excerpt = _excerpt("web/src/__tests__/y.test.ts", text=text)
    units = RE.source_units(
        excerpt, "CACHE-001",
        _context(roles=(("web/src/__tests__/y.test.ts", "test"),),
                 runner_kind="vitest"))
    assert len(units) == 1
    assert units[0].start_line == 1
    assert units[0].end_line == 4
    assert units[0].text == text


def test_literal_invocation_chain_is_bounded_and_ignores_comments():
    from ptest import review_evidence as RE

    candidates = {"install.sh", "scripts/install.py", "scripts/decoy.py"}
    caller = (
        "from pathlib import Path\n"
        "import subprocess\n"
        "installer = Path(__file__).resolve().parents[2] / 'install.sh'\n"
        "subprocess.run(['bash', str(installer)], check=True)\n")
    assert RE.static_local_invocation_targets(
        "tests/ng/test_install.py", caller, candidates) == ("install.sh",)

    shell = (
        'source_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\n'
        'python3 "$source_dir/scripts/install.py"\n'
        '# python3 "$source_dir/scripts/decoy.py"\n'
        'echo "$source_dir/scripts/decoy.py"\n')
    assert RE.static_local_invocation_targets(
        "install.sh", shell, candidates) == ("scripts/install.py",)

    # A cwd-relative string does not become source-relative evidence.
    cwd_relative = "subprocess.run(['python', 'scripts/install.py'])\n"
    assert RE.static_local_invocation_targets(
        "tests/ng/test_install.py", cwd_relative, candidates) == ()


def _rank_fixture(tmp_path, filler=100):
    from ptest import agent_assessment as AA
    from ptest import review_context as RC

    filler_texts = {
        f"src/filler_{index:03d}.py": "VALUE_%d = %d\n" % (index, index)
        for index in range(filler)
    }
    texts = dict(filler_texts)
    texts.update({
        "src/db.py": (
            "import sqlite3\n"
            "def open_db():\n"
            "    return sqlite3.connect('f.db')\n"),
        "tests/test_db.py": (
            "def test_writes(open_db):\n"
            "    open_db.execute('CREATE TABLE t (a)')\n"
            "    assert True\n"),
        "tests/conftest.py": (
            "import pytest\n"
            "from src.db import open_db\n"
            "@pytest.fixture\n"
            "def _db(open_db):\n"
            "    return open_db\n"),
        "src/cleanup.py": (
            "def close_db(conn):\n"
            "    try:\n"
            "        conn.commit()\n"
            "    finally:\n"
            "        conn.close()\n"),
        "src/timeout.py": (
            "def fetch(conn):\n"
            "    try:\n"
            "        return conn.execute('SELECT 1', timeout=5)\n"
            "    except TimeoutError:\n"
            "        conn.close()\n"
            "        raise\n"),
    })
    context = RC.ReviewContext(
        runner_kind="pytest",
        roles=(("tests/conftest.py", "fixture"),
               ("tests/test_db.py", "test"),
               ("src/db.py", "source"),
               ("src/cleanup.py", "source"),
               ("src/timeout.py", "source")),
        relations=(("tests/test_db.py", "tests/conftest.py", "fixture-use"),
                   ("tests/conftest.py", "src/db.py", "local-import")),
        config_status="resolved")
    return context, texts


def test_dangerous_caller_after_filler_reaches_its_pack():
    from ptest import review_evidence as RE
    from ptest.checklist import CATALOG

    context, texts = _rank_fixture(None)
    ranked = RE.rank_candidates(
        context, tuple(texts), texts, tuple(CATALOG))
    assert "tests/test_db.py" in ranked
    assert "src/db.py" in ranked
    assert ranked.index("src/db.py") < 100


def test_factory_through_fixture_cleanup_and_timeout_are_ranked():
    from ptest import review_evidence as RE
    from ptest.checklist import CATALOG

    context, texts = _rank_fixture(None)
    ranked = RE.rank_candidates(
        context, tuple(texts), texts, tuple(CATALOG))
    assert "tests/conftest.py" in ranked[:60]
    assert "src/cleanup.py" in ranked
    assert "src/timeout.py" in ranked


def test_timer_caller_outranks_generic_sleep_and_keeps_operation_unit():
    from ptest import agent_assessment as AA
    from ptest import review_context as RC
    from ptest import review_evidence as RE

    texts = {
        "tests/test_sleep.py": "def test_wait():\n    time.sleep(1)\n",
        "tests/test_timer.py": (
            "def test_queue_wait():\n"
            "    timer = threading.Timer(25.0, release)\n"
            "    timer.start()\n"),
    }
    context = RC.ReviewContext(
        runner_kind="pytest",
        roles=tuple((path, "test") for path in texts),
        config_status="resolved")
    ranked = RE.rank_item_candidates(
        context, texts, texts, "TIME-001")
    assert ranked[0] == "tests/test_timer.py"
    excerpts = tuple(_excerpt(path, text=text) for path, text in texts.items())
    packet = AA.EvidencePacket(
        declaration=".", project_id="cd" * 16, scope=".",
        packet_sha256="11" * 32, excerpts=excerpts, dependencies=(),
        runner_kind="pytest", excluded_count=0, truncated_count=0,
        file_count=len(excerpts), byte_count=sum(
            len(excerpt.text.encode()) for excerpt in excerpts),
        context=context)
    initial, reserve, _missing = RE.select_item_sources(packet, "TIME-001")
    assert any(unit.path == "tests/test_timer.py"
               and "threading.Timer(25.0" in unit.text
               for unit in (*initial, *reserve))


def test_filler_rename_and_reorder_keep_semantic_chain():
    from ptest import review_evidence as RE
    from ptest.checklist import CATALOG

    context, texts = _rank_fixture(None)
    ranked = RE.rank_candidates(
        context, tuple(texts), texts, tuple(CATALOG))
    renamed = {path: body for path, body in texts.items()}
    for index in range(100):
        renamed[f"zzz/renamed_{99 - index:03d}.py"] = renamed.pop(
            f"src/filler_{index:03d}.py")
    renamed_context = _context(
        roles=context.roles, relations=context.relations)
    reranked = RE.rank_candidates(
        renamed_context, tuple(renamed), renamed, tuple(CATALOG))
    semantic = [path for path in ranked if not path.startswith("src/filler_")]
    resemantic = [path for path in reranked
                  if not path.startswith("zzz/renamed_")]
    assert semantic == resemantic


def test_packet_prefix_maps_context_relations_without_suffix_guessing():
    from ptest import agent_assessment as AA
    from ptest import review_evidence as RE

    context = _context(
        roles=(("tests/test_db.py", "test"),
               ("tests/conftest.py", "fixture"),
               ("src/db.py", "helper")),
        relations=(("tests/test_db.py", "tests/conftest.py", "fixture-use"),
                   ("tests/conftest.py", "src/db.py", "local-import")))
    excerpts = (
        _excerpt("api/tests/test_db.py", text=(
            "def test_writes(db):\n    assert db.execute('select 1')\n")),
        _excerpt("api/tests/conftest.py", text=(
            "import pytest\n"
            "@pytest.fixture\n"
            "def db():\n"
            "    yield object()\n")),
        _excerpt("api/src/db.py", text=(
            "import sqlite3\n"
            "def connect():\n"
            "    conn = sqlite3.connect(':memory:')\n"
            "    try:\n        yield conn\n"
            "    finally:\n        conn.close()\n")),
        _excerpt("api/nested/tests/conftest.py", text=(
            "def unrelated():\n    return 'not this child'\n")),
    )
    packet = AA.EvidencePacket(
        declaration="api", project_id="cd" * 16, scope="api",
        packet_sha256="11" * 32, excerpts=excerpts, dependencies=(),
        runner_kind="pytest", excluded_count=0, truncated_count=0,
        file_count=len(excerpts), byte_count=sum(
            len(excerpt.text.encode()) for excerpt in excerpts),
        context=context)

    initial, reserve, _missing = RE.select_item_sources(packet, "DB-001")
    paths = {unit.path for unit in (*initial, *reserve)}
    assert "api/tests/test_db.py" in paths
    assert "api/tests/conftest.py" in paths
    assert "api/src/db.py" in paths
    nested_context = RE._with_declaration_prefix(context, "api")
    assert RE._source_role("api/nested/tests/conftest.py",
                           nested_context) == "source"
    nested_units = [unit for unit in (*initial, *reserve)
                    if unit.path == "api/nested/tests/conftest.py"]
    assert all(unit.role == "source" for unit in nested_units)
    assert len(initial) + len(reserve) <= RE.MAX_UNITS_PER_ITEM


def test_many_fixture_imports_cannot_starve_later_active_caller():
    from ptest import agent_assessment as AA
    from ptest import review_evidence as RE

    imports = "".join(f"import fixture_pkg.module_{index}\n"
                      for index in range(90))
    excerpts = (
        _excerpt("tests/conftest.py", text=(
            imports + "@pytest.fixture\ndef db():\n"
            "    yield object()\n")),
        _excerpt("tests/test_db.py", text=(
            "def test_writes(db):\n"
            "    assert db.execute('select 1')\n")),
        _excerpt("src/db.py", text=(
            "import sqlite3\n"
            "def db_connect():\n"
            "    conn = sqlite3.connect(':memory:')\n"
            "    try:\n        yield conn\n"
            "    finally:\n        conn.close()\n")),
    )
    context = _context(
        roles=(("tests/conftest.py", "fixture"),
               ("tests/test_db.py", "test"),
               ("src/db.py", "helper")),
        relations=(("tests/test_db.py", "tests/conftest.py", "fixture-use"),
                   ("tests/conftest.py", "src/db.py", "local-import")))
    packet = AA.EvidencePacket(
        declaration=".", project_id="cd" * 16, scope=".",
        packet_sha256="11" * 32, excerpts=excerpts, dependencies=(),
        runner_kind="pytest", excluded_count=0, truncated_count=0,
        file_count=len(excerpts), byte_count=sum(
            len(excerpt.text.encode()) for excerpt in excerpts),
        context=context)

    initial, reserve, _missing = RE.select_item_sources(packet, "DB-001")
    selected = (*initial, *reserve)
    paths = {unit.path for unit in selected}
    assert {"tests/test_db.py", "tests/conftest.py", "src/db.py"} <= paths
    assert len(initial) <= RE.MAX_INITIAL_UNITS
    assert len(selected) <= RE.MAX_UNITS_PER_ITEM
    assert any(unit.path == "tests/conftest.py"
               and "@pytest.fixture" in unit.text
               and "yield object()" in unit.text for unit in selected)
    assert any(unit.path == "tests/test_db.py"
               and "db.execute" in unit.text
               and "assert" in unit.text for unit in selected)
    assert any(unit.path == "src/db.py"
               and "finally:" in unit.text
               and "conn.close()" in unit.text for unit in selected)


def test_every_model_item_gets_an_opportunity_first():
    from ptest import review_evidence as RE
    from ptest.checklist import CATALOG

    context, texts = _rank_fixture(None, filler=4)
    ranked = RE.rank_candidates(
        context, tuple(texts), texts, tuple(CATALOG))
    from ptest import deterministic_items as DI

    model_ids = [entry.id for entry in CATALOG
                 if entry.id not in DI.DETERMINISTIC_ITEM_IDS]
    assert len(model_ids) == 9
    assert len(ranked) >= len(model_ids)


def test_ranking_is_a_no_op_sensitive_pure_function():
    from ptest import review_evidence as RE
    from ptest.checklist import CATALOG

    context, texts = _rank_fixture(None, filler=2)
    ranked = RE.rank_candidates(
        context, tuple(texts), texts, tuple(CATALOG))
    assert set(ranked) == set(texts)
    assert len(set(ranked)) == len(ranked)
    reranked = RE.rank_candidates(
        context, tuple(texts), texts, tuple(CATALOG))
    assert reranked == ranked


def _packet_for_select(tmp_path):
    from ptest import contracts as C
    from ptest import doctor

    (tmp_path / "tests").mkdir(parents=True)
    (tmp_path / "tests" / "conftest.py").write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def conn():\n"
        "    yield 'c'\n",
        encoding="utf-8")
    (tmp_path / "tests" / "test_db.py").write_text(
        "def test_writes(conn):\n"
        "    assert conn\n",
        encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "helper.py").write_text(
        "UNRELATED = 1\n", encoding="utf-8")
    config = C.Config(
        runner=C.RunnerConfig(kind=C.RunnerKind.PYTEST,
                              launcher=("uv",), test_roots=("tests",)),
        setup=None, resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(enabled=False, closed_inputs=False),
        project_id="cd" * 16)
    resolution = C.ConfigResolution(
        root=tmp_path, path=None, config=config, monorepo=None,
        provenance=(), warnings=(), problem=None)
    domain = C.DomainPaths(
        root=tmp_path, machine_config=tmp_path / ".ptest" / "config.toml",
        ledger=tmp_path / ".ptest" / "ledger",
        marker=tmp_path / ".ptest" / "marker", fixture=True,
        domain_id=None)
    workspace = doctor.inspect_workspace(
        domain, resolution, C.DEFAULT_SCAN_LIMITS, None)
    from ptest import agent_assessment as AA

    return AA.build_packets(workspace, resolution)[0]


def test_select_item_sources_uses_packet(tmp_path):
    from ptest import review_evidence as RE

    packet = _packet_for_select(tmp_path)
    initial, reserve, missing = RE.select_item_sources(packet, "FIX-002")
    assert initial
    assert len(initial) <= 64
    initial_paths = {unit.path for unit in initial}
    assert "tests/conftest.py" in initial_paths, (
        packet.context.roles,
        packet.context.missing,
        initial_paths,
        missing)
    assert "tests/test_db.py" in initial_paths
    assert "src/helper.py" not in initial_paths
    assert all(isinstance(unit, RE.SourceUnit) for unit in initial)
    assert all(isinstance(unit, RE.SourceUnit) for unit in reserve)
    assert len(reserve) <= 64
    assert len(initial) + len(reserve) <= RE.MAX_UNITS_PER_ITEM
    for path, reason in missing:
        assert isinstance(path, str) and isinstance(reason, str)
    for unit in initial:
        owner = next(excerpt for excerpt in packet.excerpts
                     if excerpt.path == unit.path
                     and excerpt.sha256 == unit.source_sha256)
        assert excerpt_lines(owner)[unit.start_line - owner.start_line:
                                    unit.end_line - owner.start_line + 1]


def excerpt_lines(owner):
    return owner.text.splitlines()
