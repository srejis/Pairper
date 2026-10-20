"""Actual MCP SDK registration/dispatch, without invoking an AI backend.

Uses the product create_server and in-process SDK Client transport, a real
temporary Library, and original-source mini fixtures. Remote connector discovery
and subprocess stdio transport are separate integration scopes.
"""
import copy
import json

import pytest
from mcp import Client

from test_translation_workflow import setup_workflow, engine
from pairper.storage.library import Library
from pairper.translation import service as service_module, agent_runner
from pairper.mcp.server import create_server


def payload(result):
    # Untyped `-> dict` MCP tools may return SDK text JSON rather than an
    # output-schema structured result. Both are ordinary SDK success encodings.
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(next(content.text for content in result.content if content.type == 'text'))


@pytest.fixture
def sdk_server(setup_workflow, monkeypatch, tmp_path):
    workflow, service = setup_workflow()
    library = Library(tmp_path / 'library')
    service.library = library
    calls = {'constructed':0, 'ai':0}
    class NoAIRunner:
        def __init__(self): calls['constructed'] += 1
        def run(self, *_):
            calls['ai'] += 1
            raise AssertionError('SDK planning test must not invoke an AI backend')
        def cancel(self): pass
    monkeypatch.setattr(service_module, 'TranslationService', lambda *_: service)
    monkeypatch.setattr(agent_runner, 'CodexRunner', NoAIRunner)
    def prepare(selected_library, paper_id, directory):
        assert selected_library is library
        if paper_id != service.bundle['paper_id']:
            raise ValueError('Unknown paper ID')
        return copy.deepcopy(service.bundle)
    monkeypatch.setattr(engine, 'prepare_bundle', prepare)
    return create_server(library, translation_directory=service.directory), workflow, service, calls


@pytest.mark.asyncio
async def test_actual_sdk_discovers_workflow_tools_prompts_and_updated_guides(sdk_server):
    server, _, _, calls = sdk_server
    async with Client(server) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools.tools}
        assert {'start_translation_workflow','run_translation_workflow',
                'get_translation_workflow_report','finalize_translation_workflow',
                'cancel_translation_workflow'} <= names
        prompts = await client.list_prompts()
        assert 'run_korean_translation_system' in {prompt.name for prompt in prompts.prompts}
        prompt = await client.get_prompt('run_korean_translation_system')
        assert 'start_translation_workflow' in str(prompt.messages)
        guide = await client.call_tool('get_translation_guide', {})
        assert not guide.is_error
        assert 'run_translation_workflow' in str(guide.content)
        assert calls == {'constructed':0, 'ai':0}


@pytest.mark.asyncio
async def test_actual_sdk_fresh_start_and_report_do_not_initialize_ai(sdk_server):
    server, _, service, calls = sdk_server
    async with Client(server) as client:
        arguments = {'paper_id':service.bundle['paper_id'], 'experiment_id':'sdk-fresh'}
        started = await client.call_tool('start_translation_workflow', arguments)
        assert not started.is_error
        run = payload(started)
        report = await client.call_tool('get_translation_workflow_report', {'run_id':run['run_id']})
        assert not report.is_error
        assert payload(report)['background_running'] is False
        resumed = await client.call_tool('start_translation_workflow', arguments)
        assert payload(resumed)['run_id'] == run['run_id']
        assert service.store.read(run['session_id'],service.bundle['fingerprint'])['result']['entries'] == []
        assert calls == {'constructed':0, 'ai':0}


@pytest.mark.asyncio
async def test_actual_sdk_unknown_run_returns_tool_error_without_ai_execution(sdk_server):
    server, workflow, _, calls = sdk_server
    async with Client(server) as client:
        for name in ('get_translation_workflow_report','run_translation_workflow','finalize_translation_workflow'):
            result = await client.call_tool(name, {'run_id':'unknown-run'})
            assert result.is_error, name
            assert 'Unknown' in str(result.content), result.content
        assert calls['ai'] == 0
        with workflow.connect() as con:
            assert con.execute('SELECT count(*) FROM runs').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_actual_sdk_unknown_paper_start_returns_tool_error_and_no_ai(sdk_server):
    server, workflow, _, calls = sdk_server
    async with Client(server) as client:
        result = await client.call_tool('start_translation_workflow',
                                       {'paper_id':'../../foreign-paper','experiment_id':'missing'})
        assert result.is_error and 'Unknown paper' in str(result.content)
        assert calls == {'constructed':0, 'ai':0}
        with workflow.connect() as con:
            assert con.execute('SELECT count(*) FROM runs').fetchone()[0] == 0
