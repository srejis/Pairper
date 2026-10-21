"""Source binding, independent publication, and visible occurrence regressions."""
import copy
import json

import pytest
from conftest import text_blocks

from pairper.document.models import Block, Content, Document, MathRun, Region, TextRun
from pairper.explanation.catalog import (
    math_content_kind,
    normalize_unique_term_offsets,
    prepare_catalog,
    target_id,
    target_math_content_kind,
    validate_targets,
)
from pairper.explanation.models import Explanation
from pairper.explanation.projection import project_explanations
from pairper.explanation.service import ExplanationService, validate


def catalog():
    region = Region(page=1, bbox=(0, 0, 10, 10))
    def inline():
        return MathRun(latex='x+y', region=region, refs=['m'])

    def text():
        return TextRun(refs=['t'])
    blocks = [
        Block(id='p', type='paragraph', order=0, content=Content(runs=[text(), inline()])),
        Block(id='l', type='list', order=1, content=Content(items=[[inline()]])),
        Block(id='c', type='caption', order=2, content=Content(runs=[inline()])),
        Block(id='t', type='table', order=3, content=Content(region=region, source_verified=True,
              cells=[{'row': 0, 'column': 0, 'runs': [inline()]}])),
        Block(id='e', type='equation', order=4,
              content=Content(region=region, latex='a=b', source_verified=True)),
    ]
    source = {'pages': [{'page': 1, 'spans': [
        {'id': 't', 'text': '😀 MLP와 attention', 'bbox': [0, 0, 1, 1]},
        {'id': 'm', 'text': 'x+y', 'bbox': [2, 0, 3, 1]},
    ]}]}
    return prepare_catalog(Document(paper_id='paper', source_sha256='sha', blocks=blocks),
                           source, {'view_id': 'view', 'view_sha256': 'digest'})


def term(bundle, text='MLP', start=2, end=5):
    return {'kind': 'term', 'block_id': 'p', 'field': 'runs', 'run_index': 0,
            'start': start, 'end': end, 'source_text': text}


def formula_entry(target, **extra):
    return {'target_id': target['id'], 'kind': 'math',
            'symbols': [{'symbol': 'x', 'meaning': '첫 번째 입력값'}],
            'meaning': [{'kind': 'text', 'text': '입력값을 더한다.'}],
            'process': [{'kind': 'text', 'text': '두 입력값을 받아 더한다.'}], **extra}


def test_catalog_all_inline_contexts_and_stable_occurrences():
    bundle = catalog()
    assert len(bundle['targets']) == 5
    assert {t['field'] for t in bundle['targets']} == {'runs', 'items/0', 'cells/0', 'latex'}
    assert len({t['id'] for t in bundle['targets']}) == 5
    assert bundle == catalog()
    assert all(t['id'] == target_id(t) for t in bundle['targets'])


def test_unicode_offsets_exact_quotes_and_no_sentence_targets():
    bundle = catalog()
    # Korean particles do not make a Latin acronym part of a different word.
    target = term(bundle, 'MLP와', 2, 6)
    assert validate_targets(bundle, [target])[0]['source_text'] == 'MLP와'
    assert validate_targets(bundle, [term(bundle)])[0]['source_text'] == 'MLP'
    for invalid in [term(bundle, 'MLP', 3, 6), term(bundle, 'LP', 3, 5),
                    term(bundle, '😀 MLP와 attention.', 0, 30)]:
        with pytest.raises(ValueError):
            validate_targets(bundle, [invalid])
    valid = validate_targets(bundle, [target])[0]
    with pytest.raises(ValueError, match='overlap'):
        validate_targets(bundle, [valid, valid])
    changed = copy.deepcopy(bundle['targets'][0])
    changed['source_text'] = 'arbitrary replacement'
    with pytest.raises(ValueError, match='exact source'):
        validate_targets(bundle, [changed])


def test_review_complete_and_meaning_first_prose_gates():
    bundle = catalog()
    entries = [formula_entry(t) for t in bundle['targets']]
    with pytest.raises(ValueError, match='review'):
        validate(bundle, bundle['targets'], entries, True, True)
    for entry in entries:
        entry['reviewed'] = True
    assert len(validate(bundle, bundle['targets'], entries, True, True)[1]) == 5
    with pytest.raises(ValueError, match='Every registered'):
        validate(bundle, bundle['targets'], entries[:-1], True, True)
    entry = formula_entry(bundle['targets'][0], process=[])
    with pytest.raises(ValueError, match='process'):
        Explanation.model_validate(entry)
    assert Explanation.model_validate({**entry, 'math_role': 'symbol'})
    with pytest.raises(ValueError, match='human meaning'):
        Explanation.model_validate(formula_entry(bundle['targets'][0], meaning=[
            {'kind': 'math', 'latex': 'x'}, {'kind': 'text', 'text': '입력'}]))


