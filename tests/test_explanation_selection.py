"""Fixed selection never weakens content review or erases prior rejections."""
import copy

import pytest
from test_explanation_generation import Runner as OldRunner
from test_explanation_generation import Service

from pairper.explanation.selection_prompts import (
    SELECTION_SCHEMA,
    build_prompt,
    unit_key,
    validate_protocol,
)
from pairper.explanation.selection_workflow import SelectionWorkflow


def term(text='MLP', start=0):
    return {'id': None, 'kind': 'term', 'block_id': 'b', 'field': 'runs', 'run_index': 0,
            'start': start, 'end': start + len(text), 'source_text': text, 'display': False,
            'aliases': [], 'concept_key': text.lower()}


class Runner:
    last_evidence = {'test_double': True}

    def __init__(self, verdicts=('pass',), late=False, broken=False):
        self.calls = []
        self.verdicts = iter(verdicts)
        self.late = late
        self.broken = broken

    def run(self, request, schema, images, directory):
        self.calls.append(copy.deepcopy(request))
        if request['mode'] == 'kind_review':
            ids = [c['target_id'] for c in request['input']['cases']]
            return {'input_sha256': request['input_sha256'], 'checked_ids': ids,
                    'decisions': [{'target_id': i, 'verdict': 'pass', 'reason': '실제 상위 종류를 밝힌다'} for i in ids]}
        if request['mode'] == 'selection':
            return {'source_fingerprint': 'fp',
                'checked_unit_keys': [unit_key(u) for u in request['packet']['text_units']],
                'selected_terms': [{'key': 'mlp', 'target': term(), 'reason': '이 문장의 신경망 이름'}],
                'deferred_terms': [],
                'existing_decisions': [{'target_id': t['id'], 'decision': 'retain', 'reason': '필요한 용어'}
                                       for t in request['proposed_terms']],
                'known_issue_decisions': [{'issue_id': i['issue_id'], 'classification': 'content',
                    'disposition': 'carry_to_content', 'target_keys': [], 'reason': '내용 검토 필요'}
                    for i in request['inherited_issues']]}
        if request['mode'] == 'content_review':
            verdict = next(self.verdicts)
            math = next(i for i in request['candidate']['items'] if i['target']['kind'] == 'math')
            return {'source_fingerprint': 'fp', 'verdict': verdict,
                'checked_keys': [i['key'] for i in request['candidate']['items']],
                'issues': [] if verdict == 'pass' else [
                    {'target_key': math['key'], 'code': 'abstract', 'detail': '입력값 설명 필요'}],
                'inherited_issue_checks': [{'issue_id': i['issue_id'], 'verdict': 'resolved',
                                          'reason': '수정된 설명을 원문과 대조함'}
                                         for i in request['inherited_issues']],
                'late_terms': ([{'key': 'activation', 'target': term('activation', 4),
                                'reason': '후속 선정에서 추가 검토할 용어', 'inherited_issue_ids': []}]
                               if self.late else [])}
        items = []
        for target in request['generation_targets']:
            target = {k: v for k, v in target.items()
                      if k not in ('source_refs', 'source_math_id', 'anchor_type', 'text_parts')}
            if target['kind'] == 'math':
                old = OldRunner().run({'stage': 'generate', 'packet': request['packet']}, None, [], directory)
                explanation = old['items'][0]['explanation']
            else:
                explanation = {'kind': 'term', 'math_role': 'formula', 'exclusion_reason': '',
                    'category': '신경망', 'description': [{'kind': 'text', 'text': '입력을 여러 계산 단계를 거쳐 바꾸는 신경망.'}],
                    'symbols': [], 'meaning': [], 'process': []}
            if self.broken:
                target['source_text'] = 'made up'
            items.append({'key': target['id'], 'target': target, 'explanation': explanation})
        return {'source_fingerprint': 'fp', 'items': items}


def make(tmp_path, runner=None):
    service = Service(tmp_path)
    workflow = SelectionWorkflow(service, runner or Runner())
    run = workflow.start('p')['run_id']
    return service, workflow, run


def test_selection_then_exact_scope_generation_review_publish(tmp_path):
    service, workflow, run = make(tmp_path)
    assert workflow.run(run)['status'] == 'ready'
    assert [c['mode'] for c in workflow.runner.calls] == ['selection', 'content_generate', 'content_review', 'kind_review']
    state = workflow._read(run)
    assert len(state['batches'][0]['manifest']['targets']) == 2
    workflow.commit(run)
    assert service.published
    frozen = service.saved['workflow_evidence']['batches'][0]['evidence'][-1]
    assert frozen['stage'] == 'frozen_selection_evidence'
    assert frozen['manifest']['fingerprint']


