"""Tests for column-driven graph slimming (POST /api/slim).

The slim operation keeps the export node and every required ancestor, prunes
projection/join outputs down to what is demanded, and may append one trailing
projection when the export itself (source/filter) still carries unselected
columns. Selected columns must keep exactly their original content and row
reasons; nodes and columns keep their relative order.
"""

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError

from app.engine import ValidationError, review, slim
from app.main import app
from app.models import ReviewRequest, SlimRequest


def call_slim(payload):
    return slim(SlimRequest.model_validate(payload))


def call_review(payload):
    return review(ReviewRequest.model_validate(payload))


def by_name(result):
    return {c["name"]: c for c in result["columns"]}


# Shared graph: an unreachable side branch and an unselected projection output
# must both disappear; the filter references 'country' and must keep it.
GRAPH = {
    "nodes": [
        {
            "id": "users",
            "type": "source",
            "columns": [
                {"name": "uid", "sensitive": False},
                {"name": "email", "sensitive": True},
                {"name": "country", "sensitive": False},
            ],
        },
        {
            "id": "only_cn",
            "type": "filter",
            "input": "users",
            "references": ["country"],
        },
        {
            "id": "export_users",
            "type": "projection",
            "input": "only_cn",
            "columns": [
                {"name": "user_id", "copy": "uid"},
                {"name": "mail", "copy": "email"},
                {"name": "bucket", "constant": "ALL"},
            ],
        },
        # Unreachable from the export: must be deleted entirely.
        {
            "id": "side",
            "type": "projection",
            "input": "users",
            "columns": [{"name": "mail", "copy": "email"}],
        },
    ],
    "export": "export_users",
    "columns": ["user_id", "bucket"],
}


# ----------------------------- happy paths --------------------------------


def test_projection_export_is_pruned_in_place_without_appended_node():
    res = call_slim(GRAPH)
    assert res["export"] == "export_users"
    assert [n["id"] for n in res["nodes"]] == ["users", "only_cn", "export_users"]

    export_node = res["nodes"][-1]
    assert export_node["columns"] == [
        {"name": "user_id", "copy": "uid"},
        {"name": "bucket", "constant": "ALL"},
    ]
    # No trailing projection node is appended when pruning the export itself
    # already yields exactly the selected columns.
    assert all(n["id"] != "export_users__slim_export" for n in res["nodes"])
    assert [c["name"] for c in res["review"]["columns"]] == ["user_id", "bucket"]


def test_unreachable_branch_is_deleted():
    res = call_slim(GRAPH)
    assert "side" not in {n["id"] for n in res["nodes"]}


def test_source_columns_are_never_trimmed():
    res = call_slim(GRAPH)
    source = res["nodes"][0]
    assert [c["name"] for c in source["columns"]] == ["uid", "email", "country"]
    # Sensitive flags pass through unchanged.
    assert source["columns"][1]["sensitive"] is True


def test_filter_references_are_retained():
    res = call_slim(GRAPH)
    flt = next(n for n in res["nodes"] if n["id"] == "only_cn")
    assert flt["references"] == ["country"]


def test_selected_columns_keep_original_review_reasons():
    original = by_name(call_review({"nodes": GRAPH["nodes"], "export": GRAPH["export"]}))
    res = call_slim(GRAPH)
    slimmed = by_name(res["review"])
    for name in ("user_id", "bucket"):
        assert slimmed[name]["blocked"] == original[name]["blocked"]
        assert slimmed[name]["content_reasons"] == original[name]["content_reasons"]
        assert slimmed[name]["row_reasons"] == original[name]["row_reasons"]


def test_constant_column_demands_no_upstream_column_but_keeps_input_relation():
    payload = {
        "nodes": [
            {"id": "s", "type": "source",
             "columns": [{"name": "a"}, {"name": "b"}]},
            {"id": "p", "type": "projection", "input": "s",
             "columns": [{"name": "x", "copy": "a"}, {"name": "lit", "constant": 1}]},
            {"id": "e", "type": "projection", "input": "p",
             "columns": [{"name": "lit", "copy": "lit"}]},
        ],
        "export": "e",
        "columns": ["lit"],
    }
    res = call_slim(payload)
    ids = [n["id"] for n in res["nodes"]]
    # The constant needs no column from 's', but the relation 's' stays.
    assert ids == ["s", "p", "e"]
    p_node = next(n for n in res["nodes"] if n["id"] == "p")
    assert p_node["columns"] == [{"name": "lit", "constant": 1}]


