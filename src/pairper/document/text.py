"""One source-text join policy shared by the Python and TypeScript renderers."""

import re

from pairper.document.models import MathRun


def resolve_text(run, spans, audit):
    originals = [spans[ref]["text"] for ref in run.refs]
    if run.text_override is not None:
        audit.append({"refs": run.refs, "original_strings": originals,
                      "operation": "verified_text_transcription", "result": run.text_override})
        return run.text_override
    separator = {"space": " ", "none": "", "newline": "\n", "dehyphenate": " "}[run.join]
    result = originals[0] if originals else ""
    for text in originals[1:]:
        if run.join == "dehyphenate" and result.endswith("-"):
            result = result[:-1] + text
        elif run.join == "dehyphenate" and result.endswith(("´", "˜", "¨")):
            result += text
        elif separator == " " and (
            result.endswith((" ", "\n", "\t")) or text.startswith((" ", "\n", "\t"))
        ):
            result += text
        else:
            result += separator + text
    # Observed TeX PDF extraction: spacing accents precede Latin letters / dotless i.
    composed = result
    for extracted, displayed in (("´ı", "í"), ("´e", "é"), ("´a", "á"),
                                 ("´o", "ó"), ("˜n", "ñ"), ("¨ı", "ï"),
                                 ("¨u", "ü"), ("´E", "É")):
        composed = composed.replace(extracted, displayed)
    if composed != result:
        audit.append({"refs": run.refs, "original_strings": originals,
                      "operation": "compose_pdf_accents", "before": result, "result": composed})
        result = composed
    if len(originals) > 1:
        audit.append(
            {
                "refs": run.refs,
                "original_strings": originals,
                "operation": run.join,
                "result": result,
            }
        )
    return result


def boundary(previous, current, spans):
    if previous is None:
        return ""
    if current.before != "auto":
        return {"none": "", "space": " ", "newline": "\n"}[current.before]
    left = spans[previous.refs[-1]]["text"] if previous.refs else ""
    right = spans[current.refs[0]]["text"] if current.refs else ""
    # A math reconstruction does not render the leading whitespace in its source refs.
    if isinstance(current, MathRun) and right.startswith((" ", "\n", "\t")):
        return "" if left.endswith((" ", "\n", "\t")) else " "
    # OFA/AutoML math renderers also discard source trailing whitespace.
    if isinstance(previous, MathRun) and left.endswith((" ", "\n", "\t")):
        if isinstance(current, MathRun) or not (
            right.startswith((" ", "\n", "\t")) or re.match(r"^[,.;:!?)}\]]", right)
        ):
            return " "
    if (
        left.endswith((" ", "\n", "\t", "-", "(", "[", "{"))
        or right.startswith((" ", "\n", "\t"))
        or re.match(r"^[,.;:!?)}\]]", right)
    ):
        return ""
    prev_box = spans[previous.refs[-1]]["bbox"] if previous.refs else previous.region.bbox
    next_box = spans[current.refs[0]]["bbox"] if current.refs else current.region.bbox
    size = spans[current.refs[0]].get("size", 12) if current.refs else 12
    # Adjacent font/style spans within one word must not acquire a space.
    if abs(prev_box[1] - next_box[1]) < size * 0.5 and abs(next_box[0] - prev_box[2]) < size * 0.18:
        return ""
    return " "


def resolved_runs(runs, spans, audit):
    previous = None
    for run in runs:
        prefix = boundary(previous, run, spans)
        if previous is not None:
            audit.append(
                {
                    "operation": "run_boundary",
                    "previous_refs": previous.refs,
                    "refs": run.refs,
                    "policy": run.before,
                    "inserted": prefix,
                }
            )
        yield run, prefix, None if isinstance(run, MathRun) else resolve_text(run, spans, audit)
        previous = run
