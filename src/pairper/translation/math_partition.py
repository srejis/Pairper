"""Opt-in translation-only math partitions. Original source objects are never changed.

Native proposal/review orchestration must bind the returned immutable manifest to
fresh source-only calls. This module cannot grant semantic approval by itself.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

if __package__:
    from .contract import TOKEN, sha
    from .math_contract import source_math, text_sha

VERSION = "immutable-math-partition-v2"
MAX_PARENTS = 30
MAX_SEGMENTS = 16
MAX_PARENT_CHARS = 18000


def _run_proof(parent, fragments):
    import pairper

    resource = Path(pairper.__file__).resolve().parent / "web/katex/katex.min.js"
    request = {"parent": parent, "fragments": fragments, "resource": str(resource)}
    process = subprocess.run(
        [sys.executable, __file__, "--worker"],
        input=json.dumps(request, ensure_ascii=True).encode(),
        capture_output=True,
        timeout=15,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if process.returncode:
        raise ValueError(
            "Partition renderer proof unavailable: "
            + process.stderr.decode("utf8", "replace")[-1000:]
        )
    proof = json.loads(process.stdout)
    if not proof.get("success"):
        raise ValueError("Unsafe mathematical partition: " + "; ".join(proof.get("errors", [])))
    ast = proof.pop("ast_json")
    proof["mathematical_ast_sha256"] = text_sha(ast)
    proof["renderer_sha256"] = sha(
        [
            text_sha(resource.read_text("utf8")),
            text_sha(Path(__file__).with_name("math_partition_worker.js").read_text("utf8")),
        ]
    )
    return proof


@lru_cache(maxsize=256)
def _cached_proof(parent, fragments, renderer_identity):
    return json.dumps(_run_proof(parent, fragments), sort_keys=True)


def _proof(parent, fragments):
    import pairper

    resource = Path(pairper.__file__).resolve().parent / "web/katex/katex.min.js"
    renderer = sha(
        [
            text_sha(resource.read_text("utf8")),
            text_sha(Path(__file__).with_name("math_partition_worker.js").read_text("utf8")),
        ]
    )
    return json.loads(_cached_proof(parent, tuple(fragments), renderer))


def _lexeme_boundaries(latex):
    # Conservative TeX lexical boundaries. Groups/commands/parentheses may not
    # be cut even if the isolated pieces happen to parse independently.
    boundaries = {0}
    groups = []
    i = 0
    while i < len(latex):
        char = latex[i]
        if char == "\\":
            match = re.match(r"\\[A-Za-z]+|\\.", latex[i:])
            if not match:
                raise ValueError("Unfinished TeX control sequence")
            i += len(match.group())
        else:
            if char in "{([":
                groups.append(char)
            elif char in "})]":
                if not groups or groups.pop() != {"}": "{", ")": "(", "]": "["}[char]:
                    raise ValueError("Unbalanced mathematical grouping")
            i += 1
        if not groups:
            boundaries.add(i)
    if groups:
        raise ValueError("Unbalanced mathematical grouping")
    return boundaries


def normalize_partition_plan(bundle, proposal):
    """Validate AI-proposed source ranges; never choose semantic split points."""
    if not isinstance(proposal, dict) or set(proposal) != {
        "version",
        "source_bundle_fingerprint",
        "parents",
    }:
        raise ValueError("Exact partition proposal schema required")
    if (
        proposal["version"] != VERSION
        or proposal["source_bundle_fingerprint"] != bundle["fingerprint"]
    ):
        raise ValueError("Stale or unsupported partition source")
    parents = proposal["parents"]
    if not isinstance(parents, list) or not 1 <= len(parents) <= MAX_PARENTS:
        raise ValueError("Partition needs 1..30 explicitly proposed source parents")
    originals = source_math(bundle)
    seen = set()
    normalized = []
    total_source_chars = 0
    for item in parents:
        if set(item) != {"math_id", "source_latex_sha256", "segments"}:
            raise ValueError("Exact parent partition schema required")
        mid = item["math_id"]
        if mid in seen or mid not in originals or mid.startswith("block:"):
            raise ValueError("Duplicate, foreign or standalone partition parent")
        seen.add(mid)
        parent = originals[mid]
        total_source_chars += len(parent)
        if total_source_chars > MAX_PARENT_CHARS:
            raise ValueError("Partition plan exceeds 18000 original LaTeX characters")
        if len(parent) > MAX_PARENT_CHARS or item["source_latex_sha256"] != text_sha(parent):
            raise ValueError("Stale or oversized partition parent")
        segments = item["segments"]
        if not isinstance(segments, list) or not 2 <= len(segments) <= MAX_SEGMENTS:
            raise ValueError("Partition needs 2..16 complete source segments")
        boundaries = _lexeme_boundaries(parent)
        result = []
        cursor = 0
        for index, segment in enumerate(segments):
            if set(segment) != {"start", "end", "role"}:
                raise ValueError("Exact source segment schema required")
            start, end = segment["start"], segment["end"]
            if (
                type(start) is not int
                or type(end) is not int
                or start != cursor
                or not start < end <= len(parent)
            ):
                raise ValueError("Partition must cover contiguous ordered nonoverlapping ranges")
            if start not in boundaries or end not in boundaries:
                raise ValueError("Partition cuts a TeX lexeme or group")
            role = segment["role"]
            text = parent[start:end]
            expected_role = "math" if index % 2 == 0 else "narrative_separator"
            if (
                role != expected_role
                or role == "narrative_separator"
                and not re.fullmatch(r"[ \t]*[.,;:][ \t]*", text)
            ):
                raise ValueError(
                    "Only explicitly proposed alternating narrative punctuation separators are supported"
                )
            alias = (
                "⟦fragment:"
                + sha([bundle["fingerprint"], mid, item["source_latex_sha256"], start, end, role])
                + "⟧"
            )
            result.append(
                {
                    "start": start,
                    "end": end,
                    "role": role,
                    "source_latex": text,
                    "alias": alias,
                    "source_latex_sha256": text_sha(text),
                }
            )
            cursor = end
        if cursor != len(parent) or "".join(s["source_latex"] for s in result) != parent:
            raise ValueError("Partition must exactly rejoin the complete parent")
        proof = _proof(parent, [s["source_latex"] for s in result])
        owner = next(u for u in bundle["units"] if any(m["token"] == mid for m in u["math"]))
        normalized.append(
            {
                "math_id": mid,
                "source_unit_id": owner["id"],
                "source_latex_sha256": text_sha(parent),
                "source_latex": parent,
                "parent_source_refs": list(
                    next(m["refs"] for m in owner["math"] if m["token"] == mid)
                ),
                "segments": result,
                "proof": proof,
            }
        )
    manifest = {
        "version": VERSION,
        "source_bundle_fingerprint": bundle["fingerprint"],
        "source_pdf_sha256": bundle["source_pdf_sha256"],
        "view_id": bundle["view_id"],
        "parents": normalized,
        "semantic_review_performed": False,
    }
    manifest["manifest_sha256"] = sha(manifest)
    return manifest


def verify_partition_review(bundle, manifest, review, *, proposal_call_id, review_call_id):
    """Bind a separate native review; callers must verify actual call artifacts."""
    if not proposal_call_id or not review_call_id or proposal_call_id == review_call_id:
        raise ValueError("Separate native proposal and review calls required")
    plain = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    if (
        manifest.get("manifest_sha256") != sha(plain)
        or manifest.get("source_bundle_fingerprint") != bundle["fingerprint"]
    ):
        raise ValueError("Stale or changed partition manifest")
    if (
        set(review) != {"manifest_sha256", "items", "issues"}
        or review["manifest_sha256"] != manifest["manifest_sha256"]
        or review["issues"]
    ):
        raise ValueError("Exact unresolved-free partition review required")
    expected = [p["math_id"] for p in manifest["parents"]]
    if (
        len(review["items"]) != len(expected)
        or [i.get("math_id") for i in review["items"]] != expected
    ):
        raise ValueError("Review must cover every exact parent partition")
    for item in review["items"]:
        if (
            set(item)
            != {"math_id", "verdict", "source_compared", "narrative_separators_confirmed", "checks"}
            or item["verdict"] != "pass"
            or item["source_compared"] is not True
            or item["narrative_separators_confirmed"] is not True
            or not isinstance(item["checks"], list)
            or not item["checks"]
            or any(not isinstance(c, str) or not c.strip() for c in item["checks"])
        ):
            raise ValueError("Independent PDF/context narrative-boundary review required")
    return {
        "manifest": copy.deepcopy(manifest),
        "review": copy.deepcopy(review),
        "proposal_call_id": proposal_call_id,
        "review_call_id": review_call_id,
        "translation_approval_granted": False,
    }


def validate_partition_target(reviewed, unit_id, target):
    manifest = reviewed["manifest"]
    allowed = {
        s["alias"]
        for p in manifest["parents"]
        if p["source_unit_id"] == unit_id
        for s in p["segments"]
    }
    if any(t.startswith("⟦fragment:") and t not in allowed for t in TOKEN.findall(target)):
        raise ValueError("Foreign immutable math fragment")
    for parent in manifest["parents"]:
        if parent["source_unit_id"] != unit_id:
            continue
        aliases = [s["alias"] for s in parent["segments"]]
        actual = [token for token in TOKEN.findall(target) if token in aliases]
        if actual != aliases or parent["math_id"] in target:
            raise ValueError("Missing, duplicate or reordered immutable math fragment")


def combine_partition_reviews(bundle, batches, native_provenance=None):
    parents = []
    seen = set()
    for batch in batches:
        manifest = _require_reviewed(bundle, batch)
        for parent in manifest["parents"]:
            if parent["math_id"] in seen:
                raise ValueError("Duplicate partition parent across native batches")
            seen.add(parent["math_id"])
            parents.append(copy.deepcopy(parent))
    manifest = {
        "version": VERSION,
        "source_bundle_fingerprint": bundle["fingerprint"],
        "source_pdf_sha256": bundle["source_pdf_sha256"],
        "view_id": bundle["view_id"],
        "parents": parents,
        "batch_manifest_sha256s": [b["manifest"]["manifest_sha256"] for b in batches],
        "semantic_review_performed": False,
    }
    manifest["native_provenance_sha256"] = sha(native_provenance or [])
    manifest["manifest_sha256"] = sha(manifest)
    return {
        "manifest": manifest,
        "batches": copy.deepcopy(batches),
        "native_provenance": copy.deepcopy(native_provenance or []),
        "translation_approval_granted": False,
    }


def _require_reviewed(bundle, reviewed, overlays=()):
    if "batches" in reviewed:
        rebuilt = combine_partition_reviews(
            bundle, reviewed["batches"], reviewed.get("native_provenance", [])
        )
        if rebuilt != reviewed:
            raise ValueError("Changed combined partition review envelope")
        ids = {p["math_id"] for p in rebuilt["manifest"]["parents"]}
        if any(o["math_id"] in ids for o in overlays):
            raise ValueError("Partition parents with existing math overlays are unsupported")
        return rebuilt["manifest"]
    manifest = reviewed["manifest"]
    proposal = {
        "version": VERSION,
        "source_bundle_fingerprint": bundle["fingerprint"],
        "parents": [
            {
                "math_id": p["math_id"],
                "source_latex_sha256": p["source_latex_sha256"],
                "segments": [{k: s[k] for k in ("start", "end", "role")} for s in p["segments"]],
            }
            for p in manifest["parents"]
        ],
    }
    current = normalize_partition_plan(bundle, proposal)
    if current != manifest:
        raise ValueError("Partition manifest no longer matches exact source/renderer")
    verified = verify_partition_review(
        bundle,
        manifest,
        reviewed["review"],
        proposal_call_id=reviewed["proposal_call_id"],
        review_call_id=reviewed["review_call_id"],
    )
    if verified != reviewed:
        raise ValueError("Changed reviewed partition envelope")
    ids = {p["math_id"] for p in manifest["parents"]}
    if any(o["math_id"] in ids for o in overlays):
        raise ValueError("Partition parents with existing math overlays are unsupported")
    if any(row["math_id"] in ids for row in bundle.get("math_text_inventory", [])):
        raise ValueError("Partition parents containing prose-role inventory are unsupported")
    return manifest


def build_partitioned_bundle(bundle, reviewed, *, overlays=()):
    """Create a versioned translation-only input; source Document stays exact.

    Only an explicitly opted-in fresh empty V2 experiment may store this result.
    Legacy bundles/results are never upgraded or rewritten by this helper.
    """
    manifest = _require_reviewed(bundle, reviewed, overlays)
    derived = copy.deepcopy(bundle)
    parents = {p["math_id"]: p for p in manifest["parents"]}
    original_units = []
    for unit in derived["units"]:
        maths = []
        changed = False
        for item in unit["math"]:
            parent = parents.get(item["token"])
            if parent is None:
                maths.append(item)
                continue
            changed = True
            unit["source_text"] = unit["source_text"].replace(
                item["token"], "".join(s["alias"] for s in parent["segments"])
            )
            for segment in parent["segments"]:
                maths.append(
                    {
                        "token": segment["alias"],
                        "latex": segment["source_latex"],
                        "refs": [],
                        "partition_parent_math_id": item["token"],
                        "partition_manifest_sha256": manifest["manifest_sha256"],
                        "parent_source_refs": list(item["refs"]),
                    }
                )
        if changed:
            original_units.append(
                copy.deepcopy(next(u for u in bundle["units"] if u["id"] == unit["id"]))
            )
            unit["math"] = maths
            unit["source_partition_original_unit_sha256"] = unit["fingerprint"]
            unit["source_text_sha256"] = text_sha(unit["source_text"])
            unit["fingerprint"] = sha({k: v for k, v in unit.items() if k != "fingerprint"})
    derived["math_partition_original_units"] = original_units
    derived["translation_contract_version"] = VERSION
    derived["source_bundle_fingerprint"] = bundle["fingerprint"]
    derived["math_partition"] = copy.deepcopy(reviewed)
    derived["fingerprint"] = sha({k: v for k, v in derived.items() if k != "fingerprint"})
    return derived


def original_partition_bundle(derived):
    if derived.get("translation_contract_version") != VERSION:
        return copy.deepcopy(derived)
    original = copy.deepcopy(derived)
    original_units = {u["id"]: u for u in original.pop("math_partition_original_units")}
    original["units"] = [original_units.get(u["id"], u) for u in original["units"]]
    original.pop("translation_contract_version")
    original.pop("math_partition")
    fingerprint = original.pop("source_bundle_fingerprint")
    original["fingerprint"] = fingerprint
    if sha({k: v for k, v in original.items() if k != "fingerprint"}) != fingerprint:
        raise ValueError("V2 original source bundle reconstruction differs")
    return original


def validate_partition_bundle(derived):
    original = original_partition_bundle(derived)
    if build_partitioned_bundle(original, derived["math_partition"]) != derived:
        raise ValueError("Changed V2 translation-only source contract")
    return original


def project_partition_display(bundle, reviewed, result, base_display):
    """Strict V2 projection; never duplicate a parent's fallback image.

    The full original parent link remains metadata on each display fragment.
    A current bundled-KaTeX proof is required before returning any projection.
    Runtime frontend renderer/resource failure must still block publication in
    end-to-end Reader QA; this module makes no browser-paint approval claim.
    """
    from .projection import _runs, project_display

    derived = build_partitioned_bundle(bundle, reviewed, overlays=result.get("math_overlays", []))
    if result.get("bundle_fingerprint") != derived["fingerprint"]:
        raise ValueError("Translation result is not bound to the exact partition manifest/review")
    manifest = reviewed["manifest"]
    for entry in result.get("entries", []):
        for uid in entry["source_unit_ids"]:
            validate_partition_target(reviewed, uid, entry["target"])
    base = copy.deepcopy(base_display)
    blocks = {b["id"]: b for b in base["blocks"]}
    parents = {p["math_id"]: p for p in manifest["parents"]}
    for unit in bundle["units"]:
        current_runs = _runs(blocks[unit["block_id"]]["content"], unit["field"])
        output = []
        index = 0
        for run in current_runs:
            if run["kind"] != "math":
                output.append(run)
                continue
            if index >= len(unit["math"]):
                raise ValueError("Source base math count differs before partition projection")
            original = unit["math"][index]
            if run.get("latex") != original["latex"] or run.get("refs") != original["refs"]:
                raise ValueError("Source base parent math differs before partition projection")
            mid = unit["math"][index]["token"]
            index += 1
            parent = parents.get(mid)
            if parent is None:
                output.append(run)
                continue
            for n, segment in enumerate(parent["segments"]):
                output.append(
                    {
                        "kind": "math",
                        "latex": segment["source_latex"],
                        "source_verified": True,
                        "refs": [],
                        "region": None,
                        "prefix": run.get("prefix", "") if n == 0 else "",
                        "image_url": None,
                        "partition_parent": {
                            "math_id": mid,
                            "source_latex_sha256": parent["source_latex_sha256"],
                            "source_refs": parent["parent_source_refs"],
                            "image_url": run.get("image_url"),
                        },
                        "partition_manifest_sha256": manifest["manifest_sha256"],
                    }
                )
        if index != len(unit["math"]):
            raise ValueError("Source base math count differs before partition projection")
        current_runs[:] = output
    projected = project_display(derived, result, base)
    projected["translation"].update(
        contract_version=VERSION,
        source_bundle_fingerprint=bundle["fingerprint"],
        math_partition_manifest_sha256=manifest["manifest_sha256"],
    )
    return projected


def project_translation_bundle(bundle, result, base_display):
    if bundle.get("translation_contract_version") != VERSION:
        from .projection import project_display

        return project_display(bundle, result, base_display)
    original = validate_partition_bundle(bundle)
    return project_partition_display(original, bundle["math_partition"], result, base_display)


def _worker():
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtQml import QJSEngine

    request = json.loads(sys.stdin.buffer.read())
    app = QCoreApplication([])
    engine = QJSEngine()
    engine.evaluate(Path(request.pop("resource")).read_text("utf8"))
    engine.evaluate(Path(__file__).with_name("math_partition_worker.js").read_text("utf8"))
    value = engine.evaluate(
        "JSON.stringify(partitionProof(" + json.dumps(request, ensure_ascii=True) + "))"
    )
    if value.isError():
        raise ValueError(value.toString())
    sys.stdout.buffer.write(value.toString().encode("utf8"))
    del engine
    del app


if __name__ == "__main__":
    _worker()
