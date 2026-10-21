"""An evidence-version upgrade reopens review, never grants figure approval."""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from test_translation_figure_anchors import answer, figure_claim, use_original_figure
from test_translation_workflow import engine, review, sha, submit
from test_translation_workflow import setup_workflow as setup_workflow


def blocked_legacy_review(workflow, service, *, old_version=None, repairs=1):
    use_original_figure(service, isolated=True)
    run = workflow.start(service.bundle["paper_id"], "figure-review-evidence-upgrade")
    generation = figure_claim(workflow, run["run_id"])
    response = answer(generation, isolated=True)
    submit(workflow, generation, response)
    for _ in range(6):
        claim = workflow.claim(run["run_id"])
        if claim["request"]["kind"] == "figure":
            break
        submit(workflow, claim, review(claim))
    assert claim["phase"] == "review" and claim["task_id"] == generation["task_id"]
    verdict = review(claim, "unresolved")
    submit(workflow, claim, verdict)
    feedback = {
        "bundle_fingerprint": service.bundle["fingerprint"],
        "task_id": claim["task_id"],
        "notes": ["OLD_SCANNER_VERDICT_MUST_NOT_REACH_FRESH_REVIEW"],
    }
    with workflow.connect() as con:
        event = con.execute(
            "SELECT * FROM events WHERE task_id=? AND event='claimed' ORDER BY seq DESC LIMIT 1",
            (claim["task_id"],),
        ).fetchone()
        payload = json.loads(event["payload"])
        payload["request"].pop("figure_review_evidence_version", None)
        if old_version is not None:
            payload["request"]["figure_review_evidence_version"] = old_version
        old_hash = workflow._review_hash(payload["request"])
        con.execute(
            "UPDATE events SET payload=?,input_hash=? WHERE seq=?",
            (json.dumps(payload), old_hash, event["seq"]),
        )
        con.execute(
            "UPDATE tasks SET repair_count=?,review_input_hash=?,failures=2,source_feedback=? WHERE id=?",
            (repairs, old_hash, json.dumps(feedback), claim["task_id"]),
        )
        row = dict(con.execute("SELECT * FROM tasks WHERE id=?", (claim["task_id"],)).fetchone())
    return run, claim, row, feedback


