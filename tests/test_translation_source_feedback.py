"""Durable no-correction PDF observations survive rejection, not fresh review.

Source-derived fixtures and deterministic scanner/translator replies exercise
orchestration only. No actual AI, library source repair, or semantic approval.
"""
import copy
import json

import pytest

from test_translation_workflow import setup_workflow, draft, review, submit, engine, sha
from pairper.translation.prompts import project_request


@pytest.mark.parametrize('phase', ['generate', 'repair'])
def test_no_correction_scan_feedback_survives_rejected_response_and_restart_but_not_review(setup_workflow, monkeypatch, phase):
    workflow, service = setup_workflow(max_failures=3)
    source_before = copy.deepcopy(service.bundle)
    run = workflow.start(service.bundle['paper_id'], 'no-correction-feedback-'+phase)
    if phase == 'repair':
        claim = workflow.claim(run['run_id']); submit(workflow,claim,draft(claim))
        claim = workflow.claim(run['run_id']); submit(workflow,claim,review(claim,'repair'))
    scanned, rejected_responses = [], []
    marker = 'SCANNER_ORIGINAL_SOURCE_EVIDENCE_NOT_A_TRANSLATION_REVIEW'
    feedback = {}
    class NoCorrectionScanner:
        def scan(self, bundle, task, observations, images):
            scanned.append((bundle['fingerprint'],task['task_id'],copy.deepcopy(observations)))
            source = task['units'][0]['source_text']
            notes = [marker + ': Original source exactly reads '+source+'. No transcription correction is supported.']
            feedback.update(bundle_fingerprint=bundle['fingerprint'],task_id=task['task_id'],
                            notes=notes,findings=[],artifact_dir='test-only-scan-artifacts',
                            scan_response_sha256=sha({'findings':[],'notes':notes}))
            return []
        def get_scan_feedback(self, bundle, task_id=None):
            assert bundle['fingerprint'] == feedback['bundle_fingerprint']
            assert task_id == feedback['task_id']
            return copy.deepcopy(feedback)
        def repair(self, *_): raise AssertionError('No-correction scan must not alter source')
    class IncorrectSourceIssueRunner:
        def run(self, request, *_):
            response = draft({'request':request})
            unit = request['task']['units'][0]
            response['source_concerns'] = ['Suspected source transcription mismatch; compare original.']
            response['issues'] = [{'item_id':unit['id'],'category':'source_transcription',
                'source_quote':unit['source_text'],'target_quote':response['items'][0]['target'],
                'detail':'Test translator alleges a source defect that the independent source scan disproves.'}]
            rejected_responses.append(copy.deepcopy(response))
            return response
    workflow.source_repairer = NoCorrectionScanner()
    workflow.runner = IncorrectSourceIssueRunner()
    rejected = workflow.step(run['run_id'])
    assert rejected['counts']['blocked'] == 0 and len(scanned) == 1
    assert service.bundle == source_before
    with workflow.connect() as con:
        row = con.execute('SELECT * FROM tasks').fetchone()
        event = json.loads(con.execute("SELECT payload FROM events WHERE event='source_checked_no_correction'").fetchone()[0])
    assert json.loads(row['source_feedback']) == feedback
    assert json.loads(row['last_response']) == rejected_responses[0]
    assert event['feedback'] == feedback
    assert event['response_sha256'] == sha(rejected_responses[0])
    assert event['translation_quality_approved_by_this_scan'] is False
    # The scanner's in-memory cache is gone: only durable per-task evidence can
    # make the next generation/repair input explain why source was preserved.
    restarted = engine.Workflow(service)
    monkeypatch.setattr(restarted,'_with_images',workflow._with_images)
    next_claim = restarted.claim(run['run_id'])
    assert next_claim['phase'] == phase
    assert next_claim['request']['source_verification_feedback'] == feedback
    assert project_request(next_claim['request'])['source_verification_feedback'] == feedback
    assert next_claim['request']['validation_feedback']['rejected_candidate'] == rejected_responses[0]
    submit(restarted,next_claim,draft(next_claim))
    fresh = restarted.claim(run['run_id'])
    assert fresh['phase'] == 'review'
    assert 'source_verification_feedback' not in fresh['request']
    assert 'source_verification_feedback' not in project_request(fresh['request'])
    assert marker not in json.dumps(fresh['request'],ensure_ascii=False)
    assert fresh['request']['issues'] == [] and fresh['request']['structural_error'] == ''
    # The original observations remain auditable without being fed to reviewer.
    with restarted.connect() as con:
        assert json.loads(con.execute('SELECT source_feedback FROM tasks').fetchone()[0]) == feedback
    submit(restarted,fresh,review(fresh))
    assert restarted.claim(run['run_id']) is None
    assert restarted.commit(run['run_id'])['status'] == 'ready'
    assert service.bundle == source_before


def test_additive_source_feedback_migration_retains_candidates_and_event_history(setup_workflow, monkeypatch):
    workflow, service = setup_workflow()
    run = workflow.start(service.bundle['paper_id'],'previous-schema-source-feedback')
    claim = workflow.claim(run['run_id']); response = draft(claim); submit(workflow,claim,response)
    with workflow.connect() as con:
        before = dict(con.execute('SELECT * FROM tasks').fetchone())
        old_events = [dict(row) for row in con.execute('SELECT * FROM events ORDER BY seq')]
        con.execute('ALTER TABLE tasks DROP COLUMN source_feedback')
    restarted = engine.Workflow(service)
    monkeypatch.setattr(restarted,'_with_images',workflow._with_images)
    with restarted.connect() as con:
        after = dict(con.execute('SELECT * FROM tasks').fetchone())
        new_events = [dict(row) for row in con.execute('SELECT * FROM events ORDER BY seq')]
    assert after.pop('source_feedback') is None
    before.pop('source_feedback')
    assert after == before and new_events == old_events
    assert restarted.start(service.bundle['paper_id'],'previous-schema-source-feedback')['run_id'] == run['run_id']
    fresh = restarted.claim(run['run_id'])
    assert fresh['phase'] == 'review' and fresh['request']['candidate'] == response
    assert 'source_verification_feedback' not in fresh['request']
