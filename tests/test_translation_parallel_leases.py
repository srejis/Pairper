"""Independent workers may generate different tasks without a shared approval."""
from concurrent.futures import ThreadPoolExecutor

from test_translation_workflow import setup_workflow as setup_workflow, draft, submit, review


def test_two_generation_workers_claim_unique_tasks_and_preserve_both_candidates(setup_workflow):
    workflow, service = setup_workflow(full=True)
    run = workflow.start(service.bundle['paper_id'], 'parallel-generation-leases')
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: workflow.claim(run['run_id']), range(2)))
    assert all(c and c['phase'] == 'generate' for c in claims)
    assert len({c['task_id'] for c in claims}) == 2
    assert len({c['lease_id'] for c in claims}) == 2
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda c: submit(workflow, c, draft(c)), claims))
    with workflow.connect() as con:
        rows = workflow._rows(con, run['run_id'])
        by_id = {r['id']: r for r in rows}
        assert all(by_id[c['task_id']]['candidate'] for c in claims)
        assert all(by_id[c['task_id']]['status'] == 'drafted' for c in claims)
        assert all(not by_id[c['task_id']]['lease'] for c in claims)
        assert all(not by_id[c['task_id']]['review'] for c in claims)
    assert workflow.report(run['run_id'])['counts']['accepted'] == 0


def test_two_fresh_review_workers_preserve_candidates_and_bind_their_own_requests(setup_workflow):
    workflow, service = setup_workflow(full=True, images=True)
    run = workflow.start(service.bundle['paper_id'], 'parallel-review-leases')
    while workflow.report(run['run_id'])['counts']['pending']:
        claim = workflow.claim(run['run_id'])
        assert claim['phase'] == 'generate'
        submit(workflow, claim, draft(claim))
    with workflow.connect() as con:
        candidates = {r['id']: r['candidate'] for r in workflow._rows(con, run['run_id'])}
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: workflow.claim(run['run_id']), range(2)))
    assert len({c['task_id'] for c in claims}) == 2
    assert all(c['phase'] == 'review' for c in claims)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda c: submit(workflow, c, review(c)), claims))
    with workflow.connect() as con:
        rows = {r['id']: r for r in workflow._rows(con, run['run_id'])}
        assert {key: r['candidate'] for key, r in rows.items()} == candidates
        for claim in claims:
            row = rows[claim['task_id']]
            assert row['status'] == 'accepted' and not row['lease']
            assert row['review_input_hash'] == workflow._review_hash(claim['request'])
