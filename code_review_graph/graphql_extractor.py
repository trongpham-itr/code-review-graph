"""GraphQL graph extension.

Extracts GQLField, GQLType, and Loader nodes, plus RESOLVES, RESOLVES_EXTERNAL,
FIELD_OF, RETURNS, ACCEPTS, USES_LOADER, BACKED_BY, and RESOLVES_REF edges from
GraphQL services found in the repository.

Data sources (per service):
  .schema.gql              → GQLField, GQLType nodes; FIELD_OF, RETURNS, ACCEPTS edges
  resolvers/index.js       → type→resolver-file mapping (lookup only)
  resolvers/*.js           → RESOLVES, RESOLVES_EXTERNAL, USES_LOADER, RESOLVES_REF edges
  utils/loaders/index.js   → Loader nodes; BACKED_BY edges

RESOLVES vs RESOLVES_EXTERNAL:
  RESOLVES          — resolver Function → GQLField defined in this service's schema
  RESOLVES_EXTERNAL — resolver Function → GQLField from a federated (external) service;
                      the target GQLField node is a synthetic placeholder (is_external=True)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from graphql import parse as gql_parse
from graphql.language import ast as gql_ast

from .graph import GraphStore
from .parser import EdgeInfo, NodeInfo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Built-in GraphQL scalars — not stored as GQLType nodes
BUILTIN_SCALARS: frozenset[str] = frozenset(
    {
        "String",
        "Int",
        "Float",
        "Boolean",
        "ID",
        "DateTime",
        "Date",
        "Time",
        "JSON",
        "JSONObject",
        "Upload",
        "Long",
        "BigInt",
        "BigDecimal",
        "UUID",
        "Void",
        "Timestamp",
    }
)

# Apollo Federation internals — skip entirely
_FEDERATION_SKIP: frozenset[str] = frozenset(
    {"_Entity", "_Service", "_Any", "_FieldSet"}
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def extract_graphql_for_repo(repo_root: Path, store: GraphStore) -> dict:
    """Scan *repo_root* for GraphQL services and extract nodes/edges into *store*.

    A "GraphQL service" is any directory that contains a ``.schema.gql`` file
    at its root level.

    Returns a stats dict with keys:
        services, gql_types, gql_fields, loaders, edges, errors
    """
    schema_files = list(repo_root.rglob(".schema.gql"))
    if not schema_files:
        return {"services": 0, "gql_types": 0, "gql_fields": 0, "loaders": 0, "edges": 0, "errors": []}

    stats: dict = {"services": 0, "gql_types": 0, "gql_fields": 0, "loaders": 0, "edges": 0, "errors": []}
    for schema_file in schema_files:
        try:
            svc_stats = _extract_service(schema_file.parent, schema_file, store)
            stats["services"] += 1
            for k in ("gql_types", "gql_fields", "loaders", "edges"):
                stats[k] += svc_stats.get(k, 0)
        except Exception as exc:
            logger.warning("GraphQL extraction failed for %s: %s", schema_file, exc)
            stats["errors"].append({"file": str(schema_file), "error": str(exc)})

    logger.info(
        "GraphQL extraction: %d service(s), %d types, %d fields, %d loaders, %d edges",
        stats["services"],
        stats["gql_types"],
        stats["gql_fields"],
        stats["loaders"],
        stats["edges"],
    )
    return stats


# ---------------------------------------------------------------------------
# Per-service extraction
# ---------------------------------------------------------------------------


def _extract_service(service_root: Path, schema_file: Path, store: GraphStore) -> dict:
    """Extract all GraphQL graph data for one service."""
    schema_path = str(schema_file)
    stats = {"gql_types": 0, "gql_fields": 0, "loaders": 0, "edges": 0}

    # ── Step 0: register File node for .schema.gql ────────────────────────
    # export_graph_data iterates over kind='File' nodes, so GQLType/GQLField
    # nodes are invisible in visualization unless a File node exists for this path.
    store.upsert_node(NodeInfo(
        kind="File",
        name=str(schema_file),
        file_path=schema_path,
        line_start=1,
        line_end=0,
        language="graphql",
    ))

    # ── Step 1: parse .schema.gql ──────────────────────────────────────────
    try:
        source = schema_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Cannot read %s: %s", schema_file, exc)
        return stats

    type_defs, field_defs = _parse_schema_gql(source)

    # Map type_name → qualified_name for edge targets
    gql_type_qn: dict[str, str] = {}

    for td in type_defs:
        node = NodeInfo(
            kind="GQLType",
            name=td["name"],
            file_path=schema_path,
            line_start=td.get("line", 0),
            line_end=td.get("line_end", 0),
            language="graphql",
            extra={
                "type_kind": td["type_kind"],
                "is_shareable": td.get("is_shareable", False),
                "is_external": td.get("is_external", False),
                "is_federation_entity": td.get("is_federation_entity", False),
                "key_fields": td.get("key_fields", ""),
                "description": td.get("description", ""),
            },
        )
        store.upsert_node(node)
        gql_type_qn[td["name"]] = f"{schema_path}::{td['name']}"
        stats["gql_types"] += 1

    for fd in field_defs:
        parent_type = fd["parent_type"]
        field_name = fd["name"]
        field_qn = f"{schema_path}::{parent_type}.{field_name}"

        node = NodeInfo(
            kind="GQLField",
            name=field_name,
            file_path=schema_path,
            line_start=fd.get("line", 0),
            line_end=fd.get("line", 0),
            language="graphql",
            parent_name=parent_type,
            return_type=fd.get("return_type"),
            extra={
                "operation": fd.get("operation", "type_field"),
                "return_type_is_list": fd.get("return_type_is_list", False),
                "return_type_nullable": fd.get("return_type_nullable", True),
                "auth": fd.get("auth", {}),
                "directives": fd.get("directives", []),
                "is_federation_key": fd.get("is_federation_key", False),
                "description": fd.get("description", ""),
            },
        )
        store.upsert_node(node)
        stats["gql_fields"] += 1

        # FIELD_OF: GQLField → GQLType(parent)
        parent_qn = gql_type_qn.get(parent_type, f"{schema_path}::{parent_type}")
        store.upsert_edge(
            EdgeInfo(kind="FIELD_OF", source=field_qn, target=parent_qn, file_path=schema_path, line=fd.get("line", 0))
        )
        stats["edges"] += 1

        # RETURNS: GQLField → GQLType(return_type)
        ret = fd.get("return_type")
        if ret and ret not in BUILTIN_SCALARS:
            ret_qn = gql_type_qn.get(ret, f"{schema_path}::{ret}")
            store.upsert_edge(
                EdgeInfo(kind="RETURNS", source=field_qn, target=ret_qn, file_path=schema_path, line=fd.get("line", 0))
            )
            stats["edges"] += 1

        # ACCEPTS: GQLField → GQLType(arg_type) for each custom arg
        for arg_type in fd.get("arg_types", []):
            if arg_type not in BUILTIN_SCALARS:
                arg_qn = gql_type_qn.get(arg_type, f"{schema_path}::{arg_type}")
                store.upsert_edge(
                    EdgeInfo(kind="ACCEPTS", source=field_qn, target=arg_qn, file_path=schema_path, line=fd.get("line", 0))
                )
                stats["edges"] += 1

    # ── Step 2: parse resolvers/index.js ───────────────────────────────────
    resolver_index = _find_file(service_root, [
        "app/resolvers/index.js",
        "resolvers/index.js",
        "app/graphql/resolvers/index.js",
    ])
    if resolver_index:
        try:
            index_source = resolver_index.read_text(encoding="utf-8", errors="replace")
        except OSError:
            index_source = ""

        type_to_file = _parse_resolver_index(index_source, resolver_index.parent)

        # ── Step 3: process each resolver file ─────────────────────────────
        for type_name, resolver_file in type_to_file.items():
            if not resolver_file.exists():
                continue
            try:
                res_source = resolver_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            resolver_path = str(resolver_file)
            exports = _parse_resolver_exports(res_source)

            # Build a unified list of (field_key, func_name, file_path) for edge creation.
            # For direct exports the file_path is the resolver file itself.
            # For spread aggregators (exports == {}) we follow ...spread one level deep
            # so each item carries the child file path where the function is defined.
            if exports:
                edge_items: list[tuple[str, str, str]] = [
                    (fk, fn, resolver_path) for fk, fn in exports.items()
                ]
            else:
                edge_items = _collect_spread_resolver_exports(res_source, resolver_file.parent)

            # Build import map for this resolver file so we can resolve functions
            # that are imported from other files (e.g. utils/controllers).
            # For spread child files, build their import maps lazily on first use.
            res_import_map = _build_import_map(res_source, resolver_file.parent)
            spread_import_maps: dict[str, dict[str, str]] = {}

            def _get_import_map(fp: str) -> dict[str, str]:
                if fp == resolver_path:
                    return res_import_map
                if fp not in spread_import_maps:
                    try:
                        sp_src = Path(fp).read_text(encoding="utf-8", errors="replace")
                        spread_import_maps[fp] = _build_import_map(sp_src, Path(fp).parent)
                    except OSError:
                        spread_import_maps[fp] = {}
                return spread_import_maps[fp]

            for field_key, func_name, file_path in edge_items:
                fn_qn = _resolve_fn_qn(func_name, file_path, store, _get_import_map(file_path))

                if field_key == "__resolveReference":
                    # RESOLVES_REF: Function → GQLType (entity type)
                    type_qn = gql_type_qn.get(type_name, f"{schema_path}::{type_name}")
                    store.upsert_edge(
                        EdgeInfo(kind="RESOLVES_REF", source=fn_qn, target=type_qn, file_path=file_path)
                    )
                    stats["edges"] += 1
                else:
                    # RESOLVES / RESOLVES_EXTERNAL: Function → GQLField
                    # If the field exists in local schema → RESOLVES.
                    # If not (federation-linked field from another service) →
                    # create a synthetic placeholder GQLField (is_external=True)
                    # and emit RESOLVES_EXTERNAL so the chain stays traversable.
                    field_qn = f"{schema_path}::{type_name}.{field_key}"
                    if store.get_node(field_qn) is None:
                        store.upsert_node(NodeInfo(
                            kind="GQLField",
                            name=field_key,
                            file_path=schema_path,
                            line_start=0,
                            line_end=0,
                            language="graphql",
                            parent_name=type_name,
                            extra={"is_external": True, "operation": "type_field"},
                        ))
                        store.upsert_edge(
                            EdgeInfo(kind="RESOLVES_EXTERNAL", source=fn_qn, target=field_qn, file_path=file_path)
                        )
                        logger.debug("RESOLVES_EXTERNAL: %s → %s (federation field)", fn_qn, field_qn)
                    else:
                        store.upsert_edge(
                            EdgeInfo(kind="RESOLVES", source=fn_qn, target=field_qn, file_path=file_path)
                        )
                    stats["edges"] += 1

            # USES_LOADER: Function → Loader (detect loaders.X.load() calls)
            loaders_index_path = _find_loaders_index_path(service_root)
            if loaders_index_path:
                for fn_name, loader_name in _find_loaders_usage(res_source):
                    fn_qn = f"{resolver_path}::{fn_name}"
                    loader_qn = f"{loaders_index_path}::loaders.{loader_name}"
                    store.upsert_edge(
                        EdgeInfo(kind="USES_LOADER", source=fn_qn, target=loader_qn, file_path=resolver_path)
                    )
                    stats["edges"] += 1

            # DELEGATES_TO: resolver Function → datasource Function
            # Scan the resolver file itself, then any spread child files
            # (e.g. mutation/event.js) that weren't scanned directly.
            _delegate_sources: list[tuple[str, str]] = [
                (resolver_path, res_source)
            ]
            child_paths = {fp for _, _, fp in edge_items if fp != resolver_path}
            for child_path in child_paths:
                try:
                    child_src = Path(child_path).read_text(
                        encoding="utf-8", errors="replace"
                    )
                    _delegate_sources.append((child_path, child_src))
                except OSError:
                    pass

            for _dp, _ds in _delegate_sources:
                for fn_name, ds_method in _find_datasource_calls(_ds):
                    ds_qn = _find_datasource_function_qualified(
                        store, ds_method, str(service_root)
                    )
                    if ds_qn:
                        fn_qn = f"{_dp}::{fn_name}"
                        store.upsert_edge(
                            EdgeInfo(
                                kind="DELEGATES_TO",
                                source=fn_qn,
                                target=ds_qn,
                                file_path=_dp,
                            )
                        )
                        stats["edges"] += 1

    # ── Step 4: parse utils/loaders/index.js ───────────────────────────────
    loaders_index = _find_file(
        service_root, [
            "app/utils/loaders/index.js",
            "utils/loaders/index.js",
            "app/datasources/loaders/index.js",
            "app/graphql/loaders/index.js",
        ]
    )
    if loaders_index:
        try:
            loaders_source = loaders_index.read_text(encoding="utf-8", errors="replace")
        except OSError:
            loaders_source = ""

        loaders_path = str(loaders_index)
        # Register File node so Loader nodes appear in visualization
        store.upsert_node(NodeInfo(
            kind="File",
            name=str(loaders_index),
            file_path=loaders_path,
            line_start=1,
            line_end=0,
            language="javascript",
        ))
        for loader_info in _parse_loaders_index(loaders_source):
            loader_name = loader_info["loader_name"]
            batch_fn = loader_info["batch_function"]

            # Upsert Loader node
            store.upsert_node(
                NodeInfo(
                    kind="Loader",
                    name=loader_name,
                    file_path=loaders_path,
                    line_start=loader_info.get("line", 0),
                    line_end=loader_info.get("line", 0),
                    language="javascript",
                    parent_name="loaders",
                    extra={"batch_function": batch_fn},
                )
            )
            stats["loaders"] += 1

            # BACKED_BY: Loader → Function(batch_fn)
            batch_fn_qn = _find_function_qualified(store, batch_fn, str(service_root))
            if batch_fn_qn:
                loader_qn = f"{loaders_path}::loaders.{loader_name}"
                store.upsert_edge(
                    EdgeInfo(kind="BACKED_BY", source=loader_qn, target=batch_fn_qn, file_path=loaders_path)
                )
                stats["edges"] += 1

    return stats


# ---------------------------------------------------------------------------
# .schema.gql parser (graphql-core)
# ---------------------------------------------------------------------------

_GQL_TYPE_KIND_MAP: dict[type, str] = {
    gql_ast.ObjectTypeDefinitionNode: "type",
    gql_ast.ObjectTypeExtensionNode: "type",
    gql_ast.InputObjectTypeDefinitionNode: "input",
    gql_ast.InputObjectTypeExtensionNode: "input",
    gql_ast.EnumTypeDefinitionNode: "enum",
    gql_ast.EnumTypeExtensionNode: "enum",
    gql_ast.InterfaceTypeDefinitionNode: "interface",
    gql_ast.InterfaceTypeExtensionNode: "interface",
    gql_ast.UnionTypeDefinitionNode: "union",
    gql_ast.UnionTypeExtensionNode: "union",
    gql_ast.ScalarTypeDefinitionNode: "scalar",
    gql_ast.ScalarTypeExtensionNode: "scalar",
}


def _unwrap_gql_type_node(node: object) -> tuple[str, bool, bool]:
    """Unwrap ``NonNullTypeNode`` / ``ListTypeNode`` → ``(base_name, is_list, is_nullable)``."""
    is_list = False
    nullable = True
    while True:
        if isinstance(node, gql_ast.NonNullTypeNode):
            nullable = False
            node = node.type  # type: ignore[attr-defined]
        elif isinstance(node, gql_ast.ListTypeNode):
            is_list = True
            node = node.type  # type: ignore[attr-defined]
        else:
            break
    return node.name.value, is_list, nullable  # type: ignore[attr-defined]


def _parse_schema_gql(source: str) -> tuple[list[dict], list[dict]]:
    """Parse GraphQL SDL source using graphql-core.

    Returns ``(type_defs, field_defs)`` where each entry is a plain dict.
    graphql-core handles comments, block strings, multiline args, and all
    directive syntax natively — no regex preprocessing needed.
    """
    try:
        doc = gql_parse(source)
    except Exception as exc:
        logger.warning("Failed to parse GraphQL SDL: %s", exc)
        return [], []

    type_defs: list[dict] = []
    field_defs: list[dict] = []

    for defn in doc.definitions:
        type_kind = _GQL_TYPE_KIND_MAP.get(type(defn))
        if type_kind is None:
            continue  # SchemaDefinitionNode, DirectiveDefinitionNode, etc.

        type_name = defn.name.value

        # Skip federation internals and injected namespace types
        if (
            type_name in _FEDERATION_SKIP
            or type_name.startswith(("link__", "federation__", "_"))
        ):
            continue
        if type_kind == "scalar" and type_name in BUILTIN_SCALARS:
            continue

        # Line numbers from AST token locations
        line_start = defn.loc.start_token.line if defn.loc else 0
        line_end = defn.loc.end_token.line if defn.loc else line_start

        # Type-level directive names
        type_dir_names = {d.name.value for d in (defn.directives or [])}

        # @key(fields: "...") → federation entity
        key_dir = next((d for d in (defn.directives or []) if d.name.value == "key"), None)
        key_fields = ""
        if key_dir:
            for arg in key_dir.arguments:
                if arg.name.value == "fields":
                    key_fields = arg.value.value  # StringValueNode
                    break

        type_defs.append({
            "name": type_name,
            "type_kind": type_kind,
            "is_shareable": "shareable" in type_dir_names,
            "is_external": "external" in type_dir_names,
            "is_federation_entity": key_dir is not None,
            "key_fields": key_fields,
            "line": line_start,
            "line_end": line_end,
            "description": getattr(defn, "description", None) and defn.description.value or "",
        })

        # Fields: only for type / input / interface
        if type_kind not in ("type", "input", "interface"):
            continue
        if not getattr(defn, "fields", None):
            continue

        op = (
            "query" if type_name == "Query"
            else "mutation" if type_name == "Mutation"
            else "subscription" if type_name == "Subscription"
            else "type_field"
        )

        for fdef in defn.fields:
            field_name = fdef.name.value
            if field_name.startswith("_"):
                continue

            return_type, is_list, nullable = _unwrap_gql_type_node(fdef.type)
            if return_type in _FEDERATION_SKIP:
                continue

            # Custom arg types only (skip builtins)
            arg_types: list[str] = []
            for arg in getattr(fdef, "arguments", None) or []:
                base, _, _ = _unwrap_gql_type_node(arg.type)
                if base not in BUILTIN_SCALARS and base not in _FEDERATION_SKIP and base not in arg_types:
                    arg_types.append(base)

            # @auth(groups: [...], roles: [...])
            auth: dict = {}
            auth_dir = next((d for d in (fdef.directives or []) if d.name.value == "auth"), None)
            if auth_dir:
                for arg in auth_dir.arguments:
                    if hasattr(arg.value, "values"):  # ListValueNode
                        auth[arg.name.value] = [
                            v.value for v in arg.value.values if hasattr(v, "value")
                        ]

            field_defs.append({
                "name": field_name,
                "parent_type": type_name,
                "operation": op,
                "return_type": return_type,
                "return_type_is_list": is_list,
                "return_type_nullable": nullable,
                "arg_types": arg_types,
                "directives": [f"@{d.name.value}" for d in (fdef.directives or [])],
                "auth": auth,
                "is_federation_key": any(d.name.value == "key" for d in (fdef.directives or [])),
                "line": fdef.loc.start_token.line if fdef.loc else line_start,
                "description": fdef.description.value if fdef.description else "",
            })

    return type_defs, field_defs


# ---------------------------------------------------------------------------
# Tree-sitter JavaScript parser (replaces regex-based JS parsers)
# ---------------------------------------------------------------------------

_JS_PARSER_INIT: bool = False
_JS_PARSER: object = None


def _get_js_parser():
    """Lazy-load the JS tree-sitter parser (safe under CPython GIL)."""
    global _JS_PARSER, _JS_PARSER_INIT
    if not _JS_PARSER_INIT:
        _JS_PARSER_INIT = True
        try:
            import tree_sitter_language_pack as _tslp
            _JS_PARSER = _tslp.get_parser("javascript")
        except Exception as exc:
            logger.warning("tree-sitter JS parser unavailable — JS edges will be empty: %s", exc)
    return _JS_PARSER


# ── Low-level AST utilities ──────────────────────────────────────────────────


def _ts_text(node) -> str:
    """Decode a tree-sitter node's bytes to str."""
    return node.text.decode("utf-8", errors="replace") if node.text else ""


