"""Pure, conservative cross-owner math-boundary repair proposals.

The caller must independently review the full normalized pair against original
PDF evidence and validate a fresh complete source view. These functions never
write a library, alter a PDF, call AI, or grant review approval.
"""

from __future__ import annotations

import copy
import json
import math
import uuid
from collections import Counter

from pairper.document.models import Block, Document, MathRun, Region
from pairper.document.provenance import contains, expected_refs
from pairper.translation.contract import sha

from pairper.translation.source_run_repair import _source_spans, resolve_run_group


def _json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("Duplicate boundary-transfer JSON field")
            result[key] = item
        return result

    def constant(_):
        raise ValueError("Nonfinite boundary-transfer JSON value")

    if not isinstance(value, str):
        raise ValueError("Boundary transfer requires JSON object strings")
    result = json.loads(value, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(result, dict):
        raise ValueError("Boundary transfer requires JSON objects")
    return result


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _block(document, block_id):
    matches = [block for block in document["blocks"] if block["id"] == block_id]
    if len(matches) != 1:
        raise ValueError("Unknown or duplicate boundary-transfer block")
    return matches[0]


def _counter(document):
    return Counter(ref for block in document["blocks"] for ref in block["source_refs"])


def _union(boxes):
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def _near(box, equation, threshold):
    x_overlap = min(box[2], equation[2]) - max(box[0], equation[0])
    y_overlap = min(box[3], equation[3]) - max(box[1], equation[1])
    x_gap = max(equation[0] - box[2], box[0] - equation[2], 0)
    y_gap = max(equation[1] - box[3], box[1] - equation[3], 0)
    return (y_overlap > 0 and x_gap <= threshold) or (x_overlap > 0 and y_gap <= threshold)


def _overlap_area(left, right):
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )


