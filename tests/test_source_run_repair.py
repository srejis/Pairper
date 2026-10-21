"""Source-only synthetic regrouping regressions; no AI or library writes."""
import copy
import json

import pytest

from pairper.document.models import Block, Document, MathRun, Region, TextRun
from pairper.translation.source_run_repair import (
    apply_run_groups, canonical_run_json, normalize_run_group, resolve_run_group,
)


@pytest.fixture
def original():
    spans = [
        {'id':'intro','text':'Let ', 'bbox':[0,10,15,20]},
        {'id':'d0','text':'D', 'bbox':[20,10,25,20]},
        {'id':'d1','text':'D', 'bbox':[35,10,40,20]},
        {'id':'small','text':'small', 'bbox':[35,20,45,23]},
        {'id':'tail','text':' for all.', 'bbox':[50,10,90,20]},
    ]
    runs = [TextRun(refs=['intro']),
            MathRun(latex=r'D\times D', refs=['d0','d1'], source_verified=True,
                    region=Region(page=1,bbox=[20,10,40,20])),
            TextRun(refs=['small']), TextRun(refs=['tail'])]
    block = Block(id='p7-example',type='paragraph',order=0,
                  source_refs=[span['id'] for span in spans],content={'runs':runs})
    document = Document(paper_id='source-only',source_sha256='original-pdf',blocks=[block]).model_dump(mode='json')
    unit = {'id':'p7-example/runs','block_id':'p7-example','field':'runs','pages':[1],
            'source_refs':[span['id'] for span in spans]}
    return {'document':document,'units':[unit]}, {'pages':[{'page':1,'spans':spans}]}


def finding(bundle, start=1, end=3):
    selected = bundle['document']['blocks'][0]['content']['runs'][start:end]
    proposed = [{'kind':'math','latex':r'D\times D_{small}',
                 'refs':['d0','d1','small'], 'before':'auto',
                 'region':{'page':999,'bbox':[1,1,9999,9999],'rotation':90}}]
    return {'kind':'run_group','source_id':f'p7-example/runs/range/{start}:{end}',
            'current':canonical_run_json(selected),'proposed':canonical_run_json(proposed)}


def test_regroup_math_and_text_preserves_refs_and_uses_program_region(original):
    bundle, source = original
    before = copy.deepcopy(bundle)
    normalized = normalize_run_group(bundle, finding(bundle), source)
    proposed = json.loads(normalized['proposed'])
    assert proposed[0]['region'] == {'page':1,'bbox':[20.0,10.0,45.0,23.0],'rotation':0}
    assert proposed[0]['refs'] == ['d0','d1','small']
    assert proposed[0]['source_verified'] is True
    document, audit = apply_run_groups(bundle, [finding(bundle)], source)
    runs = document['blocks'][0]['content']['runs']
    assert len(runs) == 3 and runs[1]['latex'] == r'D\times D_{small}'
    assert runs[0] == before['document']['blocks'][0]['content']['runs'][0]
    assert runs[-1] == before['document']['blocks'][0]['content']['runs'][-1]
    assert audit[0]['requires_fresh_PDF_review'] and audit[0]['requires_full_source_validation']
    restored = copy.deepcopy(document)
    restored['blocks'][0]['content']['runs'] = before['document']['blocks'][0]['content']['runs']
    assert restored == before['document'] and bundle == before


@pytest.mark.parametrize('source_id', ['p7-example/runs/range/0:99', 'p7-example/runs/range/2:2',
                                     'p7-example/runs/range/-1:3', 'p7-example/runs/range/01:3',
                                     '../../foreign/range/0:1', 'p7-example/runs/range/true:3'])
def test_selectors_are_exact_current_unit_and_half_open_range(original, source_id):
    with pytest.raises(ValueError): resolve_run_group(original[0], source_id)


@pytest.mark.parametrize('attack', ['foreign_ref','missing_ref','duplicate_ref','reorder_ref',
                                   'empty_proposal','unknown_field','empty_text_override','changed_current'])
def test_provenance_and_full_current_identity_cannot_be_changed(original, attack):
    bundle, source = original
    proposal = finding(bundle)
    runs = json.loads(proposal['proposed'])
    if attack == 'foreign_ref': runs[0]['refs'].append('tail')
    if attack == 'missing_ref': runs[0]['refs'].pop()
    if attack == 'duplicate_ref': runs[0]['refs'].append('d0')
    if attack == 'reorder_ref': runs[0]['refs'] = ['small','d0','d1']
    if attack == 'empty_proposal': runs = []
    if attack == 'unknown_field': runs[0]['source_replacement'] = 'foreign document'
    if attack == 'empty_text_override':
        runs = [{'kind':'text','refs':['d0','d1','small'],'text_override':'','source_verified':True}]
    if attack == 'changed_current':
        current = json.loads(proposal['current']); current[0]['latex'] += '+1'
        proposal['current'] = canonical_run_json(current)
    proposal['proposed'] = canonical_run_json(runs)
    before = copy.deepcopy(bundle)
    with pytest.raises(ValueError): apply_run_groups(bundle,[proposal],source)
    assert bundle == before


