"""Explicit source reinspection is not translation/quality approval.

All scanner/translator calls are deterministic fakes and all state is temporary.
The initial blocked states are produced by the actual workflow event path.
"""

import copy
import json
from types import SimpleNamespace

import pytest
from test_translation_workflow import draft, finish, review, sha, submit
from test_translation_workflow import setup_workflow as setup_workflow
from test_translation_workflow_interfaces import interfaces as interfaces

from pairper.translation import cli

ORIGINAL_CONCERN = "ORIGINAL_SOURCE_FINDING_RESPONSE_EVIDENCE"
NEW_OBSERVATION = "NEW_PDF_INSPECTION_NO_OUTPUT_CHANGE"


class InitialUnresolvedScanner:
    def scan(self, *_):
        finding = {
            "kind": "unresolved",
            "source_id": "source-only-test-ambiguity",
            "reason": "Initial PDF inspection could not establish output fidelity.",
        }
        return [SimpleNamespace(kind="unresolved", model_dump=lambda: dict(finding))]


class FreshScanner:
    def __init__(self, outcome="no_findings"):
        self.outcome = outcome
        self.calls = []
        self.feedback = None

    def scan(self, bundle, task, observations, images):
        self.calls.append(
            (bundle["fingerprint"], copy.deepcopy(task), copy.deepcopy(observations), list(images))
        )
        if self.outcome == "exception":
            raise OSError("New original PDF source scan failed")
        if self.outcome == "unresolved":
            return InitialUnresolvedScanner().scan()
        notes = [
            NEW_OBSERVATION
            + ": The original source is preserved; auxiliary evidence does not alter output."
        ]
        self.feedback = {
            "bundle_fingerprint": bundle["fingerprint"],
            "task_id": task["task_id"],
            "notes": notes,
            "findings": [],
            "artifact_dir": "test-fresh-source-scan",
            "scan_response_sha256": sha({"findings": [], "notes": notes}),
        }
        return []

    def get_scan_feedback(self, bundle, task_id=None):
        assert self.feedback["bundle_fingerprint"] == bundle["fingerprint"]
        assert self.feedback["task_id"] == task_id
        return copy.deepcopy(self.feedback)

    def repair(self, *_):
        raise AssertionError("No-correction/unresolved source recheck must not apply source edits")


def block_source(workflow, service, phase="generate", max_units=32):
    workflow.source_repairer = InitialUnresolvedScanner()
    run = workflow.start(service.bundle["paper_id"], "source-recheck-" + phase, max_units=max_units)
    if phase == "review":
        claim = workflow.claim(run["run_id"])
        submit(workflow, claim, draft(claim))
    responses = []

    class SourceConcernRunner:
        def run(self, request, *_):
            claim = {"request": request}
            answer = review(claim) if request["phase"] == "review" else draft(claim)
            answer["source_concerns"] = [ORIGINAL_CONCERN]
            responses.append(copy.deepcopy(answer))
            return answer

    workflow.runner = SourceConcernRunner()
    report = workflow.step(run["run_id"])
    assert report["status"] == "needs_review"
    with workflow.connect() as con:
        row = dict(con.execute("SELECT * FROM tasks WHERE status='blocked'").fetchone())
        finding_event = json.loads(
            con.execute(
                "SELECT payload FROM events WHERE task_id=? AND event='source_findings' ORDER BY seq DESC LIMIT 1",
                (row["id"],),
            ).fetchone()[0]
        )
    assert finding_event["translation_response"] == responses[0]
    return run, row, responses[0]


