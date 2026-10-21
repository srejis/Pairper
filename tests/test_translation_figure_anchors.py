"""Owner envelopes are not phrase anchors; persisted drafts need fresh re-review.

Original English figure fixtures only. Replies are newly authored protocol fakes,
not imports of previous Korean, AI outputs or evaluation decisions.
"""
import hashlib
import json
from pathlib import Path

import pytest

from test_translation_workflow import setup_workflow, draft, review, submit, engine, sha

FIXTURES = Path(__file__).parent / 'fixtures/workflow'


def use_original_figure(service, isolated=False):
    name = 'isolated-lm-head-original' if isolated else 'edge-fig1-original'
    original = json.loads((FIXTURES / (name+'.json')).read_text('utf-8'))
    figure = original['figure']
    data = (FIXTURES / figure['crop']).read_bytes()
    assert hashlib.sha256(data).hexdigest() == figure['crop_sha256']
    destination = service.directory / figure['crop']
    destination.write_bytes(data)
    figure['crop'] = str(destination.resolve())
    bundle = service.bundle
    bundle['document']['blocks'] = [block for block in bundle['document']['blocks'] if block['type'] != 'figure'] + [original['block']]
    bundle['figures'] = [figure]
    bundle['figure_source_spans'] = {figure['block_id']:original['source_spans']}
    bundle['fingerprint'] = sha({k:v for k,v in bundle.items() if k != 'fingerprint'})


def figure_claim(workflow, run_id):
    for _ in range(3):
        claim = workflow.claim(run_id)
        if claim['request']['kind'] == 'figure': return claim
        submit(workflow,claim,draft(claim))
    raise AssertionError('Original figure not planned')


def answer(claim, whole=True, isolated=False):
    task = claim['request']['task']
    bbox = task['figure']['region']['bbox'] if whole else [168,81,226,98]
    labels = [{'source':'LM head','target':'언어 모델 헤드','source_refs':[], 'bbox':bbox}]
    if not isolated:
        labels.extend({'source':'LM head','target':'언어 모델 헤드','source_refs':[], 'bbox':box}
                      for box in ([301,81,357,98],[424,81,480,98]))
        for source, target in [('(a) Regular Transformer','(a) 일반적인 Transformer'),
                               ('Learnable binary masks','학습 가능한 이진 마스크'),
                               ('(b) Optimize edge masks','(b) 엣지 마스크 최적화'),
                               ('Upstream nodes','상위 노드'),
                               ('(c) Obtain sparse circuit','(c) 희소 회로 도출')]:
            texts = ['Learnable ', 'binary masks'] if source == 'Learnable binary masks' else [source]
            refs = [next(span['id'] for span in task['figure_source_spans'] if span['text'] == text) for text in texts]
            labels.append({'source':source,'target':target,'source_refs':refs,'bbox':[]})
    return {'figures':[{'block_id':task['figure']['block_id'],'image_inspected':True,
                       'labels_complete':True,'labels':labels,'unresolved_errors':[],
                       'no_translatable_text_reason':''}], 'issues':[],'notes':[], 'source_concerns':[]}


@pytest.mark.parametrize('nearly', [False, True])
def test_multi_phrase_whole_image_bbox_is_rejected_before_candidate_storage(setup_workflow, nearly):
    workflow, service = setup_workflow(full=True,images=True,max_failures=1)
    use_original_figure(service)
    run = workflow.start(service.bundle['paper_id'],'whole-owner-bbox-'+str(nearly))
    claim = figure_claim(workflow,run['run_id']); proposed = answer(claim)
    if nearly:
        l,t,r,b = proposed['figures'][0]['labels'][0]['bbox']
        proposed['figures'][0]['labels'][0]['bbox'] = [l+.001*(r-l),t+.001*(b-t),r-.001*(r-l),b-.001*(b-t)]
    rejected = submit(workflow,claim,proposed)
    assert rejected['counts']['blocked'] == 1
    assert any('whole region' in item['error'] for item in rejected['errors'])
    with workflow.connect() as con:
        assert con.execute('SELECT candidate FROM tasks WHERE id=?',(claim['task_id'],)).fetchone()[0] is None
    assert service.store.read(run['session_id'],service.bundle['fingerprint'])['revision'] == 0