def test_completion_requires_all_source_math_not_just_supplied_target_list():
    bundle = catalog()
    with pytest.raises(ValueError, match='Every registered'):
        validate(bundle, [], [], require_review=True, require_complete=True)
    with pytest.raises(ValueError, match='Every registered'):
        validate(bundle, bundle['targets'][:1],
                 [formula_entry(bundle['targets'][0], reviewed=True)], True, True)


@pytest.mark.parametrize(('latex', 'expected'), [
    ('x+y', 'formula'), (r'\frac{x}{y}', 'formula'), (r'\operatorname{MLP}_{S_M}(y)', 'formula'),
    (r'\widehat y_i', 'symbol'), (r'\widetilde{y}_{i}', 'symbol'),
    (r'\boldsymbol x', 'symbol'), (r'\mathbf{W}^{(1)}_{S_M}', 'symbol'),
    ('{}^{1,2}', 'notation'), (r'\{', 'notation'), (r'\times', 'notation'),
    ('10^{19}', 'literal'), (r'\textbf{98.2}\pm0.3', 'literal'),
    (r'I^\star', 'symbol'), (r'\mathcal{M}^\star', 'symbol'),
    (r'g_\theta^\prime', 'symbol'), (r'A_\perp', 'symbol'),
    (r'\begin{gathered}0.300\\(\pm0.274)\end{gathered}', 'literal'),
    (r'\begin{gathered} 0.300 \\ (\pm 0.274) \end{gathered}', 'literal'),
    (r'\begin{gathered}1\\2\end{gathered}', 'formula'),
    (r'\begin{gathered}x\\(\pm y)\end{gathered}', 'formula'),
    (r'\mathcal{M}^\star=\mathcal{A}_{o}(I^\star)', 'formula'),
    (r'\begin{gathered}a=0.300\\(\pm0.274)\end{gathered}', 'formula'),
    ('42^{nd}', 'formula'),
    (r'(W^2_{S_M})', 'symbol'), (r'(\boldsymbol\alpha^{\prime})', 'symbol'),
    (r'\left(\widehat y_i\right)', 'symbol'), ('(d)', 'symbol'),
    (r'(x+y)', 'formula'), (r'(x)+(y)', 'formula'),
])
def test_source_math_roles_do_not_turn_formulas_into_unexplained_symbols(latex, expected):
    assert math_content_kind(latex) == expected


def test_conference_ordinals_need_source_context_and_reviewed_disposition():
    bundle = catalog()
    target = {**bundle['targets'][0], 'source_text': '42^{nd}'}
    target['id'] = target_id(target)
    bundle['targets'] = [target]
    assert target_math_content_kind(bundle, target) == 'formula'
    context = next(c for c in bundle['context'] if c['block_id'] == target['block_id'])
    context.update(type='footnote', text='Proceedings of the \\(42^{nd}\\) International Conference on Machine Learning')
    assert target_math_content_kind(bundle, target) == 'notation'
    disposition = formula_entry(target, math_role='notation', exclusion_reason='학회 개최 순서를 표시하는 서수다.',
                                symbols=[], meaning=[], process=[])
    with pytest.raises(ValueError, match='semantic review'):
        validate(bundle, [target], [disposition], True, True)
    assert validate(bundle, [target], [{**disposition, 'reviewed': True}], True, True)
    context.update(type='paragraph', text='The expression 42^{nd} denotes a numerical exponent.')
    assert target_math_content_kind(bundle, target) == 'formula'


def test_unique_quote_resolver_counts_unicode_without_mutating_response_or_ids():
    bundle = catalog()
    request = {'stage': 'generate', 'packet': {'text_units': bundle['text_units']}}
    raw = {'items': [{'key': 'term1', 'target': term(bundle, 'attention', 6, 15)}]}
    raw['items'][0]['target']['id'] = 'existing-id-must-not-be-renamed'
    original = copy.deepcopy(raw)
    normalized, corrections = normalize_unique_term_offsets(request, raw)
    target = normalized['items'][0]['target']
    assert (target['start'], target['end']) == (7, 16)
    assert target['id'] == 'existing-id-must-not-be-renamed'
    assert raw == original
    assert corrections[0]['method'] == 'unique_exact_quote'
    with pytest.raises(ValueError, match='Target ID'):
        validate_targets(bundle, [target])
    assert normalize_unique_term_offsets(request, normalized)[1] == []