def normalize_boundary_transfer(bundle, finding, *, source_snapshot):
    """Bind one original boundary range and derive both complete block changes.

    Raw current JSON: equation_latex, donor_unit_id, start, end, donor_runs.
    Raw proposed JSON: insert_offset, equation_latex. End is exclusive; only a
    nonempty paragraph prefix/suffix of MathRuns can move to its adjacent existing
    Equation. Full before/after block JSON is returned for independent review.
    """
    finding = finding.model_dump() if hasattr(finding, "model_dump") else dict(finding)
    if finding.get("kind") != "math_boundary_transfer":
        raise ValueError("Expected math_boundary_transfer finding")
    source_id = finding.get("source_id", "")
    if (
        not isinstance(source_id, str)
        or not source_id.startswith("block:")
        or not source_id.endswith("/latex")
    ):
        raise ValueError("Boundary receiver must be an existing equation math ID")
    receiver_id = source_id[6:-6]
    current, proposed = _json(finding.get("current")), _json(finding.get("proposed"))
    if set(current) != {"equation_latex", "donor_unit_id", "start", "end", "donor_runs"}:
        raise ValueError("Unexpected current boundary-transfer fields")
    if set(proposed) != {"insert_offset", "equation_latex"}:
        raise ValueError("Unexpected proposed boundary-transfer fields")
    start, end = current["start"], current["end"]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (start, end)):
        raise ValueError("Boundary range indices must be exact integers")
    if not isinstance(current["donor_unit_id"], str):
        raise ValueError("Boundary donor must be a known source unit")
    resolved = resolve_run_group(bundle, f"{current['donor_unit_id']}/range/{start}:{end}")
    document = bundle["document"]
    receiver = _block(document, receiver_id)
    donor = _block(document, resolved["block_id"])
    if (
        receiver["type"] != "equation"
        or donor["type"] != "paragraph"
        or resolved["field"] != "runs"
        or receiver_id == donor["id"]
    ):
        raise ValueError(
            "Boundary transfer supports an existing Equation and primary paragraph runs only"
        )
    if receiver["content"]["latex"] != current["equation_latex"]:
        raise ValueError("Boundary equation LaTeX is stale")
    if current["donor_runs"] != resolved["current_runs"]:
        raise ValueError("Boundary selected raw runs are stale")
    runs = donor["content"]["runs"]
    if end - start >= len(runs):
        raise ValueError("Boundary transfer cannot empty or delete the donor block")
    ordered = sorted(document["blocks"], key=lambda block: block["order"])
    if len({block["order"] for block in ordered}) != len(ordered):
        raise ValueError("Ambiguous document block order")
    ri, di = ordered.index(receiver), ordered.index(donor)
    if not ((ri + 1 == di and start == 0) or (di + 1 == ri and end == len(runs))):
        raise ValueError("Selected boundary must face its immediately adjacent equation")
    selected = resolved["current_runs"]
    if any(run.get("kind") != "math" for run in selected):
        raise ValueError("Only contiguous original MathRuns can cross an equation boundary")
    refs = [ref for run in selected for ref in run["refs"]]
    if not refs or len(set(refs)) != len(refs):
        raise ValueError("Moved references must be nonempty and distinct")
    owners = _counter(document)
    if any(owners[ref] != 1 or donor["source_refs"].count(ref) != 1 for ref in refs):
        raise ValueError("Every moved reference needs exclusive donor ownership")
    if any(ref in receiver["source_refs"] for ref in refs):
        raise ValueError("Boundary receiver already owns a moved reference")
    number_refs = {
        ref for block in document["blocks"] for ref in block["content"].get("number_refs", [])
    }
    if set(refs) & number_refs:
        raise ValueError("Equation-number references cannot move as boundary math")
    if (
        not bundle.get("source_pdf_sha256")
        or source_snapshot.get("sha256") != bundle["source_pdf_sha256"]
    ):
        raise ValueError("Original source snapshot does not bind the bundle PDF")
    spans = _source_spans(source_snapshot)
    if any(ref not in spans for ref in refs + receiver["source_refs"]):
        raise ValueError("Unknown original reference in boundary transfer")
    old_region = Region.model_validate(receiver["content"]["region"])
    page = old_region.page
    if page not in resolved["source_pages"] or any(
        spans[ref][0] != page for ref in refs + receiver["source_refs"]
    ):
        raise ValueError("Boundary transfer must remain on the same original page")
    equation_box = list(old_region.bbox)
    moving_boxes = []
    for raw in selected:
        run = MathRun.model_validate(raw)
        if (
            not run.latex.strip()
            or run.region.page != page
            or run.region.rotation != old_region.rotation
        ):
            raise ValueError("Boundary MathRun must retain nonempty same-page original math")
        boxes = [spans[ref][1]["bbox"] for ref in run.refs]
        union = _union(boxes)
        Region(page=page, bbox=union)
        if not contains(run.region.bbox, union) or not contains(union, run.region.bbox):
            raise ValueError("Boundary MathRun region disagrees with original reference bounds")
        sizes = [spans[ref][1].get("size", union[3] - union[1]) for ref in run.refs]
        if any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
            for v in sizes
        ):
            raise ValueError("Invalid boundary source font geometry")
        if not _near(union, equation_box, max(2.0, min(12.0, max(sizes)))):
            raise ValueError("Boundary fragment is remote from the original equation")
        moving_boxes.extend(boxes)
    new_box = _union([equation_box, *moving_boxes])
    allowed = set(receiver["source_refs"]) | set(refs)
    if any(
        span_page == page
        and ref not in allowed
        and (
            contains(new_box, span["bbox"])
            or _overlap_area(new_box, span["bbox"])
            > _overlap_area(equation_box, span["bbox"]) + 1e-8
        )
        for ref, (span_page, span) in spans.items()
    ):
        raise ValueError("Expanded equation region encloses an outside original span")
    old_latex = current["equation_latex"]
    offset = proposed["insert_offset"]
    if (
        not isinstance(old_latex, str)
        or isinstance(offset, bool)
        or not isinstance(offset, int)
        or not 0 <= offset <= len(old_latex)
    ):
        raise ValueError("Boundary insertion offset is outside the exact current LaTeX")
    fragment = "".join(run["latex"] for run in selected)
    new_latex = old_latex[:offset] + fragment + old_latex[offset:]
    if proposed["equation_latex"] != new_latex:
        raise ValueError(
            "Boundary proposal must be exactly one insertion of the unchanged selected math"
        )
    # Snapshot array position is authoritative even if extraction order fields
    # are absent; references must be retained in that original page order.
    positions = {
        span["id"]: index for index, span in enumerate(source_snapshot["pages"][page - 1]["spans"])
    }
    receiver_after, donor_after = copy.deepcopy(receiver), copy.deepcopy(donor)
    receiver_after["content"]["latex"] = new_latex
    receiver_after["content"]["region"]["bbox"] = new_box
    receiver_after["content"]["asset_id"] = uuid.uuid5(
        uuid.NAMESPACE_URL,
        bundle["paper_id"]
        + Region.model_validate(receiver_after["content"]["region"]).model_dump_json(),
    ).hex
    receiver_after["source_refs"] = sorted(allowed, key=positions.__getitem__)
    donor_after["content"]["runs"][start:end] = []
    donor_after["source_refs"] = [ref for ref in donor["source_refs"] if ref not in set(refs)]
    for block in (receiver_after, donor_after):
        if set(expected_refs(Block.model_validate(block), source_snapshot)) != set(
            block["source_refs"]
        ):
            raise ValueError("Boundary transfer changes source coverage beyond selected ownership")
    normalized = {
        "source_id": source_id,
        "receiver_block_id": receiver_id,
        "donor_unit_id": resolved["unit_id"],
        "donor_block_id": donor["id"],
        "start": start,
        "end": end,
        "source_pages": [page],
        "current": _canonical({"receiver": receiver, "donor": donor}),
        "proposed": _canonical({"receiver": receiver_after, "donor": donor_after}),
        "original_current": finding["current"],
        "original_proposed": finding["proposed"],
        "audit": {
            "bundle_fingerprint": bundle.get("fingerprint"),
            "moved_refs": refs,
            "fragment_latex": fragment,
            "insert_offset": offset,
            "derived_region": receiver_after["content"]["region"],
            "receiver_ref_order_after": receiver_after["source_refs"],
            "requires_fresh_PDF_review": True,
            "requires_full_source_validation": True,
            "translation_approval_granted": False,
        },
    }
    # Detect accidental post-review mutation, not an authorization signature.
    normalized["normalization_sha256"] = sha(normalized)
    apply_boundary_transfers(document, [normalized])
    return normalized


