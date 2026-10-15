import pymupdf
import pytest

from pairper.document.models import Block, Content, TextRun
from pairper.storage.library import Library


@pytest.fixture
def library(tmp_path):
    return Library(tmp_path / "library")


@pytest.fixture
def pdf_path(tmp_path):
    """Small synthetic unit fixture; never a replacement for the required arXiv paper."""
    path = tmp_path / "unit-fixture.pdf"
    with pymupdf.open() as pdf:
        p = pdf.new_page(width=600, height=800)
        p.insert_text((40, 50), "Source title", fontsize=22)
        p.insert_text((40, 100), "Original sentence with 42 units.")
        p.insert_text((320, 100), "Second column text.")
        pdf.set_metadata({"title": "Unit fixture"})
        pdf.save(path)
    return path


def text_blocks(library, paper):
    spans = library.page_content(paper["id"], 1)["spans"]
    return [
        Block(
            id=f"b{i}",
            type="title" if i == 0 else "paragraph",
            order=i,
            source_refs=[s["id"]],
            content=Content(runs=[TextRun(refs=[s["id"]])]),
        )
        for i, s in enumerate(spans)
    ]
