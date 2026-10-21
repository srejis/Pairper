"""Behavioral workflow regressions. Fake replies test orchestration, not AI quality.

Fixtures select original-source material only; no past Korean/evaluation artifacts
are read. All mutable stores and renderer output live in pytest temporary paths.
"""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

FIXTURES = Path(__file__).parent / 'fixtures/workflow'
from pairper.translation import workflow as engine
from pairper.translation.contract import Entry, sha
from pairper.translation.service import TranslationService
from pairper.translation.store import TranslationStore


class SourceService(TranslationService):
    """Real temporary TranslationStore, with a controlled current source guard."""
    def __init__(self, directory, bundle):
        self.directory = directory
        self.store = TranslationStore(directory)
        self.bundle = bundle
        self.library = SimpleNamespace()

    def current_bundle(self, session_id):
        saved = self.store.source_bundle(session_id)
        if saved['fingerprint'] != self.bundle['fingerprint']:
            raise ValueError('Source changed')
        return copy.deepcopy(self.bundle)


def original_subset(directory, full=False):
    bundle = json.loads((FIXTURES / 'original-source-mini.json').read_text('utf-8'))
    units = bundle['units'] if not full else bundle['units'][:1]
    owners = {u['block_id'] for u in units}
    if full:
        owners.update(['edge-p3-roi-eq2', 'edge-p1-roi-title-rule'])
    bundle['units'] = units
    bundle['document']['blocks'] = [b for b in bundle['document']['blocks'] if b['id'] in owners]
    if full:
        directory.mkdir(parents=True, exist_ok=True)
        for figure in bundle['figures']:
            original = FIXTURES / figure['crop']
            assert hashlib.sha256(original.read_bytes()).hexdigest() == figure['crop_sha256']
            copied = directory / original.name
            copied.write_bytes(original.read_bytes())
            figure['crop'] = str(copied.resolve())
    else:
        bundle['figures'] = []
        bundle['figure_source_spans'] = {}
        bundle['math_text_inventory'] = []
    bundle['fingerprint'] = sha({k:v for k,v in bundle.items() if k != 'fingerprint'})
    return bundle


@pytest.fixture
def setup_workflow(tmp_path, monkeypatch):
    def factory(full=False, images=False, **options):
        directory = tmp_path / ('full' if full else 'text')
        service = SourceService(directory, original_subset(directory, full))
        monkeypatch.setattr(engine, 'prepare_bundle', lambda *_: copy.deepcopy(service.bundle))
        workflow = engine.Workflow(service, **options)
        def attach(bundle, task, request):
            paths = list(task.get('images', [])) if images else []
            request['attached_images'] = [{'path':p, 'sha256':hashlib.sha256(Path(p).read_bytes()).hexdigest()} for p in paths]
            return paths
        monkeypatch.setattr(workflow, '_with_images', attach)
        return workflow, service
    return factory


def draft(claim, suffix=''):
    task = claim['request']['task']
    common = {'issues':[], 'notes':[], 'source_concerns':[]}
    if task['kind'] == 'text':
        return {**common, 'items':[{'unit_id':u['id'], 'action':'translate',
                 'target':'새 시험 번역 ' + str(i) + suffix, 'reason':'fresh test candidate'} for i,u in enumerate(task['units'])]}
    if task['kind'] == 'math':
        roles, overlays = [], []
        for m in task['math_items']:
            for n in m['text_nodes']:
                roles.append({'math_id':m['math_id'], 'source_latex_sha256':m['source_latex_sha256'],
                              'node_index':n['index'], 'action':'translate', 'reason':'The words introduce the optimization constraint.'})
            overlays.append({'math_id':m['math_id'], 'source_latex_sha256':m['source_latex_sha256'],
                             'localized_latex':m['source_latex'].replace('subject to ', '조건: '),
                             'source_prose_indices':[0], 'target_prose_indices':[0], 'role_reason':'Constraint connective only; operators unchanged.'})
        return {**common, 'math_roles':roles, 'math_overlays':overlays}
    return {**common, 'figures':[{'block_id':task['figure']['block_id'], 'image_inspected':True,
             'labels_complete':True, 'labels':[], 'unresolved_errors':[],
             'no_translatable_text_reason':'문자가 없는 제목 구분선이다.'}]}


