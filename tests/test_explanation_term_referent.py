"""A mistaken object referent uses ordinary budget and a reviewed semantic-key amendment."""
import copy

import pytest
from test_explanation_selection import Runner, make, term

from pairper.explanation.catalog import digest


class ReferentRunner(Runner):
    tamper = None

    def run(self, request, schema, images, directory):
        result = super().run(request, schema, images, directory)
        if request['mode'] == 'selection':
            extra = term('activation', 4)
            extra['concept_key'] = 'mlp'  # Same old sense; the unaffected entry must stay exact.
            result['selected_terms'].append({'key': 'activation', 'target': extra, 'reason': 'technical term'})
        if request['mode'] == 'content_repair':
            item = result['items'][0]
            item['target']['concept_key'] = 'neural_model'
            item['explanation']['category'] = '모델'
            item['explanation']['description'] = [{'kind': 'text', 'text': '입력을 처리하는 모델이다.'}]
            if self.tamper == 'alias':
                item['target']['aliases'] = ['invented']
            if self.tamper == 'other_field':
                item['explanation']['meaning'] = [{'kind': 'text', 'text': 'invented'}]
            if self.tamper == 'same_key':
                item['target']['concept_key'] = 'mlp'
        return result


def setup(tmp_path, runner=None):
    service, workflow, run = make(tmp_path, runner or ReferentRunner(('pass', 'pass')))
    service.bundle['text_units'][0]['text'] = 'MLP activation'
    workflow.run(run)
    return service, workflow, run


def register(workflow, run, **changes):
    batch = workflow._read(run)['batches'][0]
    item = next(i for i in batch['candidate']['items'] if i['target']['source_text'] == 'MLP')
    args = dict(field='term_referent', observed_value=workflow._term_referent(item),
                reason='Selected headword denotes a model, not an ability.',
                criterion='Source-specific referent and paper-local semantic key must agree.',
                expected_candidate_sha256=digest(batch['candidate']))
    args.update(changes)
    return workflow.record_content_finding(run, item['target']['id'], **args)


def test_referent_bundle_amends_manifest_preserves_unaffected_concept_then_full_reviews(tmp_path):
    service, workflow, run = setup(tmp_path)
    state = workflow._read(run)
    state['batches'][0]['semantic_repairs'] = 1
    workflow._save(state)
    before = copy.deepcopy(workflow._read(run)['batches'][0])
    register(workflow, run)
    assert workflow.run(run)['status'] == 'ready'
    after = workflow._read(run)['batches'][0]
    assert after['semantic_repairs'] == before['semantic_repairs'] + 1
    assert after['manifest_amendments'][0]['original_manifest'] == before['manifest']
    assert after['manifest']['fingerprint'] != before['manifest']['fingerprint']
    old = {i['key']: i for i in before['candidate']['items']}
    for item in after['candidate']['items']:
        previous = copy.deepcopy(old[item['key']])
        if item['target']['source_text'] == 'MLP':
            previous['target']['concept_key'] = item['target']['concept_key']
            for field in ('category', 'description'):
                previous['explanation'][field] = item['explanation'][field]
        assert previous == item
    finding = after['external_content_findings'][0]
    assert finding['used'] == 1 and finding['status'] == 'resolved'
    assert finding['budget_policy'] == 'ordinary_semantic_limit_only'
    assert [r['mode'] for r in workflow.runner.calls][-3:] == ['content_repair', 'content_review', 'kind_review']
    review = workflow.runner.calls[-2]
    assert review['manifest']['fingerprint'] == after['review_manifest_sha256']
    assert review['term_referent_manifest_amendments']
    workflow.commit(run)
    assert service.saved['workflow_evidence']['batches'][0]['evidence'][-1]['manifest_amendments']


@pytest.mark.parametrize('tamper', ['alias', 'other_field', 'same_key'])
def test_referent_bundle_rejects_changes_outside_exact_scope(tmp_path, tamper):
    runner = ReferentRunner(('pass',))
    _, workflow, run = setup(tmp_path, runner)
    original = copy.deepcopy(workflow._read(run)['batches'][0]['candidate'])
    register(workflow, run)
    runner.tamper = tamper
    assert workflow.run(run)['status'] == 'needs_review'
    batch = workflow._read(run)['batches'][0]
    assert batch['candidate'] == original
    assert batch['semantic_repairs'] == 1
    assert batch['external_content_findings'][0]['used'] == 1


