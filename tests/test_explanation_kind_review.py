"""A focused certificate supplements, never replaces, the full scientific review."""
import copy

import pytest
from test_explanation_selection import Runner, make

from pairper.explanation.catalog import digest
from pairper.explanation.kind_review import (
    binding,
    build_prompt,
    certified,
    request_for,
    schema_for,
    validate,
)


def ready(tmp_path, runner=None):
    service, workflow, run = make(tmp_path, runner)
    workflow.run(run)
    return service, workflow, run


def test_small_scope_exact_prototype_rule_and_current_certificate(tmp_path):
    service, workflow, run = ready(tmp_path)
    b = workflow._read(run)['batches'][0]
    request = request_for(service.bundle, b)
    assert len(request['input']['cases']) == 1
    assert set(request['input']['cases'][0]) == {'target_id', 'paper_id', 'source_fingerprint',
        'source_binding', 'concept_key', 'category', 'description', 'source_unit'}
    prompt = build_prompt(request)
    assert 'z=x+y' not in prompt and 'generation_targets' not in prompt
    assert 'Do not prescribe a fixed list' in prompt
    assert certified(service.bundle, b)
    assert b['kind_certificate']['binding'] == binding(service.bundle, b, request)


@pytest.mark.parametrize('field', ['category', 'description', 'concept_key', 'aliases', 'source_unit', 'manifest'])
def test_certificate_is_invalid_when_bound_term_or_context_changes(tmp_path, field):
    service, workflow, run = ready(tmp_path)
    b = workflow._read(run)['batches'][0]
    term = next(i for i in b['candidate']['items'] if i['target']['kind'] == 'term')
    if field == 'category':
        term['explanation']['category'] = '다른 종류'
    elif field == 'description':
        term['explanation']['description'][0]['text'] += ' 달라짐'
    elif field in ('concept_key', 'aliases'):
        term['target'][field] = 'other' if field == 'concept_key' else ['다른이름']
    elif field == 'source_unit':
        service.bundle['text_units'][0]['text'] += ' changed'
    else:
        b['manifest']['fingerprint'] = 'changed'
    assert not certified(service.bundle, b)


def test_exact_ids_hash_and_reason_are_host_checked(tmp_path):
    service, workflow, run = ready(tmp_path)
    b = workflow._read(run)['batches'][0]
    request = request_for(service.bundle, b)
    response = copy.deepcopy(b['kind_certificate']['response'])
    response['checked_ids'] *= 2
    with pytest.raises(ValueError, match='exact term IDs'):
        validate(request, response)
    response = copy.deepcopy(b['kind_certificate']['response'])
    response['input_sha256'] = 'wrong'
    with pytest.raises(ValueError, match='exact term/context'):
        validate(request, response)
    assert schema_for(request)['additionalProperties'] is False


def test_missing_certificate_blocks_commit_then_kind_only_resume_preserves_science(tmp_path):
    service, workflow, run = ready(tmp_path)
    state = workflow._read(run)
    b = state['batches'][0]
    science = {k: copy.deepcopy(b[k]) for k in ('review', 'review_candidate_sha256', 'review_context_sha256')}
    b.pop('kind_certificate')
    workflow._save(state)
    with pytest.raises(ValueError, match='KIND certificate'):
        workflow.commit(run)
    before = len(workflow.runner.calls)
    workflow.certify_kinds(run)
    assert [c['mode'] for c in workflow.runner.calls[before:]] == ['kind_review']
    after = workflow._read(run)['batches'][0]
    assert {k: after[k] for k in science} == science
    workflow.commit(run)
    assert service.published
    stored = service.saved['workflow_evidence']['batches'][0]['evidence'][-1]
    assert stored['kind_certificate'] == after['kind_certificate']
    assert stored['kind_certificate']['binding']['source_fingerprint'] == 'fp'
    assert stored['kind_certificate']['response_sha256'] == digest(stored['kind_certificate']['response'])
    # A valid certificate is reusable without another model call.
    workflow.certify_kinds(run)
    assert len(workflow.runner.calls) == before + 1


class RejectKind(Runner):
    def __init__(self, rewrite=False):
        super().__init__(('pass', 'pass'))
        self.kind_calls = 0
        self.rewrite = rewrite

    def run(self, request, schema, images, directory):
        response = super().run(request, schema, images, directory)
        if request['mode'] == 'kind_review':
            self.kind_calls += 1
            if self.kind_calls == 1:
                response['decisions'][0].update(verdict='reject', reason='범주가 종류 대신 전문어 동의어를 반복한다')
        if request.get('kind_findings'):
            old = {i['target']['id']: i for i in request['candidate']['items']}
            for item in response['items']:
                item['explanation'] = copy.deepcopy(old[item['target']['id']]['explanation'])
                item['explanation']['category'] = '신경망 계산 특성'
                if self.rewrite:
                    item['explanation']['description'] = [{'kind': 'text', 'text': '불필요한 변경'}]
        return response