def test_math_bbox_cannot_claim_intervening_unselected_source_span(original):
    bundle, source = original
    source['pages'][0]['spans'].append({'id':'outside','text':'other','bbox':[42,12,44,14]})
    with pytest.raises(ValueError, match='outside original span'):
        normalize_run_group(bundle,finding(bundle),source)


def test_cross_page_groups_and_unresolved_ranges_are_not_guessed(original):
    bundle, source = original
    small = source['pages'][0]['spans'].pop(3)
    source['pages'].append({'page':2,'spans':[small]})
    bundle['units'][0]['pages'] = [1,2]
    with pytest.raises(ValueError, match='Cross-page'):
        normalize_run_group(bundle,finding(bundle),source)
    run = bundle['document']['blocks'][0]['content']['runs'][2]
    run['refs'] = []; run['ref_ranges'] = [{'first_ref':'small','last_ref':'small'}]
    with pytest.raises(ValueError, match='Unmaterialized'):
        resolve_run_group(bundle,'p7-example/runs/range/1:3')


def test_multiple_disjoint_groups_use_original_indices_and_overlap_is_atomic(original):
    bundle, source = original
    main = finding(bundle)
    tail = bundle['document']['blocks'][0]['content']['runs'][3]
    tail_finding = {'kind':'run_group','source_id':'p7-example/runs/range/3:4',
                    'current':canonical_run_json([tail]),
                    'proposed':canonical_run_json([{**tail,'text_override':' for all.'}])}
    document, _ = apply_run_groups(bundle,[main,tail_finding],source)
    assert len(document['blocks'][0]['content']['runs']) == 3
    assert document['blocks'][0]['content']['runs'][-1]['text_override'] == ' for all.'
    before = copy.deepcopy(bundle)
    with pytest.raises(ValueError, match='Overlapping'):
        apply_run_groups(bundle,[main,main],source)
    assert bundle == before


def test_first_boundary_and_duplicate_json_fields_are_rejected(original):
    bundle, source = original
    proposal = finding(bundle)
    runs = json.loads(proposal['proposed']); runs[0]['before'] = 'newline'
    proposal['proposed'] = canonical_run_json(runs)
    with pytest.raises(ValueError, match='boundary'):
        normalize_run_group(bundle,proposal,source)
    proposal = finding(bundle)
    proposal['proposed'] = '[{"kind":"math","kind":"text"}]'
    with pytest.raises(ValueError, match='Duplicate JSON'):
        normalize_run_group(bundle,proposal,source)


@pytest.mark.parametrize('field,block_type', [('items/0','list'), ('cells/0','table'), ('code_lines/0','code')])
def test_nested_run_containers_keep_original_selector_and_container_metadata(original, field, block_type):
    bundle, source = original
    proposal = finding(bundle)
    block = bundle['document']['blocks'][0]
    runs = block['content']['runs']
    if block_type == 'list':
        content = {'items':[runs]}
    elif block_type == 'table':
        content = {'region':{'page':1,'bbox':[0,10,95,25]}, 'source_verified':True,
                   'cells':[{'row':0,'column':0,'header':True,'runs':runs}]}
    else:
        content = {'code_lines':[{'indent':2,'runs':runs}]}
    bundle['document']['blocks'][0] = Block.model_validate({**block,'type':block_type,'content':content}).model_dump(mode='json')
    bundle['units'][0]['field'] = field
    bundle['units'][0]['id'] = 'p7-example/'+field
    proposal['source_id'] = 'p7-example/'+field+'/range/1:3'
    before = copy.deepcopy(bundle)
    document, _ = apply_run_groups(bundle,[proposal],source)
    actual = document['blocks'][0]['content']
    if block_type == 'list':
        changed_runs = actual['items'][0]
    elif block_type == 'table':
        changed_runs = actual['cells'][0]['runs']
        assert actual['cells'][0]['header'] is True
    else:
        changed_runs = actual['code_lines'][0]['runs']
        assert actual['code_lines'][0]['indent'] == 2
    assert len(changed_runs) == 3 and changed_runs[1]['latex'] == r'D\times D_{small}'
    assert bundle == before
