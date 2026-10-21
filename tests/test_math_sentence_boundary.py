"""Source-only format guard, not semantic approval or source punctuation repair."""

import copy
import hashlib
import json

import pytest
from test_translation_workflow import draft, engine, review, sha, submit
from test_translation_workflow import setup_workflow as setup_workflow

from pairper.translation.math_boundary import (
    terminal_sentence_period,
    validate_math_sentence_boundaries,
)
from pairper.translation.prompts import validate_protocol


@pytest.mark.parametrize(
    "latex, expected",
    [
        (r"b\times c.", True),
        ("(d).", True),
        ("v. ", True),
        (r"x\.", False),
        ("3.14", False),
        ("a.b", False),
        (r"\text{end.}", False),
        (r"\right.", False),
        (r"\bigl.", False),
        ("n!", False),
        (r"\ldots", False),
        (r"{x.", False),
        (r"x\\.", True),
    ],
)
def test_literal_terminal_period_only(latex, expected):
    assert terminal_sentence_period(latex) is expected


def unit(latex="v."):
    return {
        "id": "u/runs",
        "source_text": "Value ⟦u/runs#M0⟧",
        "math": [{"token": "⟦u/runs#M0⟧", "latex": latex}],
    }


@pytest.mark.parametrize("phase", ["generate", "repair"])
def test_protocol_and_guard_report_exact_source_boundary_without_mutation(phase):
    u = unit()
    target = "값 ⟦u/runs#M0⟧인 것"
    before = copy.deepcopy(u)
    request = {"phase": phase, "kind": "text", "task": {"units": [u]}}
    response = {"items": [{"unit_id": u["id"], "action": "translate", "target": target}]}
    with pytest.raises(ValueError, match="source_latex=.*v.*target_boundary"):
        validate_protocol(request, response)
    assert u == before and response["items"][0]["target"] == target


@pytest.mark.parametrize(
    "target",
    [
        "값 ⟦u/runs#M0⟧",
        "값 ⟦u/runs#M0⟧ 다음 문장",
        "값 ⟦u/runs#M0⟧. 다음",
        "값 ⟦u/runs#M0⟧ English",
    ],
)
def test_no_direct_hangul_attachment_is_outside_narrow_guard(target):
    validate_math_sentence_boundaries(unit(), target)


def test_current_source_controls_decision_and_all_occurrences_checked():
    target = "⟦u/runs#M0⟧ 다음 ⟦u/runs#M0⟧인"
    validate_math_sentence_boundaries(unit("3.14"), target)
    with pytest.raises(ValueError, match="Opaque math"):
        validate_math_sentence_boundaries(unit(), target)


def boundary_fixture(setup_workflow):
    workflow, service = setup_workflow(max_repairs=3)
    b = service.bundle
    u = b["units"][0]
    b["units"] = [u]
    b["document"]["blocks"] = [x for x in b["document"]["blocks"] if x["id"] == u["block_id"]]
    token = "⟦" + u["id"] + "#M0⟧"
    u["math"] = [{"token": token, "latex": "v.", "refs": u["source_refs"]}]
    u["source_text"] = "Value " + token
    u["source_text_sha256"] = hashlib.sha256(u["source_text"].encode()).hexdigest()
    u["upstream_source_text_sha256"] = u["source_text_sha256"]
    u["fingerprint"] = sha({k: v for k, v in u.items() if k != "fingerprint"})
    b["document"]["blocks"][0]["content"]["runs"] = [
        {
            "kind": "math",
            "latex": "v.",
            "refs": u["source_refs"],
            "region": {"page": 1, "bbox": [1, 1, 10, 10], "rotation": 0},
            "source_verified": True,
        }
    ]
    b["fingerprint"] = sha({k: v for k, v in b.items() if k != "fingerprint"})
    return workflow, service, u, token


@pytest.mark.parametrize("verdict", ["pass", "repair"])
def test_old_accepted_cache_is_invalidated_budget_preserved_and_fresh_review_required(
    setup_workflow, monkeypatch, verdict
):
    w, s, u, token = boundary_fixture(setup_workflow)
    run = w.start(s.bundle["paper_id"], "legacy-boundary")
    c = w.claim(run["run_id"])
    bad = draft(c)
    bad["items"][0]["target"] = "값 " + token + "인 것"
    # Deterministically seed the historical pre-gate state in temporary SQLite.
    with monkeypatch.context() as patch:
        patch.setattr(engine, "validate_math_sentence_boundaries", lambda *_: None)
        import pairper.translation.prompts as prompts

        patch.setattr(prompts, "validate_math_sentence_boundaries", lambda *_: None)
        submit(w, c, bad)
        c = w.claim(run["run_id"])
        submit(w, c, review(c))
    with w.connect() as con:
        con.execute("UPDATE tasks SET repair_count=3 WHERE run_id=?", (run["run_id"],))
    assert w._normalized_cache
    regenerated = w.claim(run["run_id"])
    assert regenerated["phase"] == "generate"
    assert (
        "Opaque math sentence-period boundary"
        in regenerated["request"]["validation_feedback"]["error"]
    )
    with w.connect() as con:
        row = w._rows(con, run["run_id"])[0]
        assert row["candidate"] is None and row["review"] is None and row["repair_count"] == 3
        event = con.execute(
            "SELECT payload FROM events WHERE event='candidate_invalidated_by_validation'"
        ).fetchone()
        assert json.loads(event[0])["previous_candidate"] == bad
    good = draft(regenerated)
    good["items"][0]["target"] = "값은 다음과 같다: " + token
    submit(w, regenerated, good)
    with pytest.raises(ValueError):
        w.commit(run["run_id"])
    fresh = w.claim(run["run_id"])
    assert fresh["phase"] == "review"
    submit(w, fresh, review(fresh, verdict))
    with w.connect() as con:
        row = w._rows(con, run["run_id"])[0]
        assert (
            row["status"] == ("accepted" if verdict == "pass" else "blocked")
            and row["repair_count"] == 3
        )