def _iter_type(node, types: frozenset):
    """Yield every descendant node (including self) whose type is in *types* (DFS)."""
    if node.type in types:
        yield node
    for child in node.children:
        yield from _iter_type(child, types)


def _resolve_req_path(req_path: str, base_dir: Path) -> Path:
    """Resolve a CJS/ESM import specifier to an absolute Path (best effort)."""
    resolved = (base_dir / req_path).resolve()
    if not resolved.suffix:
        as_js = resolved.with_suffix(".js")
        as_index = resolved / "index.js"
        resolved = as_js if as_js.exists() else (as_index if as_index.exists() else as_js)
    return resolved


def _require_string(val_node) -> Optional[str]:
    """If *val_node* is a ``require('...')`` call expression, return the path string; else None."""
    if val_node.type != "call_expression":
        return None
    fn = val_node.child_by_field_name("function")
    if fn is None or _ts_text(fn) != "require":
        return None
    args = val_node.child_by_field_name("arguments")
    if args is None:
        return None
    for child in args.named_children:
        if child.type == "string":
            return _ts_text(child).strip("'\"`")
    return None


def _find_module_exports_obj(root):
    """Return the ``object`` node of ``module.exports = {…}``, or ``None``."""
    for node in _iter_type(root, frozenset({"assignment_expression"})):
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None or right.type != "object":
            continue
        if left.type != "member_expression":
            continue
        obj = left.child_by_field_name("object")
        prop = left.child_by_field_name("property")
        if obj and _ts_text(obj) == "module" and prop and _ts_text(prop) == "exports":
            return right
    return None


