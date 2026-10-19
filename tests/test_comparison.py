import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from urllib.request import url2pathname

import pymupdf
import pytest

from pairper.document.comparison import comparison_image
from pairper.document.models import Block, Content, Region, TextRun
from pairper.document.presentation import display_document


def saved_document(library, tmp_path):
    path = tmp_path / "columns.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=600, height=800)
        page.insert_text((40, 70), "Left column line one.")
        page.insert_text((40, 90), "Left column line two.")
        page.insert_text((350, 70), "Right column line one.")
        page = pdf.new_page(width=600, height=800)
        page.insert_text((40, 70), "Second page.")
        pdf.save(path)
    paper = library.register(path)
    refs = [s["id"] for p in library.source(paper["id"])["pages"] for s in p["spans"]]
    blocks = [
        Block(
            id="paragraph",
            type="paragraph",
            order=0,
            source_refs=refs,
            content=Content(runs=[TextRun(refs=refs)]),
        ),
        Block(
            id="figure",
            type="figure",
            order=1,
            content=Content(region=Region(page=2, bbox=(30, 100, 500, 500))),
        ),
    ]
    job = library.begin(paper["id"], paper["sha256"])["job_id"]
    library.append(job, blocks, [], [1, 2])
    assert library.validate(job, finalize=True)["valid"]
    return paper, display_document(library, paper["id"])


def test_comparison_keeps_lines_columns_and_pages_separate(library, tmp_path):
    paper, document = saved_document(library, tmp_path)
    regions = document["blocks"][0]["source_regions"]
    assert len(regions) == 4
    assert [r["page"] for r in regions] == [1, 1, 1, 2]
    assert all(r["bbox"][2] - r["bbox"][0] < 200 for r in regions)
    before = library.db.read_bytes()
    for block in document["blocks"]:
        for region in block["source_regions"]:
            image = comparison_image(
                library, paper["id"], document["view_id"], block["id"], region["id"]
            )
            assert image["page"] == region["page"]
            path = Path(url2pathname(urlparse(image["image_url"]).path))
            with pymupdf.open(path) as rendered:
                assert rendered[0].rect.width <= 1600
            assert path.stat().st_size < 4_000_000
    assert library.db.read_bytes() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("paper", "../secret"),
        ("view", "a" * 32),
        ("block", "../secret"),
        ("block", "unknown"),
        ("region", "r99999"),
        ("region", "../../file"),
    ],
)
def test_comparison_rejects_untrusted_or_stale_identifiers(library, tmp_path, field, value):
    paper, document = saved_document(library, tmp_path)
    ids = dict(paper=paper["id"], view=document["view_id"], block="paragraph", region="r0")
    ids[field] = value
    with pytest.raises(ValueError):
        comparison_image(library, *ids.values())


def test_invalid_coordinates_and_changed_original_fail_without_guessing(library, tmp_path):
    paper, document = saved_document(library, tmp_path)
    source_path = library.root / "sources" / f"{paper['id']}.json"
    source = json.loads(source_path.read_text("utf-8"))
    source["pages"][0]["spans"][0]["bbox"] = [0, 0, 800, 900]
    source_path.write_text(json.dumps(source), "utf-8")
    block = display_document(library, paper["id"])["blocks"][0]
    assert block["source_regions"] == [] and block["source_error"]
    with pytest.raises(ValueError):
        comparison_image(library, paper["id"], document["view_id"], "paragraph", "r0")
    library.managed(paper["original_path"]).write_bytes(b"changed PDF")
    with pytest.raises(ValueError, match="hash mismatch"):
        comparison_image(library, paper["id"], document["view_id"], "figure", "r0")


def test_original_pdf_bounds_override_invalid_extraction_metadata(library, tmp_path):
    paper, document = saved_document(library, tmp_path)
    source_path = library.root / "sources" / f"{paper['id']}.json"
    source = json.loads(source_path.read_text("utf-8"))
    source["pages"][0]["width"] = 2000
    source["pages"][0]["spans"][0]["bbox"] = [0, 0, 1000, 200]
    source_path.write_text(json.dumps(source), "utf-8")
    with pytest.raises(ValueError, match="outside original PDF"):
        comparison_image(library, paper["id"], document["view_id"], "paragraph", "r0")


def test_ui_library_uses_actual_filename_and_latest_error(library, pdf_path):
    from pairper.ui.bridge import FrontendBridge

    paper = library.register(pdf_path)
    job = library.begin(paper["id"], paper["sha256"])["job_id"]
    with library.connect() as con:
        con.execute("UPDATE papers SET metadata='{}',title='' WHERE id=?", (paper["id"],))
        con.execute("UPDATE jobs SET error='Actual saved failure' WHERE id=?", (job,))
    result = FrontendBridge.paper_list(SimpleNamespace(library=library))[0]
    assert result["title"] == pdf_path.name
    assert result["error"] == "Actual saved failure"
    assert result["original_available"] and not result["has_view"]
    assert result["created_at"] == paper["created_at"]
