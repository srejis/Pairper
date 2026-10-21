"""Complete occurrence opt-in cannot silently pick a convenient subset of one literal."""
import copy

import pytest
from test_explanation_alias_review import IDENTITY, AliasRunner
from test_explanation_selection import make

from pairper.explanation.alias_review import anchor, complete_occurrence_proposal
from pairper.explanation.catalog import digest
from pairper.explanation.selection_prompts import build_prompt


def proposal_setup(tmp_path):
    service, workflow, run = make(tmp_path, AliasRunner())
    identity = copy.deepcopy(IDENTITY)
    surface = {'block_id': 'visible', 'field': 'runs', 'run_index': 0,
               'source_unit_ids': ['b/runs'], 'text': '다층퍼셉트론 그리고 다층퍼셉트론'}
    extra = {**surface, 'run_index': 2, 'text': '다층퍼셉트론'}
    service.translation_surfaces = lambda *args: {**identity, 'surfaces': [surface, extra]}
    workflow.run(run)
    before = workflow._read(run)['batches'][0]
    tid = next(i['target']['id'] for i in before['candidate']['items'] if i['target']['kind'] == 'term')
    target = next(i['target'] for i in before['candidate']['items'] if i['target']['id'] == tid)
    proof = complete_occurrence_proposal(service, service.bundle, target, '다층퍼셉트론', identity)
    return service, workflow, run, before, tid, surface, identity, target, proof


def record(workflow, run, before, tid, identity, proof):
    return workflow.record_alias_finding(run, tid, literal='다층퍼셉트론',
        expected_translation_identity=identity, reason='모든 명칭 발생이 같은 신경망을 가리키는지 검토한다.',
        expected_candidate_sha256=digest(before['candidate']), complete_occurrences=proof)


def test_complete_multi_field_inventory_repairs_alias_only_and_requires_fresh_science_kind(tmp_path):
    service, workflow, run, before, tid, _, identity, target, proof = proposal_setup(tmp_path)
    assert proof['occurrence_count'] == 3 and len(proof['surfaces']) == 2
    with pytest.raises(ValueError, match='exactly once'):
        anchor(service, service.bundle, target, '다층퍼셉트론', identity)
    record(workflow, run, before, tid, identity, proof)
    calls = len(workflow.runner.calls)
    assert workflow.run(run)['status'] == 'ready'
    recent = workflow.runner.calls[calls:]
    assert [r['mode'] for r in recent] == ['content_repair', 'content_review', 'kind_review']
    review = recent[1]
    assert review['complete_alias_findings'][0]['alias_anchor'] == proof
    assert proof['surfaces'][0]['text'] in build_prompt(review)
    expected = copy.deepcopy(before['candidate'])
    next(i for i in expected['items'] if i['target']['id'] == tid)['target']['aliases'] = ['다층퍼셉트론']
    assert workflow._read(run)['batches'][0]['candidate'] == expected
    workflow.commit(run)
    with pytest.raises(ValueError, match='same external finding'):
        record(workflow, run, before, tid, identity, proof)


@pytest.mark.parametrize('change', ['missing', 'extra', 'duplicate', 'hash', 'source', 'revision', 'count', 'text'])
def test_incomplete_excess_stale_or_forged_complete_proof_never_registers(tmp_path, change):
    _, workflow, run, before, tid, _, identity, _, proof = proposal_setup(tmp_path)
    if change == 'missing':
        proof['surfaces'][0]['positions'].pop()
    elif change == 'extra':
        proof['surfaces'][0]['positions'].append({'start': 1, 'end': 7})
    elif change == 'duplicate':
        proof['surfaces'][0]['positions'].append(proof['surfaces'][0]['positions'][0])
    elif change == 'hash':
        proof['surfaces'][0]['surface_sha256'] = 'forged'
    elif change == 'source':
        proof['source_fingerprint'] = 'another'
    elif change == 'revision':
        proof['translation_identity']['translation_revision'] = 99
    elif change == 'count':
        proof['occurrence_count'] = 1
    else:
        proof['surfaces'][0]['text'] += ' changed'
    with pytest.raises(ValueError, match='exactly match'):
        record(workflow, run, before, tid, identity, proof)
    assert workflow._read(run)['batches'][0].get('external_content_findings') is None


def test_mixed_meaning_science_rejection_cannot_publish_or_pick_subset(tmp_path):
    _, workflow, run, before, tid, _, identity, _, proof = proposal_setup(tmp_path)
    record(workflow, run, before, tid, identity, proof)
    workflow.runner.verdicts = iter(('repair',))
    assert workflow.run(run)['status'] == 'needs_review'
    batch = workflow._read(run)['batches'][0]
    assert batch['external_content_findings'][0]['status'] == 'rejected'
    assert batch['external_content_findings'][0]['used'] == 1
    assert batch['external_content_findings'][0]['alias_anchor'] == proof
    with pytest.raises(ValueError):
        workflow.commit(run)


def test_complete_proof_rechecked_after_review_before_publication(tmp_path):
    _, workflow, run, before, tid, surface, identity, _, proof = proposal_setup(tmp_path)
    record(workflow, run, before, tid, identity, proof)
    assert workflow.run(run)['status'] == 'ready'
    surface['text'] += ' 다층퍼셉트론'
    with pytest.raises(ValueError, match='exactly match'):
        workflow.commit(run)
