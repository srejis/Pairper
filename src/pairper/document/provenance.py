"""Resolve explicit AI selections; derive provenance from actual displayed content."""

from pairper.document.models import MathRun, TextRun, block_runs, rendered_refs


def contains(outer, inner):
    return (outer[0] <= inner[0] + .25 and outer[1] <= inner[1] + .25
            and outer[2] >= inner[2] - .25 and outer[3] >= inner[3] - .25)


def expand_range(selection, source):
    for page in source["pages"]:
        spans = page["spans"]
        positions = {s["id"]: i for i, s in enumerate(spans)}
        if selection.first_ref not in positions:
            continue
        if selection.last_ref not in positions:
            raise ValueError("A source range must remain within one page and extraction block")
        first, last = positions[selection.first_ref], positions[selection.last_ref]
        if last < first or spans[first]["block"] != spans[last]["block"]:
            raise ValueError("A source range must follow extraction order within one block")
        return [s["id"] for s in spans[first:last + 1]]
    raise ValueError("Unknown source range reference")


def materialize_ranges(block, source):
    for run in block_runs(block):
        if isinstance(run, TextRun) and run.ref_ranges:
            run.refs = [ref for selection in run.ref_ranges for ref in expand_range(selection, source)]
            run.ref_ranges = []


def expected_refs(block, source):
    expected = rendered_refs(block)
    regions = ([block.content.region] if block.content.region else []) + [
        r.region for r in block_runs(block) if isinstance(r, MathRun)
    ]
    for region in regions:
        if not 1 <= region.page <= len(source["pages"]):
            continue  # The independent region check reports the invalid coordinates.
        page = source["pages"][region.page - 1]
        for span in page["spans"]:
            ref = span["id"]
            enclosed = contains(region.bbox, span["bbox"])
            if block.type == "figure":
                bounds = page.get("text_paint_bounds", {}).get(
                    ref, page.get("rotated_glyph_bounds", {}).get(ref, span["bbox"])
                )
                enclosed = enclosed or contains(region.bbox, bounds)
            if enclosed and ref not in expected:
                expected.append(ref)
    return expected
