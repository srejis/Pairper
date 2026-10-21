"""Local original-PDF evidence boundaries; no AI, approvals or library writes."""

import copy
import math

import pytest

from pairper.translation.source_figure_evidence import figure_label_regions, render_label_evidence


@pytest.fixture
def case():
    spans = [
        {
            "id": "tick",
            "text": "12500",
            "bbox": [40, 70, 42, 70.7268],
            "size": 0.62384,
            "font": "Helvetica",
            "page": 1,
        },
        {
            "id": "axis",
            "text": "Total cost",
            "bbox": [60, 20, 90, 29],
            "size": 8,
            "font": "Helvetica",
            "page": 1,
        },
        {
            "id": "axis2",
            "text": "(lbs)",
            "bbox": [91, 20, 108, 29],
            "size": 8,
            "font": "Helvetica",
            "page": 1,
        },
    ]
    figure = {
        "paper_id": "paper",
        "block_id": "fig",
        "region": {"page": 1, "bbox": [10, 10, 210, 110], "rotation": 0},
        "crop_sha256": "native-image",
    }
    bundle = {
        "paper_id": "paper",
        "fingerprint": "current",
        "figures": [figure],
        "figure_source_spans": {"fig": spans},
    }
    task = {"paper_id": "paper", "bundle_fingerprint": "current", "figure": copy.deepcopy(figure)}
    candidate = {
        "figures": [
            {
                "block_id": "fig",
                "labels": [
                    {"source": "12500", "target": "12500", "source_refs": ["tick"], "bbox": []},
                    {
                        "source": "Total cost (lbs)",
                        "target": "Total cost (lbs)",
                        "source_refs": ["axis", "axis2"],
                        "bbox": [],
                    },
                ],
            }
        ]
    }
    snapshot = {
        "pages": [
            {
                "page": 1,
                "spans": copy.deepcopy(
                    [{k: v for k, v in span.items() if k != "page"} for span in spans]
                ),
            }
        ]
    }
    return bundle, task, candidate, snapshot


def regions(case, **kwargs):
    b, t, c, s = case
    return figure_label_regions(b, t, c, source_snapshot=s, **kwargs)


def test_sub_point_tick_gets_local_adaptive_crop_not_whole_figure(case):
    before = copy.deepcopy(case)
    result = regions(case)
    tick, axis = result["regions"]
    assert tick["label_id"] == "fig@label0" and 32 <= tick["scale"] <= 40
    assert tick["region"]["bbox"] == [38, 68, 44, 72.7268]
    assert tick["source_ref_font_size"] == 0.62384
    assert tick["estimated_smallest_glyph_pixels"] == pytest.approx(24)
    assert not tick["resolution_limited"]
    assert axis["scale"] == 4 and axis["source_refs"] == ["axis", "axis2"]
    assert axis["region"]["bbox"] == [58, 18, 110, 31]
    assert result["manifest"]["snapshot_checked"]
    assert not result["manifest"]["semantic_review_performed"]
    assert not result["manifest"]["rendered_images_verified"]
    assert case == before


def test_local_render_has_enough_pixels_and_valid_bounds(case, tmp_path):
    import pymupdf

    document = pymupdf.open()
    page = document.new_page(width=250, height=150)
    page.insert_text((40, 70.6), "12500", fontsize=0.62384)
    pdf = tmp_path / "original.pdf"
    document.save(pdf)
    document.close()
    item = regions(case)["regions"][0]
    with pymupdf.open(pdf) as source:
        image = source[0].get_pixmap(
            matrix=pymupdf.Matrix(item["scale"], item["scale"]),
            clip=pymupdf.Rect(item["region"]["bbox"]),
        )
    assert image.width > 200 and image.height > 150
    assert image.width <= 2048 and image.height <= 2048
    assert image.width * image.height <= 4_000_000
    assert min(image.samples) < 255  # Actual original glyph pixels are included.


