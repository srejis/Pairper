"""Text comparisons retain exact source slices and independent mandatory coverage."""
from copy import deepcopy

import pytest

from pairper.explanation.catalog import digest, target_id, validate_targets
from pairper.explanation.projection import project_explanations
from pairper.explanation.service import ExplanationService, coverage, validate
from pairper.explanation.store import ExplanationStore
from pairper.explanation.text_math import decorate, inventory


def source(texts, block_type='paragraph'):
    bundle = {'paper_id': 'paper', 'source_identity': {'view_id': 'view'}, 'targets': [],
              'context': [{'block_id': 'p', 'type': block_type}], 'text_units': [
                  {'block_id': 'p', 'field': 'runs', 'run_index': i, 'block_type': block_type,
                   'text': text, 'source_refs': [f'ref{i}']} for i, text in enumerate(texts)]}
    bundle['fingerprint'] = digest(bundle)
    return bundle


def display(texts, translated=False):
    return {'paper_id': 'paper', 'view_id': 'view',
            **({'translation': {'status': 'published'}} if translated else {}),
            'blocks': [{'id': 'p', 'content': {'runs': [
                {'kind': 'text', 'text': text, 'bold': True, 'source_unit_ids': ['p/runs']}
                for text in texts]}}]}


def formula(target, **extra):
    return {'target_id': target['id'], 'kind': 'math', 'math_role': 'formula',
            'symbols': [{'symbol': 'SR', 'meaning': '실행 성공률'}],
            'meaning': [{'kind': 'text', 'text': '모델 실행에 성공한 경우다.'}],
            'process': [{'kind': 'text', 'text': '실행 성공률을 0과 비교한다.'}],
            'reviewed': True, **extra}


def test_adjacent_source_parts_inventory_does_not_change_legacy_fingerprint_or_raw_ids():
    base = source(['Model requires SR', ' >', ' 0.'])
    before = deepcopy(base)
    decorated = decorate(base)
    target, = decorated['supplemental_math']['targets']
    assert target['source_text'] == 'SR > 0'
    assert target['text_parts'] == [
        {'run_index': 0, 'start': 15, 'end': 17, 'text': 'SR'},
        {'run_index': 1, 'start': 0, 'end': 2, 'text': ' >'},
        {'run_index': 2, 'start': 0, 'end': 2, 'text': ' 0'}]
    assert base == before
    assert decorated['fingerprint'] == base['fingerprint']
    assert decorated['targets'] == base['targets']
    assert inventory(base) == inventory(base)
    assert validate_targets(base, [target]) == [target]
    assert target_id(target) == target['id']
    changed = deepcopy(target)
    changed['text_parts'][0]['start'] -= 1
    with pytest.raises(ValueError, match='exact source formula'):
        validate_targets(base, [changed])
    # Supplied inventory is metadata, never authority for source anchors.
    decorated['supplemental_math']['targets'][0]['source_text'] = 'SR > 10'
    with pytest.raises(ValueError, match='exact source formula'):
        validate_targets(decorated, decorated['supplemental_math']['targets'])


@pytest.mark.parametrize('text', [
    '(years > 32)', 'SR > 0.', 'x <= 1.5', 'RATE ≥ 95%', '(count != 0)',
])
def test_clear_comparisons_only(text):
    # != is deliberately outside the prose-comparison grammar.
    assert bool(inventory(source([text]))['targets']) == ('!=' not in text)


@pytest.mark.parametrize('text,block_type', [
    ('SR > 0', 'code'), ('SR > 0', 'reference'), ('SR > 0', 'authors'),
    ('maintaining >90% of performance', 'paragraph'),
    ('INPUT_DIM=21, N=40', 'paragraph'), ('C++/CUDA', 'paragraph'),
    ('https://host.test/x>0', 'paragraph'), ('Liu <SR1@host.test>', 'paragraph'),
    ('if SR > 0', 'paragraph'), ('return SR > 0;', 'paragraph'),
    ('SR > 0.5M', 'paragraph'), ('O(N) to O(1)', 'paragraph'),
    ('Sparsity is 1.0 - active neurons / total neurons.', 'paragraph'),
])
def test_nonformula_prose_settings_code_and_addresses_are_not_targets(text, block_type):
    assert inventory(source([text], block_type))['targets'] == []


def test_formula_parts_never_join_across_a_math_run_or_source_field():
    bundle = source(['SR', ' >0'])
    bundle['text_units'][1]['run_index'] = 2
    assert inventory(bundle)['targets'] == []
    bundle['text_units'][1]['run_index'] = 1
    bundle['text_units'][1]['field'] = 'items/0'
    assert inventory(bundle)['targets'] == []


