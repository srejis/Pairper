"""Parallel view publication recovery, with real private source/store databases.

No provider calls or prior corpus translations; deterministic replies test state
transitions only. Source identities are checked by the production current_source.
"""
import copy
import hashlib
import json
import threading
import time

import pytest

from pairper.document.models import Document
from pairper.storage.library import Library
from pairper.translation import cli, workflow as engine
from pairper.translation.service import TranslationService


@pytest.fixture
def private_source(tmp_path, monkeypatch):
    library = Library(tmp_path / 'source')
    pdf = library.root / 'originals/p.pdf'
    pdf.write_bytes(b'Private source identity fixture; no image parsing needed.')
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    source = {'sha256': digest, 'extractor_version': 7, 'pages': [
        {'page': 1, 'width': 100, 'height': 100, 'images': [], 'drawings': [], 'spans': [
            {'id': 'r1', 'text': 'The rate must not exceed two.', 'bbox': [1, 1, 90, 10], 'order': 0},
            {'id': 'r2', 'text': 'The second value is conditional.', 'bbox': [1, 20, 90, 30], 'order': 1}]}]}
    (library.root / 'sources/p.json').write_text(json.dumps(source), encoding='utf8')
    document = Document.model_validate({'paper_id': 'p', 'source_sha256': digest,
        'visual_review_pages': [1], 'blocks': [
            {'id': f'b{i}', 'type': 'paragraph', 'order': i, 'source_refs': [ref],
             'content': {'runs': [{'kind': 'text', 'refs': [ref]}]}}
            for i, ref in enumerate(('r1', 'r2'))]}).model_dump(mode='json')
    (library.root / 'views/old.json').write_text(json.dumps(document), encoding='utf8')
    with library.connect() as con:
        con.execute('INSERT INTO papers VALUES (?,?,?,?,?,?,?,?,?,?)',
                    ('p', digest, 'originals/p.pdf', 'p.pdf', 'Fresh', '{}', 1, 'now', 'complete', 'old'))
        con.execute('INSERT INTO views VALUES (?,?,?,?,?,?,?,?)',
                    ('old', 'p', digest, '1.1', 'views/old.json', '{"valid":true}', 'now', 'now'))
    service = TranslationService(library, tmp_path / 'translation')
    monkeypatch.setattr(engine.Workflow, '_with_images', lambda *args: [])
    def publish(name='new'):
        updated = copy.deepcopy(document)
        updated['blocks'][0]['content']['runs'][0]['source_verified'] = True
        (library.root / f'views/{name}.json').write_text(json.dumps(updated), encoding='utf8')
        with library.connect() as con:
            con.execute('INSERT INTO views VALUES (?,?,?,?,?,?,?,?)',
                        (name, 'p', digest, '1.1', f'views/{name}.json', '{"valid":true}', 'now', 'now'))
            con.execute('UPDATE papers SET current_view_id=? WHERE id=?', (name, 'p'))
    return library, service, publish


def draft(request):
    return {'items': [{'unit_id': unit['id'], 'action': 'translate', 'target': '새 결정적 시험 문장',
                       'reason': 'Fresh deterministic fixture, not semantic approval.'}
                      for unit in request['task']['units']], 'issues': [], 'notes': [], 'source_concerns': []}


def test_parallel_lease_change_rejects_old_reply_preserves_payload_and_restarts_empty(private_source):
    _, service, publish = private_source
    workflow = engine.Workflow(service)
    run = workflow.start('p', 'fresh', max_units=1)
    first = workflow.claim(run['run_id'])
    second = workflow.claim(run['run_id'])
    workflow.submit(run['run_id'], first['task_id'], first['lease_id'], draft(first['request']))
    with workflow.connect() as con:
        before = [dict(row) for row in workflow._rows(con, run['run_id'])]
    publish()
    with pytest.raises(ValueError, match='Source reconstruction changed'):
        workflow.submit(run['run_id'], second['task_id'], second['lease_id'], draft(second['request']))
    observed = workflow.observe_source_change(run['run_id'])
    assert observed['status'] == 'source_changed' and observed['leased'] == 0
    with workflow.connect() as con:
        after = [dict(row) for row in workflow._rows(con, run['run_id'])]
        events = [json.loads(row['payload']) for row in con.execute(
            "SELECT payload FROM events WHERE event='external_source_change_observed'")]
    for old, new in zip(before, after):
        assert {k: v for k, v in old.items() if k not in ('lease', 'lease_until')} == {
            k: v for k, v in new.items() if k not in ('lease', 'lease_until')}
    assert events[0]['old_identity']['view_id'] == 'old'
    assert events[0]['new_identity']['view_id'] == 'new'
    assert events[0]['revoked_task_ids'] == [second['task_id']]
    assert workflow.observe_source_change(run['run_id'])['status'] == 'source_changed'
    with workflow.connect() as con:
        assert con.execute("SELECT count(*) FROM events WHERE event='external_source_change_observed'").fetchone()[0] == 1
    restarted = workflow.start('p', 'fresh', max_units=1)
    assert restarted['run_id'] != run['run_id']
    assert restarted['session_id'] != run['session_id']
    bundle = service.current_bundle(restarted['session_id'])
    assert service.store.read(restarted['session_id'], bundle['fingerprint'])['result']['entries'] == []
    with workflow.connect() as con:
        assert all(row['candidate'] is None and row['review'] is None for row in workflow._rows(con, restarted['run_id']))


