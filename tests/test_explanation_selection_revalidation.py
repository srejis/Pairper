"""Only redundant complete source acknowledgements may be normalized; source targets cannot."""
import copy
import json

import pytest
from test_explanation_selection import Runner, make

from pairper.explanation.catalog import digest
from pairper.explanation.selection_prompts import (
    SELECTION_SCHEMA,
    normalize_selection_ack,
    unit_key,
)


def retained(tmp_path):
    service, workflow, run = make(tmp_path)
    state = workflow._read(run)
    batch = state['batches'][0]
    packet = workflow._packet(service.bundle, batch['block_ids'], target_ids=batch['target_ids'])
    request = {'mode': 'selection', 'stage': 'generate', 'source_fingerprint': 'fp', 'packet': packet,
        'proposed_terms': [], 'inherited_issues': [],
        'required_checked_unit_keys': [unit_key(u) for u in packet['text_units']], 'validation_feedback': []}
    response = Runner().run(request, SELECTION_SCHEMA, [], tmp_path)
    response['checked_unit_keys'] *= 2
    directory = tmp_path / 'actual-call'
    (directory / 'attempt-01').mkdir(parents=True)
    for name, value in [('request.json', request), ('schema.json', SELECTION_SCHEMA),
                        ('attempt-01/last-message.json', response)]:
        (directory / name).write_text(json.dumps(value, ensure_ascii=False), encoding='utf8')
    batch.update(structural_repairs=5, semantic_repairs=3, structural_issues=['original duplicate rejection'],
                 status='needs_review')
    batch['evidence'].append({'mode': 'selection', 'status': 'protocol_rejected',
        'request_sha256': digest(request), 'response_sha256': digest(response),
        'transport': {'directory': str(directory), 'attempts': [{'status': 'failed', 'error_kind': 'protocol'}]}})
    workflow._save(state)
    return service, workflow, run, directory, request, response


def test_exact_complete_ack_dedup_records_changes_but_never_fills_missing_or_unknown(tmp_path):
    *_, request, response = retained(tmp_path)
    normalized, record = normalize_selection_ack(request, response)
    assert response['checked_unit_keys'] != normalized['checked_unit_keys']
    assert normalized['checked_unit_keys'] == request['required_checked_unit_keys']
    assert record['removed_ids'] == request['required_checked_unit_keys']
    assert record['before_count'] == 2 and record['after_count'] == 1
    for wrong in ([], ['unknown', 'unknown']):
        bad = {**response, 'checked_unit_keys': wrong}
        assert normalize_selection_ack(request, bad) == (bad, None)
    targets = copy.deepcopy(response)
    targets['selected_terms'] *= 2
    normalized, _ = normalize_selection_ack(request, targets)
    assert len(normalized['selected_terms']) == 2  # Never deduplicate scientific targets.


def test_retained_revalidation_preserves_all_budgets_and_raw_evidence_then_needs_science(tmp_path):
    _, workflow, run, directory, _, _ = retained(tmp_path)
    raw_before = (directory / 'attempt-01/last-message.json').read_bytes()
    calls = len(workflow.runner.calls)
    workflow.revalidate_selection(run, 0, directory)
    batch = workflow._read(run)['batches'][0]
    assert batch['semantic_repairs'] == 3 and batch['structural_repairs'] == 5
    assert batch['review'] is None and batch['status'] == 'generated'
    assert len(workflow.runner.calls) == calls
    assert (directory / 'attempt-01/last-message.json').read_bytes() == raw_before
    record = batch['selection_host_revalidation']
    assert record['science_review_granted'] is False and record['actual_model_call_repeated'] is False
    assert record['source_ack_normalization']['before_count'] == 2
    assert workflow.run(run)['status'] == 'ready'
    assert workflow._read(run)['batches'][0]['structural_repairs'] == 5


def test_changed_source_context_refuses_retained_response(tmp_path):
    service, workflow, run, directory, _, _ = retained(tmp_path)
    service.bundle['text_units'][0]['text'] += ' changed'
    with pytest.raises(ValueError, match='scope is stale'):
        workflow.revalidate_selection(run, 0, directory)
    assert workflow._read(run)['batches'][0]['manifest'] is None


def test_duplicate_target_in_retained_response_is_still_rejected(tmp_path):
    _, workflow, run, directory, request, response = retained(tmp_path)
    response['selected_terms'] *= 2
    (directory / 'attempt-01/last-message.json').write_text(json.dumps(response), encoding='utf8')
    state = workflow._read(run)
    state['batches'][0]['evidence'][-1]['response_sha256'] = digest(response)
    workflow._save(state)
    with pytest.raises(ValueError):
        workflow.revalidate_selection(run, 0, directory)
    assert workflow._read(run)['batches'][0]['manifest'] is None


