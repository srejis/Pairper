from types import SimpleNamespace

from PySide6.QtCore import QUrl

from pairper.ui.window import FRONTEND, LocalOnly


def test_production_ui_blocks_external_and_private_files(tmp_path):
    policy = SimpleNamespace(roots=[FRONTEND.resolve(), tmp_path / "assets"], dev_url=None)
    assert LocalOnly.allowed(policy, QUrl.fromLocalFile(str(FRONTEND / "index.html")))
    assert LocalOnly.allowed(policy, QUrl("qrc:///qtwebchannel/qwebchannel.js"))
    assert not LocalOnly.allowed(policy, QUrl("https://example.com/tracker.js"))
    assert not LocalOnly.allowed(policy, QUrl("http://127.0.0.1:8080/private"))
    assert not LocalOnly.allowed(policy, QUrl.fromLocalFile(str(tmp_path / ".env")))
    assert not LocalOnly.allowed(policy, QUrl.fromLocalFile(str(tmp_path / "library.db")))
    assert not LocalOnly.allowed(policy, QUrl("data:text/html,unsafe"))
    assert not LocalOnly.allowed(policy, QUrl("qrc:///other.js"))


def test_development_ui_allows_only_explicit_loopback_origin(tmp_path):
    policy = SimpleNamespace(roots=[], dev_url="http://127.0.0.1:5173/")
    assert LocalOnly.allowed(policy, QUrl("http://127.0.0.1:5173/src/main.tsx"))
    assert LocalOnly.allowed(policy, QUrl("ws://127.0.0.1:5173/"))
    assert not LocalOnly.allowed(policy, QUrl("http://127.0.0.1:9999/"))
    assert not LocalOnly.allowed(policy, QUrl("http://evil.example:5173/"))