def test_source_and_unique_whitespace_only_korean_projection_keep_full_formula_and_styles():
    texts = ['Model requires SR', ' >', ' 0.']
    bundle = source(texts)
    target, = inventory(bundle)['targets']
    bundle.update(targets=[target], explanations=[formula(target)])
    visible = display(texts)
    original = deepcopy(visible)
    projected = project_explanations(bundle, visible)
    assert [a['text'] for a in projected['targets'][0]['localized']] == ['SR', ' >', ' 0']
    assert projected['targets'][0]['source_text'] == 'SR > 0'
    assert visible == original
    korean = display(['실행 성공률 SR', '>', '0.'], True)
    projected = project_explanations(bundle, korean)
    assert [a['text'] for a in projected['targets'][0]['localized']] == ['SR', '>', '0']
    assert projected['targets'][0]['source_text'] == 'SR > 0'
    for text in ['SR>0 and SR > 0', '성공률은 0보다 크다.', 'OTHER>0', 'SR>0.5']:
        assert not project_explanations(bundle, display([text], True))['targets'][0]['localized']
    foreign = display(['SR>0'], True)
    foreign['blocks'][0]['id'] = 'q'
    foreign['blocks'][0]['content']['runs'][0]['source_unit_ids'] = ['q/runs']
    foreign['blocks'].append({'id': 'p', 'content': {'runs': []}})
    assert not project_explanations(bundle, foreign)['targets'][0]['localized']


@pytest.mark.parametrize('tail', [
    '이 필요하다.', '가 조건이다.', '은 조건이다.', '는 조건이다.', '을 확인한다.',
    '를 확인한다.', '과 비교한다.', '와 비교한다.', '로 읽는다.', '으로 읽는다.',
    '에 해당한다.', '에서 출발한다.', '도 포함한다.', '만 남긴다.', '이, 조건이다.',
    '이', '이)',
])
def test_registered_korean_comparison_accepts_complete_particle_without_anchoring_it(tail):
    bundle = source(['Model requires SR', ' >', ' 0.'])
    target, = inventory(bundle)['targets']
    bundle.update(targets=[target], explanations=[formula(target)])
    before = deepcopy(bundle)
    korean = display(['실행 가능한 모델, 즉 SR > 0' + tail], True)
    untouched = deepcopy(korean)
    out = project_explanations(bundle, korean)
    anchor, = out['targets'][0]['localized']
    assert anchor['text'] == 'SR > 0'
    assert korean['blocks'][0]['content']['runs'][0]['text'][anchor['end']:] == tail
    assert bundle == before and korean == untouched
    assert out['targets'][0]['source_text'] == target['source_text'] == 'SR > 0'


@pytest.mark.parametrize('text', [
    'SR > 00', 'SR > 01', 'SR > 0.5', 'SR > 0x', 'SR > 0_', 'SR > 0_1',
    'SR > 0e2', 'SR > 0β', 'SR > 0이상', 'SR > 0변수', 'SR > 0이x',
    'SR > 0이_1', 'SR > 0이0', 'SR > 0이나',
])
def test_korean_particle_exception_does_not_accept_numeric_identifier_or_word_extensions(text):
    bundle = source(['SR > 0.'])
    target, = inventory(bundle)['targets']
    bundle.update(targets=[target], explanations=[formula(target)])
    assert not project_explanations(bundle, display([text], True))['targets'][0]['localized']


def test_particle_exception_keeps_source_discovery_unique_provenance_and_three_part_identity():
    base = source(['Model requires SR', ' >', ' 0.'])
    decorated = decorate(base)
    target, = decorated['supplemental_math']['targets']
    decorated.update(targets=[target], explanations=[formula(target)])
    original = deepcopy(decorated)
    korean = display(['즉 SR', ' >', ' 0이 필요하다.'], True)
    out = project_explanations(decorated, korean)
    assert [a['text'] for a in out['targets'][0]['localized']] == ['SR', ' >', ' 0']
    assert decorated == original and decorated['fingerprint'] == base['fingerprint']
    assert not inventory(source(['SR > 0이 필요하다.']))['targets']
    assert not project_explanations(decorated, display(['SR > 0이 필요하고 SR > 0이 필요하다.'], True))['targets'][0]['localized']
    foreign = display(['SR > 0이 필요하다.'], True)
    foreign['blocks'][0]['id'] = 'other'
    foreign['blocks'][0]['content']['runs'][0]['source_unit_ids'] = ['other/runs']
    assert not project_explanations(decorated, foreign)['targets'][0]['localized']


def test_supplemental_formula_is_mandatory_and_requires_reviewed_disposition_or_full_process():
    bundle = source(['SR >0.'])
    target, = inventory(bundle)['targets']
    with pytest.raises(ValueError, match='Every registered'):
        validate(bundle, [], [], True, True)
    with pytest.raises(ValueError, match='evaluation process'):
        validate(bundle, [target], [formula(target, process=[])])
    validate(bundle, [target], [formula(target)], True, True)
    disposition = formula(target, math_role='notation', symbols=[], meaning=[], process=[],
                          exclusion_reason='독립 검토에서 원문 비교식 후보의 비수학적 사용을 확인했다.')
    validate(bundle, [target], [disposition], True, True)
    report = coverage(bundle, {'status': 'published', 'explanations': [formula(target)]})
    assert report['raw_math_count'] == 0
    assert report['supplemental_math_count'] == report['reviewed_supplemental_math_count'] == 1
    assert report['complete']
    assert not coverage(bundle, {'status': 'published', 'explanations': []})['complete']


