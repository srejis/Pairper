"""Bounded, neutral PDF glyph evidence. No transcription decisions or repairs.

PDF character codes are font-local, not Unicode. get_texttrace glyph IDs are
also not character codes. Keep those observations and CMap-derived candidates
separate, including missing/ambiguous mappings, for a fresh source reviewer.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pymupdf


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _file_hash(path):
    with Path(path).open('rb') as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
        return digest.hexdigest()


def _base(name):
    return re.sub(r'^[A-Z]{6}\+', '', name)


def _unicode(hexadecimal):
    try:
        return bytes.fromhex(hexadecimal).decode('utf-16-be')
    except (ValueError, UnicodeError):
        return None


def _cmap_candidates(text, wanted):
    """Read explicit bfchar/bfrange entries only, retaining their exact lines."""
    result = []
    for block in re.findall(r'beginbfchar(.*?)endbfchar', text, re.S):
        for code, target in re.findall(r'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', block):
            value = _unicode(target)
            if value in wanted:
                result.append({'code_hex': code.upper(), 'unicode': value,
                               'mapping': f'<{code}> <{target}>', 'kind': 'bfchar'})
    for block in re.findall(r'beginbfrange(.*?)endbfrange', text, re.S):
        for first, last, target in re.findall(
                r'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]+>)', block):
            start, stop = int(first, 16), int(last, 16)
            if target.startswith('['):
                values = re.findall(r'<([0-9A-Fa-f]+)>', target)
                for offset, destination in enumerate(values[:4096]):
                    value = _unicode(destination)
                    if start + offset <= stop and value in wanted:
                        result.append({'code_hex': f'{start+offset:0{len(first)}X}', 'unicode': value,
                                       'mapping': f'<{first}> <{last}> {target[:512]}', 'kind': 'bfrange_array'})
            else:
                destination = target[1:-1]
                base = int(destination, 16)
                # Work from requested Unicode scalars; never expand an untrusted
                # range into billions of PDF character codes.
                for value in sorted(wanted):
                    encoded = value.encode('utf-16-be').hex()
                    if len(encoded) != len(destination):
                        continue
                    offset = int(encoded, 16) - base
                    if 0 <= offset <= stop - start:
                        result.append({'code_hex': f'{start+offset:0{len(first)}X}', 'unicode': value,
                                       'mapping': f'<{first}> <{last}> <{destination}>', 'kind': 'bfrange'})
    return sorted(result, key=lambda m: (len(m['code_hex']), m['code_hex'], m['unicode'], m['kind'], m['mapping']))


def _font_evidence(pdf, item, wanted, cap):
    xref, extension, subtype, basefont, resource, encoding, *_ = item
    result = {'xref': xref, 'base_font': basefont, 'resource_name': resource,
              'subtype': subtype, 'encoding_name': encoding,
              'character_code_scope': 'ToUnicode candidates, not independently located raw text operators'}
    if not xref:
        return {**result, 'status': 'unavailable', 'reason': 'Font has no indirect PDF dictionary'}
    dictionary = pdf.xref_object(xref)
    result['dictionary'] = dictionary[:2048]
    result['dictionary_sha256'] = _sha(dictionary.encode())
    result['dictionary_truncated'] = len(dictionary) > 2048
    for key in ('Encoding', 'ToUnicode'):
        kind, value = pdf.xref_get_key(xref, key)
        evidence = {'pdf_key_type': kind, 'pdf_key_value': value[:2048]}
        if kind == 'xref':
            target = int(value.split()[0])
            raw = pdf.xref_object(target)
            evidence.update(xref=target, dictionary=raw[:2048], dictionary_truncated=len(raw) > 2048)
            if pdf.xref_is_stream(target):
                # CMaps are source data, never instructions or corrections.
                data = pdf.xref_stream(target)
                evidence.update(stream_sha256=_sha(data), stream_bytes=len(data),
                                stream_truncated=len(data) > cap)
                if len(data) <= cap:
                    mappings = _cmap_candidates(data.decode('latin1'), wanted)
                    evidence.update(status='available', mappings=mappings[:256],
                                    mappings_truncated=len(mappings) > 256)
                else:
                    evidence.update(status='unavailable', reason='CMap stream exceeds processing cap')
            else:
                evidence['status'] = 'available'
        elif kind == 'null':
            evidence.update(status='unavailable', reason='PDF font has no explicit '+key)
        else:
            evidence['status'] = 'available'
        result[key] = evidence
    return result


def build_source_glyph_evidence(bundle, unit_ids=None, *, pdf_path=None,
                                source_snapshot=None, source_snapshot_path=None,
                                view_path=None, max_units=8, max_pages=4,
                                max_chars=256, max_fonts=8,
                                max_stream_bytes=65536, max_output_chars=24000):
    """Return source-only observations bound to PDF/snapshot/current-view hashes.

    Paths come from the caller's current Library, never from target/candidate
    content. Passing a snapshot dict is supported only alongside its original
    path, allowing byte-hash validation. This function never opens a database.
    ``unit_ids`` selects exact IDs; None uses the bounded first source units.
    Caps truncate explicitly; unavailable evidence never means a fidelity pass.
    """
    limits = dict(max_units=max(0, min(int(max_units), 64)), max_pages=max(0, min(int(max_pages), 16)),
                  max_chars=max(0, min(int(max_chars), 1024)), max_fonts=max(0, min(int(max_fonts), 32)),
                  max_stream_bytes=max(0, min(int(max_stream_bytes), 262144)),
                  max_output_chars=max(1024, min(int(max_output_chars), 100000)))
    result = {'version': 1, 'status': 'unavailable', 'source_only': True,
              'binding': {k: str(bundle.get(k, ''))[:128] for k in ('paper_id', 'source_pdf_sha256', 'view_id',
                          'view_sha256', 'source_snapshot_sha256', 'fingerprint')},
              'limits': limits, 'units': [], 'fonts': [], 'unavailable': [], 'truncated': False,
              'scope': 'Observed Unicode and glyph IDs from original PDF; font-local CMap code candidates. No interpretation, correction, author intent or approval.'}

    def finish():
        # Limit JSON bytes in a portable UTF-8 wire representation, not just
        # Python code points. Preserve identity/limitations when reducing data.
        cap = limits['max_output_chars']
        result['returned_unit_count'] = len(result['units'])
        def size():
            return len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf8'))
        while size() > cap and (result['units'] or result['fonts']):
            result['truncated'] = True
            # Honor the caller's priority order across the final wire budget,
            # preserving the first unit and its first font together longest.
            if len(result['units']) > 1:
                result['units'].pop()
            elif len(result['fonts']) > 1:
                result['fonts'].pop()
            elif result['units'] and result['units'][-1].get('glyphs'):
                result['units'][-1]['glyphs'].pop()
                result['units'][-1]['glyph_groups_omitted'] += 1
            elif result['fonts']:
                result['fonts'].pop()
            else:
                result['units'].pop()
            result['returned_unit_count'] = len(result['units'])
        if size() > cap:
            result['unavailable'] = ['Evidence envelope exceeds output cap']
            result['binding'] = {k: v for k, v in result['binding'].items() if k != 'fingerprint'}
            result['truncated'] = True
        if size() > cap:
            return {'version': 1, 'status': 'unavailable', 'source_only': True,
                    'binding': {k: result['binding'][k] for k in ('source_pdf_sha256', 'view_id', 'view_sha256', 'source_snapshot_sha256')},
                    'truncated': True, 'unavailable': ['Output envelope cap reached'], 'units': [], 'fonts': []}
        return result

    try:
        if not pdf_path or not source_snapshot_path or not view_path:
            raise ValueError('PDF, snapshot and current-view paths are required for byte-hash binding')
        for path, key in ((pdf_path, 'source_pdf_sha256'), (source_snapshot_path, 'source_snapshot_sha256'),
                          (view_path, 'view_sha256')):
            if not bundle.get(key) or _file_hash(path) != bundle[key]:
                raise ValueError('Source identity mismatch: '+key)
        snapshot = json.loads(Path(source_snapshot_path).read_text('utf8'))
        if source_snapshot is not None and source_snapshot != snapshot:
            raise ValueError('Supplied snapshot differs from hash-bound source file')
        view = json.loads(Path(view_path).read_text('utf8'))
        if view.get('paper_id') != bundle.get('paper_id') or view != bundle.get('document'):
            raise ValueError('Bundle document differs from hash-bound current-view file')
        if snapshot.get('sha256') != bundle['source_pdf_sha256']:
            raise ValueError('Snapshot refers to another original PDF')
        if view.get('source_sha256') != bundle['source_pdf_sha256']:
            raise ValueError('Current view refers to another original PDF')
        result['binding']['verified'] = True
        wanted_ids = (None if unit_ids is None else sorted(unit_ids)
                      if isinstance(unit_ids, (set, frozenset)) else list(dict.fromkeys(unit_ids)))
        by_id = {u['id']: u for u in bundle.get('units', [])}
        units = list(by_id.values()) if wanted_ids is None else [by_id[k] for k in wanted_ids if k in by_id]
        result['requested_unit_count'] = len(wanted_ids) if wanted_ids is not None else len(by_id)
        if wanted_ids:
            missing = set(wanted_ids) - by_id.keys()
            if missing:
                result['unavailable'].append({'unknown_unit_ids': sorted(missing)[:16]})
        if len(units) > limits['max_units']:
            result['truncated'] = True
        units = units[:limits['max_units']]
        result['selected_unit_count'] = len(units)
        refs = {s['id']: (p['page'], s) for p in snapshot.get('pages', []) for s in p.get('spans', [])}
        blocks = {b['id']: b for b in view.get('blocks', [])}
        font_items, font_wanted, page_cache = {}, {}, {}
        glyph_budget = limits['max_chars']
        with pymupdf.open(pdf_path) as pdf:
            for unit in units:
                record = {'unit_id': unit['id'], 'source_text_sha256': unit.get('source_text_sha256'),
                          'glyphs': [], 'unavailable': []}
                areas = []
                for ref in unit.get('source_refs', [])[:512]:
                    if ref in refs:
                        page, span = refs[ref]
                        areas.append((page, span['bbox'], ref))
                    else:
                        record['unavailable'].append('Unknown source ref: '+str(ref)[:160])
                if len(unit.get('source_refs', [])) > 512:
                    result['truncated'] = True
                if not areas:
                    region = blocks.get(unit.get('block_id'), {}).get('content', {}).get('region')
                    if region:
                        areas.append((region['page'], region['bbox'], None))
                if not areas:
                    record['unavailable'].append('No source-ref bbox or original block region')
                grouped = {}
                for page_no, bbox, ref in areas:
                    if page_no not in page_cache:
                        if len(page_cache) >= limits['max_pages']:
                            result['truncated'] = True
                            record['unavailable'].append('Page cap reached')
                            continue
                        if not 1 <= page_no <= len(pdf):
                            record['unavailable'].append('Source page outside original PDF')
                            continue
                        page = pdf[page_no - 1]
                        page_cache[page_no] = (page.get_texttrace(), page.get_fonts(full=True))
                    traces, fonts = page_cache[page_no]
                    rect = pymupdf.Rect(bbox)
                    for trace in traces:
                        matches = [item for item in fonts if _base(item[3]) == trace['font']]
                        for unicode_value, glyph_id, origin, char_box in trace['chars']:
                            if not rect.contains(pymupdf.Point((char_box[0]+char_box[2])/2, (char_box[1]+char_box[3])/2)):
                                continue
                            if unicode_value < 0 or unicode_value > 0x10ffff:
                                continue
                            text = chr(unicode_value)
                            key = (page_no, trace['font'], unicode_value, glyph_id)
                            if key not in grouped:
                                grouped[key] = {'unicode': text, 'codepoint': f'U+{unicode_value:04X}',
                                    'glyph_id': glyph_id, 'font': trace['font'], 'page': page_no,
                                    'font_xrefs': [item[0] for item in matches],
                                    'font_match': 'unique' if len(matches)==1 else 'ambiguous_or_unavailable',
                                    'bbox': list(char_box), 'origin': list(origin), 'source_ref': ref,
                                    'pdf_occurrences_in_selected_refs': 0}
                            grouped[key]['pdf_occurrences_in_selected_refs'] += 1
                            for item in matches:
                                font_items[item[0]] = item
                                font_wanted.setdefault(item[0], set()).add(text)
                values = list(grouped.values())
                record['glyphs'] = values[:glyph_budget]
                glyph_budget -= len(record['glyphs'])
                record['glyph_groups_available'] = len(values)
                record['glyph_groups_omitted'] = len(values)-len(record['glyphs'])
                result['truncated'] |= bool(record['glyph_groups_omitted'])
                result['units'].append(record)
            for xref, item in list(font_items.items())[:limits['max_fonts']]:
                result['fonts'].append(_font_evidence(pdf, item, font_wanted[xref], limits['max_stream_bytes']))
            result['truncated'] |= len(font_items) > limits['max_fonts']
        result['status'] = 'available' if any(u['glyphs'] for u in result['units']) else 'unavailable'
        if result['status'] == 'unavailable':
            result['unavailable'].append('No matched PDF glyph observations in selected scope')
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        result['unavailable'].append(str(error)[:400])
    return finish()
