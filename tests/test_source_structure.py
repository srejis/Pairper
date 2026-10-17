import pytest
from conftest import text_blocks
from pydantic import ValidationError

from pairper.document.models import (
    Block,
    Content,
    MathRun,
    Region,
    Relation,
    SpanRange,
    TextRun,
)
from pairper.document.presentation import display_document
from pairper.document.provenance import expand_range
from pairper.document.structure import structure_errors
from pairper.document.text import resolve_text


def group(block_id, order, role, parent=None, relations=None):
    return Block(id=block_id, type="group", order=order, parent_id=parent,
                 relations=relations or [], content=Content(group_role=role))


def test_groups_refs_ranges_and_relations_survive_save_and_reopen(library, pdf_path):
    paper = library.register(pdf_path)
    title, statement, proof = text_blocks(library, paper)
    statement.parent_id, statement.order = "theorem", 2
    ref = statement.source_refs[0]
    statement.content.runs = [TextRun(ref_ranges=[SpanRange(first_ref=ref, last_ref=ref)])]
    statement.source_refs = []
    proof.parent_id, proof.order = "proof", 4
    blocks = [title, group("theorem", 1, "theorem"), statement,
              group("proof", 3, "proof", relations=[Relation(kind="proof_of", target_id="theorem")]),
              proof]
    job = library.begin(paper["id"], paper["sha256"])["job_id"]
    library.append(job, blocks, [], [1])
    report = library.validate(job, finalize=True)
    assert report["valid"], report
    stored = library.report(job)["document"]
    assert stored["schema_version"] == "1.1"
    assert stored["blocks"][2]["content"]["runs"][0]["refs"] == [ref]
    assert stored["blocks"][2]["content"]["runs"][0]["ref_ranges"] == []
    assert stored["blocks"][2]["source_refs"] == [ref]
    data = display_document(library, paper["id"])
    assert data["blocks"][2]["parent_id"] == "theorem"
    assert data["blocks"][3]["relations"][0]["target_id"] == "theorem"
    markup = library.view_path(paper["id"]).read_text("utf-8")
    assert markup.count('id="b1"') == markup.count('id="b2"') == 1
    assert markup.index('id="theorem"') < markup.index('id="b1"') < markup.index('id="proof"')
    assert 'data-group-role="proof"' in markup


def test_invalid_hierarchies_and_dangling_relations_fail():
    leaf = Block(id="leaf", type="paragraph", order=1, parent_id="missing",
                 content=Content(runs=[TextRun(refs=["a"])]))
    assert structure_errors([leaf])
    assert structure_errors([group("empty", 0, "proof")])
    parent = group("parent", 0, "example")
    leaf.parent_id = "parent"
    unrelated = leaf.model_copy(update={"id": "root", "parent_id": None, "order": 2})
    late = leaf.model_copy(update={"id": "late", "order": 3})
    assert any("contiguous" in e for e in structure_errors([parent, leaf, unrelated, late]))
    parent.parent_id = "parent"
    assert structure_errors([parent, leaf])
    parent.parent_id = None
    leaf.relations = [Relation(kind="proof_of", target_id="parent")]
    assert any("proof_of" in e for e in structure_errors([parent, leaf]))


def test_ranges_cannot_guess_across_blocks(library, pdf_path):
    paper = library.register(pdf_path)
    source = library.source(paper["id"])
    spans = source["pages"][0]["spans"]
    with pytest.raises(ValueError, match="one block"):
        expand_range(SpanRange(first_ref=spans[0]["id"], last_ref=spans[-1]["id"]), source)
    with pytest.raises(ValidationError):
        TextRun(refs=["a"], ref_ranges=[SpanRange(first_ref="a", last_ref="b")])


def test_literal_prompt_keeps_printed_symbols_and_accents():
    spans = {"a": {"text": "Garc´ıa {user_task} **literal**"}}
    assert resolve_text(TextRun(refs=["a"], literal=True), spans, []) == spans["a"]["text"]
    assert resolve_text(TextRun(refs=["a"]), spans, []).startswith("García")


def test_reviewed_panel_regions_do_not_hide_unrepresented_text(library, pdf_path):
    paper = library.register(pdf_path)
    blocks = text_blocks(library, paper)
    panel = group("panel", 1, "prompt")
    panel.content.source_verified = True
    panel.content.source_regions = [Region(page=1, bbox=(30, 80, 550, 120))]
    # The panel encloses both columns but only one is a child. Global coverage alone
    # would pass; the group must not silently claim the other column's text.
    blocks[1].order, blocks[1].parent_id = 2, "panel"
    blocks[2].order = 3
    job = library.begin(paper["id"], paper["sha256"])["job_id"]
    library.append(job, [blocks[0], panel, *blocks[1:]], [], [1])
    report = library.validate(job)
    assert not report["valid"]
    assert any("absent from the group" in error for error in report["errors"])


def test_invalid_verified_latex_cannot_publish(library, pdf_path):
    paper = library.register(pdf_path)
    blocks = text_blocks(library, paper)
    span = library.source(paper["id"])["pages"][0]["spans"][1]
    blocks[1].content.runs = [MathRun(latex=r"\notARealMathCommand{x}",
        refs=[span["id"]], region=Region(page=1, bbox=tuple(span["bbox"])), source_verified=True)]
    job = library.begin(paper["id"], paper["sha256"])["job_id"]
    library.append(job, blocks, [], [1])
    report = library.validate(job, finalize=True)
    assert not report["valid"]
    assert report["math_render"]["checked"] == 1
    assert report["math_render"]["errors"]
    assert library.paper(paper["id"])["current_view_id"] is None
