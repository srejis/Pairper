"""Distant definitions reach both independent stages and cannot be forged at publication."""
import copy
import json

import pytest
from test_explanation_selection import Runner, make

from pairper.explanation.catalog import digest
from pairper.explanation.paper_context import (
    POLICY,
    PaperContextRunner,
    PaperContextWorkflow,
    grounded_schema,
    normalize_grounding_locations,
    validate_grounding,
)
from pairper.explanation.selection_prompts import CONTENT_REVIEW_SCHEMA, CONTENT_SCHEMA
from pairper.explanation.selection_prompts import validate_protocol as validate_selection_protocol


class GroundedRunner(Runner):
    def run(self, request, schema, images, directory):
        response = super().run(request, schema, images, directory)
        mode = request['mode']
        if mode in ('content_review', 'content_generate', 'content_repair'):
            corpus = request['packet']['paper_context']
            source = next(c for c in corpus if c['text'])
            items = request['candidate']['items'] if mode == 'content_review' else response['items']
            field = 'definition_checks' if mode == 'content_review' else 'definition_bindings'
            response[field] = [{'target_key': item['key'],
                'references': [{'block_id': source['block_id'], 'quote': source['text']}],
                'role': 'The source establishes the actual input and operation.',
                **({'verdict': 'supported'} if mode == 'content_review' else {})} for item in items]
        return response


def current(tmp_path, runner=None):
    service, old, _ = make(tmp_path / 'source')
    workflow = PaperContextWorkflow(service, runner or GroundedRunner(), directory=old.directory)
    return service, workflow, workflow.start('p')['run_id']


def test_distant_definitions_are_complete_bound_source_for_generation_and_review(tmp_path):
    service, workflow, run = current(tmp_path)
    local = service.bundle['context'][0]
    service.bundle['context'] = [{'block_id': f'other-{i}', 'text': 'Unrelated source', 'type': 'paragraph'}
                                for i in range(20)] + [local] + [
        {'block_id': f'later-{i}', 'text': '', 'type': 'paragraph'} for i in range(8)] + [
        {'block_id': 'remote-definition', 'text': 'The paper defines this normalization as x divided by its length.',
         'type': 'paragraph'}]
    packet = workflow._packet(service.bundle, ['b'])
    assert 'remote-definition' not in {c['block_id'] for c in packet['context']}
    assert packet['paper_context'] == service.bundle['context']
    assert packet['paper_context_binding']['context_sha256'] == digest(service.bundle['context'])
    # Updating source without changing the run's binding is not silently permitted.
    with pytest.raises(ValueError, match='paper-context'):
        workflow.step(run)
    service, workflow, run = current(tmp_path / 'normal')
    assert workflow.run(run)['status'] == 'ready'
    for request in workflow.runner.calls:
        if request['mode'].startswith('content_'):
            assert request['packet']['paper_context'] == service.bundle['context']
    workflow.commit(run)
    assert service.published
    assert workflow.report(run)['paper_context_checked_items'] == 2


@pytest.mark.parametrize('damage', ['quote', 'unknown_block', 'missing_key', 'duplicate_key', 'unsupported_pass'])
def test_grounding_rejects_forged_missing_or_contradictory_evidence(tmp_path, damage):
    _, workflow, run = current(tmp_path)
    workflow.run(run)
    state = workflow._read(run)
    batch = state['batches'][0]
    request = {'mode': 'content_review', 'packet': workflow._packet(workflow.service.bundle, batch['block_ids']),
               'candidate': batch['candidate']}
    response = copy.deepcopy(batch['review'])
    if damage == 'quote':
        response['definition_checks'][0]['references'][0]['quote'] = 'fabricated scientific definition'
    elif damage == 'unknown_block':
        response['definition_checks'][0]['references'][0]['block_id'] = 'outside-paper'
    elif damage == 'missing_key':
        response['definition_checks'].pop()
    elif damage == 'duplicate_key':
        response['definition_checks'][1] = response['definition_checks'][0]
    else:
        response['definition_checks'][0]['verdict'] = 'insufficient'
    with pytest.raises(ValueError):
        validate_grounding(request, response)


