"""Offline HTML. All model text is escaped; scripts only load from bundled resources."""

import html
import os
import uuid
from pathlib import Path

from pairper.document.models import Document, MathRun

WEB = Path(__file__).resolve().parents[1] / "web"


def esc(value):
    return html.escape(str(value), quote=True)


def join_text(run, spans, audit):
    texts = [spans[ref]["text"] for ref in run.refs]
    separator = {"space": " ", "none": "", "newline": "\n", "dehyphenate": " "}[run.join]
    result = texts[0] if texts else ""
    for text in texts[1:]:
        if run.join == "dehyphenate" and result.endswith("-"):
            result = result[:-1] + text
        else:
            result += separator + text
    if len(texts) > 1:
        audit.append(
            {"refs": run.refs, "original_strings": texts, "operation": run.join, "result": result}
        )
    return esc(result)


def render_document(library, doc: Document, output: Path):
    source = library.source(doc.paper_id)
    spans = {s["id"]: s for page in source["pages"] for s in page["spans"]}
    audit = []

    def image(region, inline=False):
        asset = library.asset(doc.paper_id, region)
        url = library.managed(f"assets/{asset}.png").as_uri()
        return f'<img class="{"inline-source" if inline else "source"}" src="{esc(url)}" alt="원본 영역">'

    def math(latex, region, verified, display):
        # KaTeX validates syntax in the browser. Invalid or unverified math keeps the image.
        fallback = image(region, not display)
        if not latex or not verified:
            return fallback
        return (
            f'<span class="math" data-latex="{esc(latex)}" '
            f'data-display="{str(display).lower()}"><span class="math-output"></span>'
            f'<span class="math-fallback">{fallback}</span></span>'
        )

    def runs(items):
        parts = []
        for run in items:
            if isinstance(run, MathRun):
                parts.append(math(run.latex, run.region, run.source_verified, False))
            else:
                parts.append(join_text(run, spans, audit))
        return "".join(parts)

    bodies = []
    for block in sorted(doc.blocks, key=lambda b: b.order):
        c = block.content
        body = runs(c.runs)
        if block.type == "title":
            body = f"<h1>{body}</h1>"
        elif block.type == "heading":
            body = f"<h{c.level}>{body}</h{c.level}>"
        elif block.type == "list":
            tag = "ol" if c.ordered else "ul"
            body = f"<{tag}>" + "".join(f"<li>{runs(item)}</li>" for item in c.items) + f"</{tag}>"
        elif block.type == "equation":
            body = math(c.latex, c.region, c.source_verified, True)
            if c.number_refs:
                body += (
                    '<span class="equation-number">'
                    + esc(" ".join(spans[r]["text"] for r in c.number_refs))
                    + "</span>"
                )
        elif block.type == "table" and c.cells and c.source_verified:
            rows = {}
            for cell in sorted(c.cells, key=lambda x: (x.row, x.column)):
                tag = "th" if cell.header else "td"
                rows.setdefault(cell.row, []).append(
                    f'<{tag} rowspan="{cell.rowspan}" colspan="{cell.colspan}">'
                    f"{runs(cell.runs)}</{tag}>"
                )
            body = (
                '<div class="table-scroll"><table>'
                + "".join("<tr>" + "".join(cells) + "</tr>" for cells in rows.values())
                + "</table></div>"
            )
        elif block.type in {"figure", "source_region", "table"}:
            body = image(c.region)
        else:
            body = f"<p>{body}</p>"
        # Source access is always available, including verified reconstructed equations/tables.
        if c.region:
            body += "<details><summary>원본 대조</summary>" + image(c.region) + "</details>"
        trace = [
            {"id": ref, "text": spans[ref]["text"], "bbox": spans[ref]["bbox"]}
            for ref in block.source_refs
            if ref in spans
        ]
        if trace:
            import json

            body += (
                '<details class="trace"><summary>원문 참조</summary><pre>'
                + esc(json.dumps(trace, ensure_ascii=False, indent=2))
                + "</pre></details>"
            )
        bodies.append(f'<section id="{esc(block.id)}" class="block {block.type}">{body}</section>')
    css = (WEB / "reader.css").as_uri()
    katex_css = (WEB / "katex/katex.min.css").as_uri()
    katex_js = (WEB / "katex/katex.min.js").as_uri()
    reader_js = (WEB / "reader.js").as_uri()
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "script-src file:; style-src file: 'unsafe-inline'; img-src file:; "
        "font-src file:; connect-src 'none'; base-uri 'none'; form-action 'none'\">"
        f'<link rel="stylesheet" href="{esc(css)}">'
        f'<link rel="stylesheet" href="{esc(katex_css)}">'
        f'<script defer src="{esc(katex_js)}"></script>'
        f'<script defer src="{esc(reader_js)}"></script><title>Pairper</title></head>'
        "<body><main>" + "".join(bodies) + "</main></body></html>"
    )
    temp = output.with_name(output.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(document, encoding="utf-8")
    os.replace(temp, output)
    from pairper.storage.library import write_json

    write_json(output.with_suffix(".normalization.json"), audit)
    return output
