"""Read-only dry plan by default; --apply installs verified workflow evidence only."""
import argparse
import json
import sqlite3
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from pairper.explanation.paper_context import PaperContextWorkflow as SelectionWorkflow
from pairper.explanation.projection import _fields
from pairper.explanation.service import ExplanationService
from pairper.explanation.store import ExplanationStore
from pairper.explanation.workflow_archive import import_workflow
from pairper.storage.library import Library
from pairper.translation.service import TranslationService
from pairper.translation.store import TranslationStore


class ReadOnlyConnection:
    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db.resolve().as_uri() + '?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()


class ReadOnlyLibrary(ReadOnlyConnection, Library):
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.db = self.root / 'library.db'

    def source(self, paper_id):
        return json.loads((self.root / 'sources' / (paper_id + '.json')).read_text('utf-8'))

    def asset(self, paper_id, region):
        # A workflow packet needs display provenance, not cache generation.
        return uuid.uuid5(uuid.NAMESPACE_URL, paper_id + region.model_dump_json()).hex


class ReadOnlyExplanationStore(ReadOnlyConnection, ExplanationStore):
    def __init__(self, directory):
        self.directory = Path(directory)
        self.db = self.directory / 'explanations.db'


class ReadOnlyTranslationStore(ReadOnlyConnection, TranslationStore):
    def __init__(self, directory):
        self.directory = Path(directory)
        self.db = self.directory / 'translations.db'


class ReadOnlyService(ExplanationService):
    def __init__(self, root):
        self.library = ReadOnlyLibrary(root)
        self.directory = self.library.root / 'explanations'
        self.store = ReadOnlyExplanationStore(self.directory)
        self._translation_surface_cache = {}

    def translation_surfaces(self, paper_id, bundle=None):
        # Same reader projection as the normal API, with constructors/DDL disabled.
        current = self.prepare(paper_id)
        if bundle is not None and (bundle['paper_id'] != paper_id or
                                   bundle['fingerprint'] != current['fingerprint']):
            raise ValueError('Source changed before translation anchoring')
        translations = TranslationService.__new__(TranslationService)
        translations.library = self.library
        translations.directory = self.library.root / 'translations'
        translations.store = ReadOnlyTranslationStore(translations.directory)
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
        known |= {f'{t["block_id"]}/{t["field"]}' for t in current['targets'] if t['field'] != 'latex'}
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', required=True)
    parser.add_argument('--source-state', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    service = ReadOnlyService(args.library)
    workflow = SelectionWorkflow.__new__(SelectionWorkflow)
    workflow.service, workflow.runner = service, None
    workflow.directory = service.directory / 'workflows'
    workflow.max_repairs = 2
    print(json.dumps(import_workflow(workflow, args.source_state, apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
