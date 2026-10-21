"""Import an already published workflow without rewriting scientific evidence."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from copy import deepcopy
from pathlib import Path

from .catalog import digest
from .models import Explanation

INDEX = '.archive/artifact-archives.json'


def _install_absent(source, destination):
    """Atomically install without replacement, including Windows without hardlink rights."""
    if os.name == 'nt':
        Path(source).rename(destination)  # Windows rename fails when destination exists.
    else:
        os.link(source, destination)


class ArchiveEvidenceError(ValueError):
    """Registered evidence must never silently fall back to its former location."""


def _key(path):
    # Logical evidence names must survive replacement of their former filesystem host.
    return os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(path))))


def _inside(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError('Archive path escapes its permitted root')
    return path


def _files(directory):
    result = {}
    for path in sorted(Path(directory).rglob('*')):
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise ValueError('Artifact archives cannot contain symbolic links')
        if path.is_file():
            result[path.relative_to(directory).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not result:
        raise ValueError('Referenced artifact directory is empty')
    return result


def _index(root):
    path = Path(root) / INDEX
    if not path.exists():
        return {'version': 1, 'directories': {}}
    value = json.loads(path.read_text('utf-8'))
    if value.get('version') != 1 or not isinstance(value.get('directories'), dict):
        raise ValueError('Invalid workflow artifact archive index')
    return value


def resolve_artifact_directory(workflow_directory, original):
    """An exact registered location always uses its verified archive, never fallback."""
    if not isinstance(original, (str, os.PathLike)):
        return Path(original)  # Preserve the legacy caller's missing-path handling.
    root = Path(workflow_directory).resolve()
    try:
        entry = _index(root)['directories'].get(_key(original))
        if entry is None:
            return Path(original)
        base = _inside(root / 'artifacts', root)
        directory = _inside(root / entry['relative_directory'], base)
        if not directory.is_dir() or _files(directory) != entry['files']:
            raise ValueError('Archived workflow evidence is missing or changed')
        return directory
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ArchiveEvidenceError(str(exc)) from exc


def verify_imported_archives(workflow_directory, run_id):
    """Verify immutable origin evidence, never pin the evolving current state SHA."""
    root = Path(workflow_directory).resolve()
    receipt = root / '.archive/imports' / (run_id + '.json')
    original = root / '.archive/states' / (run_id + '.json')
    if not receipt.exists() and not original.exists():
        return
    try:
        plan = json.loads(receipt.read_text('utf-8'))
        if hashlib.sha256(original.read_bytes()).hexdigest() != plan['source_state_sha256']:
            raise ValueError('Original imported state archive changed')
        index = _index(root)['directories']
        for entry in plan['archives']:
            if entry['key'] not in index:
                raise ValueError('Registered artifact location mapping is missing')
            if any(index[entry['key']][k] != entry[k]
                   for k in ('original_directory', 'relative_directory', 'files')):
                raise ValueError('Registered artifact location mapping changed')
            resolve_artifact_directory(root, entry['original_directory'])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ArchiveEvidenceError(str(exc)) from exc


def artifact_directories(value):
    """Only documented evidence-location metadata, never arbitrary string replacement."""
    found = {}

    def walk(item):
        if isinstance(item, dict):
            transport = item.get('transport')
            if isinstance(transport, dict) and isinstance(transport.get('directory'), str):
                found[_key(transport['directory'])] = transport['directory']
            if isinstance(item.get('original_evidence_dir'), str):
                found[_key(item['original_evidence_dir'])] = item['original_evidence_dir']
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return found


def _state(workflow, path):
    raw = Path(path).read_bytes()
    value = json.loads(raw)
    if workflow._path(value['run_id']).name != Path(path).name:
        raise ValueError('Workflow ID differs from its state filename')
    return raw, value


def _only_empty_manifest_amendment_append(original, checked):
    """Recognize only the historical absent-key/empty-list frozen-record difference."""
    comparison = deepcopy(checked)
    if len(original.get('batches', [])) != len(comparison.get('batches', [])):
        return False
    for before, after in zip(original['batches'], comparison['batches'], strict=True):
        previous, current = before.get('evidence', []), after.get('evidence', [])
        if previous == current:
            continue
        if len(current) != len(previous) + 1 or current[:-1] != previous:
            return False
        records = [e for e in previous if e.get('stage') == 'frozen_selection_evidence']
        appended = current[-1]
        if (not records or 'manifest_amendments' in records[-1] or
                appended.get('manifest_amendments') != [] or
                dict(records[-1], manifest_amendments=[]) != appended):
            return False
        after['evidence'] = deepcopy(previous)
    return comparison == original


def plan_import(workflow, source_state):
    """Read-only plan. The caller supplies the destination's actual service."""
    from .selection_prompts import PROMPT_VERSION
    from .service import validate

    path = Path(source_state).resolve()
    raw, state = _state(workflow, path)
    if state['status'] != 'published' or state.get('prompt_version') != PROMPT_VERSION:
        raise ValueError('Only a final published current-protocol workflow can be imported')
    bundle = workflow.service.prepare(state['paper_id'])
    if (state['source_fingerprint'] != bundle['fingerprint'] or
            state.get('supplemental_math_sha256') != (bundle.get('supplemental_math') or {}).get('sha256')):
        raise ValueError('Workflow source or supplemental inventory is stale')
    saved = workflow.service.store.read(state['paper_id'], bundle['fingerprint'], published_only=False)
    if saved is None:
        raise ValueError('Actual current published document is absent')
    if (saved.get('status') != 'published' or saved.get('fingerprint') != bundle['fingerprint'] or
            saved.get('source_identity') != bundle['source_identity']):
        raise ValueError('The actual current document must already be published for this source')
    checked = deepcopy(state)
    workflow._before_commit(checked, bundle)
    if checked != state and not _only_empty_manifest_amendment_append(state, checked):
        raise ValueError('Published workflow lacks its exact final gate evidence')
    targets, entries = [], []
    for batch in state['batches']:
        packet = workflow._packet(bundle, batch['block_ids'], target_ids=batch['target_ids'],
                                  include_text=batch.get('include_text', True))
        review = batch.get('review') or {}
        keys = [i['key'] for i in batch['candidate']['items']]
        if (batch['status'] != 'reviewed' or review.get('verdict') != 'pass' or review.get('issues') or
                batch.get('review_candidate_sha256') != digest(batch['candidate']) or
                batch.get('review_context_sha256') != digest(packet) or
                batch.get('review_translation_identity') != packet['translation_identity'] or
                len(keys) != len(set(keys)) or len(review.get('checked_keys', [])) != len(keys) or
                set(review.get('checked_keys', [])) != set(keys)):
            raise ValueError('Workflow has an incomplete, stale or unbound scientific review')
        for item in batch['candidate']['items']:
            target = item['target']
            targets.append(target)
            entries.append(Explanation.model_validate(dict(item['explanation'], target_id=target['id'],
                reviewed=True, evidence_refs=target['source_refs'])).model_dump(mode='json'))
    canonical_targets, canonical_entries = validate(bundle, targets, entries, require_review=True, require_complete=True)
    if ({t['id']: t for t in canonical_targets} != {t['id']: t for t in saved['targets']} or
            {e['target_id']: e for e in canonical_entries} != {e['target_id']: e for e in saved['explanations']}):
        raise ValueError('Workflow content differs from the actual published payload')
    records = [r for r in saved.get('workflow_evidence', []) if r['run_id'] == state['run_id']]
    if not records or len(records[-1]['batches']) != len(state['batches']):
        raise ValueError('Actual published workflow evidence is absent')
    for actual, batch in zip(records[-1]['batches'], state['batches'], strict=True):
        for name in ('review', 'evidence', 'review_candidate_sha256', 'review_translation_identity',
                     'review_context_sha256', 'semantic_repairs', 'structural_repairs'):
            if actual.get(name) != batch.get(name):
                raise ValueError('Actual published workflow history differs from source state')
    root = Path(workflow.directory).resolve()
    archives = []
    for key, original in sorted(artifact_directories(state).items()):
        # Already imported sources can be relocated again through their explicit map.
        folder = resolve_artifact_directory(path.parent, original)
        folder = _inside(folder, path.parent)
        relative = 'artifacts/' + hashlib.sha256(key.encode()).hexdigest()
        archives.append({'key': key, 'original_directory': original,
            'source_directory': str(folder), 'relative_directory': relative, 'files': _files(folder)})
    return {'version': 1, 'run_id': state['run_id'], 'paper_id': state['paper_id'],
        'source_fingerprint': bundle['fingerprint'], 'source_identity': bundle['source_identity'],
        'source_state': str(path), 'source_state_sha256': hashlib.sha256(raw).hexdigest(),
        'actual_revision': saved['revision'], 'actual_payload_sha256': digest(saved),
        'destination': str(workflow._path(state['run_id']).resolve()),
        'destination_root': str(root), 'archives': archives,
        'state_bytes_unchanged': True, 'raw_bytes_unchanged': True}


