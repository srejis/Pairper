import hashlib
import json

import pymupdf
import pytest


def test_native_evidence_and_pagination_do_not_rewrite_source(library, tmp_path):
    path = tmp_path / "evidence.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((40, 60), "Alpha beta " * 30, fontsize=10, color=(1, 0, 0))
        page.draw_rect((40, 90, 180, 150), color=(0, 0, 1))
        page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(40, 45, 100, 65),
                          "uri": "https://example.org/paper"})
        pdf.set_toc([[1, "A real bookmark", 1]])
        pdf.save(path)
    paper = library.register(path)
    source_path = library.managed(f"sources/{paper['id']}.json")
    before = source_path.read_bytes()
    manifest = library.manifest(paper["id"])["pdf_evidence"]
    assert manifest["outlines"][0][:3] == [1, "A real bookmark", 1]
    assert manifest["logical_structure"]["status"] == "not_present"
    summary = library.page_evidence(paper["id"], 1)
    assert summary["counts"]["characters"] > 100
    all_chars, offset = [], 0
    while offset is not None:
        result = library.page_evidence(paper["id"], 1, "characters", offset, 31)
        all_chars.extend(result["items"])
        offset = result["next_offset"]
    source = library.page_content(paper["id"], 1)
    assert "".join(c["c"] for c in all_chars) == "".join(s["text"] for s in source["spans"])
    assert all(c["ref"] in {s["id"] for s in source["spans"]} for c in all_chars)
    lines = library.page_evidence(paper["id"], 1, "lines")["items"]
    assert lines[0]["spans"][0]["color"] == 0xFF0000
    assert lines[0]["spans"][0]["origin"]
    assert library.page_evidence(paper["id"], 1, "paths")["items"][0]["items"]
    assert library.page_evidence(paper["id"], 1, "links")["items"][0]["uri"] == "https://example.org/paper"
    assert library.page_evidence(paper["id"], 1) == summary
    assert source_path.read_bytes() == before
    assert hashlib.sha256(path.read_bytes()).hexdigest() == paper["sha256"]
    with pytest.raises(ValueError, match="Invalid evidence"):
        library.page_evidence(paper["id"], 1, "characters", -1)
    with pytest.raises(ValueError, match="Invalid evidence"):
        library.page_evidence(paper["id"], 1, "characters", 0, 201)
    with pytest.raises(ValueError, match="outside"):
        library.page_evidence(paper["id"], 2)
    library.managed(paper["original_path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash"):
        library.page_evidence(paper["id"], 1)


def test_table_detection_is_only_an_unverified_proposal(library, tmp_path):
    path = tmp_path / "table.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        for x in (40, 140, 240):
            page.draw_line((x, 100), (x, 200))
        for y in (100, 150, 200):
            page.draw_line((40, y), (240, y))
        for x, y, text in [(50, 130, "Name"), (150, 130, "Value"),
                           (50, 180, "Alpha"), (150, 180, "42")]:
            page.insert_text((x, y), text)
        pdf.save(path)
    paper = library.register(path)
    result = library.page_evidence(paper["id"], 1, "table_candidates")
    assert result["total"] == 1
    table = result["items"][0]
    assert table["rows"] == table["columns"] == 2
    assert table["text"] == [["Name", "Value"], ["Alpha", "42"]]
    assert table["source_verified"] is False
    assert table["provenance"] == "pymupdf_find_tables_inference"
    assert len(table["refs"]) == 4
    json.dumps(result)  # All path/cell geometry is JSON, never geometry repr strings.
