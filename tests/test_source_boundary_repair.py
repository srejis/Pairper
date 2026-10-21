"""Boundary ownership transfer is atomic, narrow and original-source bound."""

import copy
import json
from collections import Counter

import pytest
from pairper.document.models import Block, Document, MathRun, Region, TextRun
from pairper.translation.contract import sha

from pairper.translation.source_boundary_repair import (
    apply_boundary_transfers,
    normalize_boundary_transfer,
)


@pytest.fixture
def original():
    spans = [
        {"id": "a", "text": "a=b", "bbox": [20, 20, 40, 28], "size": 8},
        {"id": "c", "text": "(c", "bbox": [40, 35, 48, 43], "size": 8},
        {"id": "fragment", "text": ")", "bbox": [50, 35, 52, 43], "size": 8},
        {"id": "last", "text": "=d", "bbox": [35, 50, 47, 58], "size": 8},
        {"id": "where", "text": "where ", "bbox": [15, 65, 50, 75], "size": 9},
        {"id": "mu", "text": "m", "bbox": [55, 65, 62, 75], "size": 9},
        {"id": "tail", "text": " is real.", "bbox": [65, 65, 110, 75], "size": 9},
    ]
    equation = Block(
        id="equation",
        type="equation",
        order=4,
        source_refs=["a", "c", "last"],
        content={
            "latex": r"\begin{aligned}a&=b\\&=(c\\&=d\end{aligned}",
            "region": Region(page=1, bbox=[18, 18, 49, 60]),
            "source_verified": True,
            "asset_id": "original-asset",
        },
    )
    donor = Block(
        id="paragraph",
        type="paragraph",
        order=5,
        source_refs=["fragment", "where", "mu", "tail"],
        content={
            "runs": [
                MathRun(
                    latex=")",
                    refs=["fragment"],
                    source_verified=True,
                    region=Region(page=1, bbox=[50, 35, 52, 43]),
                ),
                TextRun(refs=["where"]),
                MathRun(
                    latex=r"\mu",
                    refs=["mu"],
                    source_verified=True,
                    region=Region(page=1, bbox=[55, 65, 62, 75]),
                ),
                TextRun(refs=["tail"]),
            ]
        },
    )
    document = Document(
        paper_id="source-only", source_sha256="pdf-original", blocks=[equation, donor]
    ).model_dump(mode="json")
    bundle = {
        "paper_id": "source-only",
        "source_pdf_sha256": "pdf-original",
        "fingerprint": "source-fingerprint",
        "document": document,
        "units": [
            {
                "id": "paragraph/runs",
                "block_id": "paragraph",
                "field": "runs",
                "pages": [1],
                "source_refs": donor.source_refs,
            }
        ],
    }
    snapshot = {
        "sha256": "pdf-original",
        "pages": [{"page": 1, "width": 150, "height": 100, "spans": spans}],
    }
    return bundle, snapshot


def proposal(bundle, start=0, end=1):
    equation = next(block for block in bundle["document"]["blocks"] if block["id"] == "equation")
    donor = next(block for block in bundle["document"]["blocks"] if block["id"] == "paragraph")
    latex = equation["content"]["latex"]
    offset = latex.index("c") + 1
    return {
        "kind": "math_boundary_transfer",
        "source_id": "block:equation/latex",
        "current": json.dumps(
            {
                "equation_latex": latex,
                "donor_unit_id": "paragraph/runs",
                "start": start,
                "end": end,
                "donor_runs": donor["content"]["runs"][start:end],
            }
        ),
        "proposed": json.dumps(
            {"insert_offset": offset, "equation_latex": latex[:offset] + ")" + latex[offset:]}
        ),
    }


def normalized(original, finding=None):
    bundle, snapshot = original
    return normalize_boundary_transfer(
        bundle, finding or proposal(bundle), source_snapshot=snapshot
    )