def import_workflow(workflow, source_state, *, apply=False):
    """Verify, archive, then atomically expose an absent default state; never publish DB data."""
    root = Path(workflow.directory).resolve()
    run_id = Path(source_state).stem
    destination = _inside(workflow._path(run_id), root)
    receipt = root / '.archive/imports' / (run_id + '.json')
    original_state = root / '.archive/states' / (run_id + '.json')
    if destination.exists():
        if receipt.is_file():
            prior = json.loads(receipt.read_text('utf-8'))
            verify_imported_archives(root, run_id)
            original = original_state.read_bytes()
            fresh = workflow.service.prepare(prior['paper_id'])
            current = workflow.service.store.read(prior['paper_id'], fresh['fingerprint'], published_only=False)
            if (_key(source_state) == _key(prior['source_state']) and
                    destination.read_bytes() == original and
                    (not Path(source_state).exists() or Path(source_state).read_bytes() == original) and
                    digest(current) == prior['actual_payload_sha256']):
                return {**prior, 'status': 'already_imported'}
        raise ValueError('Destination workflow already exists; nothing was overwritten')
    plan = plan_import(workflow, source_state)
    if not apply:
        return {**plan, 'status': 'planned'}
    from pairper.storage.library import write_json

    from .workflow import _exclusive

    root.mkdir(parents=True, exist_ok=True)
    with _exclusive(root / 'workflow-import.lock'), _exclusive(Path(source_state).with_suffix('.lock')):
        current = plan_import(workflow, source_state)
        if current != plan or destination.exists():
            raise ValueError('Import source, publication or destination changed after planning')
        index = _index(root)
        with tempfile.TemporaryDirectory(prefix='.workflow-import-', dir=root) as temporary:
            stage = Path(temporary)
            for number, archive in enumerate(plan['archives']):
                entry = {k: archive[k] for k in ('original_directory', 'relative_directory', 'files')}
                existing = index['directories'].get(archive['key'])
                if existing is not None and existing != entry:
                    raise ValueError('Artifact location conflicts with an existing archive')
                base = _inside(root / 'artifacts', root)
                final = _inside(root / archive['relative_directory'], base)
                if final.exists():
                    if _files(final) != archive['files']:
                        raise ValueError('Existing artifact bytes differ; nothing was overwritten')
                else:
                    copied = stage / str(number)
                    shutil.copytree(archive['source_directory'], copied)
                    if _files(copied) != archive['files'] or _files(archive['source_directory']) != archive['files']:
                        raise ValueError('Artifact bytes changed during import')
                    final.parent.mkdir(parents=True, exist_ok=True)
                    copied.rename(final)
                index['directories'][archive['key']] = entry
            # The default state is exposed last. A crash earlier leaves no readable run.
            (root / INDEX).parent.mkdir(parents=True, exist_ok=True)
            write_json(root / INDEX, index)
            receipt.parent.mkdir(parents=True, exist_ok=True)
            if receipt.exists():
                if json.loads(receipt.read_text('utf-8')) != plan:
                    raise ValueError('Existing import receipt conflicts')
            else:
                staged_receipt = stage / 'receipt.json'
                staged_receipt.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
                _install_absent(staged_receipt, receipt)
            raw = Path(source_state).read_bytes()
            if hashlib.sha256(raw).hexdigest() != plan['source_state_sha256']:
                raise ValueError('Workflow bytes changed before final installation')
            original_state.parent.mkdir(parents=True, exist_ok=True)
            if original_state.exists():
                if original_state.read_bytes() != raw:
                    raise ValueError('Original imported state archive conflicts')
            else:
                staged_original = stage / 'original-state.json'
                staged_original.write_bytes(raw)
                _install_absent(staged_original, original_state)
            staged_state = stage / 'state.json'
            staged_state.write_bytes(raw)
            fresh = workflow.service.prepare(plan['paper_id'])
            published = workflow.service.store.read(plan['paper_id'], fresh['fingerprint'], published_only=False)
            if (fresh['fingerprint'] != plan['source_fingerprint'] or
                    fresh['source_identity'] != plan['source_identity'] or
                    digest(published) != plan['actual_payload_sha256']):
                raise ValueError('Actual publication changed before final workflow installation')
            _install_absent(staged_state, destination)
    return {**plan, 'status': 'imported'}
