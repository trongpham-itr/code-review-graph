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
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _load_repo_resolver() -> dict[str, str]:
    """Build repo_folder → canonical_service_name lookup from service-map-name.json."""
    env_path = os.getenv("SERVICE_MAP_NAME_PATH")
    candidates = [Path(env_path)] if env_path else []
    for parent in [Path(os.getcwd()), *Path(os.getcwd()).parents]:
        candidates.append(parent / "scripts" / "repo" / "service-map-name.json")
    for path in candidates:
        if path.exists():
            data = json.loads(path.read_text())
            return {
                v["repo"]: k
                for k, v in data.get("services", {}).items()
                if v.get("repo")
            }
    return {}


def _resolve_service_from_file_path(
    file_path: str | None,
    monorepo_root: Path,
    resolver: dict[str, str],
    fallback: str,
) -> str:
    """Extract canonical service name from a file path inside a monorepo.

    For a monorepo layout like ``be-repos/btcy-bioflux-backend-clinic_api/…``,
    the immediate sub-folder is looked up in *resolver* to get the canonical
    service name.  Falls back to *fallback* when the path is outside the
    monorepo or the folder is not in *resolver*.
    """
    if not file_path:
        return fallback
    try:
        rel = Path(file_path).relative_to(monorepo_root)
        folder = rel.parts[0]  # e.g. 'btcy-bioflux-backend-clinic_api'
        return resolver.get(folder, folder)
    except ValueError:
        return fallback

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

    # ── Resolve canonical service name ─────────────────────────────────────
    monorepo_root = store.db_path.parent.parent.resolve()
    repo_folder = monorepo_root.name
    _repo_resolver = _load_repo_resolver()
    service_name = _repo_resolver.get(repo_folder, repo_folder)
    logger.info("Resolved repo '%s' → service '%s'", repo_folder, service_name)

    # Detect monorepo: if resolver contains sub-folder entries that resolve to
    # different service names, we are in a multi-service monorepo layout.
    _is_monorepo = any(
        _resolve_service_from_file_path(str(monorepo_root / folder), monorepo_root, _repo_resolver, service_name) != service_name
        for folder in _repo_resolver
    )

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

    # Track (service_name, qualified_name) for GQLType nodes to link after export
    _gql_type_service: list[tuple[str, str]] = []

    for i in range(0, len(all_nodes), _NODE_BATCH):
        batch = all_nodes[i:i + _NODE_BATCH]
        params: list[dict] = []
        for node in batch:
            # Resolve per-node service for monorepo layouts
            node_service = (
                _resolve_service_from_file_path(node.file_path, monorepo_root, _repo_resolver, service_name)
                if _is_monorepo
                else service_name
            )
            props: dict[str, Any] = {
                "qualified_name": node.qualified_name,
                "name": node.name,
                "file_path": node.file_path or "",
                "language": node.language or "",
                "line_start": node.line_start or 0,
                "line_end": node.line_end or 0,
                "is_test": bool(node.is_test),
                "repo": node_service,
            }
            if node.parent_name:
                props["parent_name"] = node.parent_name
            if node.return_type:
                props["return_type"] = node.return_type
            if node.params:
                props["params"] = node.params
            props.update(_flatten_extra(node.extra))
            params.append({"kind": node.kind, "props": props})

            if node.kind == "GQLType":
                _gql_type_service.append((node_service, node.qualified_name))

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

    # ── Link File nodes to their Service node ─────────────────────────────
    # Collect all unique service names seen across nodes
    all_services: set[str] = {service_name}
    if _is_monorepo:
        all_services.update(svc for svc, _ in _gql_type_service)

    for svc in all_services:
        try:
            graph.query(
                "MERGE (s:Service {name: $svc}) ON CREATE SET s.repo = $svc",
                {"svc": svc},
            )
            graph.query(
                "MATCH (f:Node:File {repo: $svc}) "
                "MATCH (s:Service {name: $svc}) "
                "MERGE (f)-[:BELONGS_TO]->(s)",
                {"svc": svc},
            )
        except Exception as exc:  # noqa: BLE001
            msg = f"BELONGS_TO link for service '{svc}': {exc}"
            logger.error(msg)
            stats["errors"].append(msg)

    # Skip creating a bare monorepo-root Service node (e.g. 'be-repos') — it is
    # not a real service and would pollute the graph.
    if _is_monorepo and service_name not in _repo_resolver.values():
        try:
            graph.query("MATCH (s:Service {name: $svc}) DETACH DELETE s", {"svc": service_name})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not remove monorepo root service node '%s': %s", service_name, exc)

    # ── Link GQLType nodes to their Service via OWNS_TYPE ─────────────────
    if _gql_type_service:
        _GQLTYPE_BATCH = 200
        for i in range(0, len(_gql_type_service), _GQLTYPE_BATCH):
            rows = [
                {"svc": svc, "qn": qn}
                for svc, qn in _gql_type_service[i:i + _GQLTYPE_BATCH]
            ]
            try:
                graph.query(
                    "UNWIND $rows AS row "
                    "MATCH (t:Node:GQLType {qualified_name: row.qn}) "
                    "MERGE (s:Service {name: row.svc}) ON CREATE SET s.repo = row.svc "
                    "MERGE (s)-[:OWNS_TYPE]->(t)",
                    {"rows": rows},
                )
            except Exception as exc:  # noqa: BLE001
                msg = f"OWNS_TYPE batch {i}: {exc}"
                logger.error(msg)
                stats["errors"].append(msg)

    logger.info(
        "FalkorDB export complete: %d nodes, %d edges, %d errors",
        stats["nodes_written"], stats["edges_written"], len(stats["errors"]),
    )
    return stats