def test_new_optional_term_is_exact_source_backlog_not_content_failure(tmp_path):
    runner = Runner(late=True)
    service, workflow, run = make(tmp_path, runner)
    service.bundle['text_units'][0]['text'] = 'MLP activation'
    assert workflow.run(run)['status'] == 'ready'
    batch = workflow._read(run)['batches'][0]
    assert len(batch['candidate']['items']) == 2
    assert batch['supplements'][0]['target']['source_text'] == 'activation'
    assert len(batch['manifest']['targets']) == 2
    workflow.commit(run)
    assert service.published


def test_only_bad_target_is_repaired_and_all_content_reviewed_again(tmp_path):
    runner = Runner(('repair', 'pass'))
    _, workflow, run = make(tmp_path, runner)
    workflow.step(run)
    before = workflow._read(run)['batches'][0]['candidate']['items']
    assert workflow.run(run)['status'] == 'ready'
    repair = next(c for c in runner.calls if c['mode'] == 'content_repair')
    assert len(repair['generation_targets']) == 1
    assert repair['generation_targets'][0]['kind'] == 'math'
    assert len(next(c for c in reversed(runner.calls) if c['mode'] == 'content_review')['candidate']['items']) == 2
    after = workflow._read(run)['batches'][0]['candidate']['items']
    assert next(i for i in before if i['target']['kind'] == 'term') == next(
        i for i in after if i['target']['kind'] == 'term')
    assert next(c for c in reversed(runner.calls) if c['mode'] == 'content_review')['inherited_issues']


def test_tampered_manifest_cannot_publish(tmp_path):
    service, workflow, run = make(tmp_path)
    workflow.run(run)
    state = workflow._read(run)
    state['batches'][0]['manifest']['reasons'] = {'fake': 'fake'}
    workflow._save(state)
    with pytest.raises(ValueError, match='manifest was changed'):
        workflow.commit(run)
    assert not service.published


def test_invalid_fixed_binding_is_bounded_and_never_reviewed(tmp_path):
    runner = Runner(broken=True)
    service, workflow, run = make(tmp_path, runner)
    assert workflow.run(run)['status'] == 'needs_review'
    assert not any(c['mode'] == 'content_review' for c in runner.calls)
    assert len([c for c in runner.calls if c['mode'] == 'content_generate']) == 3
    assert workflow._read(run)['batches'][0]['structural_repairs'] == 3
    assert not service.published


def test_content_cannot_be_reclassified_as_selection():
    request = {'mode': 'selection', 'source_fingerprint': 'fp', 'packet': {'text_units': []},
        'proposed_terms': [], 'inherited_issues': [{'issue_id': 'i', 'issue': {'target_key': 'm1'}}]}
    response = {'source_fingerprint': 'fp', 'checked_unit_keys': [], 'selected_terms': [],
        'deferred_terms': [], 'existing_decisions': [], 'known_issue_decisions': [
            {'issue_id': 'i', 'classification': 'selection', 'disposition': 'deferred_selection',
             'target_keys': [], 'reason': 'optional'}]}
    with pytest.raises(ValueError, match='content defect'):
        validate_protocol(request, response)


def test_source_change_blocks_old_selection(tmp_path):
    service, workflow, run = make(tmp_path)
    workflow.run(run)
    service.bundle['fingerprint'] = 'changed'
    with pytest.raises(ValueError, match='Source changed'):
        workflow.commit(run)
    assert not service.published


def test_failed_v6_draft_keeps_repair_budget_and_needs_new_selection(tmp_path):
    from pairper.explanation.workflow import ExplanationWorkflow
    service = Service(tmp_path)
    old = ExplanationWorkflow(service, OldRunner())
    previous = old.start('p')['run_id']
    old.step(previous)
    state = old._read(previous)
    state['batches'][0]['status'] = 'needs_review'
    state['batches'][0]['semantic_repairs'] = 2
    old._save(state)
    runner = Runner()
    workflow = SelectionWorkflow(service, runner)
    run = workflow.start('p', adopt_from_run_id=previous)['run_id']
    batch = workflow._read(run)['batches'][0]
    assert batch['manifest'] is None and batch['status'] != 'reviewed'
    assert batch['semantic_repairs'] == 2
    assert workflow.run(run)['status'] == 'ready'
    assert runner.calls[0]['mode'] == 'selection'
    assert runner.calls[-1]['mode'] == 'kind_review'


