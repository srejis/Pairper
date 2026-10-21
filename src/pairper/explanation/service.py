"""Public source-bound explanation API shared by workflow and reader."""
from copy import deepcopy
from pathlib import Path

from pairper.translation.prepare import current_source

from .catalog import prepare_catalog, target_math_content_kind, validate_targets
from .models import Explanation
from .store import ExplanationStore
from .text_math import all_math_targets, decorate, supplemental_targets


def validate(bundle, targets, explanations, require_review=False, require_complete=False):
    targets = validate_targets(bundle, targets)
    lookup = {t['id']: t for t in targets}
    entries = [Explanation.model_validate(e).model_dump(mode='json') for e in explanations]
    seen = set()
    source_refs = {ref for u in bundle['text_units'] for ref in u['source_refs']}
    source_refs |= {ref for t in targets for ref in t['source_refs']}
    for entry in entries:
        tid = entry['target_id']
        if tid in seen or tid not in lookup or entry['kind'] != lookup[tid]['kind']:
            raise ValueError('Explanation target missing, duplicated or of a different kind')
        seen.add(tid)
        if entry['kind'] == 'math':
            source_kind = target_math_content_kind(bundle, lookup[tid])
            text_disposition = (lookup[tid].get('anchor_type') == 'text_math'
                                and entry['math_role'] == 'notation')
            if (entry['math_role'] in ('symbol', 'literal', 'notation')
                    and entry['math_role'] != source_kind and not text_disposition):
                raise ValueError('Math role does not match the source expression; formulas need a process')
        if set(entry['evidence_refs']) - source_refs:
            raise ValueError('Explanation evidence does not belong to current source')
        if require_review and not entry['reviewed']:
            raise ValueError('Every published explanation needs semantic review')
    mandatory = {t['id'] for t in all_math_targets(bundle) if t['kind'] == 'math'}
    if require_complete and (seen != set(lookup) or not mandatory <= set(lookup)):
        raise ValueError('Every registered term and source formula needs an explanation')
    return targets, entries


def coverage(bundle, saved):
    raw = {t['id'] for t in bundle['targets'] if t['kind'] == 'math'}
    supplemental = {t['id'] for t in supplemental_targets(bundle)}
    mandatory = raw | supplemental
    entries = saved.get('explanations', [])
    reviewed = {e['target_id'] for e in entries if e.get('reviewed')}
    return {'source_math_count': len(mandatory), 'reviewed_math_count': len(mandatory & reviewed),
            'raw_math_count': len(raw), 'reviewed_raw_math_count': len(raw & reviewed),
            'supplemental_math_count': len(supplemental),
            'reviewed_supplemental_math_count': len(supplemental & reviewed),
            'notation_count': sum(e.get('math_role') == 'notation' for e in entries),
            'term_count': sum(e['kind'] == 'term' for e in entries),
            'complete': saved.get('status') == 'published' and mandatory <= reviewed}


