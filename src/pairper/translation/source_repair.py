"""Image-grounded source repair; never reuse translations or approve from hashes.

Only AI-scanned, independently reviewed source transcription corrections can create
a new derived view. The caller must restart translation from an empty experiment.
This module never modifies original PDFs, extracted snapshots, or old views.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
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

from .contract import sha
from .math_contract import source_math
from .prepare import assert_current, current_source
from .prompts import response_schema, validate_protocol
from .source_boundary_repair import apply_boundary_transfers, normalize_boundary_transfer
from .source_figure_evidence import figure_label_regions, pack_label_evidence
from .source_figure_glyph_evidence import build_figure_label_glyph_evidence
from .source_glyph_evidence import build_source_glyph_evidence
from .source_run_repair import (
    apply_run_groups,
    canonical_run_json,
    normalize_run_group,
    resolve_run_group,
)


class SourceFinding(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    kind: Literal['math_latex', 'run_group', 'math_boundary_transfer', 'unresolved']
    source_id: str
    current: str
    proposed: str
    reason: str = Field(min_length=1)
    evidence_pages: list[int] = Field(min_length=1)


class SourceRepairBlocked(ValueError):
    """A bounded source-quality stop, never an infrastructure retry request."""
    def __init__(self, reason, details):
        super().__init__(reason)
        self.details = copy.deepcopy(details)


class _SourceReviewRejected(ValueError):
    def __init__(self, directory, response):
        super().__init__('Independent source repair reviewer did not pass every proposal')
        self.directory = directory
        self.response = copy.deepcopy(response)


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

    def _images(self, bundle, pages, directory, math_ids=(), extra_regions=(), label_regions=()):
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
        crop_regions = [(math_id, _math_location(bundle, math_id)[2]) for math_id in sorted(set(math_ids))]
        crop_regions.extend(extra_regions)
        for index, (math_id, original_region) in enumerate(crop_regions):
            region = Region.model_validate(original_region)
            path = directory / f'original-math-{index}.png'
            path.write_bytes(render(self.library.root / paper['original_path'], source,
                                    region.page, region=region, scale=4))
            images.append(str(path))
            anchor = ({'block_id': math_id[len('figure:'):]} if math_id.startswith('figure:')
                      else {'math_id': math_id})
            manifest.append({'path': str(path), 'page': region.page, 'sha256': _sha(path),
                             **anchor, 'region': region.model_dump(mode='json')})
        if label_regions:
            packed = pack_label_evidence(self.library.root / paper['original_path'], source,
                                         label_regions, directory / 'label-atlases',
                                         base_image_count=len(images))
            images.extend(packed['images'])
            manifest.extend(packed['manifest']['atlas_images'])
            # Preserve per-label coordinates/pixel proofs and explicit omissions.
            manifest.append({'figure_label_atlas_manifest': packed['manifest']})
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

    def _glyph_evidence(self, bundle, unit_ids):
        paper, source, _, _ = current_source(self.library, bundle['paper_id'])
        # Scope has already been narrowed to note-named units where possible.
        # Within that scope, literal/code rows must not be crowded out by the
        # first eight prose units in a large translation task.
        selected = [u for u in bundle['units'] if u['id'] in unit_ids]
        def priority(unit):
            repeated = re.search(r'([^\w\s])\1{2,}', ''.join(unit.get('source_text', '').split()))
            literal = unit.get('field', '').startswith('code_lines/') or unit.get('type') == 'code' or repeated
            return (not literal, unit.get('order', 0), unit['id'])
        selected.sort(key=priority)
        return build_source_glyph_evidence(
            bundle, [u['id'] for u in selected], pdf_path=self.library.root / paper['original_path'],
            source_snapshot=source,
            source_snapshot_path=self.library.root / 'sources' / f'{bundle["paper_id"]}.json',
            view_path=self.library.root / 'views' / f'{bundle["view_id"]}.json')

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
            evidence.append({'unit_id': unit['id'], 'runs': items,
                             'original_run_indices': list(range(len(runs))),
                             'current_raw_runs_json': canonical_run_json([run.model_dump(mode='json') for run in runs]),
                             'selector_format': unit['id'] + '/range/START:END (zero-based, END EXCLUSIVE)'})
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
        # A full math ID names its owning source unit even after the ID was
        # removed above. Inspect that unit's other current math too, matching
        # the whole-unit run evidence already supplied (e.g. M0 and "#M1").
        # No bare shorthand resolves an owner or expands to another unit/page.
        named_units.update(uid for uid, maths in unit_math.items() if named_math.intersection(maths))
        if named_units or named_math:
            # A routing note naming one paragraph is not a whole-page audit.
            # Explicit context math remains eligible when the note names it.
            scope = named_math | {m for uid in named_units for m in unit_math[uid]}
            unit_ids = named_units | {unit['id'] for unit in bundle['units'] if any(m['token'] in scope for m in unit['math'])}
        else:
            unit_ids.update(unit['id'] for unit in bundle['units'] if any(m['token'] in scope for m in unit['math']))
        pages = {page for unit in bundle['units'] if unit['id'] in unit_ids for page in unit['pages']}
        pages.update(_math_location(bundle, key)[2]['page'] for key in scope)
        figure_regions = []
        if task.get('figure'):
            figure = task['figure']
            blocks = [block for block in bundle['document']['blocks']
                      if block['id'] == figure.get('block_id') and block['type'] == 'figure']
            if len(blocks) != 1:
                raise ValueError('Unknown current source figure for inspection')
            region = Region.model_validate(blocks[0]['content']['region'])
            if Region.model_validate(figure.get('region')) != region:
                raise ValueError('Figure region differs from current source')
            if figure.get('paper_id', bundle['paper_id']) != bundle['paper_id']:
                raise ValueError('Figure belongs to another source paper')
            pages.add(region.page)
            figure_regions.append(('figure:' + blocks[0]['id'], region.model_dump(mode='json')))
        # Tasks without math can still discover a blocking nonmath transcription error.
        pages.update(p for p in task.get('pages', []) if isinstance(p, int))
        directory = self.artifact_dir / f'scan-{uuid.uuid4().hex}'
        directory.mkdir()
        label_evidence = None
        if task.get('figure') and task.get('current_translation_candidate') is not None:
            _, snapshot, _, _ = current_source(self.library, bundle['paper_id'])
            label_evidence = figure_label_regions(bundle, task, task['current_translation_candidate'],
                                                   source_snapshot=snapshot)
        actual_images, manifest = self._images(bundle, pages, directory / 'images', scope, figure_regions,
                                               label_evidence['regions'] if label_evidence else ())
        source_task = copy.deepcopy(task)
        scan_only = {key: source_task.pop(key) for key in
                     ('candidate_to_inspect', 'current_translation_candidate', 'figure_output_evidence')
                     if key in source_task}
        request = {'phase': 'source_scan', 'kind': 'math', 'task': source_task,
                   'source_math': {key: originals[key] for key in sorted(scope)},
                   'notes_to_investigate': copy.deepcopy(notes),
                   'context': copy.deepcopy(task.get('context', {})), 'original_page_images': manifest,
                   'source_run_evidence': self._source_run_evidence(bundle, unit_ids),
                   'source_glyph_evidence': self._glyph_evidence(bundle, unit_ids)}
        request.update(scan_only)
        if task.get('figure'):
            paper = self.library.paper(bundle['paper_id'])
            request['figure_label_glyph_evidence'] = build_figure_label_glyph_evidence(
                bundle, task, task.get('current_translation_candidate'),
                pdf_path=self.library.root / paper['original_path'],
                source_snapshot_path=self.library.root / 'sources' / f"{bundle['paper_id']}.json",
                view_path=self.library.root / 'views' / f"{bundle['view_id']}.json")
        if label_evidence is not None:
            packed_manifest = next((m['figure_label_atlas_manifest'] for m in manifest
                                    if 'figure_label_atlas_manifest' in m), {})
            request['figure_label_evidence'] = {**copy.deepcopy(label_evidence['manifest']),
                **copy.deepcopy(packed_manifest),
                'omissions': label_evidence['manifest']['omissions'] + packed_manifest.get('omissions', []),
                'omitted_label_ids': label_evidence['manifest']['omitted_label_ids'] + packed_manifest.get('omitted_label_ids', [])}
        response = self._run(request, actual_images, directory / 'agent')
        findings = [SourceFinding.model_validate(item) for item in response['findings']]
        _, source_snapshot, _, _ = current_source(self.library, bundle['paper_id'])
        group_audits = []
        boundary_audits = []
        for finding in findings:
            if finding.kind == 'math_latex':
                region = _math_location(bundle, finding.source_id)[2]
                if region['page'] not in finding.evidence_pages:
                    raise ValueError('Finding omits the actual source math page')
            elif finding.kind == 'run_group':
                resolved = resolve_run_group(bundle, finding.source_id)
                if resolved['unit_id'] not in unit_ids:
                    raise ValueError('Run group finding is outside inspected source unit scope')
                normalized = normalize_run_group(bundle, finding, source_snapshot)
                if not set(normalized['source_pages']) <= set(finding.evidence_pages):
                    raise ValueError('Finding omits original run group source pages')
                group_audits.append(normalized)
            elif finding.kind == 'math_boundary_transfer':
                if finding.source_id not in scope:
                    raise ValueError('Boundary receiving equation is outside inspected math scope')
                normalized = normalize_boundary_transfer(bundle, finding, source_snapshot=source_snapshot)
                if normalized['donor_unit_id'] not in unit_ids:
                    raise ValueError('Boundary donor is outside inspected source unit scope')
                if not set(normalized['source_pages']) <= set(finding.evidence_pages):
                    raise ValueError('Boundary finding omits original PDF page evidence')
                boundary_audits.append(normalized)
        self._check_overlaps(bundle, findings)
        groups = [f for f in findings if f.kind == 'run_group']
        if groups:
            apply_run_groups(bundle, groups, source_snapshot)  # Atomic whole-range gates before review.
        if boundary_audits:
            apply_boundary_transfers(bundle['document'], boundary_audits)
        record = {'bundle_fingerprint': bundle['fingerprint'], 'request': request, 'response': response,
                  'input_image_count': len(images), 'input_images_used_as_PDF_truth': False,
                  'original_images_rendered_from_current_PDF': manifest,
                  'normalized_run_groups': group_audits,
                  'normalized_boundary_transfers': boundary_audits}
        _write(directory / 'scan.json', record)
        self._scans[bundle['fingerprint']] = {'findings': [f.model_dump() for f in findings],
                                            'directory': directory, 'task': copy.deepcopy(source_task),
                                            'source_run_evidence': request['source_run_evidence'],
                                            'source_glyph_evidence': request['source_glyph_evidence'],
                                            'notes': copy.deepcopy(response['notes']),
                                            'notes_to_investigate': copy.deepcopy(notes),
                                            'manifest': copy.deepcopy(manifest),
                                            'scan_response_sha256': sha(response)}
        return findings

    def get_scan_feedback(self, bundle, task_id=None) -> dict | None:
        """Return a detached observation snapshot, never a review approval.

        Exact bundle binding prevents observations from another source identity
        being presented as current evidence. Persistent storage belongs to the
        workflow; this accessor performs no library or artifact writes.
        """
        record = self._scans.get(bundle['fingerprint'])
        if record is None:
            return None
        actual_task_id = record['task'].get('task_id')
        if task_id is not None and task_id != actual_task_id:
            return None
        return {'bundle_fingerprint': bundle['fingerprint'],
                'task_id': actual_task_id,
                'notes': copy.deepcopy(record['notes']),
                'findings': copy.deepcopy(record['findings']),
                'artifact_dir': str(record['directory']),
                'scan_response_sha256': record['scan_response_sha256']}

    @staticmethod
    def _check_overlaps(bundle, findings):
        groups = [resolve_run_group(bundle, f.source_id) for f in findings if f.kind == 'run_group']
        boundaries = [f for f in findings if f.kind == 'math_boundary_transfer']
        claimed_blocks = set()
        units = {u['id']: u for u in bundle['units']}
        for boundary in boundaries:
            current = json.loads(boundary.current)
            donor_id = current['donor_unit_id']
            donor_block = units[donor_id]['block_id']
            receiver_block = boundary.source_id[6:-6]
            if {donor_block, receiver_block} & claimed_blocks:
                raise ValueError('Boundary transfers overlap a donor or receiving block')
            claimed_blocks.update((donor_block, receiver_block))
        for finding in findings:
            if finding.kind == 'math_latex':
                block_id = _math_location(bundle, finding.source_id)[0]['id']
                if block_id in claimed_blocks:
                    raise ValueError('Math LaTeX finding overlaps a boundary transfer block')
        if any(g['block_id'] in claimed_blocks for g in groups):
            raise ValueError('Run group finding overlaps a boundary transfer block')
        for finding in findings:
            if finding.kind != 'math_latex' or finding.source_id.startswith('block:'):
                continue
            unit = next(u for u in bundle['units'] if any(m['token'] == finding.source_id for m in u['math']))
            _, container, _ = _math_location(bundle, finding.source_id)
            block = next(b for b in bundle['document']['blocks'] if b['id'] == unit['block_id'])
            field = unit['field'].split('/')
            element = block['content'][field[0]] if len(field) == 1 else block['content'][field[0]][int(field[1])]
            runs = element if field[0] in {'runs', 'items'} else element['runs']
            position = next(i for i, run in enumerate(runs) if run is container)
            if any(g['unit_id'] == unit['id'] and g['start'] <= position < g['end'] for g in groups):
                raise ValueError('Math LaTeX finding overlaps an original run group range')

    def repair(self, bundle, findings):
        """At most three complete proposals; only fresh source review permits apply.

        Reviewer feedback goes to a new source scan, never into another review's
        task/context. No partial batch is applied and no issue may disappear.
        """
        findings = [SourceFinding.model_validate(f) for f in findings]
        scanned = self._scans.get(bundle['fingerprint'])
        if not scanned or [f.model_dump() for f in findings] != scanned['findings']:
            raise ValueError('Repairs require exact findings from this instance independent AI scan')
        original_task = copy.deepcopy(scanned['task'])
        original_notes = copy.deepcopy(scanned['notes_to_investigate'])
        required_ids = {f.source_id for f in findings}
        chain_dir = self.artifact_dir / f'proposal-chain-{uuid.uuid4().hex}'
        chain_dir.mkdir()
        chain_path = chain_dir / 'chain.json'
        chain = {'bundle_fingerprint': bundle['fingerprint'], 'max_proposals': 3,
                 'attempts': [], 'status': 'reviewing', 'translation_approval_granted': False}

        def blocked(reason):
            chain.update(status='blocked', reason=reason)
            _write(chain_path, chain)
            raise SourceRepairBlocked(reason, {
                'bundle_fingerprint': bundle['fingerprint'], 'reason': reason,
                'source_issue_ids': sorted(required_ids), 'source_proposal_chain': str(chain_path),
                'source_proposal_chain_sha256': _sha(chain_path),
                'proposal_attempts': len(chain['attempts']), 'library_write_permitted': False})

        for number in range(1, 4):
            assert_current(self.library, bundle)
            current_scan = self._scans[bundle['fingerprint']]
            entry = {'proposal_number': number,
                     'scan_artifact': str(current_scan['directory'] / 'scan.json'),
                     'scan_artifact_sha256': _sha(current_scan['directory'] / 'scan.json'),
                     'scan_response_sha256': current_scan['scan_response_sha256'],
                     'findings_sha256': sha([f.model_dump() for f in findings])}
            try:
                result = self._repair_once(bundle, findings)
            except _SourceReviewRejected as rejection:
                review_path = rejection.directory / 'review.json'
                entry.update(outcome='rejected_unapplied', review_artifact=str(review_path),
                             review_artifact_sha256=_sha(review_path),
                             review_response_sha256=sha(rejection.response))
                chain['attempts'].append(entry)
                _write(chain_path, chain)
                if number == 3:
                    blocked('Independent source repair reviewer did not pass every proposal; source proposal limit exhausted')
                feedback = {'type': 'PROGRAM_SOURCE_PROPOSAL_REVISION',
                            'previous_findings_are_UNAPPLIED': True,
                            'required_source_issue_ids': sorted(required_ids),
                            'previous_unapplied_findings': [f.model_dump() for f in findings],
                            'independent_review_feedback': rejection.response,
                            'review_artifact': str(review_path),
                            'review_response_sha256': sha(rejection.response)}
                notes = original_notes + [json.dumps(feedback, ensure_ascii=False, sort_keys=True)]
                try:
                    findings = self.scan(bundle, original_task, notes, [])
                except ValueError as exc:
                    blocked('Source proposal revision failed current-source validation: ' + str(exc))
                revised_scan = self._scans[bundle['fingerprint']]
                entry.update(revision_scan_artifact=str(revised_scan['directory'] / 'scan.json'),
                             revision_scan_artifact_sha256=_sha(revised_scan['directory'] / 'scan.json'),
                             revision_scan_response_sha256=revised_scan['scan_response_sha256'])
                if not required_ids <= {f.source_id for f in findings}:
                    blocked('Source proposal revision dropped a previously identified source issue')
                required_ids.update(f.source_id for f in findings)
                if any(f.kind == 'unresolved' for f in findings):
                    blocked('Source proposal revision remains unresolved')
                continue
            review_path = Path(result['artifact_dir']) / 'review.json'
            entry.update(outcome='applied_after_fresh_review', review_artifact=str(review_path),
                         review_artifact_sha256=_sha(review_path))
            chain['attempts'].append(entry)
            chain['status'] = 'applied'
            _write(chain_path, chain)
            result.update(source_proposal_chain=str(chain_path), source_proposal_chain_sha256=_sha(chain_path),
                          source_proposal_attempts=len(chain['attempts']))
            _write(Path(result['artifact_dir']) / 'application.json', result)
            return result

    def _repair_once(self, bundle, findings):
        assert_current(self.library, bundle)
        findings = [SourceFinding.model_validate(f) for f in findings]
        scanned = self._scans.get(bundle['fingerprint'])
        if not scanned or [f.model_dump() for f in findings] != scanned['findings']:
            raise ValueError('Repairs require exact findings from this instance independent AI scan')
        if not findings or any(f.kind == 'unresolved' for f in findings):
            raise ValueError('No automatic repair for empty or unresolved source findings')
        originals = source_math(bundle)
        _, source_snapshot, _, _ = current_source(self.library, bundle['paper_id'])
        raw_findings = [f.model_dump() for f in findings]
        normalized_groups = []
        normalized_boundaries = []
        normalized_findings = []
        for finding in findings:
            if finding.kind == 'math_latex':
                if originals.get(finding.source_id) != finding.current:
                    raise ValueError('Source finding is stale')
                normalized_findings.append(finding)
            elif finding.kind == 'run_group':
                normalized = normalize_run_group(bundle, finding, source_snapshot)
                normalized_groups.append(normalized)
                normalized_findings.append(finding.model_copy(update={'current': normalized['current'], 'proposed': normalized['proposed']}))
            elif finding.kind == 'math_boundary_transfer':
                normalized = normalize_boundary_transfer(bundle, finding, source_snapshot=source_snapshot)
                normalized_boundaries.append(normalized)
                normalized_findings.append(finding.model_copy(update={'current': normalized['current'], 'proposed': normalized['proposed']}))
        self._check_overlaps(bundle, findings)
        findings = normalized_findings
        directory = self.artifact_dir / f'repair-{uuid.uuid4().hex}'
        directory.mkdir()
        images, manifest = self._images(bundle, [p for f in findings for p in f.evidence_pages],
                                        directory / 'images', [f.source_id for f in findings if f.kind == 'math_latex'],
                                        [(f.source_id + f'@math{index}', run['region'])
                                         for f in findings if f.kind == 'run_group'
                                         for index, run in enumerate(json.loads(f.proposed)) if run['kind'] == 'math']
                                        + [(n['source_id'] + '@boundary-union',
                                            json.loads(n['proposed'])['receiver']['content']['region'])
                                           for n in normalized_boundaries])
        request = {'phase': 'review', 'kind': 'math', 'source_repair_review': True,
                   'source_math': {f.source_id: f.current for f in findings if f.kind == 'math_latex'},
                   'source_run_evidence': copy.deepcopy(scanned['source_run_evidence']),
                   'source_glyph_evidence': self._glyph_evidence(bundle, {
                       row['unit_id'] for row in scanned['source_run_evidence']}),
                   'task': copy.deepcopy(scanned['task']), 'original_page_images': manifest,
                   'context': copy.deepcopy(scanned['task'].get('context', {})),
                   'review_items': [{'item_id': f.source_id, 'source': f.current, 'target': f.proposed,
                                     'math_id': f.source_id, 'source_unit_ids': []} for f in findings]}
        # Reviewer receives current/proposed plus original images, no scanner's verdict/reason.
        response = self._run(request, images, directory / 'reviewer')
        _write(directory / 'review.json', {'request': request, 'response': response,
                                         'raw_scan_findings': raw_findings, 'normalized_run_groups': normalized_groups,
                                         'normalized_boundary_transfers': normalized_boundaries})
        if response['issues'] or any(item['verdict'] != 'pass' for item in response['items']):
            raise _SourceReviewRejected(directory, response)
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
            if finding.kind != 'math_latex':
                continue
            block, container, _ = _math_location(modified, finding.source_id)
            container['latex'] = finding.proposed
            changed_blocks[block['id']] = block
        # Assert all other fields including source_verified/refs/order remain exact.
        check = copy.deepcopy(modified)
        for finding in findings:
            if finding.kind == 'math_latex':
                _math_location(check, finding.source_id, verify=False)[1]['latex'] = finding.current
        if check != bundle:
            raise ValueError('Source repair changed fields outside exact LaTeX containers')
        groups = [f for f in findings if f.kind == 'run_group']
        group_application_audit = []
        if groups:
            modified['document'], group_application_audit = apply_run_groups(modified, groups, source_snapshot)
            for finding in groups:
                block_id = resolve_run_group(bundle, finding.source_id)['block_id']
                changed_blocks[block_id] = next(b for b in modified['document']['blocks'] if b['id'] == block_id)
        boundary_application_audit = []
        if normalized_boundaries:
            modified['document'], boundary_application_audit = apply_boundary_transfers(
                modified['document'], normalized_boundaries)
            for normalized in normalized_boundaries:
                for block_id in (normalized['receiver_block_id'], normalized['donor_block_id']):
                    changed_blocks[block_id] = next(b for b in modified['document']['blocks'] if b['id'] == block_id)
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
                  'raw_scan_findings': raw_findings, 'run_group_application_audit': group_application_audit,
                  'boundary_application_audit': boundary_application_audit,
                  'requires_new_empty_translation_experiment': True, 'artifact_dir': str(directory)}
        _write(directory / 'application.json', result)
        if before != after:
            raise RuntimeError('Source repair violated original/source/old-view immutability')
        if not report.get('valid') or not report.get('view_id'):
            raise ValueError('New source view did not validate/finalize; previous view remains current')
        _, _, saved, _ = current_source(self.library, bundle['paper_id'])
        if saved.model_dump(mode='json') != modified['document']:
            raise RuntimeError('Saved source repair changed fields outside proposed source runs/LaTeX')
        return result