def test_malformed_selection_binding_retries_with_feedback_and_no_frozen_manifest(tmp_path):
    class Invalid(Runner):
        def run(self, request, schema, images, directory):
            result = super().run(request, schema, images, directory)
            if request['mode'] == 'selection':
                result['selected_terms'][0]['target']['source_text'] = 'hallucination'
            return result
    _, workflow, run = make(tmp_path, Invalid())
    assert workflow.run(run)['status'] == 'needs_review'
    batch = workflow._read(run)['batches'][0]
    assert batch['manifest'] is None
    assert workflow.runner.calls[1]['validation_feedback']


def test_supplemental_text_formula_appends_without_changing_legacy_packet(tmp_path):
    from pairper.explanation.text_math import decorate
    from pairper.explanation.workflow import ExplanationWorkflow
    service = Service(tmp_path)
    service.bundle['text_units'][0].update(text='MLP SR > 0', block_type='paragraph')
    legacy = copy.deepcopy(service.bundle)
    service.bundle = decorate(service.bundle)
    workflow = SelectionWorkflow(service, Runner(('pass', 'pass')))
    run = workflow.start('p')['run_id']
    state = workflow._read(run)
    assert len(state['batches']) == 2
    assert state['batches'][1]['include_text'] is False
    first = state['batches'][0]
    packet = workflow._packet(service.bundle, first['block_ids'], target_ids=first['target_ids'])
    previous = ExplanationWorkflow(service)._packet(legacy, first['block_ids'], target_ids=first['target_ids'])
    assert packet == previous
    assert workflow.run(run)['status'] == 'ready'
    assert len([c for c in workflow.runner.calls if c['mode'] == 'selection']) == 1
    extra = workflow._read(run)['batches'][1]['candidate']['items'][0]
    assert extra['target']['source_text'] == 'SR > 0'
    assert extra['target']['anchor_type'] == 'text_math'
    workflow.commit(run)
    assert service.published


def test_source_bound_but_unreviewed_neighbor_is_not_valid_selection_provenance(tmp_path):
    service, workflow, _ = make(tmp_path)
    target = term()
    target['block_id'] = 'neighbor'
    packet = {'text_units': service.bundle['text_units']}
    with pytest.raises(ValueError, match='outside the independently reviewed'):
        workflow._term_scope(packet, [target])


def test_known_scientific_defect_cannot_be_waived_as_schema_contract():
    request = {'mode': 'selection', 'source_fingerprint': 'fp', 'packet': {'text_units': []},
        'proposed_terms': [], 'inherited_issues': [{'issue_id': 'i',
            'issue': {'target_key': 'm1', 'code': 'wrong_symbol'}}]}
    response = {'source_fingerprint': 'fp', 'checked_unit_keys': [], 'selected_terms': [],
        'deferred_terms': [], 'existing_decisions': [], 'known_issue_decisions': [
            {'issue_id': 'i', 'classification': 'contract', 'disposition': 'not_applicable',
             'target_keys': [], 'reason': 'obsolete'}]}
    with pytest.raises(ValueError, match='Scientific rejections'):
        validate_protocol(request, response)


def test_selection_prompt_does_not_inherit_content_or_math_generation_rules(tmp_path):
    _, workflow, run = make(tmp_path)
    workflow.run(run)
    request = workflow.runner.calls[0]
    assert request['required_checked_unit_keys'] == ['b/runs/0']
    prompt = build_prompt(request)
    assert 'Include ALL supplied' not in prompt
    assert 'Formula process must be nonempty' not in prompt
    assert 'required_checked_unit_keys EXACTLY' in prompt
    assert SELECTION_SCHEMA['properties']['selected_terms']['items']['properties']['target']['properties']['kind'] == {
        'enum': ['term']}


class FindingRunner(Runner):
    def __init__(self, verdicts=('pass', 'pass'), correct=True):
        super().__init__(verdicts)
        self.correct = correct

    def run(self, request, schema, images, directory):
        response = super().run(request, schema, images, directory)
        if request.get('external_content_findings'):
            prior = {i['target']['id']: i for i in request['candidate']['items']}
            for item in response['items']:
                item['explanation'] = copy.deepcopy(prior[item['target']['id']]['explanation'])
                if self.correct:
                    item['explanation']['category'] = '신경망 계산 특성'
        return response


