"""Coverage must not inherit false completeness from projected-DOM equality."""

from copy import deepcopy

import pytest

from tools.explanation_anchor_audit import aggregate_anchor_coverage, summarize_anchor_coverage


def target(tid, kind="term", **extra):
    return {"id": tid, "kind": kind, "source_text": tid, "field": "runs", **extra}


def test_unmapped_terms_and_text_math_remain_explicit_while_dispositions_are_separate():
    saved = {
        "targets": [
            target("visible"),
            target("shared"),
            target("missing-alias"),
            target("shadowed"),
            target("relation", "math", anchor_type="text_math", source_text="SR > 0"),
            target("notation", "math"),
            target("measurement", "math", field="cells/0"),
        ],
        "explanations": [
            {"target_id": "notation", "kind": "math", "math_role": "notation"},
            {"target_id": "measurement", "kind": "math", "math_role": "literal"},
        ],
    }
    projected = {
        "targets": [
            target("visible", localized=[{"text": "term"}]),
            target("shared", localized=[], shared_with_target_id="visible"),
            target("missing-alias", localized=[]),
            target("shadowed", localized=[]),
            target("relation", "math", localized=[]),
        ],
        "projection": {
            "unresolved_target_ids": ["missing-alias", "relation"],
            "formula_shadowed_only_target_ids": ["shadowed"],
            "formula_shadowed_occurrences": [{"target_id": "shadowed"}],
        },
    }
    original = deepcopy((saved, projected))
    report = summarize_anchor_coverage(saved, projected)
    assert (saved, projected) == original
    assert report["complete"] is False
    assert report["unresolved_counts_by_kind"] == {"term": 1, "math": 1, "unknown": 0}
    relation = next(row for row in report["unresolved_details"] if row["target_id"] == "relation")
    assert relation["source_text"] == "SR > 0" and relation["anchor_type"] == "text_math"
    excluded = report["intentional_exclusions"]
    assert excluded["resolved_shared_target_ids"] == ["shared"]
    assert excluded["formula_shadowed_only_target_ids"] == ["shadowed"]
    assert excluded["notation_target_ids"] == ["notation"]
    assert excluded["table_literal_target_ids"] == ["measurement"]
    assert report["backend_projection"] == projected["projection"]
    assert not report["diagnostic_problems"]


def test_ofa_shaped_424_to_406_is_one_shared_plus_17_unresolved_not_complete():
    saved = {"targets": [target(f"t{i}") for i in range(424)]}
    projected = {
        "targets": [target(f"t{i}", localized=[{}]) for i in range(406)]
        + [target("t406", localized=[], shared_with_target_id="t0")]
        + [target(f"t{i}", localized=[]) for i in range(407, 424)],
        "projection": {"unresolved_target_ids": [f"t{i}" for i in range(407, 424)]},
    }
    report = summarize_anchor_coverage(saved, projected)
    assert report["visible_target_count"] == 406
    assert report["unresolved_target_count"] == 17
    assert report["unresolved_counts_by_kind"]["term"] == 17
    assert report["intentional_exclusions"]["resolved_shared_target_count"] == 1
    aggregate = aggregate_anchor_coverage(
        [{"paper_id": "ofa", "korean_bindings": {"projection_coverage": report}}]
    )
    assert aggregate["complete"] is False and aggregate["enforced"] is False
    assert aggregate["unresolved_target_language_pairs"] == 17


def test_missing_diagnostics_unknown_removal_and_broken_owner_cannot_look_complete():
    saved = {"targets": [target("lost"), target("orphan")]}
    report = summarize_anchor_coverage(
        saved, {"targets": [target("orphan", localized=[], shared_with_target_id="absent")]}
    )
    assert report["complete"] is False
    assert report["unresolved_target_ids"] == ["lost", "orphan"]
    assert report["diagnostic_problems"]
    assert report["intentional_exclusions"]["resolved_shared_target_count"] == 0


def test_visible_and_resolved_shared_complete_but_empty_evidence_is_not_claimed():
    saved = {"targets": [target("owner"), target("shared")]}
    projected = {
        "targets": [
            target("owner", localized=[{}]),
            target("shared", localized=[], shared_with_target_id="owner"),
        ],
        "projection": {"unresolved_target_ids": []},
    }
    assert summarize_anchor_coverage(saved, projected)["complete"] is True
    assert summarize_anchor_coverage({"targets": []}, {"targets": []})["complete"] is None
    assert aggregate_anchor_coverage([])["complete"] is None