def _key_name(key_node) -> Optional[str]:
    """Extract a plain-string key name from an object key AST node."""
    if key_node.type in ("property_identifier", "identifier"):
        return _ts_text(key_node)
    if key_node.type == "string":
        return _ts_text(key_node).strip("'\"`")
    if key_node.type == "computed_property_name":
        for child in key_node.named_children:
            if child.type == "string":
                return _ts_text(child).strip("'\"`")
    return None


def _parse_exports_obj(obj_node) -> tuple[dict[str, str], list[str]]:
    """Parse an object AST node into ``(direct_exports, spread_var_names)``.

    Returns:
        direct_exports:  ``{field_key: function_name}``
        spread_var_names: list of identifiers found in spread elements
                          (e.g. ``...facilityResolvers`` → ``["facilityResolvers"]``)
    """
    direct: dict[str, str] = {}
    spreads: list[str] = []

    for child in obj_node.named_children:
        if child.type == "pair":
            key_node = child.child_by_field_name("key")
            val_node = child.child_by_field_name("value")
            if key_node is None or val_node is None:
                continue
            key = _key_name(key_node)
            if not key:
                continue
            if val_node.type == "identifier":
                direct[key] = _ts_text(val_node)
            elif val_node.type in ("function", "arrow_function"):
                # Inline function definition — use the key as the local identifier
                direct[key] = key
        elif child.type == "shorthand_property_identifier":
            name = _ts_text(child)
            if name:
                direct[name] = name
        elif child.type == "spread_element":
            for sub in child.named_children:
                if sub.type == "identifier":
                    spreads.append(_ts_text(sub))

    return direct, spreads


