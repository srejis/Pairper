"""Real MCP registrations/threads, fake source planner and workflow; no AI or DB."""

import threading
import time
from types import SimpleNamespace

import pytest

from pairper.translation import agent_runner, service, source_repair, workflow, workflow_mcp


class Registry:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(function):
            self.tools[function.__name__] = function
            return function
        return register

    def prompt(self):
        return lambda function: function


@pytest.fixture
def backend(monkeypatch, tmp_path):
    state = SimpleNamespace(
        release=threading.Event(), executing=threading.Event(), execute_release=threading.Event(),
        entered={}, done=[], instances=[], starts=[], steps=[], commits=[], reports={}, failure=None,
    )

    class Runner:
        def __init__(self):
            self.cancel_event = threading.Event()

        def cancel(self):
            self.cancel_event.set()

    class FakeWorkflow:
        def __init__(self, selected_service, runner=None, **options):
            self.runner = runner
            state.instances.append(self)

        def start(self, paper_id, experiment_id, **options):
            state.starts.append((paper_id, experiment_id, options, self.runner))
            run_id = 'ready-' + experiment_id
            if options.get('math_partition_v2'):
                finished = threading.Event()
                state.done.append(finished)
                state.entered.setdefault(experiment_id, threading.Event()).set()
                try:
                    assert state.release.wait(5), 'Test source planner was not released'
                    if self.runner.cancel_event.is_set():
                        raise ValueError('Source planning cancelled before derived contract')
                    if state.failure:
                        raise ValueError(state.failure)
                finally:
                    finished.set()
            state.reports[run_id] = {'run_id': run_id, 'status': 'pending', 'session_id': 'empty-session'}
            return dict(state.reports[run_id])

        def report(self, run_id):
            return dict(state.reports[run_id])

        def step(self, run_id):
            state.steps.append(run_id)
            state.executing.set()
            assert state.execute_release.wait(5), 'Test execution was not released'
            state.reports[run_id]['status'] = 'ready'
            return self.report(run_id)

        def commit(self, run_id, publish):
            state.commits.append((run_id, publish))
            if publish:
                state.reports[run_id]['status'] = 'published'
            return self.report(run_id)

    monkeypatch.setattr(service, 'TranslationService', lambda *_: SimpleNamespace(directory=tmp_path))
    monkeypatch.setattr(agent_runner, 'CodexRunner', Runner)
    monkeypatch.setattr(source_repair, 'SourceRepairer', lambda *_: object())
    monkeypatch.setattr(workflow, 'Workflow', FakeWorkflow)
    registered = Registry()
    workflow_mcp.register_workflow_tools(registered, object(), registered.tool, tmp_path)
    state.tools = registered.tools
    yield state
    state.release.set()
    state.execute_release.set()
    # Wait for actual dispatch wrappers too, not only fake start() finally.
    deadline = time.monotonic() + 5
    while any(t.name in ('pairper-partition-source', 'pairper-translation') for t in threading.enumerate()):
        assert time.monotonic() < deadline, 'MCP worker leaked after test'
        time.sleep(0.002)


def start(state, experiment='one'):
    entered = state.entered.setdefault(experiment, threading.Event())
    result = state.tools['start_translation_workflow']('paper', experiment, max_units=7, math_partition_v2=True)
    assert entered.wait(2)
    return result['planning_job_id']


