"""Atomic versioned explanation drafts, isolated from source and translation stores."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class ExplanationStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = self.directory / 'explanations.db'
        with self.connect() as con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS documents (
                    paper_id TEXT NOT NULL, fingerprint TEXT NOT NULL, revision INTEGER NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(paper_id, fingerprint));
                CREATE TABLE IF NOT EXISTS history (
                    paper_id TEXT NOT NULL, fingerprint TEXT NOT NULL, revision INTEGER NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(paper_id, fingerprint, revision));
            ''')

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.db, timeout=20)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def read(self, paper_id, fingerprint, published_only=False):
        with self.connect() as con:
            if published_only:
                row = con.execute("SELECT payload FROM history WHERE paper_id=? AND fingerprint=? "
                                  "AND status='published' ORDER BY revision DESC LIMIT 1",
                                  (paper_id, fingerprint)).fetchone()
            else:
                row = con.execute('SELECT payload FROM documents WHERE paper_id=? AND fingerprint=?',
                                  (paper_id, fingerprint)).fetchone()
        return json.loads(row['payload']) if row else None

    def write(self, bundle, expected_revision=None, status='draft'):
        paper_id, fingerprint = bundle['paper_id'], bundle['fingerprint']
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT revision FROM documents WHERE paper_id=? AND fingerprint=?',
                              (paper_id, fingerprint)).fetchone()
            current = row['revision'] if row else 0
            if expected_revision is not None and expected_revision != current:
                raise ValueError('Explanation revision changed; reload before saving')
            bundle = {**bundle, 'revision': current + 1, 'status': status}
            payload = json.dumps(bundle, ensure_ascii=False)
            values = (paper_id, fingerprint, current + 1, status, payload)
            con.execute('INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?)', values)
            con.execute('INSERT INTO history VALUES (?,?,?,?,?)', values)
        return bundle