def _collect_simple_requires(root, base_dir: Path) -> dict[str, Path]:
    """Collect ``var x = require('path')`` → ``{x: resolved_path}``."""
    result: dict[str, Path] = {}
    decl_types = frozenset({"variable_declaration", "lexical_declaration"})
    for decl in _iter_type(root, decl_types):
        for vd in decl.named_children:
            if vd.type != "variable_declarator":
                continue
            name_n = vd.child_by_field_name("name")
            val_n = vd.child_by_field_name("value")
            if name_n is None or val_n is None or name_n.type != "identifier":
                continue
            req = _require_string(val_n)
            if req is None:
                continue
            result[_ts_text(name_n)] = _resolve_req_path(req, base_dir)
    return result


def _collect_destructured_requires(root, base_dir: Path) -> dict[str, str]:
    """Collect ``const { a, b } = require('path')`` and ESM ``import { a } from 'path'``.

    Returns ``{local_name: resolved_abs_path_str}``.
    """
    result: dict[str, str] = {}
    decl_types = frozenset({"variable_declaration", "lexical_declaration"})

    # CJS: const { a, b } = require('path')
    for decl in _iter_type(root, decl_types):
        for vd in decl.named_children:
            if vd.type != "variable_declarator":
                continue
            name_n = vd.child_by_field_name("name")
            val_n = vd.child_by_field_name("value")
            if name_n is None or val_n is None or name_n.type != "object_pattern":
                continue
            req = _require_string(val_n)
            if req is None:
                continue
            resolved = str(_resolve_req_path(req, base_dir))
            for child in name_n.named_children:
                if child.type in (
                    "shorthand_property_identifier_pattern",
                    "identifier",
                ):
                    result[_ts_text(child)] = resolved
                elif child.type == "pair_pattern":
                    # { original: localAlias } = require(...)
                    val_p = child.child_by_field_name("value")
                    if val_p and val_p.type in ("identifier",):
                        result[_ts_text(val_p)] = resolved

    # ESM: import { a, b } from './path'
    for imp in _iter_type(root, frozenset({"import_statement"})):
        source_n = imp.child_by_field_name("source")
        if source_n is None:
            continue
        req = _ts_text(source_n).strip("'\"`")
        if not req:
            continue
        resolved = str(_resolve_req_path(req, base_dir))
        clause = imp.child_by_field_name("import_clause")
        if clause is None:
            continue
        for child in clause.named_children:
            if child.type == "named_imports":
                for spec in child.named_children:
                    if spec.type == "import_specifier":
                        alias = spec.child_by_field_name("alias")
                        name_spec = spec.child_by_field_name("name")
                        local_n = alias if alias else name_spec
                        if local_n:
                            result[_ts_text(local_n)] = resolved

    return result