def apply_boundary_transfers(document, normalized):
    """Build an atomic candidate from freshly normalized full-block pairs only."""
    result = copy.deepcopy(document)
    touched, audits = set(), []
    before_counter = _counter(document)
    for item in normalized:
        if item.get("normalization_sha256") != sha(
            {k: v for k, v in item.items() if k != "normalization_sha256"}
        ):
            raise ValueError("Boundary normalized proposal changed after validation")
        current, proposed = _json(item["current"]), _json(item["proposed"])
        if set(current) != {"receiver", "donor"} or set(proposed) != set(current):
            raise ValueError("Boundary normalized proposal requires a whole block pair")
        receiver_id, donor_id = item["receiver_block_id"], item["donor_block_id"]
        if receiver_id == donor_id or {receiver_id, donor_id} & touched:
            raise ValueError("Conflicting boundary transfers touch the same original block")
        receiver, donor = _block(document, receiver_id), _block(document, donor_id)
        if current != {"receiver": receiver, "donor": donor}:
            raise ValueError("Boundary full source blocks changed before atomic application")
        raw_current, raw_proposed = (
            _json(item["original_current"]),
            _json(item["original_proposed"]),
        )
        start, end = item["start"], item["end"]
        if donor["content"]["runs"][start:end] != raw_current["donor_runs"]:
            raise ValueError("Boundary original selection no longer matches its source")
        selected = donor["content"]["runs"][start:end]
        offset = raw_proposed["insert_offset"]
        old_latex = receiver["content"]["latex"]
        if (
            not selected
            or any(run["kind"] != "math" for run in selected)
            or raw_current["equation_latex"] != old_latex
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 0 <= offset <= len(old_latex)
        ):
            raise ValueError("Boundary insertion no longer matches its original math")
        fragment = "".join(run["latex"] for run in selected)
        if raw_proposed["equation_latex"] != old_latex[:offset] + fragment + old_latex[offset:]:
            raise ValueError("Boundary application must retain exactly one insertion")
        moved_refs = [ref for run in selected for ref in run["refs"]]
        if moved_refs != item["audit"]["moved_refs"]:
            raise ValueError("Boundary application changed selected reference ownership")
        expected_receiver, expected_donor = copy.deepcopy(receiver), copy.deepcopy(donor)
        expected_receiver["content"]["latex"] = raw_proposed["equation_latex"]
        expected_receiver["content"]["region"] = item["audit"]["derived_region"]
        expected_receiver["content"]["asset_id"] = uuid.uuid5(
            uuid.NAMESPACE_URL,
            document["paper_id"]
            + Region.model_validate(expected_receiver["content"]["region"]).model_dump_json(),
        ).hex
        expected_receiver["source_refs"] = item["audit"]["receiver_ref_order_after"]
        expected_donor["content"]["runs"][start:end] = []
        moved = set(item["audit"]["moved_refs"])
        expected_donor["source_refs"] = [ref for ref in donor["source_refs"] if ref not in moved]
        if proposed != {"receiver": expected_receiver, "donor": expected_donor}:
            raise ValueError("Boundary proposal changes fields outside its exact permitted delta")
        for role, block_id in (("receiver", receiver_id), ("donor", donor_id)):
            replacement = proposed[role]
            index = next(i for i, block in enumerate(result["blocks"]) if block["id"] == block_id)
            result["blocks"][index] = copy.deepcopy(replacement)
        touched.update((receiver_id, donor_id))
        audits.append(
            {
                "source_id": item["source_id"],
                "current": item["current"],
                "proposed": item["proposed"],
                **copy.deepcopy(item["audit"]),
            }
        )
    if _counter(result) != before_counter:
        raise ValueError("Boundary transfer must preserve global reference multiplicity")
    Document.model_validate(result)
    return result, audits
