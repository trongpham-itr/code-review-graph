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

Node labels on FalkorDB: GQLType, GQLField (unchanged),
Code{Kind} for code nodes — CodeFile, CodeFunction, CodeClass, CodeLoader, CodeTest.

Edge relationship types on FalkorDB (see _EDGE_ALIASES for full mapping):
CODE_CONTAINS, CODE_CALLS, CODE_IMPORTS_FROM, CODE_REFERENCES, CODE_TESTED_BY, CODE_BELONGS_TO,
GQL_RESOLVES, GQL_RESOLVES_EXTERNAL, GQL_RESOLVES_REF, GQL_RESOLVES_DELETED,
GQL_OF_TYPE, GQL_RETURNS_TYPE, GQL_USES_INPUT_TYPE, GQL_USES_LOADER, GQL_BACKED_BY, GQL_DELEGATES_TO.

Unified key strategy (no SAME_AS):
  GQLType  → primary key = ``name``  (global across federation)
  GQLField → primary key = ``key``   (= "{service}:{parent}.{field}",
             same format used by graphql_rag so nodes MERGE naturally)
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _load_repo_resolver() -> dict[str, str]:
    """Build repo_folder → canonical_service_name lookup from gh_repo_w_service_name.json."""
    env_path = os.getenv("GH_REPO_MAPPING_FILE")
    if not env_path:
        return {}
    path = Path(env_path)
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return {
        repo_key: entry["name"]
        for repo_key, entry in data.items()
        if entry.get("name")
    }


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

# Canonical edge-type aliases: map SQLite internal kinds → prefixed FalkorDB types.
#   FIELD_OF  → GQL_OF_TYPE          (GQLField -[GQL_OF_TYPE]-> GQLType)
#   RETURNS   → GQL_RETURNS_TYPE     (GQLField -[GQL_RETURNS_TYPE]-> GQLType)
#   ACCEPTS   → GQL_USES_INPUT_TYPE  (GQLField -[GQL_USES_INPUT_TYPE]-> GQLType)
_EDGE_ALIASES: dict[str, str] = {
    # GQL schema edges — align with graphql_rag naming
    "FIELD_OF": "GQL_OF_TYPE",
    "RETURNS": "GQL_RETURNS_TYPE",
    "ACCEPTS": "GQL_USES_INPUT_TYPE",
    # Code structure edges
    "CONTAINS": "CODE_CONTAINS",
    "CALLS": "CODE_CALLS",
    "IMPORTS_FROM": "CODE_IMPORTS_FROM",
    "REFERENCES": "CODE_REFERENCES",
    "TESTED_BY": "CODE_TESTED_BY",
    "BELONGS_TO": "CODE_BELONGS_TO",
    # GQL resolver edges
    "RESOLVES": "GQL_RESOLVES",
    "RESOLVES_EXTERNAL": "GQL_RESOLVES_EXTERNAL",
    "RESOLVES_REF": "GQL_RESOLVES_REF",
    "RESOLVES_DELETED": "GQL_RESOLVES_DELETED",
    "USES_LOADER": "GQL_USES_LOADER",
    "BACKED_BY": "GQL_BACKED_BY",
    "DELEGATES_TO": "GQL_DELEGATES_TO",
}

_NODE_LABEL_PREFIX = "Code"