@pytest.mark.parametrize(
    "attack",
    [
        "foreign_paper",
        "stale_bundle",
        "unknown_figure",
        "changed_region",
        "changed_crop",
        "foreign_ref",
        "cross_page_ref",
        "outside_ref",
        "outside_bbox",
        "invalid_bbox",
        "snapshot_bbox_drift",
        "snapshot_text_drift",
        "snapshot_font_drift",
        "snapshot_ref_missing",
        "snapshot_page_drift",
        "duplicate_ref",
    ],
)
def test_foreign_or_drifting_source_cannot_be_presented_as_evidence(case, attack):
    b, t, c, s = case
    label = c["figures"][0]["labels"][0]
    if attack == "foreign_paper":
        t["paper_id"] = "foreign"
    if attack == "stale_bundle":
        t["bundle_fingerprint"] = "old"
    if attack == "unknown_figure":
        t["figure"]["block_id"] = "foreign"
    if attack == "changed_region":
        t["figure"]["region"]["bbox"][0] = 11
    if attack == "changed_crop":
        t["figure"]["crop_sha256"] = "old"
    if attack == "foreign_ref":
        label["source_refs"] = ["not-in-current-figure"]
    if attack == "cross_page_ref":
        b["figure_source_spans"]["fig"][0]["page"] = 2
    if attack == "outside_ref":
        b["figure_source_spans"]["fig"][0]["bbox"] = [0, 70, 9, 71]
    if attack == "outside_bbox":
        label["bbox"] = [0, 0, 999, 999]
    if attack == "invalid_bbox":
        label["bbox"] = [1, 1, math.nan, 5]
    if attack == "snapshot_bbox_drift":
        s["pages"][0]["spans"][0]["bbox"][0] += 1
    if attack == "snapshot_text_drift":
        s["pages"][0]["spans"][0]["text"] = "99999"
    if attack == "snapshot_font_drift":
        s["pages"][0]["spans"][0]["size"] = 12
    if attack == "snapshot_ref_missing":
        s["pages"][0]["spans"].pop(0)
    if attack == "snapshot_page_drift":
        s["pages"][0]["page"] = 2
    if attack == "duplicate_ref":
        label["source_refs"] = ["tick", "tick"]
    with pytest.raises(ValueError):
        regions(case)


def test_candidate_must_be_actual_draft_not_review_response(case):
    b, t, _, s = case
    with pytest.raises(ValueError, match="actual draft"):
        figure_label_regions(b, t, {"items": [], "issues": [], "notes": []}, source_snapshot=s)


def test_validated_bbox_only_is_padded_and_clamped_to_figure(case):
    label = case[2]["figures"][0]["labels"][0]
    label["source_refs"] = []
    label["bbox"] = [10, 10, 12, 11]
    result = regions(case)["regions"][0]
    assert result["region"]["bbox"] == [10, 10, 14, 13]
    assert result["source_ref_font_size"] is None
    assert result["reason"] == "validated_current_figure_bbox_only"


def test_limit_is_explicit_and_does_not_skip_validation_of_omitted_refs(case):
    labels = case[2]["figures"][0]["labels"]
    labels[:] = [copy.deepcopy(labels[0]) for _ in range(35)]
    result = regions(case, max_labels=32)
    assert result["manifest"]["total_labels"] == 35
    assert len(result["regions"]) == 32
    assert result["manifest"]["omitted_label_ids"] == ["fig@label32", "fig@label33", "fig@label34"]
    labels[-1]["source_refs"] = ["foreign"]
    with pytest.raises(ValueError, match="Foreign"):
        regions(case)


def test_budget_limits_reduce_scale_and_report_unreadable_or_omitted_regions(case):
    b, t, c, s = case
    b["figures"][0]["region"]["bbox"] = [0, 0, 2000, 2000]
    t["figure"] = copy.deepcopy(b["figures"][0])
    labels = c["figures"][0]["labels"]
    labels[:] = [
        {"source_refs": [], "bbox": [10, 10, 310, 10.1]},
        {"source_refs": [], "bbox": [10, 10, 1010, 1010]},
    ]
    result = regions(case)
    item = result["regions"][0]
    assert 4 <= item["scale"] < 40 and item["resolution_limited"]
    box = item["region"]["bbox"]
    assert math.ceil((box[2] - box[0]) * item["scale"]) <= 2048
    assert result["manifest"]["omissions"] == [
        {"label_id": "fig@label1", "reason": "pixel_budget_below_minimum_scale"}
    ]


def test_no_candidate_and_absent_font_never_claim_semantic_review(case):
    b, t, _, _ = case
    result = figure_label_regions(b, t, None)
    assert result["regions"] == [] and not result["manifest"]["candidate_available"]
    for span in b["figure_source_spans"]["fig"]:
        span.pop("size")
    result = figure_label_regions(b, t, case[2])
    assert result["regions"][0]["source_ref_font_size"] is None
    assert not result["manifest"]["snapshot_checked"]


@pytest.mark.parametrize("limit", [0, 257, True, 1.5])
def test_limit_cannot_expand_unbounded_image_delivery(case, limit):
    with pytest.raises(ValueError):
        regions(case, max_labels=limit)


