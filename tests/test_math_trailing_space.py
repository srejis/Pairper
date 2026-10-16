import pytest

from pairper.document.models import MathRun, Region, TextRun
from pairper.document.text import resolved_runs


@pytest.mark.parametrize(("right", "prefix"), [
    ("different plans", " "), ("devises each plan", " "),
    (" into", ""), (", where", ""), ("'s prompt", " "),
])
def test_math_trailing_source_whitespace_is_preserved(right, prefix):
    # AutoML-Agent: original " P " -> KaTeX P -> "different plans".
    spans = {
        "left": {"text": " P ", "bbox": (0, 0, 10, 10), "size": 10},
        "right": {"text": right, "bbox": (12, 0, 90, 10), "size": 10},
    }
    runs = [MathRun(latex="P", refs=["left"], source_verified=True,
                    region=Region(page=1, bbox=(0, 0, 10, 10))), TextRun(refs=["right"])]
    assert list(resolved_runs(runs, spans, []))[1][1] == prefix
