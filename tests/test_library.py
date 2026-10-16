import concurrent.futures
import json

import pymupdf
import pytest
from conftest import text_blocks
from pydantic import ValidationError

from pairper.document.models import Block, Content, Omission, Region, TextRun
from pairper.storage.library import Library


def build(library, paper):
    return library.begin(paper["id"], paper["sha256"])["job_id"]


def test_duplicate_restart_and_byte_identity(library, pdf_path):
    first = library.register(pdf_path)
    renamed = pdf_path.with_name("renamed.pdf")
    renamed.write_bytes(pdf_path.read_bytes())
    assert library.register(renamed)["id"] == first["id"]
    restarted = Library(library.root)
    assert len(restarted.list_papers()) == 1
    assert restarted.managed(first["original_path"]).read_bytes() == pdf_path.read_bytes()
    assert restarted.source(first["id"]) == library.source(first["id"])


def test_concurrent_import_and_begin(library, pdf_path):
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        papers = list(pool.map(lambda _: library.register(pdf_path), range(3)))
        assert len({p["id"] for p in papers}) == 1
        jobs = list(pool.map(lambda _: build(library, papers[0]), range(3)))
        assert len(set(jobs)) == 1


def test_complete_resume_pending_and_new_pdf(library, pdf_path):
    paper = library.register(pdf_path)
    job = build(library, paper)
    assert library.paper(paper["id"])["status"] == "processing"
    blocks = text_blocks(library, paper)
    library.append(job, blocks[:1], [], [1])
    restarted = Library(library.root)
    assert build(restarted, paper) == job
    restarted.append(job, blocks, [], [1])
    # Repeated batch is idempotent.
    restarted.append(job, blocks, [], [1])
    assert restarted.validate(job)["valid"]
    report = restarted.validate(job, finalize=True)
    assert report["valid"]
    assert restarted.validate(job, finalize=True)["view_id"] == report["view_id"]
    assert restarted.list_papers(pending=True) == []
    assert restarted.paper(paper["id"])["status"] == "complete"
    assert restarted.view_path(paper["id"]).is_file()
    with pytest.raises(ValueError, match="already"):
        build(restarted, paper)
    with pymupdf.open(pdf_path) as pdf:
        pdf[0].insert_text((40, 300), "New version")
        changed = pdf_path.with_name("changed.pdf")
        pdf.save(changed)
    new = library.register(changed)
    assert new["id"] != paper["id"]
    assert [p["id"] for p in library.list_papers(pending=True)] == [new["id"]]


def test_missing_duplicate_unknown_refs_and_repair(library, pdf_path):
    paper = library.register(pdf_path)
    job = build(library, paper)
    blocks = text_blocks(library, paper)
    library.append(job, blocks[:1], [], [1])
    report = library.validate(job, finalize=True)
    assert not report["valid"] and report["missing_refs"]
    assert library.paper(paper["id"])["current_view_id"] is None
    assert library.paper(paper["id"])["status"] == "review_required"
    blocks[1].content.runs[0].refs = [blocks[0].source_refs[0]]
    blocks[1].source_refs = blocks[0].source_refs
    library.append(job, blocks, [], [1])
    assert library.validate(job)["duplicate_refs"]
    blocks[1].content.runs[0].refs = ["not-a-source-id"]
    blocks[1].source_refs = ["not-a-source-id"]
    library.append(job, blocks, [], [1])
    assert any("unknown" in e for e in library.validate(job)["errors"])
    library.append(job, text_blocks(library, paper), [], [1])
    assert library.validate(job, finalize=True)["valid"]


