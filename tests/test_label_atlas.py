"""Lossless original-pixel packaging, not AI review or glyph reconstruction."""
import copy
import hashlib

import pymupdf
import pytest
from pairper.pdf.source import extract

from pairper.translation.source_figure_evidence import figure_label_regions, pack_label_evidence


@pytest.fixture
def original(tmp_path):
    pdf = tmp_path / "original.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=250, height=150)
        page.insert_text((40, 70.6), "12500", fontsize=0.62384)
        document.save(pdf)
    source = extract(pdf, hashlib.sha256(pdf.read_bytes()).hexdigest())
    span = source["pages"][0]["spans"][0]
    figure = {"block_id": "figure", "region": {"page": 1, "bbox": [10, 10, 210, 110], "rotation": 0}}
    bundle = {"paper_id": "paper", "fingerprint": "current", "figures": [figure],
              "figure_source_spans": {"figure": [{**span, "page": 1}]}}
    task = {"paper_id": "paper", "bundle_fingerprint": "current", "figure": copy.deepcopy(figure)}
    candidate = {"figures": [{"block_id": "figure", "labels": [
        {"source": "12500", "target": "12500", "source_refs": [span["id"]], "bbox": []}
        for _ in range(37)]}]}
    return pdf, source, bundle, task, candidate


def test_37_real_original_crops_have_lossless_deterministic_atlas_coverage(original, tmp_path):
    pdf, source, bundle, task, candidate = original
    before = copy.deepcopy((source, bundle, task, candidate))
    region_result = figure_label_regions(bundle, task, candidate, source_snapshot=source)
    assert len(region_result["regions"]) == 37 and not region_result["manifest"]["omissions"]
    regions = region_result["regions"]
    first = pack_label_evidence(pdf, source, regions, tmp_path / "first", base_image_count=2)
    second = pack_label_evidence(pdf, source, regions, tmp_path / "second", base_image_count=2)
    manifest = first["manifest"]
    assert len(first["images"]) + 2 <= 32 and len(first["images"]) < 37
    assert manifest["selected_labels"] == 37 and not manifest["omissions"]
    assert [x["sha256"] for x in manifest["atlas_images"]] == [x["sha256"] for x in second["manifest"]["atlas_images"]]
    assert len({x["header_label_id"] for x in manifest["rendered_images"]}) == 37
    assert {x["label_id"] for x in manifest["rendered_images"]} == {x["label_id"] for x in regions}
    for image in manifest["atlas_images"]:
        pix = pymupdf.Pixmap(image["path"])
        assert pix.width <= 2048 and pix.height <= 2048 and pix.width * pix.height <= 4_000_000
        selected = [r for r in manifest["rendered_images"] if r["atlas_id"] == image["atlas_id"]]
        for item in selected:
            assert item["pixel_identity_verified"] and not item["pixel_resampling"]
            assert item["crop_pixel_sha256"] == item["atlas_crop_pixel_sha256"]
            assert item["region"] == regions[int(item["header_label_id"][1:])]["region"]
            assert item["atlas_pixel_bbox"][1] == item["header_pixel_bbox"][3]
            for other in selected:
                if other is item:
                    continue
                a, b = item["atlas_pixel_bbox"], other["atlas_pixel_bbox"]
                assert min(a[2], b[2]) <= max(a[0], b[0]) or min(a[3], b[3]) <= max(a[1], b[1])
    assert before == (source, bundle, task, candidate)
    assert hashlib.sha256(pdf.read_bytes()).hexdigest() == source["sha256"]


def test_attachment_exhaustion_is_explicit_and_never_discards_label_ids(original, tmp_path):
    pdf, source, bundle, task, candidate = original
    regions = figure_label_regions(bundle, task, candidate, source_snapshot=source)["regions"]
    packed = pack_label_evidence(pdf, source, regions, tmp_path / "none", base_image_count=32)
    assert packed["images"] == [] and packed["manifest"]["selected_labels"] == 0
    assert len(packed["manifest"]["omissions"]) == 37
    assert {x["reason"] for x in packed["manifest"]["omissions"]} == {"total_attachment_budget"}