def test_referent_finding_requires_exact_observed_pass_and_remaining_budget(tmp_path):
    _, workflow, run = setup(tmp_path)
    with pytest.raises(ValueError, match='observed'):
        register(workflow, run, observed_value={'category': 'wrong'})
    with pytest.raises(ValueError, match='exact originally'):
        register(workflow, run, expected_candidate_sha256='stale')
    state = workflow._read(run)
    state['batches'][0]['semantic_repairs'] = state['max_repairs']
    workflow._save(state)
    with pytest.raises(ValueError, match='remaining ordinary'):
        register(workflow, run)


def test_referent_finding_does_not_grant_budget_or_allow_duplicate_registration(tmp_path):
    _, workflow, run = setup(tmp_path)
    register(workflow, run)
    with pytest.raises(ValueError, match='same external finding'):
        register(workflow, run)
    state = workflow._read(run)
    state['batches'][0]['semantic_repairs'] = state['max_repairs']
    workflow._save(state)
    assert workflow.run(run)['status'] == 'needs_review'
    assert workflow._read(run)['batches'][0]['external_content_findings'][0]['used'] == 0


def test_rejected_referent_preserves_first_rejection_and_uses_only_remaining_general_budget(tmp_path):
    class RejectOnce(ReferentRunner):
        reviews = 0

        def run(self, request, schema, images, directory):
            result = super().run(request, schema, images, directory)
            if request['mode'] == 'content_review':
                self.reviews += 1
                if self.reviews == 2:
                    tid = next(i['key'] for i in request['candidate']['items'] if i['target']['source_text'] == 'MLP')
                    result['verdict'] = 'repair'
                    result['issues'] = [{'target_key': tid, 'code': 'incorrect_term_referent', 'detail': 'Definition remains imprecise.'}]
            if request['mode'] == 'content_repair' and self.reviews >= 2:
                result['items'][0]['explanation']['description'][0]['text'] = '입력 정보를 처리하는 신경망 모델이다.'
            return result

    _, workflow, run = setup(tmp_path, RejectOnce(('pass', 'pass', 'pass')))
    state = workflow._read(run)
    state['max_repairs'] = 4
    state['batches'][0]['semantic_repairs'] = 1
    workflow._save(state)
    register(workflow, run)
    assert workflow.run(run)['status'] == 'ready'
    batch = workflow._read(run)['batches'][0]
    finding = batch['external_content_findings'][0]
    assert batch['semantic_repairs'] == 3
    assert finding['used'] == finding['allowance'] == 1
    assert [r['verdict'] for r in finding['review_history']] == ['repair', 'pass']
    assert len(batch['manifest_amendments']) == 2


def test_two_observed_referents_share_one_ordinary_repair_and_keep_third_concept_exact(tmp_path):
    class TwoReferents(ReferentRunner):
        def run(self, request, schema, images, directory):
            result = super().run(request, schema, images, directory)
            if request['mode'] == 'selection':
                extra = term('dropout', 15)
                extra['concept_key'] = 'mlp'
                result['selected_terms'].append({'key': 'dropout', 'target': extra, 'reason': 'technical term'})
            if request['mode'] == 'content_repair':
                for item in result['items']:
                    item['target']['concept_key'] = item['target']['source_text'].lower() + '_model'
                    item['explanation']['category'] = '모델'
                    item['explanation']['description'] = [{'kind': 'text', 'text': '입력을 처리하는 모델이다.'}]
            return result

    service, workflow, run = make(tmp_path, TwoReferents(('pass', 'pass')))
    service.bundle['text_units'][0]['text'] = 'MLP activation dropout'
    workflow.run(run)
    before = copy.deepcopy(workflow._read(run)['batches'][0])
    for item in before['candidate']['items']:
        if item['target']['source_text'] in ('MLP', 'activation'):
            workflow.record_content_finding(run, item['key'], field='term_referent',
                observed_value=workflow._term_referent(item), reason='Wrong selected object referent.',
                criterion='Source referent and semantic key must agree.',
                expected_candidate_sha256=digest(before['candidate']))
    assert workflow.run(run)['status'] == 'ready'
    after = workflow._read(run)['batches'][0]
    assert after['semantic_repairs'] == 1
    assert len(after['manifest_amendments']) == 1
    for source in ('dropout', 'x+y'):
        old = [i for i in before['candidate']['items'] if i['target']['source_text'] == source]
        new = [i for i in after['candidate']['items'] if i['target']['source_text'] == source]
        assert old == new
    assert all(f['status'] == 'resolved' and f['used'] == 1 for f in after['external_content_findings'])
