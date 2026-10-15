"""Shared GUI/MCP service. SQLite owns build state; files are immutable snapshots."""

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from platformdirs import user_data_path

from pairper.document.models import Block, Document, Omission, Region
from pairper.pdf.source import extract, extract_embedded_images, render, sha256


def now():
    return datetime.now(UTC).isoformat()


def write_json(path: Path, value):
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        # Windows readers briefly hold a sharing lock; retain atomic replacement and retry.
        for attempt in range(6):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        temp.unlink(missing_ok=True)


class Library:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(
            root
            or os.environ.get("PAIRPER_DATA_DIR")
            or user_data_path("Pairper", appauthor=False) / "library"
        ).resolve()
        for folder in ("originals", "sources", "views", "assets", "staging"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        self.db = self.root / "library.db"
        with self.connect() as con:
            con.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS papers (
                    id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, original_path TEXT NOT NULL,
                    filename TEXT NOT NULL, title TEXT NOT NULL, metadata TEXT NOT NULL,
                    page_count INTEGER NOT NULL, created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'registered', current_view_id TEXT);
                CREATE TABLE IF NOT EXISTS views (
                    id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id),
                    source_sha256 TEXT NOT NULL, schema_version TEXT NOT NULL,
                    json_path TEXT NOT NULL, report TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id),
                    stage TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    error TEXT, staging_path TEXT NOT NULL, document TEXT NOT NULL, report TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_build ON jobs(paper_id)
                    WHERE stage != 'finalized';
            """)

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.db, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def managed(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Path outside library")
        return path

    def paper(self, paper_id: str) -> dict:
        with self.connect() as con:
            row = con.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        if not row:
            raise ValueError("Unknown paper ID")
        return dict(row)

    def list_papers(self, pending=False):
        with self.connect() as con:
            rows = con.execute("SELECT * FROM papers ORDER BY created_at DESC").fetchall()
        papers = []
        for row in rows:
            paper = dict(row)
            paper["has_view"] = self.valid_view(paper)
            if not pending or not paper["has_view"]:
                papers.append(paper)
        return papers

    def valid_view(self, paper):
        if not paper["current_view_id"]:
            return False
        try:
            with self.connect() as con:
                row = con.execute(
                    "SELECT * FROM views WHERE id=?", (paper["current_view_id"],)
                ).fetchone()
            doc = Document.model_validate_json(self.managed(row["json_path"]).read_text("utf-8"))
            return (
                doc.source_sha256 == paper["sha256"]
                and doc.paper_id == paper["id"]
                and json.loads(row["report"])["valid"]
            )
        except (OSError, ValueError, TypeError):
            return False

    def register(self, path: str | Path):
        path = Path(path)
        # Snapshot the bytes before inspecting, avoiding source-change races during import.
        temp = self.root / "staging" / (uuid.uuid4().hex + ".pdf")
        try:
            with path.open("rb") as src, temp.open("wb") as dst:
                import shutil

                shutil.copyfileobj(src, dst)
            digest = sha256(temp)
            with self.connect() as con:
                con.execute("BEGIN IMMEDIATE")
                old = con.execute("SELECT * FROM papers WHERE sha256=?", (digest,)).fetchone()
                if old:
                    return dict(old)
                source = extract(temp, digest)
                source["embedded_images"] = extract_embedded_images(temp, self.root / "assets")
                paper_id = uuid.uuid4().hex
                relative = f"originals/{digest}.pdf"
                os.replace(temp, self.managed(relative))
                write_json(self.root / "sources" / f"{paper_id}.json", source)
                con.execute(
                    "INSERT INTO papers (id,sha256,original_path,filename,title,metadata,"
                    "page_count,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        paper_id,
                        digest,
                        relative,
                        path.name,
                        source["metadata"].get("title") or path.stem,
                        json.dumps(source["metadata"]),
                        len(source["pages"]),
                        now(),
                    ),
                )
            return self.paper(paper_id)
        finally:
            temp.unlink(missing_ok=True)

    def source(self, paper_id):
        paper = self.paper(paper_id)
        if sha256(self.managed(paper["original_path"])) != paper["sha256"]:
            raise ValueError("Original PDF hash mismatch; restore the original")
        data = json.loads((self.root / "sources" / f"{paper['id']}.json").read_text("utf-8"))
        if data["sha256"] != paper["sha256"]:
            raise ValueError("Extracted source hash mismatch")
        return data

    def manifest(self, paper_id):
        paper = self.paper(paper_id)
        source = self.source(paper_id)
        return {
            **paper,
            "metadata": source["metadata"],
            "pages": [
                {
                    "page": p["page"],
                    "width": p["width"],
                    "height": p["height"],
                    "text_spans": len(p["spans"]),
                    "images": len(p["images"]),
                    "drawings": len(p["drawings"]),
                    "text_unavailable": not p["spans"],
                }
                for p in source["pages"]
            ],
        }

    def page_content(self, paper_id, page):
        source = self.source(paper_id)
        if not 1 <= page <= len(source["pages"]):
            raise ValueError("Page outside document")
        return source["pages"][page - 1]

    def image(self, paper_id, page, region=None, scale=1.5):
        paper = self.paper(paper_id)
        return render(
            self.managed(paper["original_path"]), self.source(paper_id), page, region, scale
        )

    def asset(self, paper_id: str, region: Region):
        key = uuid.uuid5(uuid.NAMESPACE_URL, paper_id + region.model_dump_json()).hex
        path = self.root / "assets" / f"{key}.png"
        if not path.exists():
            data = self.image(paper_id, region.page, region, 2)
            temp = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
            temp.write_bytes(data)
            os.replace(temp, path)
        return key

    def begin(self, paper_id, source_sha256):
        paper = self.paper(paper_id)
        if paper["sha256"] != source_sha256:
            raise ValueError("Source hash mismatch")
        self.source(paper_id)
        if self.valid_view(paper):
            raise ValueError("Paper already has a valid view; keep the existing view")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            latest = dict(con.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone())
            if self.valid_view(latest):
                raise ValueError("Paper already has a valid view; keep the existing view")
            old = con.execute(
                "SELECT id FROM jobs WHERE paper_id=? AND stage!='finalized'", (paper_id,)
            ).fetchone()
            if old:
                return {"job_id": old["id"], "resumed": True}
            job_id = uuid.uuid4().hex
            doc = Document(paper_id=paper_id, source_sha256=source_sha256)
            staging = f"staging/{job_id}.json"
            write_json(self.managed(staging), doc.model_dump(mode="json"))
            con.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    paper_id,
                    "building",
                    now(),
                    now(),
                    None,
                    staging,
                    doc.model_dump_json(),
                    None,
                ),
            )
            con.execute("UPDATE papers SET status='processing' WHERE id=?", (paper_id,))
        return {"job_id": job_id, "resumed": False}

    def job(self, job_id, con):
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError("Unknown build ID")
        return dict(row)

    def append(
        self,
        job_id,
        blocks: list[Block],
        omissions: list[Omission],
        visual_review_pages: list[int],
        remove_block_ids: list[str] | None = None,
        remove_omission_refs: list[str] | None = None,
    ):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            job = self.job(job_id, con)
            if job["stage"] == "finalized":
                raise ValueError("Build already finalized")
            doc = Document.model_validate_json(job["document"])
            # Same IDs replace in place for repairs and idempotent retried batches.
            existing = {b.id: b for b in doc.blocks}
            for block_id in remove_block_ids or []:
                existing.pop(block_id, None)
            for block in blocks:
                existing[block.id] = block
            doc.blocks = list(existing.values())
            omit = {o.ref: o for o in doc.omissions}
            for ref in remove_omission_refs or []:
                omit.pop(ref, None)
            omit.update({o.ref: o for o in omissions})
            doc.omissions = list(omit.values())
            doc.visual_review_pages = sorted(set(doc.visual_review_pages + visual_review_pages))
            write_json(self.managed(job["staging_path"]), doc.model_dump(mode="json"))
            con.execute(
                "UPDATE jobs SET document=?,stage='building',report=NULL,error=NULL,"
                "updated_at=? WHERE id=?",
                (doc.model_dump_json(), now(), job_id),
            )
        return {"blocks": len(doc.blocks), "job_id": job_id}

    def validate(self, job_id, finalize=False):
        from pairper.document.renderer import render_document
        from pairper.validation.checks import validate_document

        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            job = self.job(job_id, con)
            if job["stage"] == "finalized":
                return json.loads(job["report"])
            doc = Document.model_validate_json(job["document"])
            fatal_error = None
            try:
                report = validate_document(self, doc)
                if report["valid"]:
                    render_document(self, doc, self.managed(f"staging/{job_id}.html"))
            except (ValueError, OSError, RuntimeError) as exc:
                fatal_error = str(exc)
                report = {"valid": False, "errors": [str(exc)], "warnings": []}
            stage = "validated" if report["valid"] else "review_required"
            if fatal_error:
                stage = "failed"
            if finalize and report["valid"]:
                view_id = uuid.uuid4().hex
                relative = f"views/{view_id}.json"
                write_json(self.managed(relative), doc.model_dump(mode="json"))
                render_document(self, doc, self.managed(f"views/{view_id}.html"))
                con.execute(
                    "INSERT INTO views VALUES (?,?,?,?,?,?,?,?)",
                    (
                        view_id,
                        doc.paper_id,
                        doc.source_sha256,
                        doc.schema_version,
                        relative,
                        json.dumps(report),
                        now(),
                        now(),
                    ),
                )
                con.execute(
                    "UPDATE papers SET current_view_id=?,status='complete' WHERE id=?",
                    (view_id, doc.paper_id),
                )
                stage = "finalized"
                report["view_id"] = view_id
            elif not report["valid"]:
                con.execute(
                    "UPDATE papers SET status=? WHERE id=? AND current_view_id IS NULL",
                    ("failed" if fatal_error else "review_required", doc.paper_id),
                )
            con.execute(
                "UPDATE jobs SET stage=?,report=?,document=?,updated_at=?,error=? WHERE id=?",
                (stage, json.dumps(report), doc.model_dump_json(), now(), fatal_error, job_id),
            )
        return report

    def report(self, job_id):
        with self.connect() as con:
            job = self.job(job_id, con)
        return {
            "job_id": job_id,
            "stage": job["stage"],
            "updated_at": job["updated_at"],
            "error": job["error"],
            "report": json.loads(job["report"]) if job["report"] else None,
            "document": json.loads(job["document"]),
        }

    def view_path(self, paper_id):
        paper = self.paper(paper_id)
        if not self.valid_view(paper):
            raise ValueError("No valid saved view")
        with self.connect() as con:
            row = con.execute(
                "SELECT json_path FROM views WHERE id=?", (paper["current_view_id"],)
            ).fetchone()
        path = self.managed(row["json_path"])
        from pairper.document.renderer import render_document

        render_document(
            self, Document.model_validate_json(path.read_text("utf-8")), path.with_suffix(".html")
        )
        return path.with_suffix(".html")
