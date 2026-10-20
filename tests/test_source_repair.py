"""Fresh private storage fixtures; deterministic agents are not semantic evidence."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pairper.document.models import Document
from pairper.storage.library import Library
from pairper.translation.prepare import current_source
from pairper.translation.prompts import build_prompt, project_request, response_schema, validate_protocol
from pairper.translation.source_repair import SourceRepairer, _math_location


class Agent:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.verdict = 'pass'

    def run(self, request, schema, images, directory):
        self.calls.append((copy.deepcopy(request), list(images)))
        if request['phase'] == 'source_scan':
            return copy.deepcopy(self.response)
        return {'items': [{'item_id': item['item_id'], 'verdict': self.verdict,
                           'source_compared': True, 'source_quote': item['source'],
                           'target_quote': item['target'], 'checks': ['Original PDF glyph differs; proposal retains its surrounding formula.'],
                           'issues': []} for item in request['review_items']], 'issues': [], 'notes': []}


class SourceRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library = Library(self.root / 'library')
        pdf = self.library.root / 'originals/p.pdf';pdf.write_bytes(b'fresh private original PDF fixture')
        self.digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
        source = {'sha256': self.digest, 'extractor_version': 999, 'pages': [{'page': 1, 'spans': [], 'width': 100, 'height': 100}]}
        (self.library.root / 'sources/p.json').write_text(json.dumps(source), encoding='utf-8')
        region = {'page': 1, 'bbox': [1, 1, 20, 20]}
        raw = {'paper_id': 'p', 'source_sha256': self.digest, 'blocks': [
            {'id': 'para', 'type': 'paragraph', 'order': 0, 'source_refs': ['same-ref'], 'content': {'runs': [
                {'kind': 'math', 'latex': 'x+1', 'region': region, 'source_verified': True},
                {'kind': 'math', 'latex': 'y+2', 'region': region}]}},
            {'id': 'eq', 'type': 'equation', 'order': 1, 'content': {'latex': 'a=b', 'region': region}}]}
        self.document = Document.model_validate(raw).model_dump(mode='json')
        path = self.library.root / 'views/old.json';path.write_text(json.dumps(self.document), encoding='utf-8')
        with self.library.connect() as con:
            con.execute('INSERT INTO papers VALUES (?,?,?,?,?,?,?,?,?,?)', ('p', self.digest, 'originals/p.pdf', 'p.pdf', 'Fresh', '{}', 1, 'now', 'complete', 'old'))
            con.execute('INSERT INTO views VALUES (?,?,?,?,?,?,?,?)', ('old', 'p', self.digest, '1.1', 'views/old.json', '{"valid":true}', 'now', 'now'))
        identity = current_source(self.library, 'p')[3]
        self.bundle = {'paper_id': 'p', 'fingerprint': 'fresh-fixture', **identity, 'document': self.document, 'units': [
            {'id': 'para/runs', 'block_id': 'para', 'field': 'runs', 'pages': [1], 'source_text': '⟦para/runs#M0⟧ ⟦para/runs#M1⟧',
             'math': [{'token': '⟦para/runs#M0⟧', 'latex': 'x+1'}, {'token': '⟦para/runs#M1⟧', 'latex': 'y+2'}]}]}
        self.finding = {'kind': 'math_latex', 'source_id': '⟦para/runs#M0⟧', 'current': 'x+1', 'proposed': 'z+1',
                        'reason': 'PDF glyph is z rather than x.', 'evidence_pages': [1]}
        self.agent = Agent({'findings': [self.finding], 'notes': []})
        self.repairer = SourceRepairer(self.library, self.agent, self.root / 'evidence')
        self.task = {'source_unit_ids': ['para/runs'], 'context': {'source_math': {'block:eq/latex': 'a=b'}}}
        self.render = patch('pairper.translation.source_repair.render', return_value=b'actual-original-render-fixture')
        self.render.start()

    def tearDown(self):
        self.render.stop();self.temp.cleanup()

    def scan(self):
        return self.repairer.scan(self.bundle, self.task, ['Investigate source fidelity.'], [])

    def finalize(self, job_id, finalize=False):
        self.assertTrue(finalize)
        with self.library.connect() as con:
            job = self.library.job(job_id, con)
            path = self.library.root / 'views/new.json';path.write_text(job['document'], encoding='utf-8')
            con.execute('INSERT INTO views VALUES (?,?,?,?,?,?,?,?)', ('new', 'p', self.digest, '1.1', 'views/new.json', '{"valid":true}', 'now', 'now'))
            con.execute("UPDATE papers SET current_view_id='new',status='complete' WHERE id='p'")
            con.execute("UPDATE jobs SET stage='finalized' WHERE id=?", (job_id,))
        return {'valid': True, 'view_id': 'new'}

    def test_two_fresh_calls_then_real_begin_append_new_view_and_backup_preserve_old(self):
        original = copy.deepcopy(self.bundle)
        findings = self.scan()
        with patch.object(self.library, 'validate', side_effect=self.finalize):
            result = self.repairer.repair(self.bundle, findings)
        self.assertEqual(len(self.agent.calls), 2)
        self.assertTrue(result['preserved'])
        self.assertTrue((Path(result['artifact_dir']) / 'library-before.db').is_file())
        self.assertEqual(self.bundle, original)
        saved = json.loads((self.library.root / 'views/new.json').read_text('utf-8'))
        expected = copy.deepcopy(self.document);expected['blocks'][0]['content']['runs'][0]['latex'] = 'z+1'
        self.assertEqual(saved, expected)
        self.assertEqual(json.loads((self.library.root / 'views/old.json').read_text('utf-8')), self.document)
        review = self.agent.calls[1][0]
        self.assertTrue(review['source_repair_review'])
        self.assertNotIn('reason', json.dumps(review))
        self.assertTrue(result['requires_new_empty_translation_experiment'])

    def test_context_math_outside_primary_is_available_and_source_images_are_regenerated(self):
        self.scan()
        request, images = self.agent.calls[0]
        self.assertIn('block:eq/latex', request['source_math'])
        self.assertTrue(Path(images[0]).is_file())
        self.assertEqual(request['original_page_images'][0]['page'], 1)
        projected = project_request(request)
        self.assertEqual(projected['source_math'], request['source_math'])
        self.assertIn('authorial defect', build_prompt(request))

    def test_note_named_math_limits_crops_but_preserves_surrounding_source_context(self):
        self.repairer.scan(self.bundle, self.task, ['Compare ⟦para/runs#M0⟧ against its original glyph.'], [])
        request, images = self.agent.calls[0]
        self.assertEqual(set(request['source_math']), {'⟦para/runs#M0⟧'})
        self.assertEqual(len(images), 2)  # One original full page plus one exact crop.
        self.assertEqual(request['context'], self.task['context'])

    def test_scan_resolves_actual_superscript_text_and_generation_keeps_reference_role(self):
        source_path = self.library.root / 'sources/p.json'
        source = json.loads(source_path.read_text('utf-8'))
        source['pages'][0]['spans'] = [{'id': 'footnote', 'text': '1', 'bbox': [20, 1, 22, 7]}]
        source_path.write_text(json.dumps(source), encoding='utf-8')
        raw = copy.deepcopy(self.document)
        raw['blocks'][0]['content']['runs'].append({'kind': 'text', 'refs': ['footnote'], 'superscript': True})
        self.bundle['document'] = Document.model_validate(raw).model_dump(mode='json')
        (self.library.root / 'views/old.json').write_text(json.dumps(self.bundle['document']), encoding='utf-8')
        self.bundle.update(current_source(self.library, 'p')[3])
        unit = copy.deepcopy(self.bundle['units'][0]);unit['source_runs'] = self.bundle['document']['blocks'][0]['content']['runs']
        self.task['units'] = [unit]
        self.scan()
        request = self.agent.calls[0][0]
        evidence = request['source_run_evidence'][0]['runs'][-1]
        self.assertEqual(evidence['text'], '1');self.assertTrue(evidence['superscript'])
        projected = project_request(request)
        self.assertEqual(projected['source_run_evidence'], request['source_run_evidence'])
        self.assertTrue(projected['task']['units'][0]['source_runs'][-1]['superscript'])
        generation = project_request({'phase': 'generate', 'kind': 'text', 'task': self.task})
        marked = generation['task']['units'][0]['source_runs']
        self.assertEqual(len(marked), 1);self.assertEqual(marked[0]['run_index'], 2)
        self.assertEqual(marked[0]['refs'], ['footnote']);self.assertTrue(marked[0]['superscript'])
        prompt = build_prompt(request)
        self.assertIn('ONLY the concrete suspected information loss', prompt)
        self.assertIn('token, argument, index, bracket, reference-role', prompt)
        self.assertIn('information-preserving presentation differences', prompt)

    def test_unresolved_nonmath_transcription_blocks_without_reviewer_or_db_write(self):
        self.agent.response['findings'] = [{**self.finding, 'kind': 'unresolved', 'source_id': 'para/runs',
            'current': 'small outside math', 'proposed': '', 'reason': 'Requires moving adjacent text; not a LaTeX-only correction.'}]
        findings = self.scan()
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            self.repairer.repair(self.bundle, findings)
        self.assertEqual(len(self.agent.calls), 1)
        self.assertEqual(self.library.paper('p')['current_view_id'], 'old')

    def test_manual_proposal_injection_is_rejected_before_reviewer(self):
        findings = self.scan()
        tampered = findings[0].model_copy(update={'proposed': 'invented'})
        with self.assertRaisesRegex(ValueError, 'exact findings'):
            self.repairer.repair(self.bundle, [tampered])
        self.assertEqual(len(self.agent.calls), 1)

    def test_stale_bundle_and_unknown_current_or_unreferenced_page_proposals_rejected(self):
        for change in ({'source_id': 'invented'}, {'current': 'wrong'}, {'evidence_pages': [2]}, {'proposed': 'x+1'}):
            self.agent.response['findings'] = [{**self.finding, **change}]
            with self.assertRaises(ValueError):
                self.scan()
        stale = {**self.bundle, 'view_sha256': 'wrong'}
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.repairer.scan(stale, self.task, [], [])

    def test_review_nonpass_or_fake_quote_never_begins_build(self):
        findings = self.scan();self.agent.verdict = 'unresolved'
        with self.assertRaisesRegex(ValueError, 'did not pass'):
            self.repairer.repair(self.bundle, findings)
        with self.library.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_active_build_blocks_rather_than_resuming(self):
        findings = self.scan()
        self.library.begin('p', self.digest, revise_existing=True)
        with self.assertRaisesRegex(ValueError, 'completed source reconstruction|active source build'):
            self.repairer.repair(self.bundle, findings)
        self.assertEqual(self.library.paper('p')['current_view_id'], 'old')

    def test_all_run_fields_and_standalone_map_to_exact_nth_math_only(self):
        for field in ('runs', 'items/0', 'cells/0', 'code_lines/0'):
            bundle = copy.deepcopy(self.bundle);unit = bundle['units'][0];unit['field'] = field
            runs = bundle['document']['blocks'][0]['content']['runs']
            if field == 'items/0':
                bundle['document']['blocks'][0]['content']['items'] = [runs]
            elif field == 'cells/0':
                bundle['document']['blocks'][0]['content']['cells'] = [{'runs': runs}]
            elif field == 'code_lines/0':
                bundle['document']['blocks'][0]['content']['code_lines'] = [{'runs': runs}]
            self.assertIs(_math_location(bundle, unit['math'][1]['token'])[1], runs[1])
        self.assertEqual(_math_location(self.bundle, 'block:eq/latex')[1]['latex'], 'a=b')

    def test_multiple_same_unit_and_standalone_findings_apply_without_stale_redundant_math(self):
        second = {**self.finding, 'source_id': '⟦para/runs#M1⟧', 'current': 'y+2', 'proposed': 'w+2'}
        equation = {**self.finding, 'source_id': 'block:eq/latex', 'current': 'a=b', 'proposed': 'a=c'}
        self.agent.response['findings'] += [second, equation]
        findings = self.scan()
        with patch.object(self.library, 'validate', side_effect=self.finalize):
            self.repairer.repair(self.bundle, findings)
        saved = json.loads((self.library.root / 'views/new.json').read_text('utf-8'))
        self.assertEqual([r['latex'] for r in saved['blocks'][0]['content']['runs']], ['z+1', 'w+2'])
        self.assertEqual(saved['blocks'][1]['content']['latex'], 'a=c')
        self.assertEqual(self.bundle['units'][0]['math'][0]['latex'], 'x+1')

    def test_authorial_faithful_no_findings_do_not_start_source_build(self):
        self.agent.response = {'findings': [], 'notes': ['The original itself states this; preserve authorial wording.']}
        findings = self.scan()
        self.assertEqual(findings, [])
        with self.assertRaisesRegex(ValueError, 'empty'):
            self.repairer.repair(self.bundle, findings)
        self.assertEqual(len(self.agent.calls), 1)

    def test_validation_failure_preserves_old_view_and_does_not_claim_success(self):
        findings = self.scan()
        with patch.object(self.library, 'validate', return_value={'valid': False, 'errors': ['syntactically invalid source']}):
            with self.assertRaisesRegex(ValueError, 'did not validate'):
                self.repairer.repair(self.bundle, findings)
        self.assertEqual(self.library.paper('p')['current_view_id'], 'old')
        applications = list((self.root / 'evidence').glob('repair-*/application.json'))
        self.assertEqual(len(applications), 1)
        self.assertTrue(json.loads(applications[0].read_text('utf-8'))['preserved'])

    def test_source_snapshot_migration_is_blocked_before_library_can_rewrite_it(self):
        path = self.library.root / 'sources/p.json'
        source = json.loads(path.read_text('utf-8'));source['extractor_version'] = 0
        path.write_text(json.dumps(source), encoding='utf-8')
        self.bundle.update(current_source(self.library, 'p')[3])
        findings = self.scan()
        with self.assertRaisesRegex(ValueError, 'migration'):
            self.repairer.repair(self.bundle, findings)
        self.assertEqual(json.loads(path.read_text('utf-8'))['extractor_version'], 0)
        self.assertEqual(self.library.paper('p')['status'], 'complete')

    def test_scan_and_source_review_without_images_are_protocol_errors(self):
        request = {'phase': 'source_scan', 'kind': 'math', 'source_math': {}, 'original_page_images': []}
        self.assertIn('findings', response_schema(request)['properties'])
        with self.assertRaisesRegex(ValueError, 'images'):
            validate_protocol(request, {'findings': [], 'notes': []})
        with self.assertRaisesRegex(ValueError, 'images'):
            validate_protocol({'phase': 'review', 'kind': 'math', 'source_repair_review': True}, {})


if __name__ == '__main__':
    unittest.main()

