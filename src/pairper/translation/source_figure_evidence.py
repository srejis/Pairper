"""Bounded original-PDF label regions; evidence preparation, never approval.

No image is rendered here, no source/candidate is changed, and no translation
meaning or projected-output dependency is inferred. Callers retain the returned
manifest, including omissions and resolution limits, alongside actual renders.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pymupdf
from pairper.document.models import Region
from pairper.pdf.source import check_region


def render_label_evidence(path, source, item):
    """Render one verified local original-PDF region with a separate tight budget.

    This deliberately does not relax the shared page/math renderer's scale cap.
    No document is saved. The caller must retain the region/scale/image hash and
    independently review the result; successful rendering is not source approval.
    """
    if not isinstance(item, dict):
        raise ValueError("Original label evidence needs a region and scale")
    raw_region = item.get("region")
    _region(raw_region)
    region = Region.model_validate(raw_region)
    scale = item.get("scale")
    if (
        isinstance(scale, bool)
        or not isinstance(scale, (int, float))
        or not math.isfinite(scale)
        or not 4 <= scale <= 40
    ):
        raise ValueError("Local label evidence scale must be finite and between 4 and 40")
    check_region(source, region)
    rectangle = pymupdf.Rect(region.bbox)
    matrix = pymupdf.Matrix(scale, scale).prerotate(region.rotation)
    pixels = (rectangle * matrix).irect

    def check_pixels(width, height):
        if not (0 < width <= 2048 and 0 < height <= 2048 and width * height <= 4_000_000):
            raise ValueError("Local label evidence exceeds 2048-side or 4-megapixel budget")

    check_pixels(pixels.width, pixels.height)
    original_bytes = Path(path).read_bytes()
    if source.get("sha256") and hashlib.sha256(original_bytes).hexdigest() != source["sha256"]:
        raise ValueError("Original PDF differs from the supplied source snapshot")
    with pymupdf.open(stream=original_bytes, filetype="pdf") as document:
        if document.needs_pass or not document.is_pdf or region.page > len(document):
            raise ValueError("Original PDF is unavailable for this source page")
        page = document[region.page - 1]
        page.set_rotation(0)
        if not page.rect.contains(rectangle):
            raise ValueError("Original label region is outside the actual PDF page")
        image = page.get_pixmap(matrix=matrix, clip=rectangle, alpha=False)
        check_pixels(image.width, image.height)
        return image.tobytes("png")


def _box(value):
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
            for v in value
        )
    ):
        raise ValueError("Figure evidence needs four finite bbox coordinates")
    box = list(map(float, value))
    if box[0] >= box[2] or box[1] >= box[3]:
        raise ValueError("Figure evidence bbox must have positive area")
    return box


def _within(inner, outer):
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and inner[2] <= outer[2]
        and inner[3] <= outer[3]
    )


def _region(value):
    if not isinstance(value, dict):
        raise ValueError("Figure evidence needs a current source region")
    page = value.get("page")
    rotation = value.get("rotation", 0)
    if (
        isinstance(page, bool)
        or not isinstance(page, int)
        or page < 1
        or isinstance(rotation, bool)
        or rotation not in (0, 90, 180, 270)
    ):
        raise ValueError("Invalid figure page or rotation")
    return {"page": page, "bbox": _box(value.get("bbox")), "rotation": rotation}


def _snapshot_spans(snapshot):
    spans = {}
    for page in snapshot.get("pages", []):
        for span in page.get("spans", []):
            if span["id"] in spans:
                raise ValueError("Duplicate original source span ID")
            spans[span["id"]] = (page["page"], span)
    return spans


def figure_label_regions(bundle, task, candidate, *, source_snapshot=None, max_labels=256):
    """Return ``{regions:[...], manifest:{...}}`` for one current figure task.

    ``candidate`` is the actual unapproved figure draft, not a reviewer response.
    The snapshot, when supplied, independently binds each current span to its
    original coordinates/text/font. All labels are validated before the explicit
    finite region cap, so an omitted foreign reference cannot evade the gate.
    """
    if isinstance(max_labels, bool) or not isinstance(max_labels, int) or not 1 <= max_labels <= 256:
        raise ValueError("Figure label evidence limit must be between 1 and 256")
    if task.get("paper_id") != bundle.get("paper_id"):
        raise ValueError("Figure task belongs to another paper")
    if task.get("bundle_fingerprint") is not None and task["bundle_fingerprint"] != bundle.get(
        "fingerprint"
    ):
        raise ValueError("Figure task belongs to another source bundle")
    supplied = task.get("figure")
    if not isinstance(supplied, dict):
        raise ValueError("Figure label evidence requires an explicit figure task")
    block_id = supplied.get("block_id")
    figures = [f for f in bundle.get("figures", []) if f.get("block_id") == block_id]
    if len(figures) != 1:
        raise ValueError("Unknown or duplicate current figure")
    figure = figures[0]
    if figure.get("paper_id", bundle["paper_id"]) != bundle["paper_id"]:
        raise ValueError("Current figure belongs to another paper")
    region = _region(figure.get("region"))
    if _region(supplied.get("region")) != region:
        raise ValueError("Task figure region differs from the current source")
    for key in ("crop_sha256",):
        if key in supplied and supplied[key] != figure.get(key):
            raise ValueError("Task figure image differs from the current source")
    original = _snapshot_spans(source_snapshot) if source_snapshot is not None else None
    spans = {}
    for span in bundle.get("figure_source_spans", {}).get(block_id, []):
        ref = span["id"]
        if ref in spans:
            raise ValueError("Duplicate current figure span ID")
        spans[ref] = span
    manifest = {
        "paper_id": bundle["paper_id"],
        "task_id": task.get("task_id"),
        "bundle_fingerprint": bundle.get("fingerprint"),
        "block_id": block_id,
        "figure_region": region,
        "figure_crop_sha256": figure.get("crop_sha256"),
        "max_labels": max_labels,
        "total_labels": 0,
        "selected_labels": 0,
        "omitted_label_ids": [],
        "omissions": [],
        "snapshot_checked": original is not None,
        "limits": {
            "min_scale": 4,
            "max_scale": 40,
            "target_glyph_pixels": 24,
            "max_width_pixels": 2048,
            "max_height_pixels": 2048,
            "max_pixels_per_crop": 4_000_000,
            "padding_points": 2,
        },
        "semantic_review_performed": False,
        "rendered_images_verified": False,
    }
    if candidate is None:
        manifest["candidate_available"] = False
        return {"regions": [], "manifest": manifest}
    values = candidate.get("figures") if isinstance(candidate, dict) else None
    if (
        not isinstance(values, list)
        or len(values) != 1
        or not isinstance(values[0], dict)
        or values[0].get("block_id") != block_id
    ):
        raise ValueError("Evidence needs the actual draft for exactly this figure")
    labels = values[0].get("labels")
    if not isinstance(labels, list) or len(labels) > 256:
        raise ValueError("Figure candidate labels must be a list of at most 256")
    manifest["candidate_available"] = True
    manifest["total_labels"] = len(labels)
    validated = []
    for index, label in enumerate(labels):
        if not isinstance(label, dict):
            raise ValueError("Invalid figure candidate label")
        label_id = f"{block_id}@label{index}"
        refs = label.get("source_refs", [])
        if (
            not isinstance(refs, list)
            or any(not isinstance(ref, str) for ref in refs)
            or len(set(refs)) != len(refs)
        ):
            raise ValueError("Figure label refs must be distinct source IDs")
        raw = label.get("bbox", [])
        if raw:
            if not _within(_box(raw), region["bbox"]):
                raise ValueError("Figure label bbox is outside the current figure")
        boxes, font_sizes, glyph_heights = [], [], []
        ref_evidence = []
        for ref in refs:
            if ref not in spans:
                raise ValueError("Foreign reference outside the current figure")
            span = spans[ref]
            box = _box(span.get("bbox"))
            used_box = [max(box[0], region["bbox"][0]), max(box[1], region["bbox"][1]),
                        min(box[2], region["bbox"][2]), min(box[3], region["bbox"][3])]
            if (span.get("page") != region["page"]
                    or used_box[0] >= used_box[2] or used_box[1] >= used_box[3]):
                raise ValueError("Figure source span is outside the current figure")
            if original is not None:
                if ref not in original or original[ref][0] != region["page"]:
                    raise ValueError("Figure ref missing from original source page")
                source = original[ref][1]
                if _box(source.get("bbox")) != box:
                    raise ValueError("Figure span bbox differs from original source")
                for key in ("text", "font", "size"):
                    if key in span and span[key] != source.get(key):
                        raise ValueError("Figure span content differs from original source")
            else:
                source = span
            size = source.get("size")
            if size is not None:
                if (
                    isinstance(size, bool)
                    or not isinstance(size, (int, float))
                    or not math.isfinite(size)
                    or size <= 0
                ):
                    raise ValueError("Invalid original figure font size")
                font_sizes.append(float(size))
            glyph_heights.append(box[3] - box[1])
            # Current-figure refs can have font-metric boxes crossing its ROI.
            # Keep the original binding exact; crop only their positive intersection.
            # This never establishes that the clipped printed text is complete.
            clipped = used_box != box
            ref_evidence.append({"source_ref": ref, "original_bbox": box,
                                 "evidence_bbox": used_box, "clipped": clipped,
                                 "source_text_may_be_truncated": clipped})
            boxes.append(used_box)
        if boxes:
            box = [
                min(b[0] for b in boxes),
                min(b[1] for b in boxes),
                max(b[2] for b in boxes),
                max(b[3] for b in boxes),
            ]
            reason = "current_figure_source_refs_union"
        elif raw:
            box = _box(raw)
            glyph_heights.append(box[3] - box[1])
            reason = "validated_current_figure_bbox_only"
        else:
            raise ValueError("Figure label has no verified local source anchor")
        padded = [
            max(region["bbox"][0], box[0] - 2),
            max(region["bbox"][1], box[1] - 2),
            min(region["bbox"][2], box[2] + 2),
            min(region["bbox"][3], box[3] + 2),
        ]
        glyph_size = min(font_sizes + glyph_heights)
        requested_scale = min(40.0, max(4.0, 24.0 / glyph_size))
        width, height = padded[2] - padded[0], padded[3] - padded[1]
        # Leave one rounding pixel per dimension for the PDF rasterizer.
        budget_scale = min(2047 / width, 2047 / height, math.sqrt(3_990_000 / (width * height)))
        scale = min(requested_scale, budget_scale)
        item = {
            "label_id": label_id,
            "block_id": block_id,
            "region": {**region, "bbox": padded},
            "scale": scale,
            "source_refs": list(refs),
            "source_ref_evidence": ref_evidence,
            "source_evidence_clipped": any(ref["clipped"] for ref in ref_evidence),
            "source_text_may_be_truncated": any(ref["clipped"] for ref in ref_evidence),
            "source_ref_font_size": min(font_sizes) if font_sizes else None,
            "reason": reason,
            "requested_scale": requested_scale,
            "estimated_smallest_glyph_pixels": glyph_size * scale,
            "resolution_limited": glyph_size * scale < 24 - 1e-6,
        }
        validated.append(item)
    selected = []
    for item in validated:
        reason = (
            "pixel_budget_below_minimum_scale"
            if item["scale"] < 4
            else "explicit_label_limit"
            if len(selected) >= max_labels
            else None
        )
        if reason:
            manifest["omitted_label_ids"].append(item["label_id"])
            manifest["omissions"].append({"label_id": item["label_id"], "reason": reason})
        else:
            selected.append(item)
    manifest["selected_labels"] = len(selected)
    return {"regions": selected, "manifest": manifest}



def pack_label_evidence(pdf_path, source, regions, directory, *, base_image_count=0, max_images=32):
    """Losslessly pack bounded original crops, never infer visual completeness.

    ASCII header aliases identify occurrences through the full label_id manifest.
    Original crop pixels are copied byte-for-byte; headers occupy separate rows.
    The caller must include all pre-existing attachments in base_image_count.
    """
    if (isinstance(base_image_count, bool) or not isinstance(base_image_count, int)
            or isinstance(max_images, bool) or not isinstance(max_images, int)
            or not 1 <= max_images <= 32 or not 0 <= base_image_count <= max_images):
        raise ValueError("Invalid total original-evidence attachment budget")
    regions = list(regions)
    if len(regions) > 256 or len({r["label_id"] for r in regions}) != len(regions):
        raise ValueError("Label atlas requires at most 256 distinct label IDs")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    output = {"images": [], "manifest": {"packaging_version": "lossless-label-atlas-v1",
              "max_input_labels": 256, "max_total_attachments": max_images,
              "base_image_count": base_image_count, "atlas_images": [], "rendered_images": [],
              "omissions": [], "omitted_label_ids": [], "pixel_resampling": False,
              "semantic_review_performed": False}}
    manifest = output["manifest"]
    slots = max_images - base_image_count
    tiles, x, y, row_height, width, height = [], 0, 0, 0, 0, 0

    def omit(item, reason):
        manifest["omitted_label_ids"].append(item["label_id"])
        manifest["omissions"].append({"label_id": item["label_id"], "reason": reason})

    def flush():
        nonlocal tiles, x, y, row_height, width, height
        if not tiles:
            return
        # Render only ASCII headers first; source raster goes into disjoint areas.
        with pymupdf.open() as header_document:
            page = header_document.new_page(width=width, height=height)
            for tile in tiles:
                page.insert_text((tile["x"] + 2, tile["y"] + 10), tile["alias"], fontsize=8)
            pixels = bytearray(page.get_pixmap(alpha=False).samples)
        for tile in tiles:
            w, h, tx, ty = tile["width"], tile["height"], tile["x"], tile["y"] + 16
            for row in range(h):
                start = ((ty + row) * width + tx) * 3
                pixels[start:start + w * 3] = tile["pixels"][row * w * 3:(row + 1) * w * 3]
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, width, height, bytes(pixels), False)
        png = pixmap.tobytes("png")
        digest = hashlib.sha256(png).hexdigest()
        path = directory / f"atlas-{digest}.png"
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Original label atlas cache changed")
        if not path.exists():
            path.write_bytes(png)
        atlas_id = f"atlas{len(output['images'])}"
        output["images"].append(str(path))
        manifest["atlas_images"].append({"atlas_id": atlas_id, "path": str(path),
            "sha256": digest, "width": width, "height": height, "role": "original_figure_label_atlas"})
        # Verify against the actual decoded output PNG, not just our copy buffer.
        decoded = pymupdf.Pixmap(png)
        raster = decoded.samples
        for tile in tiles:
            w, h, tx, ty = tile["width"], tile["height"], tile["x"], tile["y"] + 16
            extracted = b"".join(raster[((ty + row) * width + tx) * 3:
                                        ((ty + row) * width + tx + w) * 3] for row in range(h))
            if hashlib.sha256(extracted).hexdigest() != tile["pixel_sha256"]:
                raise ValueError("Label atlas changed original crop pixels")
            manifest["rendered_images"].append({**tile["item"], "atlas_id": atlas_id,
                "atlas_sha256": digest, "path": str(path), "sha256": digest,
                "header_label_id": tile["alias"], "atlas_pixel_bbox": [tx, ty, tx + w, ty + h],
                "header_pixel_bbox": [tx, tile["y"], tx + tile["tile_width"], ty],
                "crop_png_sha256": tile["png_sha256"], "crop_pixel_sha256": tile["pixel_sha256"],
                "atlas_crop_pixel_sha256": hashlib.sha256(extracted).hexdigest(),
                "pixel_identity_verified": True, "pixel_resampling": False})
        tiles, x, y, row_height, width, height = [], 0, 0, 0, 0, 0

    for index, item in enumerate(regions):
        if len(output["images"]) >= slots:
            omit(item, "total_attachment_budget")
            continue
        png = render_label_evidence(pdf_path, source, item)
        pixmap = pymupdf.Pixmap(png)
        if pixmap.n != 3 or pixmap.alpha:
            raise ValueError("Label atlas requires original RGB pixels without alpha")
        w, h = pixmap.width, pixmap.height
        tile_width, tile_height = max(w, 40), h + 16
        if tile_width > 2048 or tile_height > 2048 or tile_width * tile_height > 4_000_000:
            omit(item, "atlas_header_or_pixel_budget")
            continue
        nx, ny = x, y
        if nx + tile_width > 2048:
            nx, ny = 0, y + row_height
        nw, nh = max(width, nx + tile_width), max(height, ny + tile_height)
        if nh > 2048 or nw * nh > 4_000_000:
            flush()
            if len(output["images"]) >= slots:
                omit(item, "total_attachment_budget")
                continue
            nx, ny, nw, nh = 0, 0, tile_width, tile_height
        sample = pixmap.samples
        tiles.append({"item": dict(item), "pixels": sample, "x": nx, "y": ny,
                      "width": w, "height": h, "tile_width": tile_width, "alias": f"L{index:03d}",
                      "pixel_sha256": hashlib.sha256(sample).hexdigest(),
                      "png_sha256": hashlib.sha256(png).hexdigest()})
        if ny != y:
            row_height = 0
        x, y, row_height, width, height = nx + tile_width, ny, max(row_height, tile_height), nw, nh
    flush()
    manifest["selected_labels"] = len(manifest["rendered_images"])
    manifest["atlas_count"] = len(output["images"])
    return output
