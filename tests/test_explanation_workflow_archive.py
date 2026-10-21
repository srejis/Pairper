"""An actual published run remains resumable after its temporary origin disappears."""
import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_explanation_generation import Service
from test_explanation_selection import Runner, make

from pairper.explanation.alias_review import diagnostics
from pairper.explanation.catalog import digest
from pairper.explanation.projection import project_explanations
from pairper.explanation.selection_workflow import SelectionWorkflow
from pairper.explanation.service import validate
from pairper.explanation.workflow_archive import (
    INDEX,
    ArchiveEvidenceError,
    import_workflow,
    resolve_artifact_directory,
    verify_imported_archives,
)


class RecordedRunner(Runner):
    def run(self, request, schema, images, directory):
        response = super().run(request, schema, images, directory)
        folder = Path(directory) / ('call-' + uuid.uuid4().hex)
        folder.mkdir(parents=True)
        for name, value in [('request.json', request), ('response.json', response), ('schema.json', schema)]:
            (folder / name).write_text(json.dumps(value, ensure_ascii=False), 'utf-8')
        self.last_evidence = {'status': 'success', 'directory': str(folder.resolve())}
        return response


def setup(tmp_path):
    service, origin, run = make(tmp_path / 'origin', RecordedRunner())
    surfaces = {'translation_session_id': 'ko', 'translation_revision': 1, 'source_identity': {},
                'surfaces': [{'block_id': 'b', 'field': 'runs', 'run_index': 0,
                              'source_unit_ids': ['b/runs'], 'text': '다층 퍼셉트론'}]}
    service.translation_surfaces = lambda *args: copy.deepcopy(surfaces)
    assert origin.run(run)['status'] == 'ready'
    state = origin._read(run)
    ancestor = origin.directory / 'exw_ancestor' / 'batch-0' / 'call-old'
    ancestor.mkdir(parents=True)
    request = {'validation_feedback': ['old structural rejection'], 'review': None}
    (ancestor / 'request.json').write_text(json.dumps(request), 'utf-8')
    (ancestor / 'raw.txt').write_bytes(b'original\r\nbytes\x00')
    state['batches'][0]['evidence'].append({'stage': 'adopted_candidate',
        'repair_counts_before': {'semantic': 0, 'structural': 1},
        'previous_generation_evidence': [{'stage': 'repair', 'request_sha256': digest(request),
            'transport': {'directory': str(ancestor.resolve())}}]})
    state['batches'][0]['structural_repairs'] = 1
    state['adoption'] = {'source_run_id': 'exw_ancestor', 'source_run_sha256': digest({'original': 'state'})}
    origin._save(state)
    origin.commit(run)
    state = origin._read(run)
    targets, entries = validate(service.bundle, service.saved['targets'], service.saved['explanations'],
                                require_review=True, require_complete=True)
    actual = Service(tmp_path / 'actual' / 'explanations')
    actual.bundle = copy.deepcopy(service.bundle)
    actual.translation_surfaces = lambda *args: copy.deepcopy(surfaces)
    actual.payload = {**actual.bundle, 'status': 'published', 'revision': 7,
                      'targets': targets, 'explanations': entries,
                      'workflow_evidence': [copy.deepcopy(service.saved['workflow_evidence'])]}
    actual.store = SimpleNamespace(read=lambda *args, **kwargs: copy.deepcopy(actual.payload))
    actual.get = lambda *args, **kwargs: copy.deepcopy(actual.payload)
    actual.display = lambda *args: project_explanations(actual.payload, {
        'paper_id': 'p', 'view_id': None, 'translation': {'session_id': 'ko'},
        'blocks': [{'id': 'b', 'type': 'paragraph', 'content': {'latex': 'z=x+y',
                    'runs': [{'kind': 'text', 'text': '다층 퍼셉트론', 'source_unit_ids': ['b/runs']}]}}]})
    destination = SelectionWorkflow.__new__(SelectionWorkflow)
    destination.service, destination.runner = actual, RecordedRunner(('pass',))
    destination.directory, destination.max_repairs = actual.directory / 'workflows', 2
    return origin, destination, run, surfaces, ancestor


def test_dry_plan_then_atomic_import_keeps_bytes_revision_ancestry_and_is_idempotent(tmp_path):
    origin, live, run, _, ancestor = setup(tmp_path)
    source = origin._path(run)
    original = source.read_bytes()
    plan = import_workflow(live, source)
    assert plan['status'] == 'planned' and plan['actual_revision'] == 7
    assert not live.directory.exists() and source.read_bytes() == original
    assert any(a['original_directory'] == str(ancestor.resolve()) for a in plan['archives'])
    assert import_workflow(live, source, apply=True)['status'] == 'imported'
    assert live._path(run).read_bytes() == original
    assert (live.directory / '.archive/states' / source.name).read_bytes() == original
    assert live._read(run)['adoption'] == origin._read(run)['adoption']
    assert import_workflow(live, source, apply=True)['status'] == 'already_imported'
    assert sorted(p.name for p in live.directory.glob('*.json')) == [source.name]


