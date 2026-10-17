"""Shared hierarchy checks and tree construction; no layout or semantic guessing."""


def structure_errors(blocks):
    ordered = sorted(blocks, key=lambda b: b.order)
    by_id = {b.id: b for b in ordered}
    errors, children, stack, closed = [], {}, [], set()
    for block in ordered:
        parent = by_id.get(block.parent_id)
        if block.parent_id and (parent is None or parent.type != "group"):
            errors.append(f"{block.id}: parent must name an existing group")
        if parent and parent.order >= block.order:
            errors.append(f"{block.id}: parent must precede its children")
        if block.parent_id in closed:
            errors.append(f"{block.id}: group children must be contiguous in reading order")
        while stack and stack[-1] != block.parent_id:
            closed.add(stack.pop())
        if block.parent_id and (not stack or stack[-1] != block.parent_id):
            errors.append(f"{block.id}: invalid group nesting or noncontiguous children")
        children.setdefault(block.parent_id, []).append(block)
        if block.type == "group":
            stack.append(block.id)
            if len(stack) > 32:
                errors.append(f"{block.id}: groups exceed maximum nesting depth of 32")
        for relation in block.relations:
            target = by_id.get(relation.target_id)
            if target is None or target.id == block.id:
                errors.append(f"{block.id}: relation has an unknown or self target")
                continue
            if relation.kind == "proof_of" and not (
                block.type == "group" and block.content.group_role == "proof"
                and target.type == "group"
                and target.content.group_role in {"theorem", "lemma", "fact"}
            ):
                errors.append(f"{block.id}: proof_of must connect a proof to a statement")
            if relation.kind == "caption_of" and not (
                block.type == "caption" and target.type in {"figure", "table", "code", "group"}
            ):
                errors.append(f"{block.id}: caption_of must connect a caption to its content")
            if relation.kind == "footnote_for" and block.type != "footnote":
                errors.append(f"{block.id}: footnote_for requires a footnote")
    for block in ordered:
        if block.type == "group" and not children.get(block.id):
            errors.append(f"{block.id}: group must contain child blocks")
    return errors


def block_tree(blocks):
    errors = structure_errors(blocks)
    if errors:
        raise ValueError("; ".join(errors))
    children = {}
    for block in sorted(blocks, key=lambda b: b.order):
        children.setdefault(block.parent_id, []).append(block)
    return children