@pytest.mark.parametrize('latex', [r'{}^{\dagger *}', r'{}^\dagger', r'{}^*',
                                   r'{}^{\ddagger}', r'{}^{\dagger\ast}'])
def test_empty_base_author_contribution_superscripts_are_reviewed_notation(latex):
    bundle = catalog()
    target = {**bundle['targets'][0], 'source_text': latex}
    target['id'] = target_id(target)
    bundle['targets'] = [target]
    context = next(c for c in bundle['context'] if c['block_id'] == target['block_id'])
    assert math_content_kind(latex) == 'formula'
    assert target_math_content_kind(bundle, target) == 'formula'
    context['type'] = 'authors'
    assert target_math_content_kind(bundle, target) == 'notation'
    entry = formula_entry(target, math_role='notation', symbols=[], meaning=[], process=[],
                          exclusion_reason='저자 이름에 붙은 공동 기여 표식이다.')
    with pytest.raises(ValueError, match='semantic review'):
        validate(bundle, [target], [entry], True, True)
    assert validate(bundle, [target], [{**entry, 'reviewed': True}], True, True)
    context.update(type='footnote', text='Work completed as an intern at Microsoft. Equal contribution.')
    assert target_math_content_kind(bundle, target) == 'notation'
    context.update(type='footnote', text='The dagger operation denotes the matrix pseudoinverse.')
    assert target_math_content_kind(bundle, target) == 'formula'
    context['type'] = 'paragraph'
    with pytest.raises(ValueError, match='Math role'):
        validate(bundle, [target], [{**entry, 'reviewed': True}], True, True)


def test_author_marker_disposition_never_hides_real_scripted_variables_or_relations():
    bundle = {'context': [{'block_id': 'author', 'type': 'authors', 'text': 'Author names'}]}
    for latex, role in [(r'x^\dagger', 'symbol'), (r'x^\dagger=y', 'formula'),
                        (r'{}^{\dagger *}=x', 'formula'), (r'{}^{x+y}', 'formula')]:
        assert target_math_content_kind(bundle, {'block_id': 'author', 'source_text': latex}) == role


def test_square_is_notation_only_when_standalone_and_closing_an_active_proof():
    target = {'block_id': 'end', 'source_text': r'\square'}
    bundle = {'context': [
        {'block_id': 'section', 'type': 'heading', 'text': 'A theorem'},
        {'block_id': 'proof', 'type': 'paragraph', 'text': 'Proof. Assume the stated conditions.'},
        {'block_id': 'part', 'type': 'heading', 'text': 'Part 4.'},
        {'block_id': 'calc', 'type': 'equation', 'text': 'x=y'},
        {'block_id': 'end', 'type': 'paragraph', 'text': r'\(\square\)'},
    ]}
    assert math_content_kind(r'\square') == 'formula'
    assert target_math_content_kind(bundle, target) == 'notation'
    assert target_math_content_kind(bundle, {**target, 'source_text': r'\square A=B'}) == 'formula'
    bundle['context'][2]['text'] = 'A new section'
    assert target_math_content_kind(bundle, target) == 'formula'
    bundle['context'][2].update(type='paragraph', text=r'\(\square\)')
    assert target_math_content_kind(bundle, target) == 'formula'
    bundle['context'][2]['text'] = 'An intermediate calculation'
    bundle['context'][1]['text'] = 'The square operator denotes necessity.'
    assert target_math_content_kind(bundle, target) == 'formula'


def test_unique_quote_resolver_never_guesses_ambiguous_missing_or_foreign_occurrence():
    request = {'stage': 'repair', 'packet': {'text_units': [
        {'block_id': 'p', 'field': 'runs', 'run_index': 0, 'text': 'MLP and MLP'}]}}
    ambiguous = {'items': [{'key': 'x', 'target': {'kind': 'term', 'block_id': 'p',
                  'field': 'runs', 'run_index': 0, 'source_text': 'MLP', 'start': 1, 'end': 4}}]}
    for quote, block in [('MLP', 'p'), ('unknown', 'p'), ('MLP', 'other')]:
        raw = copy.deepcopy(ambiguous)
        raw['items'][0]['target'].update(source_text=quote, block_id=block)
        normalized, corrections = normalize_unique_term_offsets(request, raw)
        assert normalized == raw and corrections == []
    request['packet']['text_units'][0]['text'] = 'aaaa'
    ambiguous['items'][0]['target']['source_text'] = 'aaa'
    assert normalize_unique_term_offsets(request, ambiguous)[1] == []
    # Overlapping occurrences count separately; a greedy str.count would miss one.
    request['stage'] = 'review'
    assert normalize_unique_term_offsets(request, ambiguous) == (ambiguous, [])