class ExplanationService:
    def __init__(self, library, directory=None):
        self.library = library
        self.directory = Path(directory or library.root / 'explanations')
        self.store = ExplanationStore(self.directory)
        self._translation_surface_cache = {}

    def prepare(self, paper_id):
        _, source, document, identity = current_source(self.library, paper_id)
        return decorate(prepare_catalog(document, source, identity))

    def translation_surfaces(self, paper_id, bundle=None):
        """Current Korean reader text for anchoring only, never scientific evidence."""
        from pairper.translation.service import TranslationService

        from .projection import _fields

        current = self.prepare(paper_id)
        if bundle is not None and (bundle['paper_id'] != paper_id
                                   or bundle['fingerprint'] != current['fingerprint']):
            raise ValueError('Source changed before translation anchoring')
        translations = TranslationService(self.library)
        sessions = translations.sessions(paper_id)
        empty = {'translation_session_id': None, 'translation_revision': None,
                 'source_identity': current['source_identity'], 'surfaces': []}
        if not sessions:
            return empty
        session = sessions[0]
        key = (paper_id, current['fingerprint'], session['session_id'], session['revision'])
        if key in self._translation_surface_cache:
            return deepcopy(self._translation_surface_cache[key])
        display = translations.display(session['session_id'])
        if display.get('source_identity') != current['source_identity']:
            raise ValueError('Translated reader and explanation source identities differ')
        known = {f'{u["block_id"]}/{u["field"]}' for u in current['text_units']}
        known |= {f'{t["block_id"]}/{t["field"]}' for t in current['targets']
                  if t['field'] != 'latex'}
        surfaces = []
        for block in display['blocks']:
            for field, runs in _fields(block['content']):
                for index, run in enumerate(runs):
                    if run['kind'] != 'text' or not run.get('text'):
                        continue
                    units = run.get('source_unit_ids', [f'{block["id"]}/{field}'])
                    if not set(units) <= known:
                        raise ValueError('Translated surface has unknown source-unit provenance')
                    surfaces.append({'block_id': block['id'], 'field': field, 'run_index': index,
                                     'source_unit_ids': list(units), 'text': run['text']})
        result = {**empty, 'translation_session_id': session['session_id'],
                  'translation_revision': session['revision'], 'surfaces': surfaces}
        if self.prepare(paper_id)['fingerprint'] != current['fingerprint']:
            raise ValueError('Source changed while reading translated term surfaces')
        self._translation_surface_cache = {key: deepcopy(result)}
        return result

    def _current(self, paper_id):
        bundle = self.prepare(paper_id)
        saved = self.store.read(paper_id, bundle['fingerprint'])
        return bundle, saved or {**bundle, 'explanations': [], 'revision': 0, 'status': 'draft'}

    def get(self, paper_id, published_only=True):
        bundle = self.prepare(paper_id)
        saved = self.store.read(paper_id, bundle['fingerprint'], published_only)
        if saved is None:
            empty = {**bundle, 'targets': [] if published_only else bundle['targets'],
                     'explanations': [], 'revision': 0, 'status': 'empty'}
            return {**empty, 'complete': False, 'coverage': coverage(bundle, empty)}
        validate(bundle, saved['targets'], saved['explanations'],
                 require_review=published_only, require_complete=published_only)
        report = coverage(bundle, saved)
        return {**saved, 'complete': report['complete'], 'coverage': report,
                'source_footnote_markers': bundle.get('source_footnote_markers', [])}

    def project(self, paper_id, display):
        from .projection import project_explanations

        bundle = self.get(paper_id)
        if display.get('source_identity') is not None and display['source_identity'] != bundle['source_identity']:
            raise ValueError('Display source changed; reload before opening explanations')
        result = project_explanations(bundle, display)
        if self.prepare(paper_id)['fingerprint'] != bundle['fingerprint']:
            raise ValueError('Source changed while projecting explanations')
        return result

    def display(self, paper_id, translation_session_id=None):
        """Project against a freshly rendered current view with a source race guard."""
        before = self.prepare(paper_id)
        if translation_session_id:
            from pairper.translation.service import TranslationService

            display = TranslationService(self.library).display(translation_session_id)
        else:
            from pairper.document.presentation import display_document

            display = display_document(self.library, paper_id)
        if display['paper_id'] != paper_id:
            raise ValueError('Translation session belongs to a different paper')
        display['source_identity'] = before['source_identity']
        return self.project(paper_id, display)

    def register_targets(self, paper_id, targets, expected_revision=None):
        return self.save(paper_id, [], targets=targets, expected_revision=expected_revision)

    def save(self, paper_id, explanations, targets=None, expected_revision=None,
             workflow_evidence=None, replace=False):
        base, saved = self._current(paper_id)
        if type(replace) is not bool:
            raise ValueError('replace must be a boolean')
        selected = {t['id']: t for t in (all_math_targets(base) if replace else saved['targets'])}
        for target in supplemental_targets(base):
            selected.setdefault(target['id'], target)
        changed_targets = set()
        if targets is not None:
            for target in validate_targets(base, targets):
                if target['id'] in selected and selected[target['id']] != target:
                    changed_targets.add(target['id'])
                selected[target['id']] = target
        entries = {} if replace else {e['target_id']: e for e in saved['explanations']}
        # Aliases are part of what the reviewer approved. Changing an anchor's
        # localization must not inherit the previous explanation's review flag.
        for tid in changed_targets:
            if tid in entries:
                entries[tid] = {**entries[tid], 'reviewed': False}
        for entry in explanations:
            parsed = Explanation.model_validate(entry).model_dump(mode='json')
            entries[parsed['target_id']] = parsed
        selected, entries = validate(base, list(selected.values()), list(entries.values()))
        # Recheck after generation / parsing, immediately before persisting.
        if self.prepare(paper_id)['fingerprint'] != base['fingerprint']:
            raise ValueError('Source changed while saving explanations')
        payload = {**base, 'targets': selected, 'explanations': entries,
                   'workflow_evidence': saved.get('workflow_evidence', [])}
        if workflow_evidence is not None:
            if not isinstance(workflow_evidence, dict):
                raise ValueError('Workflow evidence must be an object')
            payload['workflow_evidence'] = [*payload['workflow_evidence'], workflow_evidence]
        return self.store.write(payload, saved['revision'] if expected_revision is None
                                else expected_revision)

    def publish(self, paper_id, expected_revision=None):
        base, saved = self._current(paper_id)
        validate(base, saved['targets'], saved['explanations'], True, True)
        if self.prepare(paper_id)['fingerprint'] != base['fingerprint']:
            raise ValueError('Source changed while publishing explanations')
        return self.store.write(saved, saved['revision'] if expected_revision is None
                                else expected_revision, 'published')
