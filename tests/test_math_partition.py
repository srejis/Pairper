"""Synthetic proposals test mechanics only; no native semantic approval claim."""

import copy

import pytest
from pairper.translation.contract import sha
from pairper.translation.math_contract import text_sha

from pairper.translation.math_partition import (
    VERSION,
    normalize_partition_plan,
    validate_partition_target,
    verify_partition_review,
)


def fixture(latex="x,y"):
    b = {
        "fingerprint": "synthetic-source",
        "source_pdf_sha256": "synthetic-pdf",
        "view_id": "synthetic-view",
        "units": [
            {
                "id": "u/runs",
                "math": [{"token": "⟦u/runs#M0⟧", "latex": latex, "refs": ["one-original-span"]}],
            }
        ],
        "document": {"blocks": []},
    }
    p = {
        "version": VERSION,
        "source_bundle_fingerprint": b["fingerprint"],
        "parents": [
            {
                "math_id": "⟦u/runs#M0⟧",
                "source_latex_sha256": text_sha(latex),
                "segments": [
                    {"start": 0, "end": 1, "role": "math"},
                    {"start": 1, "end": 2, "role": "narrative_separator"},
                    {"start": 2, "end": 3, "role": "math"},
                ],
            }
        ],
    }
    return b, p


def accepted(b, m):
    review = {
        "manifest_sha256": m["manifest_sha256"],
        "issues": [],
        "items": [
            {
                "math_id": p["math_id"],
                "verdict": "pass",
                "source_compared": True,
                "narrative_separators_confirmed": True,
                "checks": ["Synthetic fake review, not original-PDF evidence."],
            }
            for p in m["parents"]
        ],
    }
    return verify_partition_review(
        b, m, review, proposal_call_id="fake-proposal", review_call_id="fake-review"
    )


def test_real_katex_partition_proof_and_source_immutability():
    b, p = fixture()
    before = copy.deepcopy((b, p))
    m = normalize_partition_plan(b, p)
    assert before == (b, p) and m["semantic_review_performed"] is False
    assert m["parents"][0]["proof"]["mathematical_ast_equal"]
    assert "".join(s["source_latex"] for s in m["parents"][0]["segments"]) == "x,y"
    assert len({s["alias"] for s in m["parents"][0]["segments"]}) == 3
    reviewed = accepted(b, m)
    aliases = [s["alias"] for s in m["parents"][0]["segments"]]
    validate_partition_target(reviewed, "u/runs", aliases[0] + " first " + aliases[1] + aliases[2])
    assert reviewed["translation_approval_granted"] is False


@pytest.mark.parametrize(
    "attack",
    ["stale", "gap", "overlap", "reorder", "empty", "wrongrole", "foreign", "duplicate", "partial"],
)
def test_partition_structural_attacks_fail_before_review(attack):
    b, p = fixture()
    item = p["parents"][0]
    if attack == "stale":
        item["source_latex_sha256"] = "stale"
    if attack == "gap":
        item["segments"][1]["start"] = 2
    if attack == "overlap":
        item["segments"][1]["start"] = 0
    if attack == "reorder":
        item["segments"].reverse()
    if attack == "empty":
        item["segments"][1]["end"] = 1
    if attack == "wrongrole":
        item["segments"][1]["role"] = "mathematical_comma"
    if attack == "foreign":
        item["math_id"] = "foreign"
    if attack == "duplicate":
        p["parents"].append(copy.deepcopy(item))
    if attack == "partial":
        item["segments"][-1]["end"] = 2
    with pytest.raises(ValueError):
        normalize_partition_plan(b, p)


def test_group_or_command_cannot_be_cut_even_when_substrings_exist():
    b, p = fixture("(x,y)")
    p["parents"][0]["segments"] = [
        {"start": 0, "end": 2, "role": "math"},
        {"start": 2, "end": 3, "role": "narrative_separator"},
        {"start": 3, "end": 5, "role": "math"},
    ]
    with pytest.raises(ValueError, match="lexeme or group"):
        normalize_partition_plan(b, p)


def test_same_call_and_unresolved_or_tampered_reviews_never_authorize():
    b, p = fixture()
    m = normalize_partition_plan(b, p)
    reviewed = accepted(b, m)
    review = reviewed["review"]
    with pytest.raises(ValueError, match="Separate"):
        verify_partition_review(b, m, review, proposal_call_id="same", review_call_id="same")
    review["items"][0]["verdict"] = "unresolved"
    with pytest.raises(ValueError):
        verify_partition_review(b, m, review, proposal_call_id="fake-p", review_call_id="fake-r")
    m["parents"][0]["segments"][0]["source_latex"] = "tampered"
    with pytest.raises(ValueError, match="changed"):
        verify_partition_review(b, m, review, proposal_call_id="fake-p", review_call_id="fake-r")


