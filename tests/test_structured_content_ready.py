"""Regressions for OFA elements that previously finalized as image-only content."""
import pytest
from conftest import text_blocks

from pairper.document.models import Block, Cell, Content, MathRun, Region, TextRun


@pytest.mark.parametrize("kind", ["equation", "table", "inline"])
@pytest.mark.parametrize("verified", [False, True])
def test_image_math_and_table_cannot_finalize(library, pdf_path, kind, verified):
    paper = library.register(pdf_path)
    blocks = text_blocks(library, paper)
    original = blocks[1]
    span = library.page_content(paper["id"], 1)["spans"][1]
    region = Region(page=1, bbox=tuple(span["bbox"]))
    if kind == "inline":
        original.content.runs = [MathRun(latex="", region=region,
                                        refs=original.source_refs, source_verified=verified)]
    else:
        blocks[1] = Block(id=original.id, type=kind, order=original.order,
                          source_refs=original.source_refs,
                          content=Content(region=region, source_verified=verified))
    job = library.begin(paper["id"], paper["sha256"])["job_id"]
    library.append(job, blocks, [], [1])
    report = library.validate(job, finalize=True)
    assert not report["valid"]
    assert any("requires source-verified" in e for e in report["errors"])
    assert report["missing_refs"] == report["duplicate_refs"] == []
    assert library.paper(paper["id"])["current_view_id"] is None


def test_reviewed_cells_finalize_and_survive_reopen(library, pdf_path):
    paper = library.register(pdf_path)
    blocks = text_blocks(library, paper)
    original = blocks[1]
    span = library.page_content(paper["id"], 1)["spans"][1]
    blocks[1] = Block(id=original.id, type="table", order=original.order,
                      source_refs=original.source_refs,
                      content=Content(region=Region(page=1, bbox=tuple(span["bbox"])),
                                      source_verified=True,
                                      cells=[Cell(row=0, column=0, colspan=3, header=True,
                                                  runs=[TextRun(refs=original.source_refs)])]))
    job = library.begin(paper["id"], paper["sha256"])["job_id"]
    library.append(job, blocks, [], [1])
    report = library.validate(job, finalize=True)
    assert report["valid"]
    from pairper.document.presentation import display_document

    cell = display_document(library, paper["id"])["blocks"][1]["content"]["cells"][0]
    assert cell["colspan"] == 3 and cell["header"]
    assert cell["runs"][0]["text"] == span["text"]
