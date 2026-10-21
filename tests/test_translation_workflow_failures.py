"""Independent regressions for recoverable infrastructure and cancellation failures."""
import json
from types import SimpleNamespace

import pytest

from test_translation_workflow import setup_workflow, draft, review, submit, sha
from test_translation_workflow_interfaces import interfaces, ProtocolRunner
from pairper.translation import cli
from pairper.translation.agent_runner import RunnerCancelled


def test_source_image_preparation_failure_is_bounded_and_recorded(setup_workflow, monkeypatch):
    workflow, service = setup_workflow(max_failures=2)
    workflow.runner = ProtocolRunner()
    run = workflow.start(service.bundle['paper_id'], 'source-image-failure')
    def unavailable(*_):
        raise OSError('temporary original-page render output unavailable')
    monkeypatch.setattr(workflow, '_with_images', unavailable)
    for _ in range(3):
        workflow.step(run['run_id'])
    report = workflow.report(run['run_id'])
    assert report['status'] == 'needs_review'
    assert any('unavailable' in error['error'] for error in report['errors'])
    assert service.store.read(run['session_id'],service.bundle['fingerprint'])['revision'] == 0


def test_cli_same_experiment_can_resume_after_explicit_cancellation(interfaces, monkeypatch, capsys):
    _, service, _ = interfaces
    class CancelledRunner:
        def run(self, *_):
            raise RunnerCancelled('User cancelled actual invocation', kind='cancelled')
        def cancel(self): pass
    monkeypatch.setattr(cli, 'CodexRunner', lambda **_: CancelledRunner())
    args = [service.bundle['paper_id'],'--experiment','cli-cancel-resume','--publish']
    assert cli.main(args) == 2
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    first = rows[-1]['papers'][0]
    assert first['status'] == 'cancelled'
    monkeypatch.setattr(cli, 'CodexRunner', lambda **_: ProtocolRunner())
    assert cli.main(args) == 0
    resumed = [json.loads(line) for line in capsys.readouterr().out.splitlines()][-1]['papers'][0]
    assert resumed['run_id'] == first['run_id'] and resumed['status'] == 'published'


def test_repeated_user_cancellation_does_not_exhaust_provider_failure_budget(setup_workflow):
    workflow, service = setup_workflow(max_failures=1)
    class CancelledRunner:
        def run(self, *_): raise RunnerCancelled('explicit cancellation', kind='cancelled')
    workflow.runner = CancelledRunner()
    run = workflow.start(service.bundle['paper_id'], 'repeat-cancel')
    for _ in range(4):
        assert workflow.step(run['run_id'])['status'] == 'cancelled'
    assert workflow.report(run['run_id'])['counts']['blocked'] == 0
    with workflow.connect() as con:
        assert con.execute('SELECT sum(failures) FROM tasks').fetchone()[0] == 0
    workflow.runner = ProtocolRunner()
    for _ in range(4):
        if workflow.step(run['run_id'])['status'] == 'ready': break
    assert workflow.commit(run['run_id'])['status'] == 'ready'


@pytest.mark.parametrize('phase', ['generate', 'review'])
def test_source_repair_restarts_empty_without_accepting_triggering_response(setup_workflow, phase):
    workflow, service = setup_workflow(full=True, images=True)
    scans, repairs = [], []
    class Repairer:
        def scan(self, bundle, task, notes, images):
            scans.append((bundle['fingerprint'],task,notes,list(images)))
            return [SimpleNamespace(kind='math_latex', model_dump=lambda:{'source_id':'source-only-test-finding'})]
        def repair(self, bundle, findings):
            repairs.append(bundle['fingerprint'])
            service.bundle['view_id'] = 'new-test-view'
            service.bundle['fingerprint'] = sha({k:v for k,v in service.bundle.items() if k != 'fingerprint'})
            return {'report':{'valid':True,'view_id':'new-test-view'},
                    'requires_new_empty_translation_experiment':True}
    workflow.source_repairer = Repairer()
    class SourceObservationRunner:
        def run(self, request, schema, images, directory):
            claim = {'request':request}
            response = review(claim) if request['phase'] == 'review' else draft(claim)
            response['source_concerns'] = ['Original PDF may differ; independently inspect source.']
            return response
    run = workflow.start(service.bundle['paper_id'], 'source-restart-'+phase)
    if phase == 'review':
        for _ in range(3):
            claim = workflow.claim(run['run_id']); submit(workflow, claim, draft(claim))
    workflow.runner = SourceObservationRunner()
    changed = workflow.step(run['run_id'])
    assert changed['status'] == 'source_changed'
    assert len(scans) == len(repairs) == 1
    assert scans[0][2] == ['Original PDF may differ; independently inspect source.']
    with workflow.connect() as con:
        assert con.execute("SELECT count(*) FROM tasks WHERE status='accepted'").fetchone()[0] == 0
        assert con.execute('SELECT count(*) FROM tasks WHERE lease IS NOT NULL').fetchone()[0] == 0
    old = service.store.read(run['session_id'], repairs[0])
    assert old['revision'] == 0 and old['result']['entries'] == []
    restarted = workflow.start(service.bundle['paper_id'], 'source-restart-'+phase)
    assert restarted['run_id'] != run['run_id'] and restarted['session_id'] != run['session_id']
    assert service.store.read(restarted['session_id'],service.bundle['fingerprint'])['result']['entries'] == []


