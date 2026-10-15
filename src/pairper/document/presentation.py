"""Resolve source references in Python. The UI receives only display data and asset URLs."""

from pairper.document.models import Document, MathRun
from pairper.document.text import resolved_runs

DISPLAY_SCHEMA_VERSION = "1.0"


def display_document(library, paper_id):
    paper = library.paper(paper_id)
    if not library.valid_view(paper):
        raise ValueError("유효한 저장 문서가 없습니다.")
    with library.connect() as con:
        view = con.execute(
            "SELECT json_path FROM views WHERE id=?", (paper["current_view_id"],)
        ).fetchone()
    document = Document.model_validate_json(library.managed(view["json_path"]).read_text("utf-8"))
    if document.paper_id != paper_id or document.source_sha256 != paper["sha256"]:
        raise ValueError("문서와 원본의 연결이 일치하지 않습니다.")
    source = library.source(paper_id)
    spans = {s["id"]: {**s, "page": p["page"]} for p in source["pages"] for s in p["spans"]}
    audit = []

    def asset(region):
        return library.managed(f"assets/{library.asset(paper_id, region)}.png").as_uri()

    def runs(items):
        result = []
        for run, prefix, text in resolved_runs(items, spans, audit):
            if isinstance(run, MathRun):
                result.append(
                    {
                        "kind": "math",
                        "prefix": prefix,
                        "latex": run.latex,
                        "source_verified": run.source_verified,
                        "image_url": asset(run.region),
                    }
                )
            else:
                result.append({"kind": "text", "prefix": prefix, "text": text})
        return result

    blocks = []
    for block in sorted(document.blocks, key=lambda b: b.order):
        c = block.content
        content = {
            "level": c.level,
            "ordered": c.ordered,
            "latex": c.latex,
            "source_verified": c.source_verified,
            "caption_id": c.caption_id,
            "number": " ".join(spans[r]["text"] for r in c.number_refs),
            "image_url": asset(c.region) if c.region else None,
            "runs": runs(c.runs),
            "items": [runs(item) for item in c.items],
            "cells": [
                {
                    "row": cell.row,
                    "column": cell.column,
                    "rowspan": cell.rowspan,
                    "colspan": cell.colspan,
                    "header": cell.header,
                    "runs": runs(cell.runs),
                }
                for cell in (c.cells if block.type == "table" and c.source_verified else [])
            ],
        }
        blocks.append(
            {
                "id": block.id,
                "type": block.type,
                "order": block.order,
                "validation_status": block.validation_status,
                "content": content,
                "source_trace": [
                    {
                        "id": ref,
                        "text": spans[ref]["text"],
                        "page": spans[ref]["page"],
                        "bbox": spans[ref]["bbox"],
                    }
                    for ref in block.source_refs
                ],
            }
        )
    return {
        "display_schema_version": DISPLAY_SCHEMA_VERSION,
        "source_schema_version": document.schema_version,
        "paper_id": paper_id,
        "view_id": paper["current_view_id"],
        "blocks": blocks,
        "normalization_audit": audit,
    }