def test_genuine_isolated_one_label_image_can_use_its_region_but_still_requires_review(setup_workflow):
    workflow, service = setup_workflow(full=True,images=True)
    use_original_figure(service,isolated=True)
    run = workflow.start(service.bundle['paper_id'],'isolated-one-label')
    claim = figure_claim(workflow,run['run_id'])
    result = submit(workflow,claim,answer(claim,isolated=True))
    assert result['counts']['blocked'] == 0
    assert result['counts']['drafted'] == 3 and result['counts']['accepted'] == 0
    with pytest.raises(ValueError): workflow.commit(run['run_id'])
    # The test does not infer visual/semantic approval from area or one-label count.
    assert service.store.read(run['session_id'],service.bundle['fingerprint'])['revision'] == 0


def test_legacy_broad_anchor_is_requeued_with_exact_feedback_and_requires_new_review(setup_workflow, monkeypatch):
    workflow, service = setup_workflow(full=True,images=True)
    use_original_figure(service)
    run = workflow.start(service.bundle['paper_id'],'legacy-broad-anchor')
    claim = figure_claim(workflow,run['run_id']); old = answer(claim)
    block_id = old['figures'][0]['block_id']
    previous_review = {'items':[{'item_id':block_id,'verdict':'pass','source_compared':True,
                                'source_quote':'Original attached figure','target_quote':block_id,
                                'checks':['Legacy source/image review record.'],'issues':[]}],
                       'issues':[],'notes':[],'source_concerns':[]}
    previous_review['items'].extend({'item_id':block_id+'@label'+str(i),'verdict':'pass',
        'source_compared':True,'source_quote':label['source'],'target_quote':label['target'],
        'checks':['Legacy source-label review record.'],'issues':[]}
        for i,label in enumerate(old['figures'][0]['labels']))
    # Persist the previous-version shape without using the new normalization gate.
    # This is a migration/restart fixture, not a granted product approval.
    with workflow.connect() as con:
        con.execute("UPDATE tasks SET candidate=?,review=?,review_input_hash='legacy-hash',status='accepted',"
                    "phase='review',lease=NULL,lease_until=NULL,failures=2 WHERE id=?",
                    (json.dumps(old,ensure_ascii=False),json.dumps(previous_review,ensure_ascii=False),claim['task_id']))
    restarted = engine.Workflow(service)
    monkeypatch.setattr(restarted,'_with_images',workflow._with_images)
    new = restarted.claim(run['run_id'])
    assert new['task_id'] == claim['task_id'] and new['phase'] == 'generate'
    assert new['request']['candidate'] is None
    assert new['request']['validation_feedback']['rejected_candidate'] == old
    assert 'whole region' in new['request']['validation_feedback']['error']
    with restarted.connect() as con:
        row = con.execute('SELECT * FROM tasks WHERE id=?',(claim['task_id'],)).fetchone()
        event = json.loads(con.execute("SELECT payload FROM events WHERE event='candidate_invalidated_by_validation'").fetchone()[0])
    assert row['candidate'] is None and row['review'] is None and row['failures'] == 0
    assert json.loads(row['last_response']) == old
    assert event['previous_candidate'] == old and event['previous_review'] == previous_review
    assert event['fresh_generation_and_review_required'] is True
    with pytest.raises(ValueError): restarted.commit(run['run_id'])
    fixed = answer(new,whole=False)
    submit(restarted,new,fixed)
    with pytest.raises(ValueError): restarted.commit(run['run_id'])
    reviewed_figure = False
    for _ in range(8):
        pending = restarted.claim(run['run_id'])
        if pending is None: break
        assert pending['phase'] == 'review'
        assert 'validation_feedback' not in pending['request']
        if pending['task_id'] == claim['task_id']:
            reviewed_figure = True
            assert pending['request']['candidate'] == fixed
            assert pending['request']['issues'] == []
        submit(restarted,pending,review(pending))
    assert reviewed_figure
    assert restarted.commit(run['run_id'])['status'] == 'ready'
