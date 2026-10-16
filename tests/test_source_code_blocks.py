import json

from conftest import text_blocks

from pairper.document.models import Block, CodeLine, Content, MathRun, Region
from pairper.document.presentation import display_document


def test_source_code_keeps_text_indentation_and_reopens(library, pdf_path):
    paper = library.register(pdf_path)
    originals = text_blocks(library, paper)
    refs = [r for b in originals for r in b.source_refs]
    block = Block(id="algorithm", type="code", order=0, source_refs=refs,
                  content=Content(region=Region(page=1, bbox=(30, 20, 550, 120)),
                                  code_lines=[CodeLine(indent=4 if i else 0, runs=b.content.runs)
                                              for i, b in enumerate(originals)]))
    job = library.begin(paper["id"], paper["sha256"])["job_id"]
    library.append(job, [block], [], [1])
    report = library.validate(job, finalize=True)
    assert report["valid"]
    assert report["missing_refs"] == report["duplicate_refs"] == []
    content = display_document(library, paper["id"])["blocks"][0]["content"]
    assert [line["indent"] for line in content["code_lines"]] == [0, 4, 4]
    markup = library.view_path(paper["id"]).read_text("utf-8")
    assert "\n    Original sentence with 42 units." in markup
    assert '<pre class="code-content"><code>' in markup
    assert '<img' not in markup


def test_source_code_math_must_be_structured(library, pdf_path):
    paper = library.register(pdf_path)
    originals = text_blocks(library, paper)
    region = Region(page=1, bbox=(30, 20, 550, 120))
    refs = [r for b in originals for r in b.source_refs]
    block = Block(id="algorithm", type="code", order=0, source_refs=refs,
                  content=Content(region=region, code_lines=[CodeLine(runs=[
                      MathRun(latex="", region=region, refs=refs, source_verified=False)])]))
    job = library.begin(paper["id"], paper["sha256"])["job_id"]
    library.append(job, [block], [], [1])
    report = library.validate(job, finalize=True)
    assert not report["valid"]
    assert any("inline math requires" in error for error in report["errors"])
    assert json.loads(library.managed(f"staging/{job}.json").read_text("utf-8"))["blocks"][0]["type"] == "code"
