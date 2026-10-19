"""Read current source documents and prepare translation tasks without rewriting them."""
import hashlib
import json
from pathlib import Path

from pairper.document.models import Document, MathRun
from pairper.document.provenance import materialize_ranges
from pairper.document.text import resolved_runs
from pairper.pdf.source import render

from .contract import make_bundle


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def current_source(library, paper_id):
    paper = library.paper(paper_id)
    if paper['status'] != 'complete' or not paper.get('current_view_id'):
        raise ValueError('A completed source reconstruction is required')
    pdf_path = library.root / paper['original_path']
    source_path = library.root / 'sources' / f'{paper_id}.json'
    view_path = library.root / 'views' / f'{paper["current_view_id"]}.json'
    if file_hash(pdf_path) != paper['sha256']:
        raise ValueError('Original PDF changed')
    source = json.loads(source_path.read_text(encoding='utf-8'))
    document = Document.model_validate_json(view_path.read_text(encoding='utf-8'))
    if (source['sha256'] != paper['sha256'] or document.paper_id != paper_id
            or document.source_sha256 != paper['sha256']):
        raise ValueError('Source and reconstruction do not refer to the same original')
    return paper, source, document, {
        'source_pdf_sha256': paper['sha256'], 'view_id': paper['current_view_id'],
        'view_sha256': file_hash(view_path), 'source_snapshot_sha256': file_hash(source_path)}


def assert_current(library, bundle):
    _, _, _, identity = current_source(library, bundle['paper_id'])
    if any(bundle.get(key) != value for key, value in identity.items()):
        raise ValueError('Source reconstruction changed; keep this draft and begin a new one')


def prepare_bundle(library, paper_id, asset_directory):
    paper, source, document, identity = current_source(library, paper_id)
    destination = Path(asset_directory).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    spans = {s['id']: {**s, 'page': page['page']}
             for page in source['pages'] for s in page['spans']}
    units, figures = [], []
    for block in sorted(document.blocks, key=lambda b: b.order):
        materialize_ranges(block, source)
        content = block.content
        fields = [('runs', content.runs)]
        fields += [(f'items/{i}', runs) for i, runs in enumerate(content.items)]
        fields += [(f'cells/{i}', cell.runs) for i, cell in enumerate(content.cells)]
        fields += [(f'code_lines/{i}', line.runs) for i, line in enumerate(content.code_lines)]
        for field, runs in fields:
            if not runs:
                continue
            pieces, math, refs = [], [], []
            for run, prefix, text in resolved_runs(runs, spans, []):
                refs.extend(run.refs)
                if isinstance(run, MathRun):
                    token = f'⟦M{len(math)}⟧'
                    math.append({'token': token, 'latex': run.latex, 'refs': run.refs})
                    pieces.append(prefix + token)
                else:
                    pieces.append(prefix + text)
            text = ''.join(pieces)
            unit = {'id': f'{block.id}/{field}', 'block_id': block.id, 'field': field,
                    'type': block.type, 'order': block.order, 'parent_id': block.parent_id,
                    'relations': [r.model_dump() for r in block.relations],
                    'pages': sorted({spans[ref]['page'] for ref in refs}),
                    'source_refs': refs, 'source_text': text, 'math': math,
                    'source_text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
            if field.startswith('cells/'):
                cell = content.cells[int(field.split('/')[1])]
                unit['cell'] = {key: getattr(cell, key)
                                for key in ('row', 'column', 'rowspan', 'colspan', 'header')}
            units.append(unit)
        if block.type == 'figure':
            image = render(library.root / paper['original_path'], source,
                           content.region.page, content.region, 2)
            digest = hashlib.sha256(image).hexdigest()
            crop = destination / f'{digest}.png'
            if crop.exists():
                if file_hash(crop) != digest:
                    raise ValueError('An immutable translation crop has changed')
            else:
                with crop.open('xb') as stream:
                    stream.write(image)
            figures.append({'paper_id': paper_id, 'block_id': block.id,
                            'region': content.region.model_dump(mode='json'),
                            'crop': str(crop), 'crop_sha256': digest})
    export = {'paper': paper, 'view_sha256': identity['view_sha256'],
              'source_snapshot_sha256': identity['source_snapshot_sha256'],
              'units': units, 'document': document.model_dump(mode='json')}
    bundle = make_bundle(export, figures)
    # Expose text extraction as evidence, without asserting it is visually complete.
    bundle['figure_source_spans'] = {
        block.id: [spans[ref] for ref in block.source_refs]
        for block in document.blocks if block.type == 'figure'}
    from .math_contract import math_inventory
    bundle['math_text_inventory'] = math_inventory(bundle)
    bundle['math_inventory_version'] = 1
    from .contract import sha
    bundle['fingerprint'] = sha({key: value for key, value in bundle.items() if key != 'fingerprint'})
    return bundle