def test_parenthesis_moves_once_with_whole_pair_review_and_original_refs(original):
    bundle, snapshot = original
    before = copy.deepcopy(bundle)
    finding = proposal(bundle)
    item = normalized(original, finding)
    assert json.loads(item["current"]) == {
        "receiver": bundle["document"]["blocks"][0],
        "donor": bundle["document"]["blocks"][1],
    }
    document, audit = apply_boundary_transfers(bundle["document"], [item])
    equation, donor = document["blocks"]
    assert equation["content"]["latex"].count(")") == 1
    assert equation["content"]["region"]["bbox"] == [18, 18, 52, 60]
    assert equation["source_refs"] == ["a", "c", "fragment", "last"]
    assert donor["source_refs"] == ["where", "mu", "tail"]
    assert donor["content"]["runs"] == before["document"]["blocks"][1]["content"]["runs"][1:]
    assert Counter(ref for block in document["blocks"] for ref in block["source_refs"]) == Counter(
        ref for block in before["document"]["blocks"] for ref in block["source_refs"]
    )
    assert (
        item["original_current"] == finding["current"]
        and item["original_proposed"] == finding["proposed"]
    )
    assert item["source_pages"] == [1] and item["receiver_block_id"] == "equation"
    assert audit[0]["requires_fresh_PDF_review"] and audit[0]["requires_full_source_validation"]
    assert not audit[0]["translation_approval_granted"]
    # Restore only the explicit allowed fields and prove all metadata stayed exact.
    restored = copy.deepcopy(document)
    restored["blocks"][0]["content"]["latex"] = before["document"]["blocks"][0]["content"]["latex"]
    restored["blocks"][0]["content"]["region"] = before["document"]["blocks"][0]["content"][
        "region"
    ]
    restored["blocks"][0]["source_refs"] = before["document"]["blocks"][0]["source_refs"]
    restored["blocks"][0]["content"]["asset_id"] = before["document"]["blocks"][0]["content"][
        "asset_id"
    ]
    restored["blocks"][1]["content"]["runs"] = before["document"]["blocks"][1]["content"]["runs"]
    restored["blocks"][1]["source_refs"] = before["document"]["blocks"][1]["source_refs"]
    assert restored == before["document"] and bundle == before


@pytest.mark.parametrize(
    "attack",
    [
        "changed_current_latex",
        "changed_current_runs",
        "changed_operator",
        "duplicate_fragment",
        "foreign_receiver",
        "foreign_unit",
        "middle_range",
        "text_range",
        "entire_donor",
        "bool_index",
        "offset_bool",
        "offset_outside",
        "unknown_proposed_field",
        "duplicate_json_key",
    ],
)
def test_only_exact_original_one_place_insertion_is_supported(original, attack):
    bundle, _ = original
    finding = proposal(bundle)
    current, proposed = json.loads(finding["current"]), json.loads(finding["proposed"])
    if attack == "changed_current_latex":
        current["equation_latex"] += " "
    if attack == "changed_current_runs":
        current["donor_runs"][0]["source_verified"] = False
    if attack == "changed_operator":
        proposed["equation_latex"] = proposed["equation_latex"].replace("=d", "=e")
    if attack == "duplicate_fragment":
        proposed["equation_latex"] += ")"
    if attack == "foreign_receiver":
        finding["source_id"] = "block:foreign/latex"
    if attack == "foreign_unit":
        current["donor_unit_id"] = "foreign/runs"
    if attack == "middle_range":
        current.update(
            start=2, end=3, donor_runs=bundle["document"]["blocks"][1]["content"]["runs"][2:3]
        )
    if attack == "text_range":
        current.update(end=2, donor_runs=bundle["document"]["blocks"][1]["content"]["runs"][:2])
    if attack == "entire_donor":
        current.update(end=4, donor_runs=bundle["document"]["blocks"][1]["content"]["runs"])
    if attack == "bool_index":
        current["start"] = False
    if attack == "offset_bool":
        proposed["insert_offset"] = True
    if attack == "offset_outside":
        proposed["insert_offset"] = 99999
    if attack == "unknown_proposed_field":
        proposed["delete_paragraph"] = True
    finding["current"], finding["proposed"] = json.dumps(current), json.dumps(proposed)
    if attack == "duplicate_json_key":
        finding["proposed"] = '{"insert_offset":0,"insert_offset":1,"equation_latex":"x"}'
    with pytest.raises(ValueError):
        normalized(original, finding)