# ── Enclosing-function resolution ─────────────────────────────────────────────

_FN_NODE_TYPES = frozenset({
    "function_declaration",
    "function",
    "arrow_function",
    "generator_function_declaration",
    "generator_function",
    "method_definition",
})


def _enclosing_fn_name(node) -> Optional[str]:
    """Walk up the AST from *node* to find the nearest named enclosing function."""
    cur = node.parent
    while cur is not None:
        if cur.type in _FN_NODE_TYPES:
            name_n = cur.child_by_field_name("name")
            if name_n:
                return _ts_text(name_n)
            # Anonymous function / arrow assigned to a variable
            if cur.type in ("function", "arrow_function", "generator_function"):
                par = cur.parent
                if par and par.type == "variable_declarator":
                    nm = par.child_by_field_name("name")
                    if nm and nm.type == "identifier":
                        return _ts_text(nm)
            # method_definition key (object literal)
            if cur.type == "method_definition":
                name_n = cur.child_by_field_name("name")
                if name_n:
                    return _ts_text(name_n)
        cur = cur.parent
    return None


# ── Rewritten public API ──────────────────────────────────────────────────────


def _parse_resolver_index(source: str, resolvers_dir: Path) -> dict[str, Path]:
    """Parse ``resolvers/index.js`` → ``{TypeName: resolver_file_path}``."""
    parser = _get_js_parser()
    if parser is None:
        return {}

    root = parser.parse(source.encode()).root_node
    var_to_file = _collect_simple_requires(root, resolvers_dir)

    obj_node = _find_module_exports_obj(root)
    if obj_node is None:
        return {}

    direct, _ = _parse_exports_obj(obj_node)
    result: dict[str, Path] = {}
    for type_name, var_name in direct.items():
        # GraphQL type names start with uppercase
        if type_name and type_name[0].isupper() and var_name in var_to_file:
            result[type_name] = var_to_file[var_name]
    return result