def review(claim, verdict='pass'):
    return {'items':[{'item_id':i['item_id'], 'verdict':verdict, 'source_compared':True,
                     'source_quote':i['source'], 'target_quote':i['target'],
                     'checks':['Original/source candidate scope compared in this deterministic protocol test.'],
                     'issues':[] if verdict == 'pass' else ['Test reviewer requests a correction.']}
                    for i in claim['request']['review_items']], 'issues':[], 'notes':[], 'source_concerns':[]}


def submit(workflow, claim, response):
    return workflow.submit(claim['run_id'], claim['task_id'], claim['lease_id'], response)


def finish(workflow, run_id):
    for _ in range(50):
        claim = workflow.claim(run_id)
        if claim is None:
            return workflow.report(run_id)
        submit(workflow, claim, review(claim) if claim['phase'] == 'review' else draft(claim))
    raise AssertionError('Workflow failed to converge within bounded test steps')


def test_fresh_session_excludes_previous_korean_and_preserves_source(setup_workflow):
    workflow, service = setup_workflow()
    before = copy.deepcopy(service.bundle)
    old = service.store.begin(service.bundle, 'legacy')
    unit = service.bundle['units'][0]
    sentinel = '이전 한국어 평가 결과를 절대 재사용하지 않는다'
    service.store.save(old['session_id'], service.bundle['fingerprint'], 0,
        entries=[Entry(action='translate', source_unit_ids=[unit['id']], source_fingerprint=sha([unit['fingerprint']]), target=sentinel).model_dump()])
    run = workflow.start(service.bundle['paper_id'], 'fresh')
    assert service.store.read(run['session_id'], service.bundle['fingerprint'])['result']['entries'] == []
    claim = workflow.claim(run['run_id'])
    assert sentinel not in json.dumps(claim, ensure_ascii=False)
    assert claim['request']['prior_Korean_inputs'] == 0
    assert service.bundle == before


def test_existing_filled_session_is_not_a_fresh_workflow(setup_workflow):
    workflow, service = setup_workflow()
    old = service.store.begin(service.bundle, 'taken')
    unit = service.bundle['units'][0]
    service.store.save(old['session_id'], service.bundle['fingerprint'], 0,
        entries=[Entry(action='translate', source_unit_ids=[unit['id']], source_fingerprint=sha([unit['fingerprint']]), target='기존 번역').model_dump()])
    with pytest.raises(ValueError, match='fresh|existing|import'):
        workflow.start(service.bundle['paper_id'], 'taken')


@pytest.mark.parametrize('attack', ['partial', 'foreign', 'duplicate'])
def test_partial_or_injected_ids_never_enter_store(setup_workflow, attack):
    workflow, service = setup_workflow(max_failures=1)
    run = workflow.start(service.bundle['paper_id'], attack)
    claim = workflow.claim(run['run_id']); answer = draft(claim)
    if attack == 'partial': answer['items'].pop()
    if attack == 'foreign': answer['items'][0]['unit_id'] = '../../foreign-session'
    if attack == 'duplicate': answer['items'][1] = copy.deepcopy(answer['items'][0])
    submit(workflow, claim, answer)
    workflow.claim(run['run_id'])
    assert workflow.report(run['run_id'])['status'] == 'needs_review'
    assert service.store.read(run['session_id'], service.bundle['fingerprint'])['revision'] == 0
    with pytest.raises(ValueError): workflow.commit(run['run_id'])


