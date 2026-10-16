import copy

import pymupdf

from pairper.document.models import Block, Content, Region, TextRun
from pairper.pdf.source import refresh_visuals
from pairper.validation.checks import contains


def test_rotated_font_box_does_not_force_caption_into_figure(library, tmp_path):
    # OFA Figure 2: a reversed arrow's font bbox extends down into its caption,
    # although its painted glyph ends above it. A rotated Latin glyph reproduces
    # the font-box versus painted-box distinction without redistributing the PDF.
    path = tmp_path / "rotated-glyph.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=600, height=800)
        page.insert_text((400, 180), "A", fontsize=53, rotate=180)
        page.insert_text((320, 260), "Figure caption stays readable")
        pdf.save(path)
    paper = library.register(path)
    source = library.source(paper["id"])
    spans = copy.deepcopy(source["pages"][0]["spans"])
    glyph, caption = spans
    painted = source["pages"][0]["rotated_glyph_bounds"][glyph["id"]]
    assert not contains(painted, glyph["bbox"])
    blocks = [
        Block(id="figure", type="figure", order=0, source_refs=[glyph["id"]],
              content=Content(region=Region(page=1, bbox=tuple(painted)))),
        Block(id="caption", type="caption", order=1, source_refs=[caption["id"]],
              content=Content(runs=[TextRun(refs=[caption["id"]])])),
    ]
    job = library.begin(paper["id"], paper["sha256"])["job_id"]
    library.append(job, blocks, [], [1])
    report = library.validate(job)
    assert report["valid"]
    assert report["missing_refs"] == report["duplicate_refs"] == []
    refresh_visuals(library.managed(paper["original_path"]), source)
    assert source["pages"][0]["spans"] == spans
