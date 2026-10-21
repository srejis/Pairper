"""Exact occurrences across all reader text fields, without altering source runs."""
import hashlib
import json
import re
from copy import deepcopy

from pairper.document.models import MathRun
from pairper.document.provenance import materialize_ranges
from pairper.document.text import resolved_runs


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def fields(content):
    yield 'runs', content.runs
    yield from ((f'items/{i}', runs) for i, runs in enumerate(content.items))
    yield from ((f'cells/{i}', cell.runs) for i, cell in enumerate(content.cells))


def target_id(target):
    if target.get('anchor_type') == 'text_math':
        return 'exp_tm_' + digest({k: target.get(k) for k in
            ('block_id', 'field', 'source_text', 'text_parts')})[:24]
    return 'exp_' + digest({k: target.get(k) for k in
                           ('kind', 'block_id', 'field', 'run_index', 'start', 'end',
                            'source_text')})[:24]


def normalize_unique_term_offsets(request, response):
    """Resolve a unique exact source quote, never infer missing text or identity.

    Models select concepts; the host counts Unicode characters. Repeated quotes
    retain their supplied positions and must pass the ordinary exact validator.
    The caller records both raw response and normalized-response hashes.
    """
    normalized, corrections = deepcopy(response), []
    if request.get('stage') not in ('generate', 'repair') or not isinstance(normalized, dict):
        return normalized, corrections
    units = {}
    for unit in request.get('packet', {}).get('text_units', []):
        key = (unit.get('block_id'), unit.get('field'), unit.get('run_index'))
        units.setdefault(key, []).append(unit)
    for item in normalized.get('items', []):
        if not isinstance(item, dict) or not isinstance(item.get('target'), dict):
            continue
        target = item['target']
        if target.get('kind') != 'term':
            continue
        key = (target.get('block_id'), target.get('field'), target.get('run_index'))
        selected = units.get(key, [])
        quote = target.get('source_text')
        if len(selected) != 1 or not isinstance(quote, str) or not quote:
            continue
        text = selected[0].get('text')
        if not isinstance(text, str):
            continue
        start = text.find(quote)
        if start < 0 or text.find(quote, start + 1) >= 0:
            continue
        end = start + len(quote)
        if type(target.get('start')) is int and type(target.get('end')) is int and (
                target['start'], target['end']) == (start, end):
            continue
        corrections.append({'item_key': item.get('key'), 'block_id': key[0], 'field': key[1],
                            'run_index': key[2], 'before': {'start': target.get('start'),
                                                          'end': target.get('end')},
                            'after': {'start': start, 'end': end}, 'method': 'unique_exact_quote'})
        target.update(start=start, end=end)
    return normalized, corrections


