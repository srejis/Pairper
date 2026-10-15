import json

import pytest
from conftest import text_blocks

from pairper.document.models import TextRun
from pairper.document.presentation import display_document
from pairper.document.text import resolved_runs


def test_run_boundaries_preserve_words_spaces_punctuation_and_hyphens():
    spans = {
        "a": {"text": "Original", "bbox": [10, 10, 50, 20], "size": 12},
        "b": {"text": "words", "bbox": [55, 10, 80, 20], "size": 12},
        "c": {"text": ".", "bbox": [80, 10, 85, 20], "size": 12},
        "d": {"text": "pre-", "bbox": [10, 30, 40, 40], "size": 12},
        "e": {"text": "served", "bbox": [10, 50, 40, 60], "size": 12},
    }

    def rendered(runs):
        return "".join(prefix + text for _, prefix, text in resolved_runs(runs, spans, []))

    assert rendered([TextRun(refs=[i]) for i in ["a", "b", "c"]]) == "Original words."
    assert rendered([TextRun(refs=["d"]), TextRun(refs=["e"])]) == "pre-served"
    assert rendered([TextRun(refs=["d", "e"], join="dehyphenate")]) == "preserved"
    spans["a"]["text"] = "Original "
    assert rendered([TextRun(refs=["a"]), TextRun(refs=["b"])]) == "Original words"
    assert rendered([TextRun(refs=["b"]), TextRun(refs=["c"], before="space")]) == "words ."
    spans["f"] = {"text": "half", "bbox": [10, 80, 35, 90], "size": 12}
    spans["g"] = {"text": "word", "bbox": [35, 80, 60, 90], "size": 12}
    assert rendered([TextRun(refs=["f"]), TextRun(refs=["g"])]) == "halfword"
    assert rendered([TextRun(refs=["a", "b"], join="newline")]) == "Original \nwords"


def test_display_contract_matches_saved_source(library, pdf_path):
    paper = library.register(pdf_path)
    with pytest.raises(ValueError):
        display_document(library, paper["id"])
    job = library.begin(paper["id"], paper["sha256"])["job_id"]
    library.append(job, text_blocks(library, paper), [], [1])
    assert library.validate(job, finalize=True)["valid"]
    document = display_document(library, paper["id"])
    assert document["display_schema_version"] == "1.0"
    assert len(document["blocks"]) == 3
    assert document["blocks"][0]["content"]["runs"][0]["text"] == "Source title"
    assert document["blocks"][0]["source_trace"][0]["page"] == 1
    assert "CONTROL_PLANE_API_KEY" not in json.dumps(document)


def test_atomic_json_retries_windows_read_lock(tmp_path, monkeypatch):
    from pairper.storage import library as module

    original = module.os.replace
    attempts = []

    def replace(src, dst):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("transient sharing lock")
        original(src, dst)

    monkeypatch.setattr(module.os, "replace", replace)
    module.write_json(tmp_path / "state.json", {"state": "ready"})
    assert len(attempts) == 3
    assert json.loads((tmp_path / "state.json").read_text("utf-8"))["state"] == "ready"


def test_python_html_and_display_model_use_identical_run_text(library, pdf_path):
    from html.parser import HTMLParser

    from pairper.document.models import Block, Content

    paper = library.register(pdf_path)
    spans = library.page_content(paper["id"], 1)["spans"]
    refs = [s["id"] for s in spans]
    block = Block(
        id="paragraph",
        type="paragraph",
        order=0,
        source_refs=refs,
        content=Content(runs=[TextRun(refs=[ref]) for ref in refs]),
    )
    job = library.begin(paper["id"], paper["sha256"])["job_id"]
    library.append(job, [block], [], [1])
    assert library.validate(job, finalize=True)["valid"]
    data = display_document(library, paper["id"])
    expected = "".join(r["prefix"] + r["text"] for r in data["blocks"][0]["content"]["runs"])

    class ParagraphText(HTMLParser):
        def __init__(self):
            super().__init__()
            self.active = False
            self.result = ""
            self.finished = False

        def handle_starttag(self, tag, attrs):
            if tag == "p" and not self.finished:
                self.active = True

        def handle_endtag(self, tag):
            if tag == "p" and self.active:
                self.active = False
                self.finished = True

        def handle_data(self, text):
            if self.active:
                self.result += text

    parser = ParagraphText()
    parser.feed(library.view_path(paper["id"]).read_text("utf-8"))
    assert parser.result == expected


def test_unverified_table_does_not_resolve_unused_reconstruction(library, pdf_path):
    from pairper.document.models import Block, Cell, Content, Region

    paper = library.register(pdf_path)
    refs = [s["id"] for s in library.page_content(paper["id"], 1)["spans"]]
    table = Block(
        id="table",
        type="table",
        order=0,
        source_refs=refs,
        content=Content(
            region=Region(page=1, bbox=(30, 20, 550, 120)),
            source_verified=False,
            cells=[Cell(row=0, column=0, runs=[TextRun(refs=["unverified-model-ref"])])],
        ),
    )
    job = library.begin(paper["id"], paper["sha256"])["job_id"]
    library.append(job, [table], [], [1])
    assert library.validate(job, finalize=True)["valid"]
    content = display_document(library, paper["id"])["blocks"][0]["content"]
    assert content["cells"] == [] and content["image_url"].startswith("file:///")
