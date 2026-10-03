"""Literal native Vitest full-command proofs through the actual controller."""
from __future__ import annotations

import json

import pytest

from support import init_git_repo, git_commit_all
from test_operations import _vitest_project, _run_data


def _project(case, domain, fake_exec_node, *, args=()):
    root = _vitest_project(case, domain, fake_exec_node,
                           args=('--maxWorkers=100%', '--fileParallelism', *args))
    package = root / 'node_modules' / 'vitest' / 'package.json'
    package.write_text(json.dumps({'name': 'vitest', 'version': '3.2.6'}))
    (root / 'package.json').write_text(json.dumps({'devDependencies': {'vitest': '3.2.6'}}))
    (root / 'package-lock.json').write_text(json.dumps({
        'name': 'fixture', 'lockfileVersion': 3,
        'packages': {'': {'devDependencies': {'vitest': '3.2.6'}},
                     'node_modules/vitest': {'version': '3.2.6'}}}))
    (root / 'sample.test.ts').write_text('test("one", () => {})\n')
    (root / '.gitignore').write_text('node_modules/\norder.log\nnode-record.json\nnode-exit\nptest-result-*\nresults/\n')
    with (root / '.ptest.toml').open('a') as stream:
        stream.write('[selection]\nnon_input_outputs = ["node_modules", "order.log", "node-record.json", "node-exit", "results"]\n')
    init_git_repo(root)
    return root


def _invoke(case, domain, root, *args, **kwargs):
    import secrets
    output = root / 'results'
    output.mkdir(exist_ok=True)
    if (root / 'web' / '.ptest.toml').exists():
        (root / 'web' / 'results').mkdir(exist_ok=True)
    return case.invoke(domain, root, '--result-json',
                       'results/' + secrets.token_hex(8) + '.json', *args, **kwargs)


def test_first_native_full_produces_truthful_evidence_and_second_does_not_launch(case, fake_exec_node):
    domain = case.domain()
    root = _project(case, domain, fake_exec_node)
    first = _invoke(case, domain, root, '--full', timeout=20)
    assert first.code == 0, first.stderr.decode()
    data = _run_data(first)
    assert data['counts'] is None
    assert data['baseline_published'] is False
    second = _invoke(case, domain, root, '--full', timeout=20)
    assert second.code == 0, second.stderr.decode()
    assert (root / 'order.log').read_text() == 'node\n'
    assert _run_data(second)['attempts'] == []


@pytest.mark.parametrize('args', [('--passWithNoTests',), ('--pass-with-no-tests=true',),
                                    ('--help',), ('--testNamePattern', 'one'),
                                    ('--project=unit',), ('--shard=1/2',)])
def test_zero_exit_narrowed_or_no_test_command_never_earns_reuse(case, fake_exec_node, args):
    domain = case.domain()
    root = _project(case, domain, fake_exec_node, args=args)
    for _ in range(2):
        result = _invoke(case, domain, root, '--full', timeout=20)
        assert result.code == 0, result.stderr.decode()
    assert (root / 'order.log').read_text() == 'node\nnode\n'


def test_changed_installed_bytes_force_native_execution(case, fake_exec_node):
    domain = case.domain()
    root = _project(case, domain, fake_exec_node)
    first = _invoke(case, domain, root, '--full', timeout=20)
    assert first.code == 0, first.stderr.decode()
    entry = root / 'node_modules/vitest/vitest.mjs'
    entry.write_text('// installed runtime changed\n')
    second = _invoke(case, domain, root, '--full', timeout=20)
    assert second.code == 0, second.stderr.decode()
    assert (root / 'order.log').read_text() == 'node\nnode\n'


def test_failed_rerun_withdraws_prior_green_before_runtime_is_restored(case, fake_exec_node):
    domain = case.domain()
    root = _project(case, domain, fake_exec_node)
    first = _invoke(case, domain, root, '--full', timeout=20)
    assert first.code == 0, first.stderr.decode()
    entry = root / 'node_modules/vitest/vitest.mjs'
    original = entry.read_bytes()
    entry.write_text('// changed installed runtime\n')
    (root / 'node-exit').write_text('23')
    failed = _invoke(case, domain, root, '--full', timeout=20)
    assert failed.code == 23, failed.stderr.decode()
    entry.write_bytes(original)
    (root / 'node-exit').unlink()
    recovered = _invoke(case, domain, root, '--full', timeout=20)
    assert recovered.code == 0, recovered.stderr.decode()
    assert (root / 'order.log').read_text() == 'node\nnode\nnode\n'
    unchanged = _invoke(case, domain, root, '--full', timeout=20)
    assert unchanged.code == 0, unchanged.stderr.decode()
    assert (root / 'order.log').read_text() == 'node\nnode\nnode\n'


