"""Tests for falkordb_export — GQLField upsert null-safety (COALESCE fix).

Strategy: mock falkordb.FalkorDB so tests run without a live FalkorDB instance.
Captured Cypher queries are inspected to verify correctness.
"""

from __future__ import annotations

import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import NodeInfo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_falkordb() -> tuple[MagicMock, list[str]]:
    """Return (mock_falkordb_module, captured_queries list)."""
    captured: list[str] = []
    mock_graph = MagicMock()
    mock_graph.query.side_effect = lambda q, *a, **kw: captured.append(q)
    mock_client = MagicMock()
    mock_client.select_graph.return_value = mock_graph
    mock_module = MagicMock()
    mock_module.FalkorDB.return_value = mock_client
    return mock_module, captured


@contextmanager
def _patch_falkordb(mock_module: MagicMock):
    """Inject mock_module as 'falkordb' in sys.modules for the duration."""
    original = sys.modules.get("falkordb")
    sys.modules["falkordb"] = mock_module
    try:
        yield
    finally:
        if original is None:
            sys.modules.pop("falkordb", None)
        else:
            sys.modules["falkordb"] = original


def _run_export(store: GraphStore, mock_module: MagicMock) -> list[str]:
    """Run export_to_falkordb with mocked FalkorDB, return captured queries."""
    captured_ref: list[str] = []
    mock_graph = MagicMock()
    mock_graph.query.side_effect = lambda q, *a, **kw: captured_ref.append(q)
    mock_client = MagicMock()
    mock_client.select_graph.return_value = mock_graph
    mock_module.FalkorDB.return_value = mock_client

    from code_review_graph.falkordb_export import export_to_falkordb
    with _patch_falkordb(mock_module):
        export_to_falkordb(store, graph_name="test_graph")

    return captured_ref


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path):
    db = tmp_path / "test.db"
    s = GraphStore(str(db))
    yield s
    s.close()


def _seed_gql_field(store: GraphStore, extra: dict | None = None) -> None:
    """Seed a File + GQLField node pair into the store."""
    store.upsert_node(NodeInfo(
        kind="File", name="resolvers.js",
        file_path="/repo/resolvers.js",
        line_start=1, line_end=100, language="javascript",
    ), file_hash="fh1")
    store.upsert_node(NodeInfo(
        kind="GQLField", name="createUser",
        file_path="/repo/resolvers.js",
        line_start=10, line_end=20, language="javascript",
        parent_name="Mutation",
        extra=extra or {},
    ), file_hash="fh1")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGQLFieldCoalesceUpsert:
    """GQLField MERGE Cypher must use COALESCE for optional props."""

    _OPTIONAL_PROPS = ("is_deleted", "is_external", "operation", "auth", "directives")

    def test_coalesce_present_in_gql_field_query(self, store):
        """Every optional prop must be wrapped in COALESCE so a null value
        from row doesn't overwrite (delete) the existing property in FalkorDB."""
        _seed_gql_field(store, extra={"is_deleted": False, "operation": "mutation"})

        mock_module, _ = _make_mock_falkordb()
        queries = _run_export(store, mock_module)

        gql_merges = [q for q in queries if "GQLField" in q and "MERGE" in q]
        assert gql_merges, "No GQLField MERGE query was issued"

        q = gql_merges[0]
        for prop in self._OPTIONAL_PROPS:
            assert f"COALESCE(row.{prop}" in q, (
                f"Missing COALESCE for optional prop '{prop}'.\n"
                f"Without it a re-export nulls out the property in FalkorDB.\n"
                f"Query: {q}"
            )

    def test_coalesce_present_when_extra_is_empty(self, store):
        """COALESCE must be present even when the node has no optional props in extra.
        This is the exact case that caused the bug — empty extra → null in Cypher."""
        _seed_gql_field(store, extra={})

        mock_module, _ = _make_mock_falkordb()
        queries = _run_export(store, mock_module)

        gql_merges = [q for q in queries if "GQLField" in q and "MERGE" in q]
        assert gql_merges, "GQLField with empty extra must still be exported"

        q = gql_merges[0]
        for prop in self._OPTIONAL_PROPS:
            assert f"COALESCE(row.{prop}" in q, (
                f"Missing COALESCE for '{prop}' when extra={{}}.\n"
                f"Query: {q}"
            )

    def test_core_props_are_set_directly(self, store):
        """Core props (qualified_name, name, file_path, language, line_start,
        line_end, repo) must NOT use COALESCE — code-review-graph always owns them."""
        _seed_gql_field(store)

        mock_module, _ = _make_mock_falkordb()
        queries = _run_export(store, mock_module)

        gql_merges = [q for q in queries if "GQLField" in q and "MERGE" in q]
        assert gql_merges

        q = gql_merges[0]
        for prop in ("qualified_name", "name", "file_path", "language", "line_start", "line_end", "repo"):
            assert f"n.{prop} = row.{prop}" in q or f"n.{prop}           = row.{prop}" in q or f"row.{prop}" in q, (
                f"Core prop '{prop}' should be SET directly (not via COALESCE).\nQuery: {q}"
            )
            assert f"COALESCE(row.{prop}" not in q, (
                f"Core prop '{prop}' should NOT use COALESCE.\nQuery: {q}"
            )

    def test_export_does_not_crash_with_gql_field(self, store):
        """Smoke test: export_to_falkordb completes without exception for GQLField nodes."""
        _seed_gql_field(store, extra={"is_deleted": True, "auth": "jwt", "operation": "query"})

        mock_module, _ = _make_mock_falkordb()
        # Should not raise
        _run_export(store, mock_module)