def test_definition_grounding_is_required_again_at_commit(tmp_path):
    _, workflow, run = current(tmp_path)
    workflow.run(run)
    state = workflow._read(run)
    state['batches'][0]['review'].pop('definition_checks')
    workflow._save(state)
    with pytest.raises(ValueError, match='every item'):
        workflow.commit(run)


@pytest.mark.parametrize('empty', [False, True])
def test_bad_citation_feedback_locates_literal_without_accepting_or_mutating_it(tmp_path, empty):
    _, workflow, run = current(tmp_path)
    workflow.run(run)
    batch = workflow._read(run)['batches'][0]
    bundle = copy.deepcopy(workflow.service.bundle)
    bundle['context'].append({'block_id': 'wrong-reference', 'text': 'An unrelated bibliography entry.',
                              'type': 'reference'})
    response = copy.deepcopy(batch['review'])
    ref = response['definition_checks'][0]['references'][0]
    correct_block = ref['block_id']
    ref['block_id'] = 'wrong-reference'
    if empty:
        ref['quote'] = ''
    before = copy.deepcopy(response)
    request = {'mode': 'content_review', 'packet': workflow._packet(bundle, batch['block_ids']),
               'candidate': batch['candidate']}
    with pytest.raises(ValueError, match='not an exact original-paper substring') as error:
        validate_grounding(request, response)
    assert 'An unrelated bibliography entry.' in str(error.value)
    assert f"locations in the complete paper (up to 12)={[] if empty else [correct_block]!r}" in str(error.value)
    assert response == before


def test_additional_alias_checks_must_be_explicit_and_cannot_replace_inherited_checks(tmp_path):
    _, workflow, run = current(tmp_path)
    workflow.run(run)
    batch = workflow._read(run)['batches'][0]
    request = {'mode': 'content_review', 'source_fingerprint': workflow.service.bundle['fingerprint'],
               'packet': workflow._packet(workflow.service.bundle, batch['block_ids']),
               'candidate': batch['candidate'], 'inherited_issues': [{'issue_id': 'required'}],
               'complete_alias_findings': [{'finding_id': 'alias', 'alias_anchor': {'mode': 'complete_occurrences'}}]}
    response = copy.deepcopy(batch['review'])
    response['inherited_issue_checks'] = [{'issue_id': key, 'verdict': 'resolved', 'reason': 'Checked supplied input.'}
                                         for key in ('required', 'alias')]
    validate_selection_protocol(request, response)
    for ids in [('alias',), ('required', 'unknown'), ('required', 'alias', 'alias')]:
        bad = copy.deepcopy(response)
        bad['inherited_issue_checks'] = [{'issue_id': key, 'verdict': 'resolved', 'reason': 'Checked.'} for key in ids]
        with pytest.raises(ValueError, match='known issue'):
            validate_selection_protocol(request, bad)
    response['inherited_issue_checks'][1]['verdict'] = 'still_present'
    with pytest.raises(ValueError, match='unresolved'):
        validate_selection_protocol(request, response)