def test_filter_export_with_unselected_columns_gets_trailing_projection():
    payload = {
        "nodes": [
            {"id": "s", "type": "source",
             "columns": [{"name": "id_card", "sensitive": True},
                         {"name": "score"}, {"name": "region"}]},
            {"id": "f", "type": "filter", "input": "s",
             "references": ["region"]},
        ],
        "export": "f",
        "columns": ["score"],
    }
    res = call_slim(payload)
    assert [n["id"] for n in res["nodes"]] == ["s", "f", "f__slim_export"]
    assert res["export"] == "f__slim_export"
    trailing = res["nodes"][-1]
    assert trailing == {
        "id": "f__slim_export",
        "type": "projection",
        "input": "f",
        "columns": [{"name": "score", "copy": "score"}],
    }
    # The source stays untrimmed; the filter condition stays intact.
    assert res["nodes"][1]["references"] == ["region"]
    assert by_name(res["review"])["score"]["row_reasons"] == []


def test_source_export_appends_projection_and_keeps_source_intact():
    payload = {
        "nodes": [
            {"id": "s", "type": "source",
             "columns": [{"name": "a", "sensitive": True},
                         {"name": "b"}, {"name": "c"}]},
        ],
        "export": "s",
        "columns": ["b", "a"],
    }
    res = call_slim(payload)
    assert len(res["nodes"]) == 2
    assert res["nodes"][0] == payload["nodes"][0]
    # Final columns follow the original export column order, not request order.
    assert [c["name"] for c in res["review"]["columns"]] == ["a", "b"]
    cols = by_name(res["review"])
    assert cols["a"]["blocked"] is True
    assert cols["a"]["content_reasons"] == [{"node_id": "s", "column": "a"}]
    assert cols["b"]["blocked"] is False


def test_trailing_projection_uses_original_column_order():
    payload = {
        "nodes": [
            {"id": "s", "type": "source",
             "columns": [{"name": "a"}, {"name": "b"}, {"name": "c"}]},
        ],
        "export": "s",
        "columns": ["c", "a"],
    }
    res = call_slim(payload)
    assert res["nodes"][-1]["columns"] == [
        {"name": "a", "copy": "a"},
        {"name": "c", "copy": "c"},
    ]


def test_join_prunes_outputs_but_keeps_both_keys_and_both_inputs():
    payload = {
        "nodes": [
            {"id": "l", "type": "source",
             "columns": [{"name": "k"}, {"name": "secret", "sensitive": True},
                         {"name": "v"}]},
            {"id": "r", "type": "source",
             "columns": [{"name": "k"}, {"name": "info"}, {"name": "junk"}]},
            {
                "id": "j",
                "type": "join",
                "left_input": "l",
                "right_input": "r",
                "left_key": "k",
                "right_key": "k",
                "outputs": [
                    {"alias": "secret", "side": "left", "column": "secret"},
                    {"alias": "v", "side": "left", "column": "v"},
                    {"alias": "info", "side": "right", "column": "info"},
                    {"alias": "junk", "side": "right", "column": "junk"},
                ],
            },
        ],
        "export": "j",
        "columns": ["secret", "info"],
    }
    res = call_slim(payload)
    assert {n["id"] for n in res["nodes"]} == {"l", "r", "j"}
    join_node = next(n for n in res["nodes"] if n["id"] == "j")
    assert [o["alias"] for o in join_node["outputs"]] == ["secret", "info"]
    assert join_node["left_key"] == "k" and join_node["right_key"] == "k"

    original = by_name(call_review({"nodes": payload["nodes"], "export": "j"}))
    slimmed = by_name(res["review"])
    for name in ("secret", "info"):
        assert slimmed[name]["content_reasons"] == original[name]["content_reasons"]
        assert slimmed[name]["row_reasons"] == original[name]["row_reasons"]


def test_sensitive_join_key_row_influence_survives_slimming():
    payload = {
        "nodes": [
            {"id": "orders", "type": "source",
             "columns": [{"name": "uid_enc", "sensitive": True},
                         {"name": "amount"}, {"name": "x"}]},
            {"id": "users", "type": "source",
             "columns": [{"name": "uid"}, {"name": "name"}]},
            {
                "id": "j",
                "type": "join",
                "left_input": "orders",
                "right_input": "users",
                "left_key": "uid_enc",
                "right_key": "uid",
                "outputs": [
                    {"alias": "amt", "side": "left", "column": "amount"},
                    {"alias": "x", "side": "left", "column": "x"},
                    {"alias": "uname", "side": "right", "column": "name"},
                ],
            },
        ],
        "export": "j",
        "columns": ["amt"],
    }
    res = call_slim(payload)
    col = res["review"]["columns"][0]
    assert col["name"] == "amt"
    assert col["row_reasons"] == [{"node_id": "orders", "column": "uid_enc"}]
    assert col["blocked"] is True