def test_older_response_cannot_skip_a_later_retained_selection_rejection(tmp_path):
    _, workflow, run, directory, _, _ = retained(tmp_path)
    state = workflow._read(run)
    newer = copy.deepcopy(state['batches'][0]['evidence'][-1])
    newer['transport']['directory'] = str(tmp_path / 'newer-call')
    state['batches'][0]['evidence'].append(newer)
    workflow._save(state)
    with pytest.raises(ValueError, match='retained rejected selection'):
        workflow.revalidate_selection(run, 0, directory)


def test_repeated_quote_feedback_gives_all_exact_offsets_without_choosing_or_changing_data():
    from test_explanation_selection import term

    from pairper.explanation.selection_prompts import term_offset_issue
    packet = {'text_units': [{'block_id': 'b', 'field': 'runs', 'run_index': 0,
                              'text': 'MLP output MLP'}]}
    item = {'key': 'mlp', 'target': term(start=1)}
    before = copy.deepcopy(item)
    issue = term_offset_issue(packet, item)
    assert issue['target_key'] == 'mlp' and issue['source_unit']['run_index'] == 0
    assert issue['proposed_range'] == {'start': 1, 'end': 4}
    assert issue['exact_occurrences'] == [{'start': 0, 'end': 3}, {'start': 11, 'end': 14}]
    assert issue['host_selected_occurrence'] is False and item == before
    item['target']['block_id'] = 'missing'
    missing = term_offset_issue(packet, item)
    assert missing['matching_source_unit_count'] == 0 and missing['exact_occurrences'] == []


def test_invalid_ambiguous_quote_diagnostics_reach_next_real_feedback_and_keep_counter(tmp_path):
    class Ambiguous(Runner):
        first = True

        def run(self, request, schema, images, directory):
            result = super().run(request, schema, images, directory)
            if request['mode'] == 'selection' and self.first:
                self.first = False
                result['selected_terms'][0]['target'].update(start=1, end=4)
            return result

    service, workflow, run = make(tmp_path, Ambiguous())
    service.bundle['text_units'][0]['text'] = 'MLP output MLP'
    workflow.step(run)
    assert workflow._read(run)['batches'][0]['structural_repairs'] == 1
    assert workflow.run(run)['status'] == 'ready'
    second = [c for c in workflow.runner.calls if c['mode'] == 'selection'][1]
    issue = json.loads(second['validation_feedback'][0])
    assert issue['target_key'] == 'mlp' and len(issue['exact_occurrences']) == 2
    assert workflow._read(run)['batches'][0]['structural_repairs'] == 1


def test_retained_generic_rejection_enriches_next_request_without_rewriting_history(tmp_path):
    service, workflow, run, directory, request, response = retained(tmp_path)
    service.bundle['text_units'][0]['text'] = 'MLP output MLP'
    request['packet']['text_units'][0]['text'] = 'MLP output MLP'
    response['selected_terms'][0]['target'].update(start=1, end=4)
    for name, value in [('request.json', request), ('attempt-01/last-message.json', response)]:
        (directory / name).write_text(json.dumps(value), encoding='utf8')
    state = workflow._read(run)
    batch = state['batches'][0]
    batch['evidence'][-1].update(request_sha256=digest(request), response_sha256=digest(response))
    before = copy.deepcopy(batch)
    raw = (directory / 'attempt-01/last-message.json').read_bytes()
    issues = workflow._retained_selection_diagnostics(batch, request)
    assert issues[0]['exact_occurrences'] == [{'start': 0, 'end': 3}, {'start': 11, 'end': 14}]
    assert issues[0]['host_selected_occurrence'] is False
    assert batch == {**before, 'evidence': before['evidence'] + [batch['evidence'][-1]]}
    assert batch['structural_issues'] == ['original duplicate rejection']
    assert batch['structural_repairs'] == 5 and batch['semantic_repairs'] == 3
    assert (directory / 'attempt-01/last-message.json').read_bytes() == raw
    workflow._retained_selection_diagnostics(batch, request)
    assert len(batch['evidence']) == len(before['evidence']) + 1
    bad = copy.deepcopy(request)
    bad['packet']['text_units'][0]['text'] += ' changed'
    with pytest.raises(ValueError, match='stale or changed'):
        workflow._retained_selection_diagnostics(batch, bad)
    workflow._select(state, batch, service.bundle, request['packet'])
    assert workflow.runner.calls[-1]['retained_validation_diagnostics'] == issues
    assert workflow.runner.calls[-1]['validation_feedback'] == ['original duplicate rejection']
    batch['manifest'] = None
    batch['evidence'].append({**before['evidence'][-1], 'transport': {'directory': str(tmp_path / 'newer')}})
    with pytest.raises(FileNotFoundError):
        workflow._retained_selection_diagnostics(batch, request)
