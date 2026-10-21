"""Only an actual source footnote marker can separate a translated term."""
from types import SimpleNamespace

import pytest

from pairper.document.models import Block, Content, Document, TextRun
from pairper.explanation.catalog import digest, prepare_catalog, validate_targets
from pairper.explanation.projection import project_explanations
from pairper.explanation.service import ExplanationService


def setup(*, source_type='footnote', superscript=True, marker='4',
          translated='4Hugging Face에 기반한 테스트', name='Hugging Face',
          visible_type='footnote', provenance='f/runs'):
    source = {'pages': [{'page': 1, 'spans': [
        {'id': 'marker', 'text': marker}, {'id': 'body', 'text': f'Our {name}-based testing.'}]}]}
    document = Document(paper_id='p', source_sha256='sha', blocks=[Block(
        id='f', type=source_type, order=0, content=Content(runs=[
            TextRun(refs=['marker'], superscript=superscript),
            TextRun(refs=['body'], before='none')]))])
    bundle = prepare_catalog(document, source, {'view_id': 'v'})
    target = {'kind': 'term', 'block_id': 'f', 'field': 'runs', 'run_index': 1,
              'start': 4, 'end': 4 + len(name), 'source_text': name, 'concept_key': 'tool'}
    bundle['targets'] = validate_targets(bundle, [target])
    bundle['explanations'] = [{'kind': 'term', 'target_id': bundle['targets'][0]['id'], 'reviewed': True}]
    display = {'paper_id': 'p', 'view_id': 'v', 'translation': {'session_id': 'ko'},
        'blocks': [{'id': 'f', 'type': visible_type, 'content': {'runs': [
            {'kind': 'text', 'text': translated, 'source_unit_ids': [provenance]}]}}]}
    return bundle, display


def test_source_numeric_superscript_marker_allows_only_following_literal():
    bundle, display = setup()
    before = display['blocks'][0]['content']['runs'][0]['text']
    result = project_explanations(bundle, display)
    assert result['targets'][0]['localized'] == [{'block_id': 'f', 'field': 'runs',
        'run_index': 0, 'start': 1, 'end': 13, 'text': 'Hugging Face'}]
    assert display['blocks'][0]['content']['runs'][0]['text'] == before


def test_reader_marker_annotation_does_not_change_scientific_source_fingerprint():
    bundle, _ = setup()
    # Reconstruct the catalog-only fields before the test appended selected terms.
    raw = {k: v for k, v in bundle.items() if k not in (
        'fingerprint', 'source_footnote_markers', 'explanations')}
    raw['targets'] = []
    assert digest(raw) == bundle['fingerprint']
    assert bundle['source_footnote_markers'][0]['source_refs'] == ['marker']


def test_existing_publication_receives_fresh_reader_metadata_without_republication():
    bundle, display = setup()
    saved = {**bundle, 'revision': 2, 'status': 'published'}
    saved.pop('source_footnote_markers')
    saved['explanations'] = [{**saved['explanations'][0], 'category': '소프트웨어 플랫폼',
        'description': [{'kind': 'text', 'text': '모델과 실행 도구를 제공한다.'}]}]
    service = object.__new__(ExplanationService)
    service.prepare = lambda paper_id: bundle
    service.store = SimpleNamespace(read=lambda *args: saved)
    current = service.get('p')
    assert current['revision'] == 2 and 'source_footnote_markers' not in saved
    assert project_explanations(current, display)['targets'][0]['localized'][0]['start'] == 1


def test_matching_provenance_on_an_ordinary_visible_paragraph_is_not_a_footnote():
    bundle, display = setup()
    display['blocks'][0]['content']['runs'][0]['text'] = 'footnote elsewhere'
    display['blocks'].append({'id': 'ordinary', 'type': 'paragraph', 'content': {'runs': [
        {'kind': 'text', 'text': '4Hugging Face mention', 'source_unit_ids': ['f/runs']}]}})
    assert project_explanations(bundle, display)['targets'][0]['localized'] == []


@pytest.mark.parametrize('options', [
    {'source_type': 'paragraph'},
    {'superscript': False},
    {'marker': 'x'},
    {'translated': '5Hugging Face에 기반한 테스트'},
    {'translated': '문장 중간4Hugging Face에 기반한 테스트'},
    {'translated': '44Hugging Face에 기반한 테스트'},
    {'visible_type': 'paragraph'},
    {'provenance': 'another/runs'},
    {'name': 'A100', 'translated': '4A1000 GPU'},
])
def test_ordinary_numbers_other_markers_and_word_substrings_are_not_boundaries(options):
    bundle, display = setup(**options)
    assert project_explanations(bundle, display)['targets'][0]['localized'] == []
