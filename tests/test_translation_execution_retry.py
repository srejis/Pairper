"""Explicit execution retry never clears source/quality review decisions."""

import json

import pytest
from test_translation_workflow import draft, engine, finish, review, submit
from test_translation_workflow import setup_workflow as setup_workflow
from test_translation_workflow_interfaces import ProtocolRunner
from test_translation_workflow_interfaces import interfaces as interfaces

from pairper.translation import cli


def task_row(workflow, task_id):
    with workflow.connect() as con:
        return dict(con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())


def reject(workflow, claim):
    payload = review(claim) if claim["phase"] == "review" else draft(claim)
    payload.pop("notes")  # Structurally rejected output retained as feedback.
    submit(workflow, claim, payload)
    return payload


@pytest.mark.parametrize("phase", ["generate", "repair", "review"])
@pytest.mark.parametrize("last_event", ["provider_failed", "response_rejected"])
def test_restart_retry_preserves_candidates_reviews_feedback_and_quality_budget(
    setup_workflow, monkeypatch, phase, last_event
):
    workflow, service = setup_workflow(max_failures=2)
    run = workflow.start(service.bundle["paper_id"], "retry-" + phase + "-" + last_event)
    if phase != "generate":
        claim = workflow.claim(run["run_id"])
        submit(workflow, claim, draft(claim))
    if phase == "repair":
        claim = workflow.claim(run["run_id"])
        submit(workflow, claim, review(claim, "repair"))
    claim = workflow.claim(run["run_id"])
    assert claim["phase"] == phase
    previous_output = reject(workflow, claim)
    # A pre-existing detached scan observation is durable audit metadata, not a
    # quality approval. Retrying execution must not erase it either.
    with workflow.connect() as con:
        con.execute(
            "UPDATE tasks SET source_feedback=? WHERE id=?",
            (
                json.dumps(
                    {
                        "bundle_fingerprint": service.bundle["fingerprint"],
                        "task_id": claim["task_id"],
                        "notes": ["Test-only preserved original-source scan observation."],
                        "findings": [],
                    }
                ),
                claim["task_id"],
            ),
        )
    claim = workflow.claim(run["run_id"])
    assert claim["phase"] == phase
    if last_event == "provider_failed":
        workflow.fail(
            run["run_id"],
            claim["task_id"],
            claim["lease_id"],
            "Original PDF inspection provider unavailable",
        )
    else:
        previous_output = reject(workflow, claim)
    before = task_row(workflow, claim["task_id"])
    assert before["status"] == "blocked" and before["failures"] == 2
    restarted = engine.Workflow(service, max_failures=2)
    monkeypatch.setattr(restarted, "_with_images", workflow._with_images)
    reason = "Generic provider implementation fixed; repeat execution without granting approval."
    retried = restarted.retry_failed(run["run_id"], reason)
    assert retried["retried_count"] == 1 and retried["retried_task_ids"] == [claim["task_id"]]
    after = task_row(restarted, claim["task_id"])
    assert (
        after["status"]
        == {"generate": "pending", "repair": "repair_pending", "review": "drafted"}[phase]
    )
    assert after["failures"] == 0
    for key in (
        "candidate",
        "review",
        "repair_count",
        "last_response",
        "last_error",
        "source_feedback",
    ):
        assert after[key] == before[key], key
    with restarted.connect() as con:
        payload = json.loads(
            con.execute(
                "SELECT payload FROM events WHERE event='execution_retry_requested'"
            ).fetchone()[0]
        )
    assert payload["reason"] == reason and payload["previous_failures"] == 2
    assert (
        payload["previous_error"] == before["last_error"] and payload["failure_event"] == last_event
    )
    assert restarted.retry_failed(run["run_id"], "Duplicate retry is a no-op")["retried_count"] == 0
    pending = restarted.claim(run["run_id"])
    assert pending["phase"] == phase
    if phase in ("generate", "repair"):
        assert pending["request"]["validation_feedback"]["rejected_candidate"] == previous_output
    with pytest.raises(ValueError):
        restarted.commit(run["run_id"])
    submit(restarted, pending, review(pending) if phase == "review" else draft(pending))
    if phase != "review":
        fresh = restarted.claim(run["run_id"])
        assert fresh["phase"] == "review"
        submit(restarted, fresh, review(fresh))
    assert restarted.claim(run["run_id"]) is None
    assert restarted.commit(run["run_id"])["status"] == "ready"


@pytest.mark.parametrize("reason", ["", "   ", None, 42])
def test_retry_requires_explicit_nonempty_reason(setup_workflow, reason):
    workflow, service = setup_workflow()
    run = workflow.start(service.bundle["paper_id"], "reason-required")
    with pytest.raises(ValueError, match="reason"):
        workflow.retry_failed(run["run_id"], reason)


