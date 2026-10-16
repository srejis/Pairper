import pytest
from conftest import text_blocks

from pairper.document.models import Block, Content, Region


def test_explicit_revision_preserves_previous_view_until_valid(library, pdf_path):
    paper = library.register(pdf_path)
    job = library.begin(paper["id"], paper["sha256"])["job_id"]
    blocks = text_blocks(library, paper)
    library.append(job, blocks, [], [1])
    original = library.validate(job, finalize=True)["view_id"]
    saved = (library.root / "views" / f"{original}.json").read_bytes()
    with pytest.raises(ValueError, match="already"):
        library.begin(paper["id"], paper["sha256"])
    revision = library.begin(paper["id"], paper["sha256"], revise_existing=True)
    assert library.begin(paper["id"], paper["sha256"], revise_existing=True)["job_id"] == revision["job_id"]
    assert len(library.report(revision["job_id"])["document"]["blocks"]) == len(blocks)
    assert library.paper(paper["id"])["current_view_id"] == original
    assert library.view_path(paper["id"]).is_file()
    ref = blocks[1].source_refs
    span = library.page_content(paper["id"], 1)["spans"][1]
    bad = Block(id=blocks[1].id, type="equation", order=blocks[1].order, source_refs=ref,
                content=Content(region=Region(page=1, bbox=tuple(span["bbox"]))))
    library.append(revision["job_id"], [bad], [], [])
    assert not library.validate(revision["job_id"], finalize=True)["valid"]
    assert library.paper(paper["id"])["current_view_id"] == original
    library.append(revision["job_id"], [blocks[1]], [], [])
    repaired = library.validate(revision["job_id"], finalize=True)
    assert repaired["valid"] and repaired["view_id"] != original
    assert (library.root / "views" / f"{original}.json").read_bytes() == saved
