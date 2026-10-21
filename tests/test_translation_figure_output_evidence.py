"""Stored annotations and actually emitted reader labels are different evidence."""
import copy

import pytest
from test_translation_figure_anchors import answer, figure_claim, use_original_figure
from test_translation_workflow import review, submit
from test_translation_workflow import setup_workflow as setup_workflow


@pytest.mark.parametrize('changed', [False, True])
def test_evidence_uses_actual_reader_projection_without_approving_candidate(setup_workflow, changed):
    workflow, service = setup_workflow(full=True, images=True)
    use_original_figure(service, isolated=True)
    run = workflow.start(service.bundle['paper_id'], 'actual-projection-' + str(changed))
    claim = figure_claim(workflow, run['run_id'])
    candidate = answer(claim, isolated=True)
    if not changed:
        candidate['figures'][0]['labels'][0]['target'] = 'LM head'
    before = copy.deepcopy(candidate)
    evidence = workflow._figure_output_evidence(service.bundle, claim['request']['task'], candidate)
    assert evidence['projection_available'] and evidence['original_raster_unchanged']
    assert evidence['original_crop_sha256'] == claim['request']['task']['figure']['crop_sha256']
    assert evidence['emitted_translation_labels'] == (
        [{'source': 'LM head', 'target': '언어 모델 헤드'}] if changed else [])
    assert evidence['translation_approval_granted'] is False and candidate == before
    with workflow.connect() as con:
        assert con.execute('SELECT candidate FROM tasks WHERE id=?', (claim['task_id'],)).fetchone()[0] is None


def test_review_source_scan_gets_actual_candidate_separate_from_trigger_response(setup_workflow):
    workflow, service = setup_workflow(full=True, images=True)
    use_original_figure(service, isolated=True)
    run = workflow.start(service.bundle['paper_id'], 'review-candidate-source-evidence')
    claim = figure_claim(workflow, run['run_id'])
    candidate = answer(claim, isolated=True)
    submit(workflow, claim, candidate)
    for _ in range(6):
        claim = workflow.claim(run['run_id'])
        if claim['request']['kind'] == 'figure':
            break
        submit(workflow, claim, review(claim))
    assert claim['phase'] == 'review' and claim['request']['kind'] == 'figure'
    trigger = review(claim)
    trigger['source_concerns'] = ['Inspect the supplied original image annotation.']
    captured = []

    class Scanner:
        def scan(self, bundle, task, observations, images):
            captured.append(copy.deepcopy(task))
            return []

    workflow.source_repairer = Scanner()
    assert workflow._inspect_source_response(run['run_id'], claim, trigger) is None
    packet = captured[0]
    assert packet['candidate_to_inspect'] == trigger
    assert packet['current_translation_candidate'] == candidate
    assert packet['figure_output_evidence']['emitted_translation_labels'] == [
        {'source': 'LM head', 'target': '언어 모델 헤드'}]
    assert packet['figure_output_evidence']['translation_approval_granted'] is False
    with pytest.raises(ValueError):
        workflow.commit(run['run_id'])


def test_invalid_candidate_has_no_safe_output_claim(setup_workflow):
    workflow, service = setup_workflow(full=True, images=True)
    use_original_figure(service, isolated=True)
    run = workflow.start(service.bundle['paper_id'], 'invalid-projection-evidence')
    claim = figure_claim(workflow, run['run_id'])
    candidate = answer(claim, isolated=True)
    candidate['issues'] = [{'message': 'Unresolved annotation'}]
    evidence = workflow._figure_output_evidence(service.bundle, claim['request']['task'], candidate)
    assert evidence['projection_available'] is False
    assert evidence['translation_approval_granted'] is False
