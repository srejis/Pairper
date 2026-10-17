"""Execute the shipped KaTeX without a browser, isolated from GUI/MCP threads."""

import json
import os
import subprocess
import sys
from pathlib import Path

from pairper.document.models import MathRun, block_runs


def check_document_math(document):
    items = []
    for block in document.blocks:
        if block.type == "equation":
            items.append({"block": block.id, "latex": block.content.latex, "display": True})
        items += [{"block": block.id, "latex": run.latex, "display": False}
                  for run in block_runs(block) if isinstance(run, MathRun)]
    if not items:
        return {"checked": 0, "errors": []}
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pairper.document.mathcheck"],
            input=json.dumps(items).encode("utf-8"), capture_output=True, timeout=30, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("The bundled math renderer timed out") from exc
    if result.returncode:
        raise ValueError("The bundled math renderer could not be validated: "
                         + result.stderr.decode("utf-8", errors="replace")[-1000:])
    return json.loads(result.stdout)


def main():
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtQml import QJSEngine

    app = QCoreApplication([])
    engine = QJSEngine()
    resource = Path(__file__).resolve().parents[1] / "web" / "katex" / "katex.min.js"
    loaded = engine.evaluate(resource.read_text("utf-8"))
    if loaded.isError():
        raise RuntimeError(loaded.toString())
    items = json.loads(sys.stdin.buffer.read())
    script = """(function(items) {
        var errors = [], seen = {};
        items.forEach(function(item) {
            var key = JSON.stringify([item.latex, item.display]);
            if (!(key in seen)) {
                try {
                    katex.renderToString(item.latex, {displayMode: item.display,
                        throwOnError:true, trust:false, strict:'error', maxExpand:1000});
                    seen[key] = null;
                } catch (error) { seen[key] = String(error); }
            }
            if (seen[key]) errors.push(item.block + ': ' + seen[key]);
        });
        return JSON.stringify({checked: items.length, errors: errors, version: katex.version});
    })(""" + json.dumps(items) + ")"
    result = engine.evaluate(script)
    if result.isError():
        raise RuntimeError(result.toString())
    sys.stdout.buffer.write(result.toString().encode("utf-8"))
    del engine
    del app


if __name__ == "__main__":
    main()
