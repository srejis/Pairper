"""Trusted library boundary for source-bound translation tasks and reviews."""
import hashlib
from pathlib import Path

from .contract import entry_review_context, entry_review_requirements, sha, task_packet
from .prepare import assert_current, prepare_bundle
from .store import TranslationStore


class TranslationService:
    def __init__(self, library, directory=None):
        self.library = library
        self.directory = Path(directory or library.root / 'translations')
        self.store = TranslationStore(self.directory)

    def begin(self, paper_id, experiment_id='baseline'):
        bundle = prepare_bundle(self.library, paper_id, self.directory / 'assets')
        started = self.store.begin(bundle, experiment_id)
        return self.report(started['session_id'])

    def sessions(self, paper_id):
        available = []
        for row in self.store.sessions_for_paper(paper_id):
            if not row['has_display_content']:
                continue
            try:
                assert_current(self.library, row['source'])
            except ValueError:
                continue
            available.append({'session_id': row['session_id'], 'status': row['status'],
                              'revision': row['revision']})
        return available

    def display(self, session_id):
        from pairper.document.presentation import display_document

        from .projection import project_display
        bundle = self.current_bundle(session_id)
        saved = self.store.read(session_id, bundle['fingerprint'])
        base = display_document(self.library, bundle['paper_id'])
        # The source can change while the normal display serializer runs.
        assert_current(self.library, bundle)
        projected = project_display(bundle, saved['result'], base)
        projected['translation'].update(session_id=session_id, revision=saved['revision'], status=saved['status'])
        return projected

    def current_bundle(self, session_id):
        bundle = self.store.source_bundle(session_id)
        assert_current(self.library, bundle)
        return bundle

    def report(self, session_id, offset=0, limit=100):
        self.bounds(offset, limit)
        bundle = self.current_bundle(session_id)
        saved = self.store.read(session_id, bundle['fingerprint'])
        report = saved['report']
        all_missing = report.pop('missing_units')
        unreviewed = report.pop('unreviewed_units')
        math_pending = report.pop('unreviewed_math', [])
        return {'session_id': session_id, 'paper_id': bundle['paper_id'],
                'experiment_id': saved['experiment_id'], 'revision': saved['revision'],
                'status': saved['status'], 'bundle_fingerprint': bundle['fingerprint'],
                **report, 'missing_count': len(all_missing),
                'missing_units': all_missing[offset:offset + limit],
                'unreviewed_count': len(unreviewed),
                'unreviewed_units': unreviewed[offset:offset + limit],
                'unreviewed_math_count': len(math_pending),
                'unreviewed_math': math_pending[offset:offset + limit],
                'math_text_candidates': bundle.get('math_text_inventory', [])[offset:offset + limit],
                'next_offset': offset + limit if max(len(all_missing), len(unreviewed), len(math_pending)) > offset + limit else None}

    @staticmethod
    def bounds(offset, limit):
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 100:
            raise ValueError('Expected a nonnegative offset and a limit from one to 100')

    def task(self, session_id, source_unit_ids, context_radius=1):
        if (not isinstance(source_unit_ids, list) or not 1 <= len(source_unit_ids) <= 16
                or any(not isinstance(unit_id, str) for unit_id in source_unit_ids)):
            raise ValueError('Select from one to sixteen source units')
        bundle = self.current_bundle(session_id)
        packet = task_packet(bundle, source_unit_ids, context_radius)
        if sum(len(u['source_text']) for u in packet['targets']) > 60000:
            raise ValueError('Translation selection is too large; request a smaller batch')
        for block in packet['context_only']:
            block['figure_source_spans'] = bundle['figure_source_spans'].get(block['block_id'], [])
        saved = self.store.read(session_id, bundle['fingerprint'])
        selected = set(source_unit_ids)
        packet['existing_entries'] = [entry for entry in saved['result']['entries']
                                      if selected.intersection(entry['source_unit_ids'])]
        joined_ids = {unit_id for entry in packet['existing_entries']
                      if len(entry['source_unit_ids']) > 1 for unit_id in entry['source_unit_ids']}
        packet['existing_entry_source_units'] = [unit for unit in bundle['units'] if unit['id'] in joined_ids]
        packet['review_context'] = entry_review_context(bundle, saved['result'], sorted(selected | joined_ids))
        return packet

    def figure_task(self, session_id, block_id):
        bundle = self.current_bundle(session_id)
        figure = next((f for f in bundle['figures'] if f['block_id'] == block_id), None)
        if not figure:
            raise ValueError('Unknown figure')
        blocks = bundle['document']['blocks']
        figure_block = next(b for b in blocks if b['id'] == block_id)
        legacy_caption = figure_block['content'].get('caption_id')
        captions = [b['id'] for b in blocks if b['type'] == 'caption'
                    and (b['id'] == legacy_caption
                         or any(r['kind'] == 'caption_of' and r['target_id'] == block_id
                                for r in b.get('relations', [])))]
        saved = self.store.read(session_id, bundle['fingerprint'])
        existing = next((f for f in saved['result']['figure_reviews'] if f['block_id'] == block_id), None)
        return {'bundle_fingerprint': bundle['fingerprint'], 'figure': figure,
                'source_spans': bundle['figure_source_spans'].get(block_id, []),
                'caption_units': [u for u in bundle['units'] if u['block_id'] in captions],
                'coordinate_system': 'absolute PDF page points; top-left origin',
                'existing_review': existing,
                'labels_sha256_required': sha({'labels': existing['labels'],
                    'no_translatable_text_reason': existing['no_translatable_text_reason']}) if existing else None,
                'instruction': 'Compare the actual image. Source spans alone may miss labels. '
                               'Preserve the original diagram, identifiers and relationships. '
                               'Translate labels without explanations. Record individual source refs '
                               'or verified PDF-coordinate anchors. Do not claim unreadable glyphs were read.'}

    def figure_image(self, session_id, block_id):
        task = self.figure_task(session_id, block_id)
        path = Path(task['figure']['crop'])
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != task['figure']['crop_sha256']:
            raise ValueError('Source crop changed')
        return data

    def save(self, session_id, expected_revision, entries=None, figure_reviews=None,
             remove_entry_keys=None, math_overlays=None, math_roles=None):
        bundle = self.current_bundle(session_id)
        self.store.save(session_id, bundle['fingerprint'], expected_revision, entries,
                        figure_reviews, remove_entry_keys, math_overlays, math_roles)
        return self.report(session_id)

    def result(self, session_id, offset=0, limit=50):
        self.bounds(offset, limit)
        bundle = self.current_bundle(session_id)
        saved = self.store.read(session_id, bundle['fingerprint'])
        entries = saved['result']['entries']
        return {'session_id': session_id, 'revision': saved['revision'], 'status': saved['status'],
                'entries': [{**entry, 'entry_key': sha(entry['source_unit_ids']),
                             'review_requirements': entry_review_requirements(bundle, entry, saved['result'])}
                            for entry in entries[offset:offset + limit]],
                'next_offset': offset + limit if len(entries) > offset + limit else None}

    def finalize(self, session_id, expected_revision):
        bundle = self.current_bundle(session_id)
        self.store.finalize(session_id, bundle['fingerprint'], expected_revision)
        return self.report(session_id)

    def math_task(self, session_id, math_id):
        from .math_contract import review_requirements, source_math, text_sha
        from .math_overlay import inspect_latex
        bundle = self.current_bundle(session_id)
        original = source_math(bundle).get(math_id)
        if original is None:
            raise ValueError('Unknown source math identity')
        saved = self.store.read(session_id, bundle['fingerprint'])['result']
        overlays = [o for o in saved.get('math_overlays', []) if o['math_id'] == math_id]
        roles = [r for r in saved.get('math_roles', []) if r['math_id'] == math_id]
        return {'math_id': math_id, 'source_latex': original,
                'source_latex_sha256': text_sha(original), 'inspection': inspect_latex(original),
                'existing_overlays': [{**o, 'review_requirements': review_requirements(bundle, o, saved)} for o in overlays],
                'existing_roles': [{**r, 'review_requirements': review_requirements(bundle, r)} for r in roles],
                'instruction': 'Classify each text node from the actual paper context. Translate prose; '
                    'preserve experimental literal stimuli, identifiers and algorithm syntax. '
                    'Only explicitly selected pure prose nodes may change or split. '
                    'The server checks the remaining AST and renders both expressions. '
                    'Selected prose, numbers inside it, and the assembled sentence still require source comparison.'}