def test_hash_and_path_protection(library, pdf_path):
    paper = library.register(pdf_path)
    with pytest.raises(ValueError, match="hash"):
        library.begin(paper["id"], "wrong")
    with pytest.raises(ValueError):
        library.managed("../../outside.txt")
    library.managed(paper["original_path"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="hash"):
        library.source(paper["id"])


def test_invalid_pdf_is_not_registered(library, tmp_path):
    path = tmp_path / "bad.pdf"
    path.write_text("invalid")
    with pytest.raises(pymupdf.FileDataError):
        library.register(path)
    assert library.list_papers() == []


def test_region_and_scan_fallback(library, tmp_path):
    path = tmp_path / "blank-scan-fixture.pdf"
    with pymupdf.open() as pdf:
        p = pdf.new_page(width=200, height=300)
        p.draw_rect((20, 20, 150, 200), color=(0, 0, 0))
        pdf.save(path)
    paper = library.register(path)
    assert library.manifest(paper["id"])["pages"][0]["text_unavailable"]
    job = build(library, paper)
    region = Region(page=1, bbox=(0, 0, 200, 300))
    b = Block(id="scan", order=0, type="source_region", content=Content(region=region))
    library.append(job, [b], [], [1])
    assert library.validate(job, finalize=True)["valid"]
    with pytest.raises(ValueError):
        library.image(paper["id"], 0)
    with pytest.raises(ValueError):
        library.image(paper["id"], 1, Region(page=1, bbox=(0, 0, 999, 999)))


def test_schema_rejects_model_prose_and_bad_regions():
    with pytest.raises(ValidationError):
        Block.model_validate(
            {"id": "x", "type": "paragraph", "order": 0, "content": {"text": "invented prose"}}
        )
    with pytest.raises(ValidationError):
        Region(page=1, bbox=(10, 10, 0, 0))
    with pytest.raises(ValidationError):
        Region(page=1, bbox=(0, 0, float("nan"), 10))


def test_escaping_and_normalization_audit(library, tmp_path):
    path = tmp_path / "escape-fixture.pdf"
    with pymupdf.open() as pdf:
        p = pdf.new_page()
        p.insert_text((40, 50), '<script>alert("x")</script>')
        p.insert_text((40, 80), "word-")
        p.insert_text((40, 100), "break")
        pdf.save(path)
    paper = library.register(path)
    ids = [s["id"] for s in library.page_content(paper["id"], 1)["spans"]]
    block = Block(
        id="p",
        type="paragraph",
        order=0,
        source_refs=ids,
        content=Content(runs=[TextRun(refs=ids, join="dehyphenate")]),
    )
    job = build(library, paper)
    library.append(job, [block], [], [1])
    assert library.validate(job, finalize=True)["valid"]
    html_path = library.view_path(paper["id"])
    markup = html_path.read_text("utf-8")
    assert "<script>alert" not in markup and "&lt;script&gt;" in markup
    assert "<details" not in markup
    assert "원문 참조" not in markup and "원본 대조" not in markup
    assert "wordbreak" in markup
    audit = json.loads(html_path.with_suffix(".normalization.json").read_text("utf-8"))
    assert audit[0]["original_strings"][-2:] == ["word-", "break"]


def test_declared_refs_cannot_hide_missing_body(library, pdf_path):
    paper = library.register(pdf_path)
    blocks = text_blocks(library, paper)
    blocks[0].source_refs += blocks[1].source_refs
    job = build(library, paper)
    library.append(job, [blocks[0], blocks[2]], [], [1])
    assert not library.validate(job)["valid"]


def test_job_failure_is_reported_and_does_not_affect_other_papers(library, pdf_path, tmp_path):
    paper = library.register(pdf_path)
    job = build(library, paper)
    library.append(job, text_blocks(library, paper), [], [1])
    backup = library.managed(paper["original_path"]).read_bytes()
    library.managed(paper["original_path"]).write_bytes(b"damaged")
    assert not library.validate(job)["valid"]
    assert library.paper(paper["id"])["status"] == "failed"
    assert library.report(job)["error"]
    library.managed(paper["original_path"]).write_bytes(backup)
    assert library.validate(job, finalize=True)["valid"]


def test_remove_bad_block_and_omission(library, pdf_path):
    paper = library.register(pdf_path)
    blocks = text_blocks(library, paper)
    extra = blocks[0].model_copy(update={"id": "extra", "order": 999})
    job = build(library, paper)
    omitted = Omission(
        ref=blocks[0].source_refs[0],
        category="page_number",
        reason="Incorrect omission for repair test",
    )
    library.append(job, blocks + [extra], [omitted], [1])
    assert not library.validate(job)["valid"]
    library.append(job, [], [], [], remove_block_ids=["extra"], remove_omission_refs=[omitted.ref])
    assert library.validate(job, finalize=True)["valid"]


def test_region_rotation_and_whole_page_shortcut_rejected(library, pdf_path):
    paper = library.register(pdf_path)
    region = Region(page=1, bbox=(30, 30, 300, 130))
    normal = pymupdf.Pixmap(library.image(paper["id"], 1, region, 2))
    rotated = pymupdf.Pixmap(
        library.image(paper["id"], 1, region.model_copy(update={"rotation": 90}), 2)
    )
    assert normal.width == rotated.height and normal.height == rotated.width
    refs = [s["id"] for s in library.page_content(paper["id"], 1)["spans"]]
    block = Block(
        id="bad",
        type="source_region",
        order=0,
        source_refs=refs,
        content=Content(region=Region(page=1, bbox=(0, 0, 600, 800))),
    )
    job = build(library, paper)
    library.append(job, [block], [], [1])
    assert any("full-page" in e for e in library.validate(job)["errors"])


def test_visuals_cannot_be_silently_dropped(library, tmp_path):
    path = tmp_path / "vector-fixture.pdf"
    with pymupdf.open() as pdf:
        p = pdf.new_page(width=600, height=800)
        p.insert_text((40, 50), "Original plot caption")
        p.draw_rect((40, 150, 300, 350), color=(0, 0, 0))
        pdf.save(path)
    paper = library.register(path)
    job = build(library, paper)
    library.append(job, text_blocks(library, paper), [], [1])
    assert any("visual region" in e for e in library.validate(job)["errors"])
    figure = Block(
        id="fig",
        type="figure",
        order=10,
        content=Content(region=Region(page=1, bbox=(35, 145, 305, 355))),
    )
    library.append(job, [figure], [], [])
    assert library.validate(job, finalize=True)["valid"]