def test_retry_refuses_active_other_task_lease_and_changed_source(setup_workflow):
    workflow, service = setup_workflow(max_failures=1)
    run = workflow.start(service.bundle["paper_id"], "retry-active-source", max_units=1)
    failed = workflow.claim(run["run_id"])
    workflow.fail(run["run_id"], failed["task_id"], failed["lease_id"], "provider down")
    before = task_row(workflow, failed["task_id"])
    active = workflow.claim(run["run_id"])
    assert active["task_id"] != failed["task_id"]
    with pytest.raises(ValueError, match="lease"):
        workflow.retry_failed(run["run_id"], "fixed")
    assert task_row(workflow, failed["task_id"]) == before
    workflow.fail(run["run_id"], active["task_id"], active["lease_id"], "provider down")
    service.bundle["fingerprint"] = "changed-current-source"
    with pytest.raises(ValueError, match="Source|source"):
        workflow.retry_failed(run["run_id"], "fixed")
    assert task_row(workflow, failed["task_id"]) == before


@pytest.mark.parametrize("terminal", ["ready", "published", "source_changed"])
def test_retry_refuses_terminal_and_source_changed_workflows(setup_workflow, terminal):
    workflow, service = setup_workflow()
    run = workflow.start(service.bundle["paper_id"], "terminal-" + terminal)
    if terminal != "source_changed":
        finish(workflow, run["run_id"])
        workflow.commit(run["run_id"], publish=terminal == "published")
    else:
        with workflow.connect() as con:
            con.execute("UPDATE runs SET state='source_changed'")
    with pytest.raises(ValueError):
        workflow.retry_failed(run["run_id"], "No quality bypass")


def test_quality_repair_limit_and_source_block_are_never_released(setup_workflow):
    workflow, service = setup_workflow(max_repairs=0, max_failures=1)
    run = workflow.start(service.bundle["paper_id"], "quality-limit")
    claim = workflow.claim(run["run_id"])
    submit(workflow, claim, draft(claim))
    claim = workflow.claim(run["run_id"])
    submit(workflow, claim, review(claim, "repair"))
    before = task_row(workflow, claim["task_id"])
    assert before["status"] == "blocked"
    assert workflow.retry_failed(run["run_id"], "External code repaired")["retried_count"] == 0
    assert task_row(workflow, claim["task_id"]) == before
    # Even an execution-looking failure count cannot override the latest source
    # quality decision. This also protects old/mixed-version audit records.
    with workflow.connect() as con:
        con.execute("UPDATE tasks SET failures=1 WHERE id=?", (claim["task_id"],))
        workflow._event(
            con,
            run["run_id"],
            claim["task_id"],
            "source",
            "source_repair_blocked",
            {"findings": [{"kind": "unresolved"}]},
        )
    source_before = task_row(workflow, claim["task_id"])
    assert workflow.retry_failed(run["run_id"], "Source must remain blocked")["retried_count"] == 0
    assert task_row(workflow, claim["task_id"]) == source_before
    with pytest.raises(ValueError):
        workflow.commit(run["run_id"])


