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
from pairper.translation.source_repair import SourceRepairer, SourceRepairBlocked, _math_location
from pairper.translation.source_run_repair import canonical_run_json


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
        import pymupdf
        pix = pymupdf.Pixmap(pymupdf.csRGB, 40, 40, bytes([255]) * 40 * 40 * 3, False)
        self.label_render = patch('pairper.translation.source_figure_evidence.render_label_evidence',
                                  return_value=pix.tobytes('png'))
        self.label_render.start()

    def tearDown(self):
        self.render.stop();self.label_render.stop();self.temp.cleanup()

    def scan(self):
        return self.repairer.scan(self.bundle, self.task, ['Investigate source fidelity.'], [])

    def revision_agent(self, scans, verdicts):
        class RevisingAgent(Agent):
            def __init__(self):
                super().__init__({})
                self.scan_count = self.review_count = 0
            def run(agent, request, schema, images, directory):
                if request['phase'] == 'source_scan':
                    agent.response = copy.deepcopy(scans[min(agent.scan_count,len(scans)-1)])
                    agent.scan_count += 1
                else:
                    requested = verdicts[min(agent.review_count,len(verdicts)-1)]
                    agent.verdict = 'repair' if requested == 'mixed' else requested
                    agent.review_count += 1
                response = super().run(request,schema,images,directory)
                if request['phase'] != 'source_scan' and agent.verdict != 'pass':
                    response['items'][0]['issues'] = ['EXACT_SOURCE_REVIEW_FEEDBACK: retained symbol differs from original.']
                    if requested == 'mixed':
                        for item in response['items'][1:]:
                            item['verdict'] = 'pass'
                return response
        self.agent = RevisingAgent()
        self.repairer.runner = self.agent
        return self.agent

    def test_review_repair_revises_complete_unapplied_batch_then_fresh_review_applies_once(self):
        revised = {**self.finding,'proposed':'w+1','reason':'SCANNER_REASON_NOT_REVIEW_EVIDENCE'}
        self.revision_agent([{'findings':[self.finding],'notes':[]},
                             {'findings':[revised],'notes':[]}], ['repair','pass'])
        findings = self.scan()
        with patch.object(self.library,'begin',wraps=self.library.begin) as begin, \
             patch.object(self.library,'validate',side_effect=self.finalize) as validate:
            result = self.repairer.repair(self.bundle,findings)
        self.assertEqual(begin.call_count,1);self.assertEqual(validate.call_count,1)
        self.assertEqual(self.agent.scan_count,2);self.assertEqual(self.agent.review_count,2)
        rescan = self.agent.calls[2][0]
        feedback = json.loads(rescan['notes_to_investigate'][-1])
        self.assertTrue(feedback['previous_findings_are_UNAPPLIED'])
        self.assertEqual(feedback['previous_unapplied_findings'],[self.finding])
        self.assertEqual(feedback['required_source_issue_ids'],[self.finding['source_id']])
        self.assertIn('EXACT_SOURCE_REVIEW_FEEDBACK',json.dumps(feedback['independent_review_feedback']))
        fresh = self.agent.calls[3][0]
        self.assertNotIn('EXACT_SOURCE_REVIEW_FEEDBACK',build_prompt(fresh))
        self.assertNotIn('SCANNER_REASON_NOT_REVIEW_EVIDENCE',build_prompt(fresh))
        self.assertNotIn('PROGRAM_SOURCE_PROPOSAL_REVISION',build_prompt(fresh))
        saved = json.loads((self.library.root/'views/new.json').read_text(encoding='utf8'))
        self.assertEqual(saved['blocks'][0]['content']['runs'][0]['latex'],'w+1')
        chain = json.loads(Path(result['source_proposal_chain']).read_text(encoding='utf8'))
        self.assertEqual(chain['status'],'applied');self.assertEqual(len(chain['attempts']),2)
        self.assertEqual(hashlib.sha256(Path(result['source_proposal_chain']).read_bytes()).hexdigest(),
                         result['source_proposal_chain_sha256'])

    def test_source_revision_exhaustion_is_typed_and_never_begins_library_build(self):
        self.revision_agent([{'findings':[self.finding],'notes':[]}],['repair'])
        findings = self.scan()
        with patch.object(self.library,'begin',wraps=self.library.begin) as begin:
            with self.assertRaises(SourceRepairBlocked) as failure:
                self.repairer.repair(self.bundle,findings)
        begin.assert_not_called()
        self.assertEqual((self.agent.scan_count,self.agent.review_count),(3,3))
        self.assertEqual(failure.exception.details['proposal_attempts'],3)
        self.assertEqual(self.library.paper('p')['current_view_id'],'old')
        self.assertEqual(list((self.root/'evidence').rglob('application.json')),[])

    def test_dropped_empty_unresolved_invalid_or_stale_revisions_remain_unapplied(self):
        second={**self.finding,'source_id':'⟦para/runs#M1⟧','current':'y+2','proposed':'w+2'}
        variants=[[],[self.finding],
                  [self.finding,{**second,'kind':'unresolved'}],
                  [self.finding,{**second,'current':'not-current'}],
                  [self.finding,{**second,'source_id':'⟦unknown/runs#M1⟧'}]]
        for revised in variants:
            with self.subTest(revised=revised):
                self.revision_agent([{'findings':[self.finding,second],'notes':[]},
                                     {'findings':revised,'notes':[]}],['mixed'])
                findings=self.scan()
                with patch.object(self.library,'begin',wraps=self.library.begin) as begin:
                    with self.assertRaises(SourceRepairBlocked):
                        self.repairer.repair(self.bundle,findings)
                begin.assert_not_called()
                self.assertEqual(self.agent.review_count,1)
                self.assertEqual(self.library.paper('p')['current_view_id'],'old')

    def test_current_source_change_during_revision_is_blocked_before_any_apply(self):
        self.revision_agent([{'findings':[self.finding],'notes':[]}],['repair'])
        findings = self.scan()
        actual_scan = self.repairer.scan
        def changed_source(*args,**kwargs):
            path = self.library.root/'views/old.json'
            changed = json.loads(path.read_text(encoding='utf8'))
            changed['blocks'][0]['content']['runs'][0]['latex'] = 'external-source-change'
            path.write_text(json.dumps(changed),encoding='utf8')
            return actual_scan(*args,**kwargs)
        with patch.object(self.repairer,'scan',side_effect=changed_source), \
             patch.object(self.library,'begin',wraps=self.library.begin) as begin:
            with self.assertRaisesRegex(SourceRepairBlocked,'Source reconstruction changed'):
                self.repairer.repair(self.bundle,findings)
        begin.assert_not_called()
        self.assertEqual(self.library.paper('p')['current_view_id'],'old')

    def run_group_fixture(self):
        source_path = self.library.root / 'sources/p.json'
        source = json.loads(source_path.read_text('utf-8'))
        source['pages'][0]['spans'] = [
            {'id': 'r1', 'text': 'D', 'bbox': [1, 1, 3, 3]},
            {'id': 'r2', 'text': 'small', 'bbox': [3, 2, 10, 4]},
            {'id': 'r3', 'text': 'Y', 'bbox': [11, 1, 14, 4]}]
        source_path.write_text(json.dumps(source), encoding='utf-8')
        raw = copy.deepcopy(self.document)
        raw['blocks'][0]['source_refs'] = ['r1', 'r2', 'r3']
        raw['blocks'][0]['content']['runs'] = [
            {'kind': 'math', 'latex': 'D', 'refs': ['r1'], 'source_verified': True,
             'region': {'page': 1, 'bbox': [1, 1, 3, 3]}},
            {'kind': 'text', 'refs': ['r2'], 'join': 'none', 'before': 'none'},
            {'kind': 'math', 'latex': 'Y', 'refs': ['r3'], 'source_verified': True,
             'region': {'page': 1, 'bbox': [11, 1, 14, 4]}}]
        self.bundle['document'] = Document.model_validate(raw).model_dump(mode='json')
        self.bundle['units'][0]['math'] = [{'token': '⟦para/runs#M0⟧', 'latex': 'D'}, {'token': '⟦para/runs#M1⟧', 'latex': 'Y'}]
        self.bundle['units'][0]['source_refs'] = ['r1', 'r2', 'r3']
        self.bundle['units'][0]['source_text'] = '⟦para/runs#M0⟧small ⟦para/runs#M1⟧'
        (self.library.root / 'views/old.json').write_text(json.dumps(self.bundle['document']), encoding='utf-8')
        self.bundle.update(current_source(self.library, 'p')[3])
        runs = self.bundle['document']['blocks'][0]['content']['runs']
        proposal = {**runs[0], 'latex': 'D_{small}', 'refs': ['r1', 'r2'], 'region': {'page': 1, 'bbox': [1, 1, 10, 4]}}
        return {'kind': 'run_group', 'source_id': 'para/runs/range/0:2',
                'current': canonical_run_json(runs[:2]), 'proposed': canonical_run_json([proposal]),
                'reason': 'PDF subscript was separated from its symbol into an adjacent text run.', 'evidence_pages': [1]}

    def test_run_group_scan_fresh_review_normalized_arrays_and_new_view_preserve_refs(self):
        finding = self.run_group_fixture();self.agent.response['findings'] = [finding]
        findings = self.scan()
        with patch.object(self.library, 'validate', side_effect=self.finalize):
            result = self.repairer.repair(self.bundle, findings)
        saved = json.loads((self.library.root / 'views/new.json').read_text('utf-8'))
        runs = saved['blocks'][0]['content']['runs']
        self.assertEqual(len(runs), 2);self.assertEqual(runs[0]['latex'], 'D_{small}')
        self.assertEqual([ref for run in runs for ref in run['refs']], ['r1', 'r2', 'r3'])
        self.assertEqual(len(self.agent.calls), 2);self.assertTrue(result['preserved'])
        scan_request = self.agent.calls[0][0]
        evidence = scan_request['source_run_evidence'][0]
        self.assertEqual(evidence['original_run_indices'], [0, 1, 2])
        self.assertEqual(json.loads(evidence['current_raw_runs_json'])[1]['refs'], ['r2'])
        review = self.agent.calls[1][0]
        self.assertEqual(json.loads(review['review_items'][0]['target'])[0]['latex'], 'D_{small}')
        self.assertTrue(any('range/0:2' in page.get('math_id', '') for page in review['original_page_images']))

    def test_disjoint_math_and_group_are_safe_but_overlap_is_rejected_before_review(self):
        group = self.run_group_fixture()
        math = {**self.finding, 'source_id': '⟦para/runs#M1⟧', 'current': 'Y', 'proposed': 'Z'}
        self.agent.response['findings'] = [group, math]
        findings = self.scan()
        with patch.object(self.library, 'validate', side_effect=self.finalize):
            self.repairer.repair(self.bundle, findings)
        saved = json.loads((self.library.root / 'views/new.json').read_text('utf-8'))
        self.assertEqual([r['latex'] for r in saved['blocks'][0]['content']['runs']], ['D_{small}', 'Z'])

    def boundary_fixture(self):
        snapshot_path=self.library.root/'sources/p.json'
        snapshot=json.loads(snapshot_path.read_text(encoding='utf8'))
        snapshot['pages'][0]['spans']=[
            {'id':'eqref','text':'a=(b','bbox':[10,10,34,20],'order':0},
            {'id':'close','text':')','bbox':[35,10,37,20],'order':1},
            {'id':'prose','text':'where','bbox':[10,25,34,35],'order':2},
            {'id':'mu','text':'μ','bbox':[35,25,40,35],'order':3}]
        snapshot_path.write_text(json.dumps(snapshot),encoding='utf8')
        raw={'paper_id':'p','source_sha256':self.digest,'blocks':[
            {'id':'eq','type':'equation','order':0,'source_refs':['eqref'],
             'content':{'latex':'a=(b','region':{'page':1,'bbox':[10,10,35,20]},'source_verified':True}},
            {'id':'para','type':'paragraph','order':1,'source_refs':['close','prose','mu'],
             'content':{'runs':[
                 {'kind':'math','latex':')','refs':['close'],'region':{'page':1,'bbox':[35,10,37,20]},'source_verified':True},
                 {'kind':'text','refs':['prose']},
                 {'kind':'math','latex':'\\mu','refs':['mu'],'region':{'page':1,'bbox':[35,25,40,35]},'source_verified':True}]}}]}
        self.bundle['document']=Document.model_validate(raw).model_dump(mode='json')
        self.document=copy.deepcopy(self.bundle['document'])
        (self.library.root/'views/old.json').write_text(json.dumps(self.document),encoding='utf8')
        self.bundle.update(current_source(self.library,'p')[3])
        self.bundle['units']=[{'id':'para/runs','block_id':'para','field':'runs','pages':[1],
            'source_text':'⟦para/runs#M0⟧ where ⟦para/runs#M1⟧',
            'source_refs':['close','prose','mu'],
            'math':[{'token':'⟦para/runs#M0⟧','latex':')','refs':['close']},
                    {'token':'⟦para/runs#M1⟧','latex':'\\mu','refs':['mu']}]}]
        self.task={'source_unit_ids':['para/runs'],'context':{'source_math':{'block:eq/latex':'a=(b'}}}
        current={'equation_latex':'a=(b','donor_unit_id':'para/runs','start':0,'end':1,
                 'donor_runs':copy.deepcopy(self.document['blocks'][1]['content']['runs'][:1])}
        return {'kind':'math_boundary_transfer','source_id':'block:eq/latex',
                'current':json.dumps(current),'proposed':json.dumps({'insert_offset':4,'equation_latex':'a=(b)'}),
                'reason':'Original PDF closing delimiter belongs to preceding equation.','evidence_pages':[1]}

    def test_boundary_scan_fresh_wholepair_review_then_atomic_new_view(self):
        finding=self.boundary_fixture()
        self.agent.response={'findings':[finding],'notes':[]}
        before=copy.deepcopy(self.bundle)
        findings=self.scan()
        with patch.object(self.library,'validate',side_effect=self.finalize):
            result=self.repairer.repair(self.bundle,findings)
        review=self.agent.calls[1][0]
        old=json.loads(review['review_items'][0]['source'])
        new=json.loads(review['review_items'][0]['target'])
        self.assertEqual(old['donor'],before['document']['blocks'][1])
        self.assertEqual(new['receiver']['content']['latex'],'a=(b)')
        self.assertEqual(new['receiver']['content']['region']['bbox'],[10,10,37,20])
        self.assertTrue(any('@boundary-union' in item.get('math_id','') for item in review['original_page_images']))
        saved=json.loads((self.library.root/'views/new.json').read_text(encoding='utf8'))
        self.assertEqual(saved['blocks'],[new['receiver'],new['donor']])
        self.assertEqual(saved['blocks'][1]['content']['runs'],before['document']['blocks'][1]['content']['runs'][1:])
        self.assertEqual(saved['blocks'][0]['source_refs'],['eqref','close'])
        self.assertEqual(saved['blocks'][1]['source_refs'],['prose','mu'])
        self.assertTrue(result['preserved'])
        self.assertTrue(result['boundary_application_audit'])
        self.assertEqual(self.bundle,before)

    def test_boundary_conflicts_with_receiver_math_and_any_donor_correction_before_review(self):
        finding=self.boundary_fixture()
        for other in ({'kind':'math_latex','source_id':'block:eq/latex','current':'a=(b','proposed':'a=(c'},
                      {'kind':'math_latex','source_id':'⟦para/runs#M1⟧','current':'\\mu','proposed':'m'}):
            other.update(reason='Independent conflicting proposal.',evidence_pages=[1])
            self.agent.response={'findings':[finding,other],'notes':[]}
            with self.assertRaisesRegex(ValueError,'overlaps|Duplicate source finding'):
                self.scan()
        self.assertEqual(len(self.agent.calls),2)

    def test_boundary_repair_uses_real_full_library_validation_and_actual_pdf_renders(self):
        import pymupdf

        finding=self.boundary_fixture()
        self.render.stop();self.label_render.stop()
        pdf_path=self.library.root/'originals/p.pdf'
        with pymupdf.open() as pdf:
            page=pdf.new_page(width=100,height=100)
            page.insert_text((10,19),'a=(b',fontsize=9)
            page.insert_text((35,19),')',fontsize=9)
            page.insert_text((10,34),'where',fontsize=9)
            page.insert_text((35,34),'m',fontsize=9)
            pdf.save(pdf_path)
        self.digest=hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        source_path=self.library.root/'sources/p.json'
        snapshot=json.loads(source_path.read_text(encoding='utf8'))
        snapshot.update(sha256=self.digest,extractor_version=7)
        snapshot['pages'][0].update(images=[],drawings=[])
        source_path.write_text(json.dumps(snapshot),encoding='utf8')
        self.bundle['document']['source_sha256']=self.digest
        self.bundle['document']['visual_review_pages']=[1]
        for block in self.bundle['document']['blocks']:
            block['validation_status']='source_verified'
        (self.library.root/'views/old.json').write_text(json.dumps(self.bundle['document']),encoding='utf8')
        with self.library.connect() as conn:
            conn.execute('UPDATE papers SET sha256=? WHERE id=?',(self.digest,'p'))
        self.bundle.update(current_source(self.library,'p')[3])
        self.agent.response={'findings':[finding],'notes':[]}
        findings=self.scan()
        try:
            result=self.repairer.repair(self.bundle,findings)
        except ValueError:
            reports=list((self.root/'evidence').rglob('application.json'))
            if reports:
                self.fail(reports[-1].read_text(encoding='utf8'))
            raise
        self.assertTrue(result['report']['valid'])
        self.assertTrue(result['report']['view_id'])
        self.assertNotEqual(result['report']['view_id'],'old')
        self.assertTrue(result['preserved'])
        self.assertTrue(result['requires_new_empty_translation_experiment'])

    def test_source_glyph_scope_prioritizes_literal_code_and_keeps_source_only_metadata(self):
        self.bundle['units']=[{'id':f'prose{i}/runs','order':i,'field':'runs','source_text':'Plain source.'}
                              for i in range(12)]+[
            {'id':'code/code_lines/0','order':99,'type':'code','field':'code_lines/0','source_text':'‘‘‘'}]
        evidence={'available':True,'units':[{'unit_id':'code/code_lines/0','characters':[8216,8216,8216]}]}
        with patch('pairper.translation.source_repair.build_source_glyph_evidence',return_value=evidence) as build:
            self.assertEqual(self.repairer._glyph_evidence(self.bundle,{u['id'] for u in self.bundle['units']}),evidence)
        self.assertEqual(build.call_args.args[1][0],'code/code_lines/0')
        self.assertEqual(build.call_args.kwargs['pdf_path'],self.library.root/'originals/p.pdf')
        self.assertEqual(build.call_args.kwargs['view_path'],self.library.root/'views/old.json')

    def test_boundary_reviewer_rejection_cannot_create_a_build_or_change_view(self):
        self.agent.response={'findings':[self.boundary_fixture()],'notes':[]}
        self.agent.verdict='unresolved'
        findings=self.scan()
        with patch.object(self.library,'begin',wraps=self.library.begin) as begin:
            with self.assertRaisesRegex(ValueError,'reviewer did not pass'):
                self.repairer.repair(self.bundle,findings)
            begin.assert_not_called()
        self.assertEqual(self.library.paper('p')['current_view_id'],'old')

    def test_group_overlapping_math_finding_is_rejected_before_reviewer(self):
        group = self.run_group_fixture()
        self.agent.response['findings'] = [group, {**self.finding, 'source_id': '⟦para/runs#M0⟧', 'current': 'D', 'proposed': 'E'}]
        with self.assertRaisesRegex(ValueError, 'overlaps'):
            self.scan()
        self.assertEqual(len(self.agent.calls), 1)

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

    def figure_only_fixture(self):
        raw=copy.deepcopy(self.bundle['document'])
        region={'page':1,'bbox':[1,1,20,20],'rotation':0}
        raw['blocks'].append({'id':'f','type':'figure','order':2,'content':{'region':region}})
        self.bundle['document']=Document.model_validate(raw).model_dump(mode='json')
        (self.library.root/'views/old.json').write_text(json.dumps(self.bundle['document']),encoding='utf8')
        self.bundle.update(current_source(self.library,'p')[3])
        self.bundle['figures']=[{'block_id':'f','paper_id':'p','region':region}]
        self.bundle['figure_source_spans']={'f':[]}
        self.task={'task_id':'figure-only','paper_id':'p','kind':'figure','source_unit_ids':[],
                   'figure':{'paper_id':'p','block_id':'f','region':region},'context':{}}
        self.agent.response={'findings':[],'notes':['Inspected original figure; no source discrepancy.']}

    def test_figure_only_scan_derives_pdf_page_and_crop_without_text_or_math_units(self):
        self.figure_only_fixture()
        self.assertNotIn('pages',self.task)
        self.assertEqual(self.scan(),[])
        request,images=self.agent.calls[0]
        manifest=request['original_page_images']
        self.assertEqual(len(images),2)
        self.assertEqual({i['page'] for i in manifest},{1})
        self.assertTrue(any(i.get('block_id')=='f' for i in manifest))
        self.assertEqual(request['source_math'],{})
        request['attached_images']=[{'path':i['path'],'sha256':i['sha256']} for i in manifest]
        projected=project_request(request)
        self.assertEqual([i['role'] for i in projected['attached_images']],['original_PDF_page','original_figure'])
        self.assertEqual(projected['attached_images'][1]['block_id'],'f')
        self.assertEqual(self.library.paper('p')['current_view_id'],'old')

    def test_figure_page_cannot_be_borrowed_from_unknown_or_stale_region(self):
        self.figure_only_fixture()
        for figure in ({**self.task['figure'],'block_id':'foreign'},
                       {**self.task['figure'],'region':{'page':1,'bbox':[2,2,10,10]}},
                       {**self.task['figure'],'paper_id':'another-paper'}):
            task={**self.task,'figure':figure}
            with self.assertRaises(ValueError):
                self.repairer.scan(self.bundle,task,['Inspect the figure source.'],[])
        self.assertEqual(self.agent.calls,[])

    def test_actual_pdf_source_scan_renders_adaptive_label_above_shared_render_cap(self):
        import pymupdf
        from pairper.pdf.source import extract

        self.render.stop()  # Exercise both production renderers, no raster stub.
        self.label_render.stop()
        pdf_path=self.library.root/'originals/p.pdf'
        pdf=pymupdf.open()
        page=pdf.new_page(width=100,height=100)
        page.insert_text((4,5),'12500',fontsize=0.62384)
        pdf.save(pdf_path)
        pdf.close()
        digest=hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        source_path=self.library.root/'sources/p.json'
        source=extract(pdf_path, digest)
        source_path.write_text(json.dumps(source),encoding='utf8')
        self.bundle['document']['source_sha256']=digest
        (self.library.root/'views/old.json').write_text(json.dumps(self.bundle['document']),encoding='utf8')
        with self.library.connect() as conn:
            conn.execute('UPDATE papers SET sha256=? WHERE id=?',(digest,'p'))
        self.bundle.update(current_source(self.library,'p')[3])
        self.figure_only_fixture()
        span = source['pages'][0]['spans'][0]
        self.bundle['figure_source_spans'] = {'f': [{**span, 'page': 1}]}
        next(b for b in self.bundle['document']['blocks'] if b['id'] == 'f')['source_refs'] = [span['id']]
        (self.library.root/'views/old.json').write_text(json.dumps(self.bundle['document']), encoding='utf8')
        self.bundle.update(current_source(self.library, 'p')[3])
        self.task['current_translation_candidate']={'figures':[{'block_id':'f','labels':[
            {'source':'12500','target':'12500','source_refs':[span['id']],'bbox':[]}]}]}
        self.task['candidate_to_inspect']={'items':[{'item_id':'f@label0','verdict':'unresolved'}]}
        before=hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        self.assertEqual(self.scan(),[])
        request,images=self.agent.calls[0]
        native = request['figure_label_glyph_evidence']['labels'][0]['refs'][0]
        self.assertEqual(native['native_pdf_text'], '12500')
        self.assertTrue(native['native_span_exact'])
        self.assertFalse(request['figure_label_glyph_evidence']['full_visual_visibility_verified'])
        labels=request['figure_label_evidence']['rendered_images']
        self.assertEqual(len(labels),1)
        self.assertGreater(labels[0]['scale'],4)
        self.assertIn(labels[0]['path'],images)
        for item in request['original_page_images']:
            if 'path' not in item:
                continue
            raster=Path(item['path']).read_bytes()
            self.assertTrue(raster.startswith(b'\x89PNG\r\n\x1a\n'))
            self.assertEqual(item['sha256'],hashlib.sha256(raster).hexdigest())
        pix=pymupdf.Pixmap(labels[0]['path'])
        self.assertLessEqual(max(pix.width,pix.height),2048)
        self.assertLessEqual(pix.width*pix.height,4_000_000)
        self.assertEqual(before,hashlib.sha256(pdf_path.read_bytes()).hexdigest())

    def test_figure_auxiliary_ocr_candidate_is_supplied_without_auto_discarding_findings(self):
        self.figure_only_fixture()
        candidate={'figures':[{'block_id':'f','image_inspected':True,'labels_complete':True,
                    'labels':[{'source':'Layer Num.','target':'층 번호','source_refs':[], 'bbox':[3,3,8,5]}],
                    'unresolved_errors':[],'no_translatable_text_reason':''}],
                   'notes':['Candidate is unapproved.'],'source_concerns':['Auxiliary exponent5 lacks its original minus.'],'issues':[]}
        self.task['figure_source_spans']=[{'id':'tick5','text':'5','page':1,'bbox':[1,1,2,2]}]
        trigger={'items':[{'item_id':'f','verdict':'unresolved'}],
                 'notes':['Reviewer claims auxiliary positive ticks are displayed.']}
        self.task['candidate_to_inspect']=trigger
        self.task['current_translation_candidate']=copy.deepcopy(candidate)
        self.task['figure_output_evidence']={'emitted_labels':[], 'raster_sha256':'preserved-original',
                                             'retained_label_count':15}
        original=copy.deepcopy(self.task)
        self.agent.response={'findings':[],'notes':['Original image retains10^-5; unused auxiliary tick span is not in the emitted glossary.']}
        self.assertEqual(self.scan(),[])
        request=self.agent.calls[0][0]
        self.assertEqual(request['candidate_to_inspect'],trigger)
        self.assertEqual(request['figure_label_evidence']['selected_labels'],1)
        label_images=request['figure_label_evidence']['rendered_images']
        self.assertEqual(len(label_images),1)
        self.assertEqual(label_images[0]['label_id'],'f@label0')
        self.assertGreaterEqual(label_images[0]['scale'],4)
        self.assertTrue(label_images[0]['pixel_identity_verified'])
        self.assertEqual(label_images[0]['crop_pixel_sha256'], label_images[0]['atlas_crop_pixel_sha256'])
        self.assertNotIn('candidate_to_inspect',request['task'])
        self.assertNotIn('candidate_to_inspect',self.repairer._scans[self.bundle['fingerprint']]['task'])
        self.assertEqual(project_request(request)['candidate_to_inspect'],trigger)
        for key in ('current_translation_candidate','figure_output_evidence'):
            self.assertEqual(request[key],original[key])
            self.assertEqual(project_request(request)[key],original[key])
            self.assertNotIn(key,request['task'])
            self.assertNotIn(key,self.repairer._scans[self.bundle['fingerprint']]['task'])
        request['candidate_to_inspect']['notes'].append('mutation')
        self.assertEqual(self.task,original)
        # A fresh scanner can still report a real output-affecting finding;
        # the host never drops figure findings merely because they are OCR.
        self.agent.response={'findings':[{'kind':'unresolved','source_id':'tick5','current':'5',
                    'proposed':'−5','reason':'Candidate label uses the erroneous positive exponent.','evidence_pages':[1]}],'notes':[]}
        self.task['current_translation_candidate']['figures'][0]['labels']=[
            {'source':'10^5','target':'10^5','source_refs':['tick5'],'bbox':[]}]
        # Keep the remaining finding test bbox-only; foreign refs are rejected
        # separately by the original-snapshot crop gate.
        self.task['current_translation_candidate']['figures'][0]['labels'][0].update(
            source_refs=[],bbox=[3,3,8,5])
        findings=self.scan()
        self.assertEqual(len(findings),1)
        with self.assertRaisesRegex(ValueError,'unresolved'):
            self.repairer.repair(self.bundle,findings)

    def test_candidate_for_source_diagnosis_never_leaks_into_source_repair_reviewer(self):
        self.task['candidate_to_inspect']={'notes':['UNAPPROVED_TRANSLATOR_CLAIM']}
        self.task['current_translation_candidate']={'notes':['CURRENT_CANDIDATE_ONLY_FOR_SCAN']}
        self.task['figure_output_evidence']={'marker':'OUTPUT_ONLY_FOR_SCAN'}
        findings=self.scan()
        with patch.object(self.library,'validate',side_effect=self.finalize):
            self.repairer.repair(self.bundle,findings)
        review=self.agent.calls[1][0]
        self.assertNotIn('candidate_to_inspect',review)
        self.assertNotIn('candidate_to_inspect',review['task'])
        self.assertNotIn('UNAPPROVED_TRANSLATOR_CLAIM',build_prompt(review))
        for key in ('current_translation_candidate','figure_output_evidence'):
            self.assertNotIn(key,review)
            self.assertNotIn(key,review['task'])

    def test_note_named_math_limits_crops_but_preserves_surrounding_source_context(self):
        self.repairer.scan(self.bundle, self.task, ['Compare ⟦para/runs#M0⟧ against its original glyph.'], [])
        request, images = self.agent.calls[0]
        self.assertEqual(set(request['source_math']), {'⟦para/runs#M0⟧', '⟦para/runs#M1⟧'})
        self.assertEqual(len(images), 3)  # Full page plus both owning-unit crops; no context equation.
        self.assertEqual(request['context'], self.task['context'])

    def test_eight_explicit_owner_notes_with_paired_shorthand_include_siblings_only(self):
        # The exact eight source ID shapes and shorthand from the observed run;
        # fixture content stays private and no real correction is performed.
        owners = ['dejavu-p31-31-3', 'dejavu-p31-31-6', 'dejavu-p31-31-10',
                  'dejavu-p31-31-13', 'dejavu-p32-32-25', 'dejavu-p33-33-13',
                  'dejavu-p33-33-20', 'dejavu-p34-34-9']
        blocks, units, findings = [], [], []
        template = self.document['blocks'][0]
        for order, owner in enumerate(owners + ['outside']):
            block = copy.deepcopy(template)
            block.update(id=owner, order=order)
            blocks.append(block)
            unit = copy.deepcopy(self.bundle['units'][0])
            uid = owner + '/runs'
            unit.update(id=uid, block_id=owner)
            unit['math'] = [{'token':f'⟦{uid}#M{i}⟧','latex':latex}
                            for i,latex in enumerate(('x+1','y+2'))]
            unit['source_text'] = ' '.join(m['token'] for m in unit['math'])
            units.append(unit)
            if owner != 'outside':
                findings.extend({'kind':'math_latex','source_id':m['token'],'current':m['latex'],
                                 'proposed':'z+1' if i == 0 else 'z+2',
                                 'reason':'Private original-image fixture discrepancy.','evidence_pages':[1]}
                                for i,m in enumerate(unit['math']))
        self.bundle['document']['blocks'] = blocks
        self.bundle['units'] = units
        (self.library.root/'views/old.json').write_text(json.dumps(self.bundle['document']),encoding='utf8')
        self.bundle.update(current_source(self.library,'p')[3])
        task = {'source_unit_ids':[u['id'] for u in units], 'context':{}}
        notes = [owner+'/runs#M0 및 #M1: compare the two original symbols.' for owner in owners]
        self.agent.response = {'findings':findings,'notes':[]}
        result = self.repairer.scan(self.bundle,task,notes,[])
        request,images = self.agent.calls[0]
        self.assertEqual(len(result),16)
        self.assertEqual(set(request['source_math']),{f['source_id'] for f in findings})
        self.assertEqual(len(images),17)
        self.assertEqual({r['unit_id'] for r in request['source_run_evidence']},{o+'/runs' for o in owners})
        self.assertFalse(any('outside' in key for key in request['source_math']))

    def test_bare_ambiguous_shorthand_does_not_invent_an_owner_for_standalone_equation(self):
        self.agent.response = {'findings':[],'notes':[]}
        self.repairer.scan(self.bundle,self.task,['Inspect block:eq/latex and #M1.'],[])
        self.assertEqual(self.agent.calls[0][0]['source_math'],{'block:eq/latex':'a=b'})

    def test_owner_expansion_keeps_unknown_outside_stale_and_unchanged_proposals_rejected(self):
        for source_id,current,proposed in [('block:eq/latex','a=b','a=c'),
                ('⟦unknown/runs#M1⟧','y+2','z+2'),('⟦para/runs#M1⟧','wrong','z+2'),
                ('⟦para/runs#M1⟧','y+2','y+2')]:
            with self.subTest(source_id=source_id,current=current,proposed=proposed):
                self.agent.response={'findings':[{'kind':'math_latex','source_id':source_id,
                    'current':current,'proposed':proposed,'reason':'Fixture proposal.','evidence_pages':[1]}],'notes':[]}
                with self.assertRaisesRegex(ValueError,'bind exact current math'):
                    self.repairer.scan(self.bundle,self.task,['Inspect para/runs#M0 and #M1.'],[])

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

    def test_scan_feedback_preserves_notes_and_manifest_is_detached_and_bundle_bound(self):
        self.assertIsNone(self.repairer.get_scan_feedback(self.bundle))
        self.agent.response = {'findings': [], 'notes': ['Original image literally contains the unusual source ordering.']}
        self.scan()
        feedback = self.repairer.get_scan_feedback(self.bundle)
        self.assertEqual(feedback['bundle_fingerprint'], self.bundle['fingerprint'])
        self.assertEqual(feedback['notes'], self.agent.response['notes'])
        self.assertEqual(feedback['findings'], [])
        self.assertTrue((Path(feedback['artifact_dir'])/'scan.json').is_file())
        self.assertTrue(self.repairer._scans[self.bundle['fingerprint']]['manifest'])
        feedback['notes'].append('caller mutation');feedback['findings'].append({'injected':'proposal'})
        self.assertNotIn('caller mutation',self.repairer.get_scan_feedback(self.bundle)['notes'])
        self.assertEqual(self.repairer.get_scan_feedback(self.bundle)['findings'], [])
        self.assertIsNone(self.repairer.get_scan_feedback({**self.bundle,'fingerprint':'different-source'}))
        self.assertEqual(len(self.agent.calls), 1)

    def test_feedback_task_identity_prevents_reusing_other_task_observations(self):
        self.task['task_id']='actual-task'
        self.scan()
        feedback=self.repairer.get_scan_feedback(self.bundle,'actual-task')
        self.assertEqual(feedback['task_id'],'actual-task')
        self.assertEqual(len(feedback['scan_response_sha256']),64)
        self.assertIsNone(self.repairer.get_scan_feedback(self.bundle,'other-task'))
        self.assertIsNotNone(self.repairer.get_scan_feedback(self.bundle))

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

