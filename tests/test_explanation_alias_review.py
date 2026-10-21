"""Alias corrections are exact translated-literal proposals, never automatic casefold anchors."""
import copy

import pytest
from test_explanation_selection import Runner, make

from pairper.explanation.alias_review import anchor
from pairper.explanation.catalog import digest

IDENTITY = {'translation_session_id': 'ko', 'translation_revision': 1, 'source_identity': {}}


class AliasRunner(Runner):
    def __init__(self, bad_field=None):
        super().__init__(('pass', 'pass'))
        self.bad_field = bad_field

    def run(self, request, schema, images, directory):
        result = super().run(request, schema, images, directory)
        findings = [f for f in request.get('external_content_findings', []) if f['field'] == 'target.aliases']
        if findings:
            previous = {i['target']['id']: i for i in request['candidate']['items']}
            for item in result['items']:
                old = previous[item['target']['id']]
                item['explanation'] = copy.deepcopy(old['explanation'])
                finding = next(f for f in findings if f['target_id'] == item['target']['id'])
                item['target']['aliases'] = old['target']['aliases'] + [finding['alias_anchor']['literal']]
                if self.bad_field == 'description':
                    item['explanation']['description'][0]['text'] += ' altered'
                elif self.bad_field == 'concept_key':
                    item['target']['concept_key'] = 'another'
                elif self.bad_field == 'extra_alias':
                    item['target']['aliases'].append('다른 말')
        return result


def setup(tmp_path, bad_field=None):
    service, workflow, run = make(tmp_path, AliasRunner(bad_field))
    surface = {'block_id': 'visible', 'field': 'runs', 'run_index': 0,
               'source_unit_ids': ['b/runs'], 'text': '다층퍼셉트론 신경망'}
    identity = copy.deepcopy(IDENTITY)
    service.translation_surfaces = lambda *args: {**identity, 'surfaces': [copy.deepcopy(surface)]}
    workflow.run(run)
    b = workflow._read(run)['batches'][0]
    tid = next(i['target']['id'] for i in b['candidate']['items'] if i['target']['kind'] == 'term')
    return service, workflow, run, b, tid, surface, identity


def record(workflow, run, batch, tid, identity):
    return workflow.record_alias_finding(run, tid, literal='다층퍼셉트론',
        expected_translation_identity=identity, reason='현재 번역의 동일 신경망 명칭이 기존 aliases에 없다.',
        expected_candidate_sha256=digest(batch['candidate']))


def test_published_history_is_preserved_and_alias_only_repair_has_full_science_and_kind(tmp_path):
    service, workflow, run, before, tid, _, identity = setup(tmp_path)
    workflow.commit(run)
    saved_before = copy.deepcopy(service.saved)
    record(workflow, run, before, tid, identity)
    assert service.saved == saved_before and service.published
    reopened = workflow._read(run)['batches'][0]
    proof = reopened['external_content_findings'][0]['alias_anchor']
    assert proof['literal'] == '다층퍼셉트론' and proof['occurrence_count'] == 1
    assert proof['translation_identity'] == identity
    calls = len(workflow.runner.calls)
    assert workflow.run(run)['status'] == 'ready'
    assert [r['mode'] for r in workflow.runner.calls[calls:]] == ['content_repair', 'content_review', 'kind_review']
    after = workflow._read(run)['batches'][0]
    expected = copy.deepcopy(before['candidate'])
    next(i for i in expected['items'] if i['target']['id'] == tid)['target']['aliases'] = ['다층퍼셉트론']
    assert after['candidate'] == expected
    assert after['external_content_findings'][0]['used'] == 1
    assert after['external_content_findings'][0]['status'] == 'resolved'
    workflow.commit(run)
    assert service.saved['workflow_evidence']['batches'][0]['evidence'][-1]['external_content_findings'][0]['alias_anchor'] == proof


@pytest.mark.parametrize('bad_field', ['description', 'concept_key', 'extra_alias'])
def test_alias_only_repair_cannot_change_other_content_or_concepts(tmp_path, bad_field):
    _, workflow, run, before, tid, _, identity = setup(tmp_path, bad_field)
    record(workflow, run, before, tid, identity)
    assert workflow.run(run)['status'] == 'needs_review'
    assert workflow._read(run)['batches'][0]['candidate'] == before['candidate']


def test_alias_requires_unique_exact_current_source_provenance_and_revision(tmp_path):
    service, workflow, run, before, tid, surface, identity = setup(tmp_path)
    target = next(i['target'] for i in before['candidate']['items'] if i['target']['id'] == tid)
    for text, units in [('다층퍼셉트론 다층퍼셉트론', ['b/runs']), ('다층퍼셉트론', ['another/runs']),
                        ('MLP', ['b/runs'])]:
        surface.update(text=text, source_unit_ids=units)
        with pytest.raises(ValueError, match='exactly once'):
            anchor(service, service.bundle, target, '다층퍼셉트론', identity)
    surface.update(text='다층퍼셉트론', source_unit_ids=['b/runs'])
    with pytest.raises(ValueError, match='different current Korean'):
        anchor(service, service.bundle, target, '다층퍼셉트론', {**identity, 'translation_revision': 99})
    assert workflow._read(run)['batches'][0].get('external_content_findings') is None


def test_same_revision_target_finding_cannot_grant_another_allowance(tmp_path):
    _, workflow, run, before, tid, _, identity = setup(tmp_path)
    record(workflow, run, before, tid, identity)
    with pytest.raises(ValueError, match='same external finding'):
        record(workflow, run, before, tid, identity)


def test_literal_context_change_during_repair_blocks_publication(tmp_path):
    _, workflow, run, before, tid, surface, identity = setup(tmp_path)
    record(workflow, run, before, tid, identity)
    surface['text'] += ' changed'
    assert workflow.run(run)['status'] == 'needs_review'
    assert 'provenance changed' in workflow._read(run)['error']
