"""Real PDF observations; synthetic tests never call an AI or translation DB."""
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest

MODULE = Path(__file__).resolve().parents[1]/'src/pairper/translation/source_glyph_evidence.py'
spec = importlib.util.spec_from_file_location('source_glyph_evidence_under_test', MODULE)
evidence_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence_module)
build = evidence_module.build_source_glyph_evidence


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def original(tmp_path):
    pdf = tmp_path/'original.pdf'
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((70, 80), 'Literal 27.64 sample', fontname='cour')
        document.save(pdf)
    with pymupdf.open(pdf) as document:
        spans = [dict(s, id=f's{i}') for i,s in enumerate(document[0].get_text('dict')['blocks'][0]['lines'][0]['spans'])]
    snapshot = {'sha256':digest(pdf), 'pages':[{'page':1,'spans':spans}]}
    document = {'paper_id':'fixture-paper','source_sha256':digest(pdf), 'blocks':[
        {'id':'b','content':{'region':{'page':1,'bbox':spans[0]['bbox']}}}]}
    snapshot_path, view_path = tmp_path/'source.json', tmp_path/'view.json'
    snapshot_path.write_text(json.dumps(snapshot), encoding='utf8')
    view_path.write_text(json.dumps(document), encoding='utf8')
    snapshot = json.loads(snapshot_path.read_text('utf8'))
    document = json.loads(view_path.read_text('utf8'))
    bundle = {'paper_id':'fixture-paper','source_pdf_sha256':digest(pdf),'view_id':'fixture-view',
              'view_sha256':digest(view_path),'source_snapshot_sha256':digest(snapshot_path),
              'document':document,'units':[{'id':'b/runs','block_id':'b','source_refs':[s['id'] for s in spans],
                                           'source_text_sha256':hashlib.sha256(b'Literal 27.64 sample').hexdigest()}]}
    return bundle, {'pdf_path':pdf,'source_snapshot':snapshot,'source_snapshot_path':snapshot_path,'view_path':view_path}


def test_real_pdf_trace_is_neutral_and_does_not_mutate_inputs(original):
    bundle, paths = original
    before = copy.deepcopy(bundle)
    files = {p:digest(p) for p in paths.values() if isinstance(p, Path)}
    result = build(bundle, **paths)
    assert result['status'] == 'available' and result['binding']['verified']
    assert any(g['unicode']=='2' and isinstance(g['glyph_id'], int) for g in result['units'][0]['glyphs'])
    assert result['fonts'] and 'ToUnicode' in result['fonts'][0]
    assert 'verdict' not in result and 'proposed' not in result
    assert bundle == before and files == {p:digest(p) for p in files}


@pytest.mark.parametrize('key', ['source_pdf_sha256','source_snapshot_sha256','view_sha256'])
def test_mismatched_identity_returns_unavailable_without_observations(original, key):
    bundle, paths = original
    bundle[key] = '0'*64
    result = build(bundle, **paths)
    assert result['status']=='unavailable' and not result['units'] and not result['fonts']
    assert any('identity mismatch' in str(x) for x in result['unavailable'])


def test_snapshot_object_and_view_document_are_hash_bound(original):
    bundle, paths = original
    paths['source_snapshot'] = {'sha256':'other'}
    assert build(bundle, **paths)['status']=='unavailable'
    del paths['source_snapshot']
    bundle['document']['paper_id'] = 'changed'
    assert build(bundle, **paths)['status']=='unavailable'


def test_missing_paths_fake_pdf_and_unknown_unit_are_explicit(original, tmp_path):
    bundle, paths = original
    assert build(bundle)['status']=='unavailable'
    unknown = build(bundle, ['unknown'], **paths)
    assert unknown['status']=='unavailable' and unknown['unavailable']
    paths['pdf_path'].write_bytes(b'not a PDF')
    bundle['source_pdf_sha256']=digest(paths['pdf_path'])
    assert build(bundle, **paths)['status']=='unavailable'


def test_budget_and_ref_region_selection(original):
    bundle, paths = original
    limited = build(bundle, **paths, max_chars=2)
    assert limited['truncated'] and len(limited['units'][0]['glyphs'])==2
    assert build(bundle, **paths, max_pages=0)['status']=='unavailable'
    capped = build(bundle, **paths, max_output_chars=1024)
    assert len(json.dumps(capped, ensure_ascii=False,separators=(',',':')).encode())<=1024
    assert capped['truncated']
    bundle['units'][0]['source_refs']=[]
    assert build(bundle, **paths)['units'][0]['glyphs']  # original block ROI fallback


def test_cmap_inverse_candidates_are_not_raw_operator_claims():
    raw='2 beginbfchar <60> <2018> <1E> <0060> endbfchar 1 beginbfrange <30> <39> <0030> endbfrange'
    values=evidence_module._cmap_candidates(raw, {'‘','`','4'})
    assert {(m['code_hex'],m['unicode']) for m in values}=={('60','‘'),('1E','`'),('34','4')}