def test_join_key_sourced_from_projection_demands_that_output():
    # The join needs key 'x' from projection p even though no kept join output
    # comes from that side column; p keeps the key plus the selected output.
    payload = {
        "nodes": [
            {"id": "s", "type": "source",
             "columns": [{"name": "a"}, {"name": "b"}]},
            {"id": "p", "type": "projection", "input": "s",
             "columns": [{"name": "x", "copy": "a"}, {"name": "y", "copy": "b"}]},
            {"id": "s2", "type": "source", "columns": [{"name": "x"}]},
            {
                "id": "j",
                "type": "join",
                "left_input": "p",
                "right_input": "s2",
                "left_key": "x",
                "right_key": "x",
                "outputs": [{"alias": "y", "side": "left", "column": "y"}],
            },
        ],
        "export": "j",
        "columns": ["y"],
    }
    res = call_slim(payload)
    p_node = next(n for n in res["nodes"] if n["id"] == "p")
    assert [c["name"] for c in p_node["columns"]] == ["x", "y"]


def test_internal_projection_with_no_column_need_keeps_first_output():
    # p provides only the join key 'x'; none of its outputs is selected. The
    # rule keeps its original first output ('x') and that output's dependency.
    payload = {
        "nodes": [
            {"id": "s", "type": "source",
             "columns": [{"name": "a"}, {"name": "b"}, {"name": "z"}]},
            {"id": "p", "type": "projection", "input": "s",
             "columns": [{"name": "x", "copy": "a"}, {"name": "y", "copy": "b"}]},
            {
                "id": "j",
                "type": "join",
                "left_input": "p",
                "right_input": "s",
                "left_key": "x",
                "right_key": "a",
                "outputs": [{"alias": "z", "side": "right", "column": "z"}],
            },
        ],
        "export": "j",
        "columns": ["z"],
    }
    res = call_slim(payload)
    p_node = next(n for n in res["nodes"] if n["id"] == "p")
    assert [c["name"] for c in p_node["columns"]] == ["x"]


def test_shared_node_merges_branch_requirements():
    payload = {
        "nodes": [
            {"id": "s", "type": "source",
             "columns": [{"name": "a"}, {"name": "b"}, {"name": "k"}]},
            {"id": "p1", "type": "projection", "input": "s",
             "columns": [{"name": "a", "copy": "a"}, {"name": "k", "copy": "k"}]},
            {"id": "p2", "type": "projection", "input": "s",
             "columns": [{"name": "b", "copy": "b"}, {"name": "k", "copy": "k"}]},
            {
                "id": "jx",
                "type": "join",
                "left_input": "p1",
                "right_input": "p2",
                "left_key": "k",
                "right_key": "k",
                "outputs": [
                    {"alias": "a", "side": "left", "column": "a"},
                    {"alias": "b", "side": "right", "column": "b"},
                ],
            },
        ],
        "export": "jx",
        "columns": ["a", "b"],
    }
    res = call_slim(payload)
    nodes = {n["id"]: n for n in res["nodes"]}
    assert [c["name"] for c in nodes["p1"]["columns"]] == ["a", "k"]
    assert [c["name"] for c in nodes["p2"]["columns"]] == ["b", "k"]
    # Shared source keeps all columns (sources are never trimmed).
    assert [c["name"] for c in nodes["s"]["columns"]] == ["a", "b", "k"]


def test_filter_passthrough_keeps_join_output_needed_only_for_rows():
    # The filter references 'w'; 'v' is the selected column. The join feeding
    # the filter must keep both output aliases.
    payload = {
        "nodes": [
            {"id": "l", "type": "source",
             "columns": [{"name": "k"}, {"name": "v"}, {"name": "w"}]},
            {"id": "r", "type": "source", "columns": [{"name": "k"}]},
            {
                "id": "j",
                "type": "join",
                "left_input": "l",
                "right_input": "r",
                "left_key": "k",
                "right_key": "k",
                "outputs": [
                    {"alias": "v", "side": "left", "column": "v"},
                    {"alias": "w", "side": "left", "column": "w"},
                ],
            },
            {"id": "f", "type": "filter", "input": "j", "references": ["w"]},
        ],
        "export": "f",
        "columns": ["v"],
    }
    res = call_slim(payload)
    join_node = next(n for n in res["nodes"] if n["id"] == "j")
    assert [o["alias"] for o in join_node["outputs"]] == ["v", "w"]


