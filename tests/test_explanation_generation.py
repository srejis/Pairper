"""Workflow safety and actual review/repair state transitions, never fake model quality claims."""
import copy
import json

import pytest

from pairper.explanation.catalog import target_id
from pairper.explanation.prompts import build_prompt, validate_protocol
from pairper.explanation.runner import ExplanationRunner
from pairper.explanation.workflow import ExplanationWorkflow, batch_plan, source_batches


def bundle():
    target = {'kind': 'math', 'block_id': 'b', 'field': 'latex', 'run_index': None,
              'start': None, 'end': None, 'source_text': 'z=x+y', 'source_refs': ['s'],
              'display': True}
    target['id'] = target_id(target)
    return {'paper_id': 'p', 'fingerprint': 'fp', 'source_identity': {},
            'context': [{'block_id': 'b', 'text': 'MLP output z=x+y', 'type': 'paragraph'}],
            'text_units': [{'block_id': 'b', 'field': 'runs', 'run_index': 0,
                            'text': 'MLP output', 'source_refs': ['s']}], 'targets': [target]}


class Service:
    def __init__(self, directory):
        self.directory = directory
        self.bundle = bundle()
        self.saved = None
        self.published = False

    def prepare(self, paper_id):
        return copy.deepcopy(self.bundle)

    def save(self, paper_id, explanations, **kwargs):
        self.saved = {'paper_id': paper_id, 'explanations': explanations, 'revision': 1, **kwargs}
        return self.saved

    def publish(self, paper_id, expected_revision=None):
        assert expected_revision == self.saved['revision']
        self.published = True
        return self.saved


class Runner:
    last_evidence = {'test_double': True}

    def __init__(self, verdicts=('pass',), fail_review=False):
        self.calls = []
        self.verdicts = iter(verdicts)
        self.fail_review = fail_review

    def run(self, request, schema, images, artifact_dir):
        self.calls.append(copy.deepcopy(request))
        if request['stage'] == 'review':
            if self.fail_review:
                self.fail_review = False
                raise RuntimeError('interrupted reviewer')
            verdict = next(self.verdicts)
            return {'source_fingerprint': 'fp', 'verdict': verdict,
                    'checked_keys': [i['key'] for i in request['candidate']['items']],
                    'inherited_issue_checks': [{'issue_id': i['issue_id'], 'verdict': 'resolved',
                                                'reason': 'Current candidate fixes the issue.'}
                                               for i in request.get('inherited_issues', [])],
                    'issues': [] if verdict == 'pass' else [
                        {'target_key': 'formula', 'code': 'abstract', 'detail': 'Explain input directly'}]}
        target = {k: v for k, v in request['packet']['targets'][0].items() if k != 'source_refs'}
        target['aliases'] = []
        target['concept_key'] = ''
        explanation = {'kind': 'math', 'math_role': 'formula', 'category': '', 'description': [],
                       'exclusion_reason': '',
                       'symbols': [{'symbol': 'x', 'meaning': '첫 번째 입력값'},
                                   {'symbol': 'y', 'meaning': '두 번째 입력값'},
                                   {'symbol': 'z', 'meaning': '두 입력값을 더한 결과'}],
                       'meaning': [{'kind': 'text', 'text': '두 입력값을 합한다.'}],
                       'process': [{'kind': 'text', 'text': '첫 번째 입력값에 두 번째 입력값을 더한다.'}]}
        return {'source_fingerprint': 'fp', 'items': [
            {'key': 'formula', 'target': target, 'explanation': explanation}]}


def make(tmp_path, runner=None, max_repairs=2):
    service = Service(tmp_path)
    workflow = ExplanationWorkflow(service, runner or Runner(), max_repairs=max_repairs)
    return service, workflow, workflow.start('p')['run_id']


def test_generate_fresh_review_then_publish_bound_evidence(tmp_path):
    runner = Runner()
    service, workflow, run = make(tmp_path, runner)
    assert workflow.step(run)['status'] == 'ready'
    assert [c['stage'] for c in runner.calls] == ['generate', 'review']
    assert runner.calls[-1]['candidate']['items'][0]['target']['aliases'] == []
    assert runner.calls[-1]['candidate']['items'][0]['target']['concept_key'] == ''
    workflow.commit(run)
    assert service.published
    assert service.saved['explanations'][0]['reviewed']
    assert service.saved['explanations'][0]['evidence_refs'] == ['s']
    assert len(service.saved['workflow_evidence']['batches'][0]['evidence']) == 2