def _parse_resolver_exports(source: str) -> dict[str, str]:
    """Parse ``module.exports = {…}`` from a resolver file → ``{field_key: func_name}``."""
    parser = _get_js_parser()
    if parser is None:
        return {}

    root = parser.parse(source.encode()).root_node
    obj_node = _find_module_exports_obj(root)
    if obj_node is None:
        return {}

    direct, _ = _parse_exports_obj(obj_node)
    return direct


def _collect_spread_resolver_exports(
    source: str, file_dir: Path
) -> list[tuple[str, str, str]]:
    """Follow ``...spread`` exports in an aggregator → ``[(field_key, func_name, abs_path)]``.

    Handles the pattern::

        const facility = require('./facility');
        const report = require('./report');
        module.exports = { ...facility, ...report };

    Returns tuples carrying the *child* file path so callers emit edges with the
    correct source location (the file that actually defines the function).
    """
    parser = _get_js_parser()
    if parser is None:
        return []

    root = parser.parse(source.encode()).root_node
    var_to_file = _collect_simple_requires(root, file_dir)

    obj_node = _find_module_exports_obj(root)
    if obj_node is None:
        return []

    _, spread_vars = _parse_exports_obj(obj_node)
    results: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for var_name in spread_vars:
        spread_file = var_to_file.get(var_name)
        if spread_file is None or not spread_file.exists():
            continue
        try:
            spread_src = spread_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        file_path = str(spread_file)
        for field_key, func_name in _parse_resolver_exports(spread_src).items():
            if field_key not in seen:
                results.append((field_key, func_name, file_path))
                seen.add(field_key)

    return results


def _build_import_map(source: str, file_dir: Path) -> dict[str, str]:
    """Parse destructured ``require``/``import`` statements → ``{local_name: abs_path_str}``.

    Handles CJS destructured require and ESM named imports so callers can
    resolve functions imported from utility/controller modules.
    """
    parser = _get_js_parser()
    if parser is None:
        return {}

    root = parser.parse(source.encode()).root_node
    return _collect_destructured_requires(root, file_dir)


