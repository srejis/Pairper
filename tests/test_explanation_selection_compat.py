"""Legacy approvals survive selection-only changes, never changed evidence/content."""
import hashlib
import json
from copy import deepcopy

import pytest

from pairper.explanation.catalog import digest
from pairper.explanation.prompts import REVIEW_SCHEMA
from pairper.explanation.selection_compat import compatible_v6_pass


def hash_text(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def legacy(tmp_path):
    math = {'id': 'math', 'kind': 'math', 'source_text': 'x', 'block_id': 'p', 'field': 'runs',
            'run_index': 1, 'start': None, 'end': None}
    term = {'id': 'term', 'kind': 'term', 'source_text': 'MLP', 'block_id': 'p', 'field': 'runs',
            'run_index': 0, 'start': 0, 'end': 3, 'concept_key': 'mlp', 'aliases': ['다층 퍼셉트론']}
    packet = {'targets': [math], 'text_units': [{'block_id': 'p', 'field': 'runs',
              'run_index': 0, 'text': 'MLP input'}], 'translation_identity': {'revision': 4},
              'context': [{'text': 'grounded source'}], 'math_role_hints': {'math': 'symbol'},
              'translation_surfaces': [{'text': '다층 퍼셉트론 입력'}]}
    candidate = {'source_fingerprint': 'source', 'items': [
        {'key': 'm', 'target': math, 'explanation': {'meaning': 'input'}},
        {'key': 't', 'target': term, 'explanation': {'description': 'neural network'}}]}
    inherited = [{'issue_id': 'issue', 'issue': {'code': 'missing_term'}}]
    request = {'stage': 'review', 'source_fingerprint': 'source', 'packet': packet,
               'candidate': candidate, 'inherited_issues': inherited}
    response = {'source_fingerprint': 'source', 'verdict': 'pass', 'checked_keys': ['m', 't'],
                'issues': [], 'inherited_issue_checks': [{'issue_id': 'issue',
                'verdict': 'resolved', 'reason': 'Term now included.'}]}
    (tmp_path / 'attempt-01').mkdir()
    raw = json.dumps(response)
    prompt = 'v6 full-scope source and candidate review'
    files = {'request.json': request, 'response.json': response, 'schema.json': REVIEW_SCHEMA}
    for name, value in files.items():
        (tmp_path / name).write_text(json.dumps(value), encoding='utf-8')
    (tmp_path / 'attempt-01/last-message.json').write_text(raw, encoding='utf-8')
    (tmp_path / 'prompt.txt').write_text(prompt, encoding='utf-8')
    event = {'stage': 'review', 'request_sha256': digest(request), 'response_sha256': digest(response),
             'host_binding_corrections': [], 'transport': {'directory': str(tmp_path),
             'status': 'success', 'prompt_protocol_version': 'pairper-explanation-v6',
             'source_request_sha256': hash_text(json.dumps(request, ensure_ascii=False, sort_keys=True)),
             'provider_prompt_sha256': hash_text(prompt), 'response_sha256': hash_text(raw),
             'attempts': [{'attempt': 1, 'status': 'success', 'exit_code': 0}]}}
    batch = {'status': 'reviewed', 'candidate': candidate, 'review': response,
             'review_candidate_sha256': digest(candidate), 'review_context_sha256': digest(packet),
             'review_translation_identity': packet['translation_identity'],
             'inherited_issues': inherited, 'evidence': [event]}
    return batch, packet, tmp_path


def approve(batch, packet, validator=lambda candidate: candidate):
    return compatible_v6_pass(batch, 'source', packet, validator, content_contract_unchanged=True)


def test_v6_pass_reuses_original_proof_without_mutation_or_fake_new_review(legacy):
    batch, packet, _ = legacy
    original = deepcopy((batch, packet))
    proof = approve(batch, packet)
    assert proof['selection_origin'] == 'legacy_v6_fullscope_pass'
    assert proof['candidate_sha256'] == batch['review_candidate_sha256']
    assert proof['manifest_targets'] == [batch['candidate']['items'][1]['target']]
    assert proof['manifest_sha256'] == digest(proof['manifest_targets'])
    assert proof['selection_scope']['math_target_ids'] == ['math']
    assert proof['selection_scope']['text_units'] == [{'block_id': 'p', 'field': 'runs', 'run_index': 0}]
    assert 'reviewed_at' not in proof and 'prompt_version' not in proof
    assert (batch, packet) == original
    proof['manifest_targets'][0]['aliases'].append('must not mutate input')
    assert (batch, packet) == original
    assert compatible_v6_pass(batch, 'source', packet, lambda c: c) is None


@pytest.mark.parametrize('change', [
    lambda b, p: b.update(status='repair_pending'),
    lambda b, p: b['review'].update(verdict='repair'),
    lambda b, p: b['review'].update(issues=[{'target_key': 't', 'code': 'wrong', 'detail': 'wrong'}]),
    lambda b, p: b['review'].update(checked_keys=['m', 'm']),
    lambda b, p: b['candidate']['items'][1]['explanation'].update(description='changed'),
    lambda b, p: b['candidate']['items'].append(deepcopy(b['candidate']['items'][0])),
    lambda b, p: b.update(review_context_sha256='different'),
    lambda b, p: b.update(review_candidate_sha256='different'),
    lambda b, p: b['review_translation_identity'].update(revision=5),
    lambda b, p: p['context'][0].update(text='new source context'),
    lambda b, p: p['math_role_hints'].update(math='formula'),
    lambda b, p: p['translation_surfaces'][0].update(text='changed Korean'),
    lambda b, p: b['evidence'].append({'stage': 'review', 'status': 'protocol_rejected'}),
    lambda b, p: b['evidence'][0]['transport'].update(prompt_protocol_version='pairper-explanation-v5'),
    lambda b, p: b['evidence'][0]['transport'].update(status='failed'),
    lambda b, p: b['evidence'][0]['transport'].update(directory='relative'),
    lambda b, p: b['evidence'][0].update(request_sha256='bad'),
    lambda b, p: b['evidence'][0].update(response_sha256='bad'),
    lambda b, p: b['evidence'][0]['transport'].update(source_request_sha256='bad'),
    lambda b, p: b['evidence'][0]['transport'].update(provider_prompt_sha256='bad'),
    lambda b, p: b['evidence'][0]['transport'].update(response_sha256='bad'),
])
def test_changed_or_failed_legacy_pass_cannot_be_carried(legacy, change):
    batch, packet, _ = legacy
    change(batch, packet)
    assert approve(batch, packet) is None


@pytest.mark.parametrize('filename', ['request.json', 'response.json', 'schema.json',
                                     'prompt.txt', 'attempt-01/last-message.json'])
def test_missing_actual_evidence_fails_closed(legacy, filename):
    batch, packet, directory = legacy
    (directory / filename).unlink()
    assert approve(batch, packet) is None


def rewrite_evidence(batch, directory, *, request=None, response=None):
    """Keep transport hashes valid to exercise semantic invariants independently."""
    event = batch['evidence'][0]
    if request is not None:
        (directory / 'request.json').write_text(json.dumps(request), encoding='utf-8')
        event['request_sha256'] = digest(request)
        event['transport']['source_request_sha256'] = hash_text(
            json.dumps(request, ensure_ascii=False, sort_keys=True))
    if response is not None:
        raw = json.dumps(response)
        (directory / 'response.json').write_text(raw, encoding='utf-8')
        (directory / 'attempt-01/last-message.json').write_text(raw, encoding='utf-8')
        event['response_sha256'] = digest(response)
        event['transport']['response_sha256'] = hash_text(raw)


@pytest.mark.parametrize('change', [
    lambda b: b['review']['inherited_issue_checks'][0].update(verdict='still_present'),
    lambda b: b['review'].update(inherited_issue_checks=[]),
    lambda b: b['review']['inherited_issue_checks'][0].update(reason=''),
    lambda b: b['review'].update(checked_keys=['m']),
    lambda b: b['candidate']['items'].pop(0),
    lambda b: b['candidate']['items'][1]['target'].update(block_id='foreign'),
    lambda b: b['candidate']['items'][1]['target'].update(start=1),
])
def test_semantic_invariants_even_when_stored_transport_hashes_match(legacy, change):
    batch, packet, directory = legacy
    change(batch)
    batch['review_candidate_sha256'] = digest(batch['candidate'])
    request = json.loads((directory / 'request.json').read_text('utf-8'))
    request['candidate'] = batch['candidate']
    rewrite_evidence(batch, directory, request=request, response=batch['review'])
    assert approve(batch, packet) is None


def test_current_validator_must_pass_without_rewriting_the_approved_candidate(legacy):
    batch, packet, _ = legacy

    def mutate(candidate):
        candidate['items'][0]['explanation']['meaning'] = 'rewritten'
        return candidate

    def reject(candidate):
        raise ValueError('Current validator rejects stale content')

    assert approve(batch, packet, mutate) is None
    assert approve(batch, packet, lambda candidate: {'different': 'result'}) is None
    assert approve(batch, packet, reject) is None


def test_changed_saved_schema_is_not_a_compatible_v6_review(legacy):
    batch, packet, directory = legacy
    (directory / 'schema.json').write_text('{}', encoding='utf-8')
    assert approve(batch, packet) is None


def test_pending_inherited_issue_not_in_actual_review_request_blocks_reuse(legacy):
    batch, packet, _ = legacy
    batch['inherited_issues'].append({'issue_id': 'new-unchecked', 'issue': {'code': 'wrong'}})
    assert approve(batch, packet) is None


def test_math_only_legacy_pass_carries_only_its_actual_scope(legacy):
    batch, packet, directory = legacy
    packet['text_units'] = []
    batch['candidate']['items'].pop(1)
    batch['review'].update(checked_keys=['m'], inherited_issue_checks=[])
    batch['inherited_issues'] = []
    batch['review_candidate_sha256'] = digest(batch['candidate'])
    batch['review_context_sha256'] = digest(packet)
    request = {'stage': 'review', 'source_fingerprint': 'source', 'packet': packet,
               'candidate': batch['candidate'], 'inherited_issues': []}
    rewrite_evidence(batch, directory, request=request, response=batch['review'])
    proof = approve(batch, packet)
    assert proof['manifest_targets'] == []
    assert proof['selection_scope'] == {'text_units': [], 'math_target_ids': ['math']}
