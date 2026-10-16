import copy

import pymupdf

from pairper.document.models import Block, Content, Region, TextRun


def test_graph_font_and_white_padding_do_not_force_caption_into_image(library, tmp_path):
    # Deja Vu Figure 7: blank white plot border and label font descender boxes
    # extend lower than the graph's actual paint. Cropping must keep label ink.
    path = tmp_path / "plot-caption.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.draw_rect((60, 30, 220, 90), color=None, fill=(1, 1, 1))
        page.insert_text((72, 72), "Token Count", fontsize=12)
        page.insert_text((72, 96), "Figure 7. Caption is native text", fontsize=10)
        pdf.save(path)
    paper = library.register(path)
    source = library.source(paper["id"])
    original = copy.deepcopy(source["pages"][0]["spans"])
    label, caption = original
    paint = source["pages"][0]["text_paint_bounds"][label["id"]]
    assert paint[3] < label["bbox"][3]
    blocks = [
        Block(id="graph", type="figure", order=0, source_refs=[label["id"]],
              content=Content(region=Region(page=1, bbox=(60, 30, 220, paint[3])))),
        Block(id="caption", type="caption", order=1, source_refs=[caption["id"]],
              content=Content(runs=[TextRun(refs=[caption["id"]])])),
    ]
    job = library.begin(paper["id"], paper["sha256"])["job_id"]
    library.append(job, blocks, [], [1])
    assert library.validate(job)["valid"]
    assert library.source(paper["id"])["pages"][0]["spans"] == original
    assert source["pages"][0]["drawings"][0]["background_only"]