@pytest.fixture
def tiny_pdf(tmp_path):
    import hashlib

    import pymupdf
    from pairper.pdf.source import extract

    path = tmp_path / "original-tiny.pdf"
    document = pymupdf.open()
    page = document.new_page(width=250, height=150)
    page.insert_text((40, 70.6), "12500", fontsize=0.62384)
    page.insert_text((45, 71), "Axis", fontsize=0.7)
    page.draw_rect(pymupdf.Rect(39, 68, 39.5, 69), color=(1, 0, 0), fill=(1, 0, 0))
    page.set_rotation(90)  # Source coordinates remain unrotated PDF points.
    document.save(path)
    document.close()
    original = path.read_bytes()
    source = extract(path, hashlib.sha256(original).hexdigest())
    return path, source, original


def test_bounded_product_renderer_actually_renders_38_point_5_scale(tiny_pdf):
    import pymupdf
    from pairper.pdf.source import render

    path, source, original = tiny_pdf
    before = copy.deepcopy(source)
    item = {"region": {"page": 1, "bbox": [38, 68, 44, 73], "rotation": 0}, "scale": 38.5}
    png = render_label_evidence(path, source, item)
    image = pymupdf.Pixmap(png)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert (image.width, image.height) == (231, 193)
    assert min(image.samples) < 200
    assert path.read_bytes() == original and source == before
    # The generic renderer remains bounded; this is a separate local-only API.
    with pytest.raises(ValueError, match="between 0.5 and 4"):
        render(path, source, 1, scale=38.5)


@pytest.mark.parametrize(
    "rotation,size", [(0, (240, 200)), (90, (200, 240)), (180, (240, 200)), (270, (200, 240))]
)
def test_local_renderer_rotation_uses_original_unrotated_coordinates(tiny_pdf, rotation, size):
    import pymupdf

    path, source, original = tiny_pdf
    item = {"region": {"page": 1, "bbox": [38, 68, 44, 73], "rotation": rotation}, "scale": 40}
    image = pymupdf.Pixmap(render_label_evidence(path, source, item))
    assert (image.width, image.height) == size
    assert min(image.samples) < 200
    assert path.read_bytes() == original and source["pages"][0]["rotation"] == 90


@pytest.mark.parametrize(
    "attack",
    [
        "scale_nan",
        "scale_inf",
        "scale_bool",
        "scale_low",
        "scale_high",
        "width",
        "height",
        "pixels",
        "negative",
        "outside_page",
        "actual_page",
        "source_hash",
        "rotation",
    ],
)
def test_local_renderer_rejects_invalid_or_excessive_render_before_allocation(
    tiny_pdf, monkeypatch, attack
):
    import pymupdf

    path, source, original = tiny_pdf
    item = {"region": {"page": 1, "bbox": [38, 68, 44, 73], "rotation": 0}, "scale": 38.5}
    if attack == "scale_nan":
        item["scale"] = math.nan
    if attack == "scale_inf":
        item["scale"] = math.inf
    if attack == "scale_bool":
        item["scale"] = True
    if attack == "scale_low":
        item["scale"] = 3.99
    if attack == "scale_high":
        item["scale"] = 40.01
    if attack == "width":
        item["region"]["bbox"] = [0, 0, 100, 1]
    if attack == "height":
        item["region"]["bbox"] = [0, 0, 1, 100]
    if attack == "pixels":
        item["region"]["bbox"] = [0, 0, 52, 52]
    if attack == "negative":
        item["region"]["bbox"][0] = -1
    if attack == "outside_page":
        item["region"]["bbox"] = [249, 0, 251, 1]
    if attack == "actual_page":
        source["pages"][0]["width"] = 999
        item["region"]["bbox"] = [249, 0, 251, 1]
    if attack == "source_hash":
        source["sha256"] = "wrong-original"
    if attack == "rotation":
        item["region"]["rotation"] = 45
    calls = []
    monkeypatch.setattr(pymupdf.Page, "get_pixmap", lambda *a, **k: calls.append(True))
    with pytest.raises(ValueError):
        render_label_evidence(path, source, item)
    assert calls == [] and path.read_bytes() == original


def test_local_renderer_checks_post_render_pixel_budget(tiny_pdf, monkeypatch):
    from types import SimpleNamespace

    import pymupdf

    path, source, original = tiny_pdf
    item = {"region": {"page": 1, "bbox": [38, 68, 44, 73], "rotation": 0}, "scale": 38.5}
    monkeypatch.setattr(
        pymupdf.Page, "get_pixmap", lambda *a, **k: SimpleNamespace(width=2049, height=1)
    )
    with pytest.raises(ValueError, match="budget"):
        render_label_evidence(path, source, item)
    assert path.read_bytes() == original


