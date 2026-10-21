"""Real temporary SQLite writers remain free during cold cache preparation."""
import copy
import json
import threading

import pytest

from test_translation_workflow import setup_workflow, draft, engine, sha


def seeded(setup_workflow, monkeypatch):
    workflow, service = setup_workflow()
    run = workflow.start(service.bundle['paper_id'], 'cold', max_units=1)
    for _ in range(2):
        claim = workflow.claim(run['run_id'])
        workflow.submit(run['run_id'], claim['task_id'], claim['lease_id'], draft(claim))
    cold = engine.Workflow(service)
    monkeypatch.setattr(cold, '_with_images', workflow._with_images)
    return cold, workflow, service, run


def background(fn, values, errors):
    def execute():
        try:
            values.append(fn())
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=execute)
    thread.start()
    return thread


@pytest.mark.parametrize('stage', ['normalize', 'glyph'])
@pytest.mark.parametrize('other_run', [False, True])
def test_cold_prewarm_does_not_block_writer_and_claims_remain_unique(setup_workflow, monkeypatch, stage, other_run):
    cold, second, service, run = seeded(setup_workflow, monkeypatch)
    probe_id = second.start(service.bundle['paper_id'], 'other', max_units=1)['run_id'] if other_run else run['run_id']
    entered, release, writer_done = threading.Event(), threading.Event(), threading.Event()
    name = '_normalize' if stage == 'normalize' else '_source_glyph_evidence'
    original = getattr(cold, name)
    def delayed(*args):
        if not entered.is_set():
            entered.set()
            assert release.wait(5), 'Test did not release cold preparation'
        return original(*args)
    monkeypatch.setattr(cold, name, delayed)
    values_a, values_b, errors = [], [], []
    thread_a = background(lambda: cold.claim(run['run_id']), values_a, errors)
    assert entered.wait(3)
    def writer():
        with second.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            con.execute('UPDATE runs SET settings=settings WHERE id=?', (probe_id,))
        writer_done.set()
        return second.claim(run['run_id'])
    thread_b = background(writer, values_b, errors)
    try:
        assert writer_done.wait(2), 'Cold preparation still holds the SQLite write lock'
        thread_b.join(3)
        assert not thread_b.is_alive(), 'Second claim could not finish while first prewarms'
    finally:
        release.set()
        thread_a.join(5); thread_b.join(5)
    assert not errors
    assert values_a[0]['task_id'] != values_b[0]['task_id']
    assert values_a[0]['lease_id'] != values_b[0]['lease_id']


def test_source_is_rechecked_after_prewarm_snapshot(setup_workflow, monkeypatch):
    cold, _, service, run = seeded(setup_workflow, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = cold._normalize
    def delayed(*args):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(cold, '_normalize', delayed)
    values, errors = [], []
    thread = background(lambda: cold.claim(run['run_id']), values, errors)
    assert entered.wait(3)
    service.bundle = copy.deepcopy(service.bundle)
    service.bundle['view_id'] = 'new-current-view'
    service.bundle['fingerprint'] = sha({k:v for k,v in service.bundle.items() if k != 'fingerprint'})
    release.set(); thread.join(5)
    assert not values and len(errors) == 1 and isinstance(errors[0], ValueError)
    assert 'Source changed' in str(errors[0])
    with cold.connect() as con:
        assert not any(row['lease'] for row in cold._rows(con, run['run_id']))


def test_changed_invalid_candidate_is_revalidated_and_journaled_inside_transaction(setup_workflow, monkeypatch):
    cold, second, _, run = seeded(setup_workflow, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = cold._normalize
    def delayed(*args):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(cold, '_normalize', delayed)
    values, errors = [], []
    thread = background(lambda: cold.claim(run['run_id']), values, errors)
    assert entered.wait(3)
    try:
        with second.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = second._rows(con, run['run_id'])[0]
            bad = json.loads(row['candidate'])
            bad['items'][0]['unit_id'] = 'foreign-unit'
            con.execute('UPDATE tasks SET candidate=? WHERE run_id=? AND id=?',
                        (engine.dumps(bad),run['run_id'],row['id']))
    finally:
        release.set(); thread.join(5)
    assert not errors and values[0]['phase'] == 'generate'
    with cold.connect() as con:
        events = list(con.execute("SELECT payload FROM events WHERE run_id=? AND event='candidate_invalidated_by_validation'", (run['run_id'],)))
        assert len(events) == 1
        assert json.loads(events[0]['payload'])['previous_candidate'] == bad
        assert cold._rows(con,run['run_id'])[0]['last_response'] == engine.dumps(bad)


def test_prewarm_valueerror_remains_authoritatively_invalidated_and_hash_is_unchanged(setup_workflow, monkeypatch):
    cold, second, service, run = seeded(setup_workflow, monkeypatch)
    with cold.connect() as con:
        rows = cold._rows(con,run['run_id'])
    before = cold._request(service.bundle, rows[0], rows, 'review')
    before_hash = cold._review_hash(before)
    cold._normalized_cache.clear(); cold._glyph_evidence_cache.clear()
    cold._prewarm_claim(run['run_id'])
    after = cold._request(service.bundle, rows[0], rows, 'review')
    assert before == after and before_hash == cold._review_hash(after)
    with second.connect() as con:
        bad = json.loads(rows[0]['candidate'])
        bad['items'][0]['unit_id'] = 'foreign-unit'
        con.execute('UPDATE tasks SET candidate=? WHERE run_id=? AND id=?',
                    (engine.dumps(bad),run['run_id'],rows[0]['id']))
    assert cold.claim(run['run_id'])['phase'] == 'generate'
    with cold.connect() as con:
        assert con.execute("SELECT count(*) FROM events WHERE event='candidate_invalidated_by_validation'").fetchone()[0] == 1


def test_prewarm_does_not_swallow_nonvalidation_errors(setup_workflow, monkeypatch):
    cold, _, _, run = seeded(setup_workflow, monkeypatch)
    def failure(*args):
        raise RuntimeError('Unexpected normalization failure')
    monkeypatch.setattr(cold, '_normalize', failure)
    with pytest.raises(RuntimeError, match='Unexpected normalization failure'):
        cold.claim(run['run_id'])
    with cold.connect() as con:
        assert not any(row['lease'] for row in cold._rows(con,run['run_id']))
        assert con.execute("SELECT count(*) FROM events WHERE event='candidate_invalidated_by_validation'").fetchone()[0] == 0
