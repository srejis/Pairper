"""Read PDF evidence without rewriting the stable source spans or inferring semantics."""

import hashlib
import json

import pymupdf

EVIDENCE_VERSION = 1
RUNTIME_VERSION = pymupdf.VersionBind
SECTIONS = {"lines", "characters", "paths", "links", "annotations", "table_candidates"}


def plain(value):
    """Convert MuPDF geometry, not its string representation, into JSON data."""
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, pymupdf.Rect, pymupdf.Point, pymupdf.Quad,
                          pymupdf.Matrix)):
        return [plain(item) for item in value]
    return value


def document_evidence(path, digest):
    with pymupdf.open(path) as pdf:
        tagged = pdf.xref_get_key(pdf.pdf_catalog(), "StructTreeRoot")[0] != "null"
        return {
            "version": EVIDENCE_VERSION,
            "source_sha256": digest,
            "pymupdf_version": pymupdf.VersionBind,
            "metadata": pdf.metadata,
            "xml_metadata": pdf.get_xml_metadata(),
            "outlines": plain(pdf.get_toc(simple=False)),
            "embedded_files": pdf.embfile_names(),
            "logical_structure": {
                "present": tagged,
                "status": "requires_structure_reader" if tagged else "not_present",
                "note": "Text extraction blocks/lines are not PDF semantic tags. "
                        "A Formula tag, if present, does not imply available LaTeX.",
            },
        }


def source_fingerprint(page):
    data = [(s["id"], s["text"], s["bbox"], s["block"], s["line"])
            for s in page["spans"]]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def page_evidence(path, digest, original):
    number = original["page"]
    locations = {}
    for span in original["spans"]:
        index = int(span["id"].rsplit("-s", 1)[1])
        locations[(span["block"], span["line"], index)] = span
    lines, characters, unmatched = [], [], []
    with pymupdf.open(path) as pdf:
        page = pdf[number - 1]
        page.set_rotation(0)
        for bi, block in enumerate(page.get_text("rawdict", sort=False)["blocks"]):
            for li, line in enumerate(block.get("lines", [])):
                refs, details, pieces = [], [], []
                for si, span in enumerate(line["spans"]):
                    text = "".join(c["c"] for c in span["chars"])
                    if not text:
                        continue
                    source = locations.get((bi, li, si))
                    matched = source is not None and source["text"] == text and all(
                        abs(a - b) < .01 for a, b in zip(source["bbox"], span["bbox"], strict=True)
                    )
                    ref = source["id"] if matched else None
                    if ref:
                        refs.append(ref)
                    else:
                        unmatched.append({"block": bi, "line": li, "span": si, "text": text})
                    pieces.append(text)
                    details.append({
                        "ref": ref, "text": text,
                        **{k: plain(span[k]) for k in (
                            "bbox", "origin", "font", "size", "flags", "color", "alpha",
                            "ascender", "descender", "char_flags",
                        ) if k in span},
                    })
                    for ci, char in enumerate(span["chars"]):
                        characters.append({
                            "ref": ref, "index": ci, "block": bi, "line": li,
                            **{k: plain(char[k]) for k in ("c", "bbox", "origin", "synthetic")
                               if k in char},
                        })
                lines.append({
                    "block": bi, "line": li, "bbox": plain(line["bbox"]),
                    "direction": plain(line["dir"]), "writing_mode": line["wmode"],
                    "refs": refs, "text": "".join(pieces), "spans": details,
                })
        paths = plain(page.get_drawings(extended=True))
        links = plain(page.get_links())
        annotations = [{"xref": a.xref, "type": plain(a.type), "bbox": plain(a.rect),
                        "info": a.info} for a in page.annots() or []]
    return {
        "version": EVIDENCE_VERSION, "pymupdf_version": pymupdf.VersionBind,
        "source_sha256": digest, "source_fingerprint": source_fingerprint(original),
        "page": number, "coordinate_system": "unrotated_pdf_points_top_left",
        "provenance": {
            "lines": "pymupdf_text_extraction_not_semantic_paragraphs",
            "characters": "pymupdf_character_extraction_synthetic_flag_preserved",
            "paths": "pdf_paint_operations", "links": "pdf_link_annotations",
            "annotations": "pdf_annotations",
        },
        "lines": lines, "characters": characters, "paths": paths,
        "links": links, "annotations": annotations, "unmatched_spans": unmatched,
        "warnings": [("Coordinates and fonts do not prove paragraphs, reading order or math. "
                      "Spaces marked synthetic were inferred by the extractor.")],
    }


def table_candidates(path, original):
    """Library-derived proposals. Never mark cells or inferred text as source verified."""
    with pymupdf.open(path) as pdf:
        page = pdf[original["page"] - 1]
        page.set_rotation(0)
        finder = page.find_tables()
        result = []
        for i, table in enumerate(finder.tables):
            result.append({
                "id": f"p{original['page']}-table-candidate-{i}",
                "provenance": "pymupdf_find_tables_inference", "source_verified": False,
                "bbox": plain(table.bbox), "rows": table.row_count,
                "columns": table.col_count, "cells": plain(table.cells),
                "text": table.extract(),
                "header": {"bbox": plain(table.header.bbox), "names": table.header.names,
                           "external": table.header.external},
                "refs": [s["id"] for s in original["spans"]
                         if pymupdf.Rect(table.bbox).intersects(pymupdf.Rect(s["bbox"]))],
            })
        return result


def evidence_page(data, section, offset=0, limit=100):
    if section not in SECTIONS | {"summary"}:
        raise ValueError("Unknown evidence section")
    if offset < 0 or not 1 <= limit <= 200:
        raise ValueError("Evidence offset must be nonnegative and limit between 1 and 200")
    common = {k: data[k] for k in ("source_sha256", "page", "coordinate_system")}
    if section == "summary":
        return {**common, "provenance": data["provenance"],
                "counts": {k: len(data[k]) for k in SECTIONS if k in data},
                "unmatched_spans": data["unmatched_spans"], "warnings": data["warnings"],
                "table_candidates": "Request separately; proposals require source review."}
    items = data[section]
    end = min(offset + limit, len(items))
    return {**common, "section": section, "items": items[offset:end], "total": len(items),
            "next_offset": end if end < len(items) else None,
            "provenance": data["provenance"].get(section, "pymupdf_find_tables_inference")}