@pytest.mark.parametrize("box", [[0, 70, 42, 71], [40, 105, 42, 111],
                                 [205, 70, 215, 71], [40, 0, 42, 20]])
def test_owned_partial_span_uses_only_intersection_and_reports_truncation(case, box):
    b, t, c, snapshot = case
    b["figure_source_spans"]["fig"][0]["bbox"] = box
    snapshot["pages"][0]["spans"][0]["bbox"] = list(box)
    before = copy.deepcopy(case)
    item = regions(case)["regions"][0]
    detail = item["source_ref_evidence"][0]
    assert detail["original_bbox"] == box
    assert detail["evidence_bbox"] == [max(box[0], 10), max(box[1], 10),
                                       min(box[2], 210), min(box[3], 110)]
    assert detail["clipped"] and detail["source_text_may_be_truncated"]
    assert item["source_evidence_clipped"] and item["source_text_may_be_truncated"]
    crop = item["region"]["bbox"]
    assert 10 <= crop[0] < crop[2] <= 210 and 10 <= crop[1] < crop[3] <= 110
    assert case == before


@pytest.mark.parametrize("box", [[0, 70, 9, 71], [0, 70, 10, 71],
                                 [210, 70, 220, 71], [40, 110, 42, 120]])
def test_owned_fully_outside_or_touching_span_still_rejected(case, box):
    case[0]["figure_source_spans"]["fig"][0]["bbox"] = box
    case[3]["pages"][0]["spans"][0]["bbox"] = list(box)
    with pytest.raises(ValueError, match="outside the current figure"):
        regions(case)


def test_inside_span_marks_no_clipping_and_keeps_original_crop(case):
    item = regions(case)["regions"][0]
    assert item["region"]["bbox"] == [38, 68, 44, 72.7268]
    assert not item["source_evidence_clipped"]
    assert not item["source_text_may_be_truncated"]
    detail = item["source_ref_evidence"][0]
    assert detail["original_bbox"] == detail["evidence_bbox"]


def test_partial_owned_ref_does_not_relax_raw_candidate_bbox_or_snapshot_binding(case):
    case[0]["figure_source_spans"]["fig"][0]["bbox"] = [40, 105, 42, 111]
    case[3]["pages"][0]["spans"][0]["bbox"] = [40, 105, 42, 111]
    case[2]["figures"][0]["labels"][0]["bbox"] = [40, 105, 42, 111]
    with pytest.raises(ValueError, match="label bbox is outside"):
        regions(case)
    case[2]["figures"][0]["labels"][0]["bbox"] = []
    case[3]["pages"][0]["spans"][0]["bbox"] = [40, 105, 42, 112]
    with pytest.raises(ValueError, match="differs from original"):
        regions(case)



def test_partial_owned_span_crop_renders_real_pdf_within_original_roi(tmp_path):
    import hashlib

    import pymupdf
    from pairper.pdf.source import extract

    pdf = tmp_path / "original.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=250, height=150)
        page.insert_text((60, 80), "Sequence Length", fontsize=8)
        document.save(pdf)
    original = pdf.read_bytes()
    snapshot = extract(pdf, hashlib.sha256(original).hexdigest())
    span = snapshot["pages"][0]["spans"][0]
    region = {"page": 1, "bbox": [40, 50, 200, span["bbox"][3] - 0.5], "rotation": 0}
    figure = {"block_id": "fig", "region": region, "crop_sha256": "unchanged"}
    bundle = {"paper_id": "paper", "fingerprint": "current", "figures": [figure],
              "figure_source_spans": {"fig": [{**span, "page": 1}]}}
    task = {"paper_id": "paper", "bundle_fingerprint": "current", "figure": copy.deepcopy(figure)}
    candidate = {"figures": [{"block_id": "fig", "labels": [
        {"source": "Sequence Length", "target": "translated", "source_refs": [span["id"]], "bbox": []}]}]}
    before = copy.deepcopy((bundle, task, candidate, snapshot))
    item = figure_label_regions(bundle, task, candidate, source_snapshot=snapshot)["regions"][0]
    image = render_label_evidence(pdf, snapshot, item)
    pixmap = pymupdf.Pixmap(image)
    assert pixmap.width > 0 and pixmap.height > 0 and min(pixmap.samples) < 255
    assert item["source_evidence_clipped"] and item["region"]["bbox"][3] == region["bbox"][3]
    assert original == pdf.read_bytes()
    assert before == (bundle, task, candidate, snapshot)