def test_backend_collision_or_hidden_omission_is_reported_without_an_allowlist():
    saved = {"targets": [target("a"), target("b")]}
    projected = {
        "targets": [target("a", localized=[]), target("b", localized=[])],
        "projection": {"unresolved_target_ids": ["a"], "alias_collision_target_ids": ["a"]},
    }
    report = summarize_anchor_coverage(saved, projected)
    assert report["unresolved_target_ids"] == ["a", "b"]
    assert report["backend_projection"]["alias_collision_target_ids"] == ["a"]
    assert report["diagnostic_problems"] == [
        "Hidden targets are absent from backend unresolved diagnostics"
    ]
    # An ambiguous occurrence may coexist with another visible occurrence of
    # the same target. Zero wholly unresolved targets is still not full coverage.
    projected["targets"] = [target("a", localized=[{}]), target("b", localized=[{}])]
    projected["projection"]["unresolved_target_ids"] = []
    partial = summarize_anchor_coverage(saved, projected)
    assert partial["unresolved_target_count"] == 0 and partial["complete"] is False
    assert partial["alias_collision_target_count"] == 1


def contained_fixture():
    anchor = {
        "block_id": "translated",
        "field": "runs",
        "run_index": 0,
        "start": 8,
        "end": 15,
        "text": "Hessian",
    }
    owner_anchor = {**anchor, "start": 0, "text": "inverse Hessian"}
    short = target("short", block_id="source", concept_key="hessian", localized=[])
    owner = target(
        "owner", block_id="source", concept_key="inverse_hessian", localized=[owner_anchor]
    )
    saved = {
        "targets": [short, owner],
        "explanations": [
            {"target_id": tid, "kind": "term", "reviewed": True} for tid in ("short", "owner")
        ],
    }
    projected = {
        "targets": deepcopy([short, owner]),
        "projection": {
            "unresolved_target_ids": ["short"],
            "unmapped_target_ids": [],
            "contained_alias_shadowed_only_target_ids": ["short"],
            "contained_alias_shadowed_occurrences": [
                {
                    "target_id": "short",
                    "anchor": anchor,
                    "owner_target_id": "owner",
                    "owner_anchor": owner_anchor,
                    "reason": "strict_containment_by_distinct_reviewed_concept",
                }
            ],
        },
    }
    return saved, projected


def test_proven_contained_alias_preserves_raw_unresolved_but_is_not_true_unmapped():
    saved, projected = contained_fixture()
    original = deepcopy(projected)
    report = summarize_anchor_coverage(saved, projected)
    assert projected == original
    assert report["unresolved_target_ids"] == ["short"]
    assert report["unresolved_counts_by_kind"]["term"] == 1
    assert report["unmapped_target_ids"] == [] and report["unmapped_target_count"] == 0
    assert report["intentional_exclusions"]["contained_alias_shadowed_only_target_ids"] == ["short"]
    assert report["complete"] is True and not report["diagnostic_problems"]
    aggregate = aggregate_anchor_coverage(
        [{"paper_id": "slice", "korean_bindings": {"projection_coverage": report}}]
    )
    assert aggregate["unresolved_target_language_pairs"] == 1
    assert aggregate["unmapped_target_language_pairs"] == 0


@pytest.mark.parametrize(
    "defect",
    [
        "equal",
        "partial",
        "no-owner",
        "missing-owner",
        "no-row",
        "same-concept",
        "not-reviewed",
        "different-unit",
    ],
)
def test_unproven_containment_metadata_never_excuses_missing_or_ambiguous_aliases(defect):
    saved, projected = contained_fixture()
    diagnostic = projected["projection"]
    row = diagnostic["contained_alias_shadowed_occurrences"][0]
    if defect == "equal":
        row["anchor"] = deepcopy(row["owner_anchor"])
    elif defect == "partial":
        row["anchor"]["end"] = 18
    elif defect == "no-owner":
        projected["targets"][1]["localized"] = []
    elif defect == "missing-owner":
        projected["targets"].pop()
    elif defect == "no-row":
        diagnostic["contained_alias_shadowed_occurrences"] = []
    elif defect == "same-concept":
        projected["targets"][1]["concept_key"] = "hessian"
    elif defect == "not-reviewed":
        saved["explanations"][1]["reviewed"] = False
    else:
        projected["targets"][1]["block_id"] = "other-source"
    report = summarize_anchor_coverage(saved, projected)
    assert "short" in report["unmapped_target_ids"]
    assert report["complete"] is False
    assert report["intentional_exclusions"]["contained_alias_shadowed_only_target_ids"] == []
    assert report["diagnostic_problems"]