def open_finding(workflow, run):
    batch = workflow._read(run)['batches'][0]
    item = next(i for i in batch['candidate']['items'] if i['target']['kind'] == 'term')
    return workflow.record_content_finding(run, item['target']['id'], field='category',
        observed_value=item['explanation']['category'], reason='범주가 대상 용어의 다른 이름만 반복한다.',
        criterion='MLP=신경망처럼 종류를 먼저 밝히며 추상적 동의어 반복을 피한다.',
        expected_candidate_sha256=batch['review_candidate_sha256'])


def test_external_new_finding_preserves_old_pass_and_grants_one_targeted_repair(tmp_path):
    from pairper.explanation.catalog import digest
    runner = FindingRunner()
    service, workflow, run = make(tmp_path, runner)
    workflow.run(run)
    state = workflow._read(run)
    before = copy.deepcopy(state['batches'][0])
    state['batches'][0]['semantic_repairs'] = 4
    workflow._save(state)
    finding = open_finding(workflow, run)
    reopened = workflow._read(run)['batches'][0]
    assert reopened['semantic_repairs'] == 4
    assert reopened['external_reopened_pass']['review_sha256'] == digest(before['review'])
    with pytest.raises(ValueError):
        workflow.commit(run)
    with pytest.raises(ValueError, match='same external finding'):
        open_finding(workflow, run)
    assert workflow.run(run)['status'] == 'ready'
    after = workflow._read(run)['batches'][0]
    assert after['semantic_repairs'] == 5
    assert after['external_content_findings'][0]['used'] == 1
    assert after['external_content_findings'][0]['status'] == 'resolved'
    assert finding['finding_id'] == after['external_content_findings'][0]['finding_id']
    repair = next(c for c in runner.calls if c.get('external_content_findings'))
    assert len(repair['generation_targets']) == 1
    assert len(next(c for c in reversed(runner.calls) if c['mode'] == 'content_review')['candidate']['items']) == 2
    workflow.commit(run)
    assert service.published


def test_external_allowance_never_applies_to_an_existing_rejected_candidate(tmp_path):
    _, workflow, run = make(tmp_path, Runner(('repair',)))
    workflow.step(run)
    with pytest.raises(ValueError, match='actual candidate-bound prior pass'):
        open_finding(workflow, run)


def test_external_repair_cannot_repeat_old_bad_category_or_receive_more_allowance(tmp_path):
    _, workflow, run = make(tmp_path, FindingRunner(correct=False))
    workflow.run(run)
    open_finding(workflow, run)
    assert workflow.run(run)['status'] == 'needs_review'
    batch = workflow._read(run)['batches'][0]
    assert batch['external_content_findings'][0]['used'] == 1
    assert batch['external_reopened_pass']['review']['verdict'] == 'pass'
    with pytest.raises(ValueError):
        workflow.commit(run)
    with pytest.raises(ValueError, match='same external finding'):
        open_finding(workflow, run)


def test_failed_fresh_review_after_external_repair_stops_without_more_semantic_rounds(tmp_path):
    runner = FindingRunner(('pass', 'repair'))
    _, workflow, run = make(tmp_path, runner)
    workflow.run(run)
    open_finding(workflow, run)
    assert workflow.run(run)['status'] == 'needs_review'
    batch = workflow._read(run)['batches'][0]
    assert batch['external_content_findings'][0]['status'] == 'rejected'
    assert len([c for c in runner.calls if c['mode'] == 'content_repair']) == 1


class ProcessExperimentRunner(Runner):
    def run(self, request, schema, images, directory):
        response = super().run(request, schema, images, directory)
        if request['mode'] == 'content_review' and response['issues']:
            response['issues'][0]['code'] = 'incomplete_math_process'
        if request.get('explicit_content_experiments'):
            prior = {i['target']['id']: i for i in request['candidate']['items']}
            for item in response['items']:
                item['explanation'] = copy.deepcopy(prior[item['target']['id']]['explanation'])
                item['explanation']['process'].append({'kind': 'text', 'text': '두 입력의 합을 결과값으로 둔다.'})
        return response


def record_process_experiment(workflow, run):
    from pairper.explanation.catalog import digest
    batch = workflow._read(run)['batches'][0]
    target = next(i['target']['id'] for i in batch['candidate']['items'] if i['target']['kind'] == 'math')
    return workflow.record_rejected_content_experiment(run, target,
        reason='기존 의미 수정 이력을 보존하고 새로 확인된 계산 과정 오류만 추가 1회 실험한다.',
        expected_candidate_sha256=digest(batch['candidate']), expected_review_sha256=digest(batch['review']))