def test_restart_expired_lease_and_replay_are_safe(setup_workflow, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(engine.time, 'time', lambda: now[0])
    workflow, service = setup_workflow(lease_seconds=10)
    run = workflow.start(service.bundle['paper_id'], 'resume')
    first = workflow.claim(run['run_id'])
    restarted = engine.Workflow(service, lease_seconds=10)
    monkeypatch.setattr(restarted, '_with_images', workflow._with_images)
    assert restarted.claim(run['run_id']) is None
    now[0] += 11
    second = restarted.claim(run['run_id'])
    assert second['task_id'] == first['task_id'] and second['lease_id'] != first['lease_id']
    with pytest.raises(ValueError): submit(restarted, first, draft(first))
    submit(restarted, second, draft(second))
    with pytest.raises(ValueError): submit(restarted, second, draft(second))
    assert restarted.claim(run['run_id'])['phase'] == 'review'
    assert service.store.read(run['session_id'], service.bundle['fingerprint'])['revision'] == 0


def test_source_drift_rejects_an_inflight_reply(setup_workflow):
    workflow, service = setup_workflow()
    run = workflow.start(service.bundle['paper_id'], 'source-drift')
    claim = workflow.claim(run['run_id'])
    service.bundle['fingerprint'] = 'changed-source'
    with pytest.raises(ValueError, match='Source|source'): submit(workflow, claim, draft(claim))


@pytest.mark.parametrize('attack', ['empty_checks', 'false_quote', 'false_target_quote', 'not_compared', 'missing_item'])
def test_machine_detectable_rubberstamp_is_bounded(setup_workflow, attack):
    workflow, service = setup_workflow(max_failures=1)
    run = workflow.start(service.bundle['paper_id'], attack)
    claim = workflow.claim(run['run_id']); submit(workflow, claim, draft(claim))
    claim = workflow.claim(run['run_id']); answer = review(claim)
    if attack == 'empty_checks': answer['items'][0]['checks'] = []
    if attack == 'false_quote': answer['items'][0]['source_quote'] = 'invented evidence absent from original'
    if attack == 'false_target_quote': answer['items'][0]['target_quote'] = 'an older target absent from this candidate'
    if attack == 'not_compared': answer['items'][0]['source_compared'] = False
    if attack == 'missing_item': answer['items'].pop()
    submit(workflow, claim, answer); workflow.claim(run['run_id'])
    assert workflow.report(run['run_id'])['status'] == 'needs_review'
    with pytest.raises(ValueError): workflow.commit(run['run_id'])


def test_repair_has_before_after_and_requires_another_review(setup_workflow):
    workflow, service = setup_workflow(max_repairs=1)
    run = workflow.start(service.bundle['paper_id'], 'repair')
    claim = workflow.claim(run['run_id']); before = draft(claim); submit(workflow, claim, before)
    claim = workflow.claim(run['run_id']); submit(workflow, claim, review(claim, 'repair'))
    claim = workflow.claim(run['run_id']); assert claim['phase'] == 'repair'
    after = draft(claim, ' 수정'); submit(workflow, claim, after)
    with pytest.raises(ValueError): workflow.commit(run['run_id'])
    claim = workflow.claim(run['run_id']); assert claim['phase'] == 'review'
    assert claim['request']['candidate'] == after
    submit(workflow, claim, review(claim, 'repair')); workflow.claim(run['run_id'])
    assert workflow.report(run['run_id'])['status'] == 'needs_review'
    with workflow.connect() as con:
        events = [json.loads(r['payload']) for r in con.execute("SELECT payload FROM events WHERE event='candidate_repaired'")]
    assert events[0]['before_candidate'] == before and events[0]['response'] == after


def test_crash_rolls_back_reply_and_keeps_recoverable_lease(setup_workflow, monkeypatch):
    workflow, service = setup_workflow()
    run = workflow.start(service.bundle['paper_id'], 'crash')
    claim = workflow.claim(run['run_id'])
    original = workflow._event
    def crash(*args, **kwargs):
        raise RuntimeError('simulated power loss before durable event')
    monkeypatch.setattr(workflow, '_event', crash)
    with pytest.raises(RuntimeError): submit(workflow, claim, draft(claim))
    monkeypatch.setattr(workflow, '_event', original)
    submit(workflow, claim, draft(claim))
    assert workflow.claim(run['run_id'])['phase'] == 'review'
    assert service.store.read(run['session_id'], service.bundle['fingerprint'])['revision'] == 0


def test_all_three_gates_commit_only_after_fresh_review(setup_workflow):
    workflow, service = setup_workflow(full=True, images=True)
    original = copy.deepcopy(service.bundle)
    crop = Path(service.bundle['figures'][0]['crop']); crop_before = crop.read_bytes()
    run = workflow.start(service.bundle['paper_id'], 'all-gates')
    with pytest.raises(ValueError): workflow.commit(run['run_id'])
    outcome = finish(workflow, run['run_id'])
    assert outcome['status'] == 'ready' and outcome['counts']['accepted'] == 3
    assert outcome['semantic_quality_proven'] is False
    assert service.store.read(run['session_id'], service.bundle['fingerprint'])['revision'] == 0
    workflow.commit(run['run_id'])
    saved = service.store.read(run['session_id'], service.bundle['fingerprint'])
    assert saved['report']['complete'] and saved['status'] == 'draft'
    assert len(saved['result']['math_roles']) == len(saved['result']['math_overlays']) == 1
    assert len(saved['result']['figure_reviews']) == 1
    assert service.bundle == original and crop.read_bytes() == crop_before


def test_external_session_target_drift_must_not_be_overwritten(setup_workflow):
    workflow, service = setup_workflow()
    run = workflow.start(service.bundle['paper_id'], 'target-drift')
    finish(workflow, run['run_id'])
    unit = service.bundle['units'][0]
    service.store.save(run['session_id'], service.bundle['fingerprint'], 0,
        entries=[Entry(action='translate', source_unit_ids=[unit['id']], source_fingerprint=sha([unit['fingerprint']]), target='외부 동시 수정').model_dump()])
    with pytest.raises(ValueError, match='changed|revision|owned|external|fresh'):
        workflow.commit(run['run_id'])
    assert service.store.read(run['session_id'], service.bundle['fingerprint'])['result']['entries'][0]['target'] == '외부 동시 수정'


def test_claiming_visual_inspection_without_image_delivery_cannot_complete(setup_workflow):
    workflow, service = setup_workflow(full=True, images=False, max_failures=1)
    run = workflow.start(service.bundle['paper_id'], 'no-image')
    for _ in range(3):
        claim = workflow.claim(run['run_id'])
        if claim['request']['kind'] == 'figure':
            assert claim['images'] == []
            submit(workflow, claim, draft(claim))
            break
        submit(workflow, claim, draft(claim))
    finish(workflow, run['run_id'])
    assert workflow.report(run['run_id'])['status'] == 'needs_review'
    with pytest.raises(ValueError): workflow.commit(run['run_id'])


def test_neighbor_repair_invalidates_prior_context_review(setup_workflow):
    workflow, service = setup_workflow()
    run = workflow.start(service.bundle['paper_id'], 'neighbor-repair', max_units=1)
    for _ in range(2):
        claim = workflow.claim(run['run_id']); submit(workflow, claim, draft(claim))
    first_review = workflow.claim(run['run_id'])
    submit(workflow, first_review, review(first_review))
    second_review = workflow.claim(run['run_id'])
    submit(workflow, second_review, review(second_review, 'repair'))
    repair = workflow.claim(run['run_id']); assert repair['phase'] == 'repair'
    submit(workflow, repair, draft(repair, ' 이웃 수정'))
    recheck = workflow.claim(run['run_id'])
    assert recheck['phase'] == 'review' and recheck['task_id'] == first_review['task_id']
    assert recheck['request']['context'] != first_review['request']['context']
    with pytest.raises(ValueError): workflow.commit(run['run_id'])


def test_inflight_review_rejects_neighbor_changed_after_delivery(setup_workflow):
    workflow, service = setup_workflow(max_failures=1)
    run = workflow.start(service.bundle['paper_id'], 'inflight-context', max_units=1)
    for _ in range(2):
        claim = workflow.claim(run['run_id']); submit(workflow, claim, draft(claim))
    delayed = workflow.claim(run['run_id'])
    neighbor = workflow.claim(run['run_id'])
    submit(workflow, neighbor, review(neighbor, 'repair'))
    repaired = workflow.claim(run['run_id']); submit(workflow, repaired, draft(repaired, ' 현재 문맥'))
    rejected = submit(workflow, delayed, review(delayed))
    assert rejected['counts']['blocked'] == 1
    assert any('changed' in e['error'] for e in rejected['errors'])
    with pytest.raises(ValueError): workflow.commit(run['run_id'])


@pytest.mark.parametrize('attack', ['missing_role', 'operator_change'])
def test_math_node_coverage_and_core_relation_are_protected(setup_workflow, attack):
    workflow, service = setup_workflow(full=True, images=True, max_failures=1)
    run = workflow.start(service.bundle['paper_id'], attack)
    claim = workflow.claim(run['run_id']); assert claim['request']['kind'] == 'math'
    answer = draft(claim)
    if attack == 'missing_role': answer['math_roles'] = []
    else:
        original = answer['math_overlays'][0]['localized_latex']
        assert '\\ge c' in original
        answer['math_overlays'][0]['localized_latex'] = original.replace('\\ge c', '\\le c')
    submit(workflow, claim, answer)
    finish(workflow, run['run_id'])
    assert workflow.report(run['run_id'])['status'] == 'needs_review'
    assert service.store.read(run['session_id'], service.bundle['fingerprint'])['revision'] == 0


def test_runner_step_end_to_end_and_publish_is_local_only(setup_workflow):
    workflow, service = setup_workflow(full=True, images=True)
    calls = []
    class DeterministicRunner:
        def run(self, request, schema, images, directory):
            calls.append((request['kind'], request['phase'], len(images)))
            claim = {'request':request}
            return review(claim) if request['phase'] == 'review' else draft(claim)
    workflow.runner = DeterministicRunner()
    run = workflow.start(service.bundle['paper_id'], 'runner-protocol')
    for _ in range(10):
        outcome = workflow.step(run['run_id'])
        if outcome['status'] == 'ready': break
    assert outcome['status'] == 'ready'
    assert {(k,p) for k,p,_ in calls} == {(k,p) for k in ('text','math','figure') for p in ('generate','review')}
    assert all(n > 0 for k,_,n in calls if k == 'figure')
    assert workflow.commit(run['run_id'], publish=True)['status'] == 'published'
    assert service.store.read(run['session_id'], service.bundle['fingerprint'])['status'] == 'published'
    assert workflow.claim(run['run_id']) is None
    assert workflow.commit(run['run_id'], publish=True)['semantic_quality_proven'] is False


def test_commit_crash_after_store_write_can_resume_without_losing_history(setup_workflow, monkeypatch):
    workflow, service = setup_workflow()
    run = workflow.start(service.bundle['paper_id'], 'commit-crash')
    finish(workflow, run['run_id'])
    event = workflow._event
    def crash(con, run_id, task_id, phase, *args, **kwargs):
        if phase == 'commit': raise RuntimeError('crash after store write before workflow commit')
        return event(con, run_id, task_id, phase, *args, **kwargs)
    monkeypatch.setattr(workflow, '_event', crash)
    with pytest.raises(RuntimeError): workflow.commit(run['run_id'])
    assert service.store.read(run['session_id'], service.bundle['fingerprint'])['revision'] == 1
    restarted = engine.Workflow(service)
    restarted.commit(run['run_id'])
    saved = service.store.read(run['session_id'], service.bundle['fingerprint'])
    assert saved['revision'] == 1 and saved['report']['complete']


def test_provider_failure_is_bounded_and_never_fakes_completion(setup_workflow):
    workflow, service = setup_workflow(max_failures=2)
    class BrokenRunner:
        def run(self, *_): raise RuntimeError('provider process crashed')
    workflow.runner = BrokenRunner()
    run = workflow.start(service.bundle['paper_id'], 'provider-failure')
    for _ in range(3): workflow.step(run['run_id'])
    assert workflow.report(run['run_id'])['status'] == 'needs_review'
    assert service.store.read(run['session_id'], service.bundle['fingerprint'])['revision'] == 0
    with pytest.raises(ValueError): workflow.commit(run['run_id'])


@pytest.mark.parametrize('change', ['replace', 'delete'])
def test_image_evidence_change_is_bounded_without_touching_original(setup_workflow, change):
    workflow, service = setup_workflow(full=True, images=True, max_failures=1)
    figure = service.bundle['figures'][0]
    copied = service.directory / 'test-only-original-copy.png'
    copied.write_bytes(Path(figure['crop']).read_bytes())
    figure['crop'] = str(copied)
    service.bundle['fingerprint'] = sha({k:v for k,v in service.bundle.items() if k != 'fingerprint'})
    run = workflow.start(service.bundle['paper_id'], 'image-'+change)
    for _ in range(3):
        claim = workflow.claim(run['run_id'])
        if claim['request']['kind'] == 'figure': break
        submit(workflow, claim, draft(claim))
    assert claim['request']['kind'] == 'figure'
    if change == 'replace': copied.write_bytes(b'changed test-only image bytes')
    else: copied.unlink()
    rejected = submit(workflow, claim, draft(claim))
    assert rejected['counts']['blocked'] == 1
    with pytest.raises(ValueError): workflow.commit(run['run_id'])


@pytest.mark.parametrize('rejection', ['protocol', 'protected_ast'])
def test_rejected_output_feedback_reaches_generator_but_not_fresh_reviewer(setup_workflow, rejection):
    full = rejection == 'protected_ast'
    workflow, service = setup_workflow(full=full, images=full, max_failures=3)
    run = workflow.start(service.bundle['paper_id'], 'feedback-'+rejection)
    claim = workflow.claim(run['run_id'])
    rejected = draft(claim)
    marker = 'REJECTED_OUTPUT_ONLY_DO_NOT_ANCHOR_REVIEW'
    if rejection == 'protocol':
        rejected['items'][0]['unexpected_field'] = marker
    else:
        rejected['notes'].append(marker)
        overlay = rejected['math_overlays'][0]
        overlay['localized_latex'] = overlay['localized_latex'].replace('\\ge c', '\\le c')
    submit(workflow, claim, rejected)
    retry = workflow.claim(run['run_id'])
    feedback = retry['request']['validation_feedback']
    assert feedback['error'].strip()
    assert feedback['rejected_candidate'] == rejected
    assert retry['phase'] == 'generate'
    submit(workflow, retry, draft(retry))
    for _ in range(4):
        next_claim = workflow.claim(run['run_id'])
        if next_claim['phase'] == 'review': break
        submit(workflow, next_claim, draft(next_claim))
    assert next_claim['phase'] == 'review'
    assert 'validation_feedback' not in next_claim['request']
    assert next_claim['request']['issues'] == []
    assert next_claim['request']['structural_error'] == ''
    assert marker not in json.dumps(next_claim['request'])


def test_database_without_feedback_column_resumes_existing_pending_run(setup_workflow, monkeypatch):
    workflow, service = setup_workflow()
    run = workflow.start(service.bundle['paper_id'], 'old-workflow-schema')
    # Recreate the immediately preceding durable schema, retaining its run/task rows.
    with workflow.connect() as con:
        con.execute('ALTER TABLE tasks DROP COLUMN last_response')
    restarted = engine.Workflow(service)
    monkeypatch.setattr(restarted, '_with_images', workflow._with_images)
    resumed = restarted.start(service.bundle['paper_id'], 'old-workflow-schema')
    assert resumed['run_id'] == run['run_id']
    claim = restarted.claim(run['run_id'])
    assert claim['phase'] == 'generate'
    submit(restarted, claim, draft(claim))
    assert restarted.claim(run['run_id'])['phase'] == 'review'
