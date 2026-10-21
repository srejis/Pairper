"""Concurrent additive migration and independent CLI paper failure regressions."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_translation_workflow import draft, engine, review, submit
from test_translation_workflow import setup_workflow as setup_workflow
from test_translation_workflow_interfaces import interfaces as interfaces

from pairper.translation import cli


def test_concurrent_legacy_initialization_adds_column_once_and_preserves_rows(setup_workflow):
    workflow, service = setup_workflow()
    run = workflow.start(service.bundle["paper_id"], "concurrent-additive-migration")
    claim = workflow.claim(run["run_id"])
    submit(workflow, claim, draft(claim))
    claim = workflow.claim(run["run_id"])
    bad_review = review(claim)
    bad_review["items"][0]["checks"] = []
    submit(workflow, claim, bad_review)
    with workflow.connect() as con:
        before = [dict(row) for row in con.execute("SELECT * FROM tasks ORDER BY ordinal")]
        events_before = [dict(row) for row in con.execute("SELECT * FROM events ORDER BY seq")]
        con.execute("ALTER TABLE tasks DROP COLUMN source_feedback")
    # Every worker begins against the same existing database at once. These are
    # real SQLite connections and initializers, not a mocked migration decision.
    barrier = threading.Barrier(8, timeout=10)

    def initialize(_):
        barrier.wait()
        return engine.Workflow(service)

    with ThreadPoolExecutor(max_workers=8) as pool:
        instances = list(pool.map(initialize, range(8)))
    assert len(instances) == 8
    with workflow.connect() as con:
        columns = [row["name"] for row in con.execute("PRAGMA table_info(tasks)")]
        after = [dict(row) for row in con.execute("SELECT * FROM tasks ORDER BY ordinal")]
        events_after = [dict(row) for row in con.execute("SELECT * FROM events ORDER BY seq")]
    assert columns.count("source_feedback") == columns.count("last_response") == 1
    assert after == before and events_after == events_before
    assert after[0]["candidate"] and after[0]["last_response"]
    assert instances[0].report(run["run_id"])["run_id"] == run["run_id"]


@pytest.mark.parametrize("plan_only", [False, True])
def test_cli_setup_failure_isolated_and_other_paper_finishes_with_exit_two(
    interfaces, monkeypatch, capsys, plan_only
):
    _, service, _ = interfaces
    constructor = cli.Workflow
    calls = []

    def first_setup_fails(*args, **kwargs):
        calls.append("initialization")
        if len(calls) == 1:
            raise RuntimeError("Simulated source_feedback migration initialization failure")
        return constructor(*args, **kwargs)

    monkeypatch.setattr(cli, "Workflow", first_setup_fails)
    args = [
        "first-paper-setup-fails",
        service.bundle["paper_id"],
        "--experiment",
        "isolated-cli-migration-error",
        "--workers",
        "1",
    ]
    args += ["--plan-only"] if plan_only else ["--publish"]
    assert cli.main(args) == 2
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    failures = [row for row in rows if row.get("event") == "paper_failed"]
    assert len(failures) == 1 and failures[0]["paper_id"] == "first-paper-setup-fails"
    assert "initialization failure" in failures[0]["error"]
    assert rows[-1]["event"] == "finished"
    failed, completed = rows[-1]["papers"]
    assert failed["status"] == "failed" and failed["paper_id"] == "first-paper-setup-fails"
    assert completed["paper_id"] == service.bundle["paper_id"]
    assert completed["status"] == ("generating" if plan_only else "published")
    assert len(calls) == 2
    saved = service.store.read(completed["session_id"], service.bundle["fingerprint"])
    assert saved["revision"] == (0 if plan_only else 2)
    assert saved["status"] == ("draft" if plan_only else "published")


def test_cli_keyboard_interrupt_is_not_swallowed_as_paper_failure(interfaces, monkeypatch, capsys):
    _, service, _ = interfaces
    cancelled = []

    class InterruptedRunner:
        def run(self, *_):
            raise KeyboardInterrupt()

        def cancel(self):
            cancelled.append(True)

    monkeypatch.setattr(cli, "CodexRunner", lambda **_: InterruptedRunner())
    assert cli.main([service.bundle["paper_id"], "--experiment", "interrupt-contract"]) == 130
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[-1] == {"status": "interrupted", "completed_tasks_retained": True}
    assert not any(row.get("event") == "paper_failed" for row in rows)
    assert len(cancelled) == 1
