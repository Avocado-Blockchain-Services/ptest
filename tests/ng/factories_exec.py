"""Fake-executable and subprocess-project factories (T4, test-only).

Function-scoped builders for tests that spawn subprocesses or probe
adapter preparation:

* ``fake_bin``: a per-test ``bin`` directory prepended to ``PATH``.
* ``fake_exec``: write an owned executable script (argv/env recorders
  live here as text builders) into ``fake_bin`` or an explicit dir.
* ``fake_pytest_project``: a tmp pytest project with a given test
  layout, for the subprocess suites.
* ``fake_exec_vitest`` / ``fake_exec_go`` / ``fake_exec_cargo`` /
  ``fake_exec_node``: argv/env-recording fake runners.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import support


def recording_script(*, record: str, exit_file: str | None = None,
                     order_log: str | None = None,
                     order_token: str | None = None,
                     record_env: bool = True) -> str:
    """Python script text recording its invocation as JSON.

    The script appends ``{"argv": ..., "cwd": ..., "env": ...}`` to the
    ``record`` file (resolved against the process working directory).
    When ``exit_file`` names a file that exists, the script exits with
    its integer content.  When ``order_log`` is set, ``order_token``
    is appended there on every invocation.
    """
    payload = {
        "record": record,
        "exit_file": exit_file,
        "order_log": order_log,
        "order_token": order_token,
        "record_env": record_env,
    }
    return (
        "#!" + sys.executable + "\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        f"_SPEC = {json.dumps(payload)}\n"
        "root = Path(os.getcwd())\n"
        "entry = {'argv': sys.argv, 'cwd': os.getcwd()}\n"
        "if _SPEC['record_env']:\n"
        "    entry['env'] = dict(os.environ)\n"
        "with open(root / _SPEC['record'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(entry) + '\\n')\n"
        "if _SPEC['order_log']:\n"
        "    with open(root / _SPEC['order_log'], 'a', encoding='utf-8') as log:\n"
        "        log.write((_SPEC['order_token'] or 'exec') + '\\n')\n"
        "exit_name = _SPEC['exit_file']\n"
        "if exit_name and (root / exit_name).exists():\n"
        "    raise SystemExit(int((root / exit_name).read_text().strip()))\n"
    )


#: ``node``-shaped recorder used by the vitest execution tests: the
#: record file holds one JSON object (``node-record.json``, rewritten on
#: every invocation), every invocation appends ``node`` to
#: ``order.log``, and the ``node-exit`` file controls the exit status.
FAKE_NODE_SCRIPT = (
    "#!/usr/bin/env python3\n"
    "import json, os, sys\n"
    "from pathlib import Path\n"
    "root = Path(os.getcwd())\n"
    "(root / 'node-record.json').write_text(json.dumps(\n"
    "    {'argv': sys.argv, 'cwd': os.getcwd()}))\n"
    "with open(root / 'order.log', 'a') as log:\n"
    "    log.write('node\\n')\n"
    "exit_file = root / 'node-exit'\n"
    "raise SystemExit(int(exit_file.read_text().strip()) if exit_file.exists() else 0)\n"
)

#: ``claude``-shaped fake answering one one-row reply per item request.
FAKE_CLAUDE_SCRIPT = "\n".join([
    "#!" + sys.executable,
    "import json, os, sys",
    "here = os.path.dirname(os.path.abspath(sys.argv[0]))",
    "if len(sys.argv) > 1 and sys.argv[1] == '--version':",
    "    sys.stdout.write('claude-test 1.0\\n')",
    "    sys.exit(0)",
    "request = json.load(sys.stdin)",
    "item_id = request['policy']['item']['id']",
    "with open(os.path.join(here, 'argv.log'), 'a',",
    "          encoding='utf-8') as handle:",
    "    handle.write(json.dumps({'argv': sys.argv[1:],",
    "                              'item': item_id}) + '\\n')",
    "excerpts = request['excerpts']",
    "if excerpts:",
    "    first = excerpts[0]",
    "    quote = first['text'].splitlines()[0][:512]",
    "    reply = {'status': 'satisfied',",
    "             'rationale': ('Reviewed ' + item_id + ' against '",
    "                         'the cited excerpt lines.'),",
    "             'evidence': [{'path': first['path'],",
    "                           'start_line': first['start_line'],",
    "                           'end_line': first['end_line'],",
    "                           'sha256': first['sha256']}],",
    "             'proof': [{'role': 'applicability',",
    "                        'citation_index': 0, 'quote': quote},",
    "                       {'role': 'mechanism',",
    "                        'citation_index': 0, 'quote': quote}],",
    "             'needs': [],",
    "             'finding': None}",
    "else:",
    "    reply = {'status': 'unknown',",
    "             'rationale': ('The bounded source evidence does not '",
    "                         'establish this row.'),",
    "             'evidence': [], 'finding': None,",
    "             'proof': [], 'needs': []}",
    "envelope = {'type': 'result', 'subtype': 'success',",
    "            'is_error': False, 'num_turns': 1,",
    "            'permission_denials': [],",
    "            'result': json.dumps(reply)}",
    "sys.stdout.write(json.dumps(envelope))",
    "",
])

#: ``claude``-shaped fake answering a DB-002 gap, satisfied elsewhere.
FAKE_CLAUDE_DB_GAP_SCRIPT = "\n".join([
    "#!" + sys.executable,
    "import json, os, sys",
    "here = os.path.dirname(os.path.abspath(sys.argv[0]))",
    "if len(sys.argv) > 1 and sys.argv[1] == '--version':",
    "    sys.stdout.write('claude-test 1.0\\n')",
    "    sys.exit(0)",
    "request = json.load(sys.stdin)",
    "item_id = request['policy']['item']['id']",
    "with open(os.path.join(here, 'argv.log'), 'a',",
    "          encoding='utf-8') as handle:",
    "    handle.write(json.dumps({'argv': sys.argv[1:],",
    "                              'item': item_id}) + '\\n')",
    "excerpts = request['excerpts']",
    "if excerpts:",
    "    first = excerpts[0]",
    "    quote = first['text'].splitlines()[0][:512]",
    "    citation = {'path': first['path'],",
    "                'start_line': first['start_line'],",
    "                'end_line': first['end_line'],",
    "                'sha256': first['sha256']}",
    "    if item_id == 'DB-002':",
    "        reply = {'status': 'gap',",
    "                 'rationale': ('DB-002 shows the tests share one '",
    "                               'database without isolation.'),",
    "                 'evidence': [citation],",
    "                 'proof': [{'role': 'applicability',",
    "                            'citation_index': 0, 'quote': quote},",
    "                           {'role': 'violation',",
    "                            'citation_index': 0, 'quote': quote}],",
    "                 'needs': [],",
    "                 'finding': {",
    "                     'summary': ('Tests share one database '",
    "                                 'without isolation.'),",
    "                     'suggested_change': ('Give each test its own '",
    "                                          'isolated database.'),",
    "                     'evidence': [citation]}}",
    "    else:",
    "        reply = {'status': 'satisfied',",
    "                 'rationale': ('Reviewed ' + item_id + ' against '",
    "                             'the cited excerpt lines.'),",
    "                 'evidence': [citation],",
    "                 'proof': [{'role': 'applicability',",
    "                            'citation_index': 0, 'quote': quote},",
    "                           {'role': 'mechanism',",
    "                            'citation_index': 0, 'quote': quote}],",
    "                 'needs': [],",
    "                 'finding': None}",
    "else:",
    "    reply = {'status': 'unknown',",
    "             'rationale': ('The bounded source evidence does not '",
    "                         'establish this row.'),",
    "             'evidence': [], 'finding': None,",
    "             'proof': [], 'needs': []}",
    "envelope = {'type': 'result', 'subtype': 'success',",
    "            'is_error': False, 'num_turns': 1,",
    "            'permission_denials': [],",
    "            'result': json.dumps(reply)}",
    "sys.stdout.write(json.dumps(envelope))",
    "",
])


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    """A per-test ``bin`` directory prepended to ``PATH``."""
    bindir = tmp_path / "fake-bin"
    bindir.mkdir()
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    return bindir


@pytest.fixture
def fake_exec(fake_bin):
    """Write an owned executable ``script`` as ``name``.

    Returns a factory ``(name, script, *, bin_dir=None) -> Path``; the
    executable lands in ``fake_bin`` unless ``bin_dir`` is given.
    """
    def make(name, script: str, *, bin_dir=None) -> Path:
        target = Path(bin_dir) if bin_dir is not None else fake_bin
        return support.write_executable(target / name, script)
    return make


@pytest.fixture
def fake_pytest_project(tmp_path):
    """A tmp pytest project with the given test layout.

    Returns a factory ``(*, tests, git=True, **toml) -> Path`` where
    ``tests`` maps relative paths to file bodies and the remaining
    keywords go to ``support.ptest_toml_text`` (``kind`` defaults to
    ``"pytest"``).  With ``git`` (default) the project is committed
    through ``support.init_git_repo``.
    """
    def make(*, tests: dict, git: bool = True, **toml) -> Path:
        toml.setdefault("kind", "pytest")
        index = 0
        while (tmp_path / f"fake-pytest-{index}").exists():
            index += 1
        root = tmp_path / f"fake-pytest-{index}"
        root.mkdir()
        for relative, body in tests.items():
            support.write_file(root / relative, body)
        support.write_ptest_toml(root, **toml)
        if git:
            support.init_git_repo(root)
        return root
    return make


def _recording_runner(fake_exec, name: str, *, bin_dir=None,
                      record: str | None = None,
                      exit_code: int | None = None) -> Path:
    """An argv/env-recording ``name`` executable via ``fake_exec``."""
    script = recording_script(record=record or f"{name}-record.json")
    if exit_code is not None:
        script += f"raise SystemExit({int(exit_code)})\n"
    return fake_exec(name, script, bin_dir=bin_dir)


@pytest.fixture
def fake_exec_vitest(fake_exec):
    """Argv/env-recording ``vitest``-shaped fake runner."""
    def make(name: str = "vitest", *, bin_dir=None,
             record: str | None = None,
             exit_code: int | None = None) -> Path:
        return _recording_runner(fake_exec, name, bin_dir=bin_dir,
                                 record=record, exit_code=exit_code)
    return make


@pytest.fixture
def fake_exec_go(fake_exec):
    """Argv/env-recording ``go``-shaped fake runner."""
    def make(name: str = "go", *, bin_dir=None,
             record: str | None = None,
             exit_code: int | None = None) -> Path:
        return _recording_runner(fake_exec, name, bin_dir=bin_dir,
                                 record=record, exit_code=exit_code)
    return make


@pytest.fixture
def fake_exec_cargo(fake_exec):
    """Argv/env-recording ``cargo``-shaped fake runner."""
    def make(name: str = "cargo", *, bin_dir=None,
             record: str | None = None,
             exit_code: int | None = None) -> Path:
        return _recording_runner(fake_exec, name, bin_dir=bin_dir,
                                 record=record, exit_code=exit_code)
    return make


@pytest.fixture
def fake_exec_node(fake_exec):
    """``node``-shaped recorder for the vitest execution tests."""
    def make(root, *, name: str = "node") -> Path:
        return fake_exec(name, FAKE_NODE_SCRIPT, bin_dir=root)
    return make


@pytest.fixture
def fake_exec_claude(fake_exec):
    """``claude``-shaped provider fake for review/doctor tests."""
    def make(*, bin_dir=None, db_gap: bool = False) -> Path:
        script = FAKE_CLAUDE_DB_GAP_SCRIPT if db_gap else FAKE_CLAUDE_SCRIPT
        return fake_exec("claude", script, bin_dir=bin_dir)
    return make
