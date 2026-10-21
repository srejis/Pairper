"""Clarify reader background versus paper claims and retain exact prior alias scope."""
from copy import deepcopy

from .catalog import digest

CLARIFICATION = """READER-CONTEXT CLARIFICATION OF THE ORIGINAL EXPLANATION TASK:
Explain the term so the reader understands the paper. A concise, accurate standard meaning of
a common concept or an identified cited method is allowed as reader background even when this
paper does not teach its full definition. Its source reference anchors identity and paper usage;
do not falsely claim that this paper states the background procedure. Do not reject accurate
background solely because the paper only names the concept. Still reject false, speculative or
overlong background and all unsupported paper-specific claims/results. The paper's explicit
symbol definitions, conventions, tasks, scales and scope ALWAYS take priority over generic usage.
Do not reduce a term to a category/name-only restatement or an unhelpful 'not defined here'.

Preserve faithful existing Korean-reader aliases when correcting scientific prose. The default
unique-occurrence rule applies to the narrow alias-correction API, not a blanket ban on normal
aliases repeated in the same provenance. reader_context.preserved_alias_scope provides host-built
ALL exact occurrences and full field contexts of prior reviewed aliases for the SAME source
target/concept/current translation. Independently check every occurrence; retain the literal when
all refer to this concept, and report a real mismatch rather than selecting only some occurrences.
The host supplies occurrence coordinates: the output schema asks for aliases as literal strings,
not a new complete_occurrences object. Do not silently clear a correct existing alias merely because
the frozen manifest predates its reviewed alias correction. Do not invent aliases or phrases.
"""


def preserved_alias_scope(workflow, state, batch, packet):
    if not hasattr(workflow.service, 'translation_surfaces'):
        return []
    prior_id = (state.get('paper_context_revalidation') or {}).get('prior_run_id')
    if prior_id:
        cache = getattr(workflow, '_reader_alias_baselines', {})
        if prior_id not in cache:
            prior = workflow._read(prior_id)
            if digest(prior) != state['paper_context_revalidation']['prior_run_sha256']:
                raise ValueError('Prior reviewed alias baseline changed')
            cache[prior_id] = {i['target']['id']: deepcopy(i['target'])
                              for b in prior['batches'] for i in b['candidate']['items']}
            workflow._reader_alias_baselines = cache
        baseline = cache[prior_id]
    else:
        baseline = {i['target']['id']: i['target'] for i in (batch.get('candidate') or {}).get('items', [])}
    from .alias_review import complete_occurrence_proposal

    bundle = workflow.service.prepare(state['paper_id'])
    scope = []
    for item in (batch.get('candidate') or {}).get('items', []):
        target = item['target']
        before = baseline.get(target['id'])
        if not before or target['kind'] != 'term' or before.get('concept_key') != target.get('concept_key'):
            continue
        for literal in before.get('aliases', []):
            try:
                proof = complete_occurrence_proposal(workflow.service, bundle, target, literal,
                                                     packet['translation_identity'])
            except ValueError:
                # A stale/missing literal is not carried forward as a valid alias.
                continue
            scope.append({'target_id': target['id'], 'concept_key': target['concept_key'],
                          'prior_reviewed_literal': literal, 'complete_occurrences': proof,
                          'semantic_equivalence_requires_current_independent_review': True})
    return scope
