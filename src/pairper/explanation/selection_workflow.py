"""Staged v7: independently freeze selection, then review content without open-ended rewriting."""
from __future__ import annotations

import ast
from copy import deepcopy
from json import loads
from pathlib import Path

from .catalog import digest, normalize_unique_term_offsets, validate_targets
from .runner import ExplanationProtocolError
from .selection_compat import compatible_v6_pass
from .selection_prompts import (
    CONTENT_REVIEW_SCHEMA,
    CONTENT_SCHEMA,
    PROMPT_VERSION,
    SELECTION_SCHEMA,
    normalize_selection_ack,
    term_offset_issue,
    unit_key,
    validate_protocol,
)
from .workflow import ExplanationWorkflow, _exclusive, batch_plan


def _spec(batch):
    return {k: batch.get(k) for k in ('block_ids', 'target_ids', 'include_text')}


def _manifest_hash(manifest):
    return digest({k: v for k, v in manifest.items() if k not in ('fingerprint', 'proof')})


class SelectionWorkflow(ExplanationWorkflow):
    """The original v6 class remains available to the already-running workers."""

    def _repair_limit(self, state, batch, kind):
        return state['max_repairs']

    def _structural_host_revalidation_count(self, state, batch, bundle):
        return (batch.get('selection_host_revalidation') or {}).get('structural_repairs_at_revalidation')

    def _fresh(self, state):
        module = ast.parse(Path(__file__).with_name('selection_prompts.py').read_text(encoding='utf-8'))
        version = next(n.value.value for n in module.body if isinstance(n, ast.Assign)
                       and isinstance(n.value, ast.Constant)
                       and any(isinstance(t, ast.Name) and t.id == 'PROMPT_VERSION' for t in n.targets))
        if state.get('prompt_version') != version:
            raise ValueError('Selection protocol changed; migrate the retained candidate before resuming')
        bundle = self.service.prepare(state['paper_id'])
        if bundle['fingerprint'] != state['source_fingerprint']:
            state['status'] = 'source_changed'
            self._save(state)
            raise ValueError('Source changed; no stale selection or content can be published')
        if state.get('supplemental_math_sha256') != (bundle.get('supplemental_math') or {}).get('sha256'):
            raise ValueError('Supplemental inline formula inventory changed; fresh selection migration required')
        return bundle

    def _packet(self, bundle, ids, *, target_ids=None, include_text=True):
        raw_ids = {t['id'] for t in bundle['targets']}
        if target_ids and set(target_ids) - raw_ids:
            from .text_math import all_math_targets
            bundle = {**bundle, 'targets': all_math_targets(bundle)}
        return super()._packet(bundle, ids, target_ids=target_ids, include_text=include_text)

    def _validate_candidate(self, bundle, candidate, *, require_current=True):
        from .text_math import all_math_targets
        return super()._validate_candidate({**bundle, 'targets': all_math_targets(bundle)}, candidate,
                                          require_current=require_current)

    def start(self, paper_id, *, experiment_id='default', max_characters=6500, adopt_from_run_id=None):
        bundle = self.service.prepare(paper_id)
        run_id = 'exw_' + digest([paper_id, experiment_id, bundle['fingerprint'], max_characters, PROMPT_VERSION])[:24]
        if self._path(run_id).exists():
            return self.report(run_id)
        state = {'run_id': run_id, 'paper_id': paper_id, 'experiment_id': experiment_id,
                 'source_fingerprint': bundle['fingerprint'], 'prompt_version': PROMPT_VERSION,
                 'supplemental_math_sha256': (bundle.get('supplemental_math') or {}).get('sha256'),
                 'status': 'pending', 'max_repairs': self.max_repairs, 'error': None,
                 'batches': [{**spec, 'status': 'pending', 'repairs': 0, 'semantic_repairs': 0,
                              'structural_repairs': 0, 'candidate': None, 'review': None,
                              'evidence': [], 'manifest': None, 'supplements': []}
                             for spec in batch_plan(bundle, max_characters)]}
        if adopt_from_run_id:
            self._adopt(state, bundle, adopt_from_run_id)
            # Adoption itself obtains the old run's OS lock. The caller retires live v6
            # workers first; check its immutable digest again before carrying any pass.
            old = self._read(adopt_from_run_id)
            if digest(old) != state['adoption']['source_run_sha256']:
                raise ValueError('Origin workflow changed while preparing selection migration')
            old_batches = {digest(_spec(b)): b for b in old['batches']}
            for batch in state['batches']:
                previous = old_batches[digest(_spec(batch))]
                for key in ('external_content_findings', 'rejected_content_experiments', 'external_reopened_pass',
                            'external_reopened_pass_history'):
                    if key in previous:
                        batch[key] = deepcopy(previous[key])
                if old.get('prompt_version') != 'pairper-explanation-v6' or previous['status'] != 'reviewed':
                    continue
                packet = self._packet(bundle, batch['block_ids'], target_ids=batch['target_ids'],
                                      include_text=batch['include_text'])
                proof = compatible_v6_pass(previous, bundle['fingerprint'], packet,
                    lambda c: self._validate_candidate(bundle, c), content_contract_unchanged=True,
                    artifact_resolver=self._artifact_directory)
                if proof is not None:
                    terms = [i['target'] for i in batch['candidate']['items'] if i['target']['kind'] == 'term']
                    batch['manifest'] = self._manifest(bundle, packet, terms, {},
                        origin='legacy_v6_fullscope_pass', proof=proof)
                    batch['status'], batch['review'] = 'reviewed', deepcopy(previous['review'])
                    for key in ('review_candidate_sha256', 'review_translation_identity', 'review_context_sha256'):
                        batch[key] = previous[key]
                    batch['compatible_v6_proof'] = proof
        # Keep the complete legacy raw plan intact. Newly discovered text-range
        # comparisons receive their own unreviewed mandatory-math batches.
        supplemental = (bundle.get('supplemental_math') or {}).get('targets', [])
        for offset in range(0, len(supplemental), 16):
            targets = supplemental[offset:offset + 16]
            state['batches'].append({'block_ids': list(dict.fromkeys(t['block_id'] for t in targets)),
                'target_ids': [t['id'] for t in targets], 'include_text': False,
                'status': 'pending', 'repairs': 0, 'semantic_repairs': 0, 'structural_repairs': 0,
                'candidate': None, 'review': None, 'evidence': [], 'manifest': None, 'supplements': [],
                'supplemental_math_sha256': state['supplemental_math_sha256']})
        self._save(state)
        return self.report(run_id)

    def _manifest(self, bundle, packet, terms, reasons, *, origin, proof=None):
        targets = [dict(t, aliases=[], concept_key='') for t in packet['targets']] + terms
        scope = {k: packet[k] for k in ('paper_id', 'math_role_hints', 'translation_identity')}
        value = {'source_fingerprint': bundle['fingerprint'], 'targets': targets, 'reasons': reasons,
                 'selection_origin': origin, 'selection_scope': scope}
        value['fingerprint'] = digest(value)
        if proof is not None:
            value['proof'] = proof
        return value

    def record_content_finding(self, run_id, target_id, *, field, observed_value, reason,
                               criterion, expected_candidate_sha256, alias_proposal=None):
        """Reopen a proven pass for one externally observed defect, never reset a rejection."""
        if field not in ('category', 'target.aliases', 'process', 'term_referent') or not reason.strip() or not criterion.strip():
            raise ValueError('A narrow content finding needs the original criterion and a concrete reason')
        with _exclusive(self.directory / 'external-findings.lock'), _exclusive(self._path(run_id).with_suffix('.lock')):
            state = self._read(run_id)
            bundle = self._fresh(state)
            batch = next((b for b in state['batches'] if any(
                i['target']['id'] == target_id for i in (b.get('candidate') or {}).get('items', []))), None)
            if batch is None or (state['status'] == 'published' and field != 'target.aliases'):
                raise ValueError('Finding requires an existing unpublished reviewed candidate')
            alias_anchor = None
            if field == 'target.aliases':
                from .alias_review import anchor
                if not alias_proposal:
                    raise ValueError('Alias correction needs an exact current translated-literal proposal')
                target = next(i['target'] for i in batch['candidate']['items'] if i['target']['id'] == target_id)
                alias_anchor = anchor(self.service, bundle, target, alias_proposal['literal'],
                                      alias_proposal['expected_translation_identity'],
                                      complete_occurrences=alias_proposal.get('complete_occurrences'))
            code = ('alias_not_literal' if alias_anchor else
                    'incomplete_math_process' if field == 'process' else
                    'incorrect_term_referent' if field == 'term_referent' else 'category_not_superordinate')
            if field in ('process', 'term_referent') and self._repair_counts(batch)['semantic'] >= self._repair_limit(state, batch, 'semantic'):
                raise ValueError('Process finding requires remaining ordinary semantic repair budget')
            finding_scope = [bundle['fingerprint'], target_id, field, code]
            if alias_anchor:
                finding_scope.append(alias_anchor['translation_identity'])
            finding_id = digest(finding_scope)
            for path in self.directory.glob('exw_*.json'):
                if path == self._path(run_id):
                    continue
                historical = loads(path.read_text(encoding='utf-8'))
                if any(f['finding_id'] == finding_id for b in historical['batches']
                       for f in b.get('external_content_findings', [])):
                    raise ValueError('The same external finding was already recorded in another run')
            if any(f['finding_id'] == finding_id for f in batch.get('external_content_findings', [])):
                raise ValueError('The same external finding cannot grant another repair allowance')
            if any(f['status'] == 'rejected' for f in batch.get('external_content_findings', [])):
                raise ValueError('A failed external content repair cannot be bypassed with a new finding')
            pass_record = batch.get('external_reopened_pass')
            if pass_record and not any(f['status'] == 'open' for f in batch.get('external_content_findings', [])):
                batch.setdefault('external_reopened_pass_history', []).append(pass_record)
                pass_record = None
            if pass_record is None:
                focused_observation = any(
                    f['target_id'] == target_id and f['observed_value'] == observed_value
                    for f in batch.get('kind_pending_findings', []))
                if ((batch['status'] != 'reviewed' and not focused_observation)
                        or (batch.get('review') or {}).get('verdict') != 'pass'
                        or batch['review'].get('issues')
                        or batch.get('review_candidate_sha256') != digest(batch['candidate'])):
                    raise ValueError('Only an actual candidate-bound prior pass can receive this allowance')
                packet = self._packet(bundle, batch['block_ids'], target_ids=batch['target_ids'],
                                      include_text=batch['include_text'])
                if batch.get('review_context_sha256') != digest(packet):
                    raise ValueError('External finding cannot reopen a stale review context')
                if (_manifest_hash(batch['manifest']) != batch['manifest']['fingerprint']
                        or not batch.get('compatible_v6_proof') and
                        batch.get('review_manifest_sha256') != batch['manifest']['fingerprint']):
                    raise ValueError('External finding requires the unchanged reviewed selection manifest')
                pass_record = {k: deepcopy(batch.get(k)) for k in (
                    'review', 'review_candidate_sha256', 'review_context_sha256',
                    'review_translation_identity', 'review_manifest_sha256', 'compatible_v6_proof')}
                pass_record['review_sha256'] = digest(batch['review'])
                batch['external_reopened_pass'] = pass_record
            if (expected_candidate_sha256 != digest(batch['candidate'])
                    or expected_candidate_sha256 != pass_record['review_candidate_sha256']):
                raise ValueError('External finding must bind the exact originally approved candidate')
            item = next(i for i in batch['candidate']['items'] if i['target']['id'] == target_id)
            actual_value = (self._term_referent(item) if field == 'term_referent' else
                            item['target']['aliases'] if field == 'target.aliases' else item['explanation'][field])
            eligible = (item['target']['kind'] == 'math' and item['explanation']['math_role'] == 'formula'
                        if field == 'process' else item['target']['kind'] == 'term')
            if not eligible or actual_value != observed_value:
                raise ValueError('External finding does not match the actual observed eligible field')
            finding = {'finding_id': finding_id, 'source_fingerprint': bundle['fingerprint'],
                'source_target': deepcopy(item['target']), 'target_id': target_id, 'field': field,
                'observed_value': observed_value, 'reason': reason, 'criterion': criterion,
                'code': code, 'prior_pass_sha256': pass_record['review_sha256'],
                'prior_candidate_sha256': expected_candidate_sha256, 'allowance': 1, 'used': 0, 'status': 'open'}
            if field in ('process', 'term_referent'):
                finding['budget_policy'] = 'ordinary_semantic_limit_only'
                finding['repair_mode'] = ('term_referent_manifest_amendment' if field == 'term_referent'
                                          else 'insert_one_explanation_group')
                if batch.get('kind_certificate'):
                    batch.setdefault('retired_kind_certificates', []).append(batch.pop('kind_certificate'))
            if alias_anchor:
                if alias_anchor['literal'] in actual_value:
                    raise ValueError('The proposed exact alias is already present')
                finding['alias_anchor'] = alias_anchor
            batch.setdefault('external_content_findings', []).append(finding)
            batch.setdefault('inherited_issues', []).append({'issue_id': finding_id, 'from_run_id': run_id,
                'source_target': deepcopy(item['target']), 'classification': 'content',
                'disposition': 'carry_to_content', 'external_finding': True,
                'issue': {'target_key': target_id, 'code': finding['code'],
                    'detail': f'{reason} Original criterion: {criterion}. Observed category: {observed_value}'}})
            if batch.get('compatible_v6_proof'):
                batch['historical_v6_proof'] = batch.pop('compatible_v6_proof')
            batch['review'], batch['status'], state['status'] = None, 'generated', 'pending'
            batch['evidence'].append({'stage': 'external_content_finding', 'finding': deepcopy(finding)})
            self._save(state)
        return {'run_id': run_id, 'finding_id': finding_id, 'target_id': target_id, 'status': 'recorded'}

    @staticmethod
    def _term_referent(item):
        return {'category': item['explanation']['category'],
                'description': deepcopy(item['explanation']['description']),
                'concept_key': item['target']['concept_key']}

    def record_alias_finding(self, run_id, target_id, *, literal, expected_translation_identity,
                             reason, expected_candidate_sha256, complete_occurrences=None):
        """Record a source/pass-bound exact alias proposal, preserving published history."""
        state = self._read(run_id)
        item = next(i for b in state['batches'] for i in (b.get('candidate') or {}).get('items', [])
                    if i['target']['id'] == target_id)
        return self.record_content_finding(run_id, target_id, field='target.aliases',
            observed_value=item['target']['aliases'], reason=reason,
            criterion='Faithful names of the same source concept must literally match current Korean source-unit provenance',
            expected_candidate_sha256=expected_candidate_sha256,
            alias_proposal={'literal': literal, 'expected_translation_identity': expected_translation_identity,
                            'complete_occurrences': complete_occurrences})

    def audit_repair_budgets(self, run_id):
        """Persist evidence-derived increases only; retain the configured general limit."""
        with _exclusive(self._path(run_id).with_suffix('.lock')):
            state = self._read(run_id)
            self._fresh(state)
            changes = []
            for number, batch in enumerate(state['batches']):
                before = {k: batch.get(k + '_repairs', 0) for k in ('semantic', 'structural')}
                after = self._repair_counts(batch)
                if before != after:
                    changes.append({'batch': number, 'before': before, 'after': after})
            state.setdefault('repair_budget_audits', []).append({'changes': changes,
                'rule': 'unique durable ancestral requests; preserve all old counters and rejection history'})
            self._save(state)
        return {'run_id': run_id, 'changes': changes}

    def record_rejected_content_experiment(self, run_id, target_id, *, reason,
                                           expected_candidate_sha256, expected_review_sha256):
        """Explicitly allocate one process-only experiment on the exact current rejection."""
        if not reason.strip():
            raise ValueError('An explicitly approved experiment needs its evidence-based reason')
        with _exclusive(self.directory / 'external-findings.lock'), _exclusive(self._path(run_id).with_suffix('.lock')):
            state = self._read(run_id)
            bundle = self._fresh(state)
            batch = next((b for b in state['batches'] if any(i['target']['id'] == target_id
                         for i in (b.get('candidate') or {}).get('items', []))), None)
            if batch is None:
                raise ValueError('Unknown rejected source target')
            review = batch.get('review') or {}
            if (review.get('verdict') != 'repair' or not review.get('issues')
                    or digest(review) != expected_review_sha256
                    or digest(batch['candidate']) != expected_candidate_sha256
                    or batch.get('review_candidate_sha256') != expected_candidate_sha256):
                raise ValueError('Experiment must bind the exact currently rejected candidate and review')
            item = next(i for i in batch['candidate']['items'] if i['target']['id'] == target_id)
            if item['target']['kind'] != 'math' or any(i['target_key'] != item['key']
                    or i['code'] != 'incomplete_math_process' for i in review['issues']):
                raise ValueError('This experiment may only address the identified process rejection, not other defects')
            packet = self._packet(bundle, batch['block_ids'], target_ids=batch['target_ids'],
                                  include_text=batch['include_text'])
            if (batch.get('review_context_sha256') != digest(packet)
                    or batch.get('review_manifest_sha256') != batch['manifest']['fingerprint']
                    or _manifest_hash(batch['manifest']) != batch['manifest']['fingerprint']):
                raise ValueError('Rejected experiment source context or selection changed')
            eid = digest([bundle['fingerprint'], target_id, 'process', 'incomplete_math_process'])
            for path in self.directory.glob('exw_*.json'):
                historical = loads(path.read_text(encoding='utf-8'))
                if any(e['finding_id'] == eid for b in historical['batches']
                       for e in b.get('rejected_content_experiments', [])):
                    raise ValueError('The same rejected experiment cannot receive another allowance')
            counts = self._repair_counts(batch)
            experiment = {'finding_id': eid, 'kind': 'explicit_rejected_content_experiment',
                'source_fingerprint': bundle['fingerprint'], 'source_target': deepcopy(item['target']),
                'target_id': target_id, 'field': 'process', 'observed_value': deepcopy(item['explanation']['process']),
                'code': 'incomplete_math_process', 'reason': reason,
                'prior_rejection_sha256': expected_review_sha256,
                'prior_candidate_sha256': expected_candidate_sha256, 'counts_before': counts,
                'general_limit_unchanged': state['max_repairs'], 'allowance': 1, 'used': 0, 'status': 'open'}
            batch.setdefault('rejected_content_experiments', []).append(experiment)
            batch['inherited_issues'] = self._inherited_issues(batch, run_id)
            batch['evidence'].append({'stage': 'explicit_rejected_content_experiment',
                                      'experiment': deepcopy(experiment)})
            batch['status'], state['status'] = 'generated', 'pending'
            self._save(state)
        return {'run_id': run_id, 'experiment_id': eid, 'counts_before': counts, 'status': 'recorded'}

    def _call(self, state, batch, request, schema):
        import jsonschema
        directory = self.directory / state['run_id'] / ('batch-' + str(state['batches'].index(batch)))
        directory.mkdir(parents=True, exist_ok=True)
        try:
            response = self.runner.run(request, schema, [], directory)
            jsonschema.Draft202012Validator(schema).validate(response)
            before = digest(response)
            corrections = []
            if request['mode'] == 'selection':
                for field in ('selected_terms', 'deferred_terms'):
                    normalized, changes = normalize_unique_term_offsets({**request, 'stage': 'generate'},
                                                                         {'items': response[field]})
                    response[field] = normalized['items']
                    corrections.extend(changes)
            elif request['mode'] == 'content_review':
                normalized, corrections = normalize_unique_term_offsets({**request, 'stage': 'generate'},
                                                                         {'items': response['late_terms']})
                response['late_terms'] = normalized['items']
            response, ack_correction = normalize_selection_ack(request, response)
            if request['mode'] == 'kind_review':
                from .kind_review import validate
                validate(request, response)
            else:
                validate_protocol(request, response)
        except (ValueError, jsonschema.ValidationError, ExplanationProtocolError) as exc:
            if not isinstance(exc, ExplanationProtocolError):
                exc = ExplanationProtocolError(str(exc), locals().get('response', {}))
            batch['evidence'].append({'stage': request['stage'], 'mode': request['mode'],
                'status': 'protocol_rejected', 'error': str(exc), 'request_sha256': digest(request),
                'response_sha256': digest(exc.response), 'transport': getattr(self.runner, 'last_evidence', None)})
            raise exc
        batch['evidence'].append({'stage': request['stage'], 'mode': request['mode'],
            'request_sha256': digest(request), 'response_sha256': digest(response),
            'pre_host_response_sha256': before, 'host_binding_corrections': corrections,
            'source_ack_normalization': ack_correction,
            'transport': getattr(self.runner, 'last_evidence', None)})
        return response

    def _retained_selection_diagnostics(self, batch, request):
        """Enrich old generic errors from only the latest unchanged actual response."""
        if not batch.get('structural_issues'):
            return []
        event = next((e for e in reversed(batch['evidence'])
                      if e.get('mode') == 'selection' and e.get('transport')), None)
        if event is None or not event['transport'].get('directory'):
            return []
        directory = self._artifact_directory(event['transport']['directory'])
        previous = loads((directory / 'request.json').read_text(encoding='utf-8'))
        fields = ('mode', 'stage', 'source_fingerprint', 'packet', 'proposed_terms',
                  'inherited_issues', 'required_checked_unit_keys')
        if (digest(previous) != event['request_sha256']
                or any(previous.get(k) != request.get(k) for k in fields)
                or batch.get('manifest') is not None
                or loads((directory / 'schema.json').read_text(encoding='utf-8')) != SELECTION_SCHEMA):
            raise ValueError('Retained selection diagnostic source/context/schema is stale or changed')
        attempt = len(event['transport'].get('attempts') or [])
        raw = (directory / f'attempt-{attempt:02}' / 'last-message.json').read_bytes()
        import hashlib
        raw_sha = hashlib.sha256(raw).hexdigest()
        if event['transport'].get('response_sha256') not in (None, raw_sha):
            raise ValueError('Retained selection diagnostic raw response bytes changed')
        response = loads(raw.decode('utf-8'))
        normalization = event['transport'].get('binding_normalization') or {}
        expected = normalization.get('raw_response_object_sha256', event['response_sha256'])
        if digest(response) != expected:
            raise ValueError('Retained selection diagnostic response differs from recorded evidence')
        import jsonschema
        jsonschema.Draft202012Validator(SELECTION_SCHEMA).validate(response)
        normalized, _ = normalize_unique_term_offsets(previous, {'items': response['selected_terms']})
        issues = [issue for item in normalized['items']
                  if (issue := term_offset_issue(request['packet'], item))]
        if issues:
            proof = {'stage': 'retained_selection_diagnostics', 'guard_version': 'exact-offset-feedback-v1',
                     'request_sha256': digest(previous), 'raw_response_object_sha256': digest(response),
                     'raw_response_sha256': raw_sha,
                     'original_evidence_directory': str(directory), 'diagnostics': issues,
                     'candidate_changed': False, 'science_review_granted': False,
                     'repair_budget_changed': False}
            if not any(e == proof for e in batch['evidence']):
                batch['evidence'].append(proof)
        return issues

    def _select(self, state, batch, bundle, packet, *, retained_response=None):
        if not batch['include_text']:
            batch['manifest'] = self._manifest(bundle, packet, [], {}, origin='mandatory_math_catalog')
            return
        proposed = [i['target'] for i in (batch.get('candidate') or {}).get('items', [])
                    if i['target']['kind'] == 'term']
        request = {'mode': 'selection', 'stage': 'generate', 'source_fingerprint': bundle['fingerprint'],
                   'packet': packet, 'proposed_terms': proposed,
                   'inherited_issues': batch.get('inherited_issues', []),
                   'required_checked_unit_keys': [unit_key(u) for u in packet['text_units']],
                   'validation_feedback': batch.get('structural_issues', [])}
        if retained_response is None:
            request['retained_validation_diagnostics'] = self._retained_selection_diagnostics(batch, request)
            response = self._call(state, batch, request, SELECTION_SCHEMA)
        else:
            response = retained_response
            validate_protocol(request, response)
        for item in response['selected_terms']:
            issue = term_offset_issue(packet, item)
            if issue:
                import json
                raise ValueError(json.dumps(issue, ensure_ascii=False))
        incoming = [dict(i['target']) for i in response['selected_terms']]
        for target in incoming:
            if target.get('id') is None:
                target.pop('id', None)
            if target['kind'] != 'term' or not target.get('concept_key'):
                raise ValueError('Selection must contain source-bound technical terms with concept keys')
        terms = validate_targets(bundle, incoming)
        self._term_scope(packet, terms)
        ids = {t['id'] for t in terms}
        for decision in response['existing_decisions']:
            if (decision['decision'] == 'retain') != (decision['target_id'] in ids):
                raise ValueError('Selection decision disagrees with the frozen target inventory')
        reasons = {t['id']: i['reason'] for t, i in zip(terms, response['selected_terms'], strict=True)}
        batch['manifest'] = self._manifest(bundle, packet, terms, reasons, origin='independent_selection_review')
        batch['selection_review'] = response
        decisions = {x['issue_id']: x for x in response['known_issue_decisions']}
        for issue in batch.get('inherited_issues', []):
            decision = decisions[issue['issue_id']]
            issue.update(classification=decision['classification'], disposition=decision['disposition'],
                         selection_reason=decision['reason'])
        self._supplements(bundle, batch, response['deferred_terms'], packet, kind='deferred_selection')

    def revalidate_selection(self, run_id, batch_index, evidence_directory):
        """Recheck a stored actual selection response after a narrow host syntax fix."""
        import hashlib

        import jsonschema
        with _exclusive(self._path(run_id).with_suffix('.lock')):
            state = self._read(run_id)
            bundle = self._fresh(state)
            batch = state['batches'][batch_index]
            directory = self._artifact_directory(evidence_directory)
            event = next((e for e in reversed(batch['evidence']) if e.get('mode') == 'selection'
                          and e.get('transport')), None)
            if (event is None or self._artifact_directory(event['transport']['directory']) != directory
                    or batch.get('manifest') is not None):
                raise ValueError('A retained rejected selection call in this unresolved batch is required')
            request = loads((directory / 'request.json').read_text(encoding='utf-8'))
            packet = self._packet(bundle, batch['block_ids'], target_ids=batch['target_ids'],
                                  include_text=batch['include_text'])
            if (digest(request) != event['request_sha256'] or request['packet'] != packet
                    or request['source_fingerprint'] != bundle['fingerprint']
                    or request['required_checked_unit_keys'] != [unit_key(u) for u in packet['text_units']]
                    or request['proposed_terms'] != [i['target'] for i in (batch.get('candidate') or {}).get('items', [])
                                                      if i['target']['kind'] == 'term']
                    or request['inherited_issues'] != batch.get('inherited_issues', [])
                    or loads((directory / 'schema.json').read_text(encoding='utf-8')) != SELECTION_SCHEMA):
                raise ValueError('Retained selection request/source/Korean/schema scope is stale or changed')
            attempt = len(event['transport'].get('attempts') or [])
            raw_path = directory / f'attempt-{attempt:02}' / 'last-message.json'
            raw = raw_path.read_bytes()
            response = loads(raw.decode('utf-8'))
            norm = event['transport'].get('binding_normalization') or {}
            if digest(response) != norm.get('raw_response_object_sha256', event['response_sha256']):
                raise ValueError('Retained provider response differs from original recorded evidence')
            original_digest = digest(response)
            corrections = []
            for field in ('selected_terms', 'deferred_terms'):
                normalized, changes = normalize_unique_term_offsets(request, {'items': response[field]})
                response[field] = normalized['items']
                corrections.extend(changes)
            response, ack = normalize_selection_ack(request, response)
            jsonschema.Draft202012Validator(SELECTION_SCHEMA).validate(response)
            validate_protocol(request, response)
            self._select(state, batch, bundle, packet, retained_response=response)
            record = {'stage': 'selection_host_revalidation', 'actual_model_call_repeated': False,
                'science_review_granted': False, 'original_evidence_directory': str(directory),
                'original_request_sha256': digest(request), 'raw_provider_bytes_sha256': hashlib.sha256(raw).hexdigest(),
                'raw_response_object_sha256': original_digest, 'normalized_response_sha256': digest(response),
                'source_fingerprint': bundle['fingerprint'], 'current_packet_sha256': digest(packet),
                'translation_identity': packet['translation_identity'], 'offset_corrections': corrections,
                'source_ack_normalization': ack, 'structural_repairs_at_revalidation': batch['structural_repairs'],
                'guard_version': 'source-bound-selection-host-revalidation-v1'}
            artifact = directory / ('host-revalidated-selection-' + digest(record)[:16] + '.json')
            artifact.write_text(__import__('json').dumps({'record': record, 'response': response},
                                                      ensure_ascii=False, indent=2), encoding='utf-8')
            record['artifact'] = str(artifact)
            batch['selection_host_revalidation'] = record
            batch['manifest']['selection_origin'] = 'retained_selection_host_revalidation'
            batch['manifest']['fingerprint'] = _manifest_hash(batch['manifest'])
            batch['structural_issues'] = []
            batch['status'], state['status'], state['error'] = 'generated', 'pending', None
            batch['evidence'].append(record)
            self._save(state)
        return self.report(run_id)

    def _term_scope(self, packet, targets):
        units = {(u['block_id'], u['field'], u['run_index']) for u in packet['text_units']}
        if any((t['block_id'], t['field'], t['run_index']) not in units for t in targets):
            raise ValueError('Selected/supplementary term lies outside the independently reviewed source chunk')

    def _supplements(self, bundle, batch, terms, packet, *, kind):
        selected = {t['id'] for t in batch['manifest']['targets']}
        known = {i['issue_id'] for i in batch.get('inherited_issues', [])}
        for term in terms:
            target = dict(term['target'])
            if target.get('id') is None:
                target.pop('id', None)
            target = validate_targets(bundle, [target])[0]
            self._term_scope(packet, [target])
            if target['kind'] != 'term' or not target.get('concept_key') or target['id'] in selected:
                raise ValueError('Supplement must be an actual technical term outside the frozen scope')
            if set(term['inherited_issue_ids']) - known:
                raise ValueError('Supplement names an unknown prior issue')
            record = {'kind': kind, 'target': target, 'reason': term['reason'],
                'inherited_issue_ids': term['inherited_issue_ids'], 'status': 'recorded_for_next_selection'}
            if record not in batch['supplements']:
                batch['supplements'].append(record)

    def _assemble(self, bundle, batch):
        selected = {t['id']: t for t in batch['manifest']['targets']}
        prior = {i['target']['id']: i for i in (batch.get('candidate') or {}).get('items', [])}
        items = []
        for tid, target in selected.items():
            if tid in prior:
                item = deepcopy(prior[tid])
                item['target'] = deepcopy(target)
                item['key'] = tid
                items.append(item)
        batch['candidate'] = {'source_fingerprint': bundle['fingerprint'], 'items': items}
        return [t for tid, t in selected.items() if tid not in prior]

    def _generate_subset(self, state, batch, bundle, packet, targets, *, repair=False):
        batch['pending_targets'] = targets
        request = {'mode': 'content_repair' if repair else 'content_generate',
            'stage': 'repair' if repair else 'generate', 'source_fingerprint': bundle['fingerprint'],
            'packet': packet, 'manifest': batch['manifest'], 'generation_targets': targets,
            'candidate': batch['candidate'], 'review': batch.get('review'),
            'external_content_findings': [deepcopy(f) for f in batch.get('external_content_findings', [])
                                         if (f['status'] == 'open' or f.get('budget_policy') == 'ordinary_semantic_limit_only'
                                             and f['status'] == 'rejected')
                                         and f['target_id'] in {t['id'] for t in targets}],
            'explicit_content_experiments': [f for f in batch.get('rejected_content_experiments', [])
                                         if f['status'] == 'open' and f['target_id'] in {t['id'] for t in targets}],
            'kind_findings': batch.get('kind_pending_findings', []),
            'scoped_symbol_repair': batch.get('scoped_symbol_repair'),
            'validation_feedback': batch.get('structural_issues', [])}
        for finding in request['external_content_findings']:
            if finding.get('budget_policy') == 'ordinary_semantic_limit_only':
                finding['current_host_repair_contract'] = finding['repair_mode']
        result = self._call(state, batch, request, CONTENT_SCHEMA)
        try:
            generated = self._validate_candidate(bundle, result)
        except (ValueError, KeyError, StopIteration) as exc:
            raise ExplanationProtocolError(str(exc), result) from exc
        replacements = {i['target']['id']: i for i in generated['items']}
        existing = {i['target']['id']: i for i in batch['candidate']['items']}
        if request['scoped_symbol_repair']:
            scope = request['scoped_symbol_repair']
            if (scope['candidate_sha256'] != digest(batch['candidate'])
                    or scope['review_sha256'] != digest(batch.get('review'))
                    or set(replacements) != set(scope['target_ids'])):
                raise ExplanationProtocolError('Scoped symbol repair no longer matches the actual rejected candidate/review', result)
            for tid, item in replacements.items():
                before, after = existing[tid], item
                old_symbols, new_symbols = before['explanation']['symbols'], after['explanation']['symbols']
                if (before['target'] != after['target'] or len(new_symbols) <= len(old_symbols)
                        or new_symbols[:len(old_symbols)] != old_symbols
                        or {k: v for k, v in before['explanation'].items() if k != 'symbols'} !=
                           {k: v for k, v in after['explanation'].items() if k != 'symbols'}):
                    raise ExplanationProtocolError('Scoped symbol repair may only append missing symbol rows', result)
        for finding in request['external_content_findings'] + request['explicit_content_experiments'] + request['kind_findings']:
            tid = finding['target_id']
            if finding['field'] == 'term_referent':
                before, after = existing[tid], replacements[tid]
                value = self._term_referent(after)
                if (after['target']['kind'] != 'term' or value == self._term_referent(before)
                        or not value['category'].strip() or not value['description']
                        or not value['concept_key'].strip()
                        or value['concept_key'] == finding['observed_value']['concept_key']
                        or value['category'] == finding['observed_value']['category']
                        or value['description'] == finding['observed_value']['description']
                        or {k: v for k, v in before['target'].items() if k != 'concept_key'} !=
                           {k: v for k, v in after['target'].items() if k != 'concept_key'}
                        or {k: v for k, v in before['explanation'].items() if k not in ('category', 'description')} !=
                           {k: v for k, v in after['explanation'].items() if k not in ('category', 'description')}):
                    raise ExplanationProtocolError('Term referent repair may change only category, description and its distinct concept key', result)
                continue
            if finding['field'] == 'target.aliases':
                from .alias_review import anchor
                proof = finding['alias_anchor']
                try:
                    current_anchor = anchor(self.service, bundle, existing[tid]['target'], proof['literal'],
                                            proof['translation_identity'], complete_occurrences=(
                                                proof if proof.get('mode') == 'complete_occurrences' else None))
                except ValueError as exc:
                    raise ExplanationProtocolError(f'Alias literal provenance changed during repair: {exc}', result) from exc
                if current_anchor != proof:
                    raise ExplanationProtocolError('Alias literal provenance changed during repair', result)
                old_target, new_target = existing[tid]['target'], replacements[tid]['target']
                expected_aliases = list(dict.fromkeys(old_target['aliases'] + [proof['literal']]))
                if (new_target['aliases'] != expected_aliases
                        or {k: v for k, v in old_target.items() if k != 'aliases'} !=
                           {k: v for k, v in new_target.items() if k != 'aliases'}
                        or existing[tid]['explanation'] != replacements[tid]['explanation']):
                    raise ExplanationProtocolError('Alias-only repair changed explanation, source/concept, or unrelated aliases', result)
                continue
            if existing[tid]['target'] != replacements[tid]['target']:
                raise ExplanationProtocolError('Narrow content repair changed an immutable target binding', result)
            old = existing[tid]['explanation']
            new = replacements[tid]['explanation']
            value = new[finding['field']]
            if finding.get('budget_policy') == 'ordinary_semantic_limit_only':
                original = finding['observed_value']
                if value == old['process'] or not self._one_process_group(original, value):
                    raise ExplanationProtocolError('Process finding may only insert one contiguous explanation group; existing steps stay exact', result)
            if value == finding['observed_value'] or not value or isinstance(value, str) and not value.strip():
                raise ExplanationProtocolError('Known incorrect explanation field was not corrected', result)
            if {k: v for k, v in old.items() if k != finding['field']} != {
                    k: v for k, v in new.items() if k != finding['field']}:
                raise ExplanationProtocolError('Narrow content repair rewrote an unrelated explanation field', result)
        if request['kind_findings']:
            batch['kind_repair_inflight'] = True
            batch.pop('kind_pending_findings', None)
        existing.update(replacements)
        amendments = [f for f in request['external_content_findings'] if f['field'] == 'term_referent']
        if amendments:
            original = deepcopy(batch['manifest'])
            changed = {f['target_id'] for f in amendments}
            for target in batch['manifest']['targets']:
                if target['id'] in changed:
                    target['concept_key'] = replacements[target['id']]['target']['concept_key']
            batch['manifest']['fingerprint'] = _manifest_hash(batch['manifest'])
            amendment = {'stage': 'term_referent_manifest_amendment',
                'original_manifest': original, 'amended_manifest_sha256': batch['manifest']['fingerprint'],
                'source_fingerprint': bundle['fingerprint'], 'context_sha256': digest(packet),
                'translation_identity': packet['translation_identity'],
                'prior_candidate_sha256': digest(batch['candidate']),
                'finding_ids': [f['finding_id'] for f in amendments],
                'target_ids': sorted(changed), 'approval_pending_full_science_and_kind': True}
            batch.setdefault('manifest_amendments', []).append(amendment)
            batch['evidence'].append(deepcopy(amendment))
            batch['assembled_manifest'] = batch['manifest']['fingerprint']
        batch['candidate']['items'] = [dict(existing[t['id']], key=t['id'])
                                       for t in batch['manifest']['targets'] if t['id'] in existing]
        batch['structural_issues'] = []
        batch['pending_targets'] = []

    def _certify_kind(self, state, batch, bundle):
        from .kind_review import binding, certified, request_for, schema_for
        if certified(bundle, batch):
            return
        if _manifest_hash(batch['manifest']) != batch['manifest']['fingerprint']:
            raise ValueError('Frozen selection manifest was changed')
        self._validate_candidate(bundle, deepcopy(batch['candidate']))
        packet = self._packet(bundle, batch['block_ids'], target_ids=batch['target_ids'],
                              include_text=batch['include_text'])
        if (batch['status'] != 'reviewed' or (batch.get('review') or {}).get('verdict') != 'pass'
                or batch['review'].get('issues')
                or batch.get('review_candidate_sha256') != digest(batch['candidate'])
                or batch.get('review_context_sha256') != digest(packet)
                or batch.get('review_translation_identity') != packet['translation_identity']):
            raise ValueError('KIND certification requires the unchanged current scientific pass')
        request = request_for(bundle, batch)
        certificate = {'binding': binding(bundle, batch, request)}
        if not request['input']['cases']:
            certificate['status'] = 'empty_term_scope'
        else:
            state['status'] = 'reviewing_kinds'
            self._save(state)
            try:
                response = self._call(state, batch, request, schema_for(request))
            except Exception as exc:
                batch['structural_repairs'] += 1
                batch['kind_review_error'] = str(exc)
                batch['status'] = 'needs_review' if batch['structural_repairs'] > self._repair_limit(state, batch, 'structural') else 'reviewed'
                state['status'] = 'needs_review' if batch['status'] == 'needs_review' else 'pending'
                state['error'] = str(exc)
                self._save(state)
                return
            batch.pop('kind_review_error', None)
            current_bundle = self._fresh(state)
            if request_for(current_bundle, batch) != request:
                raise ValueError('Term/context input changed during KIND review')
            certificate.update(status='pass' if all(d['verdict'] == 'pass' for d in response['decisions'])
                               else 'repair', response=response, response_sha256=digest(response))
            rejected = [d for d in response['decisions'] if d['verdict'] == 'reject']
            if rejected:
                existing = {i['target']['id']: i for i in batch['candidate']['items']}
                batch['kind_pending_findings'] = [{'target_id': d['target_id'], 'field': 'category',
                    'observed_value': existing[d['target_id']]['explanation']['category'],
                    'reason': d['reason'], 'criterion': 'Original category-first KIND rule',
                    'source_target': deepcopy(existing[d['target_id']]['target'])} for d in rejected]
                for finding in batch['kind_pending_findings']:
                    issue_id = digest([bundle['fingerprint'], finding['target_id'], 'category', 'focused_kind'])
                    if not any(i['issue_id'] == issue_id for i in batch.get('inherited_issues', [])):
                        batch.setdefault('inherited_issues', []).append({'issue_id': issue_id,
                            'from_run_id': state['run_id'], 'source_target': finding['source_target'],
                            'classification': 'content', 'disposition': 'carry_to_content',
                            'issue': {'target_key': finding['target_id'], 'code': 'category_not_superordinate',
                                      'detail': finding['reason']}})
                batch['status'] = 'generated'
        if batch.get('kind_certificate'):
            batch.setdefault('kind_certificate_history', []).append(batch['kind_certificate'])
        batch['kind_certificate'] = certificate
        batch['evidence'].append({'stage': 'focused_kind_certificate', 'certificate': deepcopy(certificate)})
        state['status'] = 'pending'
        self._save(state)

    def report(self, run_id):
        result = super().report(run_id)
        from .kind_review import certified
        state = self._read(run_id)
        bundle = self.service.prepare(state['paper_id'])
        if bundle['fingerprint'] != state['source_fingerprint']:
            result.update(kind_certified_batches=0, kind_pending_batches=0, kind_rejections=[])
            return result
        result['kind_certified_batches'] = sum(certified(bundle, b) for b in state['batches']
                                               if b.get('candidate') and b.get('manifest'))
        result['kind_pending_batches'] = sum(b['status'] == 'reviewed' and not certified(bundle, b)
                                             for b in state['batches'])
        result['alias_corrections'] = [{'target_id': f['target_id'], 'finding_id': f['finding_id'],
            'status': f['status'], 'translation_identity': f['alias_anchor']['translation_identity']}
            for b in state['batches'] for f in b.get('external_content_findings', []) if f.get('alias_anchor')]
        result['kind_rejections'] = [{'batch': n, 'findings': b['kind_pending_findings']}
                                    for n, b in enumerate(state['batches']) if b.get('kind_pending_findings')]
        return result

    def certify_kinds(self, run_id):
        """One small independent KIND call; retain existing scientific pass unchanged."""
        with _exclusive(self._path(run_id).with_suffix('.lock')):
            state = self._read(run_id)
            bundle = self._fresh(state)
            from .kind_review import certified
            batch = next((b for b in state['batches'] if b['status'] == 'reviewed'
                          and not certified(bundle, b)), None)
            if batch is not None:
                self._certify_kind(state, batch, bundle)
            if all(b['status'] == 'reviewed' and certified(bundle, b) for b in state['batches']):
                if state['status'] != 'published':
                    state['status'] = 'ready'
                    self._save(state)
        return self.report(run_id)

    @staticmethod
    def _one_process_group(original, value):
        added = len(value) - len(original)
        return added > 0 and any(
            value[:index] == original[:index] and value[index + added:] == original[index:]
            and any(s.get('kind') == 'text' and s.get('text', '').strip()
                    for s in value[index:index + added])
            for index in range(len(original) + 1))

    def _ordinary_process_retry(self, state, batch, bundle):
        findings = [f for f in batch.get('external_content_findings', [])
                    if f.get('budget_policy') == 'ordinary_semantic_limit_only' and f['status'] == 'rejected']
        review = batch.get('review') or {}
        if (not findings or batch.get('structural_issues') or review.get('verdict') != 'repair'
                or not review.get('issues') or self._repair_counts(batch)['semantic'] >= self._repair_limit(state, batch, 'semantic')
                or any(i['target_key'] not in {f['target_id'] for f in findings} for i in review['issues'])):
            return False
        packet = self._packet(bundle, batch['block_ids'], target_ids=batch['target_ids'],
                              include_text=batch['include_text'])
        if (batch.get('review_candidate_sha256') != digest(batch['candidate'])
                or batch.get('review_context_sha256') != digest(packet)
                or batch.get('review_translation_identity') != packet['translation_identity']
                or batch.get('review_manifest_sha256') != batch['manifest']['fingerprint']
                or _manifest_hash(batch['manifest']) != batch['manifest']['fingerprint']):
            return False
        for finding in findings:
            item = next((i for i in batch['candidate']['items'] if i['target']['id'] == finding['target_id']), None)
            term_referent = finding['field'] == 'term_referent'
            original_target = {k: v for k, v in finding['source_target'].items() if not term_referent or k != 'concept_key'}
            current_target = {k: v for k, v in (item or {}).get('target', {}).items() if not term_referent or k != 'concept_key'}
            if (finding['source_fingerprint'] != bundle['fingerprint'] or finding['used'] != 1
                    or finding.get('final_review_sha256') != digest(review)
                    or finding.get('final_candidate_sha256') != digest(batch['candidate'])
                    or item is None or current_target != original_target
                    or (term_referent and item['target']['kind'] != 'term')
                    or (not term_referent and (item['target']['kind'] != 'math'
                        or item['explanation']['math_role'] != 'formula'
                        or not self._one_process_group(finding['observed_value'], item['explanation']['process'])))):
                return False
        return True

    def _kind_symbol_retry(self, state, batch, bundle):
        review = batch.get('review') or {}
        if (not (batch.get('kind_symbol_repair_origin') or
                 (batch.get('kind_certificate') or {}).get('status') == 'repair')
                or batch.get('kind_pending_findings') or batch.get('structural_issues')
                or review.get('verdict') != 'repair' or not review.get('issues')
                or self._repair_counts(batch)['semantic'] >= self._repair_limit(state, batch, 'semantic')
                or any(f['status'] != 'resolved' for f in batch.get('external_content_findings', []) +
                       batch.get('rejected_content_experiments', []))):
            return False
        items = {i['target']['id']: i for i in batch['candidate']['items']}
        for issue in review['issues']:
            item = items.get(issue['target_key'])
            if (issue['code'] != 'missing_symbol_referents' or item is None
                    or item['target']['kind'] != 'math' or item['explanation']['math_role'] != 'formula'):
                return False
        packet = self._packet(bundle, batch['block_ids'], target_ids=batch['target_ids'],
                              include_text=batch['include_text'])
        return (batch.get('review_candidate_sha256') == digest(batch['candidate'])
                and batch.get('review_context_sha256') == digest(packet)
                and batch.get('review_translation_identity') == packet['translation_identity']
                and batch.get('review_manifest_sha256') == batch['manifest']['fingerprint']
                and _manifest_hash(batch['manifest']) == batch['manifest']['fingerprint'])

    def step(self, run_id):
        if self.runner is None:
            raise ValueError('Actual selection/content runner required')
        with _exclusive(self._path(run_id).with_suffix('.lock')):
            state = self._read(run_id)
            bundle = self._fresh(state)
            from .kind_review import certified
            uncertified = next((b for b in state['batches'] if b['status'] == 'reviewed'
                                and not certified(bundle, b)), None)
            if uncertified is not None:
                self._certify_kind(state, uncertified, bundle)
                return self.report(run_id)
            if state['status'] in ('ready', 'published'):
                return self.report(run_id)
            batch = next((b for b in state['batches'] if b['status'] not in ('reviewed', 'needs_review')
                          or b['status'] == 'needs_review' and (
                              self._ordinary_process_retry(state, b, bundle)
                              or self._kind_symbol_retry(state, b, bundle))), None)
            if batch is None:
                state['status'] = 'ready' if all(b['status'] == 'reviewed' for b in state['batches']) else 'needs_review'
                self._save(state)
                return self.report(run_id)
            packet = self._packet(bundle, batch['block_ids'], target_ids=batch['target_ids'],
                                  include_text=batch['include_text'])
            if self._kind_symbol_retry(state, batch, bundle):
                batch['kind_symbol_repair_origin'] = True
                batch['scoped_symbol_repair'] = {'source_fingerprint': bundle['fingerprint'],
                    'candidate_sha256': digest(batch['candidate']), 'review_sha256': digest(batch['review']),
                    'context_sha256': digest(packet), 'manifest_sha256': batch['manifest']['fingerprint'],
                    'translation_identity': packet['translation_identity'],
                    'target_ids': list(dict.fromkeys(i['target_key'] for i in batch['review']['issues']))}
                batch['evidence'].append({'stage': 'ordinary_kind_symbol_repair_resumed',
                    'scope': deepcopy(batch['scoped_symbol_repair']),
                    'semantic_repairs_before': self._repair_counts(batch)['semantic'],
                    'extra_allowance_granted': False})
                if batch.get('kind_certificate'):
                    batch.setdefault('retired_kind_certificates', []).append(batch.pop('kind_certificate'))
            try:
                host_revalidated = self._structural_host_revalidation_count(state, batch, bundle)
                if (batch.get('structural_repairs', 0) > self._repair_limit(state, batch, 'structural')
                        and (host_revalidated != batch.get('structural_repairs') or batch.get('structural_issues'))):
                    batch['status'], state['status'] = 'needs_review', 'pending'
                    self._save(state)
                    return self.report(run_id)
                if batch.get('manifest') is None:
                    state['status'] = 'selecting'
                    self._save(state)
                    before_selection = deepcopy(batch)
                    try:
                        self._select(state, batch, bundle, packet)
                    except (ValueError, KeyError, StopIteration) as exc:
                        for key in ('manifest', 'selection_review', 'inherited_issues', 'supplements'):
                            if key in before_selection:
                                batch[key] = before_selection[key]
                            else:
                                batch.pop(key, None)
                        batch['evidence'].append({'stage': 'selection_host_validation',
                            'status': 'host_rejected', 'error': str(exc)})
                        raise ExplanationProtocolError(str(exc), {}) from exc
                    batch['structural_issues'] = []
                    self._save(state)
                if _manifest_hash(batch['manifest']) != batch['manifest']['fingerprint']:
                    raise ValueError('Frozen selection manifest was changed')
                if batch.get('candidate') is None or not batch.get('assembled_manifest'):
                    missing = self._assemble(bundle, batch)
                    batch['assembled_manifest'] = batch['manifest']['fingerprint']
                    self._save(state)
                else:
                    present = {i['target']['id'] for i in batch['candidate']['items']}
                    missing = [t for t in batch['manifest']['targets'] if t['id'] not in present]
                if missing:
                    state['status'] = 'generating'
                    self._save(state)
                    self._generate_subset(state, batch, bundle, packet, missing)
                    self._save(state)
                findings = [f for f in batch.get('external_content_findings', [])
                            if f['status'] == 'open' or f.get('budget_policy') == 'ordinary_semantic_limit_only'
                            and f['status'] == 'rejected']
                experiments = [f for f in batch.get('rejected_content_experiments', []) if f['status'] == 'open']
                allocations = findings + experiments
                if ((batch.get('structural_issues') and batch.get('structural_failure_mode') != 'reviewing')
                        or (batch.get('review') or {}).get('verdict') == 'repair' or allocations
                        or batch.get('kind_pending_findings')):
                    counts = self._repair_counts(batch)
                    structural = bool(batch.get('structural_issues'))
                    external_only = bool(findings) and not structural and not (batch.get('review') or {}).get('issues')
                    experiment_only = bool(experiments) and not structural and all(
                        x['target_key'] in {e['target_id'] for e in experiments}
                        and x['code'] == 'incomplete_math_process'
                        for x in (batch.get('review') or {}).get('issues', []))
                    explicit = external_only or experiment_only
                    ordinary_process = any(f.get('budget_policy') == 'ordinary_semantic_limit_only'
                                           for f in findings)
                    if experiment_only and any(
                            e['prior_candidate_sha256'] != digest(batch['candidate'])
                            or e['prior_rejection_sha256'] != digest(batch.get('review'))
                            for e in experiments):
                        raise ValueError('Recorded experiment no longer matches the rejected candidate/review')
                    if ((ordinary_process and counts['semantic'] >= self._repair_limit(state, batch, 'semantic'))
                            or (explicit and any(f['used'] >= f['allowance'] for f in allocations))
                            or (not explicit and counts['structural' if structural else 'semantic'] >= self._repair_limit(state, batch, 'structural' if structural else 'semantic'))):
                        batch['status'], state['status'] = 'needs_review', 'pending'
                        self._save(state)
                        return self.report(run_id)
                    if ordinary_process and any(f['status'] == 'rejected' for f in findings):
                        batch['evidence'].append({'stage': 'ordinary_process_repair_resumed',
                            'source_fingerprint': bundle['fingerprint'],
                            'prior_review_sha256': digest(batch['review']),
                            'prior_candidate_sha256': digest(batch['candidate']),
                            'context_sha256': digest(packet), 'semantic_repairs_before': counts['semantic'],
                            'finding_ids': [f['finding_id'] for f in findings],
                            'finding_used_unchanged': [f['used'] for f in findings],
                            'current_host_repair_contract': [f.get('repair_mode') for f in findings],
                            'extra_allowance_granted': False})
                    batch['inherited_issues'] = self._inherited_issues(batch, state['run_id'])
                    bad = {x['target_key'] for x in (batch.get('review') or {}).get('issues', [])}
                    bad.update(f['target_id'] for f in allocations)
                    bad.update(f['target_id'] for f in batch.get('kind_pending_findings', []))
                    if structural:
                        bad.update(t['id'] for t in batch.get('pending_targets', []))
                    targets = [t for t in batch['manifest']['targets'] if t['id'] in bad]
                    state['status'] = 'repairing'
                    self._save(state)
                    batch['structural_repairs' if structural else 'semantic_repairs'] += 1
                    batch['repairs'] += 1
                    if explicit:
                        for finding in allocations:
                            finding['used'] += 1
                        batch['evidence'].append({'stage': 'external_content_repair_allowance',
                            'finding_ids': [f['finding_id'] for f in allocations],
                            'semantic_repairs_after': batch['semantic_repairs'],
                            'budget_policy': ('ordinary_semantic_limit_only' if ordinary_process
                                              else 'source_pass_bound_one_shot'),
                            'existing_unresolved_rejection_budget_extended': False})
                    self._save(state)
                    self._generate_subset(state, batch, bundle, packet, targets, repair=True)
                    batch['review'] = None
                    self._save(state)
                candidate_ids = [i['target']['id'] for i in batch['candidate']['items']]
                if len(candidate_ids) != len(set(candidate_ids)) or set(candidate_ids) != {t['id'] for t in batch['manifest']['targets']}:
                    raise ValueError('Candidate does not cover the frozen selected scope exactly')
                self._validate_candidate(bundle, deepcopy(batch['candidate']))
                self._fresh(state)
                packet = self._packet(bundle, batch['block_ids'], target_ids=batch['target_ids'],
                                      include_text=batch['include_text'])
                request = {'mode': 'content_review', 'stage': 'review', 'source_fingerprint': bundle['fingerprint'],
                    'packet': packet, 'manifest': batch['manifest'], 'candidate': batch['candidate'],
                    'inherited_issues': batch.get('inherited_issues', []),
                    'validation_feedback': batch.get('structural_issues', [])}
                complete_aliases = [f for f in batch.get('external_content_findings', [])
                                    if f.get('alias_anchor', {}).get('mode') == 'complete_occurrences']
                if complete_aliases:
                    self._check_complete_aliases(bundle, complete_aliases)
                    request['complete_alias_findings'] = complete_aliases
                if batch.get('manifest_amendments'):
                    request['term_referent_manifest_amendments'] = batch['manifest_amendments']
                state['status'] = 'reviewing'
                self._save(state)
                review = self._call(state, batch, request, CONTENT_REVIEW_SCHEMA)
                batch['structural_issues'] = []
                batch.pop('structural_failure_mode', None)
                if batch.get('compatible_v6_proof'):
                    batch['historical_v6_proof'] = batch.pop('compatible_v6_proof')
                batch['review'] = review
                scoped_symbol_attempt = bool(batch.get('scoped_symbol_repair'))
                if scoped_symbol_attempt:
                    batch.setdefault('scoped_symbol_repair_history', []).append(batch.pop('scoped_symbol_repair'))
                batch['review_candidate_sha256'] = digest(batch['candidate'])
                batch['review_translation_identity'] = packet['translation_identity']
                batch['review_context_sha256'] = digest(packet)
                batch['review_manifest_sha256'] = batch['manifest']['fingerprint']
                try:
                    self._supplements(bundle, batch, review['late_terms'], packet, kind='late_term')
                except (ValueError, KeyError) as exc:
                    raise ExplanationProtocolError(str(exc), review) from exc
                current = self._packet(bundle, batch['block_ids'], target_ids=batch['target_ids'],
                                       include_text=batch['include_text'])
                if current['translation_identity'] != packet['translation_identity']:
                    batch['review'], batch['status'], state['status'] = None, 'generated', 'pending'
                    self._save(state)
                    return self.report(run_id)
                if allocations:
                    for finding in allocations:
                        history = finding.setdefault('review_history', [])
                        if (finding.get('final_review_sha256') and not any(
                                h['review_sha256'] == finding['final_review_sha256'] for h in history)):
                            history.append({'review_sha256': finding['final_review_sha256'],
                                            'candidate_sha256': finding['final_candidate_sha256'],
                                            'verdict': 'repair' if finding['status'] == 'rejected' else 'pass',
                                            'origin': 'retained_prior_review'})
                        history.append({
                            'review_sha256': digest(review), 'candidate_sha256': digest(batch['candidate']),
                            'verdict': review['verdict'], 'semantic_repairs': batch['semantic_repairs']})
                        finding['status'] = 'resolved' if review['verdict'] == 'pass' else 'rejected'
                        finding['final_review_sha256'] = digest(review)
                        finding['final_candidate_sha256'] = digest(batch['candidate'])
                batch['status'] = ('reviewed' if review['verdict'] == 'pass' else
                                   'needs_review' if allocations or batch.get('kind_repair_inflight')
                                   or scoped_symbol_attempt else 'generated')
                batch.pop('kind_repair_inflight', None)
                state['status'], state['error'] = 'pending', None
                self._fresh(state)
                self._save(state)
            except ExplanationProtocolError as exc:
                batch['structural_issues'] = [str(exc)]
                batch['structural_failure_mode'] = state['status']
                batch['structural_repairs'] += 1
                batch['status'] = ('needs_review' if batch['structural_repairs'] > self._repair_limit(state, batch, 'structural')
                                   else 'repair_pending')
                if any(f['used'] for f in batch.get('external_content_findings', []) +
                       batch.get('rejected_content_experiments', []) if f['status'] == 'open'):
                    batch['status'] = 'needs_review'
                state['status'], state['error'] = 'pending', str(exc)
                self._save(state)
            except Exception as exc:
                state['status'], state['error'] = 'failed', str(exc)
                self._save(state)
                raise
        return self.report(run_id)

    def _check_complete_aliases(self, bundle, findings):
        from .alias_review import anchor
        for finding in findings:
            proof = finding.get('alias_anchor', {})
            if proof.get('mode') == 'complete_occurrences':
                anchor(self.service, bundle, finding['source_target'], proof['literal'],
                       proof['translation_identity'], complete_occurrences=proof)

    def _before_commit(self, state, bundle):
        from .kind_review import certified
        for batch in state['batches']:
            self._check_complete_aliases(bundle, batch.get('external_content_findings', []))
            if not certified(bundle, batch):
                raise ValueError('Every selected term needs a current independent KIND certificate')
            if any(f['status'] != 'resolved' for f in batch.get('external_content_findings', [])):
                raise ValueError('Known externally observed content defects are still unresolved')
            if any(f['status'] != 'resolved' for f in batch.get('rejected_content_experiments', [])):
                raise ValueError('Explicit rejected-content experiment has not passed fresh full review')
            if not batch.get('manifest'):
                raise ValueError('Every text chunk needs a frozen reviewed selection before publication')
            if _manifest_hash(batch['manifest']) != batch['manifest']['fingerprint']:
                raise ValueError('Frozen selection manifest was changed')
            proof = batch.get('compatible_v6_proof')
            if proof:
                packet = self._packet(bundle, batch['block_ids'], target_ids=batch['target_ids'],
                                      include_text=batch['include_text'])
                if (proof['candidate_sha256'] != digest(batch['candidate'])
                        or proof['review_context_sha256'] != digest(packet)
                        or proof['translation_identity'] != packet['translation_identity']
                        or proof['response_sha256'] != digest(batch['review'])
                        or proof['manifest_targets'] != [i['target'] for i in batch['candidate']['items']
                                                       if i['target']['kind'] == 'term']):
                    raise ValueError('Legacy pass compatibility no longer matches the current reviewed content')
            elif batch.get('review_manifest_sha256') != batch['manifest']['fingerprint']:
                raise ValueError('Content review is not bound to the frozen manifest')
            record = {'stage': 'frozen_selection_evidence',
                    'manifest': batch['manifest'], 'supplements': batch['supplements'],
                    'selection_review': batch.get('selection_review'),
                    'external_content_findings': batch.get('external_content_findings', []),
                    'rejected_content_experiments': batch.get('rejected_content_experiments', []),
                    'external_reopened_pass': batch.get('external_reopened_pass'),
                    'manifest_amendments': batch.get('manifest_amendments', []),
                    'kind_certificate': batch.get('kind_certificate'),
                    'kind_certificate_history': batch.get('kind_certificate_history', [])}
            if record not in batch['evidence']:
                batch['evidence'].append(record)
