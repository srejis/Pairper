import hashlib
from types import SimpleNamespace

import pytest
from conftest import text_blocks

from pairper.translation.service import TranslationService
from pairper.update import PaperUpdates


def reconstructed(library, pdf_path):
    paper = library.register(pdf_path)
    job = library.begin(paper['id'], paper['sha256'])
    library.append(job['job_id'], text_blocks(library, paper), [], [1])
    assert library.validate(job['job_id'], finalize=True)['valid']
    return library.paper(paper['id'])


def test_pending_includes_missing_title_and_translation(library, pdf_path):
    updates = PaperUpdates(library)
    library.register(pdf_path)
    assert updates.list_updates()[0]['needs'] == ['reconstruction', 'title', 'korean']
    reconstructed(library, pdf_path)
    assert library.list_papers(pending=True) == []
    assert updates.list_updates()[0]['needs'] == ['title', 'korean']


def test_title_persists_without_changing_source_or_translations(library, pdf_path):
    paper = reconstructed(library, pdf_path)
    translations = TranslationService(library)
    started = translations.begin(paper['id'])
    paths = list((library.root / 'views').glob('*')) + list((library.root / 'sources').glob('*'))
    paths += list((library.root / 'originals').glob('*'))
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    bundle = translations.current_bundle(started['session_id'])
    updates = PaperUpdates(library)
    packet = updates.title_candidates(paper['id'])
    block_ids = [packet['candidates'][0]['block_id']]
    args = (paper['id'], paper['sha256'], paper['current_view_id'], block_ids, True)
    assert updates.confirm_title(*args)['title'] == 'Source title'
    assert not updates.confirm_title(*args)['changed']
    assert library.paper(paper['id'])['title'] == 'Source title'
    assert library.paper(paper['id'])['filename'] == pdf_path.name
    assert translations.current_bundle(started['session_id']) == bundle
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths} == before
    assert updates.list_updates()[0]['needs'] == ['korean']
    with library.connect() as con:
        assert con.execute('SELECT COUNT(*) FROM paper_titles').fetchone()[0] == 1


def test_title_rejects_stale_or_unreviewed_or_non_title(library, pdf_path):
    paper = reconstructed(library, pdf_path)
    updates = PaperUpdates(library)
    for digest, view, ids, inspected in [
        ('wrong', paper['current_view_id'], ['b0'], True),
        (paper['sha256'], 'old', ['b0'], True),
        (paper['sha256'], paper['current_view_id'], ['b1'], True),
        (paper['sha256'], paper['current_view_id'], ['b0'], False),
        (paper['sha256'], paper['current_view_id'], ['b0', 'b0'], True),
    ]:
        with pytest.raises(ValueError):
            updates.confirm_title(paper['id'], digest, view, ids, inspected)
    assert library.paper(paper['id'])['title'] == 'Unit fixture'


def test_title_only_update_reuses_published_translation(library, pdf_path, monkeypatch):
    paper = reconstructed(library, pdf_path)
    monkeypatch.setattr(TranslationService, 'sessions', lambda self, pid: [{'session_id': 'saved'}])
    monkeypatch.setattr(TranslationService, 'report', lambda self, sid: {
        'session_id': sid, 'revision': 9, 'status': 'published', 'complete': True})
    updates = PaperUpdates(library)
    assert updates.list_updates()[0]['needs'] == ['title']
    updates.confirm_title(paper['id'], paper['sha256'], paper['current_view_id'], ['b0'], True)
    assert updates.list_updates() == []
    assert updates.list_updates(include_complete=True)[0]['complete']


def test_failed_translation_lookup_keeps_title_work(library, pdf_path, monkeypatch):
    paper = reconstructed(library, pdf_path)
    def broken(self, pid):
        raise ValueError('Damaged draft')
    monkeypatch.setattr(TranslationService, 'sessions', broken)
    updates = PaperUpdates(library)
    result = updates.list_updates()[0]
    assert result['errors']['korean'] == 'Damaged draft'
    updates.confirm_title(paper['id'], paper['sha256'], paper['current_view_id'], ['b0'], True)
    assert updates.list_updates()[0]['needs'] == ['korean']


def test_bridge_uses_saved_title_instead_of_pdf_metadata(library, pdf_path):
    from pairper.ui.bridge import FrontendBridge
    paper = reconstructed(library, pdf_path)
    updates = PaperUpdates(library)
    updates.confirm_title(paper['id'], paper['sha256'], paper['current_view_id'], ['b0'], True)
    assert FrontendBridge.paper_list(SimpleNamespace(library=library))[0]['title'] == 'Source title'


async def test_mcp_exposes_unified_workflow_and_title_confirmation(library, pdf_path):
    from mcp import Client

    from pairper.mcp.server import create_server
    paper = reconstructed(library, pdf_path)
    async with Client(create_server(library)) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert {'list_paper_updates', 'get_paper_title_candidates', 'confirm_paper_title'} <= names
        candidates = await client.call_tool('get_paper_title_candidates', {'paper_id': paper['id']})
        assert candidates.structured_content['candidates'][0]['title'] == 'Source title'
        confirmed = await client.call_tool('confirm_paper_title', {
            'paper_id': paper['id'], 'source_sha256': paper['sha256'],
            'view_id': paper['current_view_id'], 'block_ids': ['b0'], 'image_inspected': True})
        assert not confirmed.is_error
        pending = await client.call_tool('list_paper_updates', {'paper_id': paper['id']})
        assert 'korean' in str(pending.content)
        assert 'list_paper_updates' in str((await client.get_prompt('update_papers')).messages)