def test_actual_semantic_issue_forces_repair_and_new_review(tmp_path):
    runner = Runner(('repair', 'pass'))
    service, workflow, run = make(tmp_path, runner)
    assert workflow.step(run)['status'] == 'pending'
    with pytest.raises(ValueError, match='Every batch'):
        workflow.commit(run)
    assert workflow.step(run)['status'] == 'ready'
    assert [c['stage'] for c in runner.calls] == ['generate', 'review', 'repair', 'review']
    assert runner.calls[2]['review']['issues']
    workflow.commit(run)
    assert service.published


def test_rejected_source_binding_enters_bounded_repair_with_concrete_feedback(tmp_path):
    class BrokenBinding(Runner):
        def run(self, request, schema, images, artifact_dir):
            result = super().run(request, schema, images, artifact_dir)
            if request['stage'] == 'generate':
                result['items'][0]['target']['source_text'] = 'wrong source equation'
            return result
    runner = BrokenBinding()
    service, workflow, run = make(tmp_path, runner)
    assert workflow.step(run)['status'] == 'pending'
    state = workflow._read(run)
    assert 'binding changed' in state['batches'][0]['structural_issues'][0]
    with pytest.raises(ValueError):
        workflow.commit(run)
    assert workflow.step(run)['status'] == 'ready'
    repair = next(c for c in runner.calls if c['stage'] == 'repair')
    assert 'binding changed' in repair['validation_feedback'][0]
    assert [c['stage'] for c in runner.calls] == ['generate', 'repair', 'review']
    workflow.commit(run)
    assert service.published


def test_structural_attempts_do_not_consume_semantic_repair_budget(tmp_path):
    class BrokenFirst(Runner):
        def run(self, request, schema, images, artifact_dir):
            result = super().run(request, schema, images, artifact_dir)
            if request['stage'] == 'generate':
                result['items'][0]['target']['source_text'] = 'incorrect source'
            return result
    service, workflow, run = make(tmp_path, BrokenFirst(('repair', 'repair', 'pass')))
    for _ in range(4):
        report = workflow.step(run)
    assert report['status'] == 'ready'
    batch = workflow._read(run)['batches'][0]
    assert batch['structural_repairs'] == 1
    assert batch['semantic_repairs'] == 2
    assert batch['repairs'] == 3
    workflow.commit(run)
    assert service.published


def test_legacy_repair_counters_recovered_from_actual_request_evidence(tmp_path):
    _, workflow, _ = make(tmp_path)
    events = []
    for number, request in enumerate([{'validation_feedback': ['offset error']},
                                      {'review': {'issues': [{'detail': 'meaning error'}]}}]):
        folder = tmp_path / str(number)
        folder.mkdir()
        (folder / 'request.json').write_text(json.dumps(request), encoding='utf-8')
        events.append({'stage': 'repair', 'transport': {'directory': str(folder)}})
    assert workflow._repair_counts({'evidence': events}) == {'structural': 1, 'semantic': 1}


def test_nested_durable_repairs_raise_existing_counter_without_double_counting(tmp_path):
    _, workflow, _ = make(tmp_path)
    events = []
    for number in range(6):
        folder = tmp_path / ('history-' + str(number))
        folder.mkdir()
        request = {'review': {'issues': [{'code': 'process_symbol_binding' if number == 0 else 'missing_term'}]}}
        (folder / 'request.json').write_text(json.dumps(request), encoding='utf-8')
        events.append({'stage': 'repair', 'transport': {'directory': str(folder)}})
    batch = {'semantic_repairs': 4, 'structural_repairs': 1,
        'evidence': [{'stage': 'adopted_candidate', 'repair_counts_before': {'semantic': 4, 'structural': 1},
                      'previous_generation_evidence': events + events[:2]}]}
    assert workflow._repair_counts(batch) == {'structural': 1, 'semantic': 6}
    assert len(batch['repair_budget_audit']['durable_requests']) == 6
    batch['semantic_repairs'] = 8
    assert workflow._repair_counts(batch)['semantic'] == 8


