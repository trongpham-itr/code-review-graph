"""Export code-review-graph SQLite data into FalkorDB.

Usage (CLI)::

    code-review-graph falkordb-export \\
        --repo /path/to/repo \\
        --graph-name my_service \\
        --host localhost \\
        --port 6379

The exporter reads nodes and edges from the local SQLite graph store and
writes them into a FalkorDB graph using Cypher MERGE statements so the
command is safe to re-run (idempotent).

Node labels match GraphStore kinds: File, Function, Class, Type, Test,
GQLType, GQLField, Loader.

Edge relationship types match GraphStore kinds: CALLS, IMPORTS_FROM,
INHERITS, CONTAINS, TESTED_BY, DEPENDS_ON, REFERENCES, FIELD_OF, RETURNS,
ACCEPTS, RESOLVES, RESOLVES_EXTERNAL, RESOLVES_REF, DELEGATES_TO,
USES_LOADER, BACKED_BY.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Batch size for UNWIND bulk inserts
_NODE_BATCH = 200
_EDGE_BATCH = 500


def _flatten_extra(extra: dict | None) -> dict:
    """Flatten extra dict into scalar-only properties safe for Cypher."""
    if not extra:
        return {}
    flat: dict[str, Any] = {}
    for k, v in extra.items():
        if isinstance(v, (str, int, float, bool)):
            flat[k] = v
        elif isinstance(v, (list, dict)):
            flat[k] = json.dumps(v)  # store complex types as JSON string
    return flat


def export_to_falkordb(
    store: Any,
    graph_name: str,
    host: str = "localhost",
    port: int = 6379,
    password: str | None = None,
) -> dict:
    """Export all nodes and edges from *store* into a FalkorDB graph.

    Args:
        store:      GraphStore instance (already opened).
        graph_name: Name of the FalkorDB graph to create / update.
        host:       FalkorDB host (default: localhost).
        port:       FalkorDB port (default: 6379).
        password:   Optional Redis AUTH password.

    Returns:
        Stats dict: nodes_written, edges_written, errors.
    """
    try:
        import falkordb
    except ImportError as exc:
        raise ImportError(
            "falkordb package not installed. Run: uv add falkordb"
        ) from exc

    # ── Connect ────────────────────────────────────────────────────────────
    client = falkordb.FalkorDB(host=host, port=port, password=password)
    graph = client.select_graph(graph_name)

    stats = {"nodes_written": 0, "edges_written": 0, "errors": []}

    # ── Export nodes ───────────────────────────────────────────────────────
    # Collect all nodes from every file (same iteration order as visualization)
    all_nodes: list[Any] = []
    seen_qn: set[str] = set()

    for file_path in store.get_all_files():
        for node in store.get_nodes_by_file(file_path):
            if node.qualified_name in seen_qn:
                continue
            seen_qn.add(node.qualified_name)
            all_nodes.append(node)

    # Also pick up nodes whose file has no File-kind node (e.g. GQL-only extract)
    # by scanning all distinct file_paths directly.
    import sqlite3
    conn = store._conn  # access underlying connection
    extra_fps = conn.execute(
        "SELECT DISTINCT file_path FROM nodes WHERE file_path NOT IN "
        "(SELECT DISTINCT file_path FROM nodes WHERE kind = 'File')"
    ).fetchall()
    for row in extra_fps:
        for node in store.get_nodes_by_file(row["file_path"]):
            if node.qualified_name not in seen_qn:
                seen_qn.add(node.qualified_name)
                all_nodes.append(node)

    logger.info("Exporting %d nodes to FalkorDB graph '%s'", len(all_nodes), graph_name)

    for i in range(0, len(all_nodes), _NODE_BATCH):
        batch = all_nodes[i:i + _NODE_BATCH]
        params: list[dict] = []
        for node in batch:
            props: dict[str, Any] = {
                "qualified_name": node.qualified_name,
                "name": node.name,
                "file_path": node.file_path or "",
                "language": node.language or "",
                "line_start": node.line_start or 0,
                "line_end": node.line_end or 0,
                "is_test": bool(node.is_test),
            }
            if node.parent_name:
                props["parent_name"] = node.parent_name
            if node.return_type:
                props["return_type"] = node.return_type
            if node.params:
                props["params"] = node.params
            props.update(_flatten_extra(node.extra))
            params.append({"kind": node.kind, "props": props})

        # Group by kind so each MERGE uses the correct label.
        # FalkorDB does not support dynamic labels in parameterised queries,
        # so we interpolate the label name (safe: values come from our own
        # internal kind enum, never from user input).
        by_node_kind: dict[str, list[dict]] = {}
        for p in params:
            by_node_kind.setdefault(p["kind"], []).append(p["props"])

        for kind, kind_props in by_node_kind.items():
            safe_label = kind.replace("-", "_")  # e.g. GQLField stays GQLField
            try:
                graph.query(
                    f"UNWIND $rows AS row "
                    f"MERGE (n:Node:{safe_label} {{qualified_name: row.qualified_name}}) "
                    f"SET n += row, n.kind = '{safe_label}'",
                    {"rows": kind_props},
                )
                stats["nodes_written"] += len(kind_props)
            except Exception as exc:  # noqa: BLE001
                msg = f"Node batch {kind} {i}–{i + len(kind_props)}: {exc}"
                logger.error(msg)
                stats["errors"].append(msg)

    # ── Export edges ───────────────────────────────────────────────────────
    all_edges = list(store.get_all_edges())
    logger.info("Exporting %d edges to FalkorDB graph '%s'", len(all_edges), graph_name)

    for i in range(0, len(all_edges), _EDGE_BATCH):
        batch = all_edges[i:i + _EDGE_BATCH]
        # Group by kind to emit typed relationships
        by_kind: dict[str, list[dict]] = {}
        for edge in batch:
            # Only emit edges whose endpoints are in the exported node set
            if edge.source_qualified not in seen_qn or edge.target_qualified not in seen_qn:
                continue
            by_kind.setdefault(edge.kind, []).append({
                "src": edge.source_qualified,
                "tgt": edge.target_qualified,
                "file_path": edge.file_path or "",
                "line": edge.line or 0,
            })

        for kind, rows in by_kind.items():
            # Sanitize relationship type (Cypher requires identifier-safe names)
            safe_kind = kind.replace("-", "_")
            try:
                graph.query(
                    f"UNWIND $rows AS row "
                    f"MATCH (a:Node {{qualified_name: row.src}}) "
                    f"MATCH (b:Node {{qualified_name: row.tgt}}) "
                    f"MERGE (a)-[r:{safe_kind}]->(b) "
                    f"SET r.file_path = row.file_path, r.line = row.line",
                    {"rows": rows},
                )
                stats["edges_written"] += len(rows)
            except Exception as exc:  # noqa: BLE001
                msg = f"Edge batch {kind} {i}–{i + len(rows)}: {exc}"
                logger.error(msg)
                stats["errors"].append(msg)

    logger.info(
        "FalkorDB export complete: %d nodes, %d edges, %d errors",
        stats["nodes_written"], stats["edges_written"], len(stats["errors"]),
    )
    return stats
