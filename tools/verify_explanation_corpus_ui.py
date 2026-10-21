"""Read-only native corpus rendering audit of source/Korean papers and saved explanations."""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

from explanation_anchor_audit import aggregate_anchor_coverage, summarize_anchor_coverage

os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from pairper.document.presentation import display_document
from pairper.explanation.service import ExplanationService
from pairper.storage.library import Library
from pairper.translation.service import TranslationService
from pairper.ui.window import MainWindow


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, default=Path("runtime-library"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-published", action="store_true")
    parser.add_argument("--paper-id", action="append", default=[])
    parser.add_argument("--korean-click-plan", type=Path)
    args = parser.parse_args()
    plan_bytes = args.korean_click_plan.read_bytes() if args.korean_click_plan else None
    plan = json.loads(plan_bytes) if plan_bytes else None
    if plan:
        assert set(plan) == {
            "paper_id",
            "revision",
            "fingerprint",
            "target_ids",
            "repeat_term_pairs",
        }
        assert len(set(plan["target_ids"])) == len(plan["target_ids"])
        assert 0 <= plan["repeat_term_pairs"] <= 3
        assert args.paper_id == [plan["paper_id"]], "Click plans require an exact single paper"
    root = args.library.resolve()
    output = args.output.resolve()
    if output == root or output.is_relative_to(root):
        raise ValueError("Verification output must be outside the library")
    if (output / "result.json").exists():
        raise ValueError("Choose a new output directory to preserve previous evidence")
    original_connect = sqlite3.connect

    def readonly_connect(database, *positional, **kwargs):
        path = Path(database).resolve() if isinstance(database, (str, Path)) else None
        if path is not None and path.is_relative_to(root):
            if not path.is_file():
                raise ValueError(f"Read-only audit requires an existing database: {path.name}")
            kwargs["uri"] = True
            return original_connect(path.as_uri() + "?mode=ro", *positional, **kwargs)
        return original_connect(database, *positional, **kwargs)

    # Includes stores created by actual WebChannel worker threads. SQLite itself
    # rejects writes; verification never saves, publishes, repairs or finalizes.
    sqlite3.connect = readonly_connect
    output.mkdir(parents=True, exist_ok=True)
    files = [
        p
        for folder in ("originals", "sources", "views", "translations/assets")
        for p in (root / folder).rglob("*")
        if p.is_file()
    ]
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}

    class AuditLibrary(Library):
        def __init__(self, directory):
            self.root = Path(directory).resolve()
            self.db = self.root / "library.db"
            if not self.db.is_file():
                raise ValueError("Read-only audit requires an existing library")

        def asset(self, paper_id, region):
            key = uuid.uuid5(uuid.NAMESPACE_URL, paper_id + region.model_dump_json()).hex
            if not (self.root / "assets" / f"{key}.png").is_file():
                raise ValueError(
                    "Finalized paper is missing a cached source image; read-only audit will not create it"
                )
            return key

        def source(self, paper_id):
            from pairper.pdf.source import EXTRACTOR_VERSION

            paper = self.paper(paper_id)
            data = json.loads((self.root / "sources" / f"{paper_id}.json").read_text("utf-8"))
            if data.get("extractor_version", 1) < EXTRACTOR_VERSION:
                raise ValueError("Source requires refresh; read-only audit will not modify it")
            return super().source(paper["id"])

    library = AuditLibrary(root)
    translations = TranslationService(library)
    explanations = ExplanationService(library)
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont("Malgun Gothic", 10))
    window = MainWindow(library)
    original_dispatch = window.bridge.dispatch

    def diagnostic_dispatch(request_id, function):
        def checked():
            try:
                return function()
            except Exception:
                import traceback

                traceback.print_exc()
                raise

        return original_dispatch(request_id, checked)

    window.bridge.dispatch = diagnostic_dispatch
    window.show()
    rows = []

    def pause(ms=150):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def js(code):
        loop = QEventLoop()
        values = []

        def done(v):
            values.append(v)
            loop.quit()

        window.reader.page().runJavaScript(code, done)
        QTimer.singleShot(10000, loop.quit)
        loop.exec()
        if not values:
            raise AssertionError("JS timeout")
        return values[0]

    def until(code, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if js(code):
                return
            pause()
        raise AssertionError(code)

    def click(selector):
        assert js(
            f"(()=>{{let e=document.querySelector({json.dumps(selector)});if(!e)return false;e.focus();e.click();return true}})()"
        ), selector

    def shot(name):
        pause(200)
        window.grab().save(str(output / name))

    def audit(document):
        until("document.fonts.status==='loaded'&&Array.from(document.images).every(i=>i.complete)")
        actual = js("document.querySelector('.paper-document').textContent")
        expected = []
        expected_math = 0
        for b in document["blocks"]:
            c = b["content"]
            fields = [
                c.get("runs", []),
                *c.get("items", []),
                *[cell["runs"] for cell in c.get("cells", [])],
                *[line["runs"] for line in c.get("code_lines", [])],
            ]
            if b["type"] == "equation" and (c.get("latex") or c.get("image_url")):
                expected_math += 1
            for runs in fields:
                expected_math += sum(r["kind"] == "math" for r in runs)
                expected.extend(
                    r["text"] for r in runs if r["kind"] == "text" and r["text"].strip()
                )
        missing = [text for text in expected if text not in actual]
        assert not missing, missing[:5]
        geometry = json.loads(
            js(
                "JSON.stringify({blocks:document.querySelectorAll('[data-block-id]').length,brokenImages:Array.from(document.images).filter(i=>!i.naturalWidth).length,katex:document.querySelectorAll('.paper-document .katex').length,mathFallbacks:document.querySelectorAll('.paper-document .math-fallback').length,bodyOverflow:document.body.scrollWidth>innerWidth,toolbarOverflow:document.querySelector('.reader-toolbar').scrollWidth>innerWidth})"
            )
        )
        assert geometry["katex"] + geometry["mathFallbacks"] == expected_math, (
            expected_math,
            geometry,
        )
        assert geometry["blocks"] == len(document["blocks"]), geometry
        assert geometry["brokenImages"] == 0, geometry
        assert not geometry["bodyOverflow"] and not geometry["toolbarOverflow"], geometry
        assert not js("!!document.querySelector('button button')")
        return {"text_runs_exact": len(expected), **geometry}

    def verify_bindings(paper, document, language, saved):
        projected = explanations.project(paper["id"], document)
        coverage = summarize_anchor_coverage(saved, projected)
        entries = {entry["target_id"]: entry for entry in saved["explanations"]}
        expected = {target["id"] for target in projected["targets"] if target.get("localized")}
        if expected:
            until("!!document.querySelector('.explanation-trigger')")
        actual = set(
            json.loads(
                js(
                    "JSON.stringify(Array.from(document.querySelectorAll('.explanation-trigger')).map(e=>e.dataset.explanationId))"
                )
            )
        )
        assert actual == expected, {
            "language": language,
            "missing": sorted(expected - actual),
            "unexpected": sorted(actual - expected),
        }
        checked = []
        requests = []
        for kind in ("term", "math"):
            candidates = [tid for tid in actual if entries[tid]["kind"] == kind]
            candidates.sort(key=lambda tid: (entries[tid].get("math_role") != "formula", tid))
            if not candidates:
                if args.require_published:
                    raise AssertionError(
                        f"{paper['title']} {language}: no visible {kind} explanation"
                    )
                continue
            requests.append((kind, candidates[0], 0, "representative"))
        planned = language == "korean" and plan and paper["id"] == plan["paper_id"]
        if planned:
            assert (
                saved["revision"] == plan["revision"]
                and saved["fingerprint"] == plan["fingerprint"]
            ), "Published content changed from the exact click plan"
            for tid in plan["target_ids"]:
                assert tid in actual and entries[tid]["kind"] == "term", tid
                requests.append(("term", tid, 0, "repaired_alias"))
            counts = json.loads(
                js(
                    "JSON.stringify(Array.from(document.querySelectorAll('.term-trigger')).reduce((out,e)=>{let id=e.dataset.explanationId;out[id]=(out[id]||0)+1;return out},{}))"
                )
            )
            repeats = sorted(
                (tid for tid, count in counts.items() if count >= 2),
                key=lambda tid: (tid not in plan["target_ids"], -counts[tid], tid),
            )[: plan["repeat_term_pairs"]]
            assert len(repeats) == plan["repeat_term_pairs"], "Insufficient repeated term targets"
            for tid in repeats:
                requests.extend(
                    [
                        ("term", tid, 0, "repeated_first"),
                        ("term", tid, counts[tid] - 1, "repeated_last"),
                    ]
                )
        target_lookup = {target["id"]: target for target in projected["targets"]}
        for kind, tid, occurrence_index, purpose in requests:
            expression = f"Array.from(document.querySelectorAll('.explanation-trigger')).filter(e=>e.dataset.explanationId==={json.dumps(tid)})[{occurrence_index}]"
            clicked = json.loads(
                js(
                    f"JSON.stringify((()=>{{let e={expression};return {{text:e.textContent,block_id:e.closest('[data-block-id]').dataset.blockId}}}})())"
                )
            )
            if kind == "term":
                assert any(
                    anchor["block_id"] == clicked["block_id"] and anchor["text"] == clicked["text"]
                    for anchor in target_lookup[tid]["localized"]
                ), clicked
            js(f"({expression}).scrollIntoView({{block:'center'}})")
            pause()
            before = js(
                "JSON.stringify({text:document.querySelector('.paper-document').textContent,width:document.querySelector('.paper-document').getBoundingClientRect().width,scroll:document.querySelector('.reader').scrollTop})"
            )
            assert js(
                f"(()=>{{let e={expression};window.__pairperAuditTrigger=e;e.focus({{preventScroll:true}});e.click();return true}})()"
            )
            until("!!document.querySelector('.explanation-panel.is-open')")
            after = js(
                "JSON.stringify({text:document.querySelector('.paper-document').textContent,width:document.querySelector('.paper-document').getBoundingClientRect().width,scroll:document.querySelector('.reader').scrollTop})"
            )
            assert before == after, f"Opening {language} {kind} changed the document"
            panel_text = js("document.querySelector('.explanation-panel').textContent")
            entry = entries[tid]
            if kind == "term":
                assert (
                    js("document.querySelector('.explanation-category').textContent")
                    == entry["category"]
                )
                assert (
                    js("document.querySelector('.explanation-subject').textContent")
                    == clicked["text"]
                ), "Term panel title must match the actual clicked localized alias"
                segments = entry["description"]
                rendered_math = json.loads(
                    js(
                        "JSON.stringify(Array.from(document.querySelectorAll('.explanation-panel-content .katex annotation,.explanation-panel-content .math-fallback')).map(e=>e.textContent))"
                    )
                )
                assert rendered_math == [
                    segment["latex"] for segment in segments if segment["kind"] == "math"
                ]
            else:
                segments = [*entry["meaning"], *entry["process"]]
                assert all(symbol["meaning"] in panel_text for symbol in entry["symbols"])
            assert all(
                segment["text"] in panel_text for segment in segments if segment["kind"] == "text"
            )
            assert js(f"({expression}).classList.contains('selected')")
            assert js(
                f"Array.from(document.querySelectorAll('.explanation-trigger.selected')).every(e=>e.dataset.explanationId==={json.dumps(tid)})"
            )
            suffix = kind if purpose == "representative" else f"{purpose}-{tid}-{occurrence_index}"
            shot(f"{paper['id']}-{language}-published-{suffix}.png")
            click('[aria-label="해설 닫기"]')
            assert js("document.activeElement===window.__pairperAuditTrigger"), (
                "Focus did not return to the actual clicked occurrence"
            )
            assert before == js(
                "JSON.stringify({text:document.querySelector('.paper-document').textContent,width:document.querySelector('.paper-document').getBoundingClientRect().width,scroll:document.querySelector('.reader').scrollTop})"
            )
            checked.append(
                {
                    "kind": kind,
                    "target_id": tid,
                    "occurrence_index": occurrence_index,
                    "purpose": purpose,
                    "clicked_anchor": clicked,
                    "panel_matches_published_content": True,
                    "geometry_unchanged": True,
                    "focus_restored": True,
                }
            )
        return {
            "expected_target_count": len(expected),
            "actual_target_count": len(actual),
            "projection_coverage": coverage,
            "clicks": checked,
        }

    result = {}
    try:
        expected_papers = len(library.list_papers())
        until(f"document.querySelectorAll('.paper-row').length==={expected_papers}")
        for paper in library.list_papers():
            if args.paper_id and paper["id"] not in args.paper_id:
                continue
            if not paper["has_view"]:
                continue
            row = {"paper_id": paper["id"], "title": paper["title"]}
            title = paper["title"].strip() or paper["filename"]
            assert js(
                f"(()=>{{let e=Array.from(document.querySelectorAll('.paper-row')).find(x=>x.querySelector('strong').textContent==={json.dumps(title)});if(!e)return false;e.click();return true}})()"
            )
            until("document.documentElement.dataset.readerReady==='true'")
            document = display_document(library, paper["id"])
            row["source"] = audit(document)
            shot(f"{paper['id']}-source-1280.png")
            saved = explanations.get(paper["id"])
            row["explanations_status"] = saved["status"]
            row["published_targets"] = len(saved["targets"])
            if args.require_published:
                assert saved["status"] == "published" and saved["targets"], (
                    f"{paper['title']}: published explanations missing"
                )
            if saved["status"] == "published" and saved["targets"]:
                row["source_bindings"] = verify_bindings(paper, document, "source", saved)
            sessions = translations.sessions(paper["id"])
            if sessions:
                click(".reader-language button:last-child")
                until("!!document.querySelector('[aria-label=\"한국어 논문\"]')")
                translated = translations.display(sessions[0]["session_id"])
                assert not js("!!document.querySelector('.explanation-panel.is-open')"), (
                    "Language switch retained a stale explanation"
                )
                row["korean"] = audit(translated)
                if saved["status"] == "published" and saved["targets"]:
                    row["korean_bindings"] = verify_bindings(paper, translated, "korean", saved)
                shot(f"{paper['id']}-korean-1280.png")
                # Check a long display formula and a table with inline mathematics.
                formulas = [
                    b
                    for b in translated["blocks"]
                    if b["type"] == "equation" and b["content"].get("latex")
                ]
                if formulas:
                    longest = max(formulas, key=lambda b: len(b["content"]["latex"]))
                    js(
                        f"document.getElementById({json.dumps('block-' + longest['id'])}).scrollIntoView({{block:'center'}})"
                    )
                    shot(f"{paper['id']}-long-formula-1280.png")
                window.resize(480, 850)
                pause(300)
                row["korean_narrow"] = audit(translated)
                shot(f"{paper['id']}-korean-480.png")
                window.resize(1280, 850)
                pause(200)
            click(".back-button")
            until(f"document.querySelectorAll('.paper-row').length==={expected_papers}")
            rows.append(row)
        assert before == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        result = {
            "passed": True,
            "rendering_and_published_panels_passed": True,
            "verification_scope": "Source/Korean rendering and published projected anchors/panels; anchor completeness is reported separately",
            "anchor_coverage": aggregate_anchor_coverage(rows),
            "papers": rows,
            "source_and_translation_assets_unchanged": True,
            "database_access": "SQLite mode=ro",
            "library": str(root),
            "published_required": args.require_published,
            "korean_click_plan": {"sha256": hashlib.sha256(plan_bytes).hexdigest(), "content": plan}
            if plan_bytes
            else None,
            "runtime_modules": {
                name: str(Path(sys.modules[obj.__module__].__file__).resolve())
                for name, obj in (("presentation", display_document), ("window", MainWindow))
            },
        }
    except Exception as exc:
        shot("failure.png")
        result = {
            "passed": False,
            "rendering_and_published_panels_passed": False,
            "anchor_coverage": aggregate_anchor_coverage(rows),
            "papers": rows,
            "error": str(exc),
        }
    finally:
        window.close()
        (output / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), "utf-8"
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