def test_source_math_role_disposition_is_reviewed_and_not_rendered_as_an_explanation():
    bundle = catalog()
    target = bundle['targets'][0]
    with pytest.raises(ValueError, match='Math role'):
        validate(bundle, [target], [formula_entry(target, math_role='symbol', process=[])])
    with pytest.raises(ValueError, match='Math role'):
        validate(bundle, [target], [formula_entry(target, math_role='notation', symbols=[],
                 meaning=[], process=[], exclusion_reason='부호만 있다.')])
    notation = {**target, 'source_text': r'\times'}
    notation['id'] = target_id(notation)
    bundle['targets'] = [notation]
    entry = formula_entry(notation, math_role='notation', symbols=[], meaning=[], process=[],
                          exclusion_reason='문장 안에서 배수를 나타내는 곱셈 기호다.', reviewed=True)
    assert validate(bundle, [notation], [entry], True, True)
    bundle['explanations'] = [entry]
    display = {'paper_id': 'paper', 'view_id': 'view', 'blocks': []}
    projected = project_explanations(bundle, display)
    assert projected['targets'] == []
    assert projected['projection']['notation_disposition_count'] == 1


def test_conflicting_translation_aliases_never_select_explanation_by_order():
    bundle = catalog()
    first = validate_targets(bundle, [term(bundle)])[0]
    second = validate_targets(bundle, [term(bundle, 'attention', 7, 16)])[0]
    first['aliases'] = second['aliases'] = ['신경망']
    bundle['targets'] = [first, second]
    display = {'paper_id': 'paper', 'view_id': 'view', 'translation': {},
               'blocks': [{'id': 'p', 'content': {'runs': [
                   {'kind': 'text', 'text': '신경망을 사용한다.'}]}}]}
    display['translation'] = {'status': 'published'}
    projected = project_explanations(bundle, display)
    assert all(not t['localized'] for t in projected['targets'])
    assert set(projected['projection']['alias_collision_target_ids']) == {first['id'], second['id']}


def test_reviewed_concept_repeated_in_translated_unit_has_one_owner_and_all_occurrences():
    bundle = catalog()
    bundle['text_units'][0]['text'] = 'MLP and MLP'
    first = term(bundle, 'MLP', 0, 3)
    second = term(bundle, 'MLP', 8, 11)
    targets = validate_targets(bundle, [{**t, 'concept_key': 'mlp', 'aliases': ['다층 퍼셉트론']}
                                       for t in (first, second)])
    bundle['targets'] = targets
    display = {'paper_id': 'paper', 'view_id': 'view', 'translation': {'status': 'published'},
               'blocks': [{'id': 'p', 'content': {'runs': [
                   {'kind': 'text', 'text': 'MLP와 다층 퍼셉트론을 설명한다.'}]}}]}
    projected = project_explanations(bundle, display)
    assert len(projected['targets'][0]['localized']) == 2
    assert projected['targets'][1]['localized'] == []
    assert projected['targets'][1]['shared_with_target_id'] == targets[0]['id']
    assert projected['projection']['unresolved_target_ids'] == []
    assert projected['projection']['visible_occurrence_count'] == 2
    assert projected['projection']['shared_target_count'] == 1
    assert projected['projection']['alias_collision_target_ids'] == []
    # A second meaning of the same word must not inherit this glossary entry.
    bundle['targets'][1]['concept_key'] = 'different-meaning'
    conflict = project_explanations(bundle, display)
    assert set(conflict['projection']['unresolved_target_ids']) == {t['id'] for t in targets}


@pytest.mark.parametrize('long_alias,short_alias', [
    ('신경망 아키텍처 탐색', '신경망'), ('neural architecture search', 'neural')])
