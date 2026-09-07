"""Read Alembic metadata without importing or executing candidate code."""

import ast


def graph(files):
    revisions = {}
    for name, source in files.items():
        if not name.endswith(".py") or name.endswith("__init__.py"):
            continue
        fields = {}
        for node in ast.parse(source, filename=name).body:
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, ast.AnnAssign)
                else []
            )
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {
                    "revision",
                    "down_revision",
                    "depends_on",
                    "branch_labels",
                }:
                    fields[target.id] = ast.literal_eval(node.value)
        if fields.get("depends_on") or fields.get("branch_labels"):
            raise ValueError("Migration dependencies or branches require manual review")
        revision, parent = fields["revision"], fields["down_revision"]
        if not isinstance(revision, str) or revision in revisions:
            raise ValueError("Invalid or duplicate migration revision")
        if parent is not None and not isinstance(parent, str):
            raise ValueError("Branched migrations require manual review")
        revisions[revision] = parent
    heads = set(revisions) - set(revisions.values())
    if len(heads) != 1:
        raise ValueError("A unique migration head is required")
    head = next(iter(heads))
    seen, cursor = set(), head
    while cursor is not None:
        if cursor in seen or cursor not in revisions:
            raise ValueError("Broken migration graph")
        seen.add(cursor)
        cursor = revisions[cursor]
    if seen != set(revisions):
        raise ValueError("Disconnected migration graph")
    return head, revisions


def compatible(old, new, current):
    _, old_graph = graph(old)
    head, new_graph = graph(new)
    if any(
        revision not in new_graph or new_graph[revision] != parent
        for revision, parent in old_graph.items()
    ):
        raise ValueError("Existing migration revision or parent changed or removed")
    if current not in new_graph:
        raise ValueError("Database revision is not an ancestor of candidate head")
    return head