def test_rejection_repairs_only_category_then_full_science_and_kind_recheck(tmp_path):
    service, workflow, run = ready(tmp_path, RejectKind())
    calls = [c['mode'] for c in workflow.runner.calls]
    assert calls == ['selection', 'content_generate', 'content_review', 'kind_review',
                     'content_repair', 'content_review', 'kind_review']
    repair = next(c for c in workflow.runner.calls if c['mode'] == 'content_repair')
    assert len(repair['generation_targets']) == 1 and repair['generation_targets'][0]['kind'] == 'term'
    batch = workflow._read(run)['batches'][0]
    assert batch['semantic_repairs'] == 1
    assert batch['kind_certificate_history'][0]['status'] == 'repair'
    assert certified(service.bundle, batch)


def test_kind_repair_cannot_rewrite_description(tmp_path):
    _, workflow, run = make(tmp_path, RejectKind(rewrite=True))
    assert workflow.run(run)['status'] == 'needs_review'
    assert 'unrelated explanation field' in workflow._read(run)['error']


def test_kind_rejection_cannot_reset_exhausted_semantic_budget(tmp_path):
    _, workflow, run = make(tmp_path, RejectKind())
    workflow.step(run)  # scientific pass
    state = workflow._read(run)
    state['batches'][0]['semantic_repairs'] = state['max_repairs']
    workflow._save(state)
    assert workflow.run(run)['status'] == 'needs_review'
    assert not any(c['mode'] == 'content_repair' for c in workflow.runner.calls)
    assert workflow._read(run)['batches'][0]['semantic_repairs'] == state['max_repairs']


def test_math_only_scope_has_explicit_empty_certificate_without_model_call(tmp_path):
    service, workflow, run = ready(tmp_path)
    state = workflow._read(run)
    b = state['batches'][0]
    b['candidate']['items'] = [i for i in b['candidate']['items'] if i['target']['kind'] == 'math']
    b['manifest']['targets'] = [t for t in b['manifest']['targets'] if t['kind'] == 'math']
    from pairper.explanation.selection_workflow import _manifest_hash
    b['manifest']['fingerprint'] = _manifest_hash(b['manifest'])
    b['review_manifest_sha256'] = b['manifest']['fingerprint']
    b['review_candidate_sha256'] = digest(b['candidate'])
    b['review']['checked_keys'] = [i['key'] for i in b['candidate']['items']]
    b.pop('kind_certificate')
    workflow._save(state)
    calls = len(workflow.runner.calls)
    workflow.certify_kinds(run)
    assert len(workflow.runner.calls) == calls
    assert workflow._read(run)['batches'][0]['kind_certificate']['status'] == 'empty_term_scope'


def test_actual_prior_pass_kind_finding_can_use_existing_one_shot_route(tmp_path):
    from test_explanation_selection import open_finding
    _, workflow, run = make(tmp_path, RejectKind())
    workflow.step(run)
    state = workflow._read(run)
    state['batches'][0]['semantic_repairs'] = state['max_repairs']
    workflow._save(state)
    workflow.run(run)
    result = open_finding(workflow, run)
    assert result['status'] == 'recorded'
    assert workflow.run(run)['status'] == 'ready'
    b = workflow._read(run)['batches'][0]
    assert b['semantic_repairs'] == state['max_repairs'] + 1
    assert b['external_content_findings'][0]['used'] == 1
    with pytest.raises(ValueError, match='same external finding'):
        open_finding(workflow, run)


@pytest.mark.parametrize('old_status', ['ready', 'published'])
def test_default_cli_does_not_early_stop_before_missing_certificate_backfill(tmp_path, monkeypatch, old_status):
    from pairper.explanation import cli
    calls = []

    class Workflow:
        def __init__(self, *args, **kwargs):
            pass

        def start(self, *args, **kwargs):
            return {'run_id': 'r', 'status': old_status, 'kind_pending_batches': 1}

        def step(self, run):
            calls.append('kind_only_step')
            return {'run_id': run, 'status': 'ready', 'kind_pending_batches': 0}

    assert cli.main(['p', '--library', str(tmp_path)], workflow_class=Workflow,
                    runner_class=lambda **kwargs: object()) == 0
    assert calls == ['kind_only_step']
