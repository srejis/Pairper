"""Explanation prompts on the existing isolated, no-tools Codex execution transport."""
import json
from pathlib import Path

from pairper.translation.agent_runner import CodexRunner, RunnerError

from .catalog import digest, normalize_unique_term_offsets
from .prompts import PROMPT_VERSION, build_prompt, validate_protocol


class ExplanationProtocolError(RunnerError):
    """Carry schema-valid rejected output only for source-grounded repair, never publication."""
    def __init__(self, message, response):
        super().__init__(message, kind='protocol')
        self.response = response


class ExplanationRunner(CodexRunner):
    prompt_protocol_version = PROMPT_VERSION

    def _build_prompt(self, request: dict) -> str:
        return build_prompt(request)

    def _validate_protocol(self, request: dict, response: dict, *, image_count: int) -> None:
        normalized, corrections = normalize_unique_term_offsets(request, response)
        if corrections:
            raw_response_digest = digest(response)
            response.clear()
            response.update(normalized)
            directory = Path(self.last_evidence['directory'])
            normalized_path = directory / 'normalized-response.json'
            normalized_path.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding='utf-8')
            self.last_evidence['binding_normalization'] = {
                'raw_response_object_sha256': raw_response_digest,
                'normalized_response_sha256': digest(response),
                'raw_provider_artifact': str(directory / f"attempt-{len(self.last_evidence['attempts'])+1:02}"
                                             / 'last-message.json'),
                'normalized_response_artifact': str(normalized_path),
                'corrections': corrections,
                'provider_response_sha256_in_evidence_hashes_raw_bytes': True}
        try:
            validate_protocol(request, response, image_count=image_count)
        except ValueError as exc:
            raise ExplanationProtocolError(str(exc), response) from exc