def _find_via_spread(func_name: str, index_file: str, store: "GraphStore") -> Optional[str]:
    """Search for *func_name* in files spread-exported by *index_file*.

    Handles the aggregator pattern::

        # controllers/index.js
        const holterProfileUtils = require('./holterProfile');
        module.exports = { ...holterProfileUtils };

    Returns the qualified name if *func_name* is found in a spread child file.
    """
    try:
        src = Path(index_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    parser = _get_js_parser()
    if parser is None:
        return None

    index_dir = Path(index_file).parent
    root = parser.parse(src.encode()).root_node

    var_to_file = _collect_simple_requires(root, index_dir)
    obj_node = _find_module_exports_obj(root)
    if obj_node is None:
        return None

    _, spread_vars = _parse_exports_obj(obj_node)
    for var_name in spread_vars:
        spread_file = var_to_file.get(var_name)
        if spread_file is None:
            continue
        candidate_qn = f"{spread_file}::{func_name}"
        if store.get_node(candidate_qn) is not None:
            logger.debug("_find_via_spread: %s found in %s", func_name, spread_file)
            return candidate_qn
    return None


def _resolve_fn_qn(
    func_name: str,
    file_path: str,
    store: "GraphStore",
    import_map: dict[str, str],
) -> str:
    """Return the best-effort qualified name for *func_name*.

    Priority:
    1. Local definition: ``file_path::func_name`` exists in store → use it.
    2. Direct import: *import_map* points to a file that defines *func_name*.
    3. Spread-via-index: imported file is an index aggregator that spread-exports
       *func_name* from a sub-file (e.g. ``controllers/index.js`` → ``controllers/holterProfile.js``).
    4. Fall back to local qn (may be dangling if the function is truly missing).
    """
    local_qn = f"{file_path}::{func_name}"
    if store.get_node(local_qn) is not None:
        return local_qn
    if func_name in import_map:
        imported_file = import_map[func_name]
        # Priority 2: direct match in the imported file
        candidate_qn = f"{imported_file}::{func_name}"
        if store.get_node(candidate_qn) is not None:
            logger.debug("_resolve_fn_qn: %s resolved via import from %s", func_name, imported_file)
            return candidate_qn
        # Priority 3: imported file is an index that spread-exports func_name
        deep_qn = _find_via_spread(func_name, imported_file, store)
        if deep_qn:
            return deep_qn
    return local_qn  # best effort — edge may be dangling


def _find_function_ranges(source: str) -> list[tuple[str, int, int]]:
    """Return ``[(name, start_byte, end_byte)]`` for all named JS functions.

    Uses tree-sitter AST traversal; falls back to empty list if parser unavailable.
    """
    parser = _get_js_parser()
    if parser is None:
        return []

    root = parser.parse(source.encode()).root_node
    ranges: list[tuple[str, int, int]] = []
    seen: set[str] = set()
    fn_decl_types = frozenset({"function_declaration", "generator_function_declaration"})
    fn_expr_types = frozenset({"function", "arrow_function", "generator_function"})
    decl_types = frozenset({"variable_declaration", "lexical_declaration"})

    for node in _iter_type(root, fn_decl_types):
        name_n = node.child_by_field_name("name")
        if name_n:
            fn_name = _ts_text(name_n)
            if fn_name and fn_name not in seen:
                ranges.append((fn_name, node.start_byte, node.end_byte))
                seen.add(fn_name)

    for decl in _iter_type(root, decl_types):
        for vd in decl.named_children:
            if vd.type != "variable_declarator":
                continue
            name_n = vd.child_by_field_name("name")
            val_n = vd.child_by_field_name("value")
            if name_n is None or val_n is None or name_n.type != "identifier":
                continue
            if val_n.type not in fn_expr_types:
                continue
            fn_name = _ts_text(name_n)
            if fn_name and fn_name not in seen:
                ranges.append((fn_name, decl.start_byte, decl.end_byte))
                seen.add(fn_name)

    return ranges


def _collect_datasource_aliases(root) -> set[str]:
    """Collect variable names destructured from ``dataSources``.

    Handles:
      const { mongodb } = dataSources
      const { mongodb } = context.dataSources
    Returns a set of alias names (e.g. ``{"mongodb"}``).
    """
    aliases: set[str] = set()
    decl_types = frozenset({"variable_declaration", "lexical_declaration"})
    for decl in _iter_type(root, decl_types):
        for vd in decl.named_children:
            if vd.type != "variable_declarator":
                continue
            name_n = vd.child_by_field_name("name")
            val_n = vd.child_by_field_name("value")
            if name_n is None or val_n is None or name_n.type != "object_pattern":
                continue
            # RHS must be `dataSources` or `context.dataSources` (or similar)
            val_text = _ts_text(val_n)
            is_ds = val_text == "dataSources"
            if not is_ds and val_n.type == "member_expression":
                prop_n = val_n.child_by_field_name("property")
                if prop_n and _ts_text(prop_n) == "dataSources":
                    is_ds = True
            if not is_ds:
                continue
            for child in name_n.named_children:
                if child.type in (
                    "shorthand_property_identifier_pattern",
                    "identifier",
                ):
                    aliases.add(_ts_text(child))
    return aliases


def _find_datasource_calls(source: str) -> list[tuple[str, str]]:
    """Find datasource method calls in resolver files.

    Detects two patterns:
      1. ``dataSources.X()`` / ``context.dataSources.X()``
      2. ``const { mongodb } = dataSources; mongodb.X()``  (destructured alias)

    Returns ``[(resolver_fn_name, datasource_method_name)]``.
    Powers DELEGATES_TO edges: resolver Function → datasource Function.
    """
    parser = _get_js_parser()
    if parser is None:
        return []

    root = parser.parse(source.encode()).root_node
    results: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    # Collect destructured datasource aliases: const { mongodb } = dataSources
    ds_aliases = _collect_datasource_aliases(root)

    for call in _iter_type(root, frozenset({"call_expression"})):
        fn_node = call.child_by_field_name("function")
        if fn_node is None or fn_node.type != "member_expression":
            continue
        obj = fn_node.child_by_field_name("object")
        prop = fn_node.child_by_field_name("property")
        if obj is None or prop is None:
            continue

        obj_text = _ts_text(obj)

        # Pattern 1: dataSources.foo() or context.dataSources.foo()
        is_ds = obj_text == "dataSources"
        if not is_ds and obj.type == "member_expression":
            inner_prop = obj.child_by_field_name("property")
            if inner_prop and _ts_text(inner_prop) == "dataSources":
                is_ds = True

        # Pattern 2: mongodb.foo()  where mongodb was destructured from dataSources
        if not is_ds and obj_text in ds_aliases:
            is_ds = True

        if not is_ds:
            continue

        ds_method = _ts_text(prop)
        enclosing = _enclosing_fn_name(call)
        if enclosing and ds_method and (enclosing, ds_method) not in seen:
            results.append((enclosing, ds_method))
            seen.add((enclosing, ds_method))

    return results


def _find_loaders_usage(source: str) -> list[tuple[str, str]]:
    """Find ``loaders.X.load()`` / ``context.loaders.X.load()`` calls.

    Returns ``[(function_name, loader_name)]``.
    Powers USES_LOADER edges: resolver Function → Loader.
    """
    parser = _get_js_parser()
    if parser is None:
        return []

    root = parser.parse(source.encode()).root_node
    results: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for call in _iter_type(root, frozenset({"call_expression"})):
        fn_node = call.child_by_field_name("function")
        if fn_node is None or fn_node.type != "member_expression":
            continue
        method_prop = fn_node.child_by_field_name("property")
        if method_prop is None or _ts_text(method_prop) not in ("load", "loadMany"):
            continue

        # fn_node.object = loaders.X  OR  context.loaders.X
        loader_chain = fn_node.child_by_field_name("object")
        if loader_chain is None or loader_chain.type != "member_expression":
            continue
        loader_name_n = loader_chain.child_by_field_name("property")
        loader_obj = loader_chain.child_by_field_name("object")
        if loader_name_n is None or loader_obj is None:
            continue
        loader_name = _ts_text(loader_name_n)

        # loader_obj must be `loaders` or `context.loaders`
        obj_text = _ts_text(loader_obj)
        is_loaders = obj_text == "loaders"
        if not is_loaders and loader_obj.type == "member_expression":
            lp = loader_obj.child_by_field_name("property")
            if lp and _ts_text(lp) == "loaders":
                is_loaders = True
        if not is_loaders:
            continue

        enclosing = _enclosing_fn_name(call)
        if enclosing and loader_name and (enclosing, loader_name) not in seen:
            results.append((enclosing, loader_name))
            seen.add((enclosing, loader_name))

    return results


def _parse_loaders_index(source: str) -> list[dict]:
    """Parse ``utils/loaders/index.js`` DataLoader instantiations.

    Returns ``[{loader_name, batch_function, line}]``.
    """
    loaders: list[dict] = []
    seen: set[str] = set()

    # Pattern: key: new DataLoader(... batchFn ...)
    # Handles: keys => batchFn(keys), keys => batchFn, batchFn
    patterns = [
        # key: new DataLoader(keys => batchFn(keys)) or keys => obj.batchFn(keys)
        r"\b(\w+)\s*:\s*new\s+DataLoader\s*\(\s*(?:async\s+)?(?:\w+\s*=>)?\s*(?:\w+\.)?(\w+)\s*\(",
        # key: new DataLoader(batchFn, ...)
        r"\b(\w+)\s*:\s*new\s+DataLoader\s*\(\s*(?:\w+\.)?(\w+)\s*[,)]",
    ]

    for pat in patterns:
        for m in re.finditer(pat, source):
            loader_name = m.group(1)
            batch_fn = m.group(2)
            if loader_name in seen or batch_fn in ("async", "function", "keys"):
                continue
            line = source[: m.start()].count("\n") + 1
            loaders.append({"loader_name": loader_name, "batch_function": batch_fn, "line": line})
            seen.add(loader_name)

    return loaders


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_file(service_root: Path, candidates: list[str]) -> Optional[Path]:
    """Return the first candidate path (relative to *service_root*) that exists."""
    for rel in candidates:
        p = service_root / rel
        if p.exists():
            return p
    return None


def _find_loaders_index_path(service_root: Path) -> Optional[str]:
    """Return the absolute path string for the loaders index file, or None."""
    candidates = [
        "app/utils/loaders/index.js",
        "utils/loaders/index.js",
        "app/datasources/loaders/index.js",
        "app/graphql/loaders/index.js",
    ]
    f = _find_file(service_root, candidates)
    return str(f) if f else None


_DS_PATH_HINTS = ("datasource", "controller", "command", "repository")
_RESOLVER_PATH_HINTS = ("/resolver", "/resolvers")


def _find_export_alias_in_file(file_path: str, exported_name: str) -> Optional[str]:
    """Return the local function name that *exported_name* aliases in *file_path*.

    Handles the CJS pattern:
        module.exports = { exportedName: actualFn, ... }

    Returns *actualFn* when key == *exported_name* and the value is a plain
    identifier that differs from the key (i.e. a real alias, not shorthand).
    Returns ``None`` if no alias is found.
    """
    try:
        source = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    parser = _get_js_parser()
    if parser is None:
        return None

    tree = parser.parse(source.encode("utf-8", errors="replace"))
    obj_node = _find_module_exports_obj(tree.root_node)
    if obj_node is None:
        return None

    direct, _ = _parse_exports_obj(obj_node)
    actual = direct.get(exported_name)
    # Only return when it's a real alias (value differs from key)
    if actual and actual != exported_name:
        return actual
    return None


def _find_datasource_function_qualified(
    store: GraphStore, func_name: str, service_root: str,
) -> Optional[str]:
    """Find the datasource/controller Function node for *func_name*.

    Searches all Function nodes with that name inside *service_root*, then
    picks the best match using path priority:
      1. Prefer nodes whose path contains a datasource/controller hint.
      2. Fall back to any non-resolver Function node.

    If no direct match exists, scans datasource files for CJS alias exports
    of the form ``module.exports = { func_name: actualFn }`` and retries
    with *actualFn*.

    Returns None if no suitable match exists.
    """
    root_prefix = service_root.rstrip("/") + "/"

    def _rel(qn: str) -> str:
        return qn[len(root_prefix):].lower() if qn.startswith(root_prefix) else qn.lower()

    def _pick_best(candidates: list[str]) -> Optional[str]:
        # Priority 1: path contains datasource/controller hint
        for qn in candidates:
            if any(h in _rel(qn) for h in _DS_PATH_HINTS):
                return qn
        # Priority 2: not a resolver path
        for qn in candidates:
            if not any(h in _rel(qn) for h in _RESOLVER_PATH_HINTS):
                return qn
        return None

    def _search(name: str) -> list[str]:
        try:
            nodes = store.search_nodes(name, limit=50)
        except Exception:
            return []
        return [
            n.qualified_name
            for n in nodes
            if n.kind == "Function" and n.name == name and n.file_path.startswith(service_root)
        ]

    candidates = _search(func_name)
    if candidates:
        result = _pick_best(candidates)
        if result:
            return result
        # All candidates were resolver paths — fall through to alias scan

    # Fallback: scan datasource/controller files for CJS alias exports
    # e.g. module.exports = { generateHolterReport: remakeGenerateHolterReport }
    try:
        all_files = store.get_all_files()
    except Exception:
        return None

    for file_path in all_files:
        if not file_path.startswith(service_root):
            continue
        rel = file_path[len(root_prefix):].lower()
        if not any(h in rel for h in _DS_PATH_HINTS):
            continue
        actual_name = _find_export_alias_in_file(file_path, func_name)
        if actual_name is None:
            continue
        alias_candidates = _search(actual_name)
        result = _pick_best(alias_candidates)
        if result:
            logger.debug(
                "DELEGATES_TO alias: %s → %s (via %s)",
                func_name, actual_name, file_path,
            )
            return result

    return None


def _find_function_qualified(store: GraphStore, func_name: str, service_root: str) -> Optional[str]:
    """Look up the qualified_name of *func_name* within *service_root* in the store."""
    try:
        nodes = store.search_nodes(func_name, limit=50)
    except Exception:
        return None
    for node in nodes:
        if node.kind == "Function" and node.name == func_name and node.file_path.startswith(service_root):
            return node.qualified_name
    return None