@pytest.mark.parametrize('damage', ['none', 'raw', 'candidate', 'quote', 'repair'])
def test_retained_native_alias_reply_requires_all_original_bindings_and_preserves_budget(tmp_path, monkeypatch, damage):
    _, workflow, run = current(tmp_path)
    workflow.run(run)
    state = workflow._read(run)
    batch = state['batches'][0]
    aliases = [{'finding_id': 'alias', 'status': 'resolved', 'target_id': batch['candidate']['items'][0]['target']['id'],
                'alias_anchor': {'mode': 'complete_occurrences', 'translation_identity': {'revision': 1}}}]
    batch['external_content_findings'] = aliases
    request = {'mode': 'content_review', 'stage': 'review', 'source_fingerprint': workflow.service.bundle['fingerprint'],
               'packet': workflow._packet(workflow.service.bundle, batch['block_ids'],
                                          target_ids=batch['target_ids'], include_text=batch['include_text']),
               'candidate': batch['candidate'], 'manifest': batch['manifest'],
               'inherited_issues': batch.get('inherited_issues', []), 'complete_alias_findings': aliases}
    response = copy.deepcopy(batch['review'])
    response['inherited_issue_checks'].append({'issue_id': 'alias', 'verdict': 'resolved', 'reason': 'All occurrences checked.'})
    if damage == 'repair':
        response.update(verdict='repair', issues=[{'target_key': batch['candidate']['items'][0]['key'],
                                                 'code': 'incorrect_meaning', 'detail': 'An actual content defect remains.'}])
    if damage == 'quote':
        response['definition_checks'][0]['references'][0]['quote'] = 'invented source'
    folder = workflow.directory / run / 'batch-0' / 'call-retained'
    (folder / 'attempt-01').mkdir(parents=True)
    (folder / 'request.json').write_text(json.dumps(request), 'utf-8')
    raw_path = folder / 'attempt-01/last-message.json'
    raw_path.write_text(json.dumps(response), 'utf-8')
    batch['evidence'].append({'stage': 'review', 'status': 'protocol_rejected',
        'error': 'Content review forgot a known issue', 'request_sha256': digest(request),
        'response_sha256': digest(response), 'transport': {'directory': str(folder),
            'attempts': [{'attempt': 1, 'exit_code': 0}]}})
    batch.update(review=None, status='needs_review', structural_failure_mode='reviewing',
                 structural_issues=['Content review forgot a known issue'], structural_repairs=9)
    if damage == 'candidate':
        batch['candidate']['items'][0]['explanation']['category'] += 'changed'
    if damage == 'raw':
        raw_path.write_text('{}', 'utf-8')
    workflow._save(state)
    original = workflow._path(run).read_bytes()
    calls = len(workflow.runner.calls)
    monkeypatch.setattr(workflow, '_check_complete_aliases', lambda bundle, findings: None)
    if damage not in ('none', 'repair'):
        with pytest.raises(ValueError):
            workflow.revalidate_retained_alias_review(run, 0)
        assert workflow._path(run).read_bytes() == original
    else:
        workflow.revalidate_retained_alias_review(run, 0)
        saved = workflow._read(run)['batches'][0]
        assert saved['status'] == ('generated' if damage == 'repair' else 'reviewed') and saved['review'] == response
        assert saved['structural_repairs'] == 9
        record = next(e for e in saved['evidence'] if e['stage'] == 'retained_alias_review_host_revalidation')
        assert record['model_calls'] == record['content_changes'] == record['retry_allowance_granted'] == 0
        assert workflow._structural_host_revalidation_count(workflow._read(run), saved, workflow.service.bundle) == 9
        tampered = copy.deepcopy(saved)
        tampered['candidate']['items'][0]['explanation']['category'] += 'changed'
        assert workflow._structural_host_revalidation_count(workflow._read(run), tampered, workflow.service.bundle) is None
        if damage == 'repair':
            with pytest.raises(ValueError):
                workflow.commit(run)
    assert len(workflow.runner.calls) == calls


