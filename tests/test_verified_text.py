import pytest
from pydantic import ValidationError

from pairper.document.models import TextRun
from pairper.document.text import resolve_text


def test_drawn_filename_underscore_preserves_raw_source_and_audit():
    # AutoML-Agent p16: underscore is a white PDF line, extracted as a space.
    spans = {"title": {"text": "text classification.py"}}
    run = TextRun(refs=["title"], text_override="text_classification.py", source_verified=True)
    audit = []
    assert resolve_text(run, spans, audit) == "text_classification.py"
    assert spans["title"]["text"] == "text classification.py"
    assert audit[0]["original_strings"] == ["text classification.py"]
    assert audit[0]["operation"] == "verified_text_transcription"
    with pytest.raises(ValidationError, match="requires source verification"):
        TextRun(refs=["title"], text_override="text_classification.py")
