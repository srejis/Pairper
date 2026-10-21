"""Compact reference method names must not become sentence anchors."""
import pytest

from pairper.explanation.catalog import validate_targets


def checked(quote, *, block_type='reference', prefix='', suffix=': method title'):
    unit = {'block_id': 'r', 'field': 'runs', 'run_index': 1,
            'block_type': block_type, 'text': prefix + quote + suffix,
            'source_refs': ['original-reference']}
    target = {'kind': 'term', 'block_id': 'r', 'field': 'runs', 'run_index': 1,
              'start': len(prefix), 'end': len(prefix) + len(quote), 'source_text': quote}
    return validate_targets({'targets': [], 'text_units': [unit]}, [target])


@pytest.mark.parametrize('quote', ['Llm. int8 ()', 'Abc. fp16()', 'Model. quant4_v2 ()'])
def test_reference_title_compact_method_identifier(quote):
    assert checked(quote)[0]['source_text'] == quote


@pytest.mark.parametrize('quote,options', [
    ('Fig. 8()', {}),
    ('First sentence. Second sentence', {}),
    ('A long method clause. int8 ()', {}),
    ('Llm. int8 ()', {'block_type': 'paragraph'}),
    ('Llm. int8 ()', {'prefix': 'We cite '}),
    ('Llm. int8 ()', {'suffix': ' is a method.'}),
    ('Llm. int8 ()?', {}),
])
def test_reference_exception_does_not_allow_prose_or_figure_labels(quote, options):
    with pytest.raises(ValueError, match='sentence or an oversized phrase'):
        checked(quote, **options)
