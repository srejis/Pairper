"""QtWebEngine integration test using a synthetic fixture, never the target paper."""

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

import pymupdf
from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QFileDialog

from pairper.document.models import Block, Cell, Content, MathRun, Region, TextRun
from pairper.storage.library import Library
from pairper.ui.window import MainWindow


def main():
    root = Path("tmp/ui-verification").resolve()
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
    app = QApplication(sys.argv[:1])
    app.setFont(QFont("Malgun Gothic", 10))
    window = MainWindow(library)
    # Exercise the native chooser -> restricted bridge -> real importer without user input.
    original_chooser = QFileDialog.getOpenFileNames
    QFileDialog.getOpenFileNames = staticmethod(
        lambda *args, **kwargs: ([str(path)], "PDF (*.pdf)")
    )
    window.show()
    result = {"passed": False}

    def publish():
        worker = Library(library.root)
        if not worker.paper(paper["id"])["current_view_id"]:
            job = worker.begin(paper["id"], paper["sha256"])["job_id"]
            worker.append(job, blocks, [], [1])
            report = worker.validate(job, finalize=True)
            if not report["valid"]:
                print(json.dumps(report))
                app.exit(1)

    def check():
        js = """JSON.stringify({ready:document.documentElement.dataset.readerReady,
        fallbacks:String(document.querySelectorAll('.math-fallback').length),
        blocks:document.querySelectorAll('section.document-block').length,
        katex:document.querySelectorAll('.katex').length,
        tables:document.querySelectorAll('table').length,
        colspan:document.querySelector('th')?.colSpan,
        broken:Array.from(document.images).filter(i=>!i.complete||!i.naturalWidth).length,
        imported:document.querySelector('footer')?.textContent?.includes('1개 등록 확인'),
        notice:document.querySelector('footer')?.textContent,
        text:document.querySelector('main')?.innerText})"""
        window.reader.page().runJavaScript(js, finish)

    def finish(value):
        data = json.loads(value)
        result.update(data)
        result["passed"] = (
            data.get("ready") == "true"
            and data.get("fallbacks") == "1"
            and data.get("blocks") == 14
            and data.get("katex") == 2
            and data.get("tables") == 1
            and data.get("colspan") == 2
            and data.get("broken") == 0
            and data.get("imported") is True
        )
        window.grab().save(str(root / "window.png"))
        (root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
        print(json.dumps({k: v for k, v in result.items() if k != "text"}))
        window.reader.page().runJavaScript("document.querySelector('.reader').scrollTo(0,650)")
        QTimer.singleShot(300, finish_capture)

    def finish_capture():
        window.grab().save(str(root / "equations.png"))
        QFileDialog.getOpenFileNames = original_chooser
        # A second shell against the same SQLite library must reopen the existing view.
        app.setQuitOnLastWindowClosed(False)
        window.close()
        nonlocal restarted
        restarted = MainWindow(library)
        restarted.show()
        QTimer.singleShot(
            3000,
            lambda: restarted.reader.page().runJavaScript(
                "document.querySelectorAll('.document-block').length", finish_restart
            ),
        )

    def finish_restart(count):
        result["restart_blocks"] = count
        result["passed"] = result["passed"] and count == 14
        (root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
        print(json.dumps({"restart_blocks": count, "passed": result["passed"]}))
        restarted.close()
        app.exit(0 if result["passed"] else 1)

    restarted = None

    def click_import():
        window.reader.page().runJavaScript(
            "(() => {const b=document.querySelector('.primary');if(!b)return false;b.click();return true;})()",
            import_clicked,
        )

    def import_clicked(clicked):
        if clicked:
            QTimer.singleShot(1500, publish)
            QTimer.singleShot(7500, check)
        else:
            QTimer.singleShot(200, click_import)

    QTimer.singleShot(1000, click_import)
    QTimer.singleShot(30000, lambda: app.exit(2))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
