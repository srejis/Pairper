import json

import pymupdf
from conftest import text_blocks

from pairper.document.models import Block, Content, Region


def clipped_pdf(path):
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=200, height=200)
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 2, 2), False)
        pixmap.clear_with(100)
        page.insert_image((0, 0, 200, 200), stream=pixmap.tobytes("png"))
        page.draw_rect((-50, -50, 250, 250), color=(1, 0, 0), fill=(0, 1, 0))
        contents = b"".join(pdf.xref_stream(xref) for xref in page.get_contents())
        xref = pdf.get_new_xref()
        pdf.update_object(xref, "<<>>")
        pdf.update_stream(xref, b"q 20 20 40 40 re W n\n" + contents + b"\nQ")
        page.set_contents(xref)
        page.insert_text((20, 40), "Visible caption")
        pdf.save(path)


def test_clipped_vectors_and_images_use_visible_bounds(library, tmp_path):
    path = tmp_path / "clipped.pdf"
    clipped_pdf(path)
    paper = library.register(path)
    page = library.page_content(paper["id"], 1)
    assert page["images"][0]["bbox"] != page["images"][0]["visible_bbox"]
    assert page["images"][0]["visible_bbox"] == [20, 140, 60, 180]
    assert page["drawings"][0]["bbox"][0] == -50
    assert page["drawings"][0]["visible_bbox"] == [20, 140, 60, 180]
    job = library.begin(paper["id"], paper["sha256"])["job_id"]
    blocks = text_blocks(library, paper)
    library.append(job, blocks, [], [1])
    assert not library.validate(job)["valid"]  # Visible artwork still must be covered.
    blocks.append(Block(id="figure", type="figure", order=10,
                        content=Content(region=Region(page=1, bbox=(19, 139, 61, 181)))))
    library.append(job, blocks, [], [1])
    assert library.validate(job)["valid"]


def test_old_visual_cache_refresh_keeps_original_spans_and_pdf(library, tmp_path):
    path = tmp_path / "old-clipped.pdf"
    clipped_pdf(path)
    paper = library.register(path)
    source = library.source(paper["id"])
    spans = source["pages"][0]["spans"]
    original_bytes = library.managed(paper["original_path"]).read_bytes()
    source["extractor_version"] = 1
    for page in source["pages"]:
        for item in page["images"] + page["drawings"]:
            item.pop("visible_bbox", None)
    (library.root / "sources" / f"{paper['id']}.json").write_text(json.dumps(source), "utf-8")
    refreshed = library.source(paper["id"])
    assert refreshed["pages"][0]["spans"] == spans
    assert refreshed["pages"][0]["images"][0]["visible_bbox"] == [20, 140, 60, 180]
    assert refreshed["sha256"] == paper["sha256"]
    assert library.managed(paper["original_path"]).read_bytes() == original_bytes
