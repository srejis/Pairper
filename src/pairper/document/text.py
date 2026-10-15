"""One source-text join policy shared by the Python and TypeScript renderers."""

import re

from pairper.document.models import MathRun


def resolve_text(run, spans, audit):
    originals = [spans[ref]["text"] for ref in run.refs]
    separator = {"space": " ", "none": "", "newline": "\n", "dehyphenate": " "}[run.join]
    result = originals[0] if originals else ""
    for text in originals[1:]:
        if run.join == "dehyphenate" and result.endswith("-"):
            result = result[:-1] + text
        elif separator == " " and (
            result.endswith((" ", "\n", "\t")) or text.startswith((" ", "\n", "\t"))
        ):
            result += text
        else:
            result += separator + text
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