def test_explicit_unit_priority_survives_bundle_order_and_cap(original):
    bundle, paths=original
    bundle['units'] = [dict(bundle['units'][0],id=f'unit{i}') for i in range(12)]
    result=build(bundle,['unit11','unknown','unit2'],**paths,max_units=1)
    assert [u['unit_id'] for u in result['units']]==['unit11']
    assert result['truncated'] and result['unavailable']==[{'unknown_unit_ids':['unknown']}]


def test_actual_current_source_pdf_font_mapping_integration():
    # Read only the original-source plan and isolated source Library; never
    # translation sessions, candidates, approval history or old Korean data.
    root=Path(__file__).resolve().parents[2]
    bundle_path=root/'source-plan/39746f5cfe1d4a0b84513b30e3664726-bundle.json'
    if not bundle_path.exists():
        pytest.skip('Local source-only integration fixture not installed')
    bundle=json.loads(bundle_path.read_text('utf8'))
    library=root/'source-library'
    paths={'pdf_path':library/'originals'/(bundle['source_pdf_sha256']+'.pdf'),
           'source_snapshot_path':library/'sources'/(bundle['paper_id']+'.json'),
           'view_path':library/'views'/(bundle['view_id']+'.json')}
    before={str(p):digest(p) for p in paths.values()}
    unit=next(u for u in bundle['units'] if '‘'*3 in u['source_text'])
    task_path=root/'qa/automl-code-fence-forensic/task-packet.json'
    task_ids=json.loads(task_path.read_text('utf8'))['source_unit_ids'] if task_path.exists() else [u['id'] for u in bundle['units'][:64]]
    prioritized=[unit['id']]+[uid for uid in task_ids if uid!=unit['id']]
    result=build(bundle,prioritized,**paths)
    assert result['status']=='available' and result['binding']['verified']
    quote=next(g for g in result['units'][0]['glyphs'] if g['unicode']=='‘')
    assert quote['font_match']=='unique'
    font=next(item for item in result['fonts'] if item['xref'] in quote['font_xrefs'])
    assert '/quoteleft' in font['Encoding']['dictionary']
    assert any(m['code_hex']=='60' and m['unicode']=='‘' for m in font['ToUnicode']['mappings'])
    assert result['truncated'] and len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode())<=24000
    assert before=={str(p):digest(p) for p in paths.values()}


def test_cmap_range_order_is_canonical_across_hash_seeds(tmp_path):
    script='''import importlib.util,json,sys
s=importlib.util.spec_from_file_location("glyph",sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
print(json.dumps(m._cmap_candidates("1 beginbfrange <20> <7E> <0020> endbfrange",set("Font-local 27.64 ABCxyz")),sort_keys=True))'''
    outputs=[]
    for seed in ('1','27','937'):
        env={**os.environ,'PYTHONHASHSEED':seed,'PYTHONDONTWRITEBYTECODE':'1'}
        outputs.append(subprocess.run([sys.executable,'-c',script,str(MODULE)],env=env,
                       capture_output=True,text=True,encoding='utf8',check=True,timeout=30).stdout)
    assert len(set(outputs))==1


def test_actual_pdf_evidence_is_identical_across_process_hash_seeds():
    root=Path(__file__).resolve().parents[2]
    path=root/'source-plan/39746f5cfe1d4a0b84513b30e3664726-bundle.json'
    if not path.exists():pytest.skip('Local source-only integration fixture not installed')
    script='''import importlib.util,json,sys,pathlib
s=importlib.util.spec_from_file_location("glyph",sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
r=pathlib.Path(sys.argv[2]);b=json.loads((r/"source-plan/39746f5cfe1d4a0b84513b30e3664726-bundle.json").read_text("utf8"));lib=r/"source-library"
u=next(u for u in b["units"] if chr(8216)*3 in u["source_text"])
e=m.build_source_glyph_evidence(b,[u["id"]],pdf_path=lib/"originals"/(b["source_pdf_sha256"]+".pdf"),source_snapshot_path=lib/"sources"/(b["paper_id"]+".json"),view_path=lib/"views"/(b["view_id"]+".json"))
assert e["status"]=="available"
print(json.dumps(e,sort_keys=True,ensure_ascii=True,separators=(",",":")))'''
    outputs=[]
    for seed in ('1','27','937'):
        env={**os.environ,'PYTHONHASHSEED':seed,'PYTHONDONTWRITEBYTECODE':'1'}
        outputs.append(subprocess.run([sys.executable,'-c',script,str(MODULE),str(root)],env=env,
                       capture_output=True,text=True,encoding='utf8',check=True,timeout=30).stdout)
    assert len(set(outputs))==1
