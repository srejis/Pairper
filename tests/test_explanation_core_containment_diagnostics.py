"""A display exclusion needs exact surviving-owner evidence, never an ID claim."""
from copy import deepcopy

from pairper.explanation.projection import project_explanations


def setup(text='헤시안 역행렬'):
    def term(tid, alias, concept):
        return {'id': tid, 'kind': 'term', 'block_id': 'p', 'field': 'runs',
            'run_index': 0, 'start': 0, 'end': len(tid), 'source_text': tid,
            'aliases': [alias], 'concept_key': concept}
    bundle = {'paper_id': 'paper', 'source_identity': {'view_id': 'view'},
        'targets': [term('hessian', '헤시안', 'hessian'),
                    term('inverse', '헤시안 역행렬', 'inverse-hessian')]}
    bundle['explanations'] = [{'target_id': t['id'], 'kind': 'term', 'reviewed': True}
                              for t in bundle['targets']]
    display = {'paper_id': 'paper', 'view_id': 'view', 'translation': {'session_id': 'ko'},
        'blocks': [{'id': 'p', 'content': {'runs': [
            {'kind': 'text', 'text': text, 'source_unit_ids': ['p/runs']}]}}]}
    return bundle, display


def test_fully_contained_alias_is_distinguished_without_changing_raw_unresolved():
    bundle, display = setup()
    original = deepcopy(bundle)
    result = project_explanations(bundle, display)
    report = result['projection']
    assert report['unresolved_target_ids'] == ['hessian']
    assert report['contained_alias_shadowed_only_target_ids'] == ['hessian']
    assert report['unmapped_target_ids'] == []
    row = report['contained_alias_shadowed_occurrences'][0]
    assert row['target_id'] == 'hessian' and row['owner_target_id'] == 'inverse'
    assert row['anchor']['text'] == '헤시안'
    assert row['owner_anchor'] in result['targets'][1]['localized']
    assert bundle == original


def test_another_standalone_occurrence_remains_visible():
    bundle, display = setup('헤시안 역행렬과 헤시안')
    result = project_explanations(bundle, display)
    assert result['targets'][0]['localized'][0]['start'] > 0
    assert result['projection']['contained_alias_shadowed_occurrences']
    assert result['projection']['contained_alias_shadowed_only_target_ids'] == []
    assert result['projection']['unresolved_target_ids'] == []


def test_rejected_equal_owner_cannot_hide_any_unmapped_target():
    bundle, display = setup()
    other = {**bundle['targets'][1], 'id': 'different', 'concept_key': 'another-meaning'}
    bundle['targets'].append(other)
    bundle['explanations'].append({'kind': 'term', 'target_id': 'different', 'reviewed': True})
    report = project_explanations(bundle, display)['projection']
    assert report['contained_alias_shadowed_occurrences'] == []
    assert report['contained_alias_shadowed_only_target_ids'] == []
    assert set(report['unmapped_target_ids']) == {'hessian', 'inverse', 'different'}


def test_partial_overlap_is_ambiguous_not_intentional():
    bundle, display = setup('alpha beta gamma')
    bundle['targets'][0]['aliases'] = ['alpha beta']
    bundle['targets'][1]['aliases'] = ['beta gamma']
    report = project_explanations(bundle, display)['projection']
    assert report['contained_alias_shadowed_occurrences'] == []
    assert set(report['unmapped_target_ids']) == {'hessian', 'inverse'}


def test_no_matching_literal_is_not_an_intentional_exclusion():
    bundle, display = setup('관련 없는 문장')
    report = project_explanations(bundle, display)['projection']
    assert report['contained_alias_shadowed_only_target_ids'] == []
    assert set(report['unmapped_target_ids']) == {'hessian', 'inverse'}


def test_owner_without_review_or_same_source_unit_remains_ambiguous():
    bundle, display = setup()
    bundle['explanations'][1]['reviewed'] = False
    report = project_explanations(bundle, display)['projection']
    assert report['contained_alias_shadowed_only_target_ids'] == []
    assert set(report['unmapped_target_ids']) == {'hessian', 'inverse'}
    bundle['explanations'][1]['reviewed'] = True
    bundle['targets'][1]['block_id'] = 'q'
    display['blocks'].append({'id': 'q', 'content': {'runs': []}})
    display['blocks'][0]['content']['runs'][0]['source_unit_ids'].append('q/runs')
    report = project_explanations(bundle, display)['projection']
    assert report['contained_alias_shadowed_only_target_ids'] == []
    assert set(report['unmapped_target_ids']) == {'hessian', 'inverse'}
