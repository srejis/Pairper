"""Exact current translated-literal provenance for narrowly reviewed alias corrections."""
import hashlib


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def complete_occurrence_proposal(service, bundle, target, literal, expected_translation_identity):
    """Read-only complete literal inventory; this makes no semantic equivalence claim."""
    if not isinstance(literal, str) or not literal.strip():
        raise ValueError('Alias correction needs a nonempty literal')
    surfaces = service.translation_surfaces(bundle['paper_id'], bundle)
    identity = {k: surfaces.get(k) for k in ('translation_session_id', 'translation_revision', 'source_identity')}
    if identity != expected_translation_identity:
        raise ValueError('Alias proposal is bound to a different current Korean translation revision')
    source_key = f"{target['block_id']}/{target['field']}"
    source_units = [u for u in bundle['text_units'] if all(
        u[k] == target[k] for k in ('block_id', 'field', 'run_index'))]
    if len(source_units) != 1:
        raise ValueError('Alias correction requires an exact unique original term source unit')
    matches = []
    surface_keys = set()
    for surface in surfaces['surfaces']:
        if source_key not in surface['source_unit_ids']:
            continue
        key = tuple(surface[k] for k in ('block_id', 'field', 'run_index'))
        if key in surface_keys:
            raise ValueError('Alias provenance has duplicate surface keys')
        surface_keys.add(key)
        start = 0
        while (start := surface['text'].find(literal, start)) >= 0:
            matches.append((surface, start))
            start += 1
    if not matches:
        raise ValueError('Alias literal must occur exactly once by default; no exact provenance-bound occurrence exists')
    fields = []
    seen = set()
    for surface, _ in matches:
        key = tuple(surface[k] for k in ('block_id', 'field', 'run_index'))
        if key in seen:
            continue
        seen.add(key)
        fields.append({'surface_key': dict(zip(('block_id', 'field', 'run_index'), key, strict=True)),
            'surface_source_unit_ids': surface['source_unit_ids'],
            'surface_sha256': sha(surface['text']), 'text': surface['text'],
            'positions': [{'start': start, 'end': start + len(literal)}
                          for item, start in matches if item is surface]})
    return {'mode': 'complete_occurrences', 'literal': literal, 'translation_identity': identity,
        'source_fingerprint': bundle['fingerprint'], 'source_unit_key': source_key,
        'source_unit_sha256': sha(source_units[0]['text']),
        'source_binding': {k: target[k] for k in ('block_id', 'field', 'run_index', 'start', 'end', 'source_text')},
        'surfaces': fields, 'occurrence_count': len(matches),
        'scientific_facts_source': 'original_paper_only'}


def anchor(service, bundle, target, literal, expected_translation_identity, *, complete_occurrences=None):
    proposal = complete_occurrence_proposal(service, bundle, target, literal, expected_translation_identity)
    if complete_occurrences is not None:
        if complete_occurrences != proposal:
            raise ValueError('Complete alias occurrence proof must exactly match every current field and position')
        return proposal
    if proposal['occurrence_count'] != 1:
        raise ValueError('Alias literal must occur exactly once in the current provenance-bound Korean field')
    field = proposal['surfaces'][0]
    return {'literal': literal, 'translation_identity': proposal['translation_identity'],
        'source_unit_key': proposal['source_unit_key'], 'source_unit_sha256': proposal['source_unit_sha256'],
        'source_binding': proposal['source_binding'],
        **{k: field[k] for k in ('surface_key', 'surface_source_unit_ids', 'surface_sha256', 'text')},
        **field['positions'][0],
        'occurrence_count': 1, 'scientific_facts_source': 'original_paper_only'}


def diagnostics(service, paper_id):
    bundle = service.prepare(paper_id)
    saved = service.get(paper_id)
    surfaces = service.translation_surfaces(paper_id, bundle)
    identity = {k: surfaces.get(k) for k in ('translation_session_id', 'translation_revision', 'source_identity')}
    if not identity['translation_session_id']:
        return {'paper_id': paper_id, 'source_fingerprint': bundle['fingerprint'],
                'translation_identity': identity, 'status': 'no_current_translation',
                'unresolved_terms': [], 'korean_fields': []}
    projected = service.display(paper_id, identity['translation_session_id'])
    unresolved = set(projected['projection']['unresolved_target_ids'])
    terms, fields = [], {}
    for target in saved['targets']:
        if target['kind'] != 'term' or target['id'] not in unresolved:
            continue
        unit = f"{target['block_id']}/{target['field']}"
        keys = []
        for surface in surfaces['surfaces']:
            if unit not in surface['source_unit_ids']:
                continue
            key = f"{surface['block_id']}/{surface['field']}/{surface['run_index']}"
            fields[key] = {**surface, 'surface_sha256': sha(surface['text'])}
            keys.append(key)
        terms.append({'target_id': target['id'],
            'source_binding': {k: target[k] for k in ('block_id', 'field', 'run_index', 'start', 'end', 'source_text')},
            'concept_key': target.get('concept_key'), 'aliases': target.get('aliases', []),
            'korean_field_keys': keys})
    return {'paper_id': paper_id, 'source_fingerprint': bundle['fingerprint'],
        'published_revision': saved['revision'], 'translation_identity': identity,
        'projection': projected['projection'], 'unresolved_terms': terms, 'korean_fields': fields,
        'status': 'current_literal_diagnostics_only_no_automatic_equivalence_claim'}
