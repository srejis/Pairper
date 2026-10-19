"""Offline review preview of a source-bound draft, never a reconstructed source view."""
import html
from pathlib import Path

from .contract import TOKEN, verify_result


def render_preview(bundle, result, output, web_directory, source_spans):
    report = verify_result(bundle, result)
    if not report['structurally_valid']:
        raise ValueError('; '.join(report['errors']))
    entries = {unit_id: entry for entry in result['entries'] for unit_id in entry['source_unit_ids']}
    units = {unit['id']: unit for unit in bundle['units']}
    figures = {figure['block_id']: figure for figure in bundle['figures']}
    overlays = {item['math_id']: item['localized_latex'] for item in result.get('math_overlays', [])}
    def esc(value):
        return html.escape(str(value), quote=True)

    def math(latex, display=False):
        return (f'<span class="math" data-latex="{esc(latex)}" data-display="{str(display).lower()}">'
                '<span class="math-output"></span>'
                f'<span class="math-fallback">{esc(latex)}</span></span>')

    def text(block, field):
        unit = units.get(f'{block["id"]}/{field}')
        if unit is None:
            return ''  # Keep empty table cells and empty structural labels.
        entry = entries.get(unit['id'])
        if entry and entry['source_unit_ids'][0] != unit['id']:
            return (f'<span data-unit-id="{esc(unit["id"])}" data-translation-state="joined" '
                    f'data-joined-into="{esc(entry["source_unit_ids"][0])}"></span>')
        value = entry['target'] if entry else unit['source_text']
        selected = [units[unit_id] for unit_id in entry['source_unit_ids']] if entry else [unit]
        lookup = {item['token']: overlays.get(item['token'], item['latex'])
                  for selected_unit in selected for item in selected_unit['math']}
        pieces, start = [], 0
        for match in TOKEN.finditer(value):
            pieces.extend([esc(value[start:match.start()]), math(lookup[match[0]])])
            start = match.end()
        pieces.append(esc(value[start:]))
        state = 'draft' if entry else 'source-fallback'
        return f'<span data-unit-id="{esc(unit["id"])}" data-translation-state="{state}">' + ''.join(pieces) + '</span>'

    blocks = sorted(bundle['document']['blocks'], key=lambda block: block['order'])
    children = {}
    bodies = {}
    for block in blocks:
        children.setdefault(block.get('parent_id'), []).append(block)
        content, kind = block['content'], block['type']
        body = text(block, 'runs')
        if kind == 'title':
            body = f'<h1>{body}</h1>'
        elif kind == 'heading':
            level = content['level']
            body = f'<h{level}>{body}</h{level}>'
        elif kind == 'list':
            tag = 'ol' if content['ordered'] else 'ul'
            body = f'<{tag}>' + ''.join(f'<li>{text(block, f"items/{i}")}</li>'
                                         for i in range(len(content['items']))) + f'</{tag}>'
        elif kind == 'code':
            body = '<pre><code>' + '\n'.join(' ' * line['indent'] + text(block, f'code_lines/{i}')
                                             for i, line in enumerate(content['code_lines'])) + '</code></pre>'
        elif kind == 'table' and content['cells'] and content['source_verified']:
            rows = {}
            for i, cell in sorted(enumerate(content['cells']), key=lambda item: (item[1]['row'], item[1]['column'])):
                tag = 'th' if cell['header'] else 'td'
                rows.setdefault(cell['row'], []).append(
                    f'<{tag} data-cell-index="{i}" rowspan="{cell["rowspan"]}" colspan="{cell["colspan"]}">'
                    + text(block, f'cells/{i}') + f'</{tag}>')
            body = '<div class="table-scroll"><table>' + ''.join('<tr>' + ''.join(row) + '</tr>'
                                                                    for row in rows.values()) + '</table></div>'
        elif kind == 'equation':
            body = math(overlays.get(f'block:{block["id"]}/latex', content['latex']), True)
            body += '<span class="equation-number">' + esc(' '.join(
                source_spans[ref]['text'] for ref in content['number_refs'])) + '</span>'
        elif kind == 'figure':
            figure = figures[block['id']]
            body = f'<figure><img src="{esc(Path(figure["crop"]).as_uri())}" alt="원본 그림"></figure>'
        elif kind in {'source_region', 'table'}:
            raise ValueError('A source-region image projection is required for this block')
        elif kind != 'group':
            body = f'<p>{body}</p>'
        bodies[block['id']] = body

    def sections(parent=None, depth=0):
        if depth > 32:
            raise ValueError('Excessively nested document')
        return ''.join(f'<section data-block-id="{esc(block["id"])}" data-block-type="{esc(block["type"])}">'
                       + bodies[block['id']] + sections(block['id'], depth + 1) + '</section>'
                       for block in children.get(parent, []))

    web = Path(web_directory).resolve()
    body = sections()
    style = ('body{font-family:Arial,"Malgun Gothic",sans-serif;margin:0;padding:32px;color:#222;background:white}'
             'main{max-width:1050px;margin:auto}p{line-height:1.85;overflow-wrap:anywhere}'
             'section{margin:12px 0}h1{font-size:25px}img{max-width:100%;height:auto}'
             '.table-scroll,pre{overflow:auto}table{border-collapse:collapse;min-width:50%}'
             'td,th{border:1px solid #ddd;padding:7px}pre{line-height:1.6;white-space:pre}'
             '[data-translation-state="source-fallback"]{background:#fff0bf}'
             '.notice{background:#eef2f7;padding:16px}.source-notice{background:#fff0bf}'
             '.math{white-space:nowrap}.katex{font-size:1.05em}')
    document = ('<!doctype html><html lang="ko"><meta charset="utf-8">'
                '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
                "script-src file:; style-src file: 'unsafe-inline'; img-src file:; font-src file:; "
                "connect-src 'none'; base-uri 'none'; form-action 'none'\">"
                f'<link rel="stylesheet" href="{esc((web / "katex/katex.min.css").as_uri())}">'
                f'<script defer src="{esc((web / "katex/katex.min.js").as_uri())}"></script>'
                f'<script defer src="{esc((web / "reader.js").as_uri())}"></script>'
                f'<style>{style}</style><title>Pairper 한국어 초안 검토</title><main>'
                '<aside class="notice">검토 중인 한국어 초안입니다. 원문 대조는 미완료입니다. '
                '노란 부분은 미해결 원문이며, 그림은 원본입니다.</aside>' + body + '</main></html>')
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, 'utf-8')
    return {'path': str(output.resolve()), 'blocks': len(blocks), 'entries': len(result['entries']),
            'source_fallback_units': report['missing_units'], 'semantic_quality_proven': False}