@pytest.mark.parametrize("phase", ["generate", "review"])
def test_no_findings_recheck_invokes_new_scanner_and_requires_new_translation_review(
    setup_workflow, phase
):
    workflow, service = setup_workflow()
    source_before = copy.deepcopy(service.bundle)
    run, old, original_response = block_source(workflow, service, phase)
    scanner = FreshScanner()
    workflow.source_repairer = scanner
    report = workflow.recheck_source_blocked(
        run["run_id"], "Generic auxiliary-evidence policy fixed; inspect PDF again."
    )
    assert report["source_rechecked_task_ids"] == [old["id"]]
    assert len(scanner.calls) == 1
    assert ORIGINAL_CONCERN in json.dumps(scanner.calls[0][2])
    assert scanner.calls[0][1]["candidate_to_inspect"] == original_response
    with workflow.connect() as con:
        row = dict(con.execute("SELECT * FROM tasks WHERE id=?", (old["id"],)).fetchone())
        events = [
            entry[0]
            for entry in con.execute("SELECT event FROM events WHERE task_id=?", (old["id"],))
        ]
    assert row["lease"] is None and row["lease_until"] is None
    assert row["status"] == ("pending" if phase == "generate" else "drafted")
    assert row["candidate"] == old["candidate"] and row["review"] is None
    assert row["repair_count"] == old["repair_count"]
    assert json.loads(row["source_feedback"]) == scanner.feedback
    assert (
        "source_recheck_requested" in events and "source_rechecked_requires_translation" in events
    )
    assert report["counts"]["accepted"] == 0
    assert service.bundle == source_before
    assert service.store.read(run["session_id"], service.bundle["fingerprint"])["revision"] == 0
    with pytest.raises(ValueError):
        workflow.commit(run["run_id"])
    fresh = workflow.claim(run["run_id"])
    assert fresh["phase"] == phase
    if phase == "generate":
        assert fresh["request"]["source_verification_feedback"] == scanner.feedback
        submit(workflow, fresh, draft(fresh))
        fresh = workflow.claim(run["run_id"])
    assert fresh["phase"] == "review"
    assert "source_verification_feedback" not in fresh["request"]
    assert ORIGINAL_CONCERN not in json.dumps(fresh["request"])
    assert NEW_OBSERVATION not in json.dumps(fresh["request"])
    submit(workflow, fresh, review(fresh))
    assert workflow.claim(run["run_id"]) is None
    assert workflow.commit(run["run_id"])["status"] == "ready"


@pytest.mark.parametrize("outcome", ["unresolved", "exception"])
def test_unresolved_or_failed_source_recheck_remains_blocked_and_releases_lease(
    setup_workflow, outcome
):
    workflow, service = setup_workflow()
    run, old, _ = block_source(workflow, service)
    scanner = FreshScanner(outcome)
    workflow.source_repairer = scanner
    report = workflow.recheck_source_blocked(
        run["run_id"], "Explicit independent source reinspection"
    )
    assert report["status"] == "needs_review" and report["counts"]["blocked"] == 1
    assert len(scanner.calls) == 1
    with workflow.connect() as con:
        row = dict(con.execute("SELECT * FROM tasks WHERE id=?", (old["id"],)).fetchone())
        events = [
            entry[0]
            for entry in con.execute("SELECT event FROM events WHERE task_id=?", (old["id"],))
        ]
    assert row["candidate"] == old["candidate"]
    assert row["lease"] is None and row["lease_until"] is None
    assert row["status"] == "blocked"
    assert (
        "source_recheck_failed" if outcome == "exception" else "source_repair_blocked"
    ) in events
    assert "source_rechecked_requires_translation" not in events
    with pytest.raises(ValueError):
        workflow.commit(run["run_id"])


def test_source_recheck_ignores_quality_repair_limit_block(setup_workflow):
    workflow, service = setup_workflow(max_repairs=0)
    run = workflow.start(service.bundle["paper_id"], "quality-is-not-source-recheck")
    claim = workflow.claim(run["run_id"])
    submit(workflow, claim, draft(claim))
    claim = workflow.claim(run["run_id"])
    submit(workflow, claim, review(claim, "repair"))
    scanner = FreshScanner()
    workflow.source_repairer = scanner
    with workflow.connect() as con:
        before = dict(con.execute("SELECT * FROM tasks").fetchone())
    result = workflow.recheck_source_blocked(run["run_id"], "Cannot bypass translation quality")
    assert result["source_rechecked_task_ids"] == [] and scanner.calls == []
    with workflow.connect() as con:
        assert dict(con.execute("SELECT * FROM tasks").fetchone()) == before


