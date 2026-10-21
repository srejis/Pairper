"""Map published source occurrences onto visible runs without reusing stale offsets."""
import re
from collections import defaultdict
from copy import deepcopy


def _fields(content):
    yield 'runs', content.get('runs', [])
    yield from ((f'items/{i}', row) for i, row in enumerate(content.get('items', [])))
    yield from ((f'cells/{i}', row['runs']) for i, row in enumerate(content.get('cells', [])))


def _overlap(left, right):
    return (all(left[k] == right[k] for k in ('block_id', 'field', 'run_index'))
            and left['start'] < right['end'] and left['end'] > right['start'])


def _distinct_concept_anchors(anchors):
    # Nested faithful aliases of the same concept use their longest exact quote.
    selected = []
    for anchor in sorted(anchors, key=lambda a: (-(a['end'] - a['start']), a['start'])):
        if not any(_overlap(anchor, previous) for previous in selected):
            selected.append(anchor)
    return sorted(selected, key=lambda a: (a['block_id'], a['field'], a['run_index'], a['start']))


def _text_formula_anchors(target, visible_runs, translated):
    if not translated:
        anchors = []
        lookup = {(bid, field, i): run for bid, field, i, run in visible_runs}
        for part in target['text_parts']:
            run = lookup.get((target['block_id'], target['field'], part['run_index']))
            if (not run or run.get('kind') != 'text'
                    or run['text'][part['start']:part['end']] != part['text']):
                return []
            anchors.append({'block_id': target['block_id'], 'field': target['field'], **part})
        return anchors
    from .text_math import COMPARISON

    formula = COMPARISON.fullmatch(target['source_text'])
    if formula is None:
        return []
    # An exact numeric RHS can be followed immediately by a complete Korean particle.
    # Keep the existing word/decimal guard for identifiers, numbers and other scripts.
    particle = r'(?:으로|에서|이|가|은|는|을|를|과|와|로|에|도|만)(?=$|\s|[.,;:!?…)\]}])'
    right_boundary = r'(?:(?!\w|\.\d)|(?=' + particle + r'))'
    pattern = re.compile(r'(?<!\w)' + r'\s*'.join(re.escape(formula[k])
                         for k in ('lhs', 'op', 'rhs')) + right_boundary)
    unit = f"{target['block_id']}/{target['field']}"
    groups = []
    for bid, field, i, run in visible_runs:
        eligible = (run.get('kind') == 'text'
                    and (unit in run.get('source_unit_ids', [])
                         or bid == target['block_id'] and field == target['field']))
        if not eligible:
            continue
        if (not groups or groups[-1][-1][:2] != (bid, field)
                or groups[-1][-1][2] + 1 != i):
            groups.append([])
        groups[-1].append((bid, field, i, run))
    found = []
    for group in groups:
        text = ''.join(row[3]['text'] for row in group)
        for match in pattern.finditer(text):
            parts, position = [], 0
            for bid, field, i, run in group:
                start, end = max(0, match.start() - position), min(len(run['text']), match.end() - position)
                if start < end:
                    parts.append({'block_id': bid, 'field': field, 'run_index': i,
                                  'start': start, 'end': end, 'text': run['text'][start:end]})
                position += len(run['text'])
            found.append(parts)
    return found[0] if len(found) == 1 else []


