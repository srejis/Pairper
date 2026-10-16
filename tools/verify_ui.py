"""QtWebEngine integration test using a synthetic fixture, never the target paper."""

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

import pymupdf
from PySide6.QtCore import QEventLoop, QMimeData, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QFileDialog

from pairper.document.models import Block, Cell, Content, MathRun, Region, TextRun
from pairper.storage.library import Library
from pairper.ui.window import MainWindow


def create_fixture(root):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "synthetic-renderer-fixture.pdf"
    types = [
        "title",
        "authors",
        "heading",
        "paragraph",
        "list",
        "equation",
        "equation",
        "table",
        "caption",
        "footnote",
        "reference",
        "source_region",
        "paragraph",
    ]
    labels = [
        "Pairper renderer verification",
        "Author and affiliation",
        "1 Introduction",
        "Original paragraph with 42 units.",
        "First list item",
        "x = 42",
        "bad = 7",
        "42 units",
        "Figure 1: original diagram",
        "Footnote preserved",
        "[1] Reference",
        "Uncertain source preserved",
        "Inline math:",
    ]
    with pymupdf.open() as pdf:
        p = pdf.new_page(width=600, height=1100)
        for i, text in enumerate(labels):
            p.insert_text((40, 50 + i * 65), text, fontsize=12)
        p.insert_text((220, 830), "y = 2")
        p.draw_rect((40, 930, 300, 1030), color=(0.1, 0.4, 0.4), fill=(0.8, 0.9, 0.9))
        p.insert_text((420, 100), "Second column source.")
        second = pdf.new_page(width=600, height=800)
        second.insert_text((40, 70), "Second page of the same paragraph.")
        pdf.save(path)
    library = Library(Path(tempfile.mkdtemp(prefix="library-", dir=root)))
    paper = library.register(path)
    spans = library.page_content(paper["id"], 1)["spans"]
    blocks = []
    for i, kind in enumerate(types):
        ref = spans[i]["id"]
        run = TextRun(refs=[ref])
        region = Region(page=1, bbox=(35, 30 + i * 65, 400, 60 + i * 65))
        if kind == "list":
            content = Content(items=[[run]])
        elif kind == "equation":
            content = Content(
                region=region,
                source_verified=True,
                latex=r"x = \frac{42}{1}" if i == 5 else r"\definitelyInvalid{7}",
            )
        elif kind == "table":
            content = Content(
                region=region,
                source_verified=True,
                cells=[Cell(row=0, column=0, colspan=2, header=True, runs=[run])],
            )
        elif kind == "source_region":
            content = Content(region=region)
        else:
            content = Content(runs=[run])
        if i == 12:
            content.runs.append(
                MathRun(
                    latex="y=2",
                    refs=[spans[13]["id"]],
                    region=Region(page=1, bbox=(215, 810, 300, 840)),
                    source_verified=True,
                )
            )
        refs = [ref, spans[13]["id"]] if i == 12 else [ref]
        blocks.append(Block(id=f"b{i}", type=kind, order=i, source_refs=refs, content=content))
    blocks.append(
        Block(
            id="fig",
            type="figure",
            order=13,
            content=Content(region=Region(page=1, bbox=(35, 925, 305, 1035)), caption_id="b8"),
        )
    )
    multi_refs = [spans[14]["id"], library.page_content(paper["id"], 2)["spans"][0]["id"]]
    blocks.append(
        Block(
            id="multi",
            type="paragraph",
            order=14,
            source_refs=multi_refs,
            content=Content(runs=[TextRun(refs=multi_refs)]),
        )
    )
    return library, paper, blocks, path


