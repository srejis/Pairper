"""Real staged CLI/MCP functions over real temporary workflow/store databases.

Only the AI invocation, library/source boundary and page renderer are faked.
No existing Korean or evaluation outputs are used as candidate/review data.
"""
import copy
import json
import threading
import time

import pytest

from test_translation_workflow import setup_workflow, draft, review, engine, Entry, sha
from pairper.translation import agent_runner, service as service_module
from pairper.translation import cli, workflow_mcp


class RegisteredTools:
    def __init__(self):
        self.tools, self.prompts = {}, {}

    def tool(self):
        def register(func):
            assert func.__name__ not in self.tools
            self.tools[func.__name__] = func
            return func
        return register

    def prompt(self):
        def register(func):
            self.prompts[func.__name__] = func
            return func
        return register


class ProtocolRunner:
    def run(self, request, schema, images, directory):
        claim = {'request':request}
        return review(claim) if request['phase'] == 'review' else draft(claim)

    def cancel(self):
        pass


@pytest.fixture
def interfaces(setup_workflow, monkeypatch):
    workflow, service = setup_workflow()
    original = engine.Workflow
    def constructor(selected_service, runner=None, **options):
        assert selected_service is service
        instance = original(service, runner, **options)
        monkeypatch.setattr(instance, '_with_images', workflow._with_images)
        return instance
    def prepare(library, paper_id, directory):
        if paper_id != service.bundle['paper_id']:
            raise ValueError('Unknown paper ID')
        return copy.deepcopy(service.bundle)
    monkeypatch.setattr(engine, 'prepare_bundle', prepare)
    monkeypatch.setattr(engine, 'Workflow', constructor)
    monkeypatch.setattr(service_module, 'TranslationService', lambda *_: service)
    monkeypatch.setattr(agent_runner, 'CodexRunner', lambda: registered.runner_type())
    monkeypatch.setattr(cli, 'Library', lambda *_: service.library)
    monkeypatch.setattr(cli, 'TranslationService', lambda *_: service)
    monkeypatch.setattr(cli, 'Workflow', constructor)
    monkeypatch.setattr(cli, 'CodexRunner', lambda **_: ProtocolRunner())
    registered = RegisteredTools()
    registered.runner_type = ProtocolRunner
    workflow_mcp.register_workflow_tools(registered, service.library, registered.tool, service.directory)
    return workflow, service, registered


def wait_stopped(tools, run_id):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        report = tools['get_translation_workflow_report'](run_id)
        if not report['background_running']:
            # The status snapshot can precede the thread completing between its
            # DB read and is_alive check. Read once after observed termination.
            return tools['get_translation_workflow_report'](run_id)
        time.sleep(0.01)
    raise AssertionError('Background workflow did not stop within test deadline')


def test_mcp_registration_start_report_background_and_commit(interfaces):
    _, service, registered = interfaces
    tools = registered.tools
    assert set(tools) == {'start_translation_workflow','get_translation_workflow_report',
                         'run_translation_workflow','finalize_translation_workflow','cancel_translation_workflow'}
    assert 'run_korean_translation_system' in registered.prompts
    start = tools['start_translation_workflow'](service.bundle['paper_id'], 'mcp-happy')
    before = tools['get_translation_workflow_report'](start['run_id'])
    assert before['background_running'] is False
    with pytest.raises(ValueError): tools['finalize_translation_workflow'](start['run_id'])
    tools['run_translation_workflow'](start['run_id'])
    report = wait_stopped(tools, start['run_id'])
    assert report['status'] == 'ready' and report['execution_error'] is None, report
    assert service.store.read(start['session_id'], service.bundle['fingerprint'])['status'] == 'draft'
    finalized = tools['finalize_translation_workflow'](start['run_id'])
    assert finalized['status'] == 'published'


def test_mcp_duplicate_background_start_and_cancel_keep_one_worker(interfaces, monkeypatch):
    _, service, registered = interfaces
    entered, cancelled = threading.Event(), threading.Event()
    invocations = []
    class BlockingRunner:
        def run(self, request, schema, images, directory):
            invocations.append(threading.current_thread().ident)
            entered.set()
            assert cancelled.wait(3), 'Test failed to cancel the blocked model invocation'
            raise RuntimeError('actual process cancellation simulated')
        def cancel(self): cancelled.set()
    registered.runner_type = BlockingRunner
    tools = registered.tools
    start = tools['start_translation_workflow'](service.bundle['paper_id'], 'mcp-cancel')
    again = tools['start_translation_workflow'](service.bundle['paper_id'], 'mcp-cancel')
    assert again['run_id'] == start['run_id']
    tools['run_translation_workflow'](start['run_id'])
    assert entered.wait(3)
    duplicate = tools['run_translation_workflow'](start['run_id'])
    assert duplicate['already_running'] is True
    assert len(invocations) == 1
    with pytest.raises(ValueError, match='still running'):
        tools['finalize_translation_workflow'](start['run_id'])
    assert tools['cancel_translation_workflow'](start['run_id'])['cancellation_requested']
    stopped = wait_stopped(tools, start['run_id'])
    assert stopped['status'] == 'needs_review', stopped
    assert len(set(invocations)) == 1
    assert service.store.read(start['session_id'], service.bundle['fingerprint'])['revision'] == 0


@pytest.mark.parametrize('tool_name', ['get_translation_workflow_report','run_translation_workflow','finalize_translation_workflow'])
def test_mcp_nonexistent_run_fails_without_creating_a_run(interfaces, tool_name):
    workflow, _, registered = interfaces
    with pytest.raises(ValueError): registered.tools[tool_name]('foreign-run-id')
    with workflow.connect() as con:
        assert con.execute('SELECT count(*) FROM runs').fetchone()[0] == 0


