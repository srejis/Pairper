"""Resolve source references in Python. The UI receives only display data and asset URLs."""

from pairper.document.comparison import region_descriptors
from pairper.document.models import Document, MathRun
from pairper.document.structure import block_tree
from pairper.document.text import resolved_runs

DISPLAY_SCHEMA_VERSION = "1.0"


def load_document(library, paper_id, view_id=None):
    paper = library.paper(paper_id)
    if not library.valid_view(paper):
        raise ValueError("유효한 저장 문서가 없습니다.")
    if view_id is not None and paper["current_view_id"] != view_id:
        raise ValueError("저장 문서가 변경되었습니다. 다시 열어 주세요.")
    with library.connect() as con:
        view = con.execute(
            "SELECT json_path FROM views WHERE id=?", (paper["current_view_id"],)
        ).fetchone()
    document = Document.model_validate_json(library.managed(view["json_path"]).read_text("utf-8"))
    if document.paper_id != paper_id or document.source_sha256 != paper["sha256"]:
        raise ValueError("문서와 원본의 연결이 일치하지 않습니다.")
    return paper, document


def display_document(library, paper_id):
    # A view can retain its ID while a source repair replaces its JSON snapshot.
    # Bind the exact bytes rendered, not merely the paper/view labels.
    from pairper.translation.prepare import current_source

    snapshot = library.paper(paper_id)
    source_identity = (current_source(library, paper_id)[3]
                       if snapshot.get('current_view_id') and snapshot['status'] == 'complete'
                       else None)
    paper, document = load_document(library, paper_id)
    source = library.source(paper_id)
    spans = {s["id"]: {**s, "page": p["page"]} for p in source["pages"] for s in p["spans"]}
    audit = []
    block_tree(document.blocks)  # Reject malformed nesting before it reaches recursive UI rendering.

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
                result.append({"kind": "text", "prefix": prefix, "text": text,
                               **({"bold": True} if run.bold else {}),
                               **({"italic": True} if run.italic else {}),
                               **({"underline": True} if run.underline else {}),
                               **({"superscript": True} if run.superscript else {})})
        return result

    blocks = []
    for block in sorted(document.blocks, key=lambda b: b.order):
        regions, source_error = region_descriptors(block, source, spans)
        c = block.content
        content = {
            "level": c.level,
            "ordered": c.ordered,
            "latex": c.latex,
            "source_verified": c.source_verified,
            "caption_id": c.caption_id,
            "group_role": c.group_role,
            "language": c.language,
            "number": " ".join(spans[r]["text"] for r in c.number_refs),
            "image_url": asset(c.region) if c.region else None,
            "runs": runs(c.runs),
            "items": [runs(item) for item in c.items],
            "code_lines": [{"indent": line.indent, "runs": runs(line.runs)}
                           for line in c.code_lines],
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
                "parent_id": block.parent_id,
                "relations": [r.model_dump() for r in block.relations],
                "validation_status": block.validation_status,
                "source_regions": regions,
                "source_error": source_error,
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
    if source_identity is not None and current_source(library, paper_id)[3] != source_identity:
        raise ValueError('Source changed while rendering; reload the document')
    return {
        "display_schema_version": DISPLAY_SCHEMA_VERSION,
        "source_schema_version": document.schema_version,
        "paper_id": paper_id,
        "view_id": paper["current_view_id"],
        "source_identity": source_identity,
        "blocks": blocks,
        "normalization_audit": audit,
    }
