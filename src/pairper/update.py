"""Plan resumable paper updates and persist titles selected from source evidence."""

import json

from pairper.document.models import MathRun
from pairper.document.provenance import materialize_ranges
from pairper.document.text import resolved_runs
from pairper.storage.library import now
from pairper.translation.prepare import current_source
from pairper.translation.service import TranslationService


class PaperUpdates:
    def __init__(self, library, translation_directory=None):
        self.library = library
        self.translation_directory = translation_directory
        with library.connect() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS paper_titles (
                paper_id TEXT PRIMARY KEY REFERENCES papers(id),
                source_sha256 TEXT NOT NULL, view_id TEXT NOT NULL,
                title TEXT NOT NULL, evidence TEXT NOT NULL, verified_at TEXT NOT NULL
            )""")

    def title_candidates(self, paper_id):
        paper, source, document, identity = current_source(self.library, paper_id)
        spans = {s['id']: {**s, 'page': page['page']}
                 for page in source['pages'] for s in page['spans']}
        candidates = []
        for block in sorted(document.blocks, key=lambda b: b.order):
            if block.type != 'title':
                continue
            materialize_ranges(block, source)
            pieces, refs, math = [], [], False
            for run, prefix, text in resolved_runs(block.content.runs, spans, []):
                refs.extend(run.refs)
                if isinstance(run, MathRun):
                    math = True
                    pieces.append(prefix + run.latex)
                else:
                    pieces.append(prefix + text)
            candidates.append({'block_id': block.id,
                               'title': ' '.join(''.join(pieces).split()),
                               'source_refs': refs,
                               'pages': sorted({spans[r]['page'] for r in refs}),
                               'contains_math': math})
        return {'paper_id': paper_id, 'source_sha256': paper['sha256'],
                'view_id': identity['view_id'], 'current_title': paper['title'],
                'candidates': candidates,
                'instruction': 'Inspect the original title page image. Select only actual title '
                               'blocks, in reading order, including the subtitle. Do not translate '
                               'or invent a title. This candidate list is not visual verification.'}

    def confirm_title(self, paper_id, source_sha256, view_id, block_ids, image_inspected):
        if image_inspected is not True:
            raise ValueError('Inspect the actual PDF title page before confirming')
        if (not isinstance(block_ids, list) or not block_ids
                or any(not isinstance(b, str) for b in block_ids)
                or len(set(block_ids)) != len(block_ids)):
            raise ValueError('Select unique source title block IDs')
        packet = self.title_candidates(paper_id)
        if packet['source_sha256'] != source_sha256 or packet['view_id'] != view_id:
            raise ValueError('Source changed; read the current title candidates again')
        candidates = {b['block_id']: b for b in packet['candidates']}
        if any(b not in candidates for b in block_ids):
            raise ValueError('Only source title blocks may supply the library title')
        selected = [candidates[b] for b in block_ids]
        ordered = [b['block_id'] for b in packet['candidates'] if b['block_id'] in block_ids]
        if ordered != block_ids:
            raise ValueError('Title blocks must follow the source reading order')
        if any(b['contains_math'] for b in selected):
            raise ValueError('A title containing math requires a separately reviewed display policy')
        title = ' '.join(b['title'] for b in selected).strip()
        if not title or len(title) > 2000 or not all(b['source_refs'] for b in selected):
            raise ValueError('Expected a nonempty source-backed title')
        evidence = json.dumps({'blocks': selected, 'image_inspected': True}, ensure_ascii=False)
        with self.library.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            paper = con.execute('SELECT sha256,current_view_id,title FROM papers WHERE id=?',
                                (paper_id,)).fetchone()
            if paper['sha256'] != source_sha256 or paper['current_view_id'] != view_id:
                raise ValueError('Source changed before the title could be saved')
            previous = con.execute('SELECT * FROM paper_titles WHERE paper_id=?', (paper_id,)).fetchone()
            changed = paper['title'] != title
            if (previous is None or previous['source_sha256'] != source_sha256
                    or previous['view_id'] != view_id or previous['evidence'] != evidence
                    or previous['title'] != title):
                con.execute('INSERT OR REPLACE INTO paper_titles VALUES (?,?,?,?,?,?)',
                            (paper_id, source_sha256, view_id, title, evidence, now()))
            if changed:
                con.execute('UPDATE papers SET title=? WHERE id=?', (title, paper_id))
        return {'paper_id': paper_id, 'title': title, 'changed': changed, 'verified': True}

    def list_updates(self, include_complete=False, paper_id=None):
        translations = TranslationService(self.library, self.translation_directory)
        result = []
        for paper in self.library.list_papers():
            if paper_id is not None and paper['id'] != paper_id:
                continue
            errors = {}
            reconstruction = bool(paper['has_view'])
            with self.library.connect() as con:
                title = con.execute('SELECT * FROM paper_titles WHERE paper_id=?', (paper['id'],)).fetchone()
            title_done = bool(title and title['source_sha256'] == paper['sha256']
                              and title['view_id'] == paper['current_view_id']
                              and title['title'] == paper['title'])
            korean_done, session = False, None
            if reconstruction:
                try:
                    sessions = translations.sessions(paper['id'])
                    # A resumable draft is returned when no published current result exists.
                    if sessions:
                        session = translations.report(sessions[0]['session_id'])
                        korean_done = session['status'] == 'published' and session['complete']
                except (ValueError, OSError) as exc:
                    errors['korean'] = str(exc)
            needed = [name for name, done in [('reconstruction', reconstruction),
                                             ('title', title_done), ('korean', korean_done)] if not done]
            if needed or include_complete:
                result.append({'paper_id': paper['id'], 'title': paper['title'],
                               'filename': paper['filename'], 'source_sha256': paper['sha256'],
                               'view_id': paper['current_view_id'], 'needs': needed,
                               'complete': not needed, 'errors': errors,
                               'translation': {k: session[k] for k in
                                               ('session_id', 'revision', 'status', 'complete')}
                               if session else None})
        if paper_id is not None and not result:
            self.library.paper(paper_id)  # Distinguish an unknown ID from a completed target.
        return result