def test_cli_retry_failed_once_resumes_same_experiment_after_provider_fix(
    interfaces, monkeypatch, capsys
):
    workflow, service, _ = interfaces

    class FailedRunner:
        def run(self, *_):
            raise RuntimeError("Source inspection provider internal bug")

    monkeypatch.setattr(cli, "CodexRunner", lambda **_: FailedRunner())
    args = [
        service.bundle["paper_id"],
        "--experiment",
        "cli-explicit-retry",
        "--max-steps",
        "10",
        "--publish",
    ]
    assert cli.main(args) == 2
    first = [json.loads(line) for line in capsys.readouterr().out.splitlines()][-1]["papers"][0]
    assert first["status"] == "needs_review"
    calls = []
    actual = type(workflow).retry_failed

    def counted(instance, run_id, reason):
        calls.append((run_id, reason))
        return actual(instance, run_id, reason)

    monkeypatch.setattr(type(workflow), "retry_failed", counted)
    monkeypatch.setattr(cli, "CodexRunner", lambda **_: ProtocolRunner())
    assert cli.main(args + ["--retry-failed", "Generic source-page provider fix installed"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert calls == [(first["run_id"], "Generic source-page provider fix installed")]
    assert sum(row.get("event") == "execution_retry_requested" for row in rows) == 1
    assert rows[-1]["papers"][0]["run_id"] == first["run_id"]
    assert rows[-1]["papers"][0]["status"] == "published"


@pytest.mark.parametrize("publish_first", [False, True])
def test_cli_all_retry_skips_ready_or_published_papers(
    interfaces, monkeypatch, capsys, publish_first
):
    workflow, service, _ = interfaces
    args = [service.bundle["paper_id"], "--experiment", "terminal-cli-retry"]
    if publish_first:
        args.append("--publish")
    assert cli.main(args) == 0
    capsys.readouterr()
    monkeypatch.setattr(
        service.library,
        "list_papers",
        lambda: [{"id": service.bundle["paper_id"], "has_view": True}],
        raising=False,
    )

    def forbidden(*_):
        raise AssertionError("Terminal workflow retry API must not be called by --all")

    monkeypatch.setattr(type(workflow), "retry_failed", forbidden)
    assert (
        cli.main(
            [
                "--all",
                "--experiment",
                "terminal-cli-retry",
                "--retry-failed",
                "Retry other papers only",
                "--publish",
            ]
        )
        == 0
    )
    final = [json.loads(line) for line in capsys.readouterr().out.splitlines()][-1]["papers"][0]
    assert final["status"] == "published"


@pytest.mark.parametrize("phase", ["generate", "review", "repair"])
def test_image_preparation_retry_uses_failed_claim_phase_and_preserves_evidence(
    setup_workflow, monkeypatch, phase
):
    workflow, service = setup_workflow(max_failures=3)
    run = workflow.start(service.bundle["paper_id"], "image-preparation-" + phase)
    first = workflow.claim(run["run_id"])
    if phase == "generate":
        workflow.fail(run["run_id"], first["task_id"], first["lease_id"], "initial provider failure")
    else:
        submit(workflow, first, draft(first))
        if phase == "repair":
            claim = workflow.claim(run["run_id"])
            submit(workflow, claim, review(claim, "repair"))
    attached = workflow._with_images
    def unavailable(*_):
        raise ValueError("Figure source span is outside the current figure")
    monkeypatch.setattr(workflow, "_with_images", unavailable)
    for _ in range(3):
        assert workflow.claim(run["run_id"]) is None
    before = task_row(workflow, first["task_id"])
    assert before["status"] == "blocked" and before["failures"] >= 3
    # Actual legacy shape: candidate saved, but row.phase still says generate
    # because image preparation failed before a review lease was assigned.
    if phase == "review":
        assert before["phase"] == "generate" and before["candidate"]
    with workflow.connect() as con:
        latest = dict(con.execute("SELECT * FROM events WHERE task_id=? ORDER BY seq DESC LIMIT 1",
                                  (first["task_id"],)).fetchone())
    assert latest["event"] == "image_preparation_failed" and latest["phase"] == phase
    result = workflow.retry_failed(run["run_id"], "Evidence renderer contract repaired; new independent review required")
    assert result["retried_count"] == 1
    after = task_row(workflow, first["task_id"])
    assert after["phase"] == phase
    assert after["status"] == {"generate": "pending", "review": "drafted", "repair": "repair_pending"}[phase]
    assert after["failures"] == 0
    for key in ("candidate", "review", "repair_count", "last_error", "last_response", "source_feedback"):
        assert after[key] == before[key]
    with workflow.connect() as con:
        assert dict(con.execute("SELECT * FROM events WHERE seq=?", (latest["seq"],)).fetchone()) == latest
        event = json.loads(con.execute("SELECT payload FROM events WHERE event='execution_retry_requested'").fetchone()[0])
    assert event["previous_failures"] == before["failures"]
    assert event["failure_event_seq"] == latest["seq"] and event["retry_phase"] == phase
    monkeypatch.setattr(workflow, "_with_images", attached)
    claim = workflow.claim(run["run_id"])
    assert claim["phase"] == phase
    with pytest.raises(ValueError):
        workflow.commit(run["run_id"])


@pytest.mark.parametrize("event_phase,candidate_present", [
    ("", True), ("source", True), ("unknown", False),
    ("generate", True), ("review", False), ("repair", False)])
def test_image_preparation_retry_rejects_missing_phase_or_candidate_contradiction(
    setup_workflow, event_phase, candidate_present
):
    workflow, service = setup_workflow(max_failures=3)
    run = workflow.start(service.bundle["paper_id"], "bad-image-phase")
    claim = workflow.claim(run["run_id"])
    if candidate_present:
        submit(workflow, claim, draft(claim))
    with workflow.connect() as con:
        con.execute("UPDATE tasks SET status='blocked',failures=3,lease=NULL,lease_until=NULL WHERE id=?", (claim["task_id"],))
        workflow._event(con, run["run_id"], claim["task_id"], event_phase,
                        "image_preparation_failed", {"error": "invalid stored phase"})
    before = task_row(workflow, claim["task_id"])
    assert workflow.retry_failed(run["run_id"], "Do not infer an inconsistent retry phase")["retried_count"] == 0
    assert task_row(workflow, claim["task_id"]) == before
