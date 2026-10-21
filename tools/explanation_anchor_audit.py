"""Read-only coverage reporting, separate from successful projected-DOM rendering."""

from copy import deepcopy


def summarize_anchor_coverage(saved, projected):
    """Expose missing anchors without treating intentional sharing as a defect.

    No source literal absence is silently excused here. In particular, a text
    formula missing from a translation remains explicitly unresolved for review.
    """
    original = {target["id"]: target for target in saved.get("targets", [])}
    targets = {target["id"]: target for target in projected.get("targets", [])}
    entries = {entry["target_id"]: entry for entry in saved.get("explanations", [])}
    raw = deepcopy(projected.get("projection"))
    diagnostics = raw if isinstance(raw, dict) else {}
    visible = {tid for tid, target in targets.items() if target.get("localized")}
    tables = {row["block_id"] for row in saved.get("context", []) if row["type"] == "table"}

    def kind(tid):
        return original.get(tid, targets.get(tid, {})).get(
            "kind", entries.get(tid, {}).get("kind", "unknown")
        )

    def grouped(ids):
        return {
            key: sorted(tid for tid in ids if kind(tid) == key)
            for key in ("term", "math", "unknown")
        }

    def visible_owner(tid, visited=None):
        visited = set() if visited is None else visited
        if tid in visible:
            return tid
        if tid not in targets or tid in visited:
            return None
        return visible_owner(targets[tid].get("shared_with_target_id"), visited | {tid})

    shared = [
        {
            "target_id": tid,
            "owner_target_id": target["shared_with_target_id"],
            "visible_owner_target_id": visible_owner(tid),
        }
        for tid, target in targets.items()
        if target.get("shared_with_target_id")
    ]
    resolved_shared = {row["target_id"] for row in shared if row["visible_owner_target_id"]}
    formula_only = set(diagnostics.get("formula_shadowed_only_target_ids", []))
    removed = set(original) - set(targets)
    notation = {
        tid
        for tid in removed
        if entries.get(tid, {}).get("kind") == "math"
        and entries[tid].get("math_role") == "notation"
    }
    table_literals = {
        tid
        for tid in removed
        if entries.get(tid, {}).get("kind") == "math"
        and entries[tid].get("math_role") == "literal"
        and (
            original[tid].get("field", "").startswith("cells/")
            or original[tid].get("block_id") in tables
        )
    }
    unknown_removed = removed - notation - table_literals
    backend_unresolved = set(diagnostics.get("unresolved_target_ids", []))
    collisions = set(diagnostics.get("alias_collision_target_ids", []))
    unexplained_hidden = set(targets) - visible - resolved_shared - formula_only
    unresolved = backend_unresolved | unexplained_hidden | unknown_removed
    problems = []

    def surviving_containment(row):
        tid, owner_id = row.get("target_id"), row.get("owner_target_id")
        loser, owner = targets.get(tid, {}), targets.get(owner_id, {})
        a, b = row.get("anchor", {}), row.get("owner_anchor", {})
        if not (
            row.get("reason") == "strict_containment_by_distinct_reviewed_concept"
            and loser.get("kind") == owner.get("kind") == "term"
            and loser.get("concept_key")
            and owner.get("concept_key")
            and loser["concept_key"] != owner["concept_key"]
            and entries.get(tid, {}).get("reviewed")
            and entries.get(owner_id, {}).get("reviewed")
            and all(loser.get(key) == owner.get(key) for key in ("block_id", "field"))
            and b in owner.get("localized", [])
            and all(a.get(key) == b.get(key) for key in ("block_id", "field", "run_index"))
            and all(
                isinstance(value, int)
                for value in (a.get("start"), a.get("end"), b.get("start"), b.get("end"))
            )
        ):
            return False
        return (
            0 <= b["start"] <= a["start"] < a["end"] <= b["end"]
            and (a["start"], a["end"]) != (b["start"], b["end"])
            and isinstance(a.get("text"), str)
            and isinstance(b.get("text"), str)
            and b["text"][a["start"] - b["start"] : a["end"] - b["start"]] == a["text"]
        )

    contained_rows = diagnostics.get("contained_alias_shadowed_occurrences", [])
    claimed_contained = set(diagnostics.get("contained_alias_shadowed_only_target_ids", []))
    proven_contained = {
        tid
        for tid in claimed_contained
        if tid in backend_unresolved
        and tid not in collisions
        and tid not in visible
        and tid not in resolved_shared
        and any(row.get("target_id") == tid for row in contained_rows)
        and all(surviving_containment(row) for row in contained_rows if row.get("target_id") == tid)
    }
    invalid_contained = claimed_contained - proven_contained
    unmapped = unresolved - proven_contained
    if invalid_contained:
        problems.append("Contained-alias exclusion lacks valid surviving reviewed-owner evidence")
    if (
        "unmapped_target_ids" in diagnostics
        and set(diagnostics["unmapped_target_ids"]) != backend_unresolved - proven_contained
    ):
        problems.append(
            "Backend unmapped IDs disagree with proven intentional contained-alias exclusions"
        )
    if raw is None and original:
        problems.append("Backend projection diagnostics are missing")
    if backend_unresolved & (visible | resolved_shared | formula_only):
        problems.append("Backend unresolved IDs conflict with visible/shared/suppressed IDs")
    if unexplained_hidden - backend_unresolved:
        problems.append("Hidden targets are absent from backend unresolved diagnostics")
    if unknown_removed:
        problems.append("Published targets were removed without a known disposition")
    if backend_unresolved - set(original):
        problems.append("Backend unresolved IDs are absent from the saved bundle")
    groups = grouped(unresolved)
    unmapped_groups = grouped(unmapped)
    complete = not unmapped and not problems and not collisions if original else None
    return {
        "scope": "Saved-target anchor availability and backend collisions; independent of rendering success; no dictionary-completeness claim",
        "saved_revision": saved.get("revision"),
        "source_fingerprint": saved.get("fingerprint"),
        "source_identity": deepcopy(saved.get("source_identity")),
        "complete": complete,
        "status": "not_applicable" if not original else "complete" if complete else "incomplete",
        "saved_target_count": len(original),
        "projected_target_count": len(targets),
        "visible_target_count": len(visible),
        "visible_occurrence_count": sum(len(t.get("localized", [])) for t in targets.values()),
        "unresolved_target_count": len(unresolved),
        "unresolved_target_ids": sorted(unresolved),
        "unresolved_definition": "Raw backend unresolved plus conservative audit omissions; includes proven contained-alias dispositions",
        "unresolved_ids_by_kind": groups,
        "unresolved_counts_by_kind": {key: len(ids) for key, ids in groups.items()},
        "unmapped_target_count": len(unmapped),
        "unmapped_target_ids": sorted(unmapped),
        "unmapped_definition": "Unresolved minus validated intentional contained-alias dispositions; missing literals are never automatically excused",
        "unmapped_ids_by_kind": unmapped_groups,
        "unmapped_counts_by_kind": {key: len(ids) for key, ids in unmapped_groups.items()},
        "alias_collision_target_ids": sorted(collisions),
        "alias_collision_target_count": len(collisions),
        "unresolved_details": [
            {
                "target_id": tid,
                "kind": kind(tid),
                **{
                    key: original.get(tid, targets.get(tid, {})).get(key)
                    for key in ("source_text", "anchor_type", "block_id", "field", "aliases")
                },
            }
            for tid in sorted(unresolved)
        ],
        "intentional_exclusions": {
            "resolved_shared_target_count": len(resolved_shared),
            "resolved_shared_target_ids": sorted(resolved_shared),
            "shared_relationships": shared,
            "formula_shadowed_only_target_ids": sorted(formula_only),
            "formula_shadowed_only_target_count": len(formula_only),
            "formula_shadowed_occurrences": diagnostics.get("formula_shadowed_occurrences", []),
            "notation_target_ids": sorted(notation),
            "notation_target_count": len(notation),
            "table_literal_target_ids": sorted(table_literals),
            "table_literal_target_count": len(table_literals),
            "contained_alias_shadowed_only_target_ids": sorted(proven_contained),
            "contained_alias_shadowed_only_target_count": len(proven_contained),
            "contained_alias_shadowed_occurrences": deepcopy(contained_rows),
            "invalid_contained_shadowed_target_ids": sorted(invalid_contained),
        },
        "diagnostic_problems": problems,
        "backend_projection": raw,
    }


def aggregate_anchor_coverage(rows):
    reports = [
        {"paper_id": row["paper_id"], "language": language, **row[key]["projection_coverage"]}
        for row in rows
        for language, key in (("source", "source_bindings"), ("korean", "korean_bindings"))
        if key in row and "projection_coverage" in row[key]
    ]
    return {
        "enforced": False,
        "complete": all(row["complete"] is True for row in reports) if reports else None,
        "scope": "Rendering PASS does not certify complete source/Korean anchor coverage",
        "unresolved_target_language_pairs": sum(row["unresolved_target_count"] for row in reports),
        "unmapped_target_language_pairs": sum(row["unmapped_target_count"] for row in reports),
        "reports": reports,
    }