def stopped(state, job_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        report = state.tools['get_translation_workflow_report'](job_id)
        if not report['background_running']:
            return report
        time.sleep(0.002)
    raise AssertionError('MCP background task did not finish')


def test_partition_pending_is_source_only_and_cannot_execute_or_finalize(backend):
    job = start(backend)
    report = backend.tools['get_translation_workflow_report'](job)
    assert report['status'] == 'source_planning'
    assert report['ready_run_id'] is None and report['prior_Korean_inputs'] == 0
    assert report['translation_approval_granted'] is False
    for operation in ('run_translation_workflow', 'finalize_translation_workflow'):
        with pytest.raises(ValueError, match='not ready'):
            backend.tools[operation](job)
    assert not backend.steps and not backend.commits


def test_partition_ready_job_resolves_to_new_run_for_execution_and_finalize(backend):
    job = start(backend)
    backend.release.set()
    ready = stopped(backend, job)
    assert ready['ready_run_id'] == 'ready-one' and ready['execution_error'] is None
    assert backend.starts[0][2] == {'max_units': 7, 'math_partition_v2': True}
    dispatched = backend.tools['run_translation_workflow'](job)
    assert dispatched['run_id'] == 'ready-one' and dispatched['published'] is False
    assert backend.executing.wait(2)
    duplicate = backend.tools['run_translation_workflow'](job)
    assert duplicate['already_running'] is True
    assert backend.steps == ['ready-one']
    backend.execute_release.set()
    assert stopped(backend, 'ready-one')['status'] == 'ready'
    assert backend.commits == [('ready-one', False)]
    assert backend.tools['finalize_translation_workflow'](job)['status'] == 'published'
    assert backend.commits[-1] == ('ready-one', True)


def test_cancel_pending_partition_stops_its_runner_without_creating_run(backend):
    job = start(backend)
    result = backend.tools['cancel_translation_workflow'](job)
    assert result['cancellation_requested'] and result['history_retained']
    assert backend.tools['get_translation_workflow_report'](job)['status'] == 'cancelled'
    backend.release.set()
    report = stopped(backend, job)
    assert report['status'] == 'cancelled' and report['ready_run_id'] is None
    assert 'cancelled' in report['execution_error']
    assert not backend.reports and not backend.steps and not backend.commits


def test_cancel_ready_job_targets_actual_execution_runner(backend):
    job = start(backend)
    backend.release.set()
    stopped(backend, job)
    backend.tools['run_translation_workflow'](job)
    assert backend.executing.wait(2)
    executor = backend.instances[-1].runner
    planner = backend.starts[0][3]
    backend.tools['cancel_translation_workflow'](job)
    assert executor.cancel_event.is_set() and not planner.cancel_event.is_set()


def test_duplicate_pending_plan_is_one_worker_and_does_not_bypass_four_limit(backend):
    first = start(backend)
    assert start(backend) == first
    for number in range(3):
        start(backend, str(number))
    assert len(backend.starts) == 4
    with pytest.raises(ValueError, match='At most4'):
        start(backend, 'fifth')
    assert len(backend.starts) == 4


def test_worker_limit_is_shared_between_planning_and_actual_execution(backend):
    backend.tools['start_translation_workflow']('paper', 'legacy')
    backend.tools['run_translation_workflow']('ready-legacy')
    assert backend.executing.wait(2)
    for number in range(3):
        start(backend, str(number))
    with pytest.raises(ValueError, match='At most4'):
        start(backend, 'fifth')
    assert len(backend.steps) == 1


def test_planner_failure_remains_unready_and_never_executes(backend):
    backend.failure = 'Original source identity changed during native planning'
    job = start(backend)
    backend.release.set()
    report = stopped(backend, job)
    assert report['status'] == 'needs_review' and report['ready_run_id'] is None
    assert report['execution_error'] == backend.failure
    with pytest.raises(ValueError, match='not ready'):
        backend.tools['run_translation_workflow'](job)
    assert not backend.steps and not backend.commits


def test_legacy_default_is_synchronous_without_native_source_planner(backend):
    result = backend.tools['start_translation_workflow']('paper', 'legacy', max_units=9)
    assert result['run_id'] == 'ready-legacy'
    assert 'planning_job_id' not in result
    assert backend.starts == [('paper', 'legacy', {'max_units': 9}, None)]
    assert not backend.done


def test_explicit_source_execution_retry_reason_reaches_background_planner_exactly(backend):
    reason = 'SYNTHETIC: verified native timeout recovered; preserve quality reservations'
    entered = backend.entered.setdefault('retry', threading.Event())
    result = backend.tools['start_translation_workflow'](
        'paper', 'retry', max_units=11, math_partition_v2=True,
        source_planning_retry_reason=reason,
    )
    assert entered.wait(2)
    assert backend.starts[0][2] == {
        'max_units': 11, 'math_partition_v2': True, 'source_planning_retry_reason': reason,
    }
    assert result['ready_run_id'] is None and result['translation_approval_granted'] is False
    # Forwarding an infrastructure retry reason grants no execution/quality approval.
    assert not backend.steps and not backend.commits
    backend.release.set()
    assert stopped(backend, result['planning_job_id'])['ready_run_id'] == 'ready-retry'
