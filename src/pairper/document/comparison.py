"""Read-only source comparison. Requests identify saved blocks, never files or coordinates."""

import re
import uuid

import pymupdf

from pairper.document.models import MathRun, Region, rendered_refs
from pairper.pdf.source import check_region


def block_regions(block, source, spans=None):
    if spans is None:
        spans = {s["id"]: {**s, "page": p["page"]} for p in source["pages"] for s in p["spans"]}
    content = block.content
    regions = [content.region] if content.region else []
    runs = content.runs + [r for item in content.items for r in item]
    if block.type == "table" and content.source_verified:
        runs += [r for cell in content.cells for r in cell.runs]
    regions += [r.region for r in runs if isinstance(r, MathRun)]
    refs = list(dict.fromkeys(block.source_refs + rendered_refs(block)))
    # Keep separate lines, columns and pages separate. Merge only adjacent spans on one line.
    groups = []
    for ref in refs:
        if ref not in spans:
            raise ValueError("원본 참조를 찾을 수 없습니다.")
        span = spans[ref]
        region = Region(page=span["page"], bbox=span["bbox"])
        key = (span["page"], span.get("block"), span.get("line"))
        if groups:
            old_key, old = groups[-1]
            a, b = old.bbox, region.bbox
            if (
                key == old_key
                and key[1] is not None
                and key[2] is not None
                and 0 <= b[0] - a[2] <= 12
                and min(a[3], b[3]) > max(a[1], b[1])
            ):
                groups[-1] = (
                    key,
                    Region(page=old.page, bbox=(a[0], min(a[1], b[1]), b[2], max(a[3], b[3]))),
                )
                continue
        groups.append((key, region))
    regions += [region for _, region in groups]
    unique = {}
    for region in regions:
        check_region(source, region)
        unique.setdefault(region.model_dump_json(), region)
    return list(unique.values())


def region_descriptors(block, source, spans=None):
    try:
        return [
            {"id": f"r{i}", "page": region.page, "bbox": region.bbox}
            for i, region in enumerate(block_regions(block, source, spans))
        ], None
    except (ValueError, KeyError, TypeError):
        return [], "원본 위치가 유효하지 않아 대조할 수 없습니다."


def comparison_image(library, paper_id, view_id, block_id, region_id):
    from pairper.document.presentation import load_document

    if not all(re.fullmatch(r"[a-f0-9]{32}", value) for value in (paper_id, view_id)):
        raise ValueError("Invalid document ID")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", block_id):
        raise ValueError("Invalid block ID")
    if not re.fullmatch(r"r[0-9]{1,5}", region_id):
        raise ValueError("Invalid region ID")
    paper, document = load_document(library, paper_id, view_id)
    source = library.source(paper_id)  # Verifies the original SHA-256 on every request.
    block = next((b for b in document.blocks if b.id == block_id), None)
    if block is None:
        raise ValueError("Unknown block")
    regions = block_regions(block, source)
    index = int(region_id[1:])
    if index >= len(regions):
        raise ValueError("Unknown region")
    region = regions[index]
    path = library.managed(paper["original_path"])
    with pymupdf.open(path) as pdf:
        if region.page > len(pdf):
            raise ValueError("Missing original page")
        page = pdf[region.page - 1]
        page.set_rotation(0)
        rect = pymupdf.Rect(region.bbox)
        if not page.rect.contains(rect):
            raise ValueError("Region outside original PDF")
    scale = min(2.0, 1600 / max(rect.width, rect.height), (2_000_000 / rect.get_area()) ** 0.5)
    if scale < 0.5:
        raise ValueError("Source region is too large")
    data = library.image(paper_id, region.page, region, scale)
    if len(data) > 4_000_000:
        raise ValueError("Source image exceeds size limit")
    key = uuid.uuid5(uuid.NAMESPACE_URL, view_id + block_id + region.model_dump_json()).hex
    image = library.managed(f"assets/{key}.png")
    if not image.exists():
        temp = image.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            temp.write_bytes(data)
            temp.replace(image)
        finally:
            temp.unlink(missing_ok=True)
    return {"image_url": image.as_uri(), "page": region.page, "region_id": region_id}