def test_node_and_column_relative_order_is_preserved():
    payload = {
        "nodes": [
            {"id": "e", "type": "projection", "input": "f",
             "columns": [{"name": "sc", "copy": "sc"}]},
            {"id": "f", "type": "filter", "input": "s", "references": ["r"]},
            {"id": "s", "type": "source",
             "columns": [{"name": "sc"}, {"name": "r"}]},
        ],
        "export": "e",
        "columns": ["sc"],
    }
    res = call_slim(payload)
    assert [n["id"] for n in res["nodes"]] == ["e", "f", "s"]


def test_original_node_ids_are_preserved():
    res = call_slim(GRAPH)
    assert {n["id"] for n in res["nodes"]} <= {
        "users", "only_cn", "export_users", "side"
    }


def test_trailing_projection_id_avoids_collision():
    payload = {
        "nodes": [
            {"id": "s", "type": "source",
             "columns": [{"name": "a"}, {"name": "b"}]},
            {"id": "s__slim_export", "type": "projection", "input": "s",
             "columns": [{"name": "a", "copy": "a"}]},
            {"id": "f", "type": "filter", "input": "s", "references": ["a"]},
        ],
        "export": "f",
        "columns": ["a"],
    }
    res = call_slim(payload)
    assert res["export"] == "f__slim_export"
    assert len({n["id"] for n in res["nodes"]}) == len(res["nodes"])


def test_slimmed_graph_round_trips_through_review():
    res = call_slim(GRAPH)
    follow = call_review({"nodes": res["nodes"], "export": res["export"]})
    assert follow == res["review"]
    # The returned payload is plain JSON and re-submittable.
    encoded = json.dumps({"nodes": res["nodes"], "export": res["export"]})
    assert call_review(json.loads(encoded)) == res["review"]


def test_selecting_all_projection_columns_changes_nothing():
    payload = {
        "nodes": [n for n in GRAPH["nodes"] if n["id"] != "side"],
        "export": "export_users",
        "columns": ["user_id", "mail", "bucket"],
    }
    res = call_slim(payload)
    assert res["export"] == "export_users"
    assert res["nodes"] == payload["nodes"]


# ----------------------------- rejections ---------------------------------


def expect_engine_error(payload, code):
    with pytest.raises(ValidationError) as exc:
        call_slim(payload)
    assert exc.value.code == code
    return exc.value


def test_selected_column_missing_on_export_rejected():
    exc = expect_engine_error(
        {"nodes": GRAPH["nodes"], "export": "export_users", "columns": ["ghost"]},
        "SLIM_COLUMN_NOT_FOUND",
    )
    assert exc.node_id == "export_users"
    assert exc.detail()["columns"] == ["ghost"]


def test_node_limit_rejected_when_appended_projection_would_exceed_60():
    nodes = [{"id": "s0", "type": "source",
              "columns": [{"name": "a"}, {"name": "b"}]}]
    for i in range(1, 60):
        nodes.append(
            {"id": f"n{i}", "type": "filter",
             "input": f"n{i - 1}" if i > 1 else "s0", "references": ["a"]}
        )
    exc = expect_engine_error(
        {"nodes": nodes, "export": "n59", "columns": ["a"]},
        "SLIM_NODE_LIMIT",
    )
    assert exc.detail()["node_count"] == 61
    assert exc.detail()["limit"] == 60


def test_60_nodes_with_in_place_pruning_succeeds():
    nodes = [{"id": "s0", "type": "source",
              "columns": [{"name": "a"}, {"name": "b"}]}]
    for i in range(1, 60):
        nodes.append(
            {"id": f"n{i}", "type": "projection",
             "input": f"n{i - 1}" if i > 1 else "s0",
             "columns": [{"name": "a", "copy": "a"}, {"name": "b", "copy": "b"}]}
        )
    res = call_slim({"nodes": nodes, "export": "n59", "columns": ["a"]})
    assert len(res["nodes"]) == 60
    assert res["export"] == "n59"


def test_original_graph_validation_still_applies():
    expect_engine_error(
        {
            "nodes": [
                {"id": "a", "type": "projection", "input": "b",
                 "columns": [{"name": "x", "constant": 1}]},
                {"id": "b", "type": "projection", "input": "a",
                 "columns": [{"name": "x", "constant": 1}]},
            ],
            "export": "a",
            "columns": ["x"],
        },
        "CYCLE",
    )
    expect_engine_error(
        {"nodes": [{"id": "s", "type": "source", "columns": [{"name": "a"}]}],
         "export": "missing", "columns": ["a"]},
        "INVALID_REFERENCE",
    )
    expect_engine_error(
        {
            "nodes": [
                {"id": "s", "type": "source", "columns": [{"name": "a"}]},
                {"id": "f", "type": "filter", "input": "s",
                 "references": ["ghost"]},
            ],
            "export": "f",
            "columns": ["a"],
        },
        "INVALID_REFERENCE",
    )


