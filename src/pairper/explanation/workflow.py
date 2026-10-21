"""Durable whole-paper explanation generation/review/repair, independent of any demo PDF."""
from __future__ import annotations

import ast
import json
import os
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from .catalog import digest, normalize_unique_term_offsets, target_id, validate_targets
from .prompts import GENERATION_SCHEMA, PROMPT_VERSION, REVIEW_SCHEMA, validate_protocol
from .runner import ExplanationProtocolError
from .workflow_archive import ArchiveEvidenceError


def _write(path, value):
    from pairper.storage.library import write_json
    write_json(Path(path), value)


@contextmanager
def _exclusive(path):
    """OS-released lock: crashes never strand a busy run behind a stale lock file."""
    handle = Path(path).open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise ValueError('Explanation workflow is already running') from exc
    try:
        yield
    finally:
        handle.close()


def source_batches(bundle, max_characters=6500):
    """Every reader block is visited once; formulas and term text share local context."""
    if not 500 <= max_characters <= 30000:
        raise ValueError('max_characters must be in [500,30000]')
    block_ids = [x['block_id'] for x in bundle['context']]
    chunks, ids, size = [], [], 0
    for block_id in block_ids:
        context = next(x for x in bundle['context'] if x['block_id'] == block_id)
        current_size = len(context['text'])
        if ids and size + current_size > max_characters:
            chunks.append(ids)
            ids, size = [], 0
        ids.append(block_id)
        size += current_size
    if ids:
        chunks.append(ids)
    return chunks


def batch_plan(bundle, max_characters=6500, max_math_targets=16):
    """Bound output size without omitting dense inline mathematics or duplicating term anchors."""
    if not 1 <= max_math_targets <= 64:
        raise ValueError('max_math_targets must be in [1,64]')
    planned = []
    for ids in source_batches(bundle, max_characters):
        targets = [t['id'] for t in bundle['targets'] if t['block_id'] in set(ids)]
        for offset in range(0, max(1, len(targets)), max_math_targets):
            planned.append({'block_ids': ids, 'target_ids': targets[offset:offset+max_math_targets],
                            'include_text': offset == 0})
    return planned