def test_reviewed_complete_alias_wins_only_its_contained_occurrence(long_alias, short_alias):
    bundle = catalog()
    first = validate_targets(bundle, [term(bundle)])[0]
    second = validate_targets(bundle, [term(bundle, 'attention', 7, 16)])[0]
    first.update(aliases=[long_alias], concept_key='search')
    second.update(aliases=[short_alias], concept_key='network')
    bundle.update(targets=[first, second], explanations=[
        {'target_id': t['id'], 'kind': 'term', 'reviewed': True} for t in (first, second)])
    display = {'paper_id': 'paper', 'view_id': 'view', 'translation': {'status': 'published'},
               'blocks': [{'id': 'p', 'content': {'runs': [
                   {'kind': 'text', 'text': long_alias + ' / ' + short_alias}]}}]}
    projected = project_explanations(bundle, display)
    assert [a['text'] for a in projected['targets'][0]['localized']] == [long_alias]
    assert [a['start'] for a in projected['targets'][1]['localized']] == [len(long_alias) + 3]
    assert projected['projection']['unresolved_target_ids'] == []
    assert projected['projection']['alias_collision_target_ids'] == []
    assert 'shared_with_target_id' not in projected['targets'][1]
    second['concept_key'] = first['concept_key']
    shared = project_explanations(bundle, display)
    assert shared['projection']['visible_occurrence_count'] == 2
    assert shared['projection']['shared_target_count'] == 1
    assert shared['projection']['unresolved_target_ids'] == []
    second['concept_key'] = 'network'
    # A merged translated run does not erase distinct original source units.
    second['block_id'] = 'q'
    display['blocks'].append({'id': 'q', 'content': {'runs': []}})
    display['blocks'][0]['content']['runs'][0]['source_unit_ids'] = ['p/runs', 'q/runs']
    cross_unit = project_explanations(bundle, display)
    assert set(cross_unit['projection']['alias_collision_target_ids']) == {first['id'], second['id']}
    second['block_id'] = 'p'
    # Without reviewed aliases, or with partial overlaps, ambiguity stays closed.
    bundle['explanations'][0]['reviewed'] = False
    assert project_explanations(bundle, display)['projection']['alias_collision_target_ids']
    bundle['explanations'][0]['reviewed'] = True
    first['aliases'], second['aliases'] = ['alpha beta'], ['beta gamma']
    display['blocks'][0]['content']['runs'][0]['text'] = 'alpha beta gamma'
    conflict = project_explanations(bundle, display)
    assert all(not t['localized'] for t in conflict['targets'])
    assert set(conflict['projection']['alias_collision_target_ids']) == {first['id'], second['id']}


def test_table_measurements_have_no_marker_but_body_quantities_are_retained():
    bundle = catalog()
    table = next(t for t in bundle['targets'] if t['field'] == 'cells/0')
    body = bundle['targets'][0]
    bundle['targets'] = [table, body]
    bundle['explanations'] = [{**formula_entry(t), 'math_role': 'literal'} for t in (table, body)]
    display = {'paper_id': 'paper', 'view_id': 'view', 'blocks': [
        {'id': 'p', 'content': {'runs': [{'kind': 'text', 'text': '😀 MLP와 attention'},
                                       {'kind': 'math', 'latex': body['source_text']}]}}]}
    projected = project_explanations(bundle, display)
    assert [t['id'] for t in projected['targets']] == [body['id']]
    assert projected['projection']['table_literal_count'] == 1
    assert bundle['targets'] == [table, body]


def test_independent_store_revision_publication_and_source_invalidation(library, pdf_path):
    paper = library.register(pdf_path)
    job = library.begin(paper['id'], paper['sha256'])['job_id']
    library.append(job, text_blocks(library, paper), [], [1])
    assert library.validate(job, finalize=True)['valid']
    service = ExplanationService(library)
    source_bytes = {p: p.read_bytes() for folder in ('sources', 'views', 'originals')
                    for p in (library.root / folder).iterdir() if p.is_file()}
    bundle = service.prepare(paper['id'])
    unit = next(u for u in bundle['text_units'] if 'units' in u['text'])
    start = unit['text'].index('units')
    target = {**{k: unit[k] for k in ('block_id', 'field', 'run_index')},
              'kind': 'term', 'source_text': 'units', 'start': start, 'end': start + 5}
    targets = validate_targets(bundle, [target])
    entry = {'target_id': targets[0]['id'], 'kind': 'term', 'category': '측정 단위',
             'description': [{'kind': 'text', 'text': '양을 셀 때 쓰는 기준이다.'}],
             'reviewed': True}
    saved = service.save(paper['id'], [entry], targets)
    assert service.get(paper['id'])['status'] == 'empty'
    with pytest.raises(ValueError, match='revision'):
        service.save(paper['id'], [entry], expected_revision=0)
    published = service.publish(paper['id'], saved['revision'])
    assert published['status'] == 'published'
    localized = {**targets[0], 'aliases': ['단위']}
    changed_alias = service.register_targets(paper['id'], [localized])
    assert changed_alias['explanations'][0]['reviewed'] is False
    with pytest.raises(ValueError, match='semantic review'):
        service.publish(paper['id'])
    assert service.get(paper['id'])['revision'] == published['revision']
    service.save(paper['id'], [{**entry, 'reviewed': False}])
    assert service.get(paper['id'])['revision'] == published['revision']
    replacement = service.save(paper['id'], [], targets=[], replace=True)
    assert replacement['targets'] == [] and replacement['explanations'] == []
    assert service.get(paper['id'])['revision'] == published['revision']
    assert all(path.read_bytes() == data for path, data in source_bytes.items())
    # register() return predates build; read current paper instead.
    path = library.root / 'views' / f'{library.paper(paper["id"])["current_view_id"]}.json'
    changed = json.loads(path.read_text('utf-8'))
    changed['visual_review_pages'] = []
    path.write_text(json.dumps(changed), 'utf-8')
    assert service.get(paper['id'])['status'] == 'empty'