@pytest.mark.parametrize(
    "attack",
    [
        "duplicate_owner",
        "foreign_ref",
        "cross_page",
        "remote",
        "outside_span",
        "partial_outside_span",
        "wrong_pdf",
        "wrong_region",
        "duplicate_refs",
        "equation_number_ref",
        "intervening_block",
        "wrong_boundary_direction",
    ],
)
def test_original_geometry_exclusive_ownership_and_order_gate(original, attack):
    bundle, snapshot = original
    equation, donor = bundle["document"]["blocks"]
    if attack == "duplicate_owner":
        equation["source_refs"].append("fragment")
    if attack == "foreign_ref":
        donor["content"]["runs"][0]["refs"] = ["foreign"]
    if attack == "cross_page":
        snapshot["pages"][0]["spans"].pop(2)
        snapshot["pages"].append(
            {"page": 2, "spans": [{"id": "fragment", "text": ")", "bbox": [50, 35, 52, 43]}]}
        )
    if attack == "remote":
        snapshot["pages"][0]["spans"][2]["bbox"] = [120, 35, 122, 43]
        donor["content"]["runs"][0]["region"]["bbox"] = [120, 35, 122, 43]
    if attack == "outside_span":
        snapshot["pages"][0]["spans"].append(
            {"id": "prose-overlap", "text": "other", "bbox": [50, 50, 51, 55]}
        )
    if attack == "partial_outside_span":
        snapshot["pages"][0]["spans"].append(
            {"id": "partially-covered", "text": "other", "bbox": [51, 58, 60, 65]}
        )
    if attack == "wrong_pdf":
        snapshot["sha256"] = "another-pdf"
    if attack == "wrong_region":
        donor["content"]["runs"][0]["region"]["bbox"][2] = 60
    if attack == "duplicate_refs":
        donor["content"]["runs"][0]["refs"].append("fragment")
    if attack == "equation_number_ref":
        donor["content"]["number_refs"] = ["fragment"]
    if attack == "intervening_block":
        donor["order"] = 6
        middle = Block(
            id="middle", type="paragraph", order=5, content={"runs": [TextRun(refs=["where"])]}
        ).model_dump(mode="json")
        bundle["document"]["blocks"].insert(1, middle)
    if attack == "wrong_boundary_direction":
        equation["order"], donor["order"] = 6, 5
    with pytest.raises(ValueError):
        normalized(original)


def test_suffix_can_move_to_following_equation_without_changing_other_runs(original):
    bundle, _ = original
    equation, donor = bundle["document"]["blocks"]
    donor["content"]["runs"].append(donor["content"]["runs"].pop(0))
    donor["source_refs"] = ["where", "mu", "tail", "fragment"]
    equation["order"], donor["order"] = 6, 5
    item = normalized(original, proposal(bundle, 3, 4))
    result, _ = apply_boundary_transfers(bundle["document"], [item])
    assert result["blocks"][1]["content"]["runs"] == donor["content"]["runs"][:3]
    assert result["blocks"][0]["source_refs"] == ["a", "c", "fragment", "last"]


@pytest.mark.parametrize(
    "attack", ["stale_donor", "stale_receiver", "normalization_drift", "duplicate_transfer"]
)
def test_apply_rejects_conflicts_and_drift_atomically(original, attack):
    bundle, _ = original
    item = normalized(original)
    document = copy.deepcopy(bundle["document"])
    if attack == "stale_donor":
        document["blocks"][1]["content"]["runs"][1]["before"] = "space"
    if attack == "stale_receiver":
        document["blocks"][0]["content"]["latex"] += " "
    if attack == "normalization_drift":
        item["audit"]["derived_region"]["bbox"][2] += 1
    before = copy.deepcopy(document)
    with pytest.raises(ValueError):
        apply_boundary_transfers(
            document, [item, item] if attack == "duplicate_transfer" else [item]
        )
    assert document == before


def test_explicit_delta_gate_rejects_unrelated_field_even_with_recomputed_integrity_hash(original):
    bundle, _ = original
    item = normalized(original)
    changed = json.loads(item["proposed"])
    changed["donor"]["parent_id"] = "other-parent"
    item["proposed"] = json.dumps(changed)
    item["normalization_sha256"] = sha(
        {k: v for k, v in item.items() if k != "normalization_sha256"}
    )
    with pytest.raises(ValueError, match="permitted delta"):
        apply_boundary_transfers(bundle["document"], [item])


def test_math_fragment_is_generic_not_fixed_parenthesis(original):
    bundle, snapshot = original
    bundle["document"]["blocks"][1]["content"]["runs"][0]["latex"] = "+q"
    snapshot["pages"][0]["spans"][2]["text"] = "+q"
    finding = proposal(bundle)
    current, proposed = json.loads(finding["current"]), json.loads(finding["proposed"])
    old = current["equation_latex"]
    offset = proposed["insert_offset"]
    proposed["equation_latex"] = old[:offset] + "+q" + old[offset:]
    finding["proposed"] = json.dumps(proposed)
    item = normalized(original, finding)
    document, _ = apply_boundary_transfers(bundle["document"], [item])
    assert "+q" in document["blocks"][0]["content"]["latex"]
