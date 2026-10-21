"""Conservative literal comparisons in text; never rewrites source or raw math IDs."""
import re
from copy import deepcopy

VERSION = 'text-comparison-v1'
COMPARISON = re.compile(
    r'(?<![\w@./])(?P<lhs>[A-Za-z][A-Za-z0-9_]{0,23})\s*'
    r'(?P<op><=|>=|[<>≤≥≠])\s*'
    r'(?P<rhs>[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?%?)(?![\w/]|\.\d)')


def inventory(bundle):
    """Independent inventory: its addition never changes the legacy fingerprint."""
    from .catalog import digest, target_id

    groups = []
    types = {c['block_id']: c['type'] for c in bundle.get('context', [])}
    for unit in bundle['text_units']:
        if unit.get('block_type', types.get(unit['block_id'], 'paragraph')) not in (
                'paragraph', 'caption', 'list', 'list_item', 'table'):
            continue
        if (not groups or any(groups[-1][-1][k] != unit[k] for k in ('block_id', 'field'))
                or groups[-1][-1]['run_index'] + 1 != unit['run_index']):
            groups.append([])
        groups[-1].append(unit)
    targets = []
    for group in groups:
        text = ''.join(unit['text'] for unit in group)
        for match in COMPARISON.finditer(text):
            lhs = match['lhs']
            before = text[:match.start()].rstrip()
            after = text[match.end():].lstrip()
            # Longer ordinary words need an explicitly parenthesized expression.
            if not (len(lhs) == 1 or lhs.isupper()
                    or before.endswith('(') and after.startswith(')')):
                continue
            vicinity = text[max(0, match.start() - 80):match.end() + 80]
            if re.search(r'https?://|www\.|[\w.+-]+@[\w.-]+|[{};`]', vicinity):
                continue
            # Never interpret HTML tags or executable comparison syntax as prose math.
            if before.endswith(('<', '/')) or re.search(r'\b(?:if|while|return)\s*$', before):
                continue
            parts, refs, position = [], [], 0
            for unit in group:
                start = max(0, match.start() - position)
                end = min(len(unit['text']), match.end() - position)
                if start < end:
                    parts.append({'run_index': unit['run_index'], 'start': start, 'end': end,
                                  'text': unit['text'][start:end]})
                    refs.extend(unit['source_refs'])
                position += len(unit['text'])
            first = group[0]
            target = {'kind': 'math', 'anchor_type': 'text_math', 'block_id': first['block_id'],
                      'field': first['field'], 'run_index': parts[0]['run_index'],
                      'start': None, 'end': None, 'source_text': match.group(),
                      'source_refs': list(dict.fromkeys(refs)), 'display': False,
                      'text_parts': parts}
            target['id'] = target_id(target)
            target['source_math_id'] = target['id']
            targets.append(target)
    return {'version': VERSION, 'sha256': digest({'version': VERSION, 'targets': targets}),
            'targets': targets}


def supplemental_targets(bundle):
    """Recompute exact parts from source units; untrusted inventory cannot add anchors."""
    return inventory(bundle)['targets']


def all_math_targets(bundle):
    return [*bundle['targets'], *supplemental_targets(bundle)]


def decorate(bundle):
    output = deepcopy(bundle)
    output['supplemental_math'] = inventory(bundle)
    return output
