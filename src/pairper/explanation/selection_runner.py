"""Isolated execution for the staged v7 protocol, without modifying v6 workers."""
import json
from pathlib import Path

from .catalog import digest, normalize_unique_term_offsets
from .runner import ExplanationProtocolError, ExplanationRunner
from .selection_prompts import (
    PROMPT_VERSION,
    build_prompt,
    normalize_selection_ack,
    validate_protocol,
)


class SelectionRunner(ExplanationRunner):
    prompt_protocol_version = PROMPT_VERSION

    def __init__(self, *args, **kwargs):
        # Host feedback and its persistent structural budget own corrections. A
        # blind transport retry repeats the same rejected request without evidence.
        kwargs.setdefault('max_retries', 0)
        super().__init__(*args, **kwargs)

    def _build_prompt(self, request):
        if request['mode'] == 'kind_review':
            from .kind_review import build_prompt as kind_prompt
            return kind_prompt(request)
        return build_prompt(request)

    def _validate_protocol(self, request, response, *, image_count):
        if image_count:
            raise ExplanationProtocolError('Unbound images are not accepted', response)
        if request['mode'] == 'kind_review':
            from .kind_review import validate
            try:
                validate(request, response)
            except ValueError as exc:
                raise ExplanationProtocolError(str(exc), response) from exc
            return
        if request['mode'] == 'selection':
            before = digest(response)
            corrections = []
            for field in ('selected_terms', 'deferred_terms'):
                normalized, changes = normalize_unique_term_offsets(
                    {**request, 'stage': 'generate'}, {'items': response[field]})
                response[field] = normalized['items']
                corrections.extend(changes)
        elif request['mode'] == 'content_review':
            before = digest(response)
            normalized, corrections = normalize_unique_term_offsets(
                {**request, 'stage': 'generate'}, {'items': response['late_terms']})
            response['late_terms'] = normalized['items']
        else:
            before = digest(response)
            normalized, corrections = normalize_unique_term_offsets(request, response)
            response.clear()
            response.update(normalized)
        normalized, ack_correction = normalize_selection_ack(request, response)
        if ack_correction:
            response.clear()
            response.update(normalized)
            self.last_evidence['source_ack_normalization'] = ack_correction
        if corrections or ack_correction:
            directory = Path(self.last_evidence['directory'])
            path = directory / 'normalized-response.json'
            path.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding='utf-8')
            self.last_evidence['binding_normalization'] = {
                'raw_response_object_sha256': before, 'normalized_response_sha256': digest(response),
                'normalized_response_artifact': str(path), 'corrections': corrections,
                'provider_response_sha256_in_evidence_hashes_raw_bytes': True}
        try:
            validate_protocol(request, response)
        except ValueError as exc:
            raise ExplanationProtocolError(str(exc), response) from exc
