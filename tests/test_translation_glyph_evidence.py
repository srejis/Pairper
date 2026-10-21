"""Fresh review gets PDF observations, never a scanner's answer."""
import copy
from pathlib import Path
from types import SimpleNamespace

from pairper.translation import source_glyph_evidence
from pairper.translation.prompts import project_request
from test_translation_workflow import setup_workflow as setup_workflow


def test_code_literal_not_crowded_out_by_64_prose_units_and_cache_detached(setup_workflow, monkeypatch):
    workflow, service = setup_workflow()
    service.library = SimpleNamespace(root=Path('source-library'),
                                     paper=lambda _: {'original_path': 'originals/source.pdf'})
    prose = [{'id': f'p{i}/runs', 'source_text': 'Ordinary original prose.'} for i in range(64)]
    code = {'id': 'prompt/code_lines/0', 'source_text': '‘‘‘python'}
    task = {'units': prose + [code], 'candidate': 'MUST_NOT_READ',
            'notes_to_investigate': ['MUST_NOT_READ']}
    calls = []
    def evidence(bundle, ids, **paths):
        calls.append((copy.deepcopy(bundle), list(ids), paths))
        return {'source_only': True, 'observed': ['U+2018'], 'status': 'available'}
    monkeypatch.setattr(source_glyph_evidence, 'build_source_glyph_evidence', evidence)
    first = workflow._source_glyph_evidence(service.bundle, task)
    first['observed'].append('mutated by caller')
    task['candidate'] = 'DIFFERENT_CANDIDATE'
    second = workflow._source_glyph_evidence(service.bundle, task)
    assert len(calls) == 1 and calls[0][1] == [code['id']]
    assert second['observed'] == ['U+2018']
    assert set(calls[0][2]) == {'pdf_path', 'source_snapshot_path', 'view_path'}
    assert 'MUST_NOT_READ' not in repr(calls)
    changed = {**service.bundle, 'fingerprint': 'new-source'}
    workflow._source_glyph_evidence(changed, task)
    assert len(calls) == 2


def test_ordinary_prose_does_not_pay_for_irrelevant_font_dump(setup_workflow, monkeypatch):
    workflow, service = setup_workflow()
    service.library = SimpleNamespace(root=Path('source-library'))
    def unexpected(*args, **kwargs):
        raise AssertionError('No glyph extraction needed for ordinary prose')
    monkeypatch.setattr(source_glyph_evidence, 'build_source_glyph_evidence', unexpected)
    assert workflow._source_glyph_evidence(service.bundle, {
        'units': [{'id': 'text/runs', 'source_text': 'Ordinary original prose.'}]}) is None


def test_fresh_review_projection_and_hash_include_only_neutral_pdf_observations(setup_workflow):
    workflow, _ = setup_workflow()
    neutral = {'source_only': True, 'status': 'available', 'binding': {'view_sha256': 'original'},
               'fonts': [{'ToUnicode': {'mappings': [{'code_hex': '60', 'unicode': '‘'}]}}]}
    request = {'phase': 'review', 'kind': 'text', 'task': {'units': []},
               'source_glyph_evidence': neutral,
               'source_verification_feedback': {'notes': ['OLD_SCANNER_VERDICT']},
               'issues': ['OLD_REVIEW_VERDICT'], 'review_items': []}
    projected = project_request(request)
    assert projected['source_glyph_evidence'] == neutral
    assert 'OLD_SCANNER_VERDICT' not in repr(projected)
    assert 'OLD_REVIEW_VERDICT' not in repr(projected)
    digest = workflow._review_hash(request)
    request['source_glyph_evidence']['binding']['view_sha256'] = 'changed'
    assert workflow._review_hash(request) != digest
