"""Native WebChannel explanation verification against isolated source-backed fixtures."""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from verify_ui import create_fixture

from pairper.explanation.catalog import target_id
from pairper.explanation.service import ExplanationService
from pairper.translation.contract import Entry, sha
from pairper.translation.service import TranslationService
from pairper.ui.window import MainWindow


def main():
    root = Path("tmp/explanation-ui").resolve()
    root.mkdir(parents=True, exist_ok=True)
    library, paper, blocks, pdf = create_fixture(root)
    blocks[6].content.latex = "x_1+x_2+x_3+x_4+x_5+x_6+x_7+x_8+x_9+x_{10}+x_{11}+x_{12}=7"
    # Synthetic geometry stress only, not real-paper semantic evidence.
    blocks[12].content.runs[-1].latex = r"\frac{y}{\sqrt{1+\frac{1}{1+y^2}}}=2"
    job = library.begin(paper["id"], paper["sha256"])["job_id"]
    library.append(job, blocks, [], [1, 2])
    validation = library.validate(job, finalize=True)
    assert validation["valid"], validation
    original_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
    service = ExplanationService(library)
    catalog = service.prepare(paper["id"])
    unit = next(u for u in catalog["text_units"] if u["block_id"] == "b3" and "units" in u["text"])
    start = unit["text"].index("units")
    term = {
        "kind": "term",
        "block_id": unit["block_id"],
        "field": unit["field"],
        "run_index": unit["run_index"],
        "start": start,
        "end": start + 5,
        "source_text": "units",
        "source_refs": unit["source_refs"],
        "display": False,
        "aliases": ["단위"],
    }
    term["id"] = target_id(term)
    targets = [*catalog["targets"], term]
    entries = [
        {
            "target_id": term["id"],
            "kind": "term",
            "category": "측정 단위",
            "description": [
                {"kind": "text", "text": "어떤 양의 크기를 셀 때 기준으로 삼는 크기다."}
            ],
            "evidence_refs": unit["source_refs"],
            "reviewed": True,
        }
    ]
    for target in catalog["targets"]:
        entries.append(
            {
                "target_id": target["id"],
                "kind": "math",
                "symbols": [{"symbol": "x", "meaning": "계산으로 구하려는 값이다."}],
                "meaning": [
                    {"kind": "text", "text": "구하려는 값 "},
                    {"kind": "math", "latex": "x"},
                    {"kind": "text", "text": "와 오른쪽 계산 결과가 같다는 식이다."},
                ],
                "process": [
                    {"kind": "text", "text": "오른쪽의 값을 계산하여 왼쪽 값과 같게 둔다."}
                ],
                "evidence_refs": target["source_refs"],
                "reviewed": True,
            }
        )
    service.save(paper["id"], entries, targets)
    service.publish(paper["id"])
    translations = TranslationService(library)
    started = translations.begin(paper["id"], "explanation-native-fixture")
    session = started["session_id"]
    bundle = translations.current_bundle(session)
    translated = []
    for u in bundle["units"]:
        translated.append(
            Entry(
                action="translate",
                source_unit_ids=[u["id"]],
                source_fingerprint=sha([u["fingerprint"]]),
                target=u["source_text"].replace("Original", "원래").replace("units", "단위"),
            ).model_dump()
        )
    translations.save(session, started["revision"], entries=translated)
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont("Malgun Gothic", 10))
    window = MainWindow(library)
    window.show()
    result = {"fixture": str(library.root)}

    def pause(ms=120):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def js(code):
        loop = QEventLoop()
        values = []

        def done(value):
            values.append(value)
            loop.quit()

        window.reader.page().runJavaScript(code, done)
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
        if not values:
            raise AssertionError("JavaScript timed out")
        return values[0]

    def until(code, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if js(code):
                return
            pause()
        raise AssertionError(code)

    def click(selector):
        assert js(
            f"(()=>{{let e=document.querySelector({json.dumps(selector)});if(!e)return false;e.focus();e.click();return true;}})()"
        ), selector

    def screenshot(name):
        pause(250)
        window.grab().save(str(root / name))

    def geometry():
        return json.loads(
            js(
                "JSON.stringify((()=>{let r=document.querySelector('.reader'),d=document.querySelector('.paper-document'),c=d.cloneNode(true);c.querySelectorAll('.equation-explanation-shortcut').forEach(e=>e.remove());return {sourceText:c.textContent,scroll:r.scrollTop,width:d.getBoundingClientRect().width,inlineHeight:document.querySelector('[data-block-id=b12]').getBoundingClientRect().height,equationHeights:Array.from(document.querySelectorAll('.equation-with-explanation')).map(e=>e.getBoundingClientRect().height)}})())"
            )
        )

    try:
        until("!!document.querySelector('.paper-status.ready')")
        click(".paper-status.ready")
        until(
            "document.documentElement.dataset.readerReady==='true'&&document.querySelectorAll('.explanation-trigger').length===6"
        )
        assert not js("document.body.textContent.includes('점선 용어나 수식을')")
        assert not js("!!document.querySelector('button button')")
        source_text = js("document.querySelector('.paper-document').textContent")
        for width in (1280, 1600, 480):
            window.resize(width, 850)
            pause(250)
            js("document.querySelector('.reader').scrollTop=120")
            pause()
            before = geometry()
            click(".explanation-visibility")
            assert geometry() == before, (before, geometry())
            assert not js("!!document.querySelector('.explanation-trigger')")
            click(".explanation-visibility")
            assert geometry() == before, (before, geometry())
            click(".term-trigger")
            until("!!document.querySelector('.explanation-panel.is-open')")
            assert geometry() == before, (width, before, geometry())
            assert js(
                "document.querySelector('.explanation-panel').textContent.includes('측정 단위')"
            )
            assert js(
                "document.querySelector('.term-trigger').getAttribute('aria-expanded')==='true'"
            )
            assert source_text == js("document.querySelector('.paper-document').textContent")
            assert js("document.querySelector('.reader-toolbar').scrollWidth<=window.innerWidth")
            screenshot(f"term-{width}.png")
            click('[aria-label="해설 닫기"]')
            assert js("document.activeElement.classList.contains('term-trigger')")
            click(".display-explanation-trigger")
            until("document.querySelector('.explanation-panel').textContent.includes('계산 과정')")
            screenshot(f"formula-{width}.png")
            click('[aria-label="해설 닫기"]')
            click('[data-block-id="b6"] .equation-explanation-shortcut')
            until("!!document.querySelector('.explanation-panel.is-open')")
            assert js(
                "(()=>{let b=document.querySelector('[data-block-id=b6] .equation-explanation-shortcut'),r=document.querySelector('.reader').getBoundingClientRect();return b.getBoundingClientRect().right<=r.right})()"
            )
            screenshot(f"long-formula-{width}.png")
            click('[aria-label="해설 닫기"]')
            js("document.querySelector('.inline-math-trigger').scrollIntoView({block:'center'})")
            pause()
            fraction_before = geometry()
            click(".explanation-visibility")
            assert geometry() == fraction_before, (
                "synthetic tall fraction hide",
                width,
                fraction_before,
                geometry(),
            )
            click(".explanation-visibility")
            assert geometry() == fraction_before, (
                "synthetic tall fraction restore",
                width,
                fraction_before,
                geometry(),
            )
            click(".inline-math-trigger")
            until("!!document.querySelector('.explanation-panel.is-open')")
            assert geometry() == fraction_before, (
                "synthetic tall fraction select",
                width,
                fraction_before,
                geometry(),
            )
            screenshot(f"synthetic-tall-fraction-{width}.png")
            click('[aria-label="해설 닫기"]')
            assert geometry() == fraction_before, (
                "synthetic tall fraction close",
                width,
                fraction_before,
                geometry(),
            )
            result[str(width)] = {"stable_geometry": True, "paper_width": before["width"]}
        window.resize(1280, 850)
        window.activateWindow()
        window.reader.setFocus()
        pause()
        js(
            "document.addEventListener('keydown',e=>{window.__auditKey={key:e.key,trusted:e.isTrusted,target:e.target.getAttribute('data-explanation-id')}},true);document.querySelector('.inline-math-trigger').focus({preventScroll:true})"
        )
        QTest.keyClick(window.reader.focusProxy() or window.reader, Qt.Key.Key_Return)
        until("!!document.querySelector('.explanation-panel.is-open')")
        click('[aria-label="해설 닫기"]')
        assert js("document.activeElement.classList.contains('inline-math-trigger')")
        QTest.keyClick(window.reader.focusProxy() or window.reader, Qt.Key.Key_Space)
        until("!!document.querySelector('.explanation-panel.is-open')")
        result["trusted_keyboard_enter_space"] = True
        result["trusted_key_event"] = js("JSON.stringify(window.__auditKey)")
        result["synthetic_tall_fraction_geometry"] = True
        screenshot("inline-1280.png")
        click('[aria-label="해설 닫기"]')
        click(".reader-language button:last-child")
        until("!!document.querySelector('[aria-label=\"한국어 논문\"] .term-trigger')")
        assert js("document.querySelector('.term-trigger').textContent==='단위'")
        click(".term-trigger")
        until("!!document.querySelector('.explanation-panel.is-open')")
        screenshot("korean-term-1280.png")
        before_refresh = geometry()
        revised = {
            **entries[0],
            "description": [
                {
                    "kind": "text",
                    "text": "실행 중 새로 게시된 해설도 읽던 위치를 유지하며 반영된다.",
                }
            ],
        }
        service.save(paper["id"], [revised])
        service.publish(paper["id"])
        window.bridge.poll()
        until(
            "document.querySelector('.explanation-panel').textContent.includes('실행 중 새로 게시된 해설')"
        )
        assert geometry() == before_refresh
        result["live_publish_refresh"] = True
        click('[aria-label="해설 닫기"]')
        click('[aria-label="목차"]')
        until("!!document.querySelector('dialog[open] .toc')")
        screenshot("toc-preserved-1280.png")
        assert hashlib.sha256(pdf.read_bytes()).hexdigest() == original_hash
        result.update(
            passed=True,
            source_pdf_unchanged=True,
            source_and_korean=True,
            inline_and_display=True,
            forbidden_tutorial_absent=True,
        )
    except Exception as exc:
        import traceback

        result.update(passed=False, error=str(exc), traceback=traceback.format_exc())
        screenshot("failure.png")
    finally:
        window.close()
        (root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
