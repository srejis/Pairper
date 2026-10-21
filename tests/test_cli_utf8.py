"""Real CP949 byte streams expose encoding failures hidden by UTF-8 harnesses."""
import io
import json

import pytest

from pairper.translation import cli


@pytest.mark.parametrize('source_error', [False, True])
def test_cli_cp949_console_and_pipe_emit_utf8_source_tokens(monkeypatch, source_error):
    message = '원문 ⟦source#M0⟧: α ≤ μ 한글'
    output, diagnostic = io.BytesIO(), io.BytesIO()
    stdout = io.TextIOWrapper(output, encoding='cp949', errors='strict')
    stderr = io.TextIOWrapper(diagnostic, encoding='cp949', errors='backslashreplace')
    class Plan:
        def __init__(self, *args, **kwargs):
            pass
        def start(self, *args, **kwargs):
            if source_error:
                raise ValueError(message)
            return {'paper_id': 'p', 'status': 'ready', 'run_id': message}
    monkeypatch.setattr(cli, 'Library', lambda *_: object())
    monkeypatch.setattr(cli, 'TranslationService', lambda *_: object())
    monkeypatch.setattr(cli, 'Workflow', Plan)
    def forbidden(**kwargs):
        raise AssertionError('Plan-only must not call an AI provider')
    monkeypatch.setattr(cli, 'CodexRunner', forbidden)
    monkeypatch.setattr(cli.sys, 'stdout', stdout)
    monkeypatch.setattr(cli.sys, 'stderr', stderr)
    result = cli.main(['p', '--experiment', 'utf8', '--plan-only'])
    assert result == (2 if source_error else 0)
    assert stdout.encoding == stderr.encoding == 'utf-8'
    assert stderr.errors == 'backslashreplace'
    # Real stderr writes use the same configured byte stream as argparse/errors.
    print(message, file=stderr)
    stdout.flush(); stderr.flush()
    decoded = output.getvalue().decode('utf8')
    lines = [json.loads(line) for line in decoded.splitlines()]
    assert message in decoded
    assert '\\u27e6' not in decoded
    assert diagnostic.getvalue().decode('utf8').splitlines() == [message]
    if source_error:
        assert next(x for x in lines if x.get('event') == 'paper_failed')['error'] == message
    else:
        assert next(x for x in lines if x.get('event') == 'started')['run_id'] == message
    # Prevent wrapper finalization closing the private byte buffers after restore.
    stdout.detach(); stderr.detach()


def test_cli_stringio_capture_needs_no_reconfigure(monkeypatch):
    capture = io.StringIO()
    monkeypatch.setattr(cli.sys, 'stdout', capture)
    monkeypatch.setattr(cli.sys, 'stderr', io.StringIO())
    with pytest.raises(SystemExit) as exc:
        cli.main(['--help'])
    assert exc.value.code == 0
    assert 'Pairper 원문 기반 한국어' in capture.getvalue()
