"""Synthetic source-only regression tests: no existing Korean corpus is loaded."""
import base64
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from pairper.translation import planning


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def fixture(texts):
    blocks, units = [], []
    for i, text in enumerate(texts):
        ident = f'paragraph-{i}'
        ref = f'source-span-{i}'
        blocks.append({'id': ident, 'type': 'paragraph', 'order': i,
                       'source_refs': [ref], 'parent_id': None, 'relations': [],
                       'content': {'runs': [{'kind': 'text', 'refs': [ref],
                                             'superscript': False, 'literal': False}]}})
        unit = {'id': ident + '/runs', 'block_id': ident, 'field': 'runs',
                'type': 'paragraph', 'order': i, 'parent_id': None, 'relations': [],
                'pages': [1], 'source_refs': [ref], 'source_text': text, 'math': [],
                'source_text_sha256': digest(text)}
        unit['fingerprint'] = planning._sha(unit)
        units.append(unit)
    return {'paper_id': 'synthetic-paper', 'fingerprint': 'synthetic-source-fingerprint',
            'source_pdf_sha256': 'source-pdf-digest', 'view_id': 'source-view',
            'view_sha256': 'source-view-digest', 'source_snapshot_sha256': 'source-snapshot-digest',
            'document': {'blocks': blocks}, 'units': units, 'figures': [],
            'figure_source_spans': {}, 'math_text_inventory': []}


def add_math(bundle, count=1, nodes_per_item=1):
    for i in range(count):
        unit = bundle['units'][i]
        ident = f'⟦{unit["id"]}#M0⟧'
        latex = '\\text{Procedure ' + str(i) + '}'
        unit['source_text'] += ' ' + ident
        unit['source_text_sha256'] = digest(unit['source_text'])
        unit['math'] = [{'token': ident, 'latex': latex, 'refs': unit['source_refs']}]
        bundle['math_text_inventory'].append({'math_id': ident,
            'source_latex_sha256': digest(latex), 'text_nodes': [
                {'index': j, 'path': [j], 'text': 'Procedure', 'font': '\\text', 'pure': True}
                for j in range(nodes_per_item)]})