def test_failed_batch_is_retained_while_other_pending_batches_are_processed(tmp_path):
    service = Service(tmp_path)
    source = service.bundle
    source['context'][0]['text'] = 'a' * 600
    source['context'].append({'block_id': 'c', 'type': 'paragraph', 'text': 'b' * 600})
    source['text_units'].append({**source['text_units'][0], 'block_id': 'c'})
    target = {**source['targets'][0], 'block_id': 'c'}
    target['id'] = target_id(target)
    source['targets'].append(target)
    workflow = ExplanationWorkflow(service, Runner(('repair', 'pass')), max_repairs=0)
    run = workflow.start('p', max_characters=500)['run_id']
    assert workflow.step(run)['status'] == 'pending'
    assert workflow.step(run)['status'] == 'pending'
    assert workflow.step(run)['reviewed_batches'] == 1
    assert workflow.step(run)['status'] == 'needs_review'
    state = workflow._read(run)
    assert [b['status'] for b in state['batches']] == ['needs_review', 'reviewed']
    with pytest.raises(ValueError):
        workflow.commit(run)


def test_runner_unique_quote_offsets_preserve_raw_provider_and_normalized_artifacts(tmp_path):
    request = {'stage': 'generate', 'source_fingerprint': 'fp', 'packet': bundle()}
    response = Runner().run(request, {}, [], None)
    response['items'].append({'key': 'term', 'target': {
        'id': None, 'kind': 'term', 'block_id': 'b', 'field': 'runs', 'run_index': 0,
        'start': 99, 'end': 102, 'source_text': 'MLP', 'aliases': ['다층 퍼셉트론'],
        'concept_key': 'mlp', 'display': False},
        'explanation': {'kind': 'term', 'math_role': 'formula', 'exclusion_reason': '',
            'category': '신경망', 'description': [{'kind': 'text', 'text': '입력을 계산하는 신경망이다.'}],
            'symbols': [], 'meaning': [], 'process': []}})
    raw = copy.deepcopy(response)
    attempt = tmp_path / 'attempt-01'
    attempt.mkdir()
    provider = attempt / 'last-message.json'
    provider.write_text(json.dumps(raw), encoding='utf-8')
    runner = object.__new__(ExplanationRunner)
    runner.last_evidence = {'directory': str(tmp_path), 'attempts': []}
    runner._validate_protocol(request, response, image_count=0)
    assert response['items'][-1]['target']['start'] == 0
    assert response['items'][-1]['target']['end'] == 3
    assert json.loads(provider.read_text('utf-8')) == raw
    normalized = json.loads((tmp_path / 'normalized-response.json').read_text('utf-8'))
    assert normalized == response
    audit = runner.last_evidence['binding_normalization']
    assert audit['raw_response_object_sha256'] != audit['normalized_response_sha256']
    assert audit['corrections'][0]['method'] == 'unique_exact_quote'


def test_interruption_keeps_generated_candidate_and_resumes_review(tmp_path):
    runner = Runner(fail_review=True)
    service, workflow, run = make(tmp_path, runner)
    with pytest.raises(RuntimeError):
        workflow.step(run)
    assert workflow.report(run)['item_count'] == 1
    assert workflow.step(run)['status'] == 'ready'
    assert [c['stage'] for c in runner.calls] == ['generate', 'review', 'review']


def test_changed_source_and_post_review_candidate_tamper_block_publish(tmp_path):
    service, workflow, run = make(tmp_path)
    workflow.step(run)
    path = workflow._path(run)
    state = json.loads(path.read_text('utf-8'))
    state['batches'][0]['candidate']['items'][0]['explanation']['meaning'][0]['text'] = 'changed'
    path.write_text(json.dumps(state), encoding='utf-8')
    with pytest.raises(ValueError, match='not bound'):
        workflow.commit(run)
    service.bundle['fingerprint'] = 'new-source'
    assert workflow.report(run)['status'] == 'source_changed'
    with pytest.raises(ValueError, match='Source changed'):
        workflow.commit(run)
    assert not service.published


def test_unresolved_review_exhaustion_never_publishes(tmp_path):
    service, workflow, run = make(tmp_path, Runner(('repair',)), max_repairs=0)
    assert workflow.step(run)['status'] == 'pending'
    assert workflow.step(run)['status'] == 'needs_review'
    with pytest.raises(ValueError):
        workflow.commit(run)
    assert not service.published