def math_content_kind(latex):
    """Conservative syntax classification, never a claim about scientific meaning."""
    value = re.sub(r'\s+', '', latex)
    notation = {r'\{', r'\}', '{', '}', '(', ')', '[', ']', '|', ',', '.', ':', ';',
                r'\bullet', r'\times', r'\cdot', r'\ldots', r'\cdots', r'\dots',
                '<', '>', '=', '+', '-', r'\leq', r'\geq', r'\neq', r'\in',
                r'\rightarrow', r'\leftarrow', r'\pm', r'\ast', r'\star'}
    if value in notation or re.fullmatch(r'(?:\{\})?\^(?:\{\d+(?:,\d+)*\}|\d+)', value):
        return 'notation'
    numeric = value
    for _ in range(4):
        unwrapped = re.sub(r'\\(?:mathbf|mathrm|textbf|mathit|text|textrm|boldsymbol)\{([^{}]*)\}',
                           r'\1', numeric)
        if unwrapped == numeric:
            break
        numeric = unwrapped
    number = r'[+-]?\d+(?:\.\d+)?'
    # A two-line table cell containing mean and parenthesized uncertainty is one
    # numeric quantity. Do not flatten arbitrary rows (1\\\\2 must never become 12)
    # or strip environments from actual relationships.
    gathered_quantity = re.fullmatch(
        r'\\begin\{gathered\}(' + number + r')\\\\\(\\pm(' + number
        + r')\)\\end\{gathered\}', numeric)
    if gathered_quantity:
        numeric = gathered_quantity[1] + r'\pm' + gathered_quantity[2]
    exponent = r'(?:\^(?:\{[+-]?\d+\}|[+-]?\d+))?'
    if re.fullmatch(number + exponent + r'(?:\\(?:times|cdot|pm)' + number + exponent + r')?(?:\\%)?', numeric):
        return 'literal'
    source = latex.strip()
    for left, right in ((r'\left(', r'\right)'), ('(', ')'), ('{', '}')):
        if source.startswith(left) and source.endswith(right):
            inner = source[len(left):-len(right)]
            inner_role = math_content_kind(inner)
            if inner_role in ('symbol', 'literal'):
                # Grouping one identifier/value does not create a computation.
                # Relations and compound expressions retain the formula role.
                return inner_role
    # Recognize one identifier with optional formatting and scripts. A function
    # application, sum, product or other expression still requires its process.
    greek = ('alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa '
             'lambda mu nu xi pi varpi rho varrho sigma varsigma tau upsilon phi varphi '
             'chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega '
             'ell imath jmath infty emptyset top bot nabla').split()
    wrappers = {'mathbf', 'mathrm', 'mathit', 'mathcal', 'mathbb', 'mathsf', 'boldsymbol',
                'operatorname', 'hat', 'widehat', 'bar', 'overline', 'underline', 'tilde',
                'widetilde', 'vec', 'dot', 'ddot'}

    def space(position):
        while position < len(source) and source[position].isspace():
            position += 1
        return position

    def group(position):
        depth, cursor = 1, position + 1
        while cursor < len(source):
            if source[cursor] == '\\':
                cursor += 2
                continue
            depth += (source[cursor] == '{') - (source[cursor] == '}')
            cursor += 1
            if depth == 0:
                return cursor
        return None

    def atom(position):
        position = space(position)
        if position >= len(source):
            return None
        if source[position] == '\\':
            match = re.match(r'\\([A-Za-z]+)', source[position:])
            if match is None:
                return None
            command = match.group(1)
            end = space(position + len(match.group()))
            if command in wrappers:
                if end < len(source) and source[end] == '{':
                    finish = group(end)
                    if finish is None:
                        return None
                    inside = source[end + 1:finish - 1]
                    # Formatting may wrap an identifier; never hide arithmetic.
                    if math_content_kind(inside) != 'symbol' and not re.fullmatch(r'[A-Za-z]+', inside):
                        return None
                    return finish
                return atom(end)
            return end if command in greek else None
        return position + 1 if source[position].isalpha() else None

    end = atom(0)
    while end is not None:
        end = space(end)
        if end >= len(source):
            return 'symbol'
        if source[end] == "'":
            end += 1
            continue
        if source[end] not in '_^':
            break
        end = space(end + 1)
        if end >= len(source):
            break
        if source[end] == '{':
            end = group(end)
        elif source[end].isalnum():
            end += 1
        elif source[end] == '\\':
            # A script argument can be a single TeX control sequence, including
            # \star, \prime or \perp; its braced equivalent already works above.
            script = re.match(r'\\[A-Za-z]+', source[end:])
            end = end + len(script.group()) if script else None
        else:
            end = None
    return 'formula'


