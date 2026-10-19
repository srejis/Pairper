"""Project a source-bound translation onto a copy of normal reader display data."""
from copy import deepcopy

from .contract import TOKEN, verify_result


def _runs(content, field):
    if field == 'runs':
        return content['runs']
    parts = field.split('/')
    if len(parts) != 2 or parts[0] not in ('items', 'cells', 'code_lines') or not parts[1].isdigit():
        raise ValueError('Unknown translation unit field')
    value = content[parts[0]][int(parts[1])]
    return value if parts[0] == 'items' else value['runs']


def project_display(bundle, result, base_display):
    """Return display dict; caller checks live-source freshness before this pure projection.

    Existing image URLs and source provenance are copied verbatim. Translation
    strings remain plain strings and must be rendered as text by the reader.
    """
    if (base_display.get('paper_id') != bundle['paper_id']
            or base_display.get('view_id') != bundle['view_id']
            or bundle['document']['paper_id'] != bundle['paper_id']
            or bundle['document']['source_sha256'] != bundle['source_pdf_sha256']):
        raise ValueError('Display and translation source identities differ')
    report = verify_result(bundle, result)
    if not report['structurally_valid']:
        raise ValueError('Invalid translation: ' + '; '.join(report['errors']))
    output = deepcopy(base_display)
    blocks = {block['id']: block for block in output['blocks']}
    if len(blocks) != len(output['blocks']):
        raise ValueError('Duplicate source display block')
    source_blocks = {block['id']: block for block in bundle['document']['blocks']}
    if set(blocks) != set(source_blocks):
        raise ValueError('Display source blocks differ')
    for block_id, source in source_blocks.items():
        if any(blocks[block_id].get(key) != source.get(key)
               for key in ('type', 'order', 'parent_id', 'relations')):
            raise ValueError('Display structure differs from translation source')
    overlays = {row['math_id']: row['localized_latex'] for row in result.get('math_overlays', [])}
    slots, math_runs = {}, {}
    try:
        for unit in bundle['units']:
            block = blocks[unit['block_id']]
            runs = _runs(block['content'], unit['field'])
            originals = [run for run in runs if run['kind'] == 'math']
            if len(originals) != len(unit['math']):
                raise ValueError('Source display math count differs')
            for math, original in zip(unit['math'], originals, strict=True):
                if original.get('latex') != math['latex']:
                    raise ValueError('Source display math differs')
                math_runs[math['token']] = deepcopy(original)
            if unit.get('cell'):
                cell = block['content']['cells'][int(unit['field'].split('/')[1])]
                if any(cell.get(key) != value for key, value in unit['cell'].items()):
                    raise ValueError('Source display table geometry differs')
            slots[unit['id']] = runs
        # Standalone equation math has no text unit.
        for block_id, source in source_blocks.items():
            if source['type'] == 'equation':
                content = blocks[block_id]['content']
                if content.get('latex') != source['content']['latex']:
                    raise ValueError('Source equation differs')
                mid = f'block:{block_id}/latex'
                if mid in overlays:
                    content['latex'] = overlays[mid]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError('Display translation source slot is unavailable') from exc

    # Preserve source style, spacing and fallbacks unless actual prose translates.
    for unit in bundle['units']:
        for math, original in zip(unit['math'], [r for r in slots[unit['id']] if r['kind']=='math'], strict=True):
            if math['token'] in overlays:
                original['latex'] = overlays[math['token']]
    for entry in result['entries']:
        ids = entry['source_unit_ids']
        if entry['action'] == 'preserve':
            continue
        translated = []
        position = 0
        for match in TOKEN.finditer(entry['target']):
            text = entry['target'][position:match.start()]
            if text:
                translated.append({'kind': 'text', 'prefix': '', 'text': text})
            run = deepcopy(math_runs[match.group()])
            run['prefix'] = ''
            if match.group() in overlays:
                run['latex'] = overlays[match.group()]
            translated.append(run)
            position = match.end()
        if position < len(entry['target']):
            translated.append({'kind':'text','prefix':'','text':entry['target'][position:]})
        slots[ids[0]][:] = translated
        for unit_id in ids[1:]:
            slots[unit_id][:] = []
    for review in result.get('figure_reviews', []):
        block = blocks[review['block_id']]
        if block['type'] != 'figure':
            raise ValueError('Figure review refers to another block type')
        block['content']['translation_labels'] = [
            {'source': label['source'], 'target': label['target']}
            for label in review['labels'] if label['source'] != label['target']]
    output['translation'] = {
        'status': 'draft', 'complete': report['complete'],
        'unreviewed_unit_count': len(report['unreviewed_units']),
        'unreviewed_math_count': len(report.get('unreviewed_math', [])),
        'unreviewed_figure_count': len(report.get('unreviewed_figures', [])),
        'missing_unit_count': len(report['missing_units']),
    }
    return output