def _relativize_path(file_path: str | None, monorepo_root: Path) -> str:
    """Strip monorepo_root prefix from file_path, return relative path string."""
    if not file_path:
        return ""
    try:
        return str(Path(file_path).relative_to(monorepo_root))
    except ValueError:
        return file_path


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
    service_name_override: str | None = None,
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
    _now = int(time.time())

    # ── Resolve canonical service name ─────────────────────────────────────
    # Prefer repo_root stored during `build` — CRG_DATA_DIR can move the DB
    # outside the repos directory, making db_path.parent.parent wrong.
    _stored_root = store.get_metadata("repo_root")
    monorepo_root = Path(_stored_root).resolve() if _stored_root else store.db_path.parent.parent.resolve()
    repo_folder = monorepo_root.name
    _repo_resolver = _load_repo_resolver()
    service_name = service_name_override or _repo_resolver.get(repo_folder, repo_folder)
    logger.info("Resolved repo '%s' → service '%s'", repo_folder, service_name)

    # Detect monorepo: only consider sub-folders that actually exist under monorepo_root.
    # Checking all entries in _repo_resolver (which covers the entire system) would
    # incorrectly mark every single-service repo as a monorepo, causing file nodes to
    # receive repo="src" instead of the canonical service name, breaking CODE_BELONGS_TO links.
    _is_monorepo = any(
        (monorepo_root / folder).is_dir()
        and _resolve_service_from_file_path(
            str(monorepo_root / folder), monorepo_root, _repo_resolver, service_name
        ) != service_name
        for folder in _repo_resolver
    )

    # ── Collect nodes ──────────────────────────────────────────────────────
    all_nodes: list[Any] = []
    seen_qn: set[str] = set()

    for file_path in store.get_all_files():
        for node in store.get_nodes_by_file(file_path):
            if node.qualified_name in seen_qn:
                continue
            seen_qn.add(node.qualified_name)
            all_nodes.append(node)

    # Pick up nodes whose file has no File-kind node (e.g. GQL-only extract)
    conn = store._conn
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

    # ── Build lookup: qualified_name → unified key (for edge resolution) ───
    # GQLType  unified key = name
    # GQLField unified key = "{service}:{parent}.{field}"
    # Others   unified key = qualified_name (unchanged)
    qn_to_key: dict[str, str] = {}

    # GQLType ownership (GQL_OWNS_TYPE / GQL_EXTENDS_TYPE) is managed exclusively by
    # graphql_rag which reads the Apollo SDL — the sole source of truth for
    # federation type ownership.  code-review-graph only exports implementation
    # nodes (Function, File, GQLField resolvers, etc.) and must not overwrite
    # ownership edges set by graphql_rag.
    _gql_type_service: list[tuple[str, str]] = []  # kept for CODE_BELONGS_TO service discovery only
    _all_node_services: set[str] = set()  # all service names seen across File nodes

    # Relativized qualified_names of :Node nodes exported this run — used by
    # the reconcile step to identify stale nodes that no longer exist in source.
    _exported_node_qns: set[str] = set()

    # ── Export nodes ───────────────────────────────────────────────────────
    for i in range(0, len(all_nodes), _NODE_BATCH):
        batch = all_nodes[i:i + _NODE_BATCH]

        # Separate GQLType, GQLField, and other nodes — each needs different MERGE key
        gql_types: list[dict] = []
        gql_fields: list[dict] = []
        other_nodes: list[tuple[str, dict]] = []  # (kind, props)

        for node in batch:
            node_service = (
                _resolve_service_from_file_path(
                    node.file_path, monorepo_root, _repo_resolver, service_name
                )
                if _is_monorepo
                else service_name
            )

            # Relativize qualified_name:
            # - File nodes: qualified_name IS the file path
            # - Other nodes: qualified_name is "{abs_file_path}::{symbol}" — relativize only the prefix
            if node.kind == "File":
                _rel_qn = _relativize_path(node.qualified_name, monorepo_root)
            elif "::" in (node.qualified_name or ""):
                _prefix, _suffix = node.qualified_name.split("::", 1)
                _rel_qn = f"{_relativize_path(_prefix, monorepo_root)}::{_suffix}"
            else:
                _rel_qn = node.qualified_name

            _rel_name = (
                _relativize_path(node.name, monorepo_root)
                if node.kind == "File"
                else node.name
            )

            base_props: dict[str, Any] = {
                "qualified_name": _rel_qn,
                "name": _rel_name,
                "file_path": _relativize_path(node.file_path, monorepo_root),
                "language": node.language or "",
                "line_start": node.line_start or 0,
                "line_end": node.line_end or 0,
                "is_test": bool(node.is_test),
                "repo": node_service,
                "updated_at": _now,
            }
            if node.parent_name:
                base_props["parent_name"] = node.parent_name
            if node.return_type:
                base_props["return_type"] = node.return_type
            if node.params:
                base_props["params"] = node.params
            base_props.update(_flatten_extra(node.extra))

            # Track all service names from File nodes for CODE_BELONGS_TO linking.
            # This ensures non-GQL services (pure gRPC/SQS) also get linked.
            if node.kind == "File" and node_service:
                _all_node_services.add(node_service)

            if node.kind == "GQLType":
                # Unified key = name (global across federation)
                unified_key = node.name
                qn_to_key[node.qualified_name] = unified_key
                _gql_type_service.append((node_service, unified_key))
                gql_types.append(base_props)

            elif node.kind == "GQLField":
                # Unified key = "{service}:{parent}.{field}" — matches graphql_rag format
                if node.parent_name and node_service:
                    unified_key = f"{node_service}:{node.parent_name}.{node.name}"
                else:
                    unified_key = node.qualified_name  # fallback
                qn_to_key[node.qualified_name] = unified_key
                base_props["key"] = unified_key
                gql_fields.append(base_props)

            else:
                qn_to_key[node.qualified_name] = _rel_qn
                safe_label = node.kind.replace("-", "_")
                other_nodes.append((safe_label, base_props))
                _exported_node_qns.add(_rel_qn)

        # Export GQLType — MERGE on name, SET qualified_name + code props
        if gql_types:
            try:
                graph.query(
                    "UNWIND $rows AS row "
                    "MERGE (n:GQLType {name: row.name}) "
                    "SET n.qualified_name = row.qualified_name, "
                    "    n.file_path      = row.file_path, "
                    "    n.language       = row.language, "
                    "    n.line_start     = row.line_start, "
                    "    n.line_end       = row.line_end, "
                    "    n.repo           = row.repo, "
                    "    n.kind           = 'GQLType' "
                    "SET n += row",
                    {"rows": gql_types},
                )
                stats["nodes_written"] += len(gql_types)
            except Exception as exc:  # noqa: BLE001
                msg = f"GQLType node batch {i}: {exc}"
                logger.error(msg)
                stats["errors"].append(msg)

        # Export GQLField — MERGE on key (unified with graphql_rag).
        # Only SET implementation-domain properties owned by code-review-graph.
        # Schema-domain properties (description, return_expr, kind) are owned
        # by graphql_rag and must not be overwritten.
        if gql_fields:
            try:
                graph.query(
                    "UNWIND $rows AS row "
                    "MERGE (n:GQLField {key: row.key}) "
                    "SET n.qualified_name = row.qualified_name, "
                    "    n.name           = row.name, "
                    "    n.file_path      = row.file_path, "
                    "    n.language       = row.language, "
                    "    n.line_start     = row.line_start, "
                    "    n.line_end       = row.line_end, "
                    "    n.repo           = row.repo, "
                    "    n.kind           = 'GQLField', "
                    "    n.updated_at     = row.updated_at, "
                    "    n.is_deleted     = COALESCE(row.is_deleted,  n.is_deleted), "
                    "    n.is_external    = COALESCE(row.is_external, n.is_external), "
                    "    n.operation      = COALESCE(row.operation,   n.operation), "
                    "    n.auth           = COALESCE(row.auth,        n.auth), "
                    "    n.directives     = COALESCE(row.directives,  n.directives) ",
                    {"rows": gql_fields},
                )
                stats["nodes_written"] += len(gql_fields)
            except Exception as exc:  # noqa: BLE001
                msg = f"GQLField node batch {i}: {exc}"
                logger.error(msg)
                stats["errors"].append(msg)

        # Export other nodes — MERGE on qualified_name (unchanged)
        by_label: dict[str, list[dict]] = {}
        for label, props in other_nodes:
            by_label.setdefault(label, []).append(props)

        for label, label_props in by_label.items():
            try:
                graph.query(
                    f"UNWIND $rows AS row "
                    f"MERGE (n:Node:{_NODE_LABEL_PREFIX}{label} {{qualified_name: row.qualified_name}}) "
                    f"SET n += row, n.kind = '{label}'",
                    {"rows": label_props},
                )
                stats["nodes_written"] += len(label_props)
            except Exception as exc:  # noqa: BLE001
                msg = f"Node batch {label} {i}: {exc}"
                logger.error(msg)
                stats["errors"].append(msg)

    # ── Export edges ───────────────────────────────────────────────────────
    all_edges = list(store.get_all_edges())
    logger.info("Exporting %d edges to FalkorDB graph '%s'", len(all_edges), graph_name)

    # Pre-build set of (src_key, tgt_key) pairs that have CONTAINS edge,
    # so we can skip REFERENCES for the same pair (CONTAINS is stronger).
    contains_pairs: set[tuple[str, str]] = set()
    for edge in all_edges:
        if edge.kind == "CONTAINS":
            src_key = qn_to_key.get(edge.source_qualified, edge.source_qualified)
            tgt_key = qn_to_key.get(edge.target_qualified, edge.target_qualified)
            contains_pairs.add((src_key, tgt_key))

    for i in range(0, len(all_edges), _EDGE_BATCH):
        batch = all_edges[i:i + _EDGE_BATCH]
        by_kind: dict[str, list[dict]] = {}

        for edge in batch:
            if edge.source_qualified not in seen_qn or edge.target_qualified not in seen_qn:
                continue
            src_key = qn_to_key.get(edge.source_qualified, edge.source_qualified)
            tgt_key = qn_to_key.get(edge.target_qualified, edge.target_qualified)

            # Skip REFERENCES when CONTAINS already exists for same (src, tgt) pair
            if edge.kind == "REFERENCES" and (src_key, tgt_key) in contains_pairs:
                continue

            by_kind.setdefault(edge.kind, []).append({
                "src": src_key,
                "tgt": tgt_key,
                "file_path": _relativize_path(edge.file_path, monorepo_root),
                "line": edge.line or 0,
            })

        for kind, rows in by_kind.items():
            canonical = _EDGE_ALIASES.get(kind, kind)
            safe_kind = canonical.replace("-", "_")
            try:
                # MATCH by unified key for GQLType/GQLField, by qualified_name for others.
                # Strategy: try key first (GQLField), then name (GQLType), then qualified_name.
                graph.query(
                    f"UNWIND $rows AS row "
                    f"OPTIONAL MATCH (a1:GQLField  {{key:            row.src}}) "
                    f"OPTIONAL MATCH (a2:GQLType   {{name:           row.src}}) "
                    f"OPTIONAL MATCH (a3:Node       {{qualified_name: row.src}}) "
                    f"WITH row, coalesce(a1, a2, a3) AS a "
                    f"OPTIONAL MATCH (b1:GQLField  {{key:            row.tgt}}) "
                    f"OPTIONAL MATCH (b2:GQLType   {{name:           row.tgt}}) "
                    f"OPTIONAL MATCH (b3:Node       {{qualified_name: row.tgt}}) "
                    f"WITH row, a, coalesce(b1, b2, b3) AS b "
                    f"WHERE a IS NOT NULL AND b IS NOT NULL "
                    f"MERGE (a)-[r:{safe_kind}]->(b) "
                    f"SET r.file_path = row.file_path, r.line = row.line",
                    {"rows": rows},
                )
                stats["edges_written"] += len(rows)
            except Exception as exc:  # noqa: BLE001
                msg = f"Edge batch {kind} {i}: {exc}"
                logger.error(msg)
                stats["errors"].append(msg)

    # ── Link File nodes to their Service node via BELONGS_TO ──────────────
    # Use all service names observed from File nodes so non-GQL services
    # (pure gRPC/SQS with no GQLType nodes) are also linked correctly.
    all_services: set[str] = _all_node_services or {service_name}
    all_services.update(svc for svc, _ in _gql_type_service)

    for svc in all_services:
        try:
            graph.query(
                "MERGE (s:Service {name: $svc}) "
                "ON CREATE SET s.repo = $svc "
                "SET s.updated_at = $now",
                {"svc": svc, "now": _now},
            )
            graph.query(
                f"MATCH (f:Node:{_NODE_LABEL_PREFIX}File {{repo: $svc}}) "
                "MATCH (s:Service {name: $svc}) "
                f"MERGE (f)-[:{_EDGE_ALIASES['BELONGS_TO']}]->(s)",
                {"svc": svc},
            )
        except Exception as exc:  # noqa: BLE001
            msg = f"{_EDGE_ALIASES['BELONGS_TO']} link for service '{svc}': {exc}"
            logger.error(msg)
            stats["errors"].append(msg)

    # ── Reconcile: delete stale :Node nodes (files/functions removed from source) ──
    # Any :Node with qualified_name NOT in _exported_node_qns but belonging to the
    # current repo(s) is stale — its source file was deleted or the symbol removed.
    stats["nodes_deleted"] = 0
    for svc in all_services:
        try:
            result = graph.query(
                "MATCH (n:Node {repo: $svc}) RETURN n.qualified_name",
                {"svc": svc},
            )
            stale_qns = [
                row[0]
                for row in result.result_set
                if row[0] and row[0] not in _exported_node_qns
            ]
            if stale_qns:
                logger.info(
                    "Reconcile: deleting %d stale nodes for service '%s'",
                    len(stale_qns), svc,
                )
                for j in range(0, len(stale_qns), _NODE_BATCH):
                    graph.query(
                        "UNWIND $qns AS qn "
                        "MATCH (n:Node {qualified_name: qn}) "
                        "DETACH DELETE n",
                        {"qns": stale_qns[j:j + _NODE_BATCH]},
                    )
                stats["nodes_deleted"] += len(stale_qns)
        except Exception as exc:  # noqa: BLE001
            msg = f"Reconcile for service '{svc}': {exc}"
            logger.error(msg)
            stats["errors"].append(msg)

    # Remove fully-isolated Service nodes — no edges in any direction.
    # Real external services (admin-api, AWS LAMBDA, etc.) always have at least
    # one edge (HTTP_CALLS, LAMBDA_CALLS, …) so this only catches stale
    # ghost nodes from a previous build run (e.g. .code-review-graph).
    try:
        orphan_result = graph.query(
            "MATCH (s:Service) WHERE NOT (s)-[]-() RETURN s.name"
        )
        orphan_names = [row[0] for row in orphan_result.result_set if row[0]]
        if orphan_names:
            logger.info("Reconcile: removing %d isolated Service node(s): %s", len(orphan_names), orphan_names)
            graph.query("MATCH (s:Service) WHERE NOT (s)-[]-() DETACH DELETE s")
            stats["nodes_deleted"] += len(orphan_names)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not remove isolated Service nodes: %s", exc)

    # Remove bare monorepo-root Service node (e.g. 'be-repos') — not a real service
    if _is_monorepo and service_name not in _repo_resolver.values():
        try:
            graph.query("MATCH (s:Service {name: $svc}) DETACH DELETE s", {"svc": service_name})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not remove monorepo root service node '%s': %s", service_name, exc)

    # GQL_OWNS_TYPE / GQL_EXTENDS_TYPE are intentionally NOT exported here.
    # graphql_rag (Apollo SDL ingest) is the sole source of truth for
    # federation type ownership and must not be overwritten.

    logger.info(
        "FalkorDB export complete: %d nodes, %d edges, %d deleted, %d errors",
        stats["nodes_written"], stats["edges_written"],
        stats.get("nodes_deleted", 0), len(stats["errors"]),
    )
    return stats