def target_math_content_kind(bundle, target):
    """Context-aware syntax hint. All scientific meanings still need review."""
    role = math_content_kind(target['source_text'])
    if role != 'formula':
        return role
    context = next((c for c in bundle.get('context', [])
                    if c['block_id'] == target['block_id']), None)
    if (context and context['type'] == 'paragraph' and target['source_text'] == r'\square'
            and context['text'].strip() in (r'\square', r'\(\square\)')):
        contexts = bundle['context']
        position = next(i for i, c in enumerate(contexts) if c['block_id'] == target['block_id'])
        for previous in reversed(contexts[:position]):
            if previous['text'].strip() in (r'\square', r'\(\square\)'):
                break
            if previous['type'] == 'heading' and not re.match(
                    r'^\s*(?:Part|Case|Step)\s+\d+\b', previous['text'], re.IGNORECASE):
                break
            if re.match(r'^\s*Proof\b', previous['text'], re.IGNORECASE):
                # A standalone square closing an active proof is punctuation,
                # not the square operator inside a mathematical relationship.
                return 'notation'
    author_metadata = bool(context and (context['type'] == 'authors' or (
        context['type'] == 'footnote' and re.search(
            r'\b(?:equal contribution|equal contributors|equal authorship|'
            r'work (?:was )?completed as an intern|corresponding author|correspondence to)\b',
            ' '.join(context['text'].split()), re.IGNORECASE))))
    if author_metadata:
        marker = r'(?:\\(?:dagger|ddagger|ast|star)|[*†‡])'
        if re.fullmatch(r'\{\}\^(?:\{' + marker + r'+\}|' + marker + r')',
                        re.sub(r'\s+', '', target['source_text'])):
            # Equal-contribution/contact marks on names have no mathematical
            # base or calculation. Only names or explicit authorship metadata
            # qualify; identical shapes in scientific prose remain formulas.
            return 'notation'
    if not re.fullmatch(r'\d+\^\{(?:st|nd|rd|th)\}', target['source_text']):
        return role
    if context and context['type'] in ('footnote', 'reference') and re.search(
            r'\bProceedings\b.{0,180}\b(?:Conference|Symposium|Workshop)\b',
            context['text'], re.IGNORECASE | re.DOTALL):
        # Conference ordinal typography is not a numerical exponent. This only
        # permits a reviewed notation disposition; it never removes the target.
        return 'notation'
    return role


def prepare_catalog(document, source, identity):
    spans = {s['id']: {**s, 'page': p['page']} for p in source['pages'] for s in p['spans']}
    units, targets, contexts, footnote_markers = [], [], [], []
    for block in sorted(document.blocks, key=lambda b: b.order):
        materialize_ranges(block, source)
        block_text = []
        for field, runs in fields(block.content):
            math_index = 0
            for index, (run, prefix, text) in enumerate(resolved_runs(runs, spans, [])):
                if isinstance(run, MathRun):
                    target = {'kind': 'math', 'block_id': block.id, 'field': field,
                              'run_index': index, 'start': None, 'end': None,
                              'source_text': run.latex, 'source_refs': list(run.refs),
                              'display': False,
                              'source_math_id': f'⟦{block.id}/{field}#M{math_index}⟧'}
                    math_index += 1
                    target['id'] = target_id(target)
                    targets.append(target)
                    block_text.append(prefix + '\\(' + run.latex + '\\)')
                else:
                    if (block.type == 'footnote' and field == 'runs' and index == 0
                            and run.superscript and re.fullmatch(r'[0-9]+', text)):
                        footnote_markers.append({'block_id': block.id, 'field': field,
                            'run_index': index, 'marker': text, 'superscript': True,
                            'source_refs': list(run.refs)})
                    units.append({'block_id': block.id, 'block_type': block.type, 'field': field,
                                  'run_index': index, 'text': text,
                                  'source_refs': list(run.refs)})
                    block_text.append(prefix + text)
        if block.type == 'equation' and block.content.latex:
            target = {'kind': 'math', 'block_id': block.id, 'field': 'latex',
                      'run_index': None, 'start': None, 'end': None,
                      'source_text': block.content.latex,
                      'source_refs': list(block.source_refs), 'display': True,
                      'source_math_id': f'block:{block.id}/latex'}
            target['id'] = target_id(target)
            targets.append(target)
            block_text.append(block.content.latex)
        contexts.append({'block_id': block.id, 'type': block.type, 'order': block.order,
                         'parent_id': block.parent_id, 'text': ''.join(block_text)})
    bundle = {'schema_version': 1, 'paper_id': document.paper_id,
              'source_identity': identity, 'text_units': units, 'targets': targets,
              'context': contexts}
    bundle['fingerprint'] = digest(bundle)
    # Reader-only provenance metadata: introducing a boundary annotation must not
    # invalidate the immutable scientific source/selection fingerprint.
    if footnote_markers:
        bundle['source_footnote_markers'] = footnote_markers
    return bundle