def test_changed_prompt_protocol_cannot_publish_old_review(tmp_path):
    service, workflow, run = make(tmp_path)
    state = workflow._read(run)
    state['prompt_version'] = 'retired-protocol'
    workflow._save(state)
    assert workflow.report(run)['status'] == 'needs_review'
    with pytest.raises(ValueError, match='protocol changed'):
        workflow.commit(run)
    assert not service.published


def test_protocol_adoption_carries_candidate_but_requires_entirely_new_review(tmp_path):
    old_runner = Runner()
    service, old, previous = make(tmp_path, old_runner)
    assert old.step(previous)['status'] == 'ready'
    state = old._read(previous)
    state['prompt_version'] = 'retired-protocol'
    old._save(state)
    fresh_runner = Runner()
    fresh = ExplanationWorkflow(service, fresh_runner)
    report = fresh.start('p', experiment_id='upgraded', adopt_from_run_id=previous)
    assert report['reviewed_batches'] == 0
    assert report['item_count'] == 1
    with pytest.raises(ValueError, match='Every batch'):
        fresh.commit(report['run_id'])
    assert fresh.step(report['run_id'])['status'] == 'ready'
    assert [r['stage'] for r in fresh_runner.calls] == ['review']
    adopted = fresh._read(report['run_id'])
    assert adopted['adoption']['old_reviews_invalidated']
    assert adopted['batches'][0]['evidence'][0]['approval_carried'] is False
    fresh.commit(report['run_id'])
    assert service.published


def test_protocol_adoption_rejects_different_source_or_different_batch_plan(tmp_path):
    service, workflow, previous = make(tmp_path)
    service.bundle['fingerprint'] = 'other-source'
    with pytest.raises(ValueError, match='identical paper and source'):
        workflow.start('p', experiment_id='other', adopt_from_run_id=previous)
    service.bundle['fingerprint'] = 'fp'
    old = workflow._read(previous)
    old['batches'][0]['include_text'] = False
    workflow._save(old)
    with pytest.raises(ValueError, match='identical complete batch plan'):
        workflow.start('p', experiment_id='other', adopt_from_run_id=previous)


def test_consecutive_adoption_without_new_review_preserves_known_issues(tmp_path):
    service, old, original = make(tmp_path, Runner(('repair',)))
    old.step(original)
    first = old.start('p', experiment_id='migration-one', adopt_from_run_id=original)['run_id']
    first_issues = old._read(first)['batches'][0]['inherited_issues']
    assert first_issues
    second = old.start('p', experiment_id='migration-two', adopt_from_run_id=first)['run_id']
    second_batch = old._read(second)['batches'][0]
    assert second_batch['inherited_issues'] == first_issues
    assert second_batch['review'] is None
    with pytest.raises(ValueError):
        old.commit(second)


def test_candidate_validator_rejects_duplicate_keys_before_adoption_or_commit(tmp_path):
    _, workflow, _ = make(tmp_path)
    request = {'stage': 'generate', 'source_fingerprint': 'fp', 'packet': bundle()}
    candidate = Runner().run(request, {}, [], None)
    candidate['items'].append(copy.deepcopy(candidate['items'][0]))
    with pytest.raises(ValueError, match='nonempty and unique'):
        workflow._validate_candidate(bundle(), candidate, require_current=False)


def test_rejected_adoption_candidate_still_preserves_quality_budget_and_known_issues(tmp_path):
    _, workflow, original = make(tmp_path, Runner(('repair',)))
    workflow.step(original)
    state = workflow._read(original)
    batch = state['batches'][0]
    batch['semantic_repairs'] = 2
    batch['structural_repairs'] = 0
    batch['candidate']['items'].append(copy.deepcopy(batch['candidate']['items'][0]))
    workflow._save(state)
    migrated = workflow.start('p', experiment_id='invalid-candidate', adopt_from_run_id=original)['run_id']
    after = workflow._read(migrated)
    assert after['batches'][0]['candidate'] is None
    assert after['batches'][0]['semantic_repairs'] == 2
    assert after['batches'][0]['inherited_issues']
    assert after['adoption']['rejected_batches']