def test_rejected_process_experiment_is_separate_and_keeps_all_six_old_repairs(tmp_path):
    runner = ProcessExperimentRunner(('repair', 'pass'))
    _, workflow, run = make(tmp_path, runner)
    workflow.step(run)
    state = workflow._read(run)
    before = copy.deepcopy(state['batches'][0]['candidate'])
    state['batches'][0]['semantic_repairs'] = 6
    workflow._save(state)
    result = record_process_experiment(workflow, run)
    assert result['counts_before']['semantic'] == 6
    assert workflow._read(run)['max_repairs'] == 2
    with pytest.raises(ValueError, match='same rejected experiment'):
        record_process_experiment(workflow, run)
    assert workflow.run(run)['status'] == 'ready'
    after = workflow._read(run)['batches'][0]
    assert after['semantic_repairs'] == 7
    assert after['rejected_content_experiments'][0]['used'] == 1
    assert after['rejected_content_experiments'][0]['status'] == 'resolved'
    old_math = next(i for i in before['items'] if i['target']['kind'] == 'math')
    new_math = next(i for i in after['candidate']['items'] if i['target']['kind'] == 'math')
    assert old_math['explanation']['symbols'] == new_math['explanation']['symbols']
    assert old_math['explanation']['meaning'] == new_math['explanation']['meaning']
    assert len([c for c in runner.calls if c.get('explicit_content_experiments')]) == 1


def test_failed_process_experiment_review_is_terminal_and_does_not_reset_counter(tmp_path):
    _, workflow, run = make(tmp_path, ProcessExperimentRunner(('repair', 'repair')))
    workflow.step(run)
    record_process_experiment(workflow, run)
    assert workflow.run(run)['status'] == 'needs_review'
    batch = workflow._read(run)['batches'][0]
    assert batch['rejected_content_experiments'][0]['status'] == 'rejected'
    with pytest.raises(ValueError):
        workflow.commit(run)


def test_process_experiment_cannot_claim_budget_for_another_rejection_type(tmp_path):
    _, workflow, run = make(tmp_path, Runner(('repair',)))
    workflow.step(run)
    with pytest.raises(ValueError, match='not other defects'):
        record_process_experiment(workflow, run)


def test_process_only_experiment_rejects_any_other_explanation_field_change(tmp_path):
    class RewritesMeaning(ProcessExperimentRunner):
        def run(self, request, schema, images, directory):
            result = super().run(request, schema, images, directory)
            if request.get('explicit_content_experiments'):
                result['items'][0]['explanation']['meaning'] = [{'kind': 'text', 'text': '바뀐 의미'}]
            return result
    _, workflow, run = make(tmp_path, RewritesMeaning(('repair', 'pass')))
    workflow.step(run)
    record_process_experiment(workflow, run)
    assert workflow.run(run)['status'] == 'needs_review'
    batch = workflow._read(run)['batches'][0]
    assert 'unrelated explanation field' in batch['structural_issues'][0]
    assert batch['rejected_content_experiments'][0]['used'] == 1


def test_process_experiment_binding_cannot_follow_a_changed_candidate(tmp_path):
    _, workflow, run = make(tmp_path, ProcessExperimentRunner(('repair', 'pass')))
    workflow.step(run)
    record_process_experiment(workflow, run)
    state = workflow._read(run)
    state['batches'][0]['candidate']['items'][0]['explanation']['meaning'][0]['text'] = '바뀐 후보'
    workflow._save(state)
    with pytest.raises(ValueError, match='no longer matches'):
        workflow.step(run)


def test_current_korean_reader_literal_aliases_allow_retained_english_names_without_fuzzy_matching():
    from pairper.explanation.prompts import RULES
    from pairper.explanation.selection_prompts import FIXED_RULES, SELECTION_RULES
    for rules in (FIXED_RULES, SELECTION_RULES):
        assert 'English proper name retained there' in rules
        assert 'actual capitalization or ligature spelling' in rules
        assert 'do not generate' in rules and 'casefold, fuzzy or normalized matches' in rules
        assert 'associated source-unit provenance' in rules
        assert 'one unique exact occurrence' in rules
    assert 'English proper name retained there' not in RULES  # Preserve the legacy source contract text.