def validate_targets(bundle, targets):
    """Reject freeform sentence anchors and guessed/stale character positions.

    The semantic concept/term decision is reviewed separately; these gates prevent
    whole-paragraph targets, punctuation sentences, overlap, and arbitrary scope.
    Offsets count Unicode code points (Python slicing / JS Array.from).
    """
    from .text_math import all_math_targets

    source_math = {t['id']: t for t in all_math_targets(bundle) if t['kind'] == 'math'}
    units = {(u['block_id'], u['field'], u['run_index']): u for u in bundle['text_units']}
    validated = []
    occupied = {}
    allowed = {'id', 'kind', 'block_id', 'field', 'run_index', 'start', 'end',
               'source_text', 'source_refs', 'display', 'aliases', 'source_math_id', 'concept_key',
               'anchor_type', 'text_parts'}
    for incoming in targets:
        if not isinstance(incoming, dict) or set(incoming) - allowed:
            raise ValueError('Unknown explanation target fields')
        target = dict(incoming)
        if target.get('kind') == 'math':
            expected = source_math.get(target.get('id', target_id(target)))
            if expected is None or any(target.get(k) != v for k, v in expected.items() if k != 'id'):
                raise ValueError('Math target must match an exact source formula occurrence')
            validated.append(dict(expected))
            continue
        if target.get('kind') != 'term':
            raise ValueError('Only concepts, technical terms and formulas may have explanations')
        if 'text_parts' in target or 'anchor_type' in target:
            raise ValueError('Only source-bound text formulas may have formula parts')
        key = (target.get('block_id'), target.get('field'), target.get('run_index'))
        unit = units.get(key)
        start, end = target.get('start'), target.get('end')
        if (unit is None or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(unit['text'])):
            raise ValueError('Term requires exact in-range Unicode character offsets')
        text = unit['text'][start:end]
        if target.get('source_text') != text or not text.strip() or text != text.strip():
            raise ValueError('Term quote differs from source occurrence')
        # A reference title can begin with a compact callable method name whose
        # extracted abbreviation contains a period and space. Keep this exception
        # tied to that source position and syntax, never to a known name list.
        reference_method_name = (
            unit.get('block_type') == 'reference' and start == 0
            and re.match(r'\s*:', unit['text'][end:]) is not None
            and re.fullmatch(
                r'[A-Za-z][A-Za-z0-9_-]{1,15}\. +'
                r'[A-Za-z][A-Za-z_-]{0,15}\d+[A-Za-z0-9_.-]{0,15} *\(\)', text)
            is not None)
        sentence_punctuation = bool(re.search(r'[!?。！？;\n]|\.\s', text))
        if reference_method_name:
            sentence_punctuation = False
        sentence_punctuation |= text.endswith('.') and not re.fullmatch(r'(?:[A-Za-z]\.){2,}', text)
        if len(text) > 160 or len(text.split()) > 12 or sentence_punctuation:
            raise ValueError('Term target is a sentence or an oversized phrase')
        def latin_word(char):
            return char.isascii() and (char.isalnum() or char == '_')

        if (start and latin_word(unit['text'][start - 1]) and latin_word(text[0])
                or end < len(unit['text']) and latin_word(unit['text'][end])
                and latin_word(text[-1])):
            raise ValueError('Term target splits a word')
        aliases = target.get('aliases', [])
        if (not isinstance(aliases, list) or any(not isinstance(a, str) or not a.strip()
                                               or len(a) > 160 for a in aliases)):
            raise ValueError('Aliases must be explicit short term translations')
        concept = target.get('concept_key')
        if concept is not None and (not isinstance(concept, str) or not concept.strip()
                                    or concept != concept.strip() or len(concept) > 120):
            raise ValueError('Concept key must identify a reviewed paper-local technical meaning')
        for left, right in occupied.setdefault(key, []):
            if start < right and end > left:
                raise ValueError('Term occurrences overlap')
        occupied[key].append((start, end))
        target.update(source_refs=unit['source_refs'], display=False)
        expected_id = target_id(target)
        if target.get('id', expected_id) != expected_id:
            raise ValueError('Target ID differs from its exact source occurrence')
        target['id'] = expected_id
        validated.append(target)
    if len({t['id'] for t in validated}) != len(validated):
        raise ValueError('Duplicate explanation target')
    return validated