def test_legacy_author_repair_does_not_erase_unchecked_previous_review(tmp_path):
    _, workflow, _ = make(tmp_path)
    request = {'stage': 'generate', 'source_fingerprint': 'fp', 'packet': bundle()}
    candidate = Runner().run(request, {}, [], None)
    folder = tmp_path / 'prior-review'
    folder.mkdir()
    issue = {'target_key': 'formula', 'code': 'meaning', 'detail': 'An actual unresolved source error'}
    (folder / 'response.json').write_text(json.dumps({'issues': [issue]}), encoding='utf-8')
    (folder / 'request.json').write_text(json.dumps({'candidate': candidate}), encoding='utf-8')
    previous = {'candidate': candidate, 'review': None,
                'evidence': [{'stage': 'review', 'transport': {'directory': str(folder)}}]}
    restored = workflow._inherited_issues(previous, 'exw_old')
    assert restored[0]['issue'] == issue
    assert restored[0]['source_target']['source_text'] == 'z=x+y'


def test_current_korean_context_uses_source_unit_binding_and_invalidates_old_review(tmp_path):
    runner = Runner(('pass', 'pass'))
    service, workflow, run = make(tmp_path, runner)
    revision = [1]
    service.translation_surfaces = lambda paper_id, source: {
        'translation_session_id': 'ko', 'translation_revision': revision[0], 'source_identity': {},
        'surfaces': [{'block_id': 'different-visible-host', 'field': 'runs', 'run_index': 0,
                      'source_unit_ids': ['b/runs'], 'text': '다층 퍼셉트론 출력'}]}
    assert workflow.step(run)['status'] == 'ready'
    packet = runner.calls[-1]['packet']
    assert packet['translation_surfaces'][0]['text'] == '다층 퍼셉트론 출력'
    revision[0] = 2
    with pytest.raises(ValueError, match='Korean reader text changed'):
        workflow.commit(run)
    assert workflow.report(run)['reviewed_batches'] == 0
    assert workflow.step(run)['status'] == 'ready'
    workflow.commit(run)
    assert service.published


def test_prompt_scope_and_math_process_no_obligatory_invented_numbers():
    prompt = build_prompt({'stage': 'generate', 'source_fingerprint': 'fp', 'packet': bundle()})
    assert 'sentences or ordinary phrases' in prompt
    assert 'including inline formulas' in prompt
    assert 'Never invent numerical examples' in prompt
    assert 'human meaning FIRST' in prompt
    assert 'mathematical evaluation in prose' in prompt
    assert 'source_refs' not in prompt


def test_protocol_rejects_missing_math_fake_refs_and_incomplete_review():
    request = {'stage': 'generate', 'source_fingerprint': 'fp', 'packet': bundle()}
    with pytest.raises(ValueError, match='every source formula'):
        validate_protocol(request, {'source_fingerprint': 'fp', 'items': []})
    candidate = Runner().run(request, {}, [], None)
    candidate['items'][0]['target']['source_text'] = 'unrelated'
    with pytest.raises(ValueError, match='binding changed'):
        validate_protocol(request, candidate)
    review_request = {**request, 'stage': 'review', 'candidate': candidate}
    with pytest.raises(ValueError, match='omitted'):
        validate_protocol(review_request, {'source_fingerprint': 'fp', 'verdict': 'pass',
                                            'checked_keys': [], 'issues': []})


def test_prioritization_preserves_every_block_and_complete_source_scope(tmp_path):
    service, workflow, run = make(tmp_path)
    before = workflow.report(run)
    workflow.prioritize(run, 'b')
    assert workflow.report(run)['total_batches'] == before['total_batches']
    assert [x for batch in source_batches(bundle(), 500) for x in batch] == ['b']


def test_dense_inline_math_split_preserves_all_targets_and_text_once():
    dense = bundle()
    dense['targets'] = [{**dense['targets'][0], 'id': f'math{i}'} for i in range(41)]
    plan = batch_plan(dense, max_math_targets=16)
    assert [len(b['target_ids']) for b in plan] == [16, 16, 9]
    assert [t for b in plan for t in b['target_ids']] == [t['id'] for t in dense['targets']]
    assert sum(b['include_text'] for b in plan) == 1