class ExplanationWorkflow:
    def __init__(self, service, runner=None, *, directory=None, max_repairs=2):
        if not 0 <= max_repairs <= 4:
            raise ValueError('max_repairs must be in [0,4]')
        self.service, self.runner, self.max_repairs = service, runner, max_repairs
        self.directory = Path(directory or (service.directory / 'workflows'))
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id):
        if not isinstance(run_id, str) or not run_id.startswith('exw_') or not run_id[4:].isalnum():
            raise ValueError('Invalid explanation run ID')
        return self.directory / (run_id + '.json')

    def _read(self, run_id):
        path = self._path(run_id)
        if not path.is_file():
            raise ValueError('Explanation workflow does not exist')
        from .workflow_archive import verify_imported_archives
        verify_imported_archives(self.directory, run_id)
        return json.loads(path.read_text(encoding='utf-8'))

    def _save(self, state):
        _write(self._path(state['run_id']), state)

    def _artifact_directory(self, original):
        from .workflow_archive import resolve_artifact_directory
        return resolve_artifact_directory(self.directory, original)

    def _repair_counts(self, batch):
        """Recover every durable ancestral host repair once; existing counters never decrease."""
        baseline = {k: batch.get(k + '_repairs', 0) for k in ('semantic', 'structural')}
        durable = {'semantic': 0, 'structural': 0}
        requests, unresolved, seen = [], [], set()

        def walk(events):
            for event in events:
                if event.get('stage') == 'adopted_candidate':
                    for key in baseline:
                        baseline[key] = max(baseline[key], event.get('repair_counts_before', {}).get(key, 0))
                if event.get('stage') == 'repair':
                    folder = (event.get('transport') or {}).get('directory')
                    identity = os.path.normcase(str(self._artifact_directory(folder).resolve())) if folder else digest(event)
                    if identity not in seen:
                        seen.add(identity)
                        try:
                            request = json.loads((self._artifact_directory(folder) / 'request.json').read_text(encoding='utf-8'))
                            if event.get('request_sha256') and digest(request) != event['request_sha256']:
                                raise ValueError('Stored repair request digest differs')
                        except ArchiveEvidenceError:
                            raise
                        except (TypeError, OSError, ValueError) as exc:
                            unresolved.append({'directory': folder, 'reason': str(exc)})
                        else:
                            structural = bool(request.get('validation_feedback'))
                            issues = (request.get('review') or {}).get('issues', [])
                            semantic = bool(issues) or not structural
                            durable['structural'] += int(structural)
                            durable['semantic'] += int(semantic)
                            requests.append({'directory': folder, 'request_sha256': digest(request),
                                'structural': structural, 'semantic': semantic,
                                'issue_codes': [i.get('code') for i in issues],
                                'target_keys': [i.get('target_key') for i in issues]})
                walk(event.get('previous_generation_evidence', []))

        walk(batch.get('evidence', []))
        counts = {k: max(baseline[k], durable[k]) for k in baseline}
        batch['repair_budget_audit'] = {'baseline_counts': baseline, 'durable_counts': durable,
            'recovered_counts': counts, 'durable_requests': requests, 'unverified_requests': unresolved,
            'rule': 'max(existing and carried counters, unique durable ancestral repair requests)'}
        batch['semantic_repairs'], batch['structural_repairs'] = counts['semantic'], counts['structural']
        return counts

    def _inherited_issues(self, previous, previous_id):
        """Keep known rejections until a candidate-bound independent check resolves them."""
        inherited = deepcopy(previous.get('inherited_issues', []))
        review = previous.get('review')
        candidate = previous.get('candidate') or {'items': []}
        reviewed_candidate = candidate
        bound = review and previous.get('review_candidate_sha256') == digest(candidate)
        if bound:
            resolved = {x['issue_id'] for x in review.get('inherited_issue_checks', [])
                        if x['verdict'] in ('resolved', 'not_applicable')}
            inherited = [x for x in inherited if x['issue_id'] not in resolved]
        if review is None:
            # Legacy workers cleared review after author repair, before independent checking.
            # Recover that still-unresolved review from durable call evidence.
            for event in reversed(previous.get('evidence', [])):
                if event.get('stage') != 'review':
                    continue
                directory = (event.get('transport') or {}).get('directory')
                try:
                    review = json.loads((self._artifact_directory(directory) / 'response.json').read_text(encoding='utf-8'))
                    request = json.loads((self._artifact_directory(directory) / 'request.json').read_text(encoding='utf-8'))
                    reviewed_candidate = request['candidate']
                except ArchiveEvidenceError:
                    raise
                except (TypeError, OSError, ValueError, KeyError):
                    continue
                break
        items = {i['key']: i for i in reviewed_candidate.get('items', [])}
        for issue in (review or {}).get('issues', []):
            source_target = (items.get(issue['target_key']) or {}).get('target')
            if not any(x['issue'] == issue and x.get('source_target') == source_target for x in inherited):
                inherited.append({'issue_id': digest([previous_id, issue]), 'from_run_id': previous_id,
                                  'issue': issue, 'source_target': source_target})
        return inherited

    def _fresh(self, state):
        # An already running worker must not publish old prompt quality after an update.
        source = Path(__file__).with_name('prompts.py').read_text(encoding='utf-8')
        version = next((node.value.value for node in ast.parse(source).body
                        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                        and any(isinstance(t, ast.Name) and t.id == 'PROMPT_VERSION'
                                for t in node.targets)), None)
        if state.get('prompt_version') != version:
            state['status'] = 'needs_review'
            state['error'] = 'Explanation protocol changed; start a new run for fresh generation and review.'
            self._save(state)
            raise ValueError(state['error'])
        current = self.service.prepare(state['paper_id'])
        if current['fingerprint'] != state['source_fingerprint']:
            state['status'] = 'source_changed'
            self._save(state)
            raise ValueError('Source changed; stale explanations cannot be published. Start a new run.')
        return current

    def start(self, paper_id, *, experiment_id='default', max_characters=6500,
              adopt_from_run_id=None):
        bundle = self.service.prepare(paper_id)
        identity = digest([paper_id, experiment_id, bundle['fingerprint'], max_characters,
                           PROMPT_VERSION])
        run_id = 'exw_' + identity[:24]
        path = self._path(run_id)
        if path.exists():
            return self.report(run_id)
        state = {'run_id': run_id, 'paper_id': paper_id, 'experiment_id': experiment_id,
                 'source_fingerprint': bundle['fingerprint'], 'status': 'pending',
                 'max_repairs': self.max_repairs, 'error': None,
                 'prompt_version': PROMPT_VERSION,
                 'batches': [{**spec, 'status': 'pending', 'repairs': 0,
                              'semantic_repairs': 0, 'structural_repairs': 0,
                              'candidate': None, 'review': None, 'evidence': []}
                             for spec in batch_plan(bundle, max_characters)]}
        if adopt_from_run_id:
            self._adopt(state, bundle, adopt_from_run_id)
        self._save(state)
        return self.report(run_id)

    def _adopt(self, state, bundle, previous_id):
        """Carry source-identical draft work across protocols; never carry any approval."""
        if previous_id == state['run_id']:
            raise ValueError('Cannot adopt a run into itself')
        with _exclusive(self._path(previous_id).with_suffix('.lock')):
            old = self._read(previous_id)
            if old['paper_id'] != state['paper_id'] or old['source_fingerprint'] != bundle['fingerprint']:
                raise ValueError('Candidate adoption requires the identical paper and source fingerprint')
            def spec(batch):
                return {k: batch.get(k) for k in ('block_ids', 'target_ids', 'include_text')}
            before = {digest(spec(b)): b for b in old['batches']}
            after = {digest(spec(b)): b for b in state['batches']}
            if set(before) != set(after) or len(before) != len(old['batches']):
                raise ValueError('Candidate adoption requires the identical complete batch plan')
            state['adoption'] = {'from_run_id': previous_id, 'from_protocol': old.get('prompt_version'),
                'source_run_sha256': digest(old), 'source_run_path': str(self._path(previous_id)),
                'repair_budget_before': old.get('max_repairs'), 'repair_budget_after': state['max_repairs'],
                'old_reviews_invalidated': True, 'adopted_batches': 0, 'rejected_batches': []}
            for key, batch in after.items():
                previous = before[key]
                candidate = previous.get('candidate')
                counts = self._repair_counts(previous)
                batch['repair_budget_audit'] = deepcopy(previous.get('repair_budget_audit'))
                batch['semantic_repairs'], batch['structural_repairs'] = counts['semantic'], counts['structural']
                batch['repairs'] = previous.get('repairs', counts['semantic'] + counts['structural'])
                batch['inherited_issues'] = self._inherited_issues(previous, previous_id)
                batch['evidence'] = [{'stage': 'adopted_candidate', 'from_run_id': previous_id,
                    'repair_counts_before': counts, 'raw_candidate_sha256': digest(candidate),
                    'previous_generation_evidence': previous.get('evidence', []),
                    'retired_review': previous.get('review'), 'approval_carried': False}]
                if candidate is None:
                    continue
                packet = self._packet(bundle, batch['block_ids'], target_ids=batch['target_ids'],
                                      include_text=batch['include_text'])
                try:
                    normalized, corrections = normalize_unique_term_offsets(
                        {'stage': 'generate', 'packet': packet}, candidate)
                    if normalized.get('source_fingerprint') != bundle['fingerprint']:
                        raise ValueError('Candidate fingerprint differs')
                    expected = {t['id'] for t in packet['targets']}
                    actual = [i['target']['id'] for i in normalized['items'] if i['target']['kind'] == 'math']
                    if set(actual) != expected or len(actual) != len(expected):
                        raise ValueError('Adopted candidate does not cover this exact math batch')
                    allowed_units = {(u['block_id'], u['field'], u['run_index']) for u in packet['text_units']}
                    if any((i['target']['block_id'], i['target']['field'], i['target']['run_index'])
                           not in allowed_units for i in normalized['items'] if i['target']['kind'] == 'term'):
                        raise ValueError('Adopted term is outside this batch')
                    normalized = self._validate_candidate(bundle, normalized, require_current=False)
                except (ValueError, KeyError, StopIteration) as exc:
                    state['adoption']['rejected_batches'].append({'batch': key, 'reason': str(exc)})
                    continue
                batch['candidate'] = normalized
                batch['status'] = 'generated'
                batch['evidence'][0].update(normalized_candidate_sha256=digest(normalized),
                                            host_binding_corrections=corrections)
                try:
                    self._validate_candidate(bundle, deepcopy(normalized))
                except ValueError as exc:
                    batch['structural_issues'] = [str(exc)]
                    batch['status'] = 'repair_pending'
                state['adoption']['adopted_batches'] += 1

    def report(self, run_id):
        state = self._read(run_id)
        try:
            self._fresh(state)
        except ValueError:
            if (state['status'] != 'source_changed'
                    and not (state.get('error') or '').startswith('Explanation protocol changed')):
                raise
        return {'run_id': run_id, 'paper_id': state['paper_id'], 'status': state['status'],
                'source_fingerprint': state['source_fingerprint'], 'error': state.get('error'),
                'total_batches': len(state['batches']),
                'reviewed_batches': sum(b['status'] == 'reviewed' for b in state['batches']),
                'item_count': sum(len((b.get('candidate') or {}).get('items', [])) for b in state['batches']),
                'batches': [{k: b[k] for k in ('status', 'repairs', 'block_ids', 'evidence')}
                            for b in state['batches']]}

    def _packet(self, bundle, ids, *, target_ids=None, include_text=True):
        from .catalog import target_math_content_kind
        selected = set(ids)
        positions = [i for i, c in enumerate(bundle['context']) if c['block_id'] in selected]
        context_ids = set(selected)
        for pos in positions:
            context_ids.update(x['block_id'] for x in bundle['context'][max(0, pos-2):pos+3])
        # The introductory definitions help later formulas without loading previous explanations.
        context_ids.update(x['block_id'] for x in bundle['context'][:12])
        targets = [t for t in bundle['targets'] if t['block_id'] in selected
                   and (target_ids is None or t['id'] in target_ids)]
        surfaces = (self.service.translation_surfaces(bundle['paper_id'], bundle)
                    if hasattr(self.service, 'translation_surfaces') else {'surfaces': []})
        source_unit_ids = {f"{u['block_id']}/{u['field']}" for u in bundle['text_units']
                           if u['block_id'] in selected}
        translation_surfaces = [s for s in surfaces.get('surfaces', [])
                                if source_unit_ids.intersection(s.get('source_unit_ids', []))]
        translation_identity = ({k: surfaces.get(k) for k in
                                 ('translation_session_id', 'translation_revision', 'source_identity')}
                                if 'translation_session_id' in surfaces else None)
        return {'paper_id': bundle['paper_id'],
                'text_units': [u for u in bundle['text_units']
                               if include_text and u['block_id'] in selected],
                'targets': targets,
                'math_role_hints': {t['id']: target_math_content_kind(bundle, t) for t in targets},
                'translation_surfaces': translation_surfaces,
                'translation_identity': translation_identity,
                'context': [c for c in bundle['context'] if c['block_id'] in context_ids]}

    def _call(self, state, batch, request, schema):
        import jsonschema
        call_dir = self.directory / state['run_id'] / ('batch-' + str(state['batches'].index(batch)))
        call_dir.mkdir(parents=True, exist_ok=True)
        try:
            response = self.runner.run(request, schema, [], call_dir)
            jsonschema.Draft202012Validator(schema).validate(response)
            raw_response_sha256 = digest(response)
            response, corrections = normalize_unique_term_offsets(request, response)
            try:
                validate_protocol(request, response)
            except ValueError as exc:
                raise ExplanationProtocolError(str(exc), response) from exc
        except ExplanationProtocolError as exc:
            batch['evidence'].append({'stage': request['stage'], 'status': 'protocol_rejected',
                'error': str(exc), 'request_sha256': digest(request),
                'response_sha256': digest(exc.response),
                'transport': getattr(self.runner, 'last_evidence', None)})
            raise
        record = {'stage': request['stage'], 'request_sha256': digest(request),
                  'response_sha256': digest(response),
                  'pre_host_response_sha256': raw_response_sha256,
                  'host_binding_corrections': corrections,
                  'transport': getattr(self.runner, 'last_evidence', None)}
        batch['evidence'].append(record)
        return response

    def _generate(self, state, batch, request, bundle):
        """A rejected structure becomes bounded repair input, never a reviewed candidate."""
        try:
            candidate = self._call(state, batch, request, GENERATION_SCHEMA)
        except ExplanationProtocolError as exc:
            batch['candidate'] = exc.response
            batch['structural_issues'] = [str(exc)]
            return False
        batch['candidate'] = candidate
        try:
            normalized = self._validate_candidate(bundle, deepcopy(candidate))
        except (ValueError, KeyError, StopIteration) as exc:
            batch['structural_issues'] = [str(exc)]
            return False
        batch['candidate'] = normalized
        batch['structural_issues'] = []
        return True

    def _validate_candidate(self, bundle, candidate, *, require_current=True):
        from .service import validate
        keys = [item.get('key') for item in candidate['items']]
        if any(not isinstance(key, str) or not key.strip() for key in keys) or len(set(keys)) != len(keys):
            raise ValueError('Explanation item keys must be nonempty and unique')
        targets = []
        for item in candidate['items']:
            target = dict(item['target'])
            if (require_current and target['kind'] == 'term'
                    and not (target.get('concept_key') or '').strip()):
                raise ValueError('Term needs the current protocol concept_key and Korean alias review')
            if target['kind'] == 'math':
                original = next(t for t in bundle['targets'] if t['id'] == target['id'])
                if any(target.get(k) != original.get(k) for k in
                       ('kind', 'block_id', 'field', 'run_index', 'start', 'end', 'source_text', 'display')):
                    raise ValueError('Math source binding changed before candidate normalization')
                target = dict(original)
            if target.get('id') is None:
                target.pop('id', None)
            targets.append(target)
        normalized = validate_targets(bundle, targets)
        for item, target in zip(candidate['items'], normalized, strict=True):
            item['target'] = ({**target, 'aliases': [], 'concept_key': ''}
                              if target['kind'] == 'math' else target)
        validate(bundle, normalized, [{**item['explanation'], 'target_id': item['target']['id']}
                                     for item in candidate['items']])
        return candidate

    def prioritize(self, run_id, block_id):
        """Move a requested reader block first without dropping any whole-paper coverage."""
        with _exclusive(self._path(run_id).with_suffix('.lock')):
            state = self._read(run_id)
            self._fresh(state)
            if not any(block_id in b['block_ids'] for b in state['batches']):
                raise ValueError('Unknown source block')
            state['batches'].sort(key=lambda b: (b['status'] == 'reviewed',
                                                 block_id not in b['block_ids']))
            self._save(state)
        return self.report(run_id)

    def step(self, run_id):
        if self.runner is None:
            raise ValueError('An actual model runner is required')
        with _exclusive(self._path(run_id).with_suffix('.lock')):
            state = self._read(run_id)
            bundle = self._fresh(state)
            if state['status'] in ('ready', 'published'):
                return self.report(run_id)
            batch = next((b for b in state['batches'] if b['status'] not in ('reviewed', 'needs_review')), None)
            if batch is None:
                state['status'] = ('ready' if all(b['status'] == 'reviewed' for b in state['batches'])
                                   else 'needs_review')
                self._save(state)
                return self.report(run_id)
            state['error'] = None
            packet = self._packet(bundle, batch['block_ids'], target_ids=batch.get('target_ids'),
                                  include_text=batch.get('include_text', True))
            base = {'source_fingerprint': state['source_fingerprint'], 'packet': packet}
            try:
                if batch['candidate'] is None:
                    state['status'] = 'generating'
                    self._save(state)
                    valid = self._generate(state, batch, {**base, 'stage': 'generate'}, bundle)
                    if not valid:
                        batch['status'], state['status'] = 'repair_pending', 'pending'
                        self._save(state)
                        return self.report(run_id)
                    batch['status'] = 'generated'
                    self._save(state)
                elif batch.get('structural_issues') or (batch['review'] and batch['review']['verdict'] == 'repair'):
                    counts = self._repair_counts(batch)
                    structural = bool(batch.get('structural_issues'))
                    semantic = bool((batch.get('review') or {}).get('issues'))
                    if ((structural and counts['structural'] >= state['max_repairs'])
                            or (semantic and counts['semantic'] >= state['max_repairs'])):
                        batch['status'] = 'needs_review'
                        state['status'] = ('pending' if any(b['status'] not in ('reviewed', 'needs_review')
                                                          for b in state['batches']) else 'needs_review')
                        self._save(state)
                        return self.report(run_id)
                    state['status'] = 'repairing'
                    self._save(state)
                    batch['inherited_issues'] = self._inherited_issues(batch, state['run_id'])
                    valid = self._generate(state, batch, {**base, 'stage': 'repair',
                        'candidate': batch['candidate'], 'review': batch['review'],
                        'validation_feedback': batch.get('structural_issues', [])}, bundle)
                    batch['repairs'] += 1
                    batch['structural_repairs'] += int(structural)
                    batch['semantic_repairs'] += int(semantic or not structural)
                    if not valid:
                        batch['status'], state['status'] = 'repair_pending', 'pending'
                        self._save(state)
                        return self.report(run_id)
                    batch['review'] = None
                    batch['status'] = 'generated'
                    self._save(state)
                self._fresh(state)
                # A translation can change while generation is running. Review the candidate
                # against the reader text that is current now, not the author's old surface.
                packet = self._packet(bundle, batch['block_ids'], target_ids=batch.get('target_ids'),
                                      include_text=batch.get('include_text', True))
                base['packet'] = packet
                state['status'] = 'reviewing'
                self._save(state)
                review = self._call(state, batch, {**base, 'stage': 'review',
                    'candidate': batch['candidate'],
                    'inherited_issues': batch.get('inherited_issues', [])}, REVIEW_SCHEMA)
                batch['review'] = review
                batch['review_candidate_sha256'] = digest(batch['candidate'])
                batch['review_translation_identity'] = packet.get('translation_identity')
                batch['review_context_sha256'] = digest(packet)
                current_packet = self._packet(bundle, batch['block_ids'], target_ids=batch.get('target_ids'),
                                              include_text=batch.get('include_text', True))
                if current_packet.get('translation_identity') != packet.get('translation_identity'):
                    batch['review'] = None
                    batch['status'], state['status'] = 'generated', 'pending'
                    self._save(state)
                    return self.report(run_id)
                batch['status'] = 'reviewed' if review['verdict'] == 'pass' else 'repair_pending'
                state['status'] = ('ready' if all(b['status'] == 'reviewed' for b in state['batches'])
                                   else 'pending')
                self._fresh(state)
                self._save(state)
            except Exception as exc:
                if state['status'] != 'source_changed':
                    state['status'] = 'failed'
                state['error'] = str(exc)
                # Completed generated/reviewed batches survive errors and can resume.
                self._save(state)
                raise
        return self.report(run_id)

    def run(self, run_id):
        while True:
            report = self.step(run_id)
            if report['status'] in ('ready', 'published', 'needs_review', 'source_changed'):
                return report

    def _before_commit(self, state, bundle):
        """Additional protocol gates run under the same OS lock as publication."""

    def commit(self, run_id, *, publish=True):
        with _exclusive(self._path(run_id).with_suffix('.lock')):
            state = self._read(run_id)
            bundle = self._fresh(state)
            if state['status'] not in ('ready', 'published') or any(
                    b['status'] != 'reviewed' for b in state['batches']):
                raise ValueError('Every batch must pass actual semantic review before publication')
            self._before_commit(state, bundle)
            targets, explanations = [], []
            for batch in state['batches']:
                self._validate_candidate(bundle, deepcopy(batch['candidate']))
                packet = self._packet(bundle, batch['block_ids'], target_ids=batch.get('target_ids'),
                                      include_text=batch.get('include_text', True))
                if batch.get('review_translation_identity') != packet.get('translation_identity'):
                    batch['review'] = None
                    batch['status'], state['status'] = 'generated', 'pending'
                    state['error'] = 'Korean reader text changed; fresh alias review required'
                    self._save(state)
                    raise ValueError('Korean reader text changed after review; re-review aliases before publication')
                if (batch.get('review_candidate_sha256') != digest(batch['candidate'])
                        or batch['review']['verdict'] != 'pass' or batch['review']['issues']):
                    raise ValueError('Semantic review is not bound to the current candidate')
                for item in batch['candidate']['items']:
                    target = item['target']
                    targets.append(target)
                    explanations.append({**item['explanation'], 'target_id': target_id(target),
                        'reviewed': True, 'evidence_refs': target['source_refs']})
            result = self.service.save(state['paper_id'], explanations, targets=targets,
                replace=True,
                workflow_evidence={'run_id': run_id, 'source_fingerprint': state['source_fingerprint'],
                    'batches': [{k: b.get(k) for k in ('block_ids', 'review', 'evidence',
                                'review_candidate_sha256', 'review_translation_identity',
                                'review_context_sha256', 'semantic_repairs', 'structural_repairs')}
                                for b in state['batches']]})
            if publish:
                result = self.service.publish(state['paper_id'], expected_revision=result['revision'])
                state['status'] = 'published'
                self._save(state)
            return result