def test_cli_plan_only_never_constructs_ai_or_stores_candidates(interfaces, monkeypatch, capsys):
    _, service, _ = interfaces
    def forbidden(**_): raise AssertionError('plan-only must not initialize a model client')
    monkeypatch.setattr(cli, 'CodexRunner', forbidden)
    args = [service.bundle['paper_id'], '--experiment','cli-plan','--plan-only']
    assert cli.main(args) == 0
    first = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert cli.main(args) == 0
    second = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert first[0]['run_id'] == second[0]['run_id']
    saved = service.store.read(first[0]['session_id'], service.bundle['fingerprint'])
    assert saved['revision'] == 0 and saved['result']['entries'] == []


def test_cli_all_filtered_source_only_and_duplicate_selection_rejected(interfaces, monkeypatch, capsys):
    _, service, _ = interfaces
    monkeypatch.setattr(service.library, 'list_papers', lambda:[{'id':service.bundle['paper_id'],'has_view':True},
                                                              {'id':'no-view','has_view':False}], raising=False)
    assert cli.main(['--all','--experiment','all-plan','--plan-only']) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(rows[-1]['papers']) == 1
    with pytest.raises(SystemExit) as exc:
        cli.main([service.bundle['paper_id'],service.bundle['paper_id'],'--experiment','duplicate','--plan-only'])
    assert exc.value.code == 2


def test_cli_needs_review_has_nonzero_exit_and_no_fake_commit(interfaces, monkeypatch, capsys):
    _, service, _ = interfaces
    class FailedRunner:
        def run(self, *_): raise RuntimeError('provider unavailable')
    monkeypatch.setattr(cli, 'CodexRunner', lambda **_: FailedRunner())
    assert cli.main([service.bundle['paper_id'],'--experiment','cli-failure','--max-steps','10','--publish']) == 2
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    final = rows[-1]['papers'][0]
    assert final['status'] == 'needs_review'
    assert service.store.read(final['session_id'],service.bundle['fingerprint'])['revision'] == 0


def test_cli_nonexistent_paper_is_failure_not_empty_success(interfaces, capsys):
    workflow, _, _ = interfaces
    assert cli.main(['foreign-paper-id','--experiment','missing','--plan-only']) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'failed'
    with workflow.connect() as con:
        assert con.execute('SELECT count(*) FROM runs').fetchone()[0] == 0


def test_cli_prefilled_same_experiment_is_rejected(interfaces, capsys):
    _, service, _ = interfaces
    saved = service.store.begin(service.bundle, 'prefilled')
    unit = service.bundle['units'][0]
    service.store.save(saved['session_id'],service.bundle['fingerprint'],0,
        entries=[Entry(action='translate',source_unit_ids=[unit['id']],source_fingerprint=sha([unit['fingerprint']]),target='외부 기존 번역').model_dump()])
    assert cli.main([service.bundle['paper_id'],'--experiment','prefilled','--plan-only']) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'failed'
    assert service.store.read(saved['session_id'],service.bundle['fingerprint'])['revision'] == 1


def test_cli_step_budget_stops_partial_work_without_publishing(interfaces, capsys):
    _, service, _ = interfaces
    assert cli.main([service.bundle['paper_id'],'--experiment','partial','--max-steps','1','--publish']) == 2
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    final = rows[-1]['papers'][0]
    assert final['status'] not in ('ready','published')
    assert service.store.read(final['session_id'],service.bundle['fingerprint'])['revision'] == 0


def test_cli_success_publishes_only_after_draft_and_review(interfaces, capsys):
    _, service, _ = interfaces
    assert cli.main([service.bundle['paper_id'],'--experiment','cli-happy','--publish']) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    final = rows[-1]['papers'][0]
    assert final['status'] == 'published' and final['semantic_quality_proven'] is False
    assert service.store.read(final['session_id'],service.bundle['fingerprint'])['report']['complete']


def test_mcp_stopped_report_refreshes_snapshot_read_before_worker_finished(interfaces, monkeypatch):
    workflow, service, registered = interfaces
    entered, release = threading.Event(), threading.Event()
    workers = []
    class ControlledRunner(ProtocolRunner):
        def run(self, request, schema, images, directory):
            if not workers:
                workers.append(threading.current_thread())
                entered.set()
                assert release.wait(3)
            return super().run(request, schema, images, directory)
    registered.runner_type = ControlledRunner
    tools = registered.tools
    start = tools['start_translation_workflow'](service.bundle['paper_id'], 'mcp-report-snapshot')
    tools['run_translation_workflow'](start['run_id'])
    assert entered.wait(3)
    real_report = type(workflow).report
    armed = [True]
    caller = threading.current_thread().ident
    def snapshot_before_completion(instance, run_id):
        snapshot = real_report(instance, run_id)
        if armed[0] and threading.current_thread().ident == caller:
            armed[0] = False
            assert snapshot['status'] != 'ready'
            release.set()
            workers[0].join(3)
            assert not workers[0].is_alive()
        return snapshot
    monkeypatch.setattr(type(workflow), 'report', snapshot_before_completion)
    # Force the worker to finish between the product's first DB read and its
    # thread-state check. The single returned report must contain final state.
    report = tools['get_translation_workflow_report'](start['run_id'])
    assert report['background_running'] is False
    assert report['status'] == 'ready' and report['execution_error'] is None