@pytest.mark.parametrize('case', ['unique', 'ambiguous', 'empty', 'invented'])
def test_location_normalization_changes_only_unique_verbatim_block_locator(tmp_path, case):
    _, workflow, run = current(tmp_path)
    workflow.run(run)
    batch = workflow._read(run)['batches'][0]
    request = {'mode': 'content_review', 'packet': workflow._packet(workflow.service.bundle, batch['block_ids']),
               'candidate': batch['candidate']}
    response = copy.deepcopy(batch['review'])
    ref = response['definition_checks'][0]['references'][0]
    original_block = ref['block_id']
    ref['block_id'] = 'wrong-locator'
    if case == 'ambiguous':
        request['packet']['paper_context'] = [*request['packet']['paper_context'],
            {'block_id': 'same-quote', 'text': ref['quote']}]
    elif case in ('empty', 'invented'):
        ref['quote'] = '' if case == 'empty' else 'This quote does not occur anywhere.'
    before = copy.deepcopy(response)
    normalized, corrections = normalize_grounding_locations(request, response)
    assert response == before
    if case == 'unique':
        assert len(corrections) == 1
        assert normalized['definition_checks'][0]['references'][0]['block_id'] == original_block
        normalized['definition_checks'][0]['references'][0]['block_id'] = 'wrong-locator'
        assert normalized == before
    else:
        assert not corrections and normalized == before


def test_native_grounding_normalization_retains_separate_hashed_artifact(tmp_path):
    _, workflow, run = current(tmp_path)
    workflow.run(run)
    batch = workflow._read(run)['batches'][0]
    request = {'mode': 'content_review', 'source_fingerprint': workflow.service.bundle['fingerprint'],
               'packet': workflow._packet(workflow.service.bundle, batch['block_ids']),
               'candidate': batch['candidate'], 'inherited_issues': batch.get('inherited_issues', [])}
    response = copy.deepcopy(batch['review'])
    ref = response['definition_checks'][0]['references'][0]
    correct = ref['block_id']
    ref['block_id'] = 'wrong-locator'
    raw = copy.deepcopy(response)
    runner = PaperContextRunner.__new__(PaperContextRunner)
    runner.last_evidence = {'directory': str(tmp_path)}
    runner._validate_protocol(request, response, image_count=0)
    proof = runner.last_evidence['grounding_location_normalization']
    assert proof['raw_response_object_sha256'] == digest(raw)
    assert proof['normalized_response_sha256'] == digest(response)
    assert response['definition_checks'][0]['references'][0]['block_id'] == correct
    assert json.loads((tmp_path / 'grounding-normalized-response.json').read_text('utf-8')) == response


@pytest.mark.parametrize('damage', ['none', 'raw', 'candidate', 'invented'])
def test_retained_generation_uses_normal_merge_and_requires_fresh_science(tmp_path, damage):
    _, workflow, run = current(tmp_path)
    workflow.run(run)
    state = workflow._read(run)
    batch = state['batches'][0]
    targets = [batch['candidate']['items'][0]['target']]
    request = {'mode': 'content_repair', 'stage': 'repair', 'source_fingerprint': state['source_fingerprint'],
               'packet': workflow._packet(workflow.service.bundle, batch['block_ids'],
                                          target_ids=batch['target_ids'], include_text=batch['include_text']),
               'manifest': batch['manifest'], 'generation_targets': targets, 'candidate': batch['candidate'],
               'review': batch['review'], 'external_content_findings': [], 'explicit_content_experiments': [],
               'kind_findings': [], 'scoped_symbol_repair': None, 'validation_feedback': []}
    raw = GroundedRunner().run(request, None, [], tmp_path)
    ref = raw['definition_bindings'][0]['references'][0]
    ref['block_id'] = 'wrong-locator'
    if damage == 'invented':
        ref['quote'] = 'An invented definition.'
    folder = workflow.directory / run / 'batch-0' / 'call-retained-generation'
    (folder / 'attempt-01').mkdir(parents=True)
    (folder / 'request.json').write_text(json.dumps(request), 'utf-8')
    raw_path = folder / 'attempt-01/last-message.json'
    raw_path.write_text(json.dumps(raw), 'utf-8')
    batch['evidence'].append({'stage': 'repair', 'status': 'protocol_rejected',
        'request_sha256': digest(request), 'response_sha256': digest(raw),
        'transport': {'directory': str(folder), 'attempts': [{'attempt': 1, 'exit_code': 0}]}})
    batch.update(status='needs_review', structural_repairs=9, structural_failure_mode='repairing',
                 structural_issues=['Definition citation is not an exact original-paper substring: wrong locator'])
    if damage == 'candidate':
        batch['candidate']['items'][0]['explanation']['category'] += 'changed'
    if damage == 'raw':
        raw_path.write_text('{}', 'utf-8')
    workflow._save(state)
    original = workflow._path(run).read_bytes()
    if damage != 'none':
        with pytest.raises(ValueError):
            workflow.revalidate_retained_generation_locations(run, 0)
        assert workflow._path(run).read_bytes() == original
    else:
        workflow.revalidate_retained_generation_locations(run, 0)
        saved = workflow._read(run)['batches'][0]
        assert saved['status'] == 'generated' and saved['review'] is None
        assert saved['structural_repairs'] == 9
        assert saved['generation_protocol_host_revalidation']['model_calls'] == 0
        with pytest.raises(ValueError):
            workflow.commit(run)
        workflow.runner = GroundedRunner()
        workflow.step(run)
        assert workflow._read(run)['batches'][0]['status'] == 'reviewed'
        assert workflow._read(run)['batches'][0]['structural_repairs'] == 9