@pytest.mark.parametrize("old_version", [None, "previous-visual-input-v0", "actual-output-and-local-crops-v1", "actual-output-and-lossless-label-atlas-v2"])
def test_legacy_unresolved_reopens_once_with_candidate_and_source_preserved(
    setup_workflow, old_version
):
    workflow, service = setup_workflow(full=True, images=True)
    run, claim, before, feedback = blocked_legacy_review(workflow, service, old_version=old_version)
    source_before = copy.deepcopy(service.bundle)
    result = workflow.start(service.bundle["paper_id"], "figure-review-evidence-upgrade")
    assert result["run_id"] == run["run_id"] and result["counts"]["blocked"] == 0
    with workflow.connect() as con:
        row = dict(con.execute("SELECT * FROM tasks WHERE id=?", (claim["task_id"],)).fetchone())
        events = [
            dict(e)
            for e in con.execute(
                "SELECT * FROM events WHERE event='visual_evidence_recheck_requested'"
            )
        ]
    assert len(events) == 1
    preserved = [
        "candidate",
        "packet",
        "repair_count",
        "failures",
        "source_feedback",
        "last_response",
    ]
    assert all(row[key] == before[key] for key in preserved)
    assert row["status"] == "drafted" and row["review"] is None and row["review_input_hash"] == ""
    payload = json.loads(events[0]["payload"])
    assert json.loads(before["review"]) in payload.values()
    assert before["review_input_hash"] in payload.values()
    assert sha(json.loads(before["candidate"])) in payload.values()
    assert service.bundle == source_before
    assert service.store.read(run["session_id"], service.bundle["fingerprint"])["revision"] == 0
    with pytest.raises(ValueError):
        workflow.commit(run["run_id"])
    fresh = workflow.claim(run["run_id"])
    assert fresh["task_id"] == claim["task_id"] and fresh["phase"] == "review"
    request = fresh["request"]
    assert request["figure_review_evidence_version"] == engine.FIGURE_REVIEW_EVIDENCE_VERSION
    assert request["candidate"] == json.loads(before["candidate"])
    assert request["figure_output_evidence"]["projection_available"]
    assert request["figure_output_evidence"]["translation_approval_granted"] is False
    assert "source_verification_feedback" not in request
    assert feedback["notes"][0] not in json.dumps(request)
    assert request["issues"] == [] and request["structural_error"] == ""
    submit(workflow, fresh, review(fresh, "unresolved"))
    assert (
        workflow.start(service.bundle["paper_id"], "figure-review-evidence-upgrade")["counts"][
            "blocked"
        ]
        == 1
    )
    with workflow.connect() as con:
        assert (
            con.execute(
                "SELECT count(*) FROM events WHERE event='visual_evidence_recheck_requested'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "guard",
    [
        "same_version",
        "repair_limit",
        "source_block",
        "provider_failure",
        "published",
        "ready",
        "source_changed",
    ],
)
def test_upgrade_cannot_bypass_current_quality_source_or_terminal_guards(setup_workflow, guard):
    workflow, service = setup_workflow(full=True, images=True)
    version = engine.FIGURE_REVIEW_EVIDENCE_VERSION if guard == "same_version" else None
    run, claim, _, _ = blocked_legacy_review(
        workflow,
        service,
        old_version=version,
        repairs=workflow.max_repairs if guard == "repair_limit" else 1,
    )
    with workflow.connect() as con:
        if guard in ["source_block", "provider_failure"]:
            workflow._event(
                con,
                run["run_id"],
                claim["task_id"],
                "review",
                "source_repair_blocked" if guard == "source_block" else "provider_failed",
                {"error": "Different blocker must remain blocked"},
            )
        if guard in ["published", "ready", "source_changed"]:
            con.execute("UPDATE runs SET state=? WHERE id=?", (guard, run["run_id"]))
        before = dict(con.execute("SELECT * FROM tasks WHERE id=?", (claim["task_id"],)).fetchone())
        count = con.execute("SELECT count(*) FROM events").fetchone()[0]
    workflow.start(service.bundle["paper_id"], "figure-review-evidence-upgrade")
    with workflow.connect() as con:
        assert (
            dict(con.execute("SELECT * FROM tasks WHERE id=?", (claim["task_id"],)).fetchone())
            == before
        )
        assert con.execute("SELECT count(*) FROM events").fetchone()[0] == count


def test_review_hash_binds_version_and_actual_projection_but_not_render_transport(setup_workflow):
    workflow, _ = setup_workflow()
    request = {
        "kind": "figure",
        "candidate": {"figures": []},
        "figure_review_evidence_version": "version-1",
        "figure_output_evidence": {"emitted_translation_labels": []},
    }
    baseline = workflow._review_hash(request)
    transported = {
        **copy.deepcopy(request),
        "figure_label_evidence": {"paths": ["local-render.png"]},
        "attached_images": [{"path": "other-location"}],
    }
    assert workflow._review_hash(transported) == baseline
    transported["figure_review_evidence_version"] = "version-2"
    assert workflow._review_hash(transported) != baseline
    transported["figure_review_evidence_version"] = "version-1"
    transported["figure_output_evidence"]["emitted_translation_labels"] = [
        {"source": "Axis", "target": "Cost"}
    ]
    assert workflow._review_hash(transported) != baseline


def test_fresh_figure_review_gets_real_adaptive_crops_and_projection(setup_workflow, monkeypatch):
    import pymupdf
    from pairper.pdf.source import extract

    workflow, service = setup_workflow(full=True, images=True)
    root = service.directory / "synthetic-original-library"
    (root / "sources").mkdir(parents=True)
    pdf = root / "original.pdf"
    document = pymupdf.open()
    page = document.new_page(width=250, height=150)
    page.insert_text((40, 70.6), "12500", fontsize=0.62384)
    document.save(pdf)
    document.close()
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    source = extract(pdf, digest)
    span = source["pages"][0]["spans"][0]
    block_id = "synthetic-tiny-axis"
    region = {"page": 1, "bbox": [35, 65, 50, 75], "rotation": 0}
    with pymupdf.open(pdf) as original:
        data = (
            original[0]
            .get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=pymupdf.Rect(region["bbox"]))
            .tobytes("png")
        )
    crop = root / "native.png"
    crop.write_bytes(data)
    bundle = service.bundle
    figure = {
        "paper_id": bundle["paper_id"],
        "block_id": block_id,
        "region": region,
        "crop": str(crop),
        "crop_sha256": hashlib.sha256(data).hexdigest(),
    }
    bundle["units"] = []
    bundle["math_text_inventory"] = []
    bundle["figures"] = [figure]
    bundle["figure_source_spans"] = {block_id: [{**span, "page": 1}]}
    bundle["document"]["blocks"] = [
        {
            "id": block_id,
            "type": "figure",
            "order": 0,
            "source_refs": [span["id"]],
            "content": {"region": region},
        }
    ]
    bundle["document"]["source_sha256"] = digest
    bundle["source_pdf_sha256"] = digest
    bundle["fingerprint"] = sha({k: v for k, v in bundle.items() if k != "fingerprint"})
    (root / "sources" / (bundle["paper_id"] + ".json")).write_text(
        json.dumps(source), encoding="utf-8"
    )
    (root / "views").mkdir()
    current_view = root / "views" / (bundle["view_id"] + ".json")
    current_view.write_text(json.dumps(bundle["document"]), encoding="utf-8")
    bundle["view_sha256"] = hashlib.sha256(current_view.read_bytes()).hexdigest()
    bundle["source_snapshot_sha256"] = hashlib.sha256(
        (root / "sources" / (bundle["paper_id"] + ".json")).read_bytes()).hexdigest()
    bundle["fingerprint"] = sha({k: v for k, v in bundle.items() if k != "fingerprint"})
    service.library = SimpleNamespace(root=root, paper=lambda _: {"original_path": "original.pdf"})
    monkeypatch.setattr(workflow, "_with_images", engine.Workflow._with_images.__get__(workflow))
    run = workflow.start(bundle["paper_id"], "real-local-review-input")
    claim = workflow.claim(run["run_id"])
    candidate = {
        "figures": [
            {
                "block_id": block_id,
                "image_inspected": True,
                "labels_complete": True,
                "unresolved_errors": [],
                "no_translatable_text_reason": "",
                "labels": [
                    {"source": "12500", "target": "12500", "source_refs": [span["id"]], "bbox": []}
                ],
            }
        ],
        "issues": [],
        "notes": [],
        "source_concerns": [],
    }
    submit(workflow, claim, candidate)
    before_pdf = pdf.read_bytes()
    fresh = workflow.claim(run["run_id"])
    assert fresh["phase"] == "review"
    request = fresh["request"]
    assert request["figure_review_evidence_version"] == engine.FIGURE_REVIEW_EVIDENCE_VERSION
    assert request["figure_output_evidence"]["emitted_translation_labels"] == []
    assert request["figure_label_evidence"]["selected_labels"] == 1
    assert request['figure_label_glyph_evidence']['labels'][0]['refs'][0]['native_pdf_text'] == '12500'
    assert not request['figure_label_glyph_evidence']['full_visual_visibility_verified']
    assert len(fresh["images"]) >= 3  # Native, whole page and the actual adaptive local crop.
    adaptive = [path for path in fresh["images"] if "label" in str(path)]
    assert adaptive
    image = pymupdf.Pixmap(str(adaptive[0]))
    assert image.width > 100 and image.height > 100
    assert image.width <= 2048 and image.height <= 2048 and min(image.samples) < 255
    assert pdf.read_bytes() == before_pdf
    assert "source_verification_feedback" not in request and "candidate_to_inspect" not in request
    assert service.store.read(run["session_id"], bundle["fingerprint"])["revision"] == 0
