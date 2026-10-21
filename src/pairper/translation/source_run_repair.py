"""Pure source-run regrouping gates; no AI, library writes, or approval inference.

Callers must independently review the normalized proposal against original PDF
images, then validate/finalize a new whole source view. These helpers only bind
selectors, preserve provenance, and construct the reviewed candidate document.
"""
from __future__ import annotations

import copy
import json
import math
import re
from collections import Counter

from pairper.document.models import Block, Document, MathRun, TextRun
from pairper.document.provenance import contains, expected_refs


def canonical_run_json(runs):
    return json.dumps(runs, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _json_runs(value):
    def object_pairs(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError('Duplicate JSON field in source runs')
            result[key] = item
        return result
    def constant(_):
        raise ValueError('Nonfinite JSON source-run value')
    if not isinstance(value, str):
        raise ValueError('Source runs require a JSON array string')
    runs = json.loads(value, object_pairs_hook=object_pairs, parse_constant=constant)
    if not isinstance(runs, list) or not all(isinstance(run, dict) for run in runs):
        raise ValueError('Source runs require a JSON object array')
    return runs


def _run_list(document, unit):
    blocks = [block for block in document['blocks'] if block['id'] == unit['block_id']]
    if len(blocks) != 1:
        raise ValueError('Unknown or duplicate source block')
    content = blocks[0]['content']
    field = unit['field'].split('/')
    if field == ['runs']:
        return content['runs']
    if len(field) == 2 and field[0] in ('items', 'cells', 'code_lines') and re.fullmatch(r'0|[1-9][0-9]*', field[1]):
        values = content[field[0]]
        index = int(field[1])
        if index >= len(values):
            raise ValueError('Source unit field index is outside its container')
        return values[index] if field[0] == 'items' else values[index]['runs']
    raise ValueError('Unsupported source run field')


def resolve_run_group(bundle, source_id):
    if not isinstance(source_id, str):
        raise ValueError('Source group ID must be a string')
    unit_id, separator, selection = source_id.rpartition('/range/')
    match = re.fullmatch(r'(0|[1-9][0-9]*):(0|[1-9][0-9]*)', selection)
    if not separator or not match:
        raise ValueError('Expected unit_id/range/start:end with exclusive end')
    units = [unit for unit in bundle['units'] if unit['id'] == unit_id]
    if len(units) != 1:
        raise ValueError('Unknown or duplicate source unit ID')
    unit = units[0]
    if unit_id != unit['block_id'] + '/' + unit['field']:
        raise ValueError('Source unit identity disagrees with its run container')
    runs = _run_list(bundle['document'], unit)
    start, end = map(int, match.groups())
    if not 0 <= start < end <= len(runs):
        raise ValueError('Source run range must be nonempty and within current runs')
    selected = copy.deepcopy(runs[start:end])
    for run in selected:
        if run.get('ref_ranges'):
            raise ValueError('Unmaterialized source ref_ranges are unsupported')
        cls = MathRun if run.get('kind') == 'math' else TextRun
        cls.model_validate(run)
    refs = [ref for run in selected for ref in run.get('refs', [])]
    if not refs or not set(refs) <= set(unit['source_refs']):
        raise ValueError('Run group must bind actual materialized unit references')
    return {'unit_id':unit_id, 'block_id':unit['block_id'], 'field':unit['field'],
            'start':start, 'end':end, 'current_runs':selected,
            'source_pages':list(unit.get('pages', [])), 'refs':refs}


def _source_spans(source):
    result = {}
    for page in source['pages']:
        for span in page['spans']:
            if span['id'] in result:
                raise ValueError('Duplicate original source span ID')
            box = span['bbox']
            if len(box) != 4 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in box):
                raise ValueError('Invalid original source span coordinates')
            if box[2] < box[0] or box[3] < box[1]:
                raise ValueError('Reversed original source span coordinates')
            result[span['id']] = (page['page'], span)
    return result


def normalize_run_group(bundle, finding, source_snapshot):
    finding = finding.model_dump() if hasattr(finding, 'model_dump') else dict(finding)
    if finding.get('kind') != 'run_group':
        raise ValueError('Expected a run_group finding')
    resolved = resolve_run_group(bundle, finding['source_id'])
    current = _json_runs(finding['current'])
    if canonical_run_json(current) != canonical_run_json(resolved['current_runs']):
        raise ValueError('Run group current JSON does not exactly match original run dictionaries')
    proposed = _json_runs(finding['proposed'])
    spans = _source_spans(source_snapshot)
    if any(ref not in spans for ref in resolved['refs']):
        raise ValueError('Unknown original source reference')
    pages = {spans[ref][0] for ref in resolved['refs']}
    if len(pages) != 1:
        raise ValueError('Cross-page source run groups are unsupported')
    page = next(iter(pages))
    if page not in resolved['source_pages']:
        raise ValueError('Run group original page disagrees with its source unit')
    normalized = []
    for raw in proposed:
        raw = copy.deepcopy(raw)
        if raw.get('ref_ranges'):
            raise ValueError('Proposed source ref_ranges are unsupported; use materialized refs')
        refs = raw.get('refs', [])
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in spans for ref in refs):
            raise ValueError('Every proposed run needs known nonempty original refs')
        if not set(refs) <= set(resolved['refs']):
            raise ValueError('Proposed run uses references outside the selected group')
        if raw.get('kind') == 'math':
            boxes = [spans[ref][1]['bbox'] for ref in refs]
            region = {'page':page, 'bbox':[min(b[0] for b in boxes), min(b[1] for b in boxes),
                                          max(b[2] for b in boxes), max(b[3] for b in boxes)], 'rotation':0}
            # Never trust a model's region. Check exactly the containment policy
            # used by whole-document validation, including its tolerance.
            for ref, (span_page, span) in spans.items():
                if span_page == page and ref not in refs and contains(region['bbox'], span['bbox']):
                    raise ValueError('Derived math bbox encloses an outside original span')
            raw['region'] = region
            raw['source_verified'] = True  # Planned post-review value, not an approval.
            run = MathRun.model_validate(raw)
            if not run.latex.strip():
                raise ValueError('Regrouped source math cannot be empty')
        elif raw.get('kind') == 'text':
            raw['source_verified'] = True  # Applied only after caller's fresh PDF review.
            run = TextRun.model_validate(raw)
        else:
            raise ValueError('Only original text/math runs may be regrouped')
        normalized.append(run.model_dump(mode='json'))
    original_refs = resolved['refs']
    target_refs = [ref for run in normalized for ref in run['refs']]
    if Counter(target_refs) != Counter(original_refs):
        raise ValueError('Regrouping must preserve the exact source ref multiset')
    if target_refs != original_refs:
        raise ValueError('Regrouping must preserve original flattened reference order')
    if normalized[0]['before'] != current[0].get('before', 'auto'):
        raise ValueError('Regrouping must preserve the first run boundary policy')
    document = copy.deepcopy(bundle['document'])
    unit = next(unit for unit in bundle['units'] if unit['id'] == resolved['unit_id'])
    _run_list(document, unit)[resolved['start']:resolved['end']] = normalized
    block = next(block for block in document['blocks'] if block['id'] == resolved['block_id'])
    if set(expected_refs(Block.model_validate(block), source_snapshot)) != set(block['source_refs']):
        raise ValueError('Regrouping changes block source coverage outside authorized runs')
    return {**resolved, 'source_id':finding['source_id'], 'source_pages':[page],
            'current':canonical_run_json(current), 'proposed':canonical_run_json(normalized),
            'audit':{'original_ref_order':original_refs, 'derived_math_regions':
                     [run['region'] for run in normalized if run['kind'] == 'math'],
                     'requires_fresh_PDF_review':True, 'requires_full_source_validation':True}}


def apply_run_groups(bundle, findings, source_snapshot):
    """Build a candidate only. The caller owns fresh review and actual view writes."""
    normalized = [normalize_run_group(bundle, finding, source_snapshot) for finding in findings]
    selections = {}
    for item in normalized:
        ranges = selections.setdefault(item['unit_id'], [])
        if any(item['start'] < end and start < item['end'] for start, end in ranges):
            raise ValueError('Overlapping original source run groups are forbidden')
        ranges.append((item['start'], item['end']))
    document = copy.deepcopy(bundle['document'])
    units = {unit['id']:unit for unit in bundle['units']}
    for item in sorted(normalized, key=lambda item:(item['unit_id'], -item['start'])):
        runs = _run_list(document, units[item['unit_id']])
        runs[item['start']:item['end']] = _json_runs(item['proposed'])
    Document.model_validate(document)
    return document, [{'source_id':item['source_id'], 'current':item['current'],
                       'proposed':item['proposed'], **item['audit']} for item in normalized]
