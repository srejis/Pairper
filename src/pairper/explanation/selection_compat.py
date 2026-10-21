"""Narrow evidence-backed reuse of a v6 full-scope pass, never a new review.

Only the selection protocol may change. Scientific/content requirements must be
identical; a caller must explicitly attest that compatibility. A failed, stale,
unverifiable, or subsequently changed batch needs fresh selection/content review.
"""
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import jsonschema

from .catalog import digest
from .prompts import REVIEW_SCHEMA as V6_REVIEW_SCHEMA


def _text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def compatible_v6_pass(batch, source_fingerprint, current_packet, validate_candidate,
                       *, content_contract_unchanged=False, artifact_resolver=None):
    """Return immutable legacy proof, or None. No input/file/store mutation.

    current_packet is the exact source/Korean packet, before adding v7 selection
    metadata. validate_candidate must validate a deep copy without changing it.
    The caller additionally verifies the original run is v6, freezes this manifest,
    and retains whole-paper raw math coverage checks at commit.
    """
    if content_contract_unchanged is not True:
        return None
    try:
        return _verify(batch, source_fingerprint, current_packet, validate_candidate, artifact_resolver)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError,
            jsonschema.ValidationError, jsonschema.SchemaError):
        return None


def _verify(batch, fingerprint, packet, validator, resolver):
    if batch['status'] != 'reviewed' or not batch['evidence']:
        return None
    # A later failure/rewrite must never be hidden by searching backwards for pass.
    event = batch['evidence'][-1]
    transport = event['transport']
    if (event['stage'] != 'review' or event.get('status') not in (None, 'success')
            or transport['status'] != 'success'
            or transport['prompt_protocol_version'] != 'pairper-explanation-v6'
            or event.get('host_binding_corrections')):
        return None
    directory = resolver(transport['directory']) if resolver else Path(transport['directory'])
    if not directory.is_absolute():
        return None
    request_text = (directory / 'request.json').read_text(encoding='utf-8')
    response_text = (directory / 'response.json').read_text(encoding='utf-8')
    prompt_text = (directory / 'prompt.txt').read_text(encoding='utf-8')
    schema = json.loads((directory / 'schema.json').read_text(encoding='utf-8'))
    if schema != V6_REVIEW_SCHEMA:
        return None
    request, response = json.loads(request_text), json.loads(response_text)
    attempts = transport['attempts']
    last = attempts[-1]
    if (last['status'] != 'success' or last['exit_code'] != 0
            or type(last['attempt']) is not int or last['attempt'] != len(attempts)):
        return None
    raw = (directory / f"attempt-{last['attempt']:02}" / 'last-message.json').read_text(encoding='utf-8')
    if (digest(request) != event['request_sha256']
            or digest(response) != event['response_sha256']
            or digest(json.loads(raw)) != digest(response)
            or _text_hash(raw) != transport['response_sha256']
            or _text_hash(prompt_text) != transport['provider_prompt_sha256']
            or _text_hash(json.dumps(request, ensure_ascii=False, sort_keys=True))
            != transport['source_request_sha256']):
        return None
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(response)
    candidate = batch['candidate']
    candidate_hash = digest(candidate)
    if (request['stage'] != 'review' or request['source_fingerprint'] != fingerprint
            or response['source_fingerprint'] != fingerprint
            or candidate['source_fingerprint'] != fingerprint
            or request['candidate'] != candidate
            or request['packet'] != packet
            or batch['review'] != response
            or batch['review_candidate_sha256'] != candidate_hash
            or batch['review_context_sha256'] != digest(packet)
            or batch['review_translation_identity'] != packet.get('translation_identity')
            or response['verdict'] != 'pass' or response['issues'] != []):
        return None
    keys = [item['key'] for item in candidate['items']]
    checked = response['checked_keys']
    if (any(not isinstance(key, str) or not key.strip() for key in keys)
            or len(set(keys)) != len(keys) or len(set(checked)) != len(checked)
            or set(checked) != set(keys)):
        return None
    inherited = request['inherited_issues']
    if batch.get('inherited_issues', []) != inherited:
        return None
    issue_ids = [issue['issue_id'] for issue in inherited]
    checks = response['inherited_issue_checks']
    if (len(set(issue_ids)) != len(issue_ids)
            or len(checks) != len(issue_ids)
            or {check['issue_id'] for check in checks} != set(issue_ids)
            or any(check['verdict'] not in ('resolved', 'not_applicable')
                   or not check['reason'].strip() for check in checks)):
        return None
    expected_math = [t['id'] for t in packet['targets']]
    actual_math = [item['target']['id'] for item in candidate['items']
                   if item['target']['kind'] == 'math']
    if (any(t['kind'] != 'math' for t in packet['targets'])
            or len(set(expected_math)) != len(expected_math)
            or len(actual_math) != len(expected_math) or set(actual_math) != set(expected_math)):
        return None
    original_math = {t['id']: t for t in packet['targets']}
    units = {(u['block_id'], u['field'], u['run_index']): u for u in packet['text_units']}
    terms = []
    for item in candidate['items']:
        target = item['target']
        if target['kind'] == 'math':
            if any(target.get(k) != v for k, v in original_math[target['id']].items()):
                return None
        elif target['kind'] == 'term':
            unit = units[(target['block_id'], target['field'], target['run_index'])]
            start, end = target['start'], target['end']
            if (type(start) is not int or type(end) is not int
                    or not 0 <= start < end <= len(unit['text'])
                    or unit['text'][start:end] != target['source_text']):
                return None
            terms.append(deepcopy(target))
        else:
            return None
    copy = deepcopy(candidate)
    validated = validator(copy)
    if copy != candidate or validated is not None and validated != candidate:
        return None
    return {
        'selection_origin': 'legacy_v6_fullscope_pass',
        'original_protocol': 'pairper-explanation-v6',
        'source_fingerprint': fingerprint,
        'candidate_sha256': candidate_hash,
        'request_sha256': event['request_sha256'],
        'response_sha256': event['response_sha256'],
        'review_context_sha256': digest(packet),
        'translation_identity': deepcopy(packet.get('translation_identity')),
        'original_evidence_dir': transport['directory'],
        'provider_prompt_sha256': transport['provider_prompt_sha256'],
        'raw_response_sha256': transport['response_sha256'],
        'manifest_targets': terms,
        'manifest_sha256': digest(terms),
        'selection_scope': {
            'text_units': [{k: unit[k] for k in ('block_id', 'field', 'run_index')}
                           for unit in packet['text_units']],
            'math_target_ids': list(expected_math),
        },
        'checked_keys': list(checked),
    }
