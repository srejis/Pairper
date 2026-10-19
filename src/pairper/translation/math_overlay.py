"""Isolated bundled-KaTeX proof for explicitly reviewed prose overlays.

A successful proof protects the remaining AST, not the meaning of selected prose.
Indices are preorder text-node indices in each input's own AST, never text values.
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

MAX_LATEX_CHARS = 100_000
TIMEOUT_SECONDS = 15


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _latex(value):
    if not isinstance(value, str) or len(value) > MAX_LATEX_CHARS:
        raise ValueError('latex must be a string of at most 100000 characters')
    return value


def _indices(value):
    if not isinstance(value, (list, tuple)) or any(type(x) is not int or x < 0 for x in value):
        raise ValueError('prose indices must be nonnegative integer lists')
    if len(set(value)) != len(value):
        raise ValueError('duplicate prose index')
    return list(value)


def _run(request):
    import pairper
    resource = Path(pairper.__file__).resolve().parent / 'web/katex/katex.min.js'
    if not resource.is_file():
        raise ValueError('Bundled KaTeX resource unavailable')
    payload = dict(request, resource=str(resource))
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker'],
            input=json.dumps(payload, ensure_ascii=True).encode('utf-8'), capture_output=True,
            timeout=TIMEOUT_SECONDS, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('Bundled KaTeX overlay verification timed out') from exc
    if result.returncode:
        raise ValueError('Bundled KaTeX overlay worker failed: ' + result.stderr.decode('utf-8', errors='replace')[-1000:])
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeError) as exc:
        raise ValueError('Invalid overlay worker response') from exc


def inspect_latex(latex):
    latex = _latex(latex)
    result = _run({'operation': 'inspect', 'latex': latex})
    result['source_latex_sha256'] = _hash(latex)
    result['semantic_quality_proven'] = False
    return result


def inspect_many(latex_items):
    """Inspect at most 64 expressions in one isolated renderer process.

    Caller may cache by exact source SHA256 plus bundled KaTeX version. Selected
    prose may itself contain numbers/identifiers: their meaning needs review.
    Only the unselected mathematical AST is protected by verify_overlay.
    """
    if not isinstance(latex_items, (list, tuple)) or len(latex_items) > 64:
        raise ValueError('inspect_many accepts at most 64 expressions')
    items = [_latex(x) for x in latex_items]
    if not items:
        return []
    unique = list(dict.fromkeys(items))
    results = _run({'operation': 'inspect_batch', 'items': unique})['items']
    by_latex = dict(zip(unique, results, strict=True))
    return [dict(by_latex[x], source_latex_sha256=_hash(x), semantic_quality_proven=False) for x in items]


def verify_overlay(source_latex, localized_latex, source_prose_indices, target_prose_indices):
    source_latex, localized_latex = _latex(source_latex), _latex(localized_latex)
    source_indices, target_indices = _indices(source_prose_indices), _indices(target_prose_indices)
    result = _run({'operation': 'verify', 'source': source_latex, 'target': localized_latex,
                   'source_indices': source_indices, 'target_indices': target_indices})
    result.update(source_latex_sha256=_hash(source_latex), target_latex_sha256=_hash(localized_latex),
                  semantic_quality_proven=False)
    # These hashes bind a caller's review to exact inputs and exact node selections.
    result['selection_sha256'] = _hash(json.dumps([source_indices, target_indices], separators=(',', ':')))
    if result.get('success'):
        skeleton = result.pop('mathematical_ast_json')
        result['mathematical_ast_sha256'] = _hash(skeleton)
    return result


def _worker():
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtQml import QJSEngine
    request = json.loads(sys.stdin.buffer.read())
    app = QCoreApplication([])
    engine = QJSEngine()
    loaded = engine.evaluate(Path(request.pop('resource')).read_text('utf-8'))
    if loaded.isError():
        raise RuntimeError(loaded.toString())
    script = Path(__file__).with_name('math_overlay_worker.js').read_text('utf-8')
    loaded = engine.evaluate(script)
    if loaded.isError():
        raise RuntimeError(loaded.toString())
    result = engine.evaluate('JSON.stringify(overlayRequest(' + json.dumps(request, ensure_ascii=True) + '))')
    if result.isError():
        raise RuntimeError(result.toString())
    sys.stdout.buffer.write(result.toString().encode('utf-8'))
    del engine
    del app


if __name__ == '__main__':
    _worker()