@pytest.mark.parametrize("attack", ["missing", "duplicate", "reordered", "parent"])
def test_target_alias_coverage_and_order(attack):
    b, p = fixture()
    m = normalize_partition_plan(b, p)
    r = accepted(b, m)
    aliases = [s["alias"] for s in m["parents"][0]["segments"]]
    target = {
        "missing": "".join(aliases[:-1]),
        "duplicate": "".join(aliases + aliases[:1]),
        "reordered": "".join(reversed(aliases)),
        "parent": "".join(aliases) + m["parents"][0]["math_id"],
    }[attack]
    with pytest.raises(ValueError):
        validate_partition_target(r, "u/runs", target)


def complete_fixture():
    b, p = fixture()
    u = b["units"][0]
    u.update(
        block_id="u",
        field="runs",
        type="paragraph",
        order=0,
        parent_id=None,
        pages=[1],
        source_text=u["math"][0]["token"],
        source_refs=["one-original-span"],
    )
    u["source_text_sha256"] = text_sha(u["source_text"])
    u["fingerprint"] = sha(u)
    block = {
        "id": "u",
        "type": "paragraph",
        "order": 0,
        "parent_id": None,
        "relations": [],
        "source_refs": ["one-original-span"],
        "content": {
            "runs": [
                {
                    "kind": "math",
                    "latex": "x,y",
                    "refs": ["one-original-span"],
                    "source_verified": True,
                    "region": {"page": 1, "bbox": [1, 1, 20, 10], "rotation": 0},
                    "image_url": "file:///original-parent.png",
                }
            ]
        },
    }
    b.update(paper_id="synthetic-paper", figures=[], math_text_inventory=[])
    b["document"] = {
        "paper_id": b["paper_id"],
        "source_sha256": b["source_pdf_sha256"],
        "blocks": [block],
    }
    b["fingerprint"] = sha({k: v for k, v in b.items() if k != "fingerprint"})
    p["source_bundle_fingerprint"] = b["fingerprint"]
    return b, p


def test_optin_adapter_preserves_source_and_projects_without_duplicate_parent_fallback():
    from pairper.translation.contract import Entry

    from pairper.translation.math_partition import (
        build_partitioned_bundle,
        project_partition_display,
    )

    b, p = complete_fixture()
    before = copy.deepcopy(b)
    m = normalize_partition_plan(b, p)
    r = accepted(b, m)
    derived = build_partitioned_bundle(b, r)
    assert b == before and derived["document"] == b["document"]
    assert derived["source_bundle_fingerprint"] == b["fingerprint"]
    u = derived["units"][0]
    aliases = [s["alias"] for s in m["parents"][0]["segments"]]
    target = aliases[0] + " with " + aliases[1] + aliases[2]
    result = {
        "bundle_fingerprint": derived["fingerprint"],
        "entries": [
            Entry(
                source_unit_ids=[u["id"]],
                source_fingerprint=sha([u["fingerprint"]]),
                action="translate",
                target=target,
            ).model_dump()
        ],
        "figure_reviews": [],
        "math_roles": [],
        "math_overlays": [],
    }
    base = {
        "paper_id": b["paper_id"],
        "view_id": b["view_id"],
        "blocks": copy.deepcopy(b["document"]["blocks"]),
    }
    projected = project_partition_display(b, r, result, base)
    maths = [x for x in projected["blocks"][0]["content"]["runs"] if x["kind"] == "math"]
    assert len(maths) == 3 and all(x["image_url"] is None for x in maths)
    assert all(x["partition_parent"]["image_url"] == "file:///original-parent.png" for x in maths)
    assert not projected["translation"]["complete"]  # Still requires fresh bilingual review.
    assert b == before
    with pytest.raises(ValueError, match="overlays"):
        build_partitioned_bundle(b, r, overlays=[{"math_id": m["parents"][0]["math_id"]}])
    with pytest.raises(ValueError, match="not bound"):
        project_partition_display(b, r, dict(result, bundle_fingerprint="legacy"), base)


def test_fragment_identity_cannot_leak_from_another_unit():
    b, p = fixture()
    r = accepted(b, normalize_partition_plan(b, p))
    with pytest.raises(ValueError, match="Foreign"):
        validate_partition_target(
            r, "another-unit", r["manifest"]["parents"][0]["segments"][0]["alias"]
        )


def test_final_narrative_period_can_be_protected_separately_without_source_edit():
    b, p = fixture("x.")
    p["parents"][0]["segments"] = p["parents"][0]["segments"][:2]
    before = copy.deepcopy(b)
    m = normalize_partition_plan(b, p)
    r = accepted(b, m)
    a = [s["alias"] for s in m["parents"][0]["segments"]]
    validate_partition_target(r, "u/runs", a[0] + " predicate" + a[1])
    assert b == before and "".join(s["source_latex"] for s in m["parents"][0]["segments"]) == "x."


def test_manifest_refs_are_detached_and_review_checks_must_be_a_list():
    b, p = fixture()
    m = normalize_partition_plan(b, p)
    before = copy.deepcopy(b)
    m["parents"][0]["parent_source_refs"].append("caller-mutation")
    assert b == before
    m = normalize_partition_plan(b, p)
    r = accepted(b, m)
    review = r["review"]
    review["items"][0]["checks"] = "abc"
    with pytest.raises(ValueError):
        verify_partition_review(b, m, review, proposal_call_id="fake-p", review_call_id="fake-r")