def test_default_report_run_anchor_and_alias_after_origin_disappears_without_freezing_current_state(tmp_path):
    origin, live, run, surfaces, _ = setup(tmp_path)
    source = origin._path(run)
    original = source.read_bytes()
    import_workflow(live, source, apply=True)
    origin.service.directory.rename(tmp_path / 'retired-origin')
    assert live.report(run)['status'] == 'published'
    assert live.run(run)['status'] == 'published'
    assert import_workflow(live, source)['status'] == 'already_imported'
    state = live._read(run)
    assert live._repair_counts(state['batches'][0])['structural'] == 1
    report = diagnostics(live.service, 'p')
    target = next(t for t in live.service.payload['targets'] if t['kind'] == 'term')
    assert target['id'] in {t['target_id'] for t in report['unresolved_terms']}
    identity = {k: surfaces[k] for k in ('translation_session_id', 'translation_revision', 'source_identity')}
    live.record_alias_finding(run, target['id'], literal='다층 퍼셉트론', expected_translation_identity=identity,
        reason='Exact current model name.', expected_candidate_sha256=digest(state['batches'][0]['candidate']))
    assert live._path(run).read_bytes() != original
    assert (live.directory / '.archive/states' / source.name).read_bytes() == original
    verify_imported_archives(live.directory, run)
    assert live.report(run)['status'] == 'pending'


@pytest.mark.parametrize('damage', ['bytes', 'missing', 'mapping'])
def test_registered_archive_damage_fails_closed_even_when_original_is_still_valid(tmp_path, damage):
    origin, live, run, _, ancestor = setup(tmp_path)
    import_workflow(live, origin._path(run), apply=True)
    archived = resolve_artifact_directory(live.directory, ancestor)
    if damage == 'bytes':
        (archived / 'raw.txt').write_bytes(b'changed')
    elif damage == 'missing':
        (archived / 'raw.txt').unlink()
    else:
        (live.directory / INDEX).unlink()
    assert (ancestor / 'raw.txt').read_bytes() == b'original\r\nbytes\x00'
    with pytest.raises(ArchiveEvidenceError):
        live.report(run)


def test_conflicting_current_state_is_never_overwritten(tmp_path):
    origin, live, run, _, _ = setup(tmp_path)
    live.directory.mkdir(parents=True)
    destination = live._path(run)
    destination.write_bytes(b'existing user state')
    with pytest.raises(ValueError, match='already exists'):
        import_workflow(live, origin._path(run), apply=True)
    assert destination.read_bytes() == b'existing user state'


@pytest.mark.parametrize('stale', ['content', 'history', 'translation', 'source'])
def test_stale_or_different_publication_is_not_imported(tmp_path, stale):
    origin, live, run, surfaces, _ = setup(tmp_path)
    if stale == 'content':
        live.service.payload['explanations'][0]['meaning'][0]['text'] += ' changed'
    elif stale == 'history':
        live.service.payload['workflow_evidence'][0]['batches'][0]['semantic_repairs'] += 1
    elif stale == 'translation':
        surfaces['translation_revision'] = 2
    else:
        live.service.bundle['fingerprint'] = 'changed'
    with pytest.raises(ValueError):
        import_workflow(live, origin._path(run), apply=True)
    assert not live.directory.exists()


def test_registered_logical_path_survives_former_host_resolution_change(tmp_path, monkeypatch):
    origin, live, run, _, ancestor = setup(tmp_path)
    import_workflow(live, origin._path(run), apply=True)
    expected = resolve_artifact_directory(live.directory, ancestor)
    resolve = Path.resolve

    def replaced_host(path, *args, **kwargs):
        if str(path) == str(ancestor):
            return tmp_path / 'replacement-host'
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'resolve', replaced_host)
    assert resolve_artifact_directory(live.directory, ancestor) == expected
    assert live.report(run)['status'] == 'published'


def test_distinct_archived_calls_cannot_merge_when_former_hosts_resolve_together(tmp_path, monkeypatch):
    origin, live, run, _, ancestor = setup(tmp_path)
    plan = import_workflow(live, origin._path(run), apply=True)
    other = next(a['original_directory'] for a in plan['archives']
                 if a['original_directory'] != str(ancestor) and 'request.json' in a['files'])
    folders = {str(ancestor), other}
    events = []
    for folder in folders:
        request = json.loads((resolve_artifact_directory(live.directory, folder) / 'request.json').read_text('utf-8'))
        events.append({'stage': 'repair', 'transport': {'directory': folder}, 'request_sha256': digest(request)})
    batch = {'evidence': events}
    expected = live._repair_counts(copy.deepcopy(batch))
    resolve = Path.resolve

    def replaced_hosts(path, *args, **kwargs):
        return tmp_path / 'same-replacement' if str(path) in folders else resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'resolve', replaced_hosts)
    assert live._repair_counts(batch) == expected
    assert len(batch['repair_budget_audit']['durable_requests']) == 2


