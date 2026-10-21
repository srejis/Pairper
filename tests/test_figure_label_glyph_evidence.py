"""Neutral original-native observations; no AI/visual-completeness approvals."""
import copy
import hashlib
import json

import pymupdf
import pytest
from pairper.pdf.source import extract

from pairper.translation.prompts import build_prompt, project_request
from pairper.translation.source_figure_glyph_evidence import build_figure_label_glyph_evidence


@pytest.fixture
def source_case(tmp_path):
    pdf = tmp_path / "original.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=250, height=150)
        page.insert_text((40, 70), "L2 Norm", fontsize=8)
        page.insert_text((40, 90), "Hidden", fontsize=8, render_mode=3)
        document.save(pdf)
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    snapshot = extract(pdf, digest)
    spans = snapshot["pages"][0]["spans"]
    figure = {"paper_id": "paper", "block_id": "f", "region": {"page": 1, "bbox": [20, 20, 200, 120], "rotation": 0}}
    document = {"paper_id": "paper", "source_sha256": digest, "blocks": [
        {"id": "f", "type": "figure", "source_refs": [s["id"] for s in spans], "content": {"region": figure["region"]}}]}
    snapshot_path = tmp_path / "snapshot.json"
    view = tmp_path / "view.json"
    snapshot_path.write_text(json.dumps(snapshot), "utf-8")
    view.write_text(json.dumps(document), "utf-8")
    bundle = {"paper_id": "paper", "fingerprint": "current", "view_id": "view", "document": document,
        "source_pdf_sha256": digest, "source_snapshot_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        "view_sha256": hashlib.sha256(view.read_bytes()).hexdigest(), "figures": [figure],
        "figure_source_spans": {"f": [{**s, "page": 1} for s in spans]}}
    task = {"paper_id": "paper", "bundle_fingerprint": "current", "figure": copy.deepcopy(figure)}
    candidate = {"figures": [{"block_id": "f", "labels": [
        {"source": s["text"], "target": "SECRET_KOREAN_ANSWER", "reason": "SECRET_VERDICT",
         "source_refs": [s["id"]], "bbox": []} for s in spans]}]}
    return bundle, task, candidate, {"pdf_path": pdf, "source_snapshot_path": snapshot_path, "view_path": view}


def call(case, **limits):
    b, t, c, paths = case
    return build_figure_label_glyph_evidence(b, t, c, **paths, **limits)


def test_native_refs_trace_order_and_invisible_text_never_grant_approval(source_case):
    before = copy.deepcopy(source_case[:3])
    result = call(source_case)
    assert result["status"] == "available" and result["source_only"]
    assert result["labels"][0]["refs"][0]["native_pdf_text"] == "L2 Norm"
    trace_id = result["labels"][0]["refs"][0]["traces"][0]['trace_id']
    trace = next(t for t in result['native_traces'] if t['trace_id'] == trace_id)
    assert [c[1] for c in trace['chars']] == list(map(ord, "L2 Norm"))
    hidden_id = result["labels"][1]["refs"][0]["traces"][0]['trace_id']
    hidden = next(t for t in result['native_traces'] if t['trace_id'] == hidden_id)
    assert hidden["type"] == 3
    assert not result["full_visual_visibility_verified"] and not result["translation_or_spatial_approval_granted"]
    assert "SECRET" not in json.dumps(result)
    assert before == source_case[:3]
    assert call(source_case) == result
    for phase, kind in [("generate", "figure"), ("review", "figure"), ("repair", "figure"), ("source_scan", "math")]:
        request = {"phase": phase, "kind": kind, "task": {}, "figure_label_glyph_evidence": result}
        assert project_request(request)["figure_label_glyph_evidence"] == result
        assert "do NOT prove complete visual visibility" in build_prompt(request)


@pytest.mark.parametrize("attack", ["pdf", "snapshot", "view", "bundle", "unknown", "foreign", "bbox", "page"])
def test_identity_membership_and_current_snapshot_guards(source_case, attack):
    b, t, c, paths = source_case
    if attack in {"pdf", "snapshot", "view"}:
        key = {"pdf": "pdf_path", "snapshot": "source_snapshot_path", "view": "view_path"}[attack]
        paths[key].write_bytes(paths[key].read_bytes() + b"changed")
    elif attack == "bundle":
        t["bundle_fingerprint"] = "stale"
    elif attack == "unknown":
        c["figures"][0]["labels"][0]["source_refs"] = ["foreign"]
    elif attack == "foreign":
        b["document"]["blocks"][0]["source_refs"] = []
    elif attack == "bbox":
        b["figure_source_spans"]["f"][0]["bbox"] = [40, 40, 100, 100]
    else:
        b["figure_source_spans"]["f"][0]["page"] = 2
    with pytest.raises(ValueError):
        call(source_case)


def test_generation_has_source_ref_selectors_without_candidate_text(source_case):
    b, t, _, paths = source_case
    result = build_figure_label_glyph_evidence(b, t, None, **paths)
    assert result["selection_kind"] == "current_figure_original_refs_not_candidate_labels"
    assert all("@source_ref" in x["label_id"] for x in result["labels"])


def test_character_and_byte_caps_are_explicit(source_case):
    result = call(source_case, max_chars=1, max_output_bytes=2048)
    assert result["truncated"]
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) <= 2048
    assert not result["translation_or_spatial_approval_granted"]


def test_label_and_ref_caps_retain_explicit_unavailability(source_case):
    result = call(source_case, max_labels=1, max_refs=1)
    assert result["omissions"][0]["reason"] == "label_cap"
    assert result["truncated"]



def test_generation_owned_refs_still_need_positive_current_figure_intersection(source_case):
    b, t, _, paths = source_case
    region = {"page": 1, "bbox": [150, 100, 200, 120], "rotation": 0}
    b['figures'][0]['region'] = copy.deepcopy(region)
    b['document']['blocks'][0]['content']['region'] = copy.deepcopy(region)
    t['figure']['region'] = copy.deepcopy(region)
    paths['view_path'].write_text(json.dumps(b['document']), 'utf-8')
    b['view_sha256'] = hashlib.sha256(paths['view_path'].read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="fully outside"):
        build_figure_label_glyph_evidence(b, t, None, **paths)


def test_bbox_only_label_remains_explicitly_unavailable_not_native_text_proof(source_case):
    source_case[2]['figures'][0]['labels'] = [{'source_refs': [], 'bbox': [40, 50, 80, 80]}]
    result = call(source_case)
    assert result['status'] == 'unavailable'
    assert result['labels'][0]['unavailable']
    assert not result['translation_or_spatial_approval_granted']