def test_source_recheck_image_preparation_failure_is_recorded_without_raising(
    setup_workflow, monkeypatch
):
    workflow, service = setup_workflow()
    run, old, _ = block_source(workflow, service)
    scanner = FreshScanner()
    workflow.source_repairer = scanner

    def unavailable(*_):
        raise OSError("Original image preparation unavailable during explicit source recheck")

    monkeypatch.setattr(workflow, "_with_images", unavailable)
    result = workflow.recheck_source_blocked(run["run_id"], "Retry original evidence transport")
    assert result["status"] == "needs_review" and scanner.calls == []
    with workflow.connect() as con:
        row = dict(con.execute("SELECT * FROM tasks WHERE id=?", (old["id"],)).fetchone())
        event = con.execute(
            "SELECT payload FROM events WHERE event='source_recheck_failed'"
        ).fetchone()
    assert row["status"] == "blocked" and row["lease"] is None
    assert "image preparation unavailable" in json.loads(event[0])["error"]


def test_source_recheck_refuses_other_active_lease_and_source_drift(setup_workflow):
    workflow, service = setup_workflow()
    run, old, _ = block_source(workflow, service, max_units=1)
    scanner = FreshScanner()
    workflow.source_repairer = scanner
    active = workflow.claim(run["run_id"])
    assert active["task_id"] != old["id"]
    with pytest.raises(ValueError):
        workflow.recheck_source_blocked(run["run_id"], "Active lease must finish first")
    workflow.fail(run["run_id"], active["task_id"], active["lease_id"], "Test provider stopped")
    service.bundle["fingerprint"] = "different-current-source"
    with pytest.raises(ValueError):
        workflow.recheck_source_blocked(run["run_id"], "Old source cannot be rechecked")
    assert scanner.calls == []


@pytest.mark.parametrize("state", ["ready", "published", "source_changed"])
def test_source_recheck_rejects_terminal_states_without_scanning(setup_workflow, state):
    workflow, service = setup_workflow()
    run = workflow.start(service.bundle["paper_id"], "source-recheck-terminal-" + state)
    if state != "source_changed":
        finish(workflow, run["run_id"])
        workflow.commit(run["run_id"], publish=state == "published")
    else:
        with workflow.connect() as con:
            con.execute("UPDATE runs SET state='source_changed'")
    scanner = FreshScanner()
    workflow.source_repairer = scanner
    with pytest.raises(ValueError):
        workflow.recheck_source_blocked(run["run_id"], "No terminal-state bypass")
    assert scanner.calls == []


def test_cli_explicit_source_recheck_resumes_without_auto_approval(interfaces, monkeypatch, capsys):
    workflow, service, _ = interfaces
    run, _, _ = block_source(workflow, service)
    scanner = FreshScanner()
    monkeypatch.setattr(cli, "SourceRepairer", lambda *_: scanner)
    calls = []
    actual = type(workflow).recheck_source_blocked

    def counted(instance, run_id, reason):
        calls.append((run_id, reason))
        return actual(instance, run_id, reason)

    monkeypatch.setattr(type(workflow), "recheck_source_blocked", counted)
    assert (
        cli.main(
            [
                service.bundle["paper_id"],
                "--experiment",
                "source-recheck-generate",
                "--recheck-source",
                "Auxiliary source evidence policy changed",
                "--publish",
            ]
        )
        == 0
    )
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert calls == [(run["run_id"], "Auxiliary source evidence policy changed")]
    rechecked = next(row for row in rows if row.get("event") == "source_rechecked")
    assert rechecked["counts"]["accepted"] == 0
    assert rows[-1]["papers"][0]["status"] == "published"


def test_cli_plan_only_source_recheck_is_rejected_before_execution(interfaces):
    _, service, _ = interfaces
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                service.bundle["paper_id"],
                "--experiment",
                "no-source-AI-plan",
                "--plan-only",
                "--recheck-source",
                "Requires actual independent scan",
            ]
        )
    assert exc.value.code == 2
