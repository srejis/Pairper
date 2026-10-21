"""Synthetic native archives; no model, user Library or real translation writes."""

import copy
import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from pairper.translation.contract import sha
from pairper.translation.math_contract import text_sha
from test_math_partition import complete_fixture
from test_translation_workflow import setup_workflow as setup_workflow

from pairper.translation.math_partition import (
    VERSION,
    original_partition_bundle,
    validate_partition_bundle,
)
from pairper.translation.partition_planning import (
    PartitionAwareSourceRepairer,
    PartitionPlanner,
    build_partition_prompt,
    select_partition_batches,
    validate_native_partition_binding,
)


class FakeNative:
    """Writes unmistakably synthetic transport records to exercise provenance gates."""

    def __init__(self):
        self.calls = []
        self.cancel_event = threading.Event()
        self.last_evidence = None

    def run(self, request, schema, images, directory):
        self.calls.append(copy.deepcopy(request))
        if request["partition_phase"] == "propose":
            response = {
                "version": VERSION,
                "source_bundle_fingerprint": request["source_bundle_fingerprint"],
                "parents": [
                    {
                        "math_id": p["math_id"],
                        "source_latex_sha256": p["source_latex_sha256"],
                        "segments": [
                            {"start": 0, "end": 1, "role": "math"},
                            {"start": 1, "end": 2, "role": "narrative_separator"},
                            {"start": 2, "end": 3, "role": "math"},
                        ],
                    }
                    for p in request["source_parents"]
                ],
                "assessments": [
                    {
                        "math_id": p["math_id"],
                        "decision": "partition",
                        "reason": "SYNTHETIC fake proposer reason withheld from reviewer",
                    }
                    for p in request["source_parents"]
                ],
            }
            if getattr(self, "keep_all", False):
                response["parents"] = []
                for assessment in response["assessments"]:
                    assessment["decision"] = "keep"
        else:
            assert "SYNTHETIC fake proposer reason" not in json.dumps(request)
            response = {
                "manifest_sha256": request["review_manifest_sha256"],
                "issues": [],
                "items": [
                    {
                        "math_id": p["math_id"],
                        "verdict": "pass",
                        "source_compared": True,
                        "narrative_separators_confirmed": True,
                        "checks": ["SYNTHETIC fake PDF review; not actual model evidence"],
                    }
                    for p in request["source_parents"]
                ],
            }
        if request["partition_phase"] == "review" and getattr(self, "reject_reviews", 0):
            self.reject_reviews -= 1
            response["items"][0]["verdict"] = "unresolved"
            response["items"][0]["checks"] = [
                "SYNTHETIC previously kept source role needs reassessment"
            ]
            response["issues"] = ["SYNTHETIC source-only batch review rejection"]
        call = Path(directory) / ("call-synthetic-" + str(len(self.calls)))
        attempt = call / "attempt-01"
        attempt.mkdir(parents=True)

        def write(p, x):
            p.write_text(json.dumps(x, ensure_ascii=False, indent=2), "utf8")

        prompt = build_partition_prompt(request)
        raw = json.dumps(response, ensure_ascii=False)
        write(call / "request.json", request)
        write(call / "response.json", response)
        write(call / "schema.json", schema)
        (call / "prompt.txt").write_text(prompt, "utf8")
        (attempt / "last-message.json").write_text(raw, "utf8")
        args = [
            "synthetic-NOT-native-model",
            "exec",
            "--ephemeral",
            "--output-schema",
            str((call / "schema.json").resolve()),
        ]
        for image in images:
            args.extend(["--image", str(Path(image).resolve())])
        write(
            attempt / "invocation.json",
            {
                "argv": args,
                "fresh_empty_cwd": "synthetic-empty",
                "stdin_prompt": str((call / "prompt.txt").resolve()),
                "shell": False,
            },
        )
        (attempt / "events.jsonl").write_text(
            json.dumps({"type": "turn.completed", "synthetic": True}) + "\n", "utf8"
        )
        evidence = {
            "directory": str(call),
            "status": "success",
            "synthetic_fixture": True,
            "source_request_sha256": hashlib.sha256(
                json.dumps(request, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest(),
            "provider_prompt_sha256": text_sha(prompt),
            "response_sha256": text_sha(raw),
            "attempts": [{"attempt": 1, "status": "success", "exit_code": 0}],
        }
        write(call / "evidence.json", evidence)
        self.last_evidence = evidence
        return response


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    import pairper.translation.partition_planning as planning

    b, _ = complete_fixture()
    b["units"][0]["pages"] = [1]
    # Complete fixture fingerprints remain exact after any synthetic mutations.
    b["units"][0]["fingerprint"] = sha(
        {k: v for k, v in b["units"][0].items() if k != "fingerprint"}
    )
    b["fingerprint"] = sha({k: v for k, v in b.items() if k != "fingerprint"})
    current = {"fingerprint": b["fingerprint"]}

    def guard(library, bundle):
        if current["fingerprint"] != bundle["fingerprint"]:
            raise ValueError("Source changed")

    monkeypatch.setattr(planning, "assert_current", guard)
    library = SimpleNamespace()
    runner = FakeNative()
    planner = PartitionPlanner(library, runner, tmp_path / "math-partitions")

    def images(bundle, batch, directory):
        path = directory / "original.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"SYNTHETIC image fixture, not original PDF")
        return [str(path)], [
            {"path": str(path), "page": 1, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        ]

    monkeypatch.setattr(planner, "_images", images)
    return b, planner, runner, current


def test_native_prepare_is_stable_source_only_blind_review_and_no_session_before_ready(prepared):
    b, planner, runner, current = prepared
    before = copy.deepcopy(b)
    derived, record = planner.prepare(b, "fresh-v2")
    assert len(runner.calls) == 2 and all(r["prior_Korean_inputs"] == 0 for r in runner.calls)
    assert (
        b == before
        and original_partition_bundle(derived) == b
        and validate_partition_bundle(derived) == b
    )
    assert validate_native_partition_binding(b, derived["math_partition"], planner.directory)
    second, again = planner.prepare(b, "fresh-v2")
    assert second == derived and again == record and len(runner.calls) == 2
    assert not list(planner.directory.rglob("translations.db"))
    current["fingerprint"] = "new-source"
    with pytest.raises(ValueError, match="Source changed"):
        planner.prepare(b, "fresh-v2")


@pytest.mark.parametrize(
    "filename",
    [
        "schema.json",
        "last-message.json",
        "invocation.json",
        "events.jsonl",
        "original.png",
        "plan.json",
    ],
)
def test_raw_native_or_plan_tampering_never_creates_a_cached_approval(prepared, filename):
    b, planner, runner, _ = prepared
    planner.prepare(b, "tamper")
    path = next(planner.directory.rglob(filename))
    if filename == "plan.json":
        record = json.loads(path.read_text("utf8"))
        record["native_calls"] = []
        path.write_text(json.dumps(record), "utf8")
    else:
        path.write_bytes(b"tampered synthetic record")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        planner.prepare(b, "tamper")
    assert len(runner.calls) == 2


def test_selector_accounts_for_every_batch_not_first_thirty():
    b, _ = complete_fixture()
    u = b["units"][0]
    u["math"] = [{"token": "⟦m" + str(i) + "⟧", "latex": "x,y", "refs": ["r"]} for i in range(65)]
    plan = select_partition_batches(b)
    assert [len(x) for x in plan["batches"]] == [30, 30, 5]
    assert plan["batches"][-1][-1]["math_id"] == "⟦m64⟧"
    assert plan["complete_source_inventory_accounted"] and plan["candidate_parent_count"] == 65


def test_wrapper_rebinds_alias_scope_without_mutating_fresh_candidate(prepared):
    b, planner, _, _ = prepared
    derived, _ = planner.prepare(b, "source-scope")
    observed = {}

    class Delegate:
        def scan(self, bundle, task, notes, images):
            observed.update(bundle=bundle, task=task, notes=notes)
            return []

    wrapper = PartitionAwareSourceRepairer(Delegate())
    alias = derived["units"][0]["math"][0]["token"]
    candidate = {"target": alias}
    task = {"units": derived["units"], "candidate_to_inspect": candidate}
    before = copy.deepcopy(task)
    wrapper.scan(derived, task, [alias], [])
    assert task == before and observed["bundle"] == b
    assert observed["task"]["candidate_to_inspect"] == candidate
    assert observed["task"]["units"] == b["units"]
    assert observed["notes"] == [b["units"][0]["math"][0]["token"]]


def test_v2_workflow_new_empty_session_manifest_bound_fresh_review_and_resume(
    prepared, setup_workflow, monkeypatch
):
    from test_translation_workflow import draft, engine, review, submit

    import pairper.translation.partition_planning as planning

    b, planner, runner, _ = prepared
    workflow, service = setup_workflow()
    service.bundle = copy.deepcopy(b)
    planner.directory = service.directory / "math-partitions"
    original_prepare = planner.prepare

    def prepared_bundle(bundle, experiment):
        derived, record = original_prepare(bundle, experiment)
        service.bundle = copy.deepcopy(derived)
        return derived, record

    monkeypatch.setattr(planner, "prepare", prepared_bundle)
    monkeypatch.setattr(planning, "PartitionPlanner", lambda *args: planner)
    monkeypatch.setattr(engine, "prepare_bundle", lambda *args: copy.deepcopy(b))
    run = workflow.start(b["paper_id"], "fresh-v2", math_partition_v2=True)
    with workflow.connect() as con:
        settings = json.loads(workflow._run(con, run["run_id"])["settings"])
        assert settings["math_partition_v2"] and settings["partition_manifest_sha256"]
    saved = service.store.read(run["session_id"], service.bundle["fingerprint"])
    assert saved["revision"] == 0 and not saved["result"]["entries"]
    claim = workflow.claim(run["run_id"])
    assert claim["request"]["prior_Korean_inputs"] == 0
    manifest = claim["request"]["source_math_partition"]
    aliases = [s["alias"] for s in manifest["parents"][0]["segments"]]
    response = draft(claim)
    response["items"][0]["target"] = "".join(aliases)
    submit(workflow, claim, response)
    with pytest.raises(ValueError):
        workflow.commit(run["run_id"])
    fresh = workflow.claim(run["run_id"])
    assert fresh["phase"] == "review"
    assert (
        fresh["request"]["source_math_partition"]["manifest_sha256"] == manifest["manifest_sha256"]
    )
    submit(workflow, fresh, review(fresh))
    workflow.commit(run["run_id"])
    resumed = workflow.start(b["paper_id"], "fresh-v2", math_partition_v2=True)
    assert resumed["run_id"] == run["run_id"] and len(runner.calls) == 2
    assert original_partition_bundle(service.bundle) == b


def test_optin_refuses_legacy_experiment_before_native_planning(
    prepared, setup_workflow, monkeypatch
):
    from test_translation_workflow import engine

    b, planner, runner, _ = prepared
    workflow, service = setup_workflow()
    service.bundle = copy.deepcopy(b)
    monkeypatch.setattr(engine, "prepare_bundle", lambda *args: copy.deepcopy(b))
    workflow.start(b["paper_id"], "legacy-budget-kept")
    with pytest.raises(ValueError, match="new explicit experiment"):
        workflow.start(b["paper_id"], "legacy-budget-kept", math_partition_v2=True)
    assert runner.calls == []


def test_service_current_and_display_use_original_source_and_strict_v2_projection(
    prepared, tmp_path, monkeypatch
):
    import pairper.document.presentation as presentation
    from pairper.translation.contract import Entry
    from pairper.translation.store import TranslationStore

    import pairper.translation.service as module
    from pairper.translation.service import TranslationService

    b, planner, _, _ = prepared
    directory = tmp_path / "store"
    planner.directory = directory / "math-partitions"
    derived, _ = planner.prepare(b, "service-v2")
    service = object.__new__(TranslationService)
    service.directory = directory
    service.library = SimpleNamespace()
    service.store = TranslationStore(directory)
    saved = service.store.begin(derived, "service-v2")
    monkeypatch.setattr(module, "assert_current", lambda *_: None)
    base = {
        "paper_id": b["paper_id"],
        "view_id": b["view_id"],
        "blocks": copy.deepcopy(b["document"]["blocks"]),
    }
    monkeypatch.setattr(presentation, "display_document", lambda *_: copy.deepcopy(base))
    assert service.current_bundle(saved["session_id"]) == derived
    u = derived["units"][0]
    entry = Entry(
        source_unit_ids=[u["id"]],
        source_fingerprint=sha([u["fingerprint"]]),
        action="translate",
        target=u["source_text"],
    )
    service.store.save(saved["session_id"], derived["fingerprint"], 0, entries=[entry.model_dump()])
    displayed = service.display(saved["session_id"])
    maths = [r for r in displayed["blocks"][0]["content"]["runs"] if r["kind"] == "math"]
    assert len(maths) == 3 and all(r["image_url"] is None for r in maths)
    assert all(r["partition_parent"]["source_refs"] == ["one-original-span"] for r in maths)
    base["blocks"][0]["content"]["runs"][0]["latex"] = "tampered-parent"
    with pytest.raises(ValueError, match="parent math differs"):
        service.display(saved["session_id"])


def test_cli_optin_is_preserved_on_every_source_restart(tmp_path, monkeypatch):
    from pairper.translation import cli

    calls = []

    class FakeWorkflow:
        def __init__(self, *args, **kwargs):
            pass

        def start(self, pid, experiment, **options):
            calls.append(options)
            return {"run_id": "fake-run", "status": "generating"}

        def step(self, run_id):
            return {"run_id": run_id, "status": "source_changed" if len(calls) == 1 else "ready"}

        def retry_failed(self, run_id, reason):
            return {"run_id": run_id, "status": "generating"}

        def commit(self, *args, **kwargs):
            return {"status": "ready"}

    monkeypatch.setattr(cli, "Library", lambda *_: SimpleNamespace())
    monkeypatch.setattr(cli, "TranslationService", lambda *_: SimpleNamespace(directory=tmp_path))
    monkeypatch.setattr(cli, "SourceRepairer", lambda *_: None)
    monkeypatch.setattr(cli, "Workflow", FakeWorkflow)
    monkeypatch.setattr(cli, "CodexRunner", lambda **_: SimpleNamespace())
    assert (
        cli.main(
            [
                "synthetic-paper",
                "--experiment",
                "fresh-v2",
                "--math-partition-v2",
                "--retry-failed",
                "SYNTHETIC infrastructure recovered",
                "--max-steps",
                "3",
            ]
        )
        == 0
    )
    assert len(calls) == 2 and all(c["math_partition_v2"] is True for c in calls)
    assert all(
        c["source_planning_retry_reason"] == "SYNTHETIC infrastructure recovered" for c in calls
    )


def test_native_binding_cache_is_source_manifest_and_all_artifact_stat_bound(prepared, monkeypatch):
    import pairper.translation.partition_planning as module

    b, planner, _, _ = prepared
    derived, _ = planner.prepare(b, "bounded-cold-cache")
    reviewed = derived["math_partition"]
    assert validate_native_partition_binding(b, reviewed, planner.directory)
    original = module._artifact
    calls = []

    def counted(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(module, "_artifact", counted)
    assert validate_native_partition_binding(b, reviewed, planner.directory) and not calls
    image = Path(reviewed["native_provenance"][0]["source_images"][0]["path"])
    image.write_bytes(b"changed source image")
    with pytest.raises(ValueError):
        validate_native_partition_binding(b, reviewed, planner.directory)


def test_partition_proposal_schema_and_prompt_bind_complete_assessments(prepared):
    import jsonschema

    from pairper.translation.partition_planning import (
        partition_response_schema,
        validate_partition_protocol,
    )

    b, planner, _, _ = prepared
    parents = select_partition_batches(b)["batches"][0]
    parents = [copy.deepcopy(parents[0]) for _ in range(3)]
    for index, parent in enumerate(parents):
        parent["math_id"] = f"synthetic-parent-{index}"
    request = planner._request(b, parents, [{"path": "SYNTHETIC-NOT-PDF"}])
    schema = partition_response_schema(request)
    response = {
        "version": VERSION,
        "source_bundle_fingerprint": b["fingerprint"],
        "parents": [],
        "assessments": [
            {"math_id": p["math_id"], "decision": "keep", "reason": "SYNTHETIC math punctuation"}
            for p in parents
        ],
    }
    validate_partition_protocol(request, response, image_count=1)
    prompt = build_partition_prompt(request)
    assert "parents=[]" in prompt and "keep or unresolved parent MUST NOT appear" in prompt
    assert schema["properties"]["assessments"]["minItems"] == 3
    assert schema["properties"]["assessments"]["maxItems"] == 3
    assert schema["properties"]["parents"]["items"]["properties"]["math_id"]["enum"] == [
        p["math_id"] for p in parents
    ]
    for change in ("missing", "extra", "foreign"):
        invalid = copy.deepcopy(response)
        if change == "missing":
            invalid["assessments"] = []
        elif change == "extra":
            invalid["assessments"].append(copy.deepcopy(invalid["assessments"][0]))
        else:
            invalid["assessments"][0]["math_id"] = "foreign"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid, schema)
    response["assessments"][1]["decision"] = "partition"
    proposed = {
        "math_id": parents[1]["math_id"],
        "source_latex_sha256": parents[1]["source_latex_sha256"],
        "segments": [
            {"start": 0, "end": 1, "role": "math"},
            {"start": 1, "end": 2, "role": "narrative_separator"},
        ],
    }
    response["parents"] = [proposed]
    validate_partition_protocol(request, response, image_count=1)
    for decision in ("keep", "unresolved"):
        invalid = copy.deepcopy(response)
        invalid["assessments"][1]["decision"] = decision
        with pytest.raises(ValueError, match="keep/unresolved IDs must be absent"):
            validate_partition_protocol(request, invalid, image_count=1)
    invalid = copy.deepcopy(response)
    invalid["parents"] = []
    with pytest.raises(ValueError, match="only decision=partition"):
        validate_partition_protocol(request, invalid, image_count=1)


def test_all_keep_proposal_still_requires_complete_blind_native_review(prepared):
    b, planner, runner, _ = prepared
    runner.keep_all = True
    derived, record = planner.prepare(b, "synthetic-all-keep")
    assert len(runner.calls) == 2
    assert runner.calls[1]["partition_phase"] == "review"
    assert runner.calls[1]["proposed_partition"] is None
    assert record["reviewed"]["manifest"]["parents"] == []
    assert original_partition_bundle(derived) == b
    assert validate_native_partition_binding(b, derived["math_partition"], planner.directory)


def test_existing_math_text_is_routed_without_approval_and_all_nodes_still_planned():
    from pairper.translation.math_contract import math_inventory

    from pairper.translation.planning import build_plan

    b, _ = complete_fixture()
    latex = r"\textbf{Input: }\text{Source description }M,"
    unit = b["units"][0]
    unit["math"][0]["latex"] = latex
    b["document"]["blocks"][0]["content"]["runs"][0]["latex"] = latex
    b["math_text_inventory"] = math_inventory(b)
    b["fingerprint"] = sha({k: v for k, v in b.items() if k != "fingerprint"})
    original = copy.deepcopy(b)
    selection = select_partition_batches(b)
    assert selection["total_inline_math_parents"] == 1
    assert selection["candidate_parent_count"] == 0 and selection["batches"] == []
    assert selection["not_selected_no_top_level_punctuation"] == []
    (route,) = selection["routed_to_existing_math_text_contract"]
    (inventory,) = b["math_text_inventory"]
    assert route["source_latex_sha256"] == text_sha(latex)
    assert route["source_math_text_inventory_sha256"] == sha(inventory)
    assert route["source_text_node_indices"] == [n["index"] for n in inventory["text_nodes"]]
    assert route["independent_math_review_required"] is True
    assert route["approval_granted"] is False
    tasks = build_plan(b)
    math_items = [item for task in tasks if task["kind"] == "math" for item in task["math_items"]]
    assert len(math_items) == 1
    assert math_items[0]["math_id"] == route["math_id"]
    assert math_items[0]["source_latex_sha256"] == route["source_latex_sha256"]
    assert [n["index"] for n in math_items[0]["text_nodes"]] == route["source_text_node_indices"]
    assert b == original
    for mutation in ("stale", "duplicate", "empty", "foreign", "duplicate_node"):
        invalid = copy.deepcopy(b)
        rows = invalid["math_text_inventory"]
        if mutation == "stale":
            rows[0]["source_latex_sha256"] = "not-current"
        elif mutation == "duplicate":
            rows.append(copy.deepcopy(rows[0]))
        elif mutation == "empty":
            rows[0]["text_nodes"] = []
        elif mutation == "foreign":
            rows[0]["math_id"] = "foreign"
        else:
            rows[0]["text_nodes"].append(copy.deepcopy(rows[0]["text_nodes"][0]))
        with pytest.raises(ValueError):
            select_partition_batches(invalid)


def test_math_text_route_is_distinct_from_no_punctuation_skip_and_symbolic_candidates():
    b, _ = complete_fixture()
    unit = b["units"][0]
    unit["math"] = [
        {"token": "⟦prose⟧", "latex": r"\text{Prose}", "refs": ["r0"]},
        {"token": "⟦symbol⟧", "latex": "x,y", "refs": ["r1"]},
        {"token": "⟦plain⟧", "latex": "x", "refs": ["r2"]},
    ]
    b["math_text_inventory"] = [
        {
            "math_id": "⟦prose⟧",
            "source_latex_sha256": text_sha(r"\text{Prose}"),
            "text_nodes": [{"index": 0, "text": "Prose", "classification": "review_needed"}],
        }
    ]
    selected = select_partition_batches(b)
    assert selected["total_inline_math_parents"] == 3
    assert selected["candidate_parent_count"] == 1
    assert selected["batches"][0][0]["math_id"] == "⟦symbol⟧"
    assert selected["not_selected_no_top_level_punctuation"] == ["⟦plain⟧"]
    assert [p["math_id"] for p in selected["routed_to_existing_math_text_contract"]] == ["⟦prose⟧"]


def test_failed_plan_retry_never_overwrites_any_previous_evidence_image(prepared, monkeypatch):
    b, planner, runner, _ = prepared
    original_run = runner.run

    def fail_after_archiving(*args, **kwargs):
        original_run(*args, **kwargs)
        raise ValueError("SYNTHETIC failure after native archive")

    monkeypatch.setattr(runner, "run", fail_after_archiving)
    with pytest.raises(ValueError, match="SYNTHETIC failure"):
        planner.prepare(b, "same-experiment-retry")
    first_images = list(planner.directory.rglob("original.png"))
    assert len(first_images) == 1
    old_files = {str(p): p.read_bytes() for p in planner.directory.rglob("*") if p.is_file()}
    first_images[0].write_bytes(b"SYNTHETIC immutable historical image")
    old_files[str(first_images[0])] = first_images[0].read_bytes()
    monkeypatch.setattr(runner, "run", original_run)
    with pytest.raises(ValueError, match="Incomplete reserved source round"):
        planner.prepare(b, "same-experiment-retry")
    images = list(planner.directory.rglob("original.png"))
    assert len(images) == 1
    assert all(Path(path).read_bytes() == data for path, data in old_files.items())
    assert len(list(planner.directory.glob("*/attempt-*/selection.json"))) == 2


def test_source_quality_feedback_reproposes_full_batch_and_new_reviewer_is_blind(prepared):
    b, planner, runner, _ = prepared
    runner.reject_reviews = 1
    derived, record = planner.prepare(b, "synthetic-feedback")
    assert len(runner.calls) == 4
    first, rejected, repair, fresh_review = runner.calls
    assert "source_proposal_feedback" not in first
    feedback = repair["source_proposal_feedback"]
    assert feedback["round_number"] == 2 and feedback["maximum_source_rounds"] == 3
    assert feedback["previous_independent_source_review"]["issues"]
    assert repair["source_parents"] == first["source_parents"] == fresh_review["source_parents"]
    assert "source_proposal_feedback" not in fresh_review
    assert "SYNTHETIC source-only batch review rejection" not in build_partition_prompt(
        fresh_review
    )
    assert len(record["native_calls"]) == 4
    assert len(derived["math_partition"]["native_provenance"]) == 4
    assert validate_native_partition_binding(b, derived["math_partition"], planner.directory)
    planner.prepare(b, "synthetic-feedback")
    assert len(runner.calls) == 4


def test_source_quality_cap_is_durable_across_restart_and_never_creates_korean(prepared):
    b, planner, runner, _ = prepared
    runner.reject_reviews = 99
    with pytest.raises(ValueError, match="quality budget exhausted"):
        planner.prepare(b, "synthetic-exhausted")
    assert len(runner.calls) == 6
    chain_path = next(planner.directory.rglob("chain.json"))
    chain = json.loads(chain_path.read_text("utf8"))
    assert len(chain["rounds"]) == 3 and all(r["status"] == "complete" for r in chain["rounds"])
    for _ in range(2):
        with pytest.raises(ValueError, match="quality budget exhausted"):
            planner.prepare(b, "synthetic-exhausted")
    assert len(runner.calls) == 6
    with pytest.raises(ValueError, match="quality budget exhausted"):
        planner.prepare(
            b, "synthetic-exhausted", retry_failed_reason="SYNTHETIC cannot bypass quality cap"
        )
    assert len(runner.calls) == 6
    assert not list(planner.directory.rglob("plan.json"))
    assert not list(planner.directory.rglob("translations.db"))
    chain_path.unlink()
    with pytest.raises(ValueError, match="ledger missing"):
        planner.prepare(b, "synthetic-exhausted")
    assert len(runner.calls) == 6


def test_chain_pass_cannot_be_forged_and_history_remains_immutable(prepared):
    b, planner, runner, _ = prepared
    runner.reject_reviews = 99
    with pytest.raises(ValueError, match="quality budget exhausted"):
        planner.prepare(b, "synthetic-chain-tamper")
    chain_path = next(planner.directory.rglob("chain.json"))
    chain = json.loads(chain_path.read_text("utf8"))
    chain["rounds"][0]["review_call"]["response"]["items"][0]["verdict"] = "pass"
    chain_path.write_text(json.dumps(chain), "utf8")
    with pytest.raises(ValueError, match="binding changed|differs"):
        planner.prepare(b, "synthetic-chain-tamper")
    assert len(runner.calls) == 6


@pytest.mark.parametrize("rejected", [False, True])
def test_legacy_actual_artifact_pair_is_automatically_adopted_without_rewriting(prepared, rejected):
    from pairper.translation.math_partition import normalize_partition_plan
    from pairper.translation.partition_planning import partition_response_schema

    b, planner, runner, _ = prepared
    selection = select_partition_batches(b)
    batch = selection["batches"][0]
    directory = planner.directory / sha([VERSION, b["fingerprint"], "legacy-adoption"])
    stage = directory / "attempt-legacy" / "batch-0000"
    stage.mkdir(parents=True)
    images, manifest = planner._images(b, batch, stage)
    request = planner._request(b, batch, manifest)
    response = runner.run(request, partition_response_schema(request), images, stage / "proposer")
    normalized = normalize_partition_plan(
        b, {k: response[k] for k in ("version", "source_bundle_fingerprint", "parents")}
    )
    review = copy.deepcopy(request)
    review.update(
        partition_phase="review",
        phase="review",
        proposed_partition=normalized,
        review_manifest_sha256=sha([b["fingerprint"], batch, normalized]),
    )
    runner.reject_reviews = int(rejected)
    runner.run(review, partition_response_schema(review), images, stage / "reviewer")
    old = {str(path): path.read_bytes() for path in stage.rglob("*") if path.is_file()}
    derived, record = planner.prepare(b, "legacy-adoption")
    assert len(runner.calls) == (4 if rejected else 2)
    assert len(record["native_calls"]) == len(runner.calls)
    assert all(Path(path).read_bytes() == content for path, content in old.items())
    if rejected:
        assert runner.calls[2]["source_proposal_feedback"]["round_number"] == 2
    assert validate_native_partition_binding(b, derived["math_partition"], planner.directory)


def test_explicit_native_execution_recovery_preserves_same_source_quality_slot(
    prepared, monkeypatch
):
    from pairper.translation.agent_runner import RunnerError

    b, planner, runner, _ = prepared
    original = runner.run

    def timeout(*args):
        original(*args)
        evidence = runner.last_evidence
        evidence["status"] = "failed"
        evidence["attempts"][0].update(status="failed", error_kind="timeout")
        directory = Path(evidence["directory"])
        (directory / "evidence.json").write_text(json.dumps(evidence), "utf8")
        raise RunnerError("SYNTHETIC native timeout", kind="timeout", evidence_dir=directory)

    monkeypatch.setattr(runner, "run", timeout)
    with pytest.raises(RunnerError, match="SYNTHETIC"):
        planner.prepare(b, "execution-recovery")
    old_calls = len(runner.calls)
    with pytest.raises(ValueError, match="explicit --retry-failed"):
        planner.prepare(b, "execution-recovery")
    assert len(runner.calls) == old_calls
    monkeypatch.setattr(runner, "run", original)
    planner.prepare(b, "execution-recovery", retry_failed_reason="SYNTHETIC service recovered")
    chain = json.loads(next(planner.directory.rglob("chain.json")).read_text("utf8"))
    assert len(chain["rounds"]) == 1 and chain["rounds"][0]["number"] == 1
    assert chain["rounds"][0]["status"] == "complete"
    assert len(chain["rounds"][0]["execution_failures"]) == 1
    assert chain["rounds"][0]["execution_recoveries"][0]["reason"] == "SYNTHETIC service recovered"
    assert len(runner.calls) == old_calls + 2