def test_independent_persisted_store_cannot_publish_without_supplemental_review(tmp_path):
    base = decorate(source(['SR > 0.']))
    service = object.__new__(ExplanationService)
    service.prepare = lambda paper_id: deepcopy(base)
    service.store = ExplanationStore(tmp_path / 'explanations')
    draft = service.save('paper', [], replace=True)
    target, = draft['targets']
    assert target['id'] == target_id(target)
    with pytest.raises(ValueError, match='Every registered'):
        service.publish('paper', expected_revision=draft['revision'])
    draft = service.save('paper', [formula(target, reviewed=False)])
    with pytest.raises(ValueError, match='semantic review'):
        service.publish('paper', expected_revision=draft['revision'])
    draft = service.save('paper', [formula(target)])
    service.publish('paper', expected_revision=draft['revision'])
    published = service.get('paper')
    assert published['complete'] and published['coverage']['supplemental_math_count'] == 1
    assert published['fingerprint'] == base['fingerprint']
    assert published['targets'][0]['source_text'] == 'SR > 0'


@pytest.mark.parametrize('translated', [False, True])
def test_reviewed_formula_hides_only_overlapping_term_occurrence_and_preserves_canonical_ids(translated):
    texts = ['SR', ' >', ' 0. SR is success.']
    bundle = source(texts)
    math, = inventory(bundle)['targets']
    terms = validate_targets(bundle, [
        {'kind': 'term', 'block_id': 'p', 'field': 'runs', 'run_index': i,
         'start': start, 'end': start + 2, 'source_text': 'SR', 'concept_key': 'success',
         'aliases': []} for i, start in [(0, 0), (2, 4)]])
    bundle.update(targets=[*terms, math], explanations=[formula(math)])
    visible = display(['SR>0. SR은 성공률이다.'] if translated else texts, translated)
    before = deepcopy((bundle, visible))
    projected = project_explanations(bundle, visible)
    lookup = {target['id']: target for target in projected['targets']}
    assert set(lookup) == {math['id'], *(term['id'] for term in terms)}
    assert lookup[math['id']]['source_text'] == 'SR > 0'
    assert len(lookup[math['id']]['localized']) == (1 if translated else 3)
    if translated:
        assert [a['start'] for a in lookup[terms[0]['id']]['localized']] == [6]
        assert lookup[terms[1]['id']]['shared_with_target_id'] == terms[0]['id']
        assert projected['projection']['formula_shadowed_only_target_ids'] == []
    else:
        assert lookup[terms[0]['id']]['localized'] == []
        assert lookup[terms[1]['id']]['localized'][0]['text'] == 'SR'
        assert projected['projection']['formula_shadowed_only_target_ids'] == [terms[0]['id']]
    report = projected['projection']
    assert report['formula_shadowed_occurrence_count'] == 1
    assert report['formula_shadowed_target_ids'] == [terms[0]['id']]
    assert report['formula_shadowed_occurrences'][0]['formula_target_ids'] == [math['id']]
    assert report['unresolved_target_ids'] == report['alias_collision_target_ids'] == []
    assert (bundle, visible) == before


def test_fully_shadowed_term_is_intentional_display_choice_without_semantic_merging():
    bundle = source(['SR >0.'])
    math, = inventory(bundle)['targets']
    term, = validate_targets(bundle, [{'kind': 'term', 'block_id': 'p', 'field': 'runs',
                'run_index': 0, 'start': 0, 'end': 2, 'source_text': 'SR'}])
    bundle.update(targets=[term, math], explanations=[formula(math)])
    projected = project_explanations(bundle, display(['SR >0.']))
    assert projected['targets'][0]['localized'] == []
    assert 'shared_with_target_id' not in projected['targets'][0]
    assert projected['projection']['formula_shadowed_only_target_ids'] == [term['id']]
    assert projected['projection']['unresolved_target_ids'] == []
    bundle['explanations'][0]['reviewed'] = False
    unreviewed = project_explanations(bundle, display(['SR >0.']))
    assert unreviewed['targets'][0]['localized']
    assert unreviewed['projection']['formula_shadowed_occurrence_count'] == 0


def test_formula_shadowing_does_not_hide_unrelated_alias_collision_diagnostics():
    bundle = source(['SR >0. other'])
    math, = inventory(bundle)['targets']
    terms = validate_targets(bundle, [
        {'kind': 'term', 'block_id': 'p', 'field': 'runs', 'run_index': 0,
         'start': start, 'end': end, 'source_text': quote, 'concept_key': quote,
         'aliases': ['SR']} for start, end, quote in [(0, 2, 'SR'), (7, 12, 'other')]])
    bundle.update(targets=[*terms, math], explanations=[formula(math)])
    projected = project_explanations(bundle, display(['SR >0. SR'], True))
    assert set(projected['projection']['alias_collision_target_ids']) == {t['id'] for t in terms}
    assert set(projected['projection']['unresolved_target_ids']) == {t['id'] for t in terms}
    assert projected['projection']['formula_shadowed_only_target_ids'] == []
