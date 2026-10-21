"""Source-quality stops never consume a fresh infrastructure retry cycle."""
import json
from types import SimpleNamespace

import pytest

from test_translation_workflow import setup_workflow, draft
from pairper.translation.source_repair import SourceRepairBlocked


def test_typed_source_exhaustion_blocks_once_preserves_budget_and_explicit_recheck_is_required(setup_workflow):
    workflow, service = setup_workflow()
    calls = {'scan':0,'repair':0,'model':0}
    class Repairer:
        def scan(self,*args):
            calls['scan'] += 1
            if calls['scan'] > 1:
                return []
            return [SimpleNamespace(kind='math_latex',model_dump=lambda:{'source_id':'private-orchestration-finding'})]
        def repair(self,bundle,findings):
            calls['repair'] += 1
            raise SourceRepairBlocked('Source proposal limit exhausted',{
                'bundle_fingerprint':bundle['fingerprint'],'source_proposal_chain':'private-chain.json',
                'proposal_attempts':3,'library_write_permitted':False})
    class Runner:
        def run(self,request,*args):
            calls['model'] += 1
            response = draft({'request':request})
            response['source_concerns'] = ['Inspect the original source in this deterministic fixture.']
            return response
    workflow.source_repairer = Repairer()
    workflow.runner = Runner()
    run = workflow.start(service.bundle['paper_id'],'source-quality-stop')
    # Prove the latest-event quality gate still prevents infrastructure retry
    # even when a legacy failure counter is already at its infrastructure cap.
    with workflow.connect() as con:
        con.execute('UPDATE tasks SET failures=3 WHERE run_id=?',(run['run_id'],))
    report = workflow.step(run['run_id'])
    assert report['status'] == 'needs_review' and report['leased'] == 0
    with workflow.connect() as con:
        row = workflow._rows(con,run['run_id'])[0]
        assert row['status'] == 'blocked' and row['failures'] == 3 and row['repair_count'] == 0
        assert row['candidate'] is None and row['review'] is None
        latest = con.execute('SELECT event,payload FROM events ORDER BY seq DESC LIMIT 1').fetchone()
        assert latest['event'] == 'source_repair_blocked'
        assert not json.loads(latest['payload'])['translation_approval_granted']
    retried = workflow.retry_failed(run['run_id'],'Explicit infrastructure retry must not reopen quality exhaustion')
    assert retried['retried_task_ids'] == [] and retried['status'] == 'needs_review'
    workflow.step(run['run_id'])
    assert calls == {'scan':1,'repair':1,'model':1}
    saved = service.store.read(run['session_id'],service.bundle['fingerprint'])
    assert saved['revision'] == 0 and saved['result']['entries'] == []
    # Explicit PDF source reinspection remains possible; it still grants no
    # translation approval and asks for fresh generation/review afterwards.
    rechecked = workflow.recheck_source_blocked(run['run_id'],'Fresh explicit original-PDF inspection')
    assert rechecked['source_rechecked_task_ids']
    assert rechecked['counts']['pending'] == 1
    assert calls['scan'] == 2 and calls['model'] == 1


def test_typed_quality_stop_cannot_reclassify_a_revoked_lease(setup_workflow):
    workflow, service = setup_workflow()
    class Repairer:
        def scan(self,*args):
            return [SimpleNamespace(kind='math_latex',model_dump=lambda:{'source_id':'private-finding'})]
        def repair(self,bundle,findings):
            with workflow.connect() as con:
                con.execute('UPDATE tasks SET lease=NULL,lease_until=NULL')
            raise SourceRepairBlocked('Quality stop after another worker revoked lease',
                                      {'bundle_fingerprint':bundle['fingerprint']})
    workflow.source_repairer = Repairer()
    run = workflow.start(service.bundle['paper_id'],'revoked-source-review')
    claimed = workflow.claim(run['run_id'])
    response = draft(claimed);response['source_concerns']=['Inspect original source.']
    with pytest.raises(ValueError,match='lease'):
        workflow._inspect_source_response(run['run_id'],claimed,response)
    with workflow.connect() as con:
        assert con.execute("SELECT count(*) FROM events WHERE event='source_repair_blocked'").fetchone()[0] == 0