@pytest.mark.parametrize('state', ['published', 'ready', 'cancelled'])
def test_terminal_runs_are_not_reclassified_by_observed_source(private_source, state):
    _, service, publish = private_source
    workflow = engine.Workflow(service)
    run = workflow.start('p', 'terminal', max_units=1)
    lease = workflow.claim(run['run_id'])
    with workflow.connect() as con:
        con.execute('UPDATE runs SET state=? WHERE id=?', (state, run['run_id']))
    publish()
    assert workflow.observe_source_change(run['run_id']) is None
    with workflow.connect() as con:
        assert workflow._run(con, run['run_id'])['state'] == state
        assert workflow._rows(con, run['run_id'])[0]['lease'] == lease['lease_id']


def test_same_source_and_invalid_source_do_not_become_recovery(private_source):
    library, service, _ = private_source
    workflow = engine.Workflow(service)
    run = workflow.start('p', 'same')
    workflow.claim(run['run_id'])
    assert workflow.observe_source_change(run['run_id']) is None
    pdf = library.root / 'originals/p.pdf'
    pdf.write_bytes(b'Unapproved original file change')
    with pytest.raises(ValueError, match='Original PDF changed'):
        workflow.observe_source_change(run['run_id'])
    with workflow.connect() as con:
        assert workflow._run(con, run['run_id'])['state'] == 'generating'
        assert workflow._rows(con, run['run_id'])[0]['lease']


def test_observation_preserves_existing_independent_review_and_revokes_all_other_leases(private_source):
    _, service, publish = private_source
    workflow = engine.Workflow(service)
    run = workflow.start('p', 'review-preservation', max_units=1)
    for _ in range(2):
        claimed = workflow.claim(run['run_id'])
        workflow.submit(run['run_id'], claimed['task_id'], claimed['lease_id'], draft(claimed['request']))
    first, second = workflow.claim(run['run_id']), workflow.claim(run['run_id'])
    response = {'items': [{'item_id': item['item_id'], 'verdict': 'pass', 'source_compared': True,
        'source_quote': item['source'], 'target_quote': item['target'],
        'checks': ['Whole original pair compared in deterministic orchestration fixture.'], 'issues': []}
        for item in first['request']['review_items']], 'issues': [], 'notes': [], 'source_concerns': []}
    workflow.submit(run['run_id'], first['task_id'], first['lease_id'], response)
    with workflow.connect() as con:
        before = [dict(row) for row in workflow._rows(con, run['run_id'])]
    assert any(row['review'] for row in before)
    publish()
    assert workflow.observe_source_change(run['run_id'])['status'] == 'source_changed'
    with workflow.connect() as con:
        after = [dict(row) for row in workflow._rows(con, run['run_id'])]
    for old, new in zip(before, after):
        for key in ('candidate', 'review', 'review_input_hash', 'last_response', 'status', 'repair_count'):
            assert new[key] == old[key]
        assert new['lease'] is None and new['lease_until'] is None
    assert second['lease_id']


def test_cli_recovers_source_change_using_existing_empty_restart_path(private_source, monkeypatch, capsys):
    library, service, publish = private_source
    class Runner:
        called = 0
        def run(self, request, schema, images, directory):
            self.called += 1
            if self.called == 1:
                publish()
            return draft(request)
    monkeypatch.setattr(cli, 'Library', lambda *_: library)
    monkeypatch.setattr(cli, 'TranslationService', lambda *_: service)
    monkeypatch.setattr(cli, 'CodexRunner', lambda **_: Runner())
    assert cli.main(['p', '--experiment', 'cli-fresh', '--max-units', '1', '--max-steps', '1']) == 2
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(x.get('event') == 'fresh_restart_after_source_repair' for x in lines)
    assert not any(x.get('event') == 'paper_failed' for x in lines)
    with engine.Workflow(service).connect() as con:
        runs = list(con.execute('SELECT * FROM runs'))
        assert sorted(row['state'] for row in runs) == ['generating', 'source_changed']
        new = next(row for row in runs if row['state'] == 'generating')
        assert all(row['candidate'] is None for row in engine.Workflow._rows(con, new['id']))


