"""New symbol omissions after KIND use only normal remaining repair budget."""
import copy

import pytest
from test_explanation_selection import Runner, make

from pairper.explanation.catalog import digest


class SymbolRunner(Runner):
    tamper = False

    def run(self, request, schema, images, directory):
        result = super().run(request, schema, images, directory)
        if request['mode'] in ('content_generate', 'content_repair'):
            for item in result['items']:
                if item['target']['kind'] == 'math' and not request.get('scoped_symbol_repair'):
                    item['explanation']['symbols'] = item['explanation']['symbols'][:1]
                if request.get('scoped_symbol_repair') and self.tamper:
                    item['explanation']['meaning'] = [{'kind': 'text', 'text': 'unrelated rewrite'}]
        return result


def latent_kind_failure(tmp_path, runner=None):
    _, workflow, run = make(tmp_path, runner or SymbolRunner(('pass', 'pass')))
    assert workflow.run(run)['status'] == 'ready'
    state = workflow._read(run)
    batch = state['batches'][0]
    math = next(i for i in batch['candidate']['items'] if i['target']['kind'] == 'math')
    # Model the retained actual state after a category correction's full review found y missing.
    batch['kind_certificate']['status'] = 'repair'
    batch['review'].update(verdict='repair', issues=[{'target_key': math['key'],
        'code': 'missing_symbol_referents', 'detail': '수식에 쓰인 y의 뜻을 symbols에 추가한다.'}])
    batch['status'], state['status'] = 'needs_review', 'pending'
    batch['semantic_repairs'], state['max_repairs'] = 1, 4
    workflow._save(state)
    return workflow, run, copy.deepcopy(batch)


def test_symbol_retry_appends_only_missing_rows_preserves_categories_and_uses_general_budget(tmp_path):
    workflow, run, before = latent_kind_failure(tmp_path)
    assert workflow.run(run)['status'] == 'ready'
    after = workflow._read(run)['batches'][0]
    assert after['semantic_repairs'] == 2 and not after.get('external_content_findings')
    old = {i['key']: i for i in before['candidate']['items']}
    for item in after['candidate']['items']:
        previous = old[item['key']]
        assert item['target'] == previous['target']
        if item['target']['kind'] == 'math':
            assert item['explanation']['symbols'][:len(previous['explanation']['symbols'])] == previous['explanation']['symbols']
            assert {k:v for k,v in item['explanation'].items() if k!='symbols'} == {
                k:v for k,v in previous['explanation'].items() if k!='symbols'}
        else:
            assert item == previous
    assert after['scoped_symbol_repair_history'][0]['review_sha256'] == digest(before['review'])
    assert after['kind_certificate']['status'] == 'pass' and after['retired_kind_certificates']
    assert [c['mode'] for c in workflow.runner.calls][-3:] == ['content_repair', 'content_review', 'kind_review']


@pytest.mark.parametrize('change', ['exhausted', 'stale', 'other_code', 'external_failure'])
def test_symbol_retry_cannot_revive_other_terminal_or_stale_failures(tmp_path, change):
    workflow, run, _ = latent_kind_failure(tmp_path)
    state = workflow._read(run)
    batch = state['batches'][0]
    if change == 'exhausted':
        batch['semantic_repairs'] = state['max_repairs']
    if change == 'stale':
        batch['review_candidate_sha256'] = 'changed'
    if change == 'other_code':
        batch['review']['issues'][0]['code'] = 'other_content'
    if change == 'external_failure':
        batch['external_content_findings'] = [{'status': 'rejected'}]
    assert not workflow._kind_symbol_retry(state,batch,workflow._fresh(state))


def test_symbol_repair_cannot_change_other_fields_and_failed_review_still_blocks(tmp_path):
    runner=SymbolRunner(('pass','pass'))
    workflow,run,before=latent_kind_failure(tmp_path,runner)
    runner.tamper=True
    workflow.step(run)
    batch=workflow._read(run)['batches'][0]
    assert batch['candidate']==before['candidate'] and batch['semantic_repairs']==2
    assert batch['structural_repairs']==1


def test_later_new_non_symbol_content_issue_remains_needs_review_without_another_repair(tmp_path):
    workflow,run,_=latent_kind_failure(tmp_path,SymbolRunner(('pass','repair')))
    assert workflow.run(run)['status']=='needs_review'
    batch=workflow._read(run)['batches'][0]
    assert batch['semantic_repairs']==2 and batch['review']['issues'][0]['code']=='abstract'
