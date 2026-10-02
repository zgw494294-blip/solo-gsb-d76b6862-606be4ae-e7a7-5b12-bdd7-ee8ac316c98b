"""Sensitive-provenance analysis over the node graph.

Two independent kinds of provenance are tracked for every node:

* ``content_origins`` (per column): sensitive source columns whose *values*
  reach the column. Copy propagates them; a constant replacement clears them.
* ``row_origins`` (per relation): sensitive source columns that can influence
  *which rows* the relation contains. Filters referencing a sensitive column,
  and joins whose join key carries sensitive content, taint the whole result;
  projections and constant replacements can never remove that taint, and a
  join merges the taint of both inputs.

A column of the export node is blocked when either set is non-empty.

``revise`` turns a blocked graph into one that passes review by appending a
single projection after the export node: columns carrying sensitive *content*
may be replaced by a constant ``null`` (subject to the caller-provided allow
list), while every other column is copied. Row-level influence cannot be
removed that way, so such graphs are reported as not revisable.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Union

from .models import (
    FilterNodeIn,
    JoinNodeIn,
    ProjectionNodeIn,
    PruneRequest,
    ReviewRequest,
    ReviseRequest,
    SourceNodeIn,
)

# Hard cap shared with the request models: a revised graph may never exceed it.
NODE_LIMIT = 60


class ValidationError(Exception):
    """The whole request must be rejected (HTTP 400)."""

    def __init__(
        self,
        code: str,
        message: str,
        node_id: str | None = None,
        extra: dict[str, object] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.node_id = node_id
        self.extra = extra or {}

    def detail(self) -> dict[str, object]:
        d: dict[str, object] = {"code": self.code, "message": self.message}
        if self.node_id is not None:
            d["node_id"] = self.node_id
        d.update(self.extra)
        return d


# A sensitive origin is a (source node id, source column name) pair.
Origin = tuple[str, str]


class Column:
    __slots__ = ("name", "content_origins")

    def __init__(self, name: str, content_origins: frozenset[Origin]):
        self.name = name
        self.content_origins = content_origins


class Relation:
    __slots__ = ("columns", "row_origins")

    def __init__(self, columns: list[Column], row_origins: frozenset[Origin]):
        self.columns = columns
        self.row_origins = row_origins

    def column_map(self) -> dict[str, Column]:
        return {c.name: c for c in self.columns}


def _unique(names: list[str], what: str, node_id: str) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ValidationError(
                "COLUMN_CONFLICT",
                f"{what} '{name}' is declared more than once in node '{node_id}'",
                node_id,
            )
        seen.add(name)


def _inputs(node: object) -> list[str]:
    if isinstance(node, (ProjectionNodeIn, FilterNodeIn)):
        return [node.input]
    if isinstance(node, JoinNodeIn):
        return [node.left_input, node.right_input]
    return []  # source nodes have no inputs


def topological_order(by_id: dict[str, object]) -> list[str]:
    """Return every node in dependency order.

    Edges go from an input node to its consumer. A request is rejected when an
    input id is unknown or the graph contains a cycle (self-loop included).
    The *whole* request is validated even for nodes the export does not use.
    """
    indegree: dict[str, int] = {nid: 0 for nid in by_id}
    edges: dict[str, list[str]] = defaultdict(list)
    for nid, node in by_id.items():
        for ref in _inputs(node):
            if ref not in by_id:
                raise ValidationError(
                    "INVALID_REFERENCE",
                    f"node '{nid}' references unknown input node '{ref}'",
                    nid,
                )
            edges[ref].append(nid)
            indegree[nid] += 1

    # Deterministic traversal: ids sorted alphabetically.
    ready = deque(sorted(nid for nid, d in indegree.items() if d == 0))
    order: list[str] = []
    while ready:
        nid = ready.popleft()
        order.append(nid)
        for consumer in sorted(edges[nid]):
            indegree[consumer] -= 1
            if indegree[consumer] == 0:
                ready.append(consumer)

    if len(order) != len(by_id):
        stuck = sorted(nid for nid, d in indegree.items() if d > 0)
        raise ValidationError(
            "CYCLE",
            "dependency cycle detected involving nodes: " + ", ".join(stuck),
        )
    return order


def _eval_source(node: SourceNodeIn) -> Relation:
    columns = [
        Column(
            col.name,
            frozenset({(node.id, col.name)}) if col.sensitive else frozenset(),
        )
        for col in node.columns
    ]
    return Relation(columns, frozenset())


def _eval_projection(node: ProjectionNodeIn, upstream: Relation) -> Relation:
    available = upstream.column_map()
    columns: list[Column] = []
    for spec in node.columns:
        if spec.is_constant:
            # A constant replacement clears the column's content provenance.
            columns.append(Column(spec.name, frozenset()))
        else:
            if spec.copy_name not in available:
                raise ValidationError(
                    "INVALID_REFERENCE",
                    f"projection node '{node.id}' copies unknown column "
                    f"'{spec.copy_name}' from input '{node.input}'",
                    node.id,
                )
            columns.append(Column(spec.name, available[spec.copy_name].content_origins))
    # Projection cannot remove row-level influence of upstream operations.
    return Relation(columns, upstream.row_origins)


def _eval_filter(node: FilterNodeIn, upstream: Relation) -> Relation:
    available = upstream.column_map()
    row_origins = set(upstream.row_origins)
    for ref in node.references:
        if ref not in available:
            raise ValidationError(
                "INVALID_REFERENCE",
                f"filter node '{node.id}' references unknown column "
                f"'{ref}' from input '{node.input}'",
                node.id,
            )
        # A sensitive predicate column affects every surviving row.
        row_origins |= available[ref].content_origins
    # The filter keeps all input columns, contents untouched.
    return Relation(
        [Column(c.name, c.content_origins) for c in upstream.columns],
        frozenset(row_origins),
    )


def _eval_join(
    node: JoinNodeIn, left: Relation, right: Relation
) -> Relation:
    left_cols = left.column_map()
    right_cols = right.column_map()

    for side_name, key, cols, input_id in (
        ("left", node.left_key, left_cols, node.left_input),
        ("right", node.right_key, right_cols, node.right_input),
    ):
        if key not in cols:
            raise ValidationError(
                "INVALID_REFERENCE",
                f"join node '{node.id}' uses unknown {side_name} key '{key}' "
                f"from input '{input_id}'",
                node.id,
            )

    # Row influence: both inputs' influence, plus sensitive key content on
    # either side, affects every row of the joined result.
    row_origins = (
        left.row_origins
        | right.row_origins
        | left_cols[node.left_key].content_origins
        | right_cols[node.right_key].content_origins
    )

    columns: list[Column] = []
    for out in node.outputs:
        if out.side == "left":
            if out.column not in left_cols:
                raise ValidationError(
                    "INVALID_REFERENCE",
                    f"join node '{node.id}' outputs unknown left column "
                    f"'{out.column}'",
                    node.id,
                )
            columns.append(Column(out.alias, left_cols[out.column].content_origins))
        else:
            if out.column not in right_cols:
                raise ValidationError(
                    "INVALID_REFERENCE",
                    f"join node '{node.id}' outputs unknown right column "
                    f"'{out.column}'",
                    node.id,
                )
            columns.append(Column(out.alias, right_cols[out.column].content_origins))
    return Relation(columns, row_origins)


def _origin_view(origins: frozenset[Origin]) -> list[dict[str, str]]:
    return [
        {"node_id": node_id, "column": column}
        for node_id, column in sorted(origins)
    ]


def _build_by_id(nodes: list[object]) -> dict[str, object]:
    """Check unique node/column names and index nodes by id."""
    by_id: dict[str, object] = {}
    for node in nodes:
        if node.id in by_id:
            raise ValidationError(
                "DUPLICATE_NODE",
                f"node id '{node.id}' is used more than once",
                node.id,
            )
        by_id[node.id] = node

        if isinstance(node, SourceNodeIn):
            _unique([c.name for c in node.columns], "column", node.id)
        elif isinstance(node, ProjectionNodeIn):
            _unique([c.name for c in node.columns], "output column", node.id)
        elif isinstance(node, FilterNodeIn):
            _unique(node.references, "reference", node.id)
        elif isinstance(node, JoinNodeIn):
            _unique([o.alias for o in node.outputs], "output alias", node.id)
    return by_id


def _evaluate_graph(
    request: Union[ReviewRequest, ReviseRequest, PruneRequest],
) -> tuple[dict[str, object], list[str], dict[str, Relation]]:
    """Run every original validation rule and evaluate all relations.

    Returns the node index, topological order and per-node relations. Raises
    :class:`ValidationError` exactly as ``review`` always has.
    """
    by_id = _build_by_id(list(request.nodes))

    if request.export not in by_id:
        raise ValidationError(
            "INVALID_REFERENCE",
            f"export node '{request.export}' does not exist",
            request.export,
        )

    # Validate and evaluate the whole graph: invalid references in branches
    # the export does not use still reject the whole request. At most 60 nodes.
    order = topological_order(by_id)

    relations: dict[str, Relation] = {}
    for nid in order:
        node = by_id[nid]
        if isinstance(node, SourceNodeIn):
            relations[nid] = _eval_source(node)
        elif isinstance(node, ProjectionNodeIn):
            relations[nid] = _eval_projection(node, relations[node.input])
        elif isinstance(node, FilterNodeIn):
            relations[nid] = _eval_filter(node, relations[node.input])
        else:
            relations[nid] = _eval_join(
                node, relations[node.left_input], relations[node.right_input]
            )
    return by_id, order, relations


def _review_export(
    export_id: str, relations: dict[str, Relation]
) -> dict[str, object]:
    """Review every column of one evaluated relation."""
    export_relation = relations[export_id]
    columns_out: list[dict[str, object]] = []
    any_blocked = False
    for column in export_relation.columns:
        content = column.content_origins
        rows = export_relation.row_origins
        blocked = bool(content) or bool(rows)
        any_blocked = any_blocked or blocked
        columns_out.append(
            {
                "name": column.name,
                "blocked": blocked,
                "content_reasons": _origin_view(content),
                "row_reasons": _origin_view(rows),
            }
        )

    return {
        "export": export_id,
        "blocked": any_blocked,
        "columns": columns_out,
    }


def review(request: ReviewRequest) -> dict[str, object]:
    _by_id, _order, relations = _evaluate_graph(request)
    return _review_export(request.export, relations)


def serialize_node(node: object) -> dict[str, object]:
    """Dump a validated node back into request-compatible JSON.

    ``exclude_unset`` is essential for projection columns: an unset
    ``constant`` must not be emitted as ``null`` next to a ``copy``.
    """
    return node.model_dump(by_alias=True, exclude_unset=True)  # type: ignore[attr-defined]


def _safe_projection_id(export_id: str, by_id: dict[str, object]) -> str:
    """Pick a deterministic projection id that cannot collide with originals."""
    base = f"{export_id}__safe_export"
    candidate = base
    suffix = 2
    while candidate in by_id:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def revise(request: ReviseRequest) -> dict[str, object]:
    """Produce a re-submittable, review-safe version of a reviewed graph.

    The original graph is validated and reviewed under exactly the same rules
    as :func:`review`. When it is already safe it is returned untouched (no new
    node, no replaced columns). Otherwise one projection may be appended after
    the export node; the request is rejected as not revisable (without any
    partial graph) when row-level influence exists, a blocked column is not on
    the allow list, or the appended node would exceed the node limit.
    """
    by_id, _order, relations = _evaluate_graph(request)
    export_relation = relations[request.export]
    export_column_names = {c.name for c in export_relation.columns}

    # The allow list itself is part of the request contract: every name must
    # resolve on the export node (distinctness is enforced by the model).
    missing = [n for n in request.allowed_replacements if n not in export_column_names]
    if missing:
        raise ValidationError(
            "REPLACEMENT_COLUMN_NOT_FOUND",
            "allowed replacement column(s) not found on export node "
            f"'{request.export}': " + ", ".join(missing),
            request.export,
            {"columns": missing},
        )

    review_result = _review_export(request.export, relations)
    original_nodes = [serialize_node(n) for n in request.nodes]

    # Already safe: return the graph as-is, nothing appended, nothing replaced.
    if not review_result["blocked"]:
        return {
            "nodes": original_nodes,
            "export": request.export,
            "revised": False,
            "replaced_columns": [],
            "review": review_result,
        }

    # Row-level influence lands on every column; a trailing projection cannot
    # remove it, so no revision is possible.
    if export_relation.row_origins:
        raise ValidationError(
            "UNREVISABLE_ROW_INFLUENCE",
            "the export relation is influenced at row level by sensitive "
            "columns; appending a projection cannot remove row-level influence",
            request.export,
            {"row_reasons": _origin_view(export_relation.row_origins)},
        )

    # With no row taint every blocked column carries sensitive content; each
    # one must be on the allow list or the revision is impossible.
    allowed = set(request.allowed_replacements)
    not_allowed = [
        c.name for c in export_relation.columns if c.content_origins and c.name not in allowed
    ]
    if not_allowed:
        raise ValidationError(
            "UNREVISABLE_COLUMN_NOT_ALLOWED",
            "blocked export column(s) are not in the allowed replacement list: "
            + ", ".join(not_allowed),
            request.export,
            {"columns": not_allowed},
        )

    if len(request.nodes) + 1 > NODE_LIMIT:
        raise ValidationError(
            "UNREVISABLE_NODE_LIMIT",
            f"appending the safe-export projection would exceed the "
            f"{NODE_LIMIT}-node limit (graph already has {len(request.nodes)} "
            "nodes)",
            request.export,
            {"node_count": len(request.nodes), "limit": NODE_LIMIT},
        )

    # Append exactly one projection: same column names and order, same row set;
    # permitted content-sensitive columns become constant null, others copy.
    projection_id = _safe_projection_id(request.export, by_id)
    projection_columns: list[dict[str, object]] = []
    replaced_columns: list[str] = []
    for column in export_relation.columns:
        if column.content_origins and column.name in allowed:
            projection_columns.append({"name": column.name, "constant": None})
            replaced_columns.append(column.name)
        else:
            projection_columns.append({"name": column.name, "copy": column.name})

    new_nodes = original_nodes + [
        {
            "id": projection_id,
            "type": "projection",
            "input": request.export,
            "columns": projection_columns,
        }
    ]

    # Re-review the constructed graph under the original rules before returning.
    re_review_payload = ReviewRequest.model_validate(
        {"nodes": new_nodes, "export": projection_id}
    )
    re_review_result = review(re_review_payload)
    if re_review_result["blocked"]:  # pragma: no cover - invariant by construction
        raise ValidationError(
            "UNREVISABLE_RESIDUAL_RISK",
            "internal error: the revised export is still blocked",
            projection_id,
        )

    return {
        "nodes": new_nodes,
        "export": projection_id,
        "revised": True,
        "replaced_columns": replaced_columns,
        "review": re_review_result,
    }


# ---------------------------------------------------------------------------
# Pruning by selected export columns
# ---------------------------------------------------------------------------


def _ancestors(export_id: str, by_id: dict[str, object]) -> set[str]:
    """All nodes the export depends on (the export itself included).

    A shared node feeds every branch that needs it; the ancestor closure is
    exactly the node set that may influence the export's columns or row set.
    Every input relation of a retained node is therefore retained too — a
    constant-only projection keeps its upstream even though it copies nothing.
    """
    keep: set[str] = {export_id}
    stack = [export_id]
    while stack:
        for ref in _inputs(by_id[stack.pop()]):
            if ref not in keep:
                keep.add(ref)
                stack.append(ref)
    return keep


def _safe_projection_id_from(base: str, used: set[str]) -> str:
    """Pick a deterministic projection id that cannot collide with ``used``."""
    candidate = f"{base}__export"
    suffix = 2
    while candidate in used:
        candidate = f"{base}__export_{suffix}"
        suffix += 1
    return candidate


def _column_reason_view(review_result: dict[str, object]) -> dict[str, object]:
    """Map column name -> (content reasons, row reasons) for review comparison."""
    return {
        col["name"]: (col["content_reasons"], col["row_reasons"])
        for col in review_result["columns"]
    }


def _compare_selected_columns(
    original: dict[str, object],
    pruned: dict[str, object],
    selected: list[str],
) -> dict[str, object]:
    """Return per-column discrepancies of the pruned review against original.

    The pruned export must expose exactly the selected columns, in the export's
    original order, and every one must carry the same content/row reasons as in
    the original graph (pruning is not a safety transform, so reasons must be
    preserved, never weakened). Used both by :func:`prune` and its tests.
    """
    orig_cols = _column_reason_view(original)
    pruned_cols = _column_reason_view(pruned)
    pruned_order = list(pruned_cols)

    diffs: dict[str, object] = {}
    missing = [name for name in selected if name not in pruned_cols]
    extra = [name for name in pruned_order if name not in selected]
    if missing:
        diffs["missing_columns"] = missing
    if extra:
        diffs["extra_columns"] = extra
    if pruned_order != selected:
        diffs["expected_order"] = selected
        diffs["actual_order"] = pruned_order

    mismatched: list[dict[str, object]] = []
    for name in selected:
        if name not in pruned_cols or name not in orig_cols:
            continue
        o_content, o_rows = orig_cols[name]
        p_content, p_rows = pruned_cols[name]
        if o_content != p_content or o_rows != p_rows:
            mismatched.append(
                {
                    "name": name,
                    "original": {
                        "content_reasons": o_content,
                        "row_reasons": o_rows,
                    },
                    "pruned": {
                        "content_reasons": p_content,
                        "row_reasons": p_rows,
                    },
                }
            )
    if mismatched:
        diffs["mismatched_columns"] = mismatched
    return diffs


def prune(request: PruneRequest) -> dict[str, object]:
    """Narrow a graph to a chosen subset of export columns.

    The whole original graph is validated exactly as :func:`review` does, and
    every selected column must exist on the export node. The export node and
    all of its ancestor nodes are retained (shared nodes satisfy every branch
    feeding them); nodes unreachable from the export are dropped. Columns are
    then narrowed bottom-up by a reverse dependency-demand pass:

    * source columns are never trimmed;
    * a filter keeps its referenced columns and every column its consumers
      need;
    * a projection and a join emit only the outputs that are needed — except
      the export node, which is left fully intact;
    * a retained projection/join with no demanded output keeps its original
      first output (and that output's dependencies);
    * both join keys are always demanded on their side inputs.

    When the export still contains columns the caller did not select, one
    trailing projection (copying only the selected columns, in original order)
    is appended so the final export exposes exactly the chosen columns. The
    result must stay within the 60-node cap and re-review identically for the
    selected columns; otherwise the request is rejected without any partial
    graph.
    """
    by_id, order, relations = _evaluate_graph(request)
    export_relation = relations[request.export]
    export_columns = [c.name for c in export_relation.columns]
    export_column_set = set(export_columns)

    # The selection is part of the request contract: every name must resolve on
    # the export node (distinctness/non-emptiness is enforced by the model).
    missing = [name for name in request.columns if name not in export_column_set]
    if missing:
        raise ValidationError(
            "PRUNE_COLUMN_NOT_FOUND",
            "selected column(s) not found on export node "
            f"'{request.export}': " + ", ".join(missing),
            request.export,
            {"columns": missing},
        )

    selected_set = set(request.columns)
    selected_in_order = [name for name in export_columns if name in selected_set]
    original_review = _review_export(request.export, relations)

    # --- node retention: export plus every dependency ancestor --------------
    keep = _ancestors(request.export, by_id)

    # --- reverse pass: which output columns each retained node must emit ----
    # The export node stays fully intact: seed every one of its outputs.
    demanded: dict[str, set[str]] = {request.export: set(export_columns)}
    for nid in reversed(order):
        if nid not in keep:
            continue
        node = by_id[nid]
        needed = demanded.setdefault(nid, set())

        if isinstance(node, SourceNodeIn):
            continue  # source columns are never trimmed

        if isinstance(node, FilterNodeIn):
            # Keep the predicate columns and whatever downstream still reads.
            demanded.setdefault(node.input, set()).update(needed)
            demanded[node.input].update(node.references)

        elif isinstance(node, ProjectionNodeIn):
            if nid != request.export:
                specs_by_name = {spec.name: spec for spec in node.columns}
                if not needed:
                    # No live output: preserve the node's first output instead
                    # of emitting an (illegal) empty projection. Mutate the set
                    # stored in ``demanded`` so the build phase sees the choice.
                    needed.add(node.columns[0].name)
                for name in needed:
                    spec = specs_by_name[name]
                    if not spec.is_constant:
                        # A constant demands no upstream content, but its input
                        # relation stays via the ancestor closure.
                        demanded.setdefault(node.input, set()).add(spec.copy_name)
            else:
                # Export is untouched: every copy target must remain available.
                demanded.setdefault(node.input, set())
                for spec in node.columns:
                    if not spec.is_constant:
                        demanded[node.input].add(spec.copy_name)

        else:  # JoinNodeIn
            if nid != request.export and not needed:
                needed.add(node.outputs[0].alias)
            left_need = demanded.setdefault(node.left_input, set())
            right_need = demanded.setdefault(node.right_input, set())
            # Both keys are always required: they shape the joined row set.
            left_need.add(node.left_key)
            right_need.add(node.right_key)
            outputs_by_alias = {out.alias: out for out in node.outputs}
            for alias in needed:
                out = outputs_by_alias[alias]
                if out.side == "left":
                    left_need.add(out.column)
                else:
                    right_need.add(out.column)

    # --- build the pruned nodes, keeping the original list/column order -----
    new_node_dicts: list[dict[str, object]] = []
    trimmed = False
    for node in request.nodes:
        nid = node.id
        if nid not in keep:
            continue
        data = serialize_node(node)

        if isinstance(node, ProjectionNodeIn) and nid != request.export:
            wanted = demanded[nid]
            kept_cols = [
                col for col in data["columns"] if col["name"] in wanted
            ]
            if len(kept_cols) != len(data["columns"]):
                trimmed = True
            data["columns"] = kept_cols

        elif isinstance(node, JoinNodeIn) and nid != request.export:
            wanted = demanded[nid]
            kept_outs = [
                out for out in data["outputs"] if out["alias"] in wanted
            ]
            if len(kept_outs) != len(data["outputs"]):
                trimmed = True
            data["outputs"] = kept_outs

        new_node_dicts.append(data)

    dropped_nodes = [node.id for node in request.nodes if node.id not in keep]

    # --- trailing projection when the export still has unselected columns ---
    needs_trailing = set(export_columns) != selected_set
    if needs_trailing and len(new_node_dicts) + 1 > NODE_LIMIT:
        raise ValidationError(
            "PRUNE_NODE_LIMIT",
            f"appending the export projection would exceed the {NODE_LIMIT}-node "
            f"limit (pruned graph already has {len(new_node_dicts)} nodes)",
            request.export,
            {"node_count": len(new_node_dicts) + 1, "limit": NODE_LIMIT},
        )

    final_export = request.export
    added_node = False
    if needs_trailing:
        trailing_id = _safe_projection_id_from(
            request.export, {n["id"] for n in new_node_dicts}
        )
        new_node_dicts.append(
            {
                "id": trailing_id,
                "type": "projection",
                "input": request.export,
                "columns": [
                    {"name": name, "copy": name} for name in selected_in_order
                ],
            }
        )
        final_export = trailing_id
        added_node = True

    # --- validate and re-review the constructed graph under original rules --
    re_payload = ReviewRequest.model_validate(
        {"nodes": new_node_dicts, "export": final_export}
    )
    pruned_review = review(re_payload)

    diffs = _compare_selected_columns(
        original_review, pruned_review, selected_in_order
    )
    if diffs:  # pragma: no cover - invariant by the demand construction
        raise ValidationError(
            "PRUNE_REVIEW_MISMATCH",
            "the pruned export does not preserve the original review for the "
            "selected columns",
            final_export,
            {"differences": diffs},
        )

    return {
        "nodes": new_node_dicts,
        "export": final_export,
        "columns": selected_in_order,
        "pruned": bool(dropped_nodes or trimmed or added_node),
        "dropped_nodes": dropped_nodes,
        "added_projection": final_export if added_node else None,
        "review": pruned_review,
    }
