"""GraphQL graph extension.

Extracts GQLField, GQLType, and Loader nodes, plus RESOLVES, FIELD_OF,
RETURNS, ACCEPTS, USES_LOADER, BACKED_BY, and RESOLVES_REF edges from
GraphQL services found in the repository.

Data sources (per service):
  .schema.gql              → GQLField, GQLType nodes; FIELD_OF, RETURNS, ACCEPTS edges
  resolvers/index.js       → type→resolver-file mapping (lookup only)
  resolvers/*.js           → RESOLVES, USES_LOADER, RESOLVES_REF edges
  utils/loaders/index.js   → Loader nodes; BACKED_BY edges
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
                "return_type": fd.get("return_type", ""),
                "return_type_is_list": fd.get("return_type_is_list", False),
                "return_type_nullable": fd.get("return_type_nullable", True),
                "auth": fd.get("auth", {}),
                "directives": fd.get("directives", []),
                "is_federation_key": fd.get("is_federation_key", False),
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

            for field_key, func_name, file_path in edge_items:
                if field_key == "__resolveReference":
                    # RESOLVES_REF: Function → GQLType (entity type)
                    type_qn = gql_type_qn.get(type_name, f"{schema_path}::{type_name}")
                    fn_qn = f"{file_path}::{func_name}"
                    store.upsert_edge(
                        EdgeInfo(kind="RESOLVES_REF", source=fn_qn, target=type_qn, file_path=file_path)
                    )
                    stats["edges"] += 1
                else:
                    # RESOLVES: Function → GQLField
                    field_qn = f"{schema_path}::{type_name}.{field_key}"
                    fn_qn = f"{file_path}::{func_name}"
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
            })

    return type_defs, field_defs


# ---------------------------------------------------------------------------
# JavaScript resolver / loader parsers
# ---------------------------------------------------------------------------


def _find_js_block_end(source: str, start: int) -> int:
    """Return index of the ``}`` matching the ``{`` at *start*, skipping JS strings."""
    depth = 0
    in_string = False
    string_char = ""
    i = start
    while i < len(source):
        ch = source[i]
        if in_string:
            if ch == "\\" :
                i += 2
                continue
            if ch == string_char:
                in_string = False
        else:
            if ch in ('"', "'", "`"):
                in_string = True
                string_char = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return len(source) - 1


def _find_exports_block(source: str) -> str:
    """Return the content between ``{`` and ``}`` of ``module.exports = { … }``."""
    idx = source.find("module.exports")
    if idx == -1:
        return ""
    brace_idx = source.find("{", idx)
    if brace_idx == -1:
        return ""
    end = _find_js_block_end(source, brace_idx)
    return source[brace_idx + 1 : end]


def _parse_resolver_index(source: str, resolvers_dir: Path) -> dict[str, Path]:
    """Parse ``resolvers/index.js`` → ``{TypeName: resolver_file_path}`` mapping."""
    # Step 1: build variable → file mapping from require() calls
    var_to_file: dict[str, Path] = {}
    for m in re.finditer(
        r"(?:const|let|var)\s+(\w+)\s*=\s*require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
        source,
    ):
        var_name = m.group(1)
        req_path = m.group(2)
        resolved = (resolvers_dir / req_path).resolve()
        if not resolved.suffix:
            # Node.js resolution: try path.js first, then path/index.js
            as_js = resolved.with_suffix(".js")
            as_index = resolved / "index.js"
            resolved = as_js if as_js.exists() else (as_index if as_index.exists() else as_js)
        var_to_file[var_name] = resolved

    # Step 2: parse module.exports { TypeName: varName, ... }
    exports_block = _find_exports_block(source)
    if not exports_block:
        return {}

    result: dict[str, Path] = {}
    for m in re.finditer(r"\b([A-Z]\w*)\s*:\s*(\w+)\b", exports_block):
        type_name = m.group(1)
        var_name = m.group(2)
        if var_name in var_to_file:
            result[type_name] = var_to_file[var_name]

    return result


def _parse_resolver_exports(source: str) -> dict[str, str]:
    """Parse ``module.exports = { … }`` from a resolver file.

    Returns ``{field_key: function_name}``.
    """
    exports_block = _find_exports_block(source)
    if not exports_block:
        return {}

    result: dict[str, str] = {}

    # Explicit key: value pairs (e.g. linkedStudies: resolveLinkedStudies)
    for m in re.finditer(r"\b(\w+)\s*:\s*(\w+)\b", exports_block):
        key, value = m.group(1), m.group(2)
        if key != "__proto__":
            result[key] = value

    # Shorthand properties: identifier NOT followed by ':'
    # Pattern 1 — multi-line: identifier alone on a line (e.g. "  inboxes,")
    for m in re.finditer(r"^\s*([a-zA-Z_$][\w$]*)\s*,?\s*$", exports_block, re.MULTILINE):
        name = m.group(1)
        if name not in result:
            result[name] = name
    # Pattern 2 — inline: identifier after a comma, followed by ',', '}', or end-of-string
    # (the closing '}' is consumed by _find_exports_block, so we must also match '$')
    for m in re.finditer(r",\s*([a-zA-Z_$][\w$]*)(?=\s*(?:,|\}|$))", exports_block):
        name = m.group(1)
        if name not in result:
            result[name] = name
    # Pattern 3 — first item in an inline block: { firstItem, secondItem }
    # Pattern 2 requires a leading comma so it misses the very first identifier.
    for m in re.finditer(r"^\s*([a-zA-Z_$][\w$]*)(?=\s*,)", exports_block, re.MULTILINE):
        name = m.group(1)
        if name not in result:
            result[name] = name

    return result


def _collect_spread_resolver_exports(
    source: str, file_dir: Path
) -> list[tuple[str, str, str]]:
    """Follow ``...spread`` exports in a resolver aggregator file (one level deep).

    Handles the pattern where a resolver file only re-exports via spreads::

        const facility = require('./facility');
        const report = require('./report');
        module.exports = { ...facility, ...report };

    Returns ``[(field_key, func_name, source_file_path)]`` so callers can emit
    edges with the correct file path (the child file, not the aggregator).
    """
    var_to_file: dict[str, Path] = {}
    for m in re.finditer(
        r"(?:const|let|var)\s+(\w+)\s*=\s*require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
        source,
    ):
        var_name, req_path = m.group(1), m.group(2)
        resolved = (file_dir / req_path).resolve()
        if not resolved.suffix:
            resolved = resolved.with_suffix(".js")
        var_to_file[var_name] = resolved

    exports_block = _find_exports_block(source)
    if not exports_block:
        return []

    results: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for m in re.finditer(r"\.\.\.\s*(\w+)", exports_block):
        spread_file = var_to_file.get(m.group(1))
        if spread_file and spread_file.exists():
            try:
                spread_source = spread_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            file_path = str(spread_file)
            for field_key, func_name in _parse_resolver_exports(spread_source).items():
                if field_key not in seen:
                    results.append((field_key, func_name, file_path))
                    seen.add(field_key)

    return results


def _find_function_ranges(source: str) -> list[tuple[str, int, int]]:
    """Return ``(name, start, end)`` for all named JS functions in *source*."""
    ranges: list[tuple[str, int, int]] = []
    seen: set[str] = set()

    patterns = [
        # async function name(...) {
        r"\basync\s+function\s+(\w+)\s*\([^)]*\)\s*\{",
        # function name(...) {
        r"\bfunction\s+(\w+)\s*\([^)]*\)\s*\{",
        # const name = async (...) => {
        r"\b(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>\s*\{",
        # const name = async function(...) {
        r"\b(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?function\s*\([^)]*\)\s*\{",
    ]

    for pat in patterns:
        for m in re.finditer(pat, source):
            fn_name = m.group(1)
            if fn_name in seen:
                continue
            brace_start = source.rfind("{", m.start(), m.end())
            if brace_start == -1:
                continue
            brace_end = _find_js_block_end(source, brace_start)
            ranges.append((fn_name, m.start(), brace_end))
            seen.add(fn_name)

    return ranges


def _find_loaders_usage(source: str) -> list[tuple[str, str]]:
    """Find ``loaders.X.load()`` / ``loadMany()`` calls and their enclosing functions.

    Returns ``[(function_name, loader_name)]``.
    """
    func_ranges = _find_function_ranges(source)
    results: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for m in re.finditer(r"\bloaders\.(\w+)\.(?:load|loadMany)\s*\(", source):
        loader_name = m.group(1)
        call_pos = m.start()
        enclosing = None
        for fn_name, fn_start, fn_end in func_ranges:
            if fn_start <= call_pos <= fn_end:
                enclosing = fn_name
                break
        if enclosing and (enclosing, loader_name) not in seen:
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