def test_prior_publication_keeps_bytes_counts_scope_but_retires_all_approvals(tmp_path):
    service, old, old_run = make(tmp_path)
    old.run(old_run)
    old.commit(old_run)
    state = old._read(old_run)
    state['batches'][0]['semantic_repairs'] = 5
    old._save(state)
    original = old._path(old_run).read_bytes()
    workflow = PaperContextWorkflow(service, GroundedRunner(), directory=old.directory, max_repairs=2)
    new_run = workflow.start('p', adopt_from_run_id=old_run)['run_id']
    new = workflow._read(new_run)
    assert new_run != old_run and old._path(old_run).read_bytes() == original
    assert new['batches'][0]['candidate'] == state['batches'][0]['candidate']
    assert new['batches'][0]['manifest'] == state['batches'][0]['manifest']
    assert new['batches'][0]['semantic_repairs'] == 5
    assert workflow._repair_limit(new, new['batches'][0], 'semantic') == 7
    assert new['batches'][0]['review'] is None and 'kind_certificate' not in new['batches'][0]
    assert workflow.run(new_run)['status'] == 'ready'
    assert old._path(old_run).read_bytes() == original
    assert workflow.start('p', adopt_from_run_id=old_run)['run_id'] == new_run
    workflow.commit(new_run)


def test_unresolved_prior_run_cannot_get_a_new_context_budget(tmp_path):
    service, old, old_run = make(tmp_path)
    workflow = PaperContextWorkflow(service, GroundedRunner(), directory=old.directory)
    with pytest.raises(ValueError, match='completely reviewed'):
        workflow.start('p', adopt_from_run_id=old_run)


def test_old_review_cannot_be_published_or_run_as_whole_paper_review(tmp_path):
    service, old, old_run = make(tmp_path)
    old.run(old_run)
    workflow = PaperContextWorkflow(service, GroundedRunner(), directory=old.directory)
    assert workflow.report(old_run)['paper_context_revalidation_required']
    with pytest.raises(ValueError, match='paper-context'):
        workflow.commit(old_run)
    with pytest.raises(ValueError, match='paper-context'):
        workflow.step(old_run)


def test_native_prompt_and_output_contract_require_paper_specific_definitions():
    runner = PaperContextRunner.__new__(PaperContextRunner)
    request = {'mode': 'content_generate'}
    prompt = runner._build_prompt(request)
    assert 'EVERY source block' in prompt
    assert "paper's explicit definitions" in prompt
    assert 'definition_bindings' in grounded_schema(CONTENT_SCHEMA, 'content_generate')['required']
    assert 'definition_checks' in grounded_schema(CONTENT_REVIEW_SCHEMA, 'content_review')['required']
    assert POLICY in runner.prompt_protocol_version
