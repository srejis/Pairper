"""Resumable source-only generation, independent review and bounded correction.

The scheduler owns selection and state. A model returns constrained content;
neither a model nor a structural check can declare the entire run complete.
"""

import copy
import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .contract import (
    Entry,
    FigureReview,
    MathOverlay,
    MathRole,
    entry_review_requirements,
    sha,
    verify_result,
)
from .math_boundary import validate_math_sentence_boundaries
from .math_contract import review_requirements as math_requirements
from .math_contract import source_math
from .prepare import current_source, prepare_bundle

VERSION = "source-generation-review-repair-v1"
FIGURE_REVIEW_EVIDENCE_VERSION = 'actual-output-atlas-native-glyph-v3'


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Workflow:
    def __init__(
        self,
        service,
        runner=None,
        max_repairs=3,
        max_failures=3,
        lease_seconds=1800,
        source_repairer=None,
    ):
        if not (0 <= max_repairs <= 10 and 1 <= max_failures <= 10 and lease_seconds > 0):
            raise ValueError("Invalid workflow retry limits")
        self.service, self.runner = service, runner
        if source_repairer is not None:
            from .partition_planning import PartitionAwareSourceRepairer
            source_repairer = PartitionAwareSourceRepairer(source_repairer)
        self.source_repairer = source_repairer
        self.max_repairs, self.max_failures = max_repairs, max_failures
        self.lease_seconds = lease_seconds
        self._normalized_cache = {}
        self._glyph_evidence_cache = {}
        self.directory = service.directory / "workflow"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = self.directory / "workflow.db"
        with self.connect() as con:
            con.executescript("""
              CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, paper_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, settings TEXT NOT NULL, state TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS tasks (
                run_id TEXT NOT NULL, id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                kind TEXT NOT NULL, packet TEXT NOT NULL, candidate TEXT,
                review TEXT, review_input_hash TEXT NOT NULL DEFAULT '',
                phase TEXT NOT NULL DEFAULT 'generate', status TEXT NOT NULL DEFAULT 'pending',
                lease TEXT, lease_until REAL, repair_count INTEGER NOT NULL DEFAULT 0,
                failures INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(run_id,id));
              CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                task_id TEXT, phase TEXT NOT NULL, event TEXT NOT NULL,
                input_hash TEXT, output_hash TEXT, payload TEXT NOT NULL, at REAL NOT NULL);
            """)
            # Serialize the schema check and additive migrations across paper
            # workers/processes sharing the same workflow store.
            con.execute("BEGIN IMMEDIATE")
            if "last_response" not in {r["name"] for r in con.execute("PRAGMA table_info(tasks)")}:
                con.execute("ALTER TABLE tasks ADD COLUMN last_response TEXT")
            if "source_feedback" not in {
                r["name"] for r in con.execute("PRAGMA table_info(tasks)")
            }:
                con.execute("ALTER TABLE tasks ADD COLUMN source_feedback TEXT")

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.db, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def _event(self, con, run_id, task_id, phase, event, payload, input_hash="", output_hash=""):
        con.execute(
            "INSERT INTO events(run_id,task_id,phase,event,input_hash,output_hash,payload,at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (run_id, task_id, phase, event, input_hash, output_hash, dumps(payload), time.time()),
        )

    def _run(self, con, run_id):
        row = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise ValueError("Unknown translation workflow")
        return row

    def _bundle(self, run):
        bundle = self.service.current_bundle(run["session_id"])
        if bundle["fingerprint"] != run["fingerprint"]:
            raise ValueError("Workflow source changed; retain history and start a new run")
        return bundle

    @staticmethod
    def _rows(con, run_id):
        return con.execute(
            "SELECT * FROM tasks WHERE run_id=? ORDER BY ordinal", (run_id,)
        ).fetchall()

    def start(self, paper_id, experiment_id, max_units=32, max_source_chars=18000, math_partition_v2=False, source_planning_retry_reason=None):
        from .planning import build_plan

        if not isinstance(experiment_id, str) or not 1 <= len(experiment_id) <= 100:
            raise ValueError("An explicit fresh experiment identifier is required")
        bundle = prepare_bundle(self.service.library, paper_id, self.service.directory / "assets")
        if math_partition_v2:
            from .math_partition import VERSION as PARTITION_VERSION
            from .partition_planning import PartitionPlanner
            # Read source metadata only, never previous Korean result payloads.
            with self.service.store.connect() as con:
                previous = [dict(r) for r in con.execute(
                    "SELECT source FROM sessions WHERE paper_id=? AND experiment_id=?",
                    (paper_id, experiment_id)).fetchall()]
            if any(json.loads(r["source"]).get("translation_contract_version") != PARTITION_VERSION for r in previous):
                raise ValueError("V2 requires a new explicit experiment, not a legacy quality-budget restart")
            bundle, _partition_record = PartitionPlanner(self.service.library, self.runner,
                self.service.directory / "math-partitions").prepare(bundle, experiment_id,
                    **({"retry_failed_reason": source_planning_retry_reason} if source_planning_retry_reason is not None else {}))
        run_id = sha([VERSION, bundle["fingerprint"], experiment_id])
        settings = {
            "version": VERSION,
            "max_units": max_units,
            "max_source_chars": max_source_chars,
            "max_repairs": self.max_repairs,
            "max_failures": self.max_failures,
        }
        if math_partition_v2:
            settings["math_partition_v2"] = True
            settings["partition_manifest_sha256"] = bundle["math_partition"]["manifest"]["manifest_sha256"]
        with self.connect() as con:
            existing = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if existing:
            if json.loads(existing["settings"]) != settings:
                raise ValueError("Cannot change settings of an existing run")
            self._refresh_blocked_figure_evidence(run_id, bundle)
            return self.report(run_id)
        saved = self.service.store.begin(bundle, experiment_id)
        if any(
            saved["result"].get(k)
            for k in ("entries", "figure_reviews", "math_roles", "math_overlays")
        ):
            raise ValueError(
                "Fresh workflow cannot import an existing Korean result; choose a new experiment"
            )
        tasks = build_plan(bundle, max_units=max_units, max_source_chars=max_source_chars)
        if not tasks or len({t["task_id"] for t in tasks}) != len(tasks):
            raise ValueError("Empty or duplicate workflow plan")
        text_ids = [uid for t in tasks if t["kind"] == "text" for uid in t["source_unit_ids"]]
        if sorted(text_ids) != sorted(u["id"] for u in bundle["units"]):
            raise ValueError("Plan must cover every source unit exactly once")
        planned_math = [m["math_id"] for t in tasks if t["kind"] == "math" for m in t["math_items"]]
        if sorted(planned_math) != sorted(
            m["math_id"] for m in bundle.get("math_text_inventory", [])
        ):
            raise ValueError("Plan must cover all mathematical text nodes")
        if sorted(t["figure"]["block_id"] for t in tasks if t["kind"] == "figure") != sorted(
            f["block_id"] for f in bundle["figures"]
        ):
            raise ValueError("Plan must include every figure, even figures without extracted spans")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?)",
                (
                    run_id,
                    saved["session_id"],
                    paper_id,
                    bundle["fingerprint"],
                    dumps(settings),
                    "generating",
                ),
            )
            for index, task in enumerate(tasks):
                ready = task.get("preflight", {}).get("ready", True)
                con.execute(
                    "INSERT INTO tasks(run_id,id,ordinal,kind,packet,status,last_error) VALUES(?,?,?,?,?,?,?)",
                    (
                        run_id,
                        task["task_id"],
                        index,
                        task["kind"],
                        dumps(task),
                        "pending" if ready else "blocked",
                        ""
                        if ready
                        else "Source preflight requires review: " + dumps(task["preflight"]),
                    ),
                )
            self._event(
                con,
                run_id,
                None,
                "start",
                "fresh_source_plan",
                {"settings": settings, "tasks": len(tasks), "prior_Korean_inputs": 0},
                bundle["fingerprint"],
            )
        return self.report(run_id)

    def _refresh_blocked_figure_evidence(self, run_id, bundle):
        """A new evidence version can require one fresh unresolved visual review."""
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            run = self._run(con, run_id)
            if run['state'] in ('published', 'ready', 'source_changed'):
                return
            if run['fingerprint'] != bundle['fingerprint']:
                raise ValueError('Cannot upgrade review evidence from another source')
            rows = self._rows(con, run_id)
            for row in rows:
                if (row['kind'] != 'figure' or row['status'] != 'blocked'
                        or row['phase'] != 'review' or row['lease']
                        or not row['candidate'] or not row['review']
                        or row['repair_count'] >= self.max_repairs):
                    continue
                previous = json.loads(row['review'])
                if not any(i.get('verdict') == 'unresolved' for i in previous.get('items', [])):
                    continue
                latest = con.execute('SELECT event FROM events WHERE run_id=? AND task_id=? ORDER BY seq DESC LIMIT 1',
                                     (run_id, row['id'])).fetchone()
                if not latest or latest['event'] != 'review_found_issues':
                    continue
                claim = con.execute("SELECT payload FROM events WHERE run_id=? AND task_id=? AND event='claimed' AND phase='review' ORDER BY seq DESC LIMIT 1",
                                    (run_id, row['id'])).fetchone()
                if not claim:
                    continue
                old_version = json.loads(claim['payload'])['request'].get('figure_review_evidence_version')
                if old_version == FIGURE_REVIEW_EVIDENCE_VERSION:
                    continue
                self._event(con, run_id, row['id'], 'review', 'visual_evidence_recheck_requested',
                            {'previous_evidence_version': old_version,
                             'new_evidence_version': FIGURE_REVIEW_EVIDENCE_VERSION,
                             'previous_review': previous, 'previous_review_hash': row['review_input_hash'],
                             'candidate_sha256': sha(json.loads(row['candidate'])),
                             'fresh_review_required': True, 'translation_approval_granted': False})
                con.execute("UPDATE tasks SET status='drafted',review=NULL,review_input_hash='' WHERE run_id=? AND id=?",
                            (run_id, row['id']))
                con.execute("UPDATE runs SET state='reviewing' WHERE id=?", (run_id,))

    @staticmethod
    def _blank(bundle):
        return {
            "bundle_fingerprint": bundle["fingerprint"],
            "entries": [],
            "figure_reviews": [],
            "math_roles": [],
            "math_overlays": [],
        }

    def _normalize(self, bundle, task, answer):
        """Attach trusted identities; reject foreign selections and partial batches."""
        # A new source-format gate must also check previously cached candidates.
        if task["kind"] == "text":
            by_id = {u["id"]: u for u in bundle["units"]}
            for item in answer.get("items", []):
                if item.get("unit_id") in by_id:
                    validate_math_sentence_boundaries(by_id[item["unit_id"]], item["target"])
                    if bundle.get("math_partition"):
                        from .math_partition import validate_partition_target
                        validate_partition_target(bundle["math_partition"], item["unit_id"], item["target"])
        cache_key = (bundle["fingerprint"], task["task_id"], sha(answer))
        if cache_key in self._normalized_cache:
            return copy.deepcopy(self._normalized_cache[cache_key])
        result = self._blank(bundle)
        units = {u["id"]: u for u in bundle["units"]}
        if answer.get("issues"):
            raise ValueError("Generator reported unresolved issues: " + dumps(answer["issues"]))
        if task["kind"] == "text":
            items = answer["items"]
            if sorted(i["unit_id"] for i in items) != sorted(task["source_unit_ids"]):
                raise ValueError("Text response must cover exactly the selected source units")
            for item in items:
                uid = item["unit_id"]
                entry = Entry(
                    source_unit_ids=[uid],
                    source_fingerprint=sha([units[uid]["fingerprint"]]),
                    action=item["action"],
                    target=item["target"],
                    preservation_reason=item["reason"] if item["action"] == "preserve" else "",
                ).model_dump()
                result["entries"].append(entry)
        elif task["kind"] == "math":
            inventory = {
                (m["math_id"], n["index"]) for m in task["math_items"] for n in m["text_nodes"]
            }
            roles = [MathRole.model_validate(r).model_dump() for r in answer["math_roles"]]
            if sorted((r["math_id"], r["node_index"]) for r in roles) != sorted(inventory):
                raise ValueError(
                    "Math response must classify every selected text node exactly once"
                )
            overlays = [MathOverlay.model_validate(o).model_dump() for o in answer["math_overlays"]]
            if len({o["math_id"] for o in overlays}) != len(overlays) or any(
                o["math_id"] not in {m[0] for m in inventory} for o in overlays
            ):
                raise ValueError("Foreign or duplicate mathematical overlay")
            selected = {(o["math_id"], i) for o in overlays for i in o["source_prose_indices"]}
            if any(
                r["action"] == "translate" and (r["math_id"], r["node_index"]) not in selected
                for r in roles
            ):
                raise ValueError(
                    "Every mathematical prose translation requires a corresponding selected overlay"
                )
            result.update(math_roles=roles, math_overlays=overlays)
        elif task["kind"] == "figure":
            items = answer["figures"]
            if len(items) != 1 or items[0]["block_id"] != task["figure"]["block_id"]:
                raise ValueError("Figure response must identify the selected original image")
            item = copy.deepcopy(items[0])
            if (
                item["image_inspected"] is not True
                or item["labels_complete"] is not True
                or item["unresolved_errors"]
            ):
                raise ValueError("Figure remains uninspected or unresolved")
            if not item["labels"] and not item["no_translatable_text_reason"].strip():
                raise ValueError("An empty figure label list requires an actual image-based reason")
            block = next(b for b in bundle["document"]["blocks"] if b["id"] == item["block_id"])
            allowed = set(block["source_refs"])
            left, top, right, bottom = task["figure"]["region"]["bbox"]
            for label in item["labels"]:
                refs, bbox = label["source_refs"], label["bbox"]
                if (
                    not (refs or bbox)
                    or not set(refs) <= allowed
                    or (
                        bbox
                        and not (
                            len(bbox) == 4
                            and left <= bbox[0] < bbox[2] <= right
                            and top <= bbox[1] < bbox[3] <= bottom
                        )
                    )
                ):
                    raise ValueError(
                        "Figure label has foreign refs or invalid original-PDF coordinates"
                    )
                if (
                    not refs
                    and bbox
                    and len(item["labels"]) > 1
                    and (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                    >= 0.98 * (right - left) * (bottom - top)
                ):
                    raise ValueError(
                        "A multi-label figure cannot use its whole region as one phrase anchor; "
                        "provide tight original-PDF bounds for each occurrence, or leave it unresolved"
                    )
            item["crop_sha256"] = task["figure"]["crop_sha256"]
            result["figure_reviews"] = [FigureReview.model_validate(item).model_dump()]
        else:
            raise ValueError("Unknown workflow kind")
        report = verify_result(bundle, result)
        if not report["structurally_valid"]:
            raise ValueError("Source/structure violation: " + "; ".join(report["errors"]))
        self._normalized_cache[cache_key] = copy.deepcopy(result)
        return result

    def _candidate_result(self, bundle, rows):
        result = self._blank(bundle)
        for row in rows:
            if row["candidate"]:
                normalized = self._normalize(
                    bundle, json.loads(row["packet"]), json.loads(row["candidate"])
                )
                for key in ("entries", "figure_reviews", "math_roles", "math_overlays"):
                    result[key].extend(normalized[key])
        return result

    @staticmethod
    def _expanded(unit, result):
        entry = next((e for e in result["entries"] if unit["id"] in e["source_unit_ids"]), None)
        source, target = unit["source_text"], entry["target"] if entry else unit["source_text"]
        overlays = {o["math_id"]: o for o in result["math_overlays"]}
        for math in unit["math"]:
            source = source.replace(math["token"], math["latex"])
            target = target.replace(
                math["token"], overlays.get(math["token"], {}).get("localized_latex", math["latex"])
            )
        return source, target

    def _review_items(self, bundle, task, normalized):
        by_id = {u["id"]: u for u in bundle["units"]}
        items = []
        if task["kind"] == "text":
            for e in normalized["entries"]:
                uid = e["source_unit_ids"][0]
                items.append(
                    {
                        "item_id": uid,
                        "source": by_id[uid]["source_text"],
                        "target": e["target"],
                        "source_unit_ids": [uid],
                    }
                )
        elif task["kind"] == "math":
            originals = source_math(bundle)
            inv = {m["math_id"]: m for m in task["math_items"]}
            for r in normalized["math_roles"]:
                node = next(
                    n for n in inv[r["math_id"]]["text_nodes"] if n["index"] == r["node_index"]
                )
                items.append(
                    {
                        "item_id": r["math_id"] + "@" + str(r["node_index"]),
                        "source": node["text"],
                        "target": dumps({k: v for k, v in r.items() if k != "review"}),
                        "source_unit_ids": task["source_unit_ids"],
                    }
                )
            for o in normalized["math_overlays"]:
                items.append(
                    {
                        "item_id": o["math_id"] + "@overlay",
                        "source": originals[o["math_id"]],
                        "target": o["localized_latex"],
                        "source_unit_ids": task["source_unit_ids"],
                    }
                )
        else:
            for f in normalized["figure_reviews"]:
                items.append(
                    {
                        "item_id": f["block_id"],
                        "source": "Original attached figure; " + dumps(task.get("figure", {})),
                        "target": dumps(
                            {k: v for k, v in f.items() if k not in ("reviewer", "labels_sha256")}
                        ),
                        "source_unit_ids": task["source_unit_ids"],
                    }
                )
                for i, label in enumerate(f["labels"]):
                    items.append(
                        {
                            "item_id": f["block_id"] + "@label" + str(i),
                            "source": label["source"],
                            "target": label["target"],
                            "source_unit_ids": task["source_unit_ids"],
                        }
                    )
        return items

    def _context(self, bundle, task, result):
        """Current full assembly for actual dependencies, including all math-review dependencies."""
        selected = set(task["source_unit_ids"])
        blocks = sorted(bundle["document"]["blocks"], key=lambda b: b["order"])
        positions = {b["id"]: i for i, b in enumerate(blocks)}
        owner_ids = {u["block_id"] for u in task["units"]}
        for m in task.get("math_items", []):
            if m["math_id"].startswith("block:"):
                owner_ids.add(m["math_id"][6:-6])
        if task["kind"] == "figure":
            owner_ids.add(task["figure"]["block_id"])
        context_blocks = set(owner_ids)
        for bid in owner_ids:
            if bid not in positions:
                continue
            pos = positions[bid]
            context_blocks.update(b["id"] for b in blocks[max(0, pos - 1) : pos + 2])
            parent = blocks[pos].get("parent_id")
            if parent:
                context_blocks.add(parent)
                context_blocks.update(b["id"] for b in blocks if b.get("parent_id") == parent)
        full_ids = {u["id"] for u in bundle["units"] if u["block_id"] in context_blocks}
        # Text reviews bind at most64 whole units; mathematical context has no such contract cap.
        dependencies = list(task["source_unit_ids"])
        for u in bundle["units"]:
            if (
                u["id"] in full_ids
                and u["id"] not in selected
                and (task["kind"] != "text" or len(dependencies) < 64)
            ):
                dependencies.append(u["id"])
        assembly = [
            {
                "unit_id": u["id"],
                "source": self._expanded(u, result)[0],
                "target": self._expanded(u, result)[1],
            }
            for u in bundle["units"]
            if u["id"] in set(dependencies)
        ]
        context_math = {
            m["token"] for u in bundle["units"] if u["id"] in set(dependencies) for m in u["math"]
        }
        context_math.update(
            "block:" + b["id"] + "/latex"
            for b in blocks
            if b["id"] in context_blocks and b["type"] == "equation"
        )
        originals = source_math(bundle)
        overlays = {o["math_id"]: o["localized_latex"] for o in result["math_overlays"]}
        equations = [
            {"math_id": mid, "source": originals[mid], "target": overlays.get(mid, originals[mid])}
            for mid in sorted(context_math)
        ]
        return {
            "context_unit_ids": dependencies,
            "assembled_source_and_Korean": assembly,
            "equations": equations,
            "source_context": task.get("context", []),
        }

    def _request(self, bundle, row, rows, phase):
        task = json.loads(row["packet"])
        result = self._candidate_result(bundle, rows)
        candidate = json.loads(row["candidate"]) if row["candidate"] else None
        normalized = self._normalize(bundle, task, candidate) if candidate else self._blank(bundle)
        request = {
            "version": VERSION,
            "phase": phase,
            "kind": row["kind"],
            "task": task,
            "candidate": candidate,
            "context": self._context(bundle, task, result),
            "review_items": self._review_items(bundle, task, normalized) if candidate else [],
            "issues": json.loads(row["review"]) if row["review"] else [],
            "structural_error": row["last_error"],
            "prior_Korean_inputs": 0,
        }
        if phase == "review":
            request["issues"] = []
            request["structural_error"] = ""
        elif row["last_error"]:
            request["validation_feedback"] = {"error": row["last_error"]}
            if row["last_response"]:
                request["validation_feedback"]["rejected_candidate"] = json.loads(
                    row["last_response"]
                )
        if phase in ("generate", "repair") and row["source_feedback"]:
            feedback = json.loads(row["source_feedback"])
            if feedback.get("bundle_fingerprint") == bundle["fingerprint"]:
                request["source_verification_feedback"] = feedback
        if task['kind'] == 'figure' and candidate is not None:
            request['figure_review_evidence_version'] = FIGURE_REVIEW_EVIDENCE_VERSION
            request['figure_output_evidence'] = self._figure_output_evidence(bundle, task, candidate)
        if task['kind'] == 'figure':
            figure_glyphs = self._figure_glyph_evidence(bundle, task, candidate)
            if figure_glyphs is not None:
                request['figure_label_glyph_evidence'] = figure_glyphs
        if bundle.get("math_partition"):
            selected=set(task["source_unit_ids"]) | set(request["context"].get("context_unit_ids", []))
            manifest=bundle["math_partition"]["manifest"]
            request["source_math_partition"]={"version":manifest["version"],
                "manifest_sha256":manifest["manifest_sha256"],
                "source_bundle_fingerprint":bundle["source_bundle_fingerprint"],
                "parents":[copy.deepcopy(p) for p in manifest["parents"] if p["source_unit_id"] in selected]}
        glyphs = self._source_glyph_evidence(bundle, task)
        if glyphs is not None:
            request['source_glyph_evidence'] = glyphs
        return request

    def _figure_glyph_evidence(self, bundle, task, candidate):
        library = self.service.library
        if not hasattr(library, 'root'):
            return None
        from .source_figure_glyph_evidence import build_figure_label_glyph_evidence

        selectors = None if candidate is None else [
            {'source_refs': x.get('source_refs', []), 'bbox': x.get('bbox', [])}
            for x in candidate['figures'][0]['labels']]
        key = ('figure-native-glyph-v1', bundle['fingerprint'], task['figure']['block_id'], sha(selectors))
        if key not in self._glyph_evidence_cache:
            paper = library.paper(bundle['paper_id'])
            self._glyph_evidence_cache[key] = build_figure_label_glyph_evidence(
                bundle, task, candidate, pdf_path=library.root / paper['original_path'],
                source_snapshot_path=library.root / 'sources' / f"{bundle['paper_id']}.json",
                view_path=library.root / 'views' / f"{bundle['view_id']}.json")
        return copy.deepcopy(self._glyph_evidence_cache[key])

    def _source_glyph_evidence(self, bundle, task):
        """Neutral original-PDF evidence, independent of candidates and verdicts."""
        library = self.service.library
        if not hasattr(library, 'root'):
            # Synthetic orchestration fixtures have no actual PDF Library.
            return None
        from .source_glyph_evidence import build_source_glyph_evidence

        units = task.get('units', [])
        # A bounded selection must not let introductory prose crowd out code
        # literals. This rank uses original unit data only, never a diagnosis.
        def priority(unit):
            if '/code_lines/' in unit['id']:
                return 0
            text = unit.get('source_text', '')
            if re.search(r'([^\w\s])\1{2,}', text):
                return 1
            return 2
        selected = [u['id'] for u in sorted(units, key=priority) if priority(u) < 2][:8]
        if not selected:
            return None
        key = (bundle['fingerprint'], tuple(selected))
        if key not in self._glyph_evidence_cache:
            paper = library.paper(bundle['paper_id'])
            self._glyph_evidence_cache[key] = build_source_glyph_evidence(
                bundle, selected,
                pdf_path=library.root / paper['original_path'],
                source_snapshot_path=library.root / 'sources' / f"{bundle['paper_id']}.json",
                view_path=library.root / 'views' / f"{bundle['view_id']}.json",
            )
        return copy.deepcopy(self._glyph_evidence_cache[key])

    def _with_images(self, bundle, task, request):
        images = list(task.get("images", []))
        evidence = []
        library = self.service.library
        paper = library.paper(bundle["paper_id"])
        pdf_path = library.root / paper["original_path"]
        pages = sorted({p for u in task["units"] for p in u.get("pages", [])})
        source_refs = {ref for m in task.get("math_items", []) for ref in m.get("source_refs", [])}
        if source_refs:
            source = json.loads(
                (library.root / "sources" / f"{bundle['paper_id']}.json").read_text("utf-8")
            )
            pages = sorted(
                set(pages)
                | {
                    p["page"]
                    for p in source["pages"]
                    if any(s["id"] in source_refs for s in p["spans"])
                }
            )
        if task["kind"] == "figure":
            pages = sorted(set(pages) | {task["figure"]["region"]["page"]})
        if pages:
            import pymupdf

            directory = self.directory / "source-images" / bundle["source_pdf_sha256"]
            directory.mkdir(parents=True, exist_ok=True)
            with pymupdf.open(pdf_path) as doc:
                for page in pages:
                    data = (
                        doc[page - 1]
                        .get_pixmap(matrix=pymupdf.Matrix(1.35, 1.35), alpha=False)
                        .tobytes("png")
                    )
                    digest = hashlib.sha256(data).hexdigest()
                    path = directory / f"page-{page}-{digest}.png"
                    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                        raise ValueError("Original page image cache was changed")
                    if not path.exists():
                        path.write_bytes(data)
                    evidence.append(
                        {
                            "page": page,
                            "path": str(path),
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        }
                    )
                    images.append(str(path))
        # Every attached figure is verified against immutable source crop bytes.
        if task["kind"] == "figure":
            f = task["figure"]
            if hashlib.sha256(Path(f["crop"]).read_bytes()).hexdigest() != f["crop_sha256"]:
                raise ValueError("Original figure image changed")
            if request.get('candidate') is not None:
                from .source_figure_evidence import figure_label_regions, pack_label_evidence

                source = json.loads((library.root / 'sources' / f"{bundle['paper_id']}.json").read_text('utf-8'))
                local = figure_label_regions(bundle, task, request['candidate'], source_snapshot=source)
                directory = self.directory / 'source-images' / bundle['source_pdf_sha256'] / 'figure-labels'
                packed = pack_label_evidence(pdf_path, source, local['regions'], directory,
                                             base_image_count=len(set(images)))
                images.extend(packed['images'])
                evidence.extend(packed['manifest']['atlas_images'])
                request['figure_label_evidence'] = {**local['manifest'], **packed['manifest'],
                    'omissions': local['manifest']['omissions'] + packed['manifest']['omissions'],
                    'omitted_label_ids': local['manifest']['omitted_label_ids'] + packed['manifest']['omitted_label_ids']}
        request["original_page_images"] = evidence
        request["attached_images"] = [
            {"path": p, "sha256": hashlib.sha256(Path(p).read_bytes()).hexdigest()}
            for p in dict.fromkeys(images)
        ]
        return list(dict.fromkeys(images))

    def _review_hash(self, request):
        # Diagnostic transport metadata does not change what was actually reviewed.
        return sha(
            {
                k: v
                for k, v in request.items()
                if k
                not in (
                    "phase",
                    "issues",
                    "structural_error",
                    "original_page_images",
                    "attached_images",
                    "figure_label_evidence",
                )
            }
        )

    def _prewarm_claim(self, run_id):
        """Fill content-keyed caches without holding a workflow write lock.

        This snapshot grants no lease or verdict. claim re-reads all state and
        re-runs its existing source/candidate/hash guards inside BEGIN IMMEDIATE.
        """
        with self.connect() as con:
            run = self._run(con, run_id)
            bundle = self._bundle(run)
            rows = [dict(row) for row in self._rows(con, run_id)]
        if run['state'] == 'published':
            return
        for row in rows:
            packet = json.loads(row['packet'])
            if row['candidate']:
                try:
                    self._normalize(bundle, packet, json.loads(row['candidate']))
                except ValueError:
                    # Only the existing transaction may invalidate a candidate
                    # and journal that decision against freshly read state.
                    pass
            self._source_glyph_evidence(bundle, packet)

    def claim(self, run_id):
        from .prompts import response_schema

        self._prewarm_claim(run_id)
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            run = self._run(con, run_id)
            bundle = self._bundle(run)
            if run["state"] == "published":
                return None
            rows = self._rows(con, run_id)
            for row in rows:
                if row["lease"] and row["lease_until"] < time.time():
                    con.execute(
                        "UPDATE tasks SET lease=NULL,lease_until=NULL WHERE run_id=? AND id=?",
                        (run_id, row["id"]),
                    )
                    self._event(
                        con,
                        run_id,
                        row["id"],
                        row["phase"],
                        "expired_lease_reclaimed",
                        {"old_lease": row["lease"]},
                    )
            rows = self._rows(con, run_id)
            for row in rows:
                if not row["candidate"] or row["lease"]:
                    continue
                try:
                    self._normalize(bundle, json.loads(row["packet"]), json.loads(row["candidate"]))
                except ValueError as exc:
                    con.execute(
                        "UPDATE tasks SET candidate=NULL,review=NULL,review_input_hash='',"
                        "status='pending',phase='generate',failures=0,last_error=?,last_response=? "
                        "WHERE run_id=? AND id=?",
                        (str(exc), row["candidate"], run_id, row["id"]),
                    )
                    self._event(
                        con,
                        run_id,
                        row["id"],
                        "generate",
                        "candidate_invalidated_by_validation",
                        {
                            "error": str(exc),
                            "previous_candidate": json.loads(row["candidate"]),
                            "previous_review": json.loads(row["review"]) if row["review"] else None,
                            "fresh_generation_and_review_required": True,
                        },
                    )
            rows = self._rows(con, run_id)
            # Finish generation in plan order before requesting any semantic approval.
            generating = any(not r["candidate"] and r["status"] != "blocked" for r in rows)
            selected = None
            for row in rows:
                if row["lease"] or row["status"] == "blocked":
                    continue
                if generating and row["candidate"]:
                    continue
                phase = (
                    "generate"
                    if not row["candidate"]
                    else ("repair" if row["status"] == "repair_pending" else "review")
                )
                if phase == "review" and row["status"] == "accepted":
                    req = self._request(bundle, row, rows, "review")
                    if row["review_input_hash"] == self._review_hash(req):
                        continue
                    self._event(
                        con,
                        run_id,
                        row["id"],
                        "review",
                        "context_changed_recheck",
                        {"previous_hash": row["review_input_hash"]},
                    )
                selected = (row, phase)
                break
            if selected is None:
                state = (
                    "needs_review"
                    if any(r["status"] == "blocked" for r in rows)
                    else ("working" if any(r["lease"] for r in rows) else "ready")
                )
                con.execute("UPDATE runs SET state=? WHERE id=?", (state, run_id))
                return None
            row, phase = selected
            request = self._request(bundle, row, rows, phase)
            try:
                images = self._with_images(bundle, json.loads(row["packet"]), request)
            except (OSError, ValueError, RuntimeError) as exc:
                # Rendering/reading original evidence happens before a lease and
                # must consume the same bounded infrastructure-failure budget.
                count = row["failures"] + 1
                status = "blocked" if count >= self.max_failures else row["status"]
                con.execute(
                    "UPDATE tasks SET failures=?,status=?,last_error=? WHERE run_id=? AND id=?",
                    (count, status, str(exc)[:12000], run_id, row["id"]),
                )
                con.execute(
                    "UPDATE runs SET state=? WHERE id=?",
                    (
                        "needs_review"
                        if status == "blocked"
                        else ("generating" if phase == "generate" else "reviewing"),
                        run_id,
                    ),
                )
                self._event(
                    con,
                    run_id,
                    row["id"],
                    phase,
                    "image_preparation_failed",
                    {"error": str(exc)[:12000], "failures": count},
                )
                return None
            lease = uuid.uuid4().hex
            con.execute(
                "UPDATE tasks SET phase=?,lease=?,lease_until=? WHERE run_id=? AND id=?",
                (phase, lease, time.time() + self.lease_seconds, run_id, row["id"]),
            )
            state = "generating" if phase == "generate" else "reviewing"
            con.execute("UPDATE runs SET state=? WHERE id=?", (state, run_id))
            self._event(
                con,
                run_id,
                row["id"],
                phase,
                "claimed",
                {"lease_id": lease, "request": request},
                sha(request),
            )
            return {
                "run_id": run_id,
                "task_id": row["id"],
                "lease_id": lease,
                "phase": phase,
                "request": request,
                "schema": response_schema(request),
                "images": images,
            }

    def _lease(self, con, run_id, task_id, lease_id):
        run = self._run(con, run_id)
        bundle = self._bundle(run)
        row = con.execute(
            "SELECT * FROM tasks WHERE run_id=? AND id=?", (run_id, task_id)
        ).fetchone()
        if (
            not row
            or not row["lease"]
            or row["lease"] != lease_id
            or row["lease_until"] < time.time()
        ):
            raise ValueError("Unknown, expired or already submitted workflow lease")
        return run, bundle, row

    @staticmethod
    def _review_valid(request, response):
        expected = {i["item_id"]: i for i in request["review_items"]}
        items = response["items"]
        if sorted(i["item_id"] for i in items) != sorted(expected):
            raise ValueError("Review must cover every exact current candidate item")
        for item in items:
            current = expected[item["item_id"]]
            if (
                not item["source_compared"]
                or not item["checks"]
                or any(not x.strip() for x in item["checks"])
            ):
                raise ValueError("Review requires actual source comparison and concrete checks")
            if not item["source_quote"] or item["source_quote"] not in current["source"]:
                raise ValueError(
                    "Review source quotation is not present in the actual selected source"
                )
            if not item["target_quote"] or item["target_quote"] not in current["target"]:
                raise ValueError("Review target quotation is not present in the current candidate")
            if item["verdict"] == "pass" and item["issues"]:
                raise ValueError("A passing item cannot retain unresolved issues")
        known = set(expected)
        if any(i["item_id"] not in known for i in response["issues"]):
            raise ValueError("Issue refers to an unknown item")
        return all(i["verdict"] == "pass" for i in items) and not response["issues"]

    def submit(self, run_id, task_id, lease_id, response, evidence=None):
        import jsonschema

        from .prompts import response_schema, validate_protocol

        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            run, bundle, row = self._lease(con, run_id, task_id, lease_id)
            rows = self._rows(con, run_id)
            current_request = self._request(bundle, row, rows, row["phase"])
            claimed = con.execute(
                "SELECT payload FROM events WHERE run_id=? AND task_id=? AND event='claimed' ORDER BY seq DESC LIMIT 1",
                (run_id, task_id),
            ).fetchone()
            if not claimed:
                raise ValueError("No actual claimed request evidence")
            claim = json.loads(claimed["payload"])
            if claim["lease_id"] != lease_id:
                raise ValueError("Request evidence belongs to a different lease")
            request = claim["request"]
            try:
                if row["phase"] == "review" and self._review_hash(
                    current_request
                ) != self._review_hash(request):
                    raise ValueError(
                        "Reviewed assembly changed during AI invocation; fresh review required"
                    )
                jsonschema.validate(response, response_schema(request))
                validate_protocol(
                    request, response, image_count=len(request.get("attached_images", []))
                )
                for attached in request.get("attached_images", []):
                    if (
                        hashlib.sha256(Path(attached["path"]).read_bytes()).hexdigest()
                        != attached["sha256"]
                    ):
                        raise ValueError("Image evidence changed during AI invocation")
                if row["phase"] in ("generate", "repair"):
                    self._normalize(bundle, json.loads(row["packet"]), response)
                    con.execute(
                        "UPDATE tasks SET candidate=?,review=NULL,review_input_hash='',status='drafted',failures=0,last_error='',last_response=NULL WHERE run_id=? AND id=?",
                        (dumps(response), run_id, task_id),
                    )
                    event = (
                        "candidate_generated"
                        if row["phase"] == "generate"
                        else "candidate_repaired"
                    )
                else:
                    passed = self._review_valid(request, response)
                    if passed:
                        status = "accepted"
                    elif (
                        any(i["verdict"] == "unresolved" for i in response["items"])
                        or row["repair_count"] >= self.max_repairs
                    ):
                        status = "blocked"
                    else:
                        status = "repair_pending"
                    con.execute(
                        "UPDATE tasks SET review=?,review_input_hash=?,status=?,repair_count=repair_count+?,failures=0,last_error=? WHERE run_id=? AND id=?",
                        (
                            dumps(response),
                            self._review_hash(request),
                            status,
                            int(status == "repair_pending"),
                            "" if passed else "Source-grounded review requires correction",
                            run_id,
                            task_id,
                        ),
                    )
                    event = "review_passed" if passed else "review_found_issues"
                self._event(
                    con,
                    run_id,
                    task_id,
                    row["phase"],
                    event,
                    {
                        "before_candidate": json.loads(row["candidate"])
                        if row["candidate"]
                        else None,
                        "response": response,
                        "evidence": evidence
                        or {"actor": "connected AI client; caller-attested comparison"},
                    },
                    sha(request),
                    sha(response),
                )
            except (ValueError, KeyError, TypeError, OSError, jsonschema.ValidationError) as exc:
                count = row["failures"] + 1
                status = "blocked" if count >= self.max_failures else row["status"]
                con.execute(
                    "UPDATE tasks SET failures=?,status=?,last_error=?,last_response=? WHERE run_id=? AND id=?",
                    (count, status, str(exc)[:12000], dumps(response), run_id, task_id),
                )
                self._event(
                    con,
                    run_id,
                    task_id,
                    row["phase"],
                    "response_rejected",
                    {"error": str(exc), "response": response},
                    sha(request),
                    sha(response),
                )
            con.execute(
                "UPDATE tasks SET lease=NULL,lease_until=NULL WHERE run_id=? AND id=?",
                (run_id, task_id),
            )
        return self.report(run_id)

    def fail(self, run_id, task_id, lease_id, error):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            _, _, row = self._lease(con, run_id, task_id, lease_id)
            count = row["failures"] + 1
            con.execute(
                "UPDATE tasks SET failures=?,status=?,last_error=?,lease=NULL,lease_until=NULL WHERE run_id=? AND id=?",
                (
                    count,
                    "blocked" if count >= self.max_failures else row["status"],
                    str(error)[:12000],
                    run_id,
                    task_id,
                ),
            )
            self._event(
                con, run_id, task_id, row["phase"], "provider_failed", {"error": str(error)[:12000]}
            )
        return self.report(run_id)

    def retry_failed(self, run_id, reason):
        """Explicitly retry bounded execution failures without undoing quality gates."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("An explicit nonempty execution retry reason is required")
        retried = []
        retried_phases = []
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            run = self._run(con, run_id)
            self._bundle(run)
            if run["state"] in ("published", "ready", "source_changed"):
                raise ValueError(
                    "Published, ready or source-changed workflows cannot retry failures"
                )
            rows = self._rows(con, run_id)
            if any(
                row["lease"]
                and row["lease_until"] is not None
                and row["lease_until"] >= time.time()
                for row in rows
            ):
                raise ValueError("Cannot retry failures while a workflow lease is active")
            failure_limit = json.loads(run["settings"])["max_failures"]
            next_status = {"generate": "pending", "repair": "repair_pending", "review": "drafted"}
            for row in rows:
                if row["status"] != "blocked" or row["failures"] < failure_limit:
                    continue
                latest = con.execute(
                    "SELECT seq,event,phase FROM events WHERE run_id=? AND task_id=? ORDER BY seq DESC LIMIT 1",
                    (run_id, row["id"]),
                ).fetchone()
                if (
                    not latest
                    or latest["event"] not in ("provider_failed", "response_rejected", "image_preparation_failed")
                    or row["phase"] not in next_status
                ):
                    continue
                retry_phase = row["phase"]
                if latest["event"] == "image_preparation_failed":
                    # Image preparation precedes lease assignment, so a legacy
                    # row can still say generate while its failed claim was review.
                    retry_phase = latest["phase"]
                    if retry_phase not in next_status or (
                        retry_phase == "generate" and row["candidate"] is not None
                    ) or (
                        retry_phase in ("review", "repair") and not row["candidate"]
                    ):
                        continue
                # Retain candidate/review/repair budget and exact rejected-output
                # evidence. This grants another execution attempt, no approval.
                con.execute(
                    "UPDATE tasks SET status=?,phase=?,failures=0 WHERE run_id=? AND id=?",
                    (next_status[retry_phase], retry_phase, run_id, row["id"]),
                )
                self._event(
                    con,
                    run_id,
                    row["id"],
                    retry_phase,
                    "execution_retry_requested",
                    {
                        "reason": reason,
                        "previous_failures": row["failures"],
                        "previous_phase": row["phase"],
                        "retry_phase": retry_phase,
                        "previous_error": row["last_error"],
                        "failure_event": latest["event"],
                        "failure_event_seq": latest["seq"],
                        "candidate_preserved": True,
                        "review_preserved": True,
                        "repair_count_preserved": row["repair_count"],
                    },
                )
                retried.append(row["id"])
                retried_phases.append(retry_phase)
            if retried:
                state = (
                    "generating"
                    if "generate" in retried_phases
                    else "reviewing"
                )
                con.execute("UPDATE runs SET state=? WHERE id=?", (state, run_id))
        report = self.report(run_id)
        return {**report, "retried_count": len(retried), "retried_task_ids": retried}

    def recheck_source_blocked(self, run_id, reason):
        """Reinspect original evidence without granting translation approval."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("An explicit source reinspection reason is required")
        if self.source_repairer is None:
            raise ValueError("Independent source inspection is required")
        with self.connect() as con:
            run = self._run(con, run_id)
            self._bundle(run)
            if run['state'] in ('published', 'ready', 'source_changed'):
                raise ValueError("Terminal or source-changed workflows cannot recheck source")
            rows = self._rows(con, run_id)
            if any(r['lease'] and r['lease_until'] >= time.time() for r in rows):
                raise ValueError("Cannot recheck source while a workflow lease is active")
            selected = [r['id'] for r in rows if r['status'] == 'blocked']
        checked = []
        for task_id in selected:
            with self.connect() as con:
                con.execute('BEGIN IMMEDIATE')
                run = self._run(con, run_id)
                bundle = self._bundle(run)
                rows = self._rows(con, run_id)
                if any(r['lease'] and r['lease_until'] >= time.time() for r in rows):
                    raise ValueError("Cannot recheck source while a workflow lease is active")
                row = next(r for r in rows if r['id'] == task_id)
                latest = con.execute('SELECT event FROM events WHERE run_id=? AND task_id=? ORDER BY seq DESC LIMIT 1',
                                     (run_id, task_id)).fetchone()
                if row['status'] != 'blocked' or not latest or latest['event'] not in (
                        'source_repair_blocked', 'source_recheck_failed'):
                    continue
                finding = con.execute("SELECT payload FROM events WHERE run_id=? AND task_id=? AND event='source_findings' ORDER BY seq DESC LIMIT 1",
                                      (run_id, task_id)).fetchone()
                if not finding:
                    raise ValueError('Source block has no original observation evidence')
                response = json.loads(finding['payload'])['translation_response']
                request = self._request(bundle, row, rows, row['phase'])
                try:
                    images = self._with_images(bundle, json.loads(row['packet']), request)
                except (OSError, ValueError, RuntimeError) as exc:
                    self._event(con, run_id, task_id, 'source', 'source_recheck_failed',
                                {'error': str(exc), 'reason': reason, 'translation_approval_granted': False})
                    continue
                lease = uuid.uuid4().hex
                con.execute('UPDATE tasks SET lease=?,lease_until=? WHERE run_id=? AND id=?',
                            (lease, time.time() + self.lease_seconds, run_id, task_id))
                self._event(con, run_id, task_id, 'source', 'source_recheck_requested',
                            {'reason': reason, 'original_response_sha256': sha(response),
                             'translation_approval_granted': False})
            task = {'task_id': task_id, 'lease_id': lease, 'request': request, 'images': images}
            try:
                result = self._inspect_source_response(run_id, task, response)
                if result is None:
                    with self.connect() as con:
                        con.execute('BEGIN IMMEDIATE')
                        _, _, current = self._lease(con, run_id, task_id, lease)
                        status = ('pending' if not current['candidate'] else
                                  'repair_pending' if current['phase'] == 'repair' else 'drafted')
                        self._event(con, run_id, task_id, 'source', 'source_rechecked_requires_translation',
                                    {'candidate_preserved': True, 'previous_review': json.loads(current['review']) if current['review'] else None,
                                     'fresh_translation_review_required': True, 'translation_approval_granted': False})
                        con.execute("UPDATE tasks SET status=?,review=NULL,review_input_hash='',lease=NULL,lease_until=NULL WHERE run_id=? AND id=?",
                                    (status, run_id, task_id))
                        con.execute('UPDATE runs SET state=? WHERE id=?',
                                    ('generating' if status == 'pending' else 'reviewing', run_id))
                checked.append(task_id)
                if result and result['status'] == 'source_changed':
                    break
            except Exception as exc:
                with self.connect() as con:
                    con.execute('UPDATE tasks SET lease=NULL,lease_until=NULL WHERE run_id=? AND id=? AND lease=?',
                                (run_id, task_id, lease))
                    self._event(con, run_id, task_id, 'source', 'source_recheck_failed',
                                {'error': str(exc), 'reason': reason, 'translation_approval_granted': False})
        return {**self.report(run_id), 'source_rechecked_task_ids': checked}

    def observe_source_change(self, run_id):
        """Retire only an active run bound to a demonstrably different complete source.

        No error-message matching and no candidate migration: this reads the old
        stored identity without asserting it is current, checks the actual current
        PDF/snapshot/view, and revokes every old lease while preserving all payloads.
        A caller catching an error must re-raise it when this returns None.
        """
        keys = ('source_pdf_sha256', 'view_id', 'view_sha256', 'source_snapshot_sha256')
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            run = self._run(con, run_id)
            if run['state'] in ('published', 'ready', 'cancelled'):
                return None
            old = self.service.store.source_bundle(run['session_id'])
            if old['fingerprint'] != run['fingerprint'] or old['paper_id'] != run['paper_id']:
                raise ValueError('Stored workflow source identity is inconsistent')
            paper, _, _, identity = current_source(self.service.library, run['paper_id'])
            if paper['status'] != 'complete' or paper['current_view_id'] != identity['view_id']:
                raise ValueError('Current source is not a complete bound view')
            if any(not isinstance(identity.get(key), str) or not identity[key] for key in keys):
                raise ValueError('Current source identity is incomplete')
            old_identity = {key: old[key] for key in keys}
            new_identity = {key: identity[key] for key in keys}
            if old_identity == new_identity:
                return None
            if run['state'] != 'source_changed':
                rows = self._rows(con, run_id)
                con.execute("UPDATE runs SET state='source_changed' WHERE id=?", (run_id,))
                con.execute('UPDATE tasks SET lease=NULL,lease_until=NULL WHERE run_id=?', (run_id,))
                self._event(con, run_id, None, 'source', 'external_source_change_observed', {
                    'old_identity': old_identity, 'new_identity': new_identity,
                    'revoked_task_ids': [row['id'] for row in rows if row['lease']],
                    'task_payloads_preserved': True, 'prior_Korean_reusable': False,
                    'fresh_empty_translation_required': True,
                    'translation_approval_granted': False})
        return self.report(run_id)

    def report(self, run_id):
        with self.connect() as con:
            run = self._run(con, run_id)
            if run["state"] != "source_changed":
                self._bundle(run)
            rows = self._rows(con, run_id)
        counts = {
            s: sum(r["status"] == s for r in rows)
            for s in ("pending", "drafted", "accepted", "repair_pending", "blocked")
        }
        state = run["state"]
        if state not in ("published", "source_changed") and counts["blocked"]:
            state = "needs_review"
        return {
            "run_id": run_id,
            "session_id": run["session_id"],
            "paper_id": run["paper_id"],
            "status": state,
            "tasks": len(rows),
            "counts": counts,
            "leased": sum(bool(r["lease"]) for r in rows),
            "repairs_requested": sum(r["repair_count"] for r in rows),
            "errors": [
                {"task_id": r["id"], "phase": r["phase"], "error": r["last_error"]}
                for r in rows
                if r["last_error"]
            ],
            "prior_Korean_inputs": 0,
            "semantic_quality_proven": False,
        }

    def _figure_output_evidence(self, bundle, packet, candidate):
        """Use the actual reader projector, rather than infer emitted text from storage."""
        from .math_partition import project_translation_bundle

        try:
            result = self._normalize(bundle, packet, candidate)
            base = {'paper_id': bundle['paper_id'], 'view_id': bundle['view_id'],
                    'blocks': copy.deepcopy(bundle['document']['blocks'])}
            projected = project_translation_bundle(bundle, result, base)
            block_id = packet['figure']['block_id']
            original = next(b for b in base['blocks'] if b['id'] == block_id)
            actual = next(b for b in projected['blocks'] if b['id'] == block_id)
            emitted = actual['content'].get('translation_labels', [])
            without_labels = copy.deepcopy(actual)
            without_labels['content'].pop('translation_labels', None)
            if without_labels != original:
                raise ValueError('Figure projection changed original content')
            return {'projection_available': True, 'block_id': block_id,
                    'candidate_sha256': sha(candidate), 'original_raster_unchanged': True,
                    'original_crop_sha256': packet['figure']['crop_sha256'],
                    'emitted_translation_labels': emitted,
                    'translation_approval_granted': False}
        except (KeyError, ValueError, TypeError, StopIteration) as exc:
            return {'projection_available': False, 'error': str(exc),
                    'translation_approval_granted': False}

    def _inspect_source_response(self, run_id, task, response):
        """Route model observations to PDF-grounded source repair, never target editing."""
        if self.source_repairer is None:
            return None
        observations = response.get("source_concerns", []) + [
            dumps(i) for i in response.get("issues", [])
        ]
        if not observations:
            return None
        with self.connect() as con:
            run = self._run(con, run_id)
            bundle = self._bundle(run)
            row = con.execute('SELECT * FROM tasks WHERE run_id=? AND id=?',
                              (run_id, task['task_id'])).fetchone()
            if row is None:
                raise ValueError('Source inspection has no current translation task')
            current_candidate = (json.loads(row['candidate'])
                                 if row['phase'] == 'review' and row['candidate'] else response)
        packet = copy.deepcopy(task["request"]["task"])
        packet["context"] = task["request"].get("context", {})
        packet['candidate_to_inspect'] = copy.deepcopy(response)
        packet['current_translation_candidate'] = copy.deepcopy(current_candidate)
        if packet.get('kind') == 'figure':
            packet['figure_output_evidence'] = self._figure_output_evidence(bundle, packet, current_candidate)
        findings = self.source_repairer.scan(bundle, packet, observations, task["images"])
        if not findings:
            feedback_reader = getattr(self.source_repairer, "get_scan_feedback", None)
            feedback = (
                feedback_reader(bundle, packet["task_id"])
                if feedback_reader
                else {
                    "bundle_fingerprint": bundle["fingerprint"],
                    "task_id": packet["task_id"],
                    "findings": [],
                    "notes": [],
                }
            )
            if (
                feedback.get("bundle_fingerprint") != bundle["fingerprint"]
                or feedback.get("task_id") != packet["task_id"]
                or feedback.get("findings")
            ):
                raise ValueError("Source feedback must bind the exact no-correction scan")
            with self.connect() as con:
                self._lease(con, run_id, task["task_id"], task["lease_id"])
                con.execute(
                    "UPDATE tasks SET source_feedback=? WHERE run_id=? AND id=? AND lease=?",
                    (dumps(feedback), run_id, task["task_id"], task["lease_id"]),
                )
                self._event(
                    con,
                    run_id,
                    task["task_id"],
                    "source",
                    "source_checked_no_correction",
                    {
                        "feedback": feedback,
                        "response_sha256": sha(response),
                        "observations": observations,
                        "translation_quality_approved_by_this_scan": False,
                    },
                )
            return None
        serialized = [f.model_dump() for f in findings]
        with self.connect() as con:
            self._event(
                con,
                run_id,
                task["task_id"],
                "source",
                "source_findings",
                {"findings": serialized, "translation_response": response},
            )
            if any(f.kind == "unresolved" for f in findings):
                con.execute(
                    "UPDATE tasks SET status='blocked',last_error=?,lease=NULL,lease_until=NULL "
                    "WHERE run_id=? AND id=?",
                    (
                        "Unresolved PDF/source transcription: " + dumps(serialized),
                        run_id,
                        task["task_id"],
                    ),
                )
                self._event(
                    con,
                    run_id,
                    task["task_id"],
                    "source",
                    "source_repair_blocked",
                    {"findings": serialized, "translation_response_accepted": False},
                )
        if any(f.kind == "unresolved" for f in findings):
            return self.report(run_id)
        from .source_repair import SourceRepairBlocked

        try:
            repaired = self.source_repairer.repair(bundle, findings)
        except SourceRepairBlocked as exc:
            with self.connect() as con:
                con.execute('BEGIN IMMEDIATE')
                self._lease(con, run_id, task['task_id'], task['lease_id'])
                if exc.details.get('bundle_fingerprint') != bundle['fingerprint']:
                    raise ValueError('Source quality stop belongs to another source bundle') from exc
                con.execute("UPDATE tasks SET status='blocked',last_error=?,lease=NULL,lease_until=NULL "
                            'WHERE run_id=? AND id=? AND lease=?',
                            (str(exc), run_id, task['task_id'], task['lease_id']))
                self._event(con, run_id, task['task_id'], 'source', 'source_repair_blocked', {
                    'findings': serialized, 'source_repair_quality_stop': exc.details,
                    'bundle_fingerprint': bundle['fingerprint'],
                    'translation_response_accepted': False, 'translation_approval_granted': False})
            return self.report(run_id)
        if not repaired.get("report", {}).get("valid"):
            raise ValueError("Source repair requires review: " + dumps(repaired))
        with self.connect() as con:
            con.execute("UPDATE runs SET state='source_changed' WHERE id=?", (run_id,))
            con.execute("UPDATE tasks SET lease=NULL,lease_until=NULL WHERE run_id=?", (run_id,))
            self._event(
                con,
                run_id,
                task["task_id"],
                "source",
                "source_repaired_restart_required",
                {"repair": repaired, "prior_Korean_reusable": False},
            )
        return self.report(run_id)

    def step(self, run_id):
        if self.runner is None:
            raise ValueError("An AI runner is required for automatic execution")
        task = self.claim(run_id)
        if task is None:
            return self.report(run_id)
        directory = self.directory / "runs" / run_id / task["task_id"] / task["lease_id"]
        try:
            response = self.runner.run(task["request"], task["schema"], task["images"], directory)
            source_report = self._inspect_source_response(run_id, task, response)
            if source_report is not None:
                return source_report
        except Exception as exc:
            if getattr(exc, "kind", None) == "cancelled":
                with self.connect() as con:
                    con.execute("UPDATE runs SET state='cancelled' WHERE id=?", (run_id,))
                    con.execute(
                        "UPDATE tasks SET lease=NULL,lease_until=NULL WHERE run_id=? AND id=?",
                        (run_id, task["task_id"]),
                    )
                    self._event(
                        con,
                        run_id,
                        task["task_id"],
                        task["phase"],
                        "execution_cancelled",
                        {"error": str(exc), "failure_budget_consumed": False},
                    )
                return self.report(run_id)
            return self.fail(run_id, task["task_id"], task["lease_id"], str(exc))
        return self.submit(
            run_id,
            task["task_id"],
            task["lease_id"],
            response,
            evidence={
                "actor": "program-invoked independent CLI call",
                "artifacts": str(directory),
                "request_sha256": sha(task["request"]),
                "response_sha256": sha(response),
            },
        )

    def commit(self, run_id, publish=False):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            run = self._run(con, run_id)
            bundle = self._bundle(run)
            rows = self._rows(con, run_id)
            if run["state"] == "published":
                return self.report(run_id)
            if any(r["lease"] or r["status"] != "accepted" for r in rows):
                raise ValueError("Unfinished or unresolved AI tasks cannot be committed")
            result = self._candidate_result(bundle, rows)
            for row in rows:
                request = self._request(bundle, row, rows, "review")
                if row["review_input_hash"] != self._review_hash(request):
                    raise ValueError("Review context changed; claim and perform fresh review")
                if not self._review_valid(request, json.loads(row["review"])):
                    raise ValueError("Review has remaining issues")
                proof = {
                    "workflow": VERSION,
                    "run_id": run_id,
                    "task_id": row["id"],
                    "actual_review": json.loads(row["review"]),
                    "review_input_sha256": row["review_input_hash"],
                }
                review = {
                    "reviewer": "fresh program-invoked/connected AI review; same-family nonblind",
                    "method": dumps(proof),
                    "source_compared": True,
                    "unresolved_errors": [],
                }
                task = json.loads(row["packet"])
                normalized = self._normalize(bundle, task, json.loads(row["candidate"]))
                if row["kind"] == "text":
                    ids = set(task["source_unit_ids"])
                    for entry in result["entries"]:
                        if ids.intersection(entry["source_unit_ids"]):
                            entry["review"] = {
                                **review,
                                "context_unit_ids": request["context"]["context_unit_ids"],
                            }
                            entry["review"].update(entry_review_requirements(bundle, entry, result))
                elif row["kind"] == "math":
                    rolekeys = {(r["math_id"], r["node_index"]) for r in normalized["math_roles"]}
                    mathids = {o["math_id"] for o in normalized["math_overlays"]}
                    for role in result["math_roles"]:
                        if (role["math_id"], role["node_index"]) in rolekeys:
                            role["review"] = {**review, **math_requirements(bundle, role)}
                    for overlay in result["math_overlays"]:
                        if overlay["math_id"] in mathids:
                            overlay["review"] = {
                                **review,
                                **math_requirements(bundle, overlay, result),
                            }
                else:
                    for fig in result["figure_reviews"]:
                        if fig["block_id"] == task["figure"]["block_id"]:
                            fig["reviewer"] = dumps(proof)
                            fig["labels_sha256"] = sha(
                                {
                                    "labels": fig["labels"],
                                    "no_translatable_text_reason": fig[
                                        "no_translatable_text_reason"
                                    ],
                                }
                            )
            validation = verify_result(bundle, result, require_complete=True)
            if not validation["complete"]:
                raise ValueError("Full source-bound final validation failed: " + dumps(validation))
            if bundle.get("math_partition"):
                from .math_partition import project_translation_bundle
                project_translation_bundle(bundle, result, {"paper_id":bundle["paper_id"],
                    "view_id":bundle["view_id"], "blocks":copy.deepcopy(bundle["document"]["blocks"])})
            saved = self.service.store.read(run["session_id"], bundle["fingerprint"])
            if saved["result"] != result and (
                saved["revision"] != 0
                or any(
                    saved["result"].get(k)
                    for k in ("entries", "figure_reviews", "math_roles", "math_overlays")
                )
            ):
                raise ValueError(
                    "Workflow session was edited externally; do not overwrite its content"
                )
            if saved["status"] == "draft" and saved["result"] != result:
                self.service.store.save(
                    run["session_id"],
                    bundle["fingerprint"],
                    saved["revision"],
                    entries=result["entries"],
                    figure_reviews=result["figure_reviews"],
                    math_roles=result["math_roles"],
                    math_overlays=result["math_overlays"],
                )
            saved = self.service.store.read(run["session_id"], bundle["fingerprint"])
            if publish:
                self.service.finalize(run["session_id"], saved["revision"])
            con.execute(
                "UPDATE runs SET state=? WHERE id=?", ("published" if publish else "ready", run_id)
            )
            self._event(
                con,
                run_id,
                None,
                "commit",
                "published" if publish else "validated_draft",
                {
                    "complete": True,
                    "structural_validation": validation,
                    "external_publication": False,
                },
                sha(result),
            )
        return self.report(run_id)