def test_cli_does_not_hide_unrelated_value_error(private_source, monkeypatch, capsys):
    library, service, _ = private_source
    monkeypatch.setattr(cli, 'Library', lambda *_: library)
    monkeypatch.setattr(cli, 'TranslationService', lambda *_: service)
    monkeypatch.setattr(cli, 'CodexRunner', lambda **_: object())
    monkeypatch.setattr(engine.Workflow, 'step', lambda *_: (_ for _ in ()).throw(ValueError('Unrelated invalid schema')))
    assert cli.main(['p', '--experiment', 'bad', '--max-steps', '1']) == 2
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    failure = next(x for x in lines if x.get('event') == 'paper_failed')
    assert failure['error'] == 'Unrelated invalid schema'
    assert not any(x.get('event') == 'fresh_restart_after_source_repair' for x in lines)


def mcp_tools(service, monkeypatch, runner):
    from pairper.translation import agent_runner, service as service_module, workflow_mcp
    class Registry:
        def __init__(self):
            self.tools = {}
        def tool(self):
            def register(fn):
                self.tools[fn.__name__] = fn
                return fn
            return register
        def prompt(self):
            return lambda fn: fn
    registry = Registry()
    monkeypatch.setattr(service_module, 'TranslationService', lambda *_: service)
    monkeypatch.setattr(agent_runner, 'CodexRunner', lambda: runner)
    workflow_mcp.register_workflow_tools(registry, service.library, registry.tool, service.directory)
    return registry.tools


def stopped_report(tools, run_id):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        report = tools['get_translation_workflow_report'](run_id)
        if not report['background_running']:
            return tools['get_translation_workflow_report'](run_id)
        time.sleep(0.01)
    raise AssertionError('Private background worker did not stop')


def test_mcp_background_observes_valid_new_source_without_error_or_automatic_restart(private_source, monkeypatch):
    _, service, publish = private_source
    changed, release = threading.Event(), threading.Event()
    class Runner:
        def run(self, request, schema, images, directory):
            publish()
            changed.set()
            assert release.wait(3), 'Private source race was not released'
            return draft(request)
    tools = mcp_tools(service, monkeypatch, Runner())
    start = tools['start_translation_workflow']('p', 'mcp-current', max_units=1)
    tools['run_translation_workflow'](start['run_id'])
    workflow = engine.Workflow(service)
    assert changed.wait(3)
    try:
        # Deliberately query while the old provider call is still in flight:
        # the getter, not a completed worker, must observe the new source.
        during = tools['get_translation_workflow_report'](start['run_id'])
        assert during['status'] == 'source_changed' and during['background_running']
        assert during['execution_error'] is None and during['leased'] == 0
    finally:
        release.set()
    report = stopped_report(tools, start['run_id'])
    assert report['status'] == 'source_changed'
    assert report['execution_error'] is None and report['leased'] == 0
    with workflow.connect() as con:
        assert con.execute('SELECT count(*) FROM runs').fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM events WHERE event='external_source_change_observed'").fetchone()[0] == 1
        assert all(row['candidate'] is None for row in workflow._rows(con, start['run_id']))


def test_mcp_background_retains_unrelated_value_error_without_source_reclassification(private_source, monkeypatch):
    _, service, _ = private_source
    tools = mcp_tools(service, monkeypatch, object())
    monkeypatch.setattr(engine.Workflow, 'step', lambda *_: (_ for _ in ()).throw(ValueError('Unrelated protocol fault')))
    start = tools['start_translation_workflow']('p', 'mcp-unrelated')
    tools['run_translation_workflow'](start['run_id'])
    report = stopped_report(tools, start['run_id'])
    assert report['status'] == 'generating'
    assert report['execution_error'] == 'Unrelated protocol fault'
    workflow = engine.Workflow(service)
    with workflow.connect() as con:
        assert con.execute("SELECT count(*) FROM events WHERE event='external_source_change_observed'").fetchone()[0] == 0


@pytest.mark.parametrize('invalid_original', [False, True])
def test_mcp_report_preserves_unrelated_and_invalid_source_errors(private_source, monkeypatch, invalid_original):
    library, service, _ = private_source
    tools = mcp_tools(service, monkeypatch, object())
    start = tools['start_translation_workflow']('p', 'mcp-report-error')
    if invalid_original:
        (library.root/'originals/p.pdf').write_bytes(b'Unapproved changed original')
        message = 'Original PDF changed'
    else:
        message = 'Unrelated report validation error'
        monkeypatch.setattr(engine.Workflow, 'report', lambda *_: (_ for _ in ()).throw(ValueError(message)))
    with pytest.raises(ValueError, match=message):
        tools['get_translation_workflow_report'](start['run_id'])
    workflow = engine.Workflow(service)
    with workflow.connect() as con:
        assert workflow._run(con, start['run_id'])['state'] == 'generating'
        assert con.execute("SELECT count(*) FROM events WHERE event='external_source_change_observed'").fetchone()[0] == 0