@pytest.mark.parametrize("base,total", [(33, 32), (-1, 32), (0, 33), (True, 32), (0, 0)])
def test_attachment_budget_is_bounded(original, tmp_path, base, total):
    with pytest.raises(ValueError, match="budget"):
        pack_label_evidence(original[0], original[1], [], tmp_path, base_image_count=base, max_images=total)


def test_input_bound_and_duplicate_ids_are_not_hidden_by_image_budget(original, tmp_path):
    pdf, source, bundle, task, candidate = original
    labels = candidate["figures"][0]["labels"]
    labels[:] = [copy.deepcopy(labels[0]) for _ in range(257)]
    with pytest.raises(ValueError, match="256"):
        figure_label_regions(bundle, task, candidate, source_snapshot=source)
    with pytest.raises(ValueError, match="distinct"):
        pack_label_evidence(pdf, source, [{"label_id": "same"}] * 2, tmp_path, base_image_count=32)


def test_unrepresentable_crop_is_explicit_without_resampling(original, tmp_path, monkeypatch):
    import pairper.translation.source_figure_evidence as module
    pix = pymupdf.Pixmap(pymupdf.csRGB, 20, 2040, bytes([255]) * 20 * 2040 * 3, False)
    monkeypatch.setattr(module, "render_label_evidence", lambda *_: pix.tobytes("png"))
    packed = pack_label_evidence(original[0], original[1], [{"label_id": "large"}], tmp_path)
    assert packed["images"] == []
    assert packed["manifest"]["omissions"] == [{"label_id": "large", "reason": "atlas_header_or_pixel_budget"}]



def test_full_sheet_exhaustion_reports_every_unpacked_label_without_scaling(original, tmp_path, monkeypatch):
    import pairper.translation.source_figure_evidence as module
    pix = pymupdf.Pixmap(pymupdf.csRGB, 1000, 1000, bytes([255]) * 1000 * 1000 * 3, False)
    monkeypatch.setattr(module, "render_label_evidence", lambda *_: pix.tobytes("png"))
    items = [{"label_id": f"label{i}"} for i in range(5)]
    packed = pack_label_evidence(original[0], original[1], items, tmp_path, base_image_count=31)
    assert len(packed["images"]) == 1
    rendered = packed["manifest"]["rendered_images"]
    omitted = packed["manifest"]["omitted_label_ids"]
    assert len(rendered) == 2 and len(omitted) == 3
    assert {x["label_id"] for x in rendered} | set(omitted) == {x["label_id"] for x in items}
    assert all(x["atlas_pixel_bbox"][2] - x["atlas_pixel_bbox"][0] == 1000 for x in rendered)
    assert all(x["pixel_identity_verified"] for x in rendered)


def test_tampered_atlas_cache_is_rejected(original, tmp_path):
    from pathlib import Path
    pdf, source, bundle, task, candidate = original
    regions = figure_label_regions(bundle, task, candidate, source_snapshot=source)["regions"][:1]
    packed = pack_label_evidence(pdf, source, regions, tmp_path)
    Path(packed["images"][0]).write_bytes(b"tampered evidence")
    with pytest.raises(ValueError, match="cache changed"):
        pack_label_evidence(pdf, source, regions, tmp_path)


def test_atlas_provenance_reaches_fresh_review_and_source_scan_prompt():
    from pairper.translation.prompts import build_prompt, project_request
    evidence = {"rendered_images": [{"label_id": "figure@label36", "header_label_id": "L036",
        "atlas_pixel_bbox": [0, 16, 80, 50], "atlas_id": "atlas0", "atlas_sha256": "actualhash"}]}
    for phase, kind in [("review", "figure"), ("repair", "figure"), ("source_scan", "math")]:
        request = {"phase": phase, "kind": kind, "task": {}, "figure_label_evidence": evidence,
                   "original_page_images": [{"path": "atlas.png", "atlas_id": "atlas0", "sha256": "actualhash"}],
                   "attached_images": [{"path": "atlas.png", "sha256": "actualhash"}], "review_items": []}
        projected = project_request(request)
        assert projected["figure_label_evidence"] == evidence
        assert projected["attached_images"][0]["role"] == "original_figure_label_atlas"
        prompt = build_prompt(request)
        assert "L036" in prompt and "never reconstructs absent glyphs" in prompt