def test_source_scan_failure_is_bounded_and_does_not_accept_translation(setup_workflow):
    workflow, service = setup_workflow(max_failures=2)
    class ScanUnavailable:
        def scan(self, *_): raise OSError('Original PDF source scan unavailable')
    workflow.source_repairer = ScanUnavailable()
    class ObservationRunner:
        def run(self, request, *_):
            response = draft({'request':request})
            response['source_concerns'] = ['Inspect source transcription.']
            return response
    workflow.runner = ObservationRunner()
    run = workflow.start(service.bundle['paper_id'], 'failed-source-scan')
    for _ in range(3): workflow.step(run['run_id'])
    assert workflow.report(run['run_id'])['status'] == 'needs_review'
    assert service.store.read(run['session_id'],service.bundle['fingerprint'])['revision'] == 0


def test_transient_source_image_failure_recovers_without_losing_task(setup_workflow, monkeypatch):
    workflow, service = setup_workflow(max_failures=2)
    workflow.runner = ProtocolRunner()
    run = workflow.start(service.bundle['paper_id'], 'recover-image')
    actual = workflow._with_images
    def unavailable(*_): raise OSError('transient original image preparation failure')
    monkeypatch.setattr(workflow, '_with_images', unavailable)
    first = workflow.step(run['run_id'])
    assert first['counts']['blocked'] == 0 and first['leased'] == 0
    monkeypatch.setattr(workflow, '_with_images', actual)
    for _ in range(4):
        if workflow.step(run['run_id'])['status'] == 'ready': break
    assert workflow.commit(run['run_id'])['status'] == 'ready'


def test_general_notes_do_not_trigger_extra_source_inference(setup_workflow):
    workflow, service = setup_workflow()
    class NoSourceConcern:
        def scan(self, *_):
            raise AssertionError('General notes are not source findings')
    class NotesRunner:
        def run(self, request, *_):
            response = draft({'request': request})
            response['notes'] = ['A faithful authorial inconsistency was preserved.']
            response['source_concerns'] = []
            return response
    workflow.source_repairer = NoSourceConcern()
    workflow.runner = NotesRunner()
    run = workflow.start(service.bundle['paper_id'], 'notes-without-source-loss')
    report = workflow.step(run['run_id'])
    assert report['counts']['drafted'] == 1 and not report['errors']


def test_cli_source_restart_limit_stops_without_accepting_any_old_candidates(interfaces, monkeypatch, capsys):
    _, service, _ = interfaces
    repairs = []
    class RepeatedSourceRepairer:
        def scan(self, *_):
            return [SimpleNamespace(kind='math_latex', model_dump=lambda:{'source_id':'source-only-fake-finding'})]
        def repair(self, bundle, findings):
            repairs.append(bundle['fingerprint'])
            service.bundle['view_id'] = 'source-test-view-'+str(len(repairs))
            service.bundle['fingerprint'] = sha({k:v for k,v in service.bundle.items() if k != 'fingerprint'})
            return {'report':{'valid':True,'view_id':service.bundle['view_id']}}
    class ObservationRunner:
        def run(self, request, *_):
            response = draft({'request':request})
            response['source_concerns'] = ['Inspect possible source transcription discrepancy.']
            return response
        def cancel(self): pass
    monkeypatch.setattr(cli, 'SourceRepairer', lambda *_: RepeatedSourceRepairer())
    monkeypatch.setattr(cli, 'CodexRunner', lambda **_: ObservationRunner())
    assert cli.main([service.bundle['paper_id'],'--experiment','source-restart-limit','--max-steps','10']) == 2
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[-1]['papers'][0]['status'] == 'needs_review'
    # Initial run plus three permitted restarts: the fourth changed source is
    # reported, but a fifth translation run is never started in this invocation.
    assert len(repairs) == 4
    with service.store.connect() as con:
        sessions = con.execute('SELECT revision,result FROM sessions').fetchall()
    assert len(sessions) == 4
    assert all(row['revision'] == 0 and json.loads(row['result'])['entries'] == [] for row in sessions)


def test_confirmed_unresolved_source_stops_once_without_accepting_candidate(setup_workflow):
    workflow, service = setup_workflow(max_failures=3)
    calls = {'model':0, 'scan':0, 'repair':0}
    finding = {'kind':'unresolved', 'source_id':'original-source-ambiguity',
               'reason':'The supplied PDF does not permit a faithful transcription.'}
    class UnresolvedRepairer:
        def scan(self, *_):
            calls['scan'] += 1
            return [SimpleNamespace(kind='unresolved', model_dump=lambda:dict(finding))]
        def repair(self, *_):
            calls['repair'] += 1
            raise AssertionError('Confirmed unresolved source must not be applied')
    class ObservationRunner:
        def run(self, request, *_):
            calls['model'] += 1
            response = draft({'request':request})
            response['source_concerns'] = ['Inspect an unresolved original-source ambiguity.']
            return response
    workflow.source_repairer = UnresolvedRepairer()
    workflow.runner = ObservationRunner()
    run = workflow.start(service.bundle['paper_id'], 'confirmed-source-unresolved')
    first = workflow.step(run['run_id'])
    assert first['status'] == 'needs_review' and first['counts']['blocked'] == 1
    assert first['leased'] == 0
    assert workflow.step(run['run_id'])['status'] == 'needs_review'
    assert calls == {'model':1, 'scan':1, 'repair':0}
    with workflow.connect() as con:
        row = con.execute('SELECT candidate,failures FROM tasks').fetchone()
        assert row['candidate'] is None and row['failures'] == 0
        event = con.execute("SELECT payload FROM events WHERE event='source_repair_blocked'").fetchone()
    assert json.loads(event['payload']) == {'findings':[finding], 'translation_response_accepted':False}
    assert service.store.read(run['session_id'],service.bundle['fingerprint'])['revision'] == 0
    with pytest.raises(ValueError): workflow.commit(run['run_id'])