# ----------------------------- request validation -------------------------


def test_empty_columns_list_rejected():
    with pytest.raises(PydanticValidationError):
        SlimRequest.model_validate(
            {"nodes": [{"id": "s", "type": "source",
                        "columns": [{"name": "a"}]}],
             "export": "s", "columns": []}
        )


def test_missing_columns_field_rejected():
    with pytest.raises(PydanticValidationError):
        SlimRequest.model_validate(
            {"nodes": [{"id": "s", "type": "source",
                        "columns": [{"name": "a"}]}], "export": "s"}
        )


def test_duplicate_column_names_rejected():
    with pytest.raises(PydanticValidationError):
        SlimRequest.model_validate(
            {"nodes": [{"id": "s", "type": "source",
                        "columns": [{"name": "a"}]}],
             "export": "s", "columns": ["a", "a"]}
        )


def test_empty_column_name_rejected():
    with pytest.raises(PydanticValidationError):
        SlimRequest.model_validate(
            {"nodes": [{"id": "s", "type": "source",
                        "columns": [{"name": "a"}]}],
             "export": "s", "columns": [""]}
        )


def test_unknown_field_rejected():
    with pytest.raises(PydanticValidationError):
        SlimRequest.model_validate(
            {"nodes": [{"id": "s", "type": "source",
                        "columns": [{"name": "a"}]}],
             "export": "s", "columns": ["a"], "nope": 1}
        )


def test_more_than_60_input_nodes_rejected():
    nodes = [{"id": "s0", "type": "source", "columns": [{"name": "a"}]}]
    for i in range(1, 61):
        nodes.append(
            {"id": f"n{i}", "type": "projection",
             "input": f"n{i - 1}" if i > 1 else "s0",
             "columns": [{"name": "a", "copy": "a"}]}
        )
    with pytest.raises(PydanticValidationError):
        SlimRequest.model_validate({"nodes": nodes, "export": "n60",
                                    "columns": ["a"]})


# ----------------------------- HTTP endpoint ------------------------------


client = TestClient(app)


def test_http_slim_success():
    resp = client.post("/api/slim", json=GRAPH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["export"] == "export_users"
    assert [n["id"] for n in body["nodes"]] == ["users", "only_cn", "export_users"]
    assert [c["name"] for c in body["review"]["columns"]] == ["user_id", "bucket"]


def test_http_slim_round_trips_to_review():
    first = client.post("/api/slim", json=GRAPH)
    assert first.status_code == 200
    body = first.json()
    follow = client.post("/api/review",
                         json={"nodes": body["nodes"], "export": body["export"]})
    assert follow.status_code == 200
    assert follow.json() == body["review"]


def test_http_slim_missing_column_returns_400_without_partial_graph():
    resp = client.post(
        "/api/slim",
        json={"nodes": GRAPH["nodes"], "export": "export_users",
              "columns": ["ghost"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "SLIM_COLUMN_NOT_FOUND"
    assert "nodes" not in body and "review" not in body


def test_http_slim_node_limit_returns_400():
    nodes = [{"id": "s0", "type": "source",
              "columns": [{"name": "a"}, {"name": "b"}]}]
    for i in range(1, 60):
        nodes.append({"id": f"n{i}", "type": "filter",
                      "input": f"n{i - 1}" if i > 1 else "s0",
                      "references": ["a"]})
    resp = client.post("/api/slim",
                       json={"nodes": nodes, "export": "n59",
                             "columns": ["a"]})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "SLIM_NODE_LIMIT"


def test_http_slim_schema_errors_return_422():
    resp = client.post(
        "/api/slim",
        json={"nodes": [{"id": "s", "type": "source",
                         "columns": [{"name": "a"}]}],
              "export": "s", "columns": ["a", "a"]},
    )
    assert resp.status_code == 422

    resp = client.post(
        "/api/slim",
        json={"nodes": [{"id": "s", "type": "source",
                         "columns": [{"name": "a"}]}],
              "export": "s"},
    )
    assert resp.status_code == 422


def test_http_existing_endpoints_unchanged():
    review_resp = client.post(
        "/api/review",
        json={"nodes": GRAPH["nodes"], "export": GRAPH["export"]},
    )
    assert review_resp.status_code == 200
    body = review_resp.json()
    assert body["export"] == "export_users"
    assert "review" not in body
    assert client.get("/health").json() == {"status": "ok"}
