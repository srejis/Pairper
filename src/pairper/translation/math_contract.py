"""Trusted math inventory and source-bound prose-overlay judgments."""
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from .math_overlay import inspect_many, verify_overlay


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def text_sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def source_math(bundle):
    items = {m['token']: m['latex'] for unit in bundle['units'] for m in unit['math']}
    items.update({f'block:{block["id"]}/latex': block['content']['latex']
                  for block in bundle['document']['blocks'] if block['type'] == 'equation'})
    return items


def renderer_identity():
    import pairper
    resource = Path(pairper.__file__).resolve().parent / 'web/katex/katex.min.js'
    worker = Path(__file__).with_name('math_overlay_worker.js')
    return sha([text_sha(resource.read_text('utf-8')), text_sha(worker.read_text('utf-8'))])


@lru_cache(maxsize=128)
def _inspect_cached(latex_tuple, renderer_id):
    return json.dumps(inspect_many(latex_tuple), ensure_ascii=False)


def math_inventory(bundle):
    items = list(source_math(bundle).items())
    inventory = []
    renderer_id = renderer_identity()
    for start in range(0, len(items), 64):
        batch = items[start:start + 64]
        inspected = json.loads(_inspect_cached(tuple(latex for _, latex in batch), renderer_id))
        for (math_id, latex), result in zip(batch, inspected, strict=True):
            if not result.get('success'):
                raise ValueError('Cannot inspect original math: ' + math_id + ': ' + '; '.join(result['errors']))
            if result['text_nodes']:
                inventory.append({'math_id': math_id, 'source_latex_sha256': text_sha(latex),
                                  'text_nodes': result['text_nodes']})
    return inventory


def context_sha(bundle, math_id, result):
    blocks = sorted(bundle['document']['blocks'], key=lambda block: block['order'])
    block_id = next((u['block_id'] for u in bundle['units']
                     if any(m['token'] == math_id for m in u['math'])), None)
    if block_id is None and math_id.startswith('block:') and math_id.endswith('/latex'):
        block_id = math_id[len('block:'):-len('/latex')]
    positions = {block['id']: i for i, block in enumerate(blocks)}
    if block_id not in positions:
        raise ValueError('Unknown source math context')
    position = positions[block_id]
    selected = {block['id'] for block in blocks[max(0, position - 1):position + 2]}
    parent = blocks[position].get('parent_id')
    if parent:
        selected.add(parent)
        selected.update(block['id'] for block in blocks if block.get('parent_id') == parent)
    ids = {u['id'] for u in bundle['units'] if u['block_id'] in selected}
    entries = sorted(({key: value for key, value in entry.items() if key != 'review'}
                      for entry in result.get('entries', []) if ids.intersection(entry['source_unit_ids'])),
                     key=lambda entry: tuple(entry['source_unit_ids']))
    covered = {unit_id for entry in entries for unit_id in entry['source_unit_ids']}
    other_math = {m['token'] for u in bundle['units'] if u['block_id'] in selected for m in u['math']}
    other_math.update(f'block:{block["id"]}/latex' for block in blocks
                      if block['id'] in selected and block['type'] == 'equation')
    overlays = sorted(({key: value for key, value in overlay.items() if key != 'review'}
                       for overlay in result.get('math_overlays', [])
                       if overlay['math_id'] in other_math and overlay['math_id'] != math_id),
                      key=lambda overlay: overlay['math_id'])
    return sha({'bundle_fingerprint': bundle['fingerprint'], 'block_ids': sorted(selected),
                'entries': entries, 'source_fallback_unit_ids': sorted(ids - covered), 'other_overlays': overlays})


def review_requirements(bundle, record, result=None):
    payload = {key: value for key, value in record.items() if key != 'review'}
    requirements = {'source_fingerprint': sha([bundle['fingerprint'], record['math_id'], record['source_latex_sha256']]),
                    'target_sha256': sha(payload)}
    if 'localized_latex' in record:
        requirements['context_sha256'] = context_sha(bundle, record['math_id'], result or {})
    return requirements


def reviewed(bundle, record, result=None):
    review = record['review']
    return (all(review.get(key) == value for key, value in review_requirements(bundle, record, result).items())
            and bool(review.get('reviewer')) and bool(review.get('method'))
            and review.get('source_compared') is True and review.get('unresolved_errors') == [])


@lru_cache(maxsize=1024)
def _proof(source, target, source_indices, target_indices, renderer_id):
    return json.dumps(verify_overlay(source, target, list(source_indices), list(target_indices)), ensure_ascii=False)


def verify_math_records(bundle, result):
    errors, unreviewed = [], []
    originals = source_math(bundle)
    overlays, roles, proofs = {}, {}, {}
    renderer_id = None
    for overlay in result.get('math_overlays', []):
        math_id = overlay['math_id']
        if math_id in overlays or math_id not in originals:
            errors.append('Unknown or duplicate math overlay: ' + math_id)
            continue
        overlays[math_id] = overlay
        if text_sha(originals[math_id]) != overlay['source_latex_sha256']:
            errors.append('Stale math source: ' + math_id)
            continue
        renderer_id = renderer_id or renderer_identity()
        proof = json.loads(_proof(originals[math_id], overlay['localized_latex'],
                                  tuple(overlay['source_prose_indices']), tuple(overlay['target_prose_indices']), renderer_id))
        proofs[math_id] = proof
        if not proof.get('success'):
            errors.append('Invalid math overlay: ' + math_id + ': ' + '; '.join(proof['errors']))
        if not reviewed(bundle, overlay, result):
            unreviewed.append(math_id + ':overlay')
    inventory = {(row['math_id'], node['index']): (row, node)
                 for row in bundle.get('math_text_inventory', []) for node in row['text_nodes']}
    for role in result.get('math_roles', []):
        key = (role['math_id'], role['node_index'])
        if key in roles or key not in inventory:
            errors.append('Unknown or duplicate math prose role: ' + str(key))
            continue
        roles[key] = role
        original, node = inventory[key]
        if role['source_latex_sha256'] != original['source_latex_sha256']:
            errors.append('Stale math prose role: ' + str(key))
        overlay = overlays.get(role['math_id'])
        selected = overlay and role['node_index'] in overlay['source_prose_indices']
        if role['action'] == 'preserve' and selected:
            errors.append('Preserved identifier/literal selected as editable prose: ' + str(key))
        if role['action'] == 'translate' and (not node['pure'] or not selected):
            unreviewed.append(str(key) + ':untranslated-prose')
        if not reviewed(bundle, role):
            unreviewed.append(str(key) + ':role-review')
    for key in inventory.keys() - roles.keys():
        unreviewed.append(str(key) + ':unclassified')
    return {'errors': errors, 'unreviewed_math': sorted(set(unreviewed)), 'proofs': proofs}