def main():
    """Drive the real WebChannel and renderer against isolated synthetic PDF data."""
    import time

    root = Path("tmp/ui-verification").resolve()
    library, paper, blocks, path = create_fixture(root)
    pending = root / "unprocessed-example.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((40, 50), "Synthetic pending paper for library verification.")
        pdf.save(pending)
    app = QApplication(sys.argv[:1])
    saved_clipboard = QMimeData()
    current_clipboard = app.clipboard().mimeData()
    if current_clipboard:
        for mime_format in current_clipboard.formats():
            saved_clipboard.setData(mime_format, current_clipboard.data(mime_format))
    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont("Malgun Gothic", 10))
    window = MainWindow(library)
    window.show()
    result = {}
    original_chooser = QFileDialog.getOpenFileNames
    QFileDialog.getOpenFileNames = staticmethod(
        lambda *args, **kwargs: ([str(pending)], "PDF (*.pdf)")
    )

    def pause(ms=150):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def js(expression):
        loop = QEventLoop()
        values = []

        def done(value):
            values.append(value)
            loop.quit()

        window.reader.page().runJavaScript(expression, done)
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(5000)
        loop.exec()
        timer.stop()
        if not values:
            raise AssertionError("JavaScript timeout")
        return values[0]

    def until(expression, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if js(expression):
                return
            pause()
        raise AssertionError(f"UI condition failed: {expression}")

    def click(selector):
        assert js(
            f"(()=>{{const b=document.querySelector({json.dumps(selector)});if(!b)return false;b.focus();b.click();return true;}})()"
        ), selector

    def snapshot(name):
        pause(300)
        window.grab().save(str(root / name))

    def geometry():
        return json.loads(
            js(
                """JSON.stringify((()=>{const r=document.querySelector('.reader'),d=document.querySelector('.paper-document'),b=document.querySelector('[data-block-id=b3]');return {scroll:r.scrollTop,width:d.getBoundingClientRect().width,textHeight:b.getBoundingClientRect().height};})())"""
            )
        )

    try:
        until("document.querySelectorAll('.paper-row').length===1")
        assert not js("!!document.querySelector('.reader')")
        assert not js("!!document.querySelector('footer')")
        click(".primary")
        until("document.querySelectorAll('.paper-row').length===2")
        assert library.list_papers()[0]["filename"] == pending.name
        result["native_pdf_import"] = True
        click(".paper-row")
        until("!!document.querySelector('.paper-expansion')")
        click(".request-copy button")
        until(
            "!!document.querySelector('.toast') && document.querySelector('.toast').textContent.includes('복사했습니다')"
        )
        assert "Pairper 논문 업데이트해 줘" in app.clipboard().text()
        result["native_copy_request"] = True
        snapshot("library-pending-1280.png")
        job = library.begin(paper["id"], paper["sha256"])["job_id"]
        library.append(job, blocks, [], [1, 2])
        assert library.validate(job, finalize=True)["valid"]
        until("!!document.querySelector('.paper-status.ready')")
        snapshot("library-1280.png")
        click(".paper-status.ready")
        until("document.documentElement.dataset.readerReady==='true'")
        until("Array.from(document.images).every(i=>i.complete&&i.naturalWidth>0)")
        result.update(
            json.loads(
                js(
                    """JSON.stringify({blocks:document.querySelectorAll('.document-block').length,katex:document.querySelectorAll('.katex').length,fallbacks:document.querySelectorAll('.math-fallback').length,colspan:document.querySelector('th').colSpan,broken:Array.from(document.images).filter(i=>!i.complete||!i.naturalWidth).length,fonts:document.fonts.check('14px Inter')&&document.fonts.check('14px Pretendard')})"""
                )
            )
        )
        assert result["blocks"] == 15 and result["katex"] == 2
        assert result["fallbacks"] == 1 and result["colspan"] == 2 and result["broken"] == 0
        assert not js("!!document.querySelector('.app-header')")
        snapshot("reader-1280.png")
        js("document.querySelector('.reader').scrollTo(0,200)")
        pause()
        before = geometry()
        click('[aria-label="목차"]')
        until("!!document.querySelector('dialog[open] .toc')")
        during = geometry()
        assert before == during, (before, during)
        snapshot("toc-1280.png")
        click('[aria-label="목차 닫기"]')
        until("!document.querySelector('dialog')")
        assert geometry() == before
        assert js("document.activeElement.getAttribute('aria-label')==='목차'")
        result["panel_geometry_and_focus"] = True
        click('[aria-label="목차"]')
        click(".toc button")
        assert js("document.querySelector('.toc [aria-current=location]')!==null")
        assert js(
            "Math.abs(document.querySelector('[data-block-id=b2]').getBoundingClientRect().top-document.querySelector('.reader').getBoundingClientRect().top)<40"
        )
        js(
            "document.querySelector('dialog').dispatchEvent(new MouseEvent('click',{clientX:0,clientY:0,bubbles:true}))"
        )
        until("!document.querySelector('dialog')")
        click('[data-block-id="b3"] .inspect-button')
        until("!!document.querySelector('.comparison-viewport img')")
        until("document.querySelector('.comparison-viewport img').naturalWidth>0")
        assert js("!!document.querySelector('[data-block-id=b3].source-selected')")
        snapshot("source-1280.png")
        click('[aria-label="원본 확대"]')
        assert js("document.querySelector('[aria-label=\"원본 배율\"]').textContent==='125%'")
        js("document.querySelector('dialog').dispatchEvent(new Event('cancel',{cancelable:true}))")
        until("!document.querySelector('dialog')")
        assert not js("!!document.querySelector('.source-selected')")
        assert js("document.activeElement.classList.contains('inspect-button')")
        result["source_render_and_escape"] = True
        click('[data-block-id="multi"] .inspect-button')
        until("!!document.querySelector('.comparison-viewport img')")
        click('[aria-label="다음 원본 영역"]')
        until(
            "document.querySelector('.region-caption')?.textContent.includes('2페이지') && !!document.querySelector('.comparison-viewport img')"
        )
        until("document.querySelector('.comparison-viewport img').naturalWidth>0")
        snapshot("source-multipage-1280.png")
        result["multiple_source_pages"] = True
        click('[aria-label="원본 대조 닫기"]')
        until("!document.querySelector('dialog')")
        click('[aria-label="글자 설정"]')
        click('[aria-label="글자 크기 증가"]')
        assert js("document.querySelector('[aria-label=\"본문 글자 크기\"]').textContent==='18px'")
        click(".font-settings .text-button")
        click('[aria-label="글자 설정 닫기"]')
        until("!document.querySelector('dialog')")
        window.resize(480, 760)
        js("document.querySelector('.reader').scrollTo(0,0)")
        pause(400)
        snapshot("reader-480.png")
        assert js("document.querySelector('.reader-toolbar').scrollWidth<=window.innerWidth")
        assert js("document.querySelector('.paper-document').getBoundingClientRect().width<480")
        click('[aria-label="목차"]')
        snapshot("toc-480.png")
        click('[aria-label="목차 닫기"]')
        until("!document.querySelector('dialog')")
        click(".back-button")
        snapshot("library-480.png")
        click('[aria-label="설정"]')
        until("!!document.querySelector('[role=switch]:not(:disabled)')")
        snapshot("settings-480.png")
        click('[aria-label="설정 닫기"]')
        until("!document.querySelector('dialog')")
        window.resize(1280, 850)
        click(".paper-status.ready")
        until("document.documentElement.dataset.readerReady==='true'")
        assert js("document.querySelectorAll('.document-block').length===15")
        result["reopen_blocks"] = 15
        result["blocked_requests"] = window.interceptor.blocked_count
        result["passed"] = True
    except Exception as exc:
        result.update(passed=False, error=str(exc))
        snapshot("failure.png")
    finally:
        app.clipboard().setMimeData(saved_clipboard)
        QFileDialog.getOpenFileNames = original_chooser
        window.close()
        (root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
