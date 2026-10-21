"""Source-bound opaque-math sentence boundaries; validation, never rewriting."""

import re

_HANGUL = re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7a3]")
_DELIMITER = re.compile(r"\\(?:left|right|middle|big|Big|bigg|Bigg)[lrm]?\s*$")


def terminal_sentence_period(latex):
    """Only an unescaped, top-level final literal period, not a TeX delimiter."""
    if not isinstance(latex, str):
        return False
    latex = latex.rstrip()
    if not latex.endswith("."):
        return False
    prefix = latex[:-1]
    backslashes = len(prefix) - len(prefix.rstrip("\\"))
    if backslashes % 2 or _DELIMITER.search(prefix):
        return False
    depth = 0
    escaped = False
    for char in prefix:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def validate_math_sentence_boundaries(unit, target):
    """Reject direct Hangul attachment to a source token's sentence period.

    The source/token/target remain untouched. Whitespace, decimal/internal periods,
    commands and factorials are outside this narrow format gate; semantic review
    still judges the complete expanded sentence.
    """
    for math in unit.get("math", []):
        token, latex = math.get("token"), math.get("latex")
        if not token or not terminal_sentence_period(latex):
            continue
        start = 0
        while (position := target.find(token, start)) >= 0:
            end = position + len(token)
            if end < len(target) and _HANGUL.match(target[end]):
                boundary = target[max(0, position - 24) : end + 32]
                raise ValueError(
                    "Opaque math sentence-period boundary: unit="
                    + str(unit.get("id", unit.get("unit_id")))
                    + "; token="
                    + token
                    + "; source_latex="
                    + repr(latex)
                    + "; target_boundary="
                    + repr(boundary)
                    + ". Preserve the exact source math token and rebuild the surrounding "
                    "Korean sentence; do not alter source punctuation, insert only whitespace, "
                    "or add notation-reading explanations."
                )
            start = end
