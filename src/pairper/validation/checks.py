import re
from collections import Counter

from pairper.document.models import Document, MathRun, TextRun, rendered_refs
from pairper.pdf.source import check_region


def contains(outer, inner):
    return (
        outer[0] <= inner[0] + 0.25
        and outer[1] <= inner[1] + 0.25
        and outer[2] >= inner[2] - 0.25
        and outer[3] >= inner[3] - 0.25
    )


def validate_document(library, doc: Document):
    errors, warnings = [], []
    paper = library.paper(doc.paper_id)
    if doc.source_sha256 != paper["sha256"]:
        errors.append("Source hash mismatch")
    source = library.source(doc.paper_id)
    spans = {s["id"]: (p["page"], s) for p in source["pages"] for s in p["spans"]}
    used = Counter()
    regions = []
    verified_text_bounds = []
    ids = [b.id for b in doc.blocks]
    if not doc.blocks:
        errors.append("Document has no blocks")
    if len(ids) != len(set(ids)):
        errors.append("Duplicate block IDs")
    orders = [b.order for b in doc.blocks]
    if len(orders) != len(set(orders)):
        errors.append("Duplicate reading order")
    for block in doc.blocks:
        c = block.content
        block.validation_status = "source_verified"
        expected = rendered_refs(block)
        own_regions = [c.region] if c.region else []
        runs = list(c.runs) + [r for item in c.items for r in item]
        runs += [r for cell in c.cells for r in cell.runs]
        runs += [r for line in c.code_lines for r in line.runs]
        own_regions += [r.region for r in runs if isinstance(r, MathRun)]
        # SliceGPT captions use small coloured rectangles behind ordinary words.
        # Reviewed native text covers a decoration contained in that same glyph box.
        verified_text_bounds += [
            (spans[ref][0], spans[ref][1]["bbox"])
            for run in runs if isinstance(run, TextRun) and run.source_verified
            for ref in run.refs if ref in spans
        ]
        for region in own_regions:
            try:
                check_region(source, region)
            except ValueError as exc:
                errors.append(f"{block.id}: {exc}")
                continue
            regions.append(region)
            page_info = source["pages"][region.page - 1]
            area = (region.bbox[2] - region.bbox[0]) * (region.bbox[3] - region.bbox[1])
            if page_info["spans"] and area > page_info["width"] * page_info["height"] * 0.9:
                errors.append(f"{block.id}: full-page fallback is only allowed for textless pages")
            # The source image preserves enclosed text even when glyph extraction is poor.
            enclosed = [
                ref
                for ref, (page, s) in spans.items()
                if page == region.page and (
                    contains(region.bbox, s["bbox"])
                    or (block.type == "figure" and contains(
                        region.bbox, page_info.get("text_paint_bounds", {}).get(
                            ref, page_info.get("rotated_glyph_bounds", {}).get(ref, s["bbox"])
                        ),
                    ))
                )
            ]
            expected += [ref for ref in enclosed if ref not in expected]
        if set(block.source_refs) != set(expected):
            errors.append(f"{block.id}: source_refs must match displayed text and enclosed regions")
        if len(block.source_refs) != len(set(block.source_refs)):
            errors.append(f"{block.id}: repeated source_refs")
        for ref in expected:
            used[ref] += 1
            if ref not in spans:
                errors.append(f"{block.id}: unknown text ID {ref}")
        if c.caption_id and (
            c.caption_id not in ids
            or next(b for b in doc.blocks if b.id == c.caption_id).type != "caption"
        ):
            errors.append(f"{block.id}: invalid caption link")
        if c.region:
            expected_asset = library.asset(doc.paper_id, c.region)
            if c.asset_id and c.asset_id != expected_asset:
                errors.append(f"{block.id}: asset_id does not match source region")
            c.asset_id = expected_asset
            if not library.managed(f"assets/{c.asset_id}.png").is_file():
                errors.append(f"{block.id}: missing asset")
            if block.type == "equation" and not (c.source_verified and c.latex and c.latex.strip()):
                errors.append(f"{block.id}: equation requires source-verified LaTeX")
                block.validation_status = "fallback"
            if block.type == "table" and not (c.source_verified and c.cells):
                errors.append(f"{block.id}: table requires source-verified structured cells")
                block.validation_status = "fallback"
        if block.type == "table" and c.cells and c.source_verified:
            occupied = set()
            for cell in c.cells:
                for row in range(cell.row, cell.row + cell.rowspan):
                    for col in range(cell.column, cell.column + cell.colspan):
                        if (row, col) in occupied:
                            errors.append(f"{block.id}: overlapping table cells")
                        occupied.add((row, col))
        for run in runs:
            if isinstance(run, MathRun):
                for ref in run.refs:
                    if ref in spans and (
                        spans[ref][0] != run.region.page
                        or not contains(run.region.bbox, spans[ref][1]["bbox"])
                    ):
                        errors.append(f"{block.id}: inline math reference outside source region")
                if not (run.source_verified and run.latex.strip()):
                    errors.append(f"{block.id}: inline math requires source-verified LaTeX")
                    block.validation_status = "fallback"
    omitted = Counter(o.ref for o in doc.omissions)
    for omission in doc.omissions:
        if omission.ref not in spans:
            errors.append(f"Unknown omitted text ID {omission.ref}")
        if used[omission.ref]:
            errors.append(f"Text both displayed and omitted: {omission.ref}")
        if omission.ref in spans:
            page_number, span = spans[omission.ref]
            page = source["pages"][page_number - 1]
            marginal = (
                span["bbox"][3] < page["height"] * 0.12 or span["bbox"][1] > page["height"] * 0.88
            )
            if not marginal:
                errors.append(f"Omission is not in a page margin: {omission.ref}")
            if omission.category == "page_number":
                if not re.fullmatch(r"[\s\divxlcIVXLC()\[\]-]+", span["text"]):
                    errors.append(f"Omission is not a page number: {omission.ref}")
            else:
                repeats = {p for p, s in spans.values() if s["text"] == span["text"]}
                if len(repeats) < 2:
                    errors.append(f"Omitted header/footer is not repeated: {omission.ref}")
    missing = sorted(set(spans) - set(used) - set(omitted))
    duplicates = sorted(ref for ref, count in used.items() if count > 1)
    if missing:
        errors.append(f"{len(missing)} source text spans omitted")
    if duplicates or any(n > 1 for n in omitted.values()):
        errors.append("Duplicate source references")
    for p in source["pages"]:
        if p["page"] not in doc.visual_review_pages:
            errors.append(f"Page {p['page']}: visual review not attested")
        for item in p["images"] + p["drawings"]:
            if item.get("background_only"):
                continue
            box = item.get("visible_bbox", item["bbox"])
            if box is None:
                continue
            if box[2] - box[0] < 1 or box[3] - box[1] < 1:
                continue
            if not (
                any(r.page == p["page"] and contains(r.bbox, box) for r in regions)
                or (item in p["drawings"] and any(
                    page == p["page"] and contains(bounds, box)
                    for page, bounds in verified_text_bounds
                ))
            ):
                errors.append(f"Page {p['page']}: uncovered visual region {box}")
        if not p["spans"] and not any(
            r.page == p["page"] and contains(r.bbox, (0, 0, p["width"], p["height"]))
            for r in regions
        ):
            errors.append(f"Page {p['page']}: no extractable text; full source fallback required")
    warnings.append(
        "Coverage checks do not prove reading order or mathematical/semantic equivalence. "
        "Visual review is an explicit builder attestation, not proof of client delivery."
    )
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "missing_refs": missing,
        "duplicate_refs": duplicates,
        "covered_spans": len(set(spans) & set(used)),
        "total_spans": len(spans),
        "omissions": [o.model_dump() for o in doc.omissions],
    }
