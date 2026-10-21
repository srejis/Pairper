"""Reader clarification preserves exact alias provenance without granting author retries."""
import copy

import pytest
from test_explanation_paper_context import GroundedRunner
from test_explanation_selection import make

from pairper.explanation.catalog import digest
from pairper.explanation.paper_context import PaperContextWorkflow
from pairper.explanation.reader_context import CLARIFICATION, preserved_alias_scope


def setup(tmp_path):
    service, old, prior = make(tmp_path)
    old.run(prior)
    state = old._read(prior)
    target = next(i['target'] for i in state['batches'][0]['candidate']['items'] if i['target']['kind'] == 'term')
    target['aliases'] = ['다층퍼셉트론']
    old._save(state)
    identity = {'translation_session_id': 'ko', 'translation_revision': 1, 'source_identity': {}}
    surface = {'block_id': 'visible', 'field': 'runs', 'run_index': 0,
               'source_unit_ids': ['b/runs'], 'text': '다층퍼셉트론과 다층퍼셉트론'}
    service.translation_surfaces = lambda *args: {**identity, 'surfaces': [copy.deepcopy(surface)]}
    workflow = PaperContextWorkflow(service, GroundedRunner(), directory=old.directory)
    run = workflow.start('p', adopt_from_run_id=prior)['run_id']
    # The synthetic service has no translated packet; bind the same current fixture identity.
    original_packet = workflow._packet

    def packet(*args, **kwargs):
        return {**original_packet(*args, **kwargs), 'translation_identity': identity}

    workflow._packet = packet
    return service, workflow, run, prior, surface


def rejected(workflow, run, code='alias_occurrence_scope'):
    state = workflow._read(run)
    batch = state['batches'][0]
    item = next(i for i in batch['candidate']['items'] if i['target']['kind'] == 'term')
    item['target']['aliases'] = []
    batch.update(status='needs_review', semantic_repairs=4,
                 review={'verdict': 'repair', 'issues': [{'code': code, 'target_key': item['key'], 'detail': 'check'}]})
    batch['review_candidate_sha256'] = digest(batch['candidate'])
    batch['review_context_sha256'] = digest(workflow._packet(workflow.service.bundle, batch['block_ids'],
        target_ids=batch['target_ids'], include_text=batch['include_text']))
    workflow._save(state)
    return copy.deepcopy(batch)


def test_prior_alias_inventory_contains_every_exact_current_occurrence(tmp_path):
    service, workflow, run, prior, surface = setup(tmp_path)
    state = workflow._read(run)
    batch = state['batches'][0]
    packet = workflow._packet(service.bundle, batch['block_ids'])
    scope = preserved_alias_scope(workflow, state, batch, packet)
    proof = scope[0]['complete_occurrences']
    assert proof['occurrence_count'] == 2
    assert len(proof['surfaces'][0]['positions']) == 2
    assert proof['surfaces'][0]['text'] == surface['text']
    assert scope[0]['semantic_equivalence_requires_current_independent_review'] is True
    surface['text'] = '다른 용어'
    assert preserved_alias_scope(workflow, state, batch, packet) == []
    # A new process cannot accept a changed prior reviewed baseline.
    del workflow._reader_alias_baselines
    changed = workflow._read(prior)
    changed['error'] = 'changed'
    workflow._save(changed)
    with pytest.raises(ValueError, match='baseline changed'):
        preserved_alias_scope(workflow, state, batch, packet)


def test_reassessment_restores_only_aliases_and_requires_new_science_once(tmp_path):
    _, workflow, run, _, _ = setup(tmp_path)
    before = rejected(workflow, run)
    calls = len(workflow.runner.calls)
    workflow.reassess_reader_context(run, 0)
    batch = workflow._read(run)['batches'][0]
    assert batch['semantic_repairs'] == 4 and batch['review'] is None
    assert batch['manifest'] == before['manifest']
    assert [i['explanation'] for i in batch['candidate']['items']] == [i['explanation'] for i in before['candidate']['items']]
    assert len(workflow.runner.calls) == calls
    assert batch['evidence'][-1]['previous_review'] == before['review']
    assert batch['evidence'][-1]['repair_allowance_granted'] == 0
    with pytest.raises(ValueError, match='cannot be repeated'):
        workflow.reassess_reader_context(run, 0)
    workflow.step(run)
    request = workflow.runner.calls[-1]
    assert request['mode'] == 'content_review'
    assert request['reader_context']['clarification'] == CLARIFICATION
    assert request['reader_context']['preserved_alias_scope'][0]['complete_occurrences']['occurrence_count'] == 2


@pytest.mark.parametrize('damage', ['factual_error', 'candidate', 'context', 'missing_alias'])
def test_reassessment_cannot_waive_real_rejections_or_stale_bindings(tmp_path, damage):
    _, workflow, run, _, surface = setup(tmp_path)
    rejected(workflow, run, 'wrong_formula' if damage == 'factual_error' else 'alias_occurrence_scope')
    state = workflow._read(run)
    if damage in ('candidate', 'context'):
        state['batches'][0]['review_' + damage + '_sha256'] = 'stale'
        workflow._save(state)
    elif damage == 'missing_alias':
        surface['text'] = '다른 용어'
    original = workflow._path(run).read_bytes()
    with pytest.raises(ValueError):
        workflow.reassess_reader_context(run, 0)
    assert workflow._path(run).read_bytes() == original