def test_publication_changed_during_copy_does_not_expose_default_state(tmp_path, monkeypatch):
    from pairper.explanation import workflow_archive
    origin, live, run, _, _ = setup(tmp_path)
    copytree = workflow_archive.shutil.copytree

    def changed_publication(*args, **kwargs):
        result = copytree(*args, **kwargs)
        live.service.payload['revision'] += 1
        return result

    monkeypatch.setattr(workflow_archive.shutil, 'copytree', changed_publication)
    with pytest.raises(ValueError, match='publication changed'):
        import_workflow(live, origin._path(run), apply=True)
    assert not live._path(run).exists()


def test_archive_destination_escape_fails_closed(tmp_path):
    origin, live, run, _, ancestor = setup(tmp_path)
    import_workflow(live, origin._path(run), apply=True)
    path = live.directory / INDEX
    index = json.loads(path.read_text('utf-8'))
    key = next(k for k, v in index['directories'].items() if v['original_directory'] == str(ancestor))
    index['directories'][key]['relative_directory'] = '../outside'
    path.write_text(json.dumps(index), 'utf-8')
    with pytest.raises(ArchiveEvidenceError):
        resolve_artifact_directory(live.directory, ancestor)


def test_atomic_install_never_replaces_a_racing_destination(tmp_path):
    from pairper.explanation.workflow_archive import _install_absent
    source, destination = tmp_path / 'source', tmp_path / 'destination'
    source.write_bytes(b'new')
    destination.write_bytes(b'existing')
    with pytest.raises(FileExistsError):
        _install_absent(source, destination)
    assert destination.read_bytes() == b'existing'


def test_report_inventory_verification_is_linear_in_unique_archives(tmp_path, monkeypatch):
    from pairper.explanation import workflow_archive
    origin, live, run, _, _ = setup(tmp_path)
    plan = import_workflow(live, origin._path(run), apply=True)
    calls = []
    files = workflow_archive._files

    def counted(directory):
        calls.append(str(directory))
        return files(directory)

    monkeypatch.setattr(workflow_archive, '_files', counted)
    assert live.report(run)['status'] == 'published'
    assert len(set(calls)) == len(plan['archives'])
    # report reads state twice; neither read recursively rehashes all other directories.
    assert len(calls) == 2 * len(plan['archives'])


def test_historical_empty_manifest_key_import_preserves_original_state_and_published_evidence(tmp_path):
    origin, live, run, _, _ = setup(tmp_path)
    state = origin._read(run)
    frozen = [e for e in state['batches'][0]['evidence'] if e.get('stage') == 'frozen_selection_evidence'][-1]
    assert frozen.pop('manifest_amendments') == []
    origin._save(state)
    live.service.payload['workflow_evidence'][-1]['batches'][0]['evidence'] = copy.deepcopy(state['batches'][0]['evidence'])
    raw, published = origin._path(run).read_bytes(), copy.deepcopy(live.service.payload)
    assert import_workflow(live, origin._path(run), apply=True)['status'] == 'imported'
    assert live._path(run).read_bytes() == raw
    assert live.service.payload == published


@pytest.mark.parametrize('damage', ['other_missing_key', 'nonempty_amendment', 'changed_old_evidence', 'changed_candidate'])
def test_empty_manifest_key_compatibility_does_not_allow_other_gate_changes(damage):
    from pairper.explanation.workflow_archive import _only_empty_manifest_amendment_append
    old = {'stage': 'frozen_selection_evidence', 'manifest': {'sha': 'original'}}
    original = {'batches': [{'evidence': [old], 'candidate': {'key': 'original'}}]}
    checked = copy.deepcopy(original)
    appended = dict(old, manifest_amendments=[])
    checked['batches'][0]['evidence'].append(appended)
    assert _only_empty_manifest_amendment_append(original, checked)
    if damage == 'other_missing_key':
        appended['other_key'] = []
    elif damage == 'nonempty_amendment':
        appended['manifest_amendments'] = [{'change': 'actual amendment'}]
    elif damage == 'changed_old_evidence':
        checked['batches'][0]['evidence'][0]['manifest']['sha'] = 'changed'
    else:
        checked['batches'][0]['candidate']['key'] = 'changed'
    assert not _only_empty_manifest_amendment_append(original, checked)