def test_scoped_green_does_not_skip_later_full(case, fake_exec_node):
    domain = case.domain()
    root = _project(case, domain, fake_exec_node)
    scoped = _invoke(case, domain, root, 'sample.test.ts', timeout=20)
    assert scoped.code == 0, scoped.stderr.decode()
    full = _invoke(case, domain, root, '--full', timeout=20)
    assert full.code == 0, full.stderr.decode()
    assert (root / 'order.log').read_text() == 'node\nnode\n'


def test_committed_source_change_runs_again_and_literal_worker_argv_is_preserved(case, fake_exec_node):
    domain = case.domain()
    root = _project(case, domain, fake_exec_node)
    first = _invoke(case, domain, root, '--full', timeout=20)
    assert first.code == 0, first.stderr.decode()
    (root / 'sample.test.ts').write_text('test("two", () => {})\n')
    git_commit_all(root)
    second = _invoke(case, domain, root, '--full', timeout=20)
    assert second.code == 0, second.stderr.decode()
    assert (root / 'order.log').read_text() == 'node\nnode\n'
    assert json.loads((root / 'node-record.json').read_text())['argv'][1:] == [
        'node_modules/vitest/vitest.mjs', 'run', '--maxWorkers=100%', '--fileParallelism']


def test_durable_delayed_green_cannot_resurrect_after_other_checkout_failure(case, monkeypatch, tmp_path):
    """Pause A after its SQLite commit; B withdraws before A feeds the ledger."""
    from dataclasses import replace
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from ptest import contracts as C, history, verified

    domain = case.domain()
    checkout = case.checkout(domain)
    other_root = domain.root / 'other-checkout'
    other_root.mkdir()
    import hashlib, os
    other = replace(checkout, checkout_id=hashlib.sha256(os.fsencode(str(other_root))).hexdigest()[:32], root=other_root)
    before = case.snapshot(digest='11' * 32, compatibility='22' * 32)
    result = replace(case.result(sequence=1, project_id=checkout.project_id,
        checkout_id=checkout.checkout_id, input_before=before, input_after=before,
        policy_digest='33' * 32,
        plan=C.Plan(mode=C.Mode.FULL, execution='full', input_digest=before.digest, compatibility=before.compatibility),
        command=C.summarize_command(C.RunnerKind.VITEST, C.Mode.FULL, ['node', 'run'], workers=1),
        attempts=(C.AttemptResult(attempt_id='a001', phase='execution',
            status=C.Status.PASSED, raw_exit_code=0, final_exit_code=0, source_valid=True),)),
        runtime_identity='44' * 32)
    token = verified.begin_full(domain, checkout.project_id, before.digest)
    reached, resume = Event(), Event()
    feed = history._feed_verified_ledger

    def paused_feed(d, c, r, published, verification_token=None):
        if r.run_id == result.run_id:
            assert published.committed
            reached.set()
            assert resume.wait(10)
        feed(d, c, r, published, verification_token)

    monkeypatch.setattr(history, '_feed_verified_ledger', paused_feed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(history.publish_outcome, domain, checkout, result,
                              None, verification_token=token)
        assert reached.wait(10)
        assert history.read_history_summaries(domain, checkout, 5)[0]['run_id'] == result.run_id
        failed = replace(result, run_id='ff' * 16, checkout_id=other.checkout_id,
                         status=C.Status.FAILED, runner_exit_code=23, exit_code=23,
                         full_gate_eligible=False)
        assert history.publish_outcome(domain, other, failed, None).committed
        resume.set()
        assert pending.result(timeout=10).committed
    assert verified.find(domain, checkout.project_id) == ()
    # A genuine later passing execution can restore the same input.
    fresh = verified.begin_full(domain, checkout.project_id, before.digest)
    restored = replace(result, run_id='ee' * 16, sequence=2)
    assert history.publish_outcome(domain, checkout, restored, None,
                                   verification_token=fresh).committed
    assert [r.run_id for r in verified.find(domain, checkout.project_id)] == ['ee' * 16]


@pytest.mark.parametrize('target,content', [
    ('package-lock.json', '{"lockfileVersion":3,"packages":{}}'),
    ('package.json', '{"devDependencies":{"vitest":"3.2.6"},"changed":true}'),
    ('vitest.config.ts', 'export default { passWithNoTests: true };\n'),
    ('.ptest.toml', '# changed full config\n'),
])
def test_committed_full_input_changes_cannot_reuse_native_green(case, fake_exec_node, target, content):
    domain = case.domain()
    root = _project(case, domain, fake_exec_node)
    first = _invoke(case, domain, root, '--full', timeout=20)
    assert first.code == 0, first.stderr.decode()
    path = root / target
    if target == '.ptest.toml':
        content = path.read_text() + content
    path.write_text(content)
    git_commit_all(root)
    second = _invoke(case, domain, root, '--full', timeout=20)
    assert second.code == 0, second.stderr.decode()
    assert (root / 'order.log').read_text() == 'node\nnode\n'


def test_inherited_node_runtime_flags_force_execution(case, fake_exec_node):
    domain = case.domain()
    root = _project(case, domain, fake_exec_node)
    first = _invoke(case, domain, root, '--full', timeout=20)
    assert first.code == 0, first.stderr.decode()
    second = _invoke(case, domain, root, '--full', env={'NODE_OPTIONS': '--conditions=custom'}, timeout=20)
    assert second.code == 0, second.stderr.decode()
    assert (root / 'order.log').read_text() == 'node\nnode\n'


def test_monorepo_sibling_commit_skips_native_child_but_shared_dependency_runs_it(case, fake_exec_node):
    domain = case.domain()
    root = _project(case, domain, fake_exec_node)
    web = root / 'web'
    web.mkdir()
    for path in tuple(root.iterdir()):
        if path.name not in ('.git', 'web'):
            path.rename(web / path.name)
    config = web / '.ptest.toml'
    config.write_text(config.read_text().replace(str(root / 'node'), str(web / 'node')))
    (root / '.ptest.toml').write_text('version = 2\n[monorepo]\nchildren = ["web"]\n')
    (root / '.gitignore').write_text('ptest-result-*\nresults/\n')
    (root / 'sibling.txt').write_text('unrelated\n')
    (root / 'package.json').write_text('{"name":"root"}\n')
    git_commit_all(root)
    first = _invoke(case, domain, root, '--full', timeout=20)
    assert first.code == 0, first.stderr.decode()
    (root / 'sibling.txt').write_text('changed sibling\n')
    git_commit_all(root)
    second = _invoke(case, domain, root, '--full', timeout=20)
    assert second.code == 0, second.stderr.decode()
    assert (web / 'order.log').read_text() == 'node\n'
    assert b'skipped' in second.stderr
    (root / 'package.json').write_text('{"name":"root","changed":true}\n')
    git_commit_all(root)
    third = _invoke(case, domain, root, '--full', timeout=20)
    assert third.code == 0, third.stderr.decode()
    assert (web / 'order.log').read_text() == 'node\nnode\n'


@pytest.mark.parametrize('boundary', ['guard-launch', 'quiescence'])
def test_incomplete_controller_boundaries_withdraw_green(case, fake_exec_node, monkeypatch, boundary):
    from ptest import contracts as C, config as config_api, operations, scheduler, verified
    domain = case.domain()
    root = _project(case, domain, fake_exec_node)
    first = _invoke(case, domain, root, '--full', timeout=20)
    assert first.code == 0, first.stderr.decode()
    project_id = _run_data(first)['project_id']
    assert len(verified.find(domain, project_id)) == 1
    entry = root / 'node_modules/vitest/vitest.mjs'
    original = entry.read_bytes()
    entry.write_text('// runtime changed to force a genuine rerun\n')
    config = config_api.resolve_config(root).config

    def refuse(*_args, **_kwargs):
        raise C.Problem(code='ownership-uncertain', message='fixture proof unavailable', phase='guard')

    with monkeypatch.context() as patch:
        if boundary == 'guard-launch':
            patch.setattr(operations, '_run_guard', refuse)
        else:
            patch.setattr(scheduler, 'begin_finalization', refuse)
        result = operations.execute(domain, config, C.RunRequest(mode=C.Mode.FULL))
    assert result.status is C.Status.INCOMPLETE
    entry.write_bytes(original)
    assert verified.find(domain, project_id) == ()
    assert operations._full_verified_elsewhere(domain, config, operations._checkout(config),
                                               C.RunRequest(mode=C.Mode.FULL)) is None