def test_translation_projection_uses_provenance_and_exact_alias_only():
    bundle = catalog()
    math = bundle['targets'][0]
    target = validate_targets(bundle, [term(bundle, 'attention', 7, 16)])[0]
    target['aliases'] = ['어텐션']
    bundle['targets'] = [math, target]
    display = {'paper_id': 'paper', 'view_id': 'view', 'translation': {'status': 'published'},
               'blocks': [{'id': 'p', 'content': {'runs': [
                   {'kind': 'text', 'text': '여기에는 어텐션을 사용한다.'},
                   {'kind': 'math', 'latex': '다르게 렌더링', 'source_math_id': math['source_math_id']},
               ]}}]}
    mapped = project_explanations(bundle, display)
    assert mapped['targets'][0]['localized'][0]['run_index'] == 1
    assert mapped['targets'][1]['localized'][0]['start'] == 5
    assert 'localized' not in target
    display['blocks'][0]['content']['runs'][0]['text'] = '어텐션 어텐션'
    assert not project_explanations(bundle, display)['targets'][1]['localized']
    display['view_id'] = 'different'
    assert all(not t['localized'] for t in project_explanations(bundle, display)['targets'])


def test_merged_translation_keeps_source_occurrence_provenance():
    bundle = catalog()
    math = bundle['targets'][0]
    target = validate_targets(bundle, [term(bundle, 'attention', 7, 16)])[0]
    target['aliases'] = ['어텐션']
    bundle['targets'] = [math, target]
    display = {'paper_id': 'paper', 'view_id': 'view', 'translation': {'status': 'published'},
               'blocks': [{'id': 'p', 'content': {'runs': []}},
                          {'id': 'host', 'content': {'runs': [
                              {'kind': 'text', 'text': '어텐션을 계산한다.',
                               'source_unit_ids': ['host/runs', 'p/runs']},
                              {'kind': 'math', 'latex': 'x+y',
                               'source_math_id': math['source_math_id']}]}}]}
    projected = project_explanations(bundle, display)
    assert all(t['localized'][0]['block_id'] == 'host' for t in projected['targets'])


def test_reader_binds_exact_snapshot_and_rejects_mid_render_change(library, pdf_path, monkeypatch):
    from pairper.document import presentation

    paper = library.register(pdf_path)
    job = library.begin(paper['id'], paper['sha256'])['job_id']
    library.append(job, text_blocks(library, paper), [], [1])
    assert library.validate(job, finalize=True)['valid']
    service = ExplanationService(library)
    display = presentation.display_document(library, paper['id'])
    assert display['source_identity'] == service.prepare(paper['id'])['source_identity']
    stale = copy.deepcopy(display)
    stale['source_identity']['view_sha256'] = 'stale-same-view'
    with pytest.raises(ValueError, match='Display source changed'):
        service.project(paper['id'], stale)

    original = presentation.resolved_runs
    changed = False

    def mutate_during_render(*args):
        nonlocal changed
        if not changed:
            view_id = library.paper(paper['id'])['current_view_id']
            path = library.root / 'views' / f'{view_id}.json'
            path.write_text(path.read_text('utf-8') + '\n', 'utf-8')
            changed = True
        return original(*args)

    monkeypatch.setattr(presentation, 'resolved_runs', mutate_during_render)
    with pytest.raises(ValueError, match='Source changed while rendering'):
        presentation.display_document(library, paper['id'])
