"""A passed formula process correction consumes the ordinary budget, never a new grant."""
import copy

import pytest
from test_explanation_selection import Runner, make

from pairper.explanation.catalog import digest


class ProcessRunner(Runner):
    tamper = False

    def run(self, request, schema, images, directory):
        result = super().run(request, schema, images, directory)
        if request['mode'] in ('content_generate', 'content_repair') and request.get('external_content_findings'):
            item = result['items'][0]
            item['explanation']['process'].append({'kind': 'text', 'text': '앞 단계에서 얻은 두 값을 더한다.'})
            if self.tamper:
                item['explanation']['meaning'] = [{'kind': 'text', 'text': 'Changed unrelated meaning.'}]
        return result


def register(workflow, run):
    batch = workflow._read(run)['batches'][0]
    item = next(i for i in batch['candidate']['items'] if i['target']['kind'] == 'math')
    return workflow.record_content_finding(run, item['target']['id'], field='process',
        observed_value=item['explanation']['process'], reason='기본 계산 과정 한 절이 빠졌다.',
        criterion='Original evaluation-process requirement', expected_candidate_sha256=digest(batch['candidate']))


def test_passed_process_finding_changes_one_segment_then_full_science_and_kind(tmp_path):
    runner = ProcessRunner(('pass', 'pass'))
    _, workflow, run = make(tmp_path, runner)
    assert workflow.run(run)['status'] == 'ready'
    before = copy.deepcopy(workflow._read(run)['batches'][0])
    register(workflow, run)
    assert workflow.run(run)['status'] == 'ready'
    after = workflow._read(run)['batches'][0]
    assert after['semantic_repairs'] == before['semantic_repairs'] + 1
    finding = after['external_content_findings'][0]
    assert finding['budget_policy'] == 'ordinary_semantic_limit_only'
    assert finding['used'] == 1 and finding['status'] == 'resolved'
    old = {i['target']['id']: i for i in before['candidate']['items']}
    for item in after['candidate']['items']:
        previous = old[item['target']['id']]
        assert item['target'] == previous['target']
        if item['target']['kind'] == 'math':
            assert item['explanation']['process'][:-1] == previous['explanation']['process']
        else:
            assert item == previous
    assert [c['mode'] for c in runner.calls][-3:] == ['content_repair', 'content_review', 'kind_review']


def test_process_finding_cannot_extend_exhausted_general_budget_or_reopen_published(tmp_path):
    _, workflow, run = make(tmp_path)
    workflow.run(run)
    state = workflow._read(run)
    state['batches'][0]['semantic_repairs'] = state['max_repairs']
    workflow._save(state)
    with pytest.raises(ValueError, match='remaining ordinary'):
        register(workflow, run)
    state['batches'][0]['semantic_repairs'] = 0
    workflow._save(state)
    workflow.commit(run)
    with pytest.raises(ValueError, match='unpublished'):
        register(workflow, run)


def test_process_finding_refuses_unrelated_field_rewrite_and_retains_consumption(tmp_path):
    runner = ProcessRunner(('pass',))
    _, workflow, run = make(tmp_path, runner)
    workflow.run(run)
    register(workflow, run)
    runner.tamper = True
    assert workflow.run(run)['status'] == 'needs_review'
    batch = workflow._read(run)['batches'][0]
    assert batch['semantic_repairs'] == 1
    assert batch['external_content_findings'][0]['used'] == 1


def test_process_finding_still_blocks_if_general_budget_exhausts_after_registration(tmp_path):
    _, workflow, run = make(tmp_path)
    workflow.run(run)
    register(workflow, run)
    state = workflow._read(run)
    state['batches'][0]['semantic_repairs'] = state['max_repairs']
    workflow._save(state)
    assert workflow.run(run)['status'] == 'needs_review'
    batch = workflow._read(run)['batches'][0]
    assert batch['external_content_findings'][0]['used'] == 0
    assert batch['semantic_repairs'] == state['max_repairs']


def test_rejected_ordinary_process_uses_remaining_budget_without_another_allowance(tmp_path):
    class RichProcess(ProcessRunner):
        repairs = 0

        def run(self, request, schema, images, directory):
            result = super().run(request, schema, images, directory)
            if request['mode'] == 'content_repair':
                self.repairs += 1
                if self.repairs > 1:
                    result['items'][0]['explanation']['process'][-1:] = [
                        {'kind': 'text', 'text': '입력 합 '}, {'kind': 'math', 'latex': 'x+y'},
                        {'kind': 'text', 'text': '를 결과로 삼는다.'}]
            return result

    runner = RichProcess(('pass', 'repair', 'pass'))
    _, workflow, run = make(tmp_path, runner)
    workflow.run(run)
    state = workflow._read(run)
    state['max_repairs'] = 4
    state['batches'][0]['semantic_repairs'] = 1
    workflow._save(state)
    register(workflow, run)
    assert workflow.run(run)['status'] == 'ready'
    batch = workflow._read(run)['batches'][0]
    finding = batch['external_content_findings'][0]
    assert batch['semantic_repairs'] == 3 and finding['used'] == finding['allowance'] == 1
    assert [h['verdict'] for h in finding['review_history']] == ['repair', 'pass']
    assert finding['status'] == 'resolved'
    assert batch['retired_kind_certificates'] and batch['kind_certificate']['status'] == 'pass'


def test_rich_process_preserves_original_steps_and_requires_one_contiguous_group():
    from pairper.explanation.selection_workflow import SelectionWorkflow
    baseline = [{'kind': 'text', 'text': 'sample'}, {'kind': 'math', 'latex': 'x'},
                {'kind': 'text', 'text': 'clip'}]
    group = [{'kind': 'text', 'text': 'input'}, {'kind': 'math', 'latex': 'x'}]
    assert SelectionWorkflow._one_process_group(baseline, baseline[:2] + group + baseline[2:])
    assert not SelectionWorkflow._one_process_group(baseline, group[:1] + baseline + group[1:])
    assert not SelectionWorkflow._one_process_group(baseline, baseline[:1] + group)


def test_rejected_ordinary_process_does_not_resume_stale_review_or_exhausted_budget(tmp_path):
    runner = ProcessRunner(('pass', 'repair'))
    _, workflow, run = make(tmp_path, runner)
    workflow.run(run)
    register(workflow, run)
    workflow.step(run)  # One real repair and its rejection; retain it before the next step.
    state = workflow._read(run)
    batch = state['batches'][0]
    bundle = workflow._fresh(state)
    assert workflow._ordinary_process_retry(state, batch, bundle)
    batch['review_context_sha256'] = 'changed'
    assert not workflow._ordinary_process_retry(state, batch, bundle)
    batch['review_context_sha256'] = digest(workflow._packet(bundle, batch['block_ids'],
                                                            target_ids=batch['target_ids']))
    batch['semantic_repairs'] = state['max_repairs']
    assert not workflow._ordinary_process_retry(state, batch, bundle)
