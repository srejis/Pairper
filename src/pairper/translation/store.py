"""Atomic resumable translation drafts; original documents stay immutable.

The caller supplies a trusted current bundle from the source library. The AI
can submit translations and review records, never a replacement source bundle.
"""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .contract import Result, sha, verify_result


class TranslationStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = self.directory / 'translations.db'
        with self.connect() as con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, paper_id TEXT NOT NULL, experiment_id TEXT NOT NULL, source TEXT NOT NULL,
                    result TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS history (
                    session_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    result TEXT NOT NULL, status TEXT NOT NULL,
                    PRIMARY KEY(session_id, revision));
            ''')

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.db, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def begin(self, bundle, experiment_id='baseline'):
        if not isinstance(experiment_id, str) or not 1 <= len(experiment_id) <= 100:
            raise ValueError('A short experiment identifier is required')
        empty = {'bundle_fingerprint': bundle['fingerprint'], 'entries': [], 'figure_reviews': []}
        report = verify_result(bundle, empty)
        if not report['structurally_valid']:
            raise ValueError('; '.join(report['errors']))
        session_id = sha([bundle['fingerprint'], experiment_id])
        payload = json.dumps(empty, ensure_ascii=False)
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            existing = con.execute('SELECT id FROM sessions WHERE id=?', (session_id,)).fetchone()
            if not existing:
                con.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?)',
                            (session_id, bundle['paper_id'], experiment_id, json.dumps(bundle, ensure_ascii=False),
                             payload, 0, 'draft'))
                con.execute('INSERT INTO history VALUES (?,?,?,?)', (session_id, 0, payload, 'draft'))
        return self.read(session_id, bundle['fingerprint'])

    def read(self, session_id, current_fingerprint):
        with self.connect() as con:
            row = con.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
        if not row:
            raise ValueError('Unknown translation session')
        bundle, result = json.loads(row['source']), json.loads(row['result'])
        if bundle['fingerprint'] != current_fingerprint:
            raise ValueError('Source changed; start a new session and retain this history')
        return {'session_id': session_id, 'revision': row['revision'], 'status': row['status'],
                'experiment_id': row['experiment_id'],
                'result': result, 'report': verify_result(bundle, result)}

    def source_bundle(self, session_id):
        with self.connect() as con:
            row = con.execute('SELECT source FROM sessions WHERE id=?', (session_id,)).fetchone()
        if not row:
            raise ValueError('Unknown translation session')
        return json.loads(row['source'])

    def sessions_for_paper(self, paper_id):
        with self.connect() as con:
            rows = con.execute('SELECT id,source,result,status,revision FROM sessions WHERE paper_id=? '
                               "ORDER BY (status='published') DESC,rowid DESC", (paper_id,)).fetchall()
        sessions = []
        for row in rows:
            result = json.loads(row['result'])
            has_entries = bool(result['entries'])
            has_labels = any(label['source'] != label['target']
                             for figure in result.get('figure_reviews', []) for label in figure['labels'])
            sessions.append({'session_id': row['id'], 'source': json.loads(row['source']),
                             'has_entries': has_entries,
                             'has_display_content': has_entries or bool(result.get('math_overlays')) or has_labels,
                             'status': row['status'], 'revision': row['revision']})
        return sessions

    def save(self, session_id, current_fingerprint, expected_revision, entries=None,
             figure_reviews=None, remove_entry_keys=None, math_overlays=None, math_roles=None):
        if type(expected_revision) is not int:
            raise ValueError('An integer revision is required')
        # Validate shapes and unknown fields before combining with saved data.
        incoming = Result.model_validate({'bundle_fingerprint': current_fingerprint,
                                         'entries': [] if entries is None else entries,
                                         'figure_reviews': [] if figure_reviews is None else figure_reviews,
                                         'math_overlays': [] if math_overlays is None else math_overlays,
                                         'math_roles': [] if math_roles is None else math_roles}).model_dump()
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
            if not row or json.loads(row['source'])['fingerprint'] != current_fingerprint:
                raise ValueError('Unknown or stale translation session')
            if row['status'] != 'draft':
                raise ValueError('Published translation is immutable; start a new revision')
            if row['revision'] != expected_revision:
                raise ValueError('Translation changed; reload before saving')
            bundle, result = json.loads(row['source']), json.loads(row['result'])
            old = {sha(e['source_unit_ids']): e for e in result['entries']}
            remove = remove_entry_keys or []
            if not isinstance(remove, list) or any(not isinstance(k, str) for k in remove):
                raise ValueError('Removal keys must be strings')
            if set(remove) - old.keys():
                raise ValueError('Cannot remove an unknown translation entry')
            for key in remove:
                del old[key]
            incoming_keys = [sha(e['source_unit_ids']) for e in incoming['entries']]
            if len(set(incoming_keys)) != len(incoming_keys):
                raise ValueError('Duplicate entries in submitted batch')
            old.update(zip(incoming_keys, incoming['entries'], strict=True))
            figures = {f['block_id']: f for f in result['figure_reviews']}
            figure_ids = [f['block_id'] for f in incoming['figure_reviews']]
            if len(set(figure_ids)) != len(figure_ids):
                raise ValueError('Duplicate figure reviews in submitted batch')
            figures.update(zip(figure_ids, incoming['figure_reviews'], strict=True))
            overlays = {o['math_id']: o for o in result.get('math_overlays', [])}
            overlay_ids = [o['math_id'] for o in incoming['math_overlays']]
            roles = {(r['math_id'], r['node_index']): r for r in result.get('math_roles', [])}
            role_ids = [(r['math_id'], r['node_index']) for r in incoming['math_roles']]
            if len(set(overlay_ids)) != len(overlay_ids) or len(set(role_ids)) != len(role_ids):
                raise ValueError('Duplicate math overlay or role in submitted batch')
            overlays.update(zip(overlay_ids, incoming['math_overlays'], strict=True))
            roles.update(zip(role_ids, incoming['math_roles'], strict=True))
            result = {'bundle_fingerprint': current_fingerprint, 'entries': list(old.values()),
                      'figure_reviews': list(figures.values()), 'math_overlays': list(overlays.values()),
                      'math_roles': list(roles.values())}
            report = verify_result(bundle, result)
            if not report['structurally_valid']:
                raise ValueError('; '.join(report['errors']))
            payload = json.dumps(result, ensure_ascii=False)
            revision = row['revision'] + 1
            con.execute('UPDATE sessions SET result=?,revision=? WHERE id=?',
                        (payload, revision, session_id))
            con.execute('INSERT INTO history VALUES (?,?,?,?)',
                        (session_id, revision, payload, 'draft'))
        return self.read(session_id, current_fingerprint)

    def finalize(self, session_id, current_fingerprint, expected_revision):
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
            if (not row or json.loads(row['source'])['fingerprint'] != current_fingerprint
                    or row['revision'] != expected_revision):
                raise ValueError('Unknown or stale source/revision')
            report = verify_result(json.loads(row['source']), json.loads(row['result']), True)
            if not report['complete']:
                raise ValueError('Translation and source-grounded reviews are incomplete')
            if row['status'] == 'published':
                return {'session_id': session_id, 'revision': row['revision'], 'status': 'published'}
            revision = row['revision'] + 1
            con.execute('UPDATE sessions SET revision=?,status=? WHERE id=?',
                        (revision, 'published', session_id))
            con.execute('INSERT INTO history VALUES (?,?,?,?)',
                        (session_id, revision, row['result'], 'published'))
        return self.read(session_id, current_fingerprint)
