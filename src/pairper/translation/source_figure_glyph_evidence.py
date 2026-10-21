"""Bounded native-PDF observations for current figure refs; never visibility approval."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pymupdf

from .source_figure_evidence import figure_label_regions
from .source_glyph_evidence import _base, _file_hash, _font_evidence


def build_figure_label_glyph_evidence(bundle, task, candidate=None, *, pdf_path,
                                      source_snapshot_path, view_path,
                                      max_labels=256, max_refs=512, max_chars=1024,
                                      max_pages=4, max_fonts=8, max_output_bytes=262144):
    """Observe original span text and ordered native trace glyphs, not intended words.

    Candidate selectors supply IDs/refs/geometry only. Targets, claims and verdicts
    never enter this source-only envelope. Native text can be hidden or clipped;
    matching Unicode is not proof that complete lettering is visible in the PDF.
    """
    caps = {"labels": (max_labels, 256), "refs": (max_refs, 512),
            "chars": (max_chars, 1024), "pages": (max_pages, 4), "fonts": (max_fonts, 8),
            "output_bytes": (max_output_bytes, 262144)}
    if any(isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= top
           for v, top in caps.values()) or max_output_bytes < 2048:
        raise ValueError("Invalid bounded figure glyph evidence limits")
    for path, key in ((pdf_path, 'source_pdf_sha256'),
                      (source_snapshot_path, 'source_snapshot_sha256'), (view_path, 'view_sha256')):
        if not bundle.get(key) or _file_hash(path) != bundle[key]:
            raise ValueError('Figure glyph source identity mismatch: ' + key)
    snapshot = json.loads(Path(source_snapshot_path).read_text('utf-8'))
    view = json.loads(Path(view_path).read_text('utf-8'))
    if (view != bundle.get('document') or view.get('paper_id') != bundle.get('paper_id')
            or snapshot.get('sha256') != bundle['source_pdf_sha256']
            or view.get('source_sha256') != bundle['source_pdf_sha256']):
        raise ValueError('Figure glyph source document binding differs')
    figure_label_regions(bundle, task, candidate, source_snapshot=snapshot)
    block_id = task['figure']['block_id']
    block = next(b for b in view['blocks'] if b['id'] == block_id)
    allowed = set(block.get('source_refs', []))
    figure_spans = {s['id']: s for s in bundle.get('figure_source_spans', {}).get(block_id, [])}
    snapshot_spans = {s['id']: (p['page'], s) for p in snapshot['pages'] for s in p['spans']}
    if candidate is None:
        selectors = [{'label_id': block_id + '@source_ref' + str(i), 'refs': [ref]}
                     for i, ref in enumerate(figure_spans)]
        selection_kind = 'current_figure_original_refs_not_candidate_labels'
    else:
        selectors = [{'label_id': block_id + '@label' + str(i), 'refs': list(label.get('source_refs', []))}
                     for i, label in enumerate(candidate['figures'][0]['labels'])]
        selection_kind = 'current_candidate_reference_selectors_only'
    # Validate every selected ref, including those beyond a resource cap.
    for selection in selectors:
        for ref in selection['refs']:
            if ref not in allowed or ref not in figure_spans or ref not in snapshot_spans:
                raise ValueError('Foreign or unknown current figure glyph ref')
            page_no, span = snapshot_spans[ref]
            current = figure_spans[ref]
            box, roi = current['bbox'], task['figure']['region']['bbox']
            if max(box[0], roi[0]) >= min(box[2], roi[2]) or max(box[1], roi[1]) >= min(box[3], roi[3]):
                raise ValueError('Current figure glyph ref is fully outside the figure')
            if current.get('page') != page_no or page_no != task['figure']['region']['page']:
                raise ValueError('Figure glyph ref belongs to another page')
            if any(current.get(k) != span.get(k) for k in ('text', 'font', 'size')) or list(current['bbox']) != list(span['bbox']):
                raise ValueError('Figure glyph ref differs from current original snapshot')
    result = {'version': 'figure-native-glyph-v1', 'status': 'unavailable', 'source_only': True,
              'binding': {k: bundle[k] for k in ('paper_id', 'fingerprint', 'source_pdf_sha256',
                           'view_id', 'view_sha256', 'source_snapshot_sha256')},
              'block_id': block_id, 'selection_kind': selection_kind,
              'limits': {k: v for k, (v, _) in caps.items()}, 'labels': [], 'fonts': [],
              'omissions': [], 'unavailable': [], 'truncated': False,
              'full_visual_visibility_verified': False, 'author_intent_inferred': False,
              'translation_or_spatial_approval_granted': False,
              'scope': 'Native PDF text-dict Unicode and ordered trace glyph observations. '
                       'Glyph IDs are not raw PDF text-operator character codes. Text can be '
                       'clipped, masked, overpainted, invisible or an OCR layer; no visibility or intent proof.'}
    ref_count = char_count = 0
    seen_chars = set()
    cache, font_items, wanted = {}, {}, {}
    try:
        pdf = pymupdf.open(pdf_path)
    except (OSError, RuntimeError, ValueError) as exc:
        result['unavailable'].append(str(exc)[:300])
        pdf = None
    if pdf is not None:
        with pdf:
            for index, selection in enumerate(selectors):
                if index >= max_labels:
                    result['omissions'].append({'label_id': selection['label_id'], 'reason': 'label_cap'})
                    continue
                record = {'label_id': selection['label_id'], 'source_refs': selection['refs'],
                          'refs': [], 'unavailable': [], 'truncated': False}
                if not selection['refs']:
                    record['unavailable'].append('BBox-only label has no native source-ref identity')
                for ref in selection['refs']:
                    if ref_count >= max_refs:
                        record['unavailable'].append('Source ref cap reached')
                        record['truncated'] = True
                        continue
                    ref_count += 1
                    page_no, span = snapshot_spans[ref]
                    entry = {'source_ref': ref, 'page': page_no, 'source_bbox': list(span['bbox']),
                             'snapshot_text': span['text'][:256], 'snapshot_text_truncated': len(span['text']) > 256,
                             'provenance': 'original_native_PDF_text_extraction_not_visual_OCR_claim',
                             'traces': [], 'unavailable': []}
                    record['refs'].append(entry)
                    if not 1 <= page_no <= len(pdf):
                        entry['unavailable'].append('Original source page is outside actual PDF')
                        continue
                    if page_no not in cache:
                        if len(cache) >= max_pages:
                            entry['unavailable'].append('Page cap reached')
                            continue
                        page = pdf[page_no - 1]
                        page.set_rotation(0)
                        cache[page_no] = (page.get_text('dict', sort=False)['blocks'],
                                          page.get_texttrace(), page.get_fonts(full=True))
                    blocks, traces, fonts = cache[page_no]
                    selector = re.search(r'-p(\d+)-b(\d+)-l(\d+)-s(\d+)$', ref)
                    native = None
                    if selector and int(selector[1]) == page_no:
                        try:
                            native = blocks[int(selector[2])]['lines'][int(selector[3])]['spans'][int(selector[4])]
                        except (IndexError, KeyError, TypeError):
                            pass
                    if (native is None or native['text'] != span['text'] or native['font'] != span['font']
                            or list(native['bbox']) != list(span['bbox']) or native['size'] != span['size']
                            or tuple(blocks[int(selector[2])]['lines'][int(selector[3])]['dir']) != tuple(span['direction'])):
                        entry['unavailable'].append('Fresh native PDF span association is unavailable or differs')
                        continue
                    entry.update(native_pdf_text=native['text'][:256], native_pdf_text_truncated=len(native['text']) > 256,
                                 native_pdf_bbox=list(native['bbox']), native_pdf_font=native['font'],
                                 native_pdf_size=native['size'], native_pdf_direction=list(span['direction']), native_span_exact=True)
                    rect = pymupdf.Rect(span['bbox'])
                    for trace in traces:
                        if trace['font'] != span['font'] or tuple(trace['dir']) != tuple(span['direction']):
                            continue
                        chars = []
                        for position, (unicode_value, glyph_id, origin, bbox) in enumerate(trace['chars']):
                            if not rect.contains(pymupdf.Point((bbox[0]+bbox[2])/2, (bbox[1]+bbox[3])/2)):
                                continue
                            char_key = (page_no, trace.get('seqno'), position)
                            if char_key not in seen_chars:
                                if char_count >= max_chars:
                                    record['truncated'] = True
                                    continue
                                seen_chars.add(char_key)
                                char_count += 1
                            chars.append({'index_in_trace': position, 'unicode': unicode_value,
                                          'glyph_id': glyph_id, 'origin': list(origin), 'bbox': list(bbox)})
                        if not chars:
                            continue
                        matched = [f for f in fonts if _base(f[3]) == trace['font']]
                        entry['traces'].append({'seqno': trace.get('seqno'), 'type': trace.get('type'),
                            'opacity': trace.get('opacity'), 'font': trace['font'], 'size': trace['size'],
                            'direction': list(trace['dir']), 'trace_bbox': list(trace['bbox']),
                            'font_xrefs': [f[0] for f in matched],
                            'font_match': 'unique' if len(matched) == 1 else 'ambiguous_or_unavailable',
                            'selected_chars_in_native_trace_order': chars})
                        for font in matched:
                            font_items[font[0]] = font
                            wanted.setdefault(font[0], set()).update(chr(c['unicode']) for c in chars if 0 <= c['unicode'] <= 0x10ffff)
                    if not entry['traces']:
                        entry['unavailable'].append('No native trace observations in this exact source bbox')
                result['labels'].append(record)
            for xref, font in list(font_items.items())[:max_fonts]:
                result['fonts'].append(_font_evidence(pdf, font, wanted[xref], 65536))
            if len(font_items) > max_fonts:
                result['omissions'].append({'reason': 'font_cap', 'count': len(font_items)-max_fonts})
    # Share original trace metadata/glyphs across split refs. Keep occurrence
    # indices, never deduplicate repeated letters or change numeric precision.
    trace_pool = {}
    for label in result['labels']:
        for ref in label['refs']:
            linked = []
            for trace in ref['traces']:
                key = (ref['page'], trace['seqno'], trace['font'])
                if key not in trace_pool:
                    trace_pool[key] = {'trace_id': 'trace' + str(len(trace_pool)),
                        'page': ref['page'], **{k: v for k, v in trace.items()
                            if k != 'selected_chars_in_native_trace_order'}, '_chars': {}}
                pooled = trace_pool[key]
                indices = []
                for char in trace['selected_chars_in_native_trace_order']:
                    index = char['index_in_trace']
                    indices.append(index)
                    pooled['_chars'][index] = [index, char['unicode'], char['glyph_id'],
                                              *char['origin'], *char['bbox']]
                linked.append({'trace_id': pooled['trace_id'], 'char_indices': indices})
            ref['traces'] = linked
            ref['trace_association'] = 'single' if len(linked) == 1 else 'multiple_or_unavailable'
    result['native_trace_char_columns'] = ['index_in_trace', 'unicode', 'glyph_id',
                                          'origin_x', 'origin_y', 'bbox_x0', 'bbox_y0', 'bbox_x1', 'bbox_y1']
    result['native_traces'] = []
    for trace in trace_pool.values():
        chars = trace.pop('_chars')
        result['native_traces'].append({**trace, 'chars': [chars[k] for k in sorted(chars)]})
    result['truncated'] = bool(result['omissions']) or any(x['truncated'] for x in result['labels'])
    result['status'] = 'available' if any(r.get('native_span_exact') for x in result['labels'] for r in x['refs']) else 'unavailable'
    def wire_size():
        return len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    # A bounded output retains explicit evidence omissions instead of claiming complete coverage.
    while wire_size() > max_output_bytes - 128 and (result['fonts'] or result['labels']):
        result['truncated'] = True
        if result['fonts']:
            result['fonts'].pop()
            if not any(o.get('reason') == 'output_byte_cap_fonts' for o in result['omissions']):
                result['omissions'].append({'reason': 'output_byte_cap_fonts'})
        else:
            removed = result['labels'].pop()
            referenced = {t['trace_id'] for label in result['labels'] for ref in label['refs'] for t in ref['traces']}
            result['native_traces'] = [t for t in result['native_traces'] if t['trace_id'] in referenced]
            result['omissions'].append({'label_id': removed['label_id'], 'reason': 'output_byte_cap'})
    referenced = {t['trace_id'] for label in result['labels'] for ref in label['refs'] for t in ref['traces']}
    result['native_traces'] = [t for t in result['native_traces'] if t['trace_id'] in referenced]
    if wire_size() > max_output_bytes - 128:
        omitted = result['omissions']
        result['omissions'] = [{'reason': 'output_byte_cap_summary', 'count': len(omitted),
            'omissions_sha256': hashlib.sha256(json.dumps(omitted, sort_keys=True).encode()).hexdigest()}]
    if not result['labels']:
        result['status'] = 'unavailable'
    result['evidence_sha256'] = hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True,
                                        separators=(',', ':')).encode('utf-8')).hexdigest()
    return result
