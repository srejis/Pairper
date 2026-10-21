"""A cold reader and its explanation projection can render the same asset."""
import os
import uuid

import pytest

from pairper.document.models import Region


def test_asset_keeps_concurrent_winner_opened_by_reader(library, monkeypatch):
    region = Region(page=1, bbox=(0, 0, 20, 20))
    key = uuid.uuid5(uuid.NAMESPACE_URL, 'paper' + region.model_dump_json()).hex
    destination = library.root / 'assets' / f'{key}.png'
    monkeypatch.setattr(library, 'image', lambda *args: b'same complete PNG')

    def concurrent_winner(temp, path):
        # Simulate another projection winning the race and Qt opening its file.
        path.write_bytes(b'same complete PNG')
        raise PermissionError(5, 'Reader already opened the published asset')

    monkeypatch.setattr(os, 'rename' if os.name == 'nt' else 'replace', concurrent_winner)
    assert library.asset('paper', region) == key
    assert destination.read_bytes() == b'same complete PNG'
    assert list(destination.parent.glob('*.tmp')) == []


def test_asset_does_not_hide_failure_without_valid_winner(library, monkeypatch):
    region = Region(page=1, bbox=(0, 0, 20, 20))
    monkeypatch.setattr(library, 'image', lambda *args: b'complete PNG')

    def denied(temp, path):
        raise PermissionError('Destination not writable')

    monkeypatch.setattr(os, 'rename' if os.name == 'nt' else 'replace', denied)
    with pytest.raises(PermissionError):
        library.asset('paper', region)
    assert list((library.root / 'assets').glob('*.tmp')) == []