def project_explanations(bundle, display):
    """Return localized exact anchors; unresolved/ambiguous anchors remain absent.

    Translation math IDs are source-provenance IDs carried by translation projection.
    A term alias is reviewed together with its source-bound explanation. It can only
    match inside the originating source field, never by searching the whole paper.
    """
    output = deepcopy(bundle)
    notation = {e['target_id'] for e in output.get('explanations', [])
                if e['kind'] == 'math' and e.get('math_role') == 'notation'}
    literals = {e['target_id'] for e in output.get('explanations', [])
                if e['kind'] == 'math' and e.get('math_role') == 'literal'}
    tables = {c['block_id'] for c in bundle.get('context', []) if c['type'] == 'table'}
    table_literals = {t['id'] for t in output.get('targets', []) if t['id'] in literals
                      and (t['block_id'] in tables or t['field'].startswith('cells/'))}
    output['targets'] = [t for t in output.get('targets', [])
                         if t['id'] not in notation | table_literals]
    for target in output.get('targets', []):
        target['localized'] = []
        target.pop('shared_with_target_id', None)
    if (display.get('paper_id') != bundle.get('paper_id')
            or display.get('view_id') != bundle.get('source_identity', {}).get('view_id')
            or display.get('source_identity') is not None
            and display['source_identity'] != bundle.get('source_identity')):
        return output
    blocks = {b['id']: b for b in display['blocks']}
    translated = bool(display.get('translation'))
    visible_runs = [(b['id'], field, index, run) for b in display['blocks']
                    for field, runs in _fields(b['content']) for index, run in enumerate(runs)]
    for target in output.get('targets', []):
        block = blocks.get(target['block_id'])
        if block is None:
            continue
        content = block['content']
        common = {'block_id': block['id'], 'field': target['field']}
        if target.get('anchor_type') == 'text_math':
            target['localized'] = _text_formula_anchors(target, visible_runs, translated)
            continue
        if target['field'] == 'latex':
            if (content.get('source_math_id') == target.get('source_math_id')
                    or content.get('latex') == target['source_text']):
                target['localized'] = [{**common, 'run_index': None, 'start': None,
                                         'end': None, 'text': content.get('latex')}]
            continue
        slots = dict(_fields(content))
        runs = slots.get(target['field'], [])
        if target['kind'] == 'math':
            matches = [(bid, field, i, r) for bid, field, i, r in visible_runs
                       if r.get('kind') == 'math'
                       and (r.get('source_math_id') == target.get('source_math_id')
                            or (not translated and bid == block['id'] and field == target['field']
                                and i == target['run_index']
                                and r.get('latex') == target['source_text']))]
            if len(matches) == 1:
                bid, field, i, run = matches[0]
                target['localized'] = [{'block_id': bid, 'field': field,
                                         'run_index': i, 'start': None,
                                         'end': None, 'text': run.get('latex')}]
            continue
        if not translated:
            i = target['run_index']
            if i < len(runs) and runs[i].get('kind') == 'text':
                text = runs[i]['text']
                if text[target['start']:target['end']] == target['source_text']:
                    target['localized'] = [{**common, 'run_index': i, 'start': target['start'],
                                             'end': target['end'], 'text': target['source_text']}]
            continue
        matches = []
        aliases = {target['source_text'], *target.get('aliases', [])}
        source_unit_id = f'{target["block_id"]}/{target["field"]}'
        for bid, field, i, run in visible_runs:
            if not (source_unit_id in run.get('source_unit_ids', [])
                    or bid == target['block_id'] and field == target['field']):
                continue
            if run.get('kind') != 'text':
                continue
            for alias in aliases:
                for match in re.finditer(re.escape(alias), run['text']):
                    start, end = match.span()
                    # Latin acronym within a different word is never a term match.
                    def latin_word(char):
                        return char.isascii() and (char.isalnum() or char == '_')

                    marker_boundary = (block.get('type') == 'footnote'
                        and blocks[bid].get('type') == 'footnote' and i == 0
                        and source_unit_id in run.get('source_unit_ids', [])
                        and any(m['block_id'] == target['block_id']
                            and m['field'] == target['field'] == field == 'runs'
                            and m['run_index'] == 0 and target['run_index'] > 0
                            and m.get('superscript') is True
                            and re.fullmatch(r'[0-9]+', m['marker'])
                            and start == len(m['marker'])
                            and run['text'].startswith(m['marker'])
                            for m in bundle.get('source_footnote_markers', [])))
                    if (alias.isascii() and
                            (start and latin_word(run['text'][start - 1]) and not marker_boundary
                             or end < len(run['text']) and latin_word(run['text'][end]))):
                        continue
                    matches.append({'block_id': bid, 'field': field,
                                    'run_index': i, 'start': start,
                                    'end': end, 'text': alias})
        # Only an explicitly reviewed semantic concept can share its explanation
        # across repeated aliases. Legacy/ambiguous occurrences retain exactness.
        if target.get('concept_key'):
            target['localized'] = _distinct_concept_anchors(matches)
        elif len(matches) == 1:
            target['localized'] = matches
    if translated:
        groups = defaultdict(list)
        for target in output['targets']:
            if target['kind'] == 'term' and target.get('concept_key'):
                groups[(target['block_id'], target['field'], target['concept_key'])].append(target)
        for members in groups.values():
            owner = min(members, key=lambda t: (t['run_index'], t['start'], t['id']))
            anchors = _distinct_concept_anchors([a for t in members for a in t['localized']])
            owner['localized'] = anchors
            for member in members:
                if member['id'] != owner['id']:
                    member['localized'] = []
                    member['shared_with_target_id'] = owner['id']
    # Reviewed complete aliases take precedence over a contained shorter alias
    # from the same source unit. Equal or partial overlaps remain ambiguous.
    reviewed = {e['target_id'] for e in output.get('explanations', []) if e.get('reviewed')}
    # A reviewed text formula owns every one of its exact displayed parts. Hide
    # only the overlapping glossary occurrence; never merge its concept/ID into
    # the formula or remove other occurrences of the same glossary term.
    formula_parts = [(t['id'], anchor) for t in output['targets']
                     if t.get('anchor_type') == 'text_math' and t['id'] in reviewed
                     for anchor in t['localized']]
    shadowed = []
    for target in output['targets']:
        if target['kind'] != 'term':
            continue
        keep = []
        for anchor in target['localized']:
            owners = sorted({tid for tid, part in formula_parts if _overlap(anchor, part)})
            if owners:
                shadowed.append({'target_id': target['id'], 'formula_target_ids': owners,
                                 'anchor': deepcopy(anchor)})
            else:
                keep.append(anchor)
        target['localized'] = keep
    shadowed_ids = {row['target_id'] for row in shadowed}
    fully_shadowed_ids = {t['id'] for t in output['targets']
                          if t['id'] in shadowed_ids and not t['localized']}
    collisions, rejected, contained_candidates = set(), set(), []
    original_alias_counts = {t['id']: len(t['localized']) for t in output['targets']}
    slots = defaultdict(list)
    for target in output['targets']:
        if target['kind'] == 'term':
            for index, anchor in enumerate(target['localized']):
                slots[(anchor['block_id'], anchor['field'], anchor['run_index'])].append(
                    (target, index, anchor))
    for anchored in slots.values():
        anchored.sort(key=lambda row: row[2]['start'])
        for index, (left, li, a) in enumerate(anchored):
            for right, ri, b in anchored[index + 1:]:
                if b['start'] >= a['end']:
                    break
                if left['id'] == right['id'] or not _overlap(a, b):
                    continue
                if left.get('concept_key') and left['concept_key'] == right.get('concept_key'):
                    loser, loser_index, winner = ((right, ri, left) if left['id'] < right['id']
                                                 else (left, li, right))
                    rejected.add((loser['id'], loser_index))
                    loser['shared_with_target_id'] = winner['id']
                else:
                    same_source_unit = all(left[k] == right[k] for k in ('block_id', 'field'))
                    contained = None
                    if (translated and same_source_unit
                            and left['id'] in reviewed and right['id'] in reviewed
                            and (a['start'], a['end']) != (b['start'], b['end'])):
                        if a['start'] <= b['start'] and a['end'] >= b['end']:
                            contained = (right['id'], ri)
                        elif b['start'] <= a['start'] and b['end'] >= a['end']:
                            contained = (left['id'], li)
                    if contained is not None:
                        rejected.add(contained)
                        loser, loser_index, loser_anchor, owner, owner_index, owner_anchor = (
                            (right, ri, b, left, li, a) if contained == (right['id'], ri)
                            else (left, li, a, right, ri, b))
                        contained_candidates.append((loser['id'], loser_index, owner['id'],
                            owner_index, deepcopy(loser_anchor), deepcopy(owner_anchor)))
                    else:
                        collisions.update((left['id'], right['id']))
                        rejected.update(((left['id'], li), (right['id'], ri)))
    for target in output['targets']:
        target['localized'] = [a for i, a in enumerate(target['localized'])
                               if (target['id'], i) not in rejected]
    lookup = {t['id']: t for t in output['targets']}

    def resolved(target, visited=None):
        visited = set() if visited is None else visited
        if target['localized']:
            return True
        owner = target.get('shared_with_target_id')
        if owner not in lookup or owner in visited:
            return False
        return resolved(lookup[owner], visited | {target['id']})

    def fully_shadowed(target, visited=None):
        if target['localized']:
            return False
        if target['id'] in fully_shadowed_ids:
            return True
        visited = set() if visited is None else visited
        owner = target.get('shared_with_target_id')
        if owner not in lookup or owner in visited:
            return False
        return fully_shadowed(lookup[owner], visited | {target['id']})

    # Diagnostics only: preserve the raw unresolved aggregate and all bindings.
    # A nominal owner that was itself rejected is not evidence of intentional
    # exclusion; only a surviving longer reviewed literal can explain the gap.
    contained_occurrences, contained_indices = [], defaultdict(set)
    for loser, index, owner, owner_index, anchor, owner_anchor in contained_candidates:
        if (owner, owner_index) in rejected or owner_anchor not in lookup[owner]['localized']:
            continue
        contained_occurrences.append({'target_id': loser, 'anchor': anchor,
            'owner_target_id': owner, 'owner_anchor': owner_anchor,
            'reason': 'strict_containment_by_distinct_reviewed_concept'})
        contained_indices[loser].add(index)
    unresolved = [t['id'] for t in output['targets']
                  if not resolved(t) and not fully_shadowed(t)]
    contained_only = sorted(tid for tid in unresolved if tid not in collisions
        and original_alias_counts[tid] > 0
        and contained_indices[tid] == set(range(original_alias_counts[tid])))
    output['projection'] = {
        'visible_target_count': sum(bool(t['localized']) for t in output['targets']),
        'visible_occurrence_count': sum(len(t['localized']) for t in output['targets']),
        'shared_target_count': sum(bool(t.get('shared_with_target_id')) and resolved(t)
                                   for t in output['targets']),
        'unresolved_target_ids': unresolved,
        'contained_alias_shadowed_occurrences': contained_occurrences,
        'contained_alias_shadowed_only_target_ids': contained_only,
        'unmapped_target_ids': [tid for tid in unresolved if tid not in contained_only],
        'formula_shadowed_occurrence_count': len(shadowed),
        'formula_shadowed_target_ids': sorted(shadowed_ids),
        'formula_shadowed_only_target_ids': [t['id'] for t in output['targets'] if fully_shadowed(t)],
        'formula_shadowed_occurrences': shadowed,
        'alias_collision_target_ids': sorted(collisions),
        'notation_disposition_count': len(notation),
        'table_literal_count': len(table_literals),
    }
    return output