class SourcePlanningTests(unittest.TestCase):
    def setUp(self):
        # Keep temporary fixtures inside the explicitly writable test workspace.
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)

    def figure(self, bundle):
        image = Path(self.temp.name) / 'source.png'
        image.write_bytes(base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j3l0AAAAASUVORK5CYII='))
        bundle['document']['blocks'].append({'id': 'diagram', 'type': 'figure',
            'order': 20, 'source_refs': [], 'parent_id': None, 'relations': [],
            'content': {'region': {'page': 1, 'bbox': [1, 1, 10, 10], 'rotation': 0}}})
        bundle['figures'].append({'paper_id': bundle['paper_id'], 'block_id': 'diagram',
            'region': {'page': 1, 'bbox': [1, 1, 10, 10], 'rotation': 0},
            'crop': str(image.resolve()), 'crop_sha256': hashlib.sha256(image.read_bytes()).hexdigest()})
        return image

    def test_every_source_unit_once_and_bounded_batches(self):
        bundle = fixture(['abcde'] * 13)
        tasks = planning.build_plan(bundle, max_units=3, max_source_chars=11)
        self.assertEqual([i for t in tasks for i in t['source_unit_ids']],
                         [u['id'] for u in bundle['units']])
        self.assertTrue(all(len(t['units']) <= 3 and t['source_characters'] <= 11 for t in tasks))

    def test_math_all_nodes_and_max_eight_items_before_other_work(self):
        bundle = fixture(['Source'] * 17)
        add_math(bundle, count=17, nodes_per_item=8)
        self.figure(bundle)
        plan = planning.build_plan(bundle)
        math = [t for t in plan if t['kind'] == 'math']
        self.assertEqual([len(t['math_items']) for t in math], [8, 8, 1])
        self.assertEqual(sum(len(m['text_nodes']) for t in math for m in t['math_items']), 136)
        self.assertEqual([t['kind'] for t in plan], ['math'] * 3 + ['text', 'figure'])
        self.assertTrue(all(set(t['source_unit_ids']) == {i for m in t['math_items']
                            for i in m['source_unit_ids']} for t in math))

    def test_standalone_equation_with_no_text_unit_is_scheduled(self):
        bundle = fixture(['A source explanation.'])
        latex = '\\text{subject to } x > 0'
        bundle['document']['blocks'].append({'id': 'equation', 'type': 'equation', 'order': 2,
            'source_refs': ['math-source'], 'content': {'latex': latex,
            'region': {'page': 1, 'bbox': [1, 1, 20, 10], 'rotation': 0}}})
        bundle['math_text_inventory'] = [{'math_id': 'block:equation/latex',
            'source_latex_sha256': digest(latex), 'text_nodes': [
                {'index': 0, 'path': [0], 'text': 'subject to ', 'font': '\\text', 'pure': True}]}]
        math = planning.build_plan(bundle)[0]
        self.assertEqual(math['kind'], 'math')
        self.assertEqual(math['source_unit_ids'], [])
        self.assertEqual(math['math_items'][0]['source_latex'], latex)
        self.assertEqual(math['math_items'][0]['source_refs'], ['math-source'])

    def test_visual_task_required_even_without_extracted_spans(self):
        bundle = fixture(['Source'])
        image = self.figure(bundle)
        task = planning.build_plan(bundle)[-1]
        self.assertEqual(task['kind'], 'figure')
        self.assertEqual(task['figure_source_spans'], [])
        self.assertEqual(task['images'], [str(image.resolve())])
        self.assertTrue(task['preflight']['ready'])
        self.assertIn('figure_text_layer_empty', [x['code'] for x in task['preflight']['issues']])

    def test_missing_and_corrupt_images_are_blocked_not_dropped(self):
        bundle = fixture(['Source'])
        image = self.figure(bundle)
        image.write_bytes(b'changed-source-image')
        task = planning.build_plan(bundle)[-1]
        self.assertFalse(task['preflight']['ready'])
        self.assertEqual(task['images'], [])
        bundle['figures'] = []
        task = planning.build_plan(bundle)[-1]
        self.assertEqual(task['kind'], 'figure')
        self.assertFalse(task['preflight']['ready'])

    def test_math_inventory_missing_or_stale_is_reported(self):
        bundle = fixture(['Source'])
        add_math(bundle)
        bundle['math_text_inventory'][0]['source_latex_sha256'] = 'wrong'
        self.assertFalse(planning.build_plan(bundle)[0]['preflight']['ready'])
        del bundle['math_text_inventory']
        self.assertFalse(planning.preflight_bundle(bundle)['ready'])

    def test_only_blank_text_without_math_is_auto_preservable(self):
        bundle = fixture([' \n\t', '123.45', 'model-code', 'Equation'])
        add_math(bundle, count=4)
        bundle['units'][0]['source_text'] = ' \n\t'
        bundle['units'][0]['source_text_sha256'] = digest(' \n\t')
        bundle['units'][0]['math'] = []
        bundle['math_text_inventory'].pop(0)
        plan = planning.build_plan(bundle)
        text = [t for t in plan if t['kind'] == 'text']
        self.assertEqual([i for t in text for i in t['auto_preserve_source_unit_ids']],
                         [bundle['units'][0]['id']])

    def test_source_only_no_target_review_or_hint_contamination(self):
        bundle = fixture(['Source'])
        add_math(bundle)
        sentinel = 'OLD_TRANSLATION_MUST_NEVER_BE_READ'
        bundle['result'] = {'target': sentinel}
        bundle['units'][0]['target'] = sentinel
        bundle['units'][0]['math'][0]['review'] = {'target': sentinel}
        bundle['document']['blocks'][0]['target'] = sentinel
        bundle['math_text_inventory'][0]['text_nodes'][0]['translated_text'] = sentinel
        before = deepcopy(bundle)
        plan = planning.build_plan(bundle)
        self.assertNotIn(sentinel, json.dumps(plan))
        self.assertEqual(bundle, before)
        self.assertEqual(plan, planning.build_plan(deepcopy(bundle)))

    def test_full_selected_provenance_and_source_typography_retained(self):
        bundle = fixture(['Source'])
        refs = [f'span-{i}' for i in range(1200)]
        bundle['units'][0]['source_refs'] = refs
        bundle['document']['blocks'][0]['content']['runs'][0].update(
            refs=refs, superscript=True, source_verified=True, text_override='Source')
        unit = planning.build_plan(bundle)[0]['units'][0]
        self.assertEqual(unit['source_refs'], refs)
        self.assertEqual(unit['source_runs'][0]['refs'], refs)
        self.assertTrue(unit['source_runs'][0]['superscript'])
        self.assertEqual(unit['source_runs'][0]['text_override'], 'Source')

    def test_context_links_headers_and_equations_are_bounded(self):
        bundle = fixture(['Heading', 'Parent', 'Header', 'Current cell', 'Caption', 'Nearby prose' * 10000])
        blocks, units = bundle['document']['blocks'], bundle['units']
        blocks[0]['type'] = units[0]['type'] = 'heading'
        blocks[1]['type'] = units[1]['type'] = 'group'
        blocks[2]['type'] = blocks[3]['type'] = units[2]['type'] = units[3]['type'] = 'table'
        # Two units belong to one original table, carrying actual header geometry.
        units[3].update(block_id=blocks[2]['id'], field='cells/1')
        units[2]['field'] = 'cells/0'
        units[2]['cell'] = {'row': 0, 'column': 0, 'rowspan': 1, 'colspan': 1, 'header': True}
        units[3]['cell'] = {'row': 1, 'column': 0, 'rowspan': 1, 'colspan': 1, 'header': False}
        blocks[2]['parent_id'] = blocks[1]['id']
        blocks[2]['content']['cells'] = [{'runs': []}, {'runs': []}]
        blocks[4]['type'] = units[4]['type'] = 'caption'
        blocks[4]['relations'] = [{'kind': 'caption_of', 'target_id': blocks[2]['id']}]
        plan = planning.build_plan(bundle, max_units=1, max_source_chars=1000)
        task = next(t for t in plan if t['kind'] == 'text' and t['source_unit_ids'] == [units[3]['id']])
        context = task['context']
        self.assertEqual(context['headings'][0]['block_id'], blocks[0]['id'])
        self.assertEqual(context['parents'][0]['block_id'], blocks[1]['id'])
        self.assertEqual(context['captions'][0]['block_id'], blocks[4]['id'])
        self.assertEqual(context['table_headers'][0]['units'][0]['id'], units[2]['id'])
        self.assertLessEqual(context['source_characters_in_excerpts'], context['source_character_budget'])
        self.assertLessEqual(len(context['context_unit_ids']) + len(task['source_unit_ids']), 64)

    def test_oversize_source_not_silently_truncated_or_preserved(self):
        text = 'source ' * 100
        task = planning.build_plan(fixture([text]), max_source_chars=10)[0]
        self.assertEqual(task['units'][0]['source_text'], text)
        self.assertFalse(task['preflight']['ready'])
        self.assertEqual(task['auto_preserve_source_unit_ids'], [])

    def test_generic_source_risk_does_not_repair_any_source(self):
        source = 'STATE VECTOR LENGTH=16 from alpha beta.module and image labels.csv'
        bundle = fixture([source])
        plan = planning.build_plan(bundle)
        self.assertEqual(plan[0]['units'][0]['source_text'], source)
        self.assertIn('possible_identifier_glyph_or_spacing_loss',
                      [x['code'] for x in plan[0]['preflight']['issues']])
        self.assertTrue(plan[0]['preflight']['ready'])

    def test_invalid_structural_ids_and_limits_fail_explicitly(self):
        bundle = fixture(['Source'])
        bundle['units'].append(deepcopy(bundle['units'][0]))
        with self.assertRaises(ValueError):
            planning.build_plan(bundle)
        for args in [{'max_units': True}, {'max_units': 0}, {'max_units': 65}, {'max_source_chars': 0}]:
            with self.assertRaises(ValueError):
                planning.build_plan(fixture(['Source']), **args)


if __name__ == '__main__':
    unittest.main()
