"""GraphQL-specific MCP tools.

Tools 23–24:
  23. list_gql_fields  – list GQLField nodes (Query/Mutation/type fields) with auth/type info
  24. get_gql_field    – full resolver chain for one field: resolver fn, return type,
                        accepted inputs, loaders used, auth requirements
"""

from __future__ import annotations

from typing import Any, Optional

from ._common import _get_store

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _field_summary(node: Any) -> dict:
    """Compact dict for a single GQLField node."""
    extra = node.extra or {}
    return {
        "field": f"{node.parent_name}.{node.name}",
        "operation": extra.get("operation", "type_field"),
        "return_type": extra.get("return_type", ""),
        "return_type_is_list": extra.get("return_type_is_list", False),
        "auth": extra.get("auth", {}),
        "directives": extra.get("directives", []),
    }


# ---------------------------------------------------------------------------
# Tool 23: list_gql_fields
# ---------------------------------------------------------------------------


def list_gql_fields(
    operation: Optional[str] = None,
    auth_group: Optional[str] = None,
    auth_role: Optional[str] = None,
    repo_root: Optional[str] = None,
    detail_level: str = "standard",
) -> dict[str, Any]:
    """List all GraphQL fields extracted from .schema.gql.

    [EXPLORE] Returns GQLField nodes with operation type, return type,
    and auth requirements. Useful for auditing API surface area and
    access control coverage.

    Args:
        operation: Filter by operation type: "query", "mutation",
                   "subscription", or "type_field". Omit to return all.
        auth_group: Filter fields that require a specific auth group
                    (e.g. "ALL_CLINIC").
        auth_role: Filter fields that require a specific role
                   (e.g. "FACILITY_ADMIN").
        repo_root: Repository root path. Auto-detected if omitted.
        detail_level: "standard" (full list) or "minimal" (counts only).

    Returns:
        List of GQLField nodes with their operation type, return type,
        and auth metadata. In minimal mode returns only counts per operation.
    """
    store, _root = _get_store(repo_root)
    try:
        from ..graph import GraphNode

        all_nodes = store.get_nodes_by_kind(["GQLField"])
        fields = [n for n in all_nodes if isinstance(n, GraphNode)]

        # Apply filters
        if operation:
            fields = [
                n for n in fields
                if (n.extra or {}).get("operation", "type_field") == operation
            ]
        if auth_group:
            fields = [
                n for n in fields
                if auth_group in (n.extra or {}).get("auth", {}).get("groups", [])
            ]
        if auth_role:
            fields = [
                n for n in fields
                if auth_role in (n.extra or {}).get("auth", {}).get("roles", [])
            ]

        if detail_level == "minimal":
            counts: dict[str, int] = {}
            no_auth = 0
            for n in fields:
                op = (n.extra or {}).get("operation", "type_field")
                counts[op] = counts.get(op, 0) + 1
                auth = (n.extra or {}).get("auth", {})
                if not auth.get("groups") and not auth.get("roles"):
                    no_auth += 1
            return {
                "status": "ok",
                "summary": f"{len(fields)} GQLField(s) found",
                "counts_by_operation": counts,
                "fields_without_auth": no_auth,
                "next_tool_suggestions": [
                    "get_gql_field_tool(field_name=<name>) for resolver chain",
                    "list_gql_fields_tool(operation='query') for Query fields only",
                ],
            }

        field_list = [_field_summary(n) for n in fields]

        return {
            "status": "ok",
            "summary": f"{len(field_list)} GQLField(s) found",
            "fields": field_list,
            "next_tool_suggestions": [
                "get_gql_field_tool(field_name=<name>) for full resolver chain",
                "query_graph_tool(pattern='resolvers_of', target=<field>) for resolver",
            ],
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 24: get_gql_field
# ---------------------------------------------------------------------------


def get_gql_field(
    field_name: str,
    parent_type: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Get the full resolver chain for a single GraphQL field.

    [EXPLORE] Given a field name (e.g. "inboxes" or "Query.inboxes"),
    returns:
      - auth requirements (groups / roles)
      - return type and whether it is a list
      - accepted input types (ACCEPTS edges)
      - resolver function name and file (RESOLVES edge)
      - loaders used by the resolver (USES_LOADER edges)
      - batch functions backing each loader (BACKED_BY edges)
      - federation info (is_federation_key, __resolveReference if present)

    Args:
        field_name: Field name (e.g. "inboxes") or fully qualified
                    "ParentType.fieldName" (e.g. "Query.inboxes").
        parent_type: Optional parent type filter (e.g. "Query", "Mutation")
                     when field_name is ambiguous.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Full resolver chain with auth, types, resolver fn, and loaders.
    """
    store, _root = _get_store(repo_root)
    try:
        # ── resolve field node ─────────────────────────────────────────────
        # Accept "Query.inboxes" or bare "inboxes"
        resolved_parent: Optional[str] = parent_type
        resolved_name = field_name
        if "." in field_name:
            parts = field_name.split(".", 1)
            resolved_parent, resolved_name = parts[0], parts[1]

        candidates = store.search_nodes(resolved_name, limit=50)
        field_nodes = [
            n for n in candidates
            if n.kind == "GQLField" and n.name == resolved_name
        ]
        if resolved_parent:
            field_nodes = [n for n in field_nodes if n.parent_name == resolved_parent]

        if not field_nodes:
            return {
                "status": "not_found",
                "summary": f"No GQLField found matching '{field_name}'.",
                "hint": (
                    "Use list_gql_fields_tool() to browse available fields, "
                    "or build_or_update_graph_tool() if the graph is stale."
                ),
            }

        if len(field_nodes) > 1:
            return {
                "status": "ambiguous",
                "summary": (
                    f"Multiple GQLFields named '{resolved_name}'. "
                    "Specify parent_type to disambiguate."
                ),
                "candidates": [
                    {"field": f"{n.parent_name}.{n.name}", "file": n.file_path}
                    for n in field_nodes
                ],
            }

        field_node = field_nodes[0]
        extra = field_node.extra or {}
        field_qn = field_node.qualified_name

        result: dict[str, Any] = {
            "status": "ok",
            "field": f"{field_node.parent_name}.{field_node.name}",
            "operation": extra.get("operation", "type_field"),
            "return_type": extra.get("return_type", ""),
            "return_type_is_list": extra.get("return_type_is_list", False),
            "return_type_nullable": extra.get("return_type_nullable", True),
            "auth": extra.get("auth", {}),
            "directives": extra.get("directives", []),
            "schema_file": field_node.file_path,
        }

        # ── ACCEPTS: input types ───────────────────────────────────────────
        accepts: list[str] = []
        for e in store.get_edges_by_source(field_qn):
            if e.kind == "ACCEPTS":
                type_node = store.get_node(e.target_qualified)
                accepts.append(type_node.name if type_node else e.target_qualified.split("::")[-1])
        result["accepts_types"] = accepts

        # ── RESOLVES / RESOLVES_EXTERNAL: resolver function ───────────────
        resolvers: list[dict] = []
        for e in store.get_edges_by_target(field_qn):
            if e.kind not in ("RESOLVES", "RESOLVES_EXTERNAL"):
                continue
            fn_node = store.get_node(e.source_qualified)
            if fn_node:
                resolver_info: dict[str, Any] = {
                    "function": fn_node.name,
                    "file": fn_node.file_path,
                    "line": fn_node.line_start,
                    "qualified_name": fn_node.qualified_name,
                    "is_external_field": e.kind == "RESOLVES_EXTERNAL",
                }

                # ── USES_LOADER: loaders used by resolver ──────────────
                loaders_used: list[dict] = []
                for le in store.get_edges_by_source(fn_node.qualified_name):
                    if le.kind == "USES_LOADER":
                        loader_node = store.get_node(le.target_qualified)
                        loader_info: dict[str, Any] = {
                            "loader": loader_node.name if loader_node else le.target_qualified.split("::")[-1],
                        }
                        if loader_node:
                            batch_fn = (loader_node.extra or {}).get("batch_function", "")
                            loader_info["batch_function"] = batch_fn
                            # BACKED_BY: batch function location
                            for be in store.get_edges_by_source(loader_node.qualified_name):
                                if be.kind == "BACKED_BY":
                                    batch_node = store.get_node(be.target_qualified)
                                    if batch_node:
                                        loader_info["batch_file"] = batch_node.file_path
                                        loader_info["batch_line"] = batch_node.line_start
                        loaders_used.append(loader_info)

                resolver_info["loaders"] = loaders_used
                resolvers.append(resolver_info)

        result["resolvers"] = resolvers

        # ── RESOLVES_REF: federation __resolveReference ────────────────────
        # (only relevant for entity types — check parent GQLType)
        resolve_refs: list[str] = []
        parent_type_qn = f"{field_node.file_path}::{field_node.parent_name}"
        for e in store.get_edges_by_target(parent_type_qn):
            if e.kind == "RESOLVES_REF":
                resolve_refs.append(e.source_qualified.split("::")[-1])
        if resolve_refs:
            result["resolve_reference_fns"] = resolve_refs

        # ── Summary ───────────────────────────────────────────────────────
        auth = result["auth"]
        auth_str = (
            f"groups={auth.get('groups', [])} roles={auth.get('roles', [])}"
            if (auth.get("groups") or auth.get("roles"))
            else "public (no auth)"
        )
        resolver_names = [r["function"] for r in resolvers]
        result["summary"] = (
            f"{result['field']} → {result['return_type']}"
            f"{'[]' if result['return_type_is_list'] else ''} | "
            f"auth: {auth_str} | "
            f"resolver: {', '.join(resolver_names) or 'none found'}"
        )
        result["next_tool_suggestions"] = [
            f"query_graph_tool(pattern='callees_of', target='{resolver_names[0]}') to trace resolver internals"
            if resolver_names else "build_or_update_graph_tool() to rebuild graph",
            "list_gql_fields_tool(operation='mutation') to see all mutations",
        ]

        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()
