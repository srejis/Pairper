import pymupdf

from pairper.document.models import Block, Content, TextRun


def test_reviewed_caption_text_covers_its_own_background(library, tmp_path):
    # SliceGPT Figure 2: a blue highlight behind the ordinary word "blue".
    path = tmp_path / "caption-highlight.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.draw_rect((72, 62, 93, 70), color=None, fill=(0.6, 0.8, 1))
        page.insert_text((72, 72), "blue", fontsize=11)
        pdf.save(path)
    paper = library.register(path)
    ref = library.source(paper["id"])["pages"][0]["spans"][0]["id"]
    block = Block(id="caption", type="caption", order=0, source_refs=[ref],
                  content=Content(runs=[TextRun(refs=[ref])]))
    job = library.begin(paper["id"], paper["sha256"])["job_id"]
    library.append(job, [block], [], [1])
    assert not library.validate(job)["valid"]
    block.content.runs[0].source_verified = True
    library.append(job, [block], [], [1])
    assert library.validate(job)["valid"]
