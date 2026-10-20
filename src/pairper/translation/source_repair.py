"""Image-grounded source repair; never reuse translations or approve from hashes.

Only AI-scanned, independently reviewed LaTeX transcription corrections can create
a new derived view. The caller must restart translation from an empty experiment.
This module never modifies original PDFs, extracted snapshots, or old views.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Literal

import jsonschema
from pairper.document.models import Block, Document, MathRun, Region
from pairper.document.provenance import expected_refs, materialize_ranges
from pairper.document.text import resolve_text
from pairper.pdf.source import EXTRACTOR_VERSION, render
from pydantic import BaseModel, ConfigDict, Field

from .math_contract import source_math
from .prepare import assert_current, current_source
from .prompts import response_schema, validate_protocol


class SourceFinding(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    kind: Literal['math_latex', 'unresolved']
    source_id: str
    current: str
    proposed: str
    reason: str = Field(min_length=1)
    evidence_pages: list[int] = Field(min_length=1)


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _math_location(bundle, math_id, *, verify=True):
    """Return the exact block dict, mutable LaTeX container, and original region."""
    blocks = {block['id']: block for block in bundle['document']['blocks']}
    if math_id.startswith('block:') and math_id.endswith('/latex'):
        block = blocks[math_id[6:-6]]
        if block['type'] != 'equation':
            raise ValueError('Standalone math ID is not an equation')
        return block, block['content'], block['content']['region']
    unit = next((unit for unit in bundle['units'] if any(m['token'] == math_id for m in unit['math'])), None)
    if unit is None:
        raise ValueError('Unknown source math ID')
    index = next(i for i, math in enumerate(unit['math']) if math['token'] == math_id)
    block = blocks[unit['block_id']]
    field = unit['field'].split('/')
    if field == ['runs']:
        runs = block['content']['runs']
    elif len(field) == 2 and field[0] in {'cells', 'code_lines', 'items'}:
        element = block['content'][field[0]][int(field[1])]
        runs = element if field[0] == 'items' else element['runs']
    else:
        raise ValueError('Unsupported source unit field')
    maths = [run for run in runs if run['kind'] == 'math']
    run = maths[index]
    if verify and run['latex'] != unit['math'][index]['latex']:
        raise ValueError('Source math disagrees with document MathRun')
    return block, run, run['region']


class SourceRepairer:
    def __init__(self, library, runner, artifact_dir):
        self.library = library
        self.runner = runner
        self.artifact_dir = Path(artifact_dir).resolve()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self._scans = {}

    def _images(self, bundle, pages, directory, math_ids=()):
        paper, source, _, _ = current_source(self.library, bundle['paper_id'])
        directory.mkdir(parents=True, exist_ok=True)
        images, manifest = [], []
        for page in sorted(set(pages)):
            if not isinstance(page, int) or isinstance(page, bool) or page < 1:
                raise ValueError('Invalid original PDF page')
            path = directory / f'original-page-{page}.png'
            path.write_bytes(render(self.library.root / paper['original_path'], source, page, scale=1.5))
            images.append(str(path))
            manifest.append({'path': str(path), 'page': page, 'sha256': _sha(path)})
        # Full pages retain surrounding claims; original-coordinate crops make
        # small subscripts/glyphs inspectable without a model using tools.
        for index, math_id in enumerate(sorted(set(math_ids))):
            region = Region.model_validate(_math_location(bundle, math_id)[2])
            path = directory / f'original-math-{index}.png'
            path.write_bytes(render(self.library.root / paper['original_path'], source,
                                    region.page, region=region, scale=4))
            images.append(str(path))
            manifest.append({'path': str(path), 'page': region.page, 'sha256': _sha(path),
                             'math_id': math_id, 'region': region.model_dump(mode='json')})
        if not images:
            raise ValueError('Source inspection requires original PDF pages')
        return images, manifest

    def _run(self, request, images, directory):
        schema = response_schema(request)
        response = self.runner.run(request, schema, images, directory)
        # Also validate adapters used in tests or non-Codex implementations.
        jsonschema.validate(response, schema)
        validate_protocol(request, response, image_count=len(images))
        return response

    def _source_run_evidence(self, bundle, unit_ids):
        _, snapshot, _, _ = current_source(self.library, bundle['paper_id'])
        spans = {span['id']: span for page in snapshot['pages'] for span in page['spans']}
        blocks = {b.id: b for b in Document.model_validate(bundle['document']).blocks}
        evidence = []
        for unit in bundle['units']:
            if unit['id'] not in unit_ids:
                continue
            field = unit['field'].split('/')
            content = blocks[unit['block_id']].content
            element = getattr(content, field[0])
            if len(field) == 2:
                element = element[int(field[1])]
            runs = element if field[0] in {'runs', 'items'} else element.runs
            items = []
            for index, run in enumerate(runs):
                item = {'run_index': index, 'kind': run.kind, 'refs': list(run.refs)}
                if isinstance(run, MathRun):
                    item['latex'] = run.latex
                else:
                    item['text'] = resolve_text(run, spans, [])
                    item.update({name: True for name in ('superscript', 'bold', 'italic', 'underline', 'literal') if getattr(run, name)})
                items.append(item)
            evidence.append({'unit_id': unit['id'], 'runs': items})
        return evidence

    def scan(self, bundle, task, notes, images) -> list[SourceFinding]:
        assert_current(self.library, bundle)
        originals = source_math(bundle)
        unit_math = {unit['id']: [m['token'] for m in unit['math']] for unit in bundle['units']}
        # Scope is selected task units/math, plus explicit current source_math.
        unit_ids = set(task.get('source_unit_ids', []))
        selected_ids = {u.get('id', u.get('unit_id')) for key in ('targets', 'units') for u in task.get(key, [])}
        unit_ids.update(selected_ids)
        scope = {m['token'] for unit in bundle['units'] if unit['id'] in unit_ids for m in unit['math']}
        scope.update(m['math_id'] for m in task.get('math_items', []))
        supplied = task.get('source_math', {})
        if isinstance(supplied, dict):
            for key, value in supplied.items():
                if originals.get(key) != value:
                    raise ValueError('Task source_math is unknown or stale')
            scope.update(supplied)
        def context_math(value):
            if isinstance(value, str):
                scope.update(unit_math.get(value, []))
                if value in originals:
                    scope.add(value)
            elif isinstance(value, list):
                for item in value:
                    context_math(item)
            elif isinstance(value, dict):
                for name in ('math_id', 'token'):
                    if isinstance(value.get(name), str) and value[name] in originals:
                        scope.add(value[name])
                for key, item in value.items():
                    if key in originals:
                        if isinstance(item, str) and item != originals[key]:
                            raise ValueError('Context source_math is stale')
                        scope.add(key)
                    if key in {'id', 'block_id'} and isinstance(item, str) and f'block:{item}/latex' in originals:
                        scope.add(f'block:{item}/latex')
                    context_math(item)
        context_math(task.get('context', {}))
        if not scope <= originals.keys():
            raise ValueError('Task requests unknown source math')
        note_text = json.dumps(notes, ensure_ascii=False)
        named_math = {key for key in scope if key in note_text or key.strip('⟦⟧') in note_text}
        note_without_math_ids = note_text
        for key in named_math:
            note_without_math_ids = note_without_math_ids.replace(key, '').replace(key.strip('⟦⟧'), '')
        named_units = {unit['id'] for unit in bundle['units'] if unit['id'] in note_without_math_ids}
        if named_units or named_math:
            # A routing note naming one paragraph is not a whole-page audit.
            # Explicit context math remains eligible when the note names it.
            scope = named_math | {m for uid in named_units for m in unit_math[uid]}
            unit_ids = named_units | {unit['id'] for unit in bundle['units'] if any(m['token'] in scope for m in unit['math'])}
        else:
            unit_ids.update(unit['id'] for unit in bundle['units'] if any(m['token'] in scope for m in unit['math']))
        pages = {page for unit in bundle['units'] if unit['id'] in unit_ids for page in unit['pages']}
        pages.update(_math_location(bundle, key)[2]['page'] for key in scope)
        # Tasks without math can still discover a blocking nonmath transcription error.
        pages.update(p for p in task.get('pages', []) if isinstance(p, int))
        directory = self.artifact_dir / f'scan-{uuid.uuid4().hex}'
        directory.mkdir()
        actual_images, manifest = self._images(bundle, pages, directory / 'images', scope)
        request = {'phase': 'source_scan', 'kind': 'math', 'task': copy.deepcopy(task),
                   'source_math': {key: originals[key] for key in sorted(scope)},
                   'notes_to_investigate': copy.deepcopy(notes),
                   'context': copy.deepcopy(task.get('context', {})), 'original_page_images': manifest,
                   'source_run_evidence': self._source_run_evidence(bundle, unit_ids)}
        response = self._run(request, actual_images, directory / 'agent')
        findings = [SourceFinding.model_validate(item) for item in response['findings']]
        for finding in findings:
            if finding.kind == 'math_latex':
                region = _math_location(bundle, finding.source_id)[2]
                if region['page'] not in finding.evidence_pages:
                    raise ValueError('Finding omits the actual source math page')
        record = {'bundle_fingerprint': bundle['fingerprint'], 'request': request, 'response': response,
                  'input_image_count': len(images), 'input_images_used_as_PDF_truth': False,
                  'original_images_rendered_from_current_PDF': manifest}
        _write(directory / 'scan.json', record)
        self._scans[bundle['fingerprint']] = {'findings': [f.model_dump() for f in findings],
                                            'directory': directory, 'task': copy.deepcopy(task),
                                            'source_run_evidence': request['source_run_evidence']}
        return findings

    def repair(self, bundle, findings):
        assert_current(self.library, bundle)
        findings = [SourceFinding.model_validate(f) for f in findings]
        scanned = self._scans.get(bundle['fingerprint'])
        if not scanned or [f.model_dump() for f in findings] != scanned['findings']:
            raise ValueError('Repairs require exact findings from this instance independent AI scan')
        if not findings or any(f.kind != 'math_latex' for f in findings):
            raise ValueError('No automatic repair for empty or unresolved source findings')
        originals = source_math(bundle)
        for finding in findings:
            if originals.get(finding.source_id) != finding.current:
                raise ValueError('Source finding is stale')
        directory = self.artifact_dir / f'repair-{uuid.uuid4().hex}'
        directory.mkdir()
        images, manifest = self._images(bundle, [p for f in findings for p in f.evidence_pages],
                                        directory / 'images', [f.source_id for f in findings])
        request = {'phase': 'review', 'kind': 'math', 'source_repair_review': True,
                   'source_math': {f.source_id: f.current for f in findings},
                   'source_run_evidence': copy.deepcopy(scanned['source_run_evidence']),
                   'task': copy.deepcopy(scanned['task']), 'original_page_images': manifest,
                   'context': copy.deepcopy(scanned['task'].get('context', {})),
                   'review_items': [{'item_id': f.source_id, 'source': f.current, 'target': f.proposed,
                                     'math_id': f.source_id, 'source_unit_ids': []} for f in findings]}
        # Reviewer receives current/proposed plus original images, no scanner's verdict/reason.
        response = self._run(request, images, directory / 'reviewer')
        _write(directory / 'review.json', {'request': request, 'response': response})
        if response['issues'] or any(item['verdict'] != 'pass' for item in response['items']):
            raise ValueError('Independent source repair reviewer did not pass every proposal')
        assert_current(self.library, bundle)
        paper, source_snapshot, document, identity = current_source(self.library, bundle['paper_id'])
        if document.model_dump(mode='json') != bundle['document']:
            raise ValueError('Bundle document is not the current complete source document')
        if source_snapshot.get('extractor_version', 1) < EXTRACTOR_VERSION:
            raise ValueError('Source snapshot needs migration; refuse Library.begin automatic source refresh')
        # SQLite backup includes live WAL contents; never copy only the main DB file.
        with closing(sqlite3.connect(self.library.db.as_uri() + '?mode=ro', uri=True)) as source:
            if source.execute("SELECT 1 FROM jobs WHERE paper_id=? AND stage!='finalized'", (bundle['paper_id'],)).fetchone():
                raise ValueError('An active source build exists; do not resume or merge it')
            with closing(sqlite3.connect(directory / 'library-before.db')) as backup:
                source.backup(backup)
        protected = [self.library.root / paper['original_path'],
                     self.library.root / 'sources' / f'{paper["id"]}.json',
                     self.library.root / 'views' / f'{paper["current_view_id"]}.json']
        before = {str(path): _sha(path) for path in protected}
        for index, path in enumerate(protected):
            (directory / f'preserved-{index}{path.suffix}').write_bytes(path.read_bytes())
        modified = copy.deepcopy(bundle)
        changed_blocks = {}
        for finding in findings:
            block, container, _ = _math_location(modified, finding.source_id)
            container['latex'] = finding.proposed
            changed_blocks[block['id']] = block
        # Assert all other fields including source_verified/refs/order remain exact.
        check = copy.deepcopy(modified)
        for finding in findings:
            _math_location(check, finding.source_id, verify=False)[1]['latex'] = finding.current
        if check != bundle:
            raise ValueError('Source repair changed fields outside exact LaTeX containers')
        replacement_blocks = [Block.model_validate(b) for b in changed_blocks.values()]
        for block in replacement_blocks:
            before_append = block.model_dump(mode='json')
            materialize_ranges(block, source_snapshot)
            if not block.source_refs:
                block.source_refs = expected_refs(block, source_snapshot)
            if block.model_dump(mode='json') != before_append:
                raise ValueError('Library.append would alter source references outside LaTeX')
        assert_current(self.library, bundle)
        job = self.library.begin(bundle['paper_id'], bundle['source_pdf_sha256'], revise_existing=True)
        if job.get('resumed'):
            raise ValueError('Concurrent source build appeared; refusing resumed job')
        self.library.append(job['job_id'], replacement_blocks, [], [])
        report = self.library.validate(job['job_id'], finalize=True)
        after = {str(path): _sha(path) for path in protected}
        result = {'report': report, 'old_identity': identity, 'protected_before': before,
                  'protected_after': after, 'preserved': before == after,
                  'findings': [f.model_dump() for f in findings],
                  'requires_new_empty_translation_experiment': True, 'artifact_dir': str(directory)}
        _write(directory / 'application.json', result)
        if before != after:
            raise RuntimeError('Source repair violated original/source/old-view immutability')
        if not report.get('valid') or not report.get('view_id'):
            raise ValueError('New source view did not validate/finalize; previous view remains current')
        _, _, saved, _ = current_source(self.library, bundle['paper_id'])
        if saved.model_dump(mode='json') != modified['document']:
            raise RuntimeError('Saved source repair changed fields outside proposed LaTeX')
        return result
