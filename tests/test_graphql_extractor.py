"""Tests for graphql_extractor.py.

Covers:
  - _parse_schema_gql       : GQLType and GQLField extraction from SDL
  - _parse_resolver_index   : type → resolver file mapping
  - _parse_resolver_exports : module.exports key/value parsing
  - _parse_loaders_index    : DataLoader instantiation detection
  - _find_loaders_usage     : loaders.X.load() call detection
  - extract_graphql_for_repo: full integration against a synthetic service tree
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.graphql_extractor import (
    BUILTIN_SCALARS,
    _find_loaders_usage,
    _parse_gql_field,
    _parse_loaders_index,
    _parse_resolver_exports,
    _parse_resolver_index,
    _parse_schema_gql,
    extract_graphql_for_repo,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    db_path = tmp_path / "graph.db"
    s = GraphStore(str(db_path))
    yield s
    s.close()


# ---------------------------------------------------------------------------
# _parse_schema_gql
# ---------------------------------------------------------------------------


SDL_BASIC = """
type Query {
  inboxes(filter: InboxFilterInput, limit: Int): InboxesResponse @auth(groups: [ALL_CLINIC], roles: [CLINIC_PHYSICIAN])
  physicians: [Physician]
}

type Mutation {
  markInbox(id: ID!): Boolean
}

input InboxFilterInput {
  status: String
  page: Int
}

type InboxesResponse {
  items: [Inbox]
  total: Int
}

type Inbox {
  id: ID!
  message: String
}

scalar DateTime
"""


class TestParseSchemaGql:
    def test_type_names_extracted(self):
        types, _ = _parse_schema_gql(SDL_BASIC)
        names = {t["name"] for t in types}
        # DateTime is in BUILTIN_SCALARS → excluded
        assert {"Query", "Mutation", "InboxFilterInput", "InboxesResponse", "Inbox"} == names

    def test_builtin_scalars_excluded(self):
        types, _ = _parse_schema_gql(SDL_BASIC)
        names = {t["name"] for t in types}
        assert "String" not in names
        assert "Int" not in names
        assert "Boolean" not in names
        assert "ID" not in names

    def test_field_names_extracted(self):
        _, fields = _parse_schema_gql(SDL_BASIC)
        field_ids = {f"{f['parent_type']}.{f['name']}" for f in fields}
        assert "Query.inboxes" in field_ids
        assert "Query.physicians" in field_ids
        assert "Mutation.markInbox" in field_ids
        assert "InboxesResponse.items" in field_ids

    def test_operation_types(self):
        _, fields = _parse_schema_gql(SDL_BASIC)
        ops = {f["name"]: f["operation"] for f in fields}
        assert ops["inboxes"] == "query"
        assert ops["markInbox"] == "mutation"
        assert ops["total"] == "type_field"

    def test_return_type_unwrapped(self):
        _, fields = _parse_schema_gql(SDL_BASIC)
        f = next(x for x in fields if x["name"] == "inboxes")
        assert f["return_type"] == "InboxesResponse"
        assert f["return_type_is_list"] is False

    def test_list_return_type(self):
        _, fields = _parse_schema_gql(SDL_BASIC)
        f = next(x for x in fields if x["name"] == "physicians")
        assert f["return_type"] == "Physician"
        assert f["return_type_is_list"] is True

    def test_auth_directive_parsed(self):
        _, fields = _parse_schema_gql(SDL_BASIC)
        f = next(x for x in fields if x["name"] == "inboxes")
        assert f["auth"]["groups"] == ["ALL_CLINIC"]
        assert f["auth"]["roles"] == ["CLINIC_PHYSICIAN"]

    def test_custom_arg_type_extracted(self):
        _, fields = _parse_schema_gql(SDL_BASIC)
        f = next(x for x in fields if x["name"] == "inboxes")
        assert "InboxFilterInput" in f["arg_types"]

    def test_scalar_arg_type_excluded(self):
        _, fields = _parse_schema_gql(SDL_BASIC)
        f = next(x for x in fields if x["name"] == "inboxes")
        assert "Int" not in f["arg_types"]

    def test_federation_entity(self):
        sdl = """
        type Study @key(fields: "id") @shareable {
          id: ID!
          linkedStudies: [Study]
        }
        """
        types, _ = _parse_schema_gql(sdl)
        study = next(t for t in types if t["name"] == "Study")
        assert study["is_federation_entity"] is True
        assert study["key_fields"] == "id"
        assert study["is_shareable"] is True

    def test_extend_type_parsed(self):
        sdl = """
        extend type Query {
          getStudy(id: ID!): Study
        }
        type Study { id: ID! }
        """
        types, fields = _parse_schema_gql(sdl)
        names = {t["name"] for t in types}
        assert "Query" in names
        assert any(f["name"] == "getStudy" for f in fields)

    def test_comments_stripped(self):
        sdl = """
        # This is a comment
        type Query {
          # field comment
          hello: String  # inline comment
        }
        """
        _, fields = _parse_schema_gql(sdl)
        assert any(f["name"] == "hello" for f in fields)

    def test_federation_internals_skipped(self):
        sdl = """
        type _Entity { id: ID }
        type _Service { sdl: String }
        type Query { ping: Boolean }
        """
        types, _ = _parse_schema_gql(sdl)
        names = {t["name"] for t in types}
        assert "_Entity" not in names
        assert "_Service" not in names

    def test_enum_type_kind(self):
        sdl = "enum Status { ACTIVE INACTIVE }"
        types, _ = _parse_schema_gql(sdl)
        assert types[0]["type_kind"] == "enum"

    def test_input_type_kind(self):
        sdl = "input MyInput { field: String }"
        types, _ = _parse_schema_gql(sdl)
        assert types[0]["type_kind"] == "input"

    def test_multiline_field(self):
        sdl = """
        type Query {
          search(
            term: String,
            filter: SearchFilter
          ): SearchResult
        }
        """
        _, fields = _parse_schema_gql(sdl)
        f = next((x for x in fields if x["name"] == "search"), None)
        assert f is not None
        assert f["return_type"] == "SearchResult"
        assert "SearchFilter" in f["arg_types"]


# ---------------------------------------------------------------------------
# _parse_gql_field
# ---------------------------------------------------------------------------


class TestParseGqlField:
    def test_simple_field(self):
        f = _parse_gql_field("hello: String")
        assert f is not None
        assert f["name"] == "hello"
        assert f["return_type"] == "String"

    def test_non_null_field(self):
        f = _parse_gql_field("id: ID!")
        assert f is not None
        assert f["return_type"] == "ID"
        assert f["return_type_nullable"] is False

    def test_list_field(self):
        f = _parse_gql_field("items: [Item]")
        assert f is not None
        assert f["return_type"] == "Item"
        assert f["return_type_is_list"] is True

    def test_field_with_args(self):
        f = _parse_gql_field("search(q: String, filter: FilterInput): Result")
        assert f is not None
        assert "FilterInput" in f["arg_types"]
        assert "String" not in f["arg_types"]

    def test_field_without_colon_returns_none(self):
        assert _parse_gql_field("notAField") is None

    def test_empty_line_returns_none(self):
        assert _parse_gql_field("") is None
        assert _parse_gql_field("   ") is None

    def test_keyword_returns_none(self):
        assert _parse_gql_field("type Something: Foo") is None


# ---------------------------------------------------------------------------
# _parse_resolver_index
# ---------------------------------------------------------------------------


class TestParseResolverIndex:
    def test_basic_mapping(self, tmp_path):
        resolvers_dir = tmp_path / "resolvers"
        resolvers_dir.mkdir()
        (resolvers_dir / "query.js").touch()
        (resolvers_dir / "mutation.js").touch()

        source = """
        const queryResolver = require('./query');
        const mutResolver = require('./mutation');
        module.exports = {
          Query: queryResolver,
          Mutation: mutResolver,
        };
        """
        result = _parse_resolver_index(source, resolvers_dir)
        assert "Query" in result
        assert "Mutation" in result
        assert result["Query"].name == "query.js"
        assert result["Mutation"].name == "mutation.js"

    def test_type_resolver_mapping(self, tmp_path):
        resolvers_dir = tmp_path / "resolvers"
        resolvers_dir.mkdir()
        (resolvers_dir / "study.js").touch()

        source = """
        const studyResolver = require('./study');
        module.exports = { Study: studyResolver };
        """
        result = _parse_resolver_index(source, resolvers_dir)
        assert "Study" in result
        assert result["Study"].name == "study.js"

    def test_missing_require_skipped(self, tmp_path):
        resolvers_dir = tmp_path / "resolvers"
        resolvers_dir.mkdir()

        source = "module.exports = { Query: unknownVar };"
        result = _parse_resolver_index(source, resolvers_dir)
        # unknownVar has no require() → should not appear
        assert "Query" not in result

    def test_lowercase_key_skipped(self, tmp_path):
        resolvers_dir = tmp_path / "resolvers"
        resolvers_dir.mkdir()
        (resolvers_dir / "utils.js").touch()

        source = """
        const utilsResolver = require('./utils');
        module.exports = { utils: utilsResolver };
        """
        # lowercase key → not a GraphQL type
        result = _parse_resolver_index(source, resolvers_dir)
        assert "utils" not in result


# ---------------------------------------------------------------------------
# _parse_resolver_exports
# ---------------------------------------------------------------------------


class TestParseResolverExports:
    def test_shorthand_exports(self):
        source = """
        module.exports = {
          inboxes,
          physicians,
        };
        """
        exports = _parse_resolver_exports(source)
        assert exports["inboxes"] == "inboxes"
        assert exports["physicians"] == "physicians"

    def test_explicit_key_value(self):
        source = """
        module.exports = {
          linkedStudies: resolveLinkedStudies,
          device: resolveDevice,
        };
        """
        exports = _parse_resolver_exports(source)
        assert exports["linkedStudies"] == "resolveLinkedStudies"
        assert exports["device"] == "resolveDevice"

    def test_resolve_reference(self):
        source = """
        module.exports = {
          inboxes,
          __resolveReference,
        };
        """
        exports = _parse_resolver_exports(source)
        assert exports["__resolveReference"] == "__resolveReference"

    def test_mixed_exports(self):
        source = """
        module.exports = {
          inboxes,
          linkedStudies: resolveLinkedStudies,
          __resolveReference,
        };
        """
        exports = _parse_resolver_exports(source)
        assert exports["inboxes"] == "inboxes"
        assert exports["linkedStudies"] == "resolveLinkedStudies"
        assert exports["__resolveReference"] == "__resolveReference"

    def test_no_exports_returns_empty(self):
        assert _parse_resolver_exports("const x = 1;") == {}


# ---------------------------------------------------------------------------
# _parse_loaders_index
# ---------------------------------------------------------------------------


class TestParseLoadersIndex:
    def test_basic_dataloader(self):
        source = """
        function createLoaders() {
          return {
            study: new DataLoader(keys => batchStudies(keys)),
          };
        }
        """
        loaders = _parse_loaders_index(source)
        assert len(loaders) == 1
        assert loaders[0]["loader_name"] == "study"
        assert loaders[0]["batch_function"] == "batchStudies"

    def test_multiple_loaders(self):
        source = """
        return {
          study: new DataLoader(keys => batchStudies(keys)),
          lastStudyHistory: new DataLoader(keys => batchLastStudyHistory(keys)),
          patientReturn: new DataLoader(batchPatientReturn),
        };
        """
        loaders = _parse_loaders_index(source)
        names = {l["loader_name"] for l in loaders}
        assert {"study", "lastStudyHistory", "patientReturn"} == names

    def test_batch_function_captured(self):
        source = "{ myLoader: new DataLoader(keys => myBatchFn(keys)) }"
        loaders = _parse_loaders_index(source)
        assert loaders[0]["batch_function"] == "myBatchFn"

    def test_no_dataloader_returns_empty(self):
        assert _parse_loaders_index("const x = 1;") == []


# ---------------------------------------------------------------------------
# _find_loaders_usage
# ---------------------------------------------------------------------------


class TestFindLoadersUsage:
    def test_load_call_detected(self):
        source = """
        async function resolveLinkedStudies(study, args, { loaders }) {
          return loaders.study.load(study.id);
        }
        """
        usage = _find_loaders_usage(source)
        assert ("resolveLinkedStudies", "study") in usage

    def test_load_many_detected(self):
        source = """
        async function resolveHistory(study, args, { loaders }) {
          return loaders.lastStudyHistory.loadMany([study.id]);
        }
        """
        usage = _find_loaders_usage(source)
        assert ("resolveHistory", "lastStudyHistory") in usage

    def test_multiple_functions(self):
        source = """
        async function resolveLinkedStudies(obj, args, { loaders }) {
          return loaders.study.load(obj.id);
        }
        async function __resolveReference(obj, { loaders }) {
          return loaders.study.load(obj.id);
        }
        async function resolveHistory(obj, args, { loaders }) {
          return loaders.lastStudyHistory.loadMany([obj.id]);
        }
        """
        usage = _find_loaders_usage(source)
        fn_names = {u[0] for u in usage}
        assert "resolveLinkedStudies" in fn_names
        assert "__resolveReference" in fn_names
        assert "resolveHistory" in fn_names

    def test_deduplication(self):
        source = """
        async function resolveStudy(obj, args, { loaders }) {
          const a = loaders.study.load(obj.id);
          const b = loaders.study.load(obj.parentId);
          return [a, b];
        }
        """
        usage = _find_loaders_usage(source)
        # Same (fn, loader) pair should appear only once
        assert usage.count(("resolveStudy", "study")) == 1

    def test_no_loaders_returns_empty(self):
        source = "async function fn() { return db.query('SELECT 1'); }"
        assert _find_loaders_usage(source) == []


# ---------------------------------------------------------------------------
# Integration: extract_graphql_for_repo
# ---------------------------------------------------------------------------


def _make_service(base: Path) -> Path:
    """Create a minimal GraphQL service directory tree under *base*."""
    base.mkdir(parents=True, exist_ok=True)
    # .schema.gql
    (base / ".schema.gql").write_text(
        """
type Query {
  inboxes(filter: InboxFilterInput): InboxesResponse @auth(groups: [ALL_CLINIC])
}
type Mutation {
  markInbox(id: ID!): Boolean
}
type Study @key(fields: "id") {
  id: ID!
  linkedStudies: [Study]
}
input InboxFilterInput { status: String }
type InboxesResponse { items: [Inbox] total: Int }
type Inbox { id: ID! message: String }
""",
        encoding="utf-8",
    )

    # resolvers/index.js
    resolvers_dir = base / "app" / "resolvers"
    resolvers_dir.mkdir(parents=True)
    (resolvers_dir / "index.js").write_text(
        """
const queryResolver = require('./query');
const studyResolver = require('./study');
module.exports = { Query: queryResolver, Study: studyResolver };
""",
        encoding="utf-8",
    )

    # resolvers/query.js
    (resolvers_dir / "query.js").write_text(
        """
async function inboxes(parent, args, { loaders }) {
  return loaders.inbox.load(args.filter);
}
module.exports = { inboxes };
""",
        encoding="utf-8",
    )

    # resolvers/study.js
    (resolvers_dir / "study.js").write_text(
        """
async function resolveLinkedStudies(study, args, { loaders }) {
  return loaders.study.load(study.id);
}
async function __resolveReference(study, { loaders }) {
  return loaders.study.load(study.id);
}
module.exports = { linkedStudies: resolveLinkedStudies, __resolveReference };
""",
        encoding="utf-8",
    )

    # utils/loaders/index.js
    loaders_dir = base / "app" / "utils" / "loaders"
    loaders_dir.mkdir(parents=True)
    (loaders_dir / "index.js").write_text(
        """
const DataLoader = require('dataloader');
const { batchStudies } = require('../../datasources/loaders/study');
const { batchInboxes } = require('../../datasources/loaders/inbox');
function createLoaders() {
  return {
    study: new DataLoader(keys => batchStudies(keys)),
    inbox: new DataLoader(keys => batchInboxes(keys)),
  };
}
module.exports = { createLoaders };
""",
        encoding="utf-8",
    )

    return base


class TestExtractGraphqlForRepo:
    @pytest.fixture()
    def service_root(self, tmp_path):
        return _make_service(tmp_path / "clinic_api")

    @pytest.fixture()
    def populated_store(self, service_root, store):
        extract_graphql_for_repo(service_root.parent, store)
        return store

    def test_stats_returned(self, service_root, store):
        stats = extract_graphql_for_repo(service_root.parent, store)
        assert stats["services"] == 1
        assert stats["gql_types"] > 0
        assert stats["gql_fields"] > 0
        assert stats["loaders"] > 0
        assert stats["edges"] > 0

    def test_gqlfield_nodes_created(self, service_root, populated_store):
        nodes = populated_store.search_nodes("Query.inboxes", limit=5)
        field_nodes = [n for n in nodes if n.kind == "GQLField"]
        assert field_nodes, "GQLField Query.inboxes should exist"

    def test_gqltype_nodes_created(self, service_root, populated_store):
        nodes = populated_store.search_nodes("InboxesResponse", limit=5)
        type_nodes = [n for n in nodes if n.kind == "GQLType"]
        assert type_nodes, "GQLType InboxesResponse should exist"

    def test_loader_nodes_created(self, service_root, populated_store):
        nodes = populated_store.search_nodes("study", limit=20)
        loader_nodes = [n for n in nodes if n.kind == "Loader"]
        assert loader_nodes, "Loader 'study' should exist"

    def test_field_of_edge_created(self, service_root, populated_store):
        schema_path = str(service_root / ".schema.gql")
        field_qn = f"{schema_path}::Query.inboxes"
        edges = populated_store.get_edges_by_source(field_qn)
        kinds = {e.kind for e in edges}
        assert "FIELD_OF" in kinds

    def test_returns_edge_created(self, service_root, populated_store):
        schema_path = str(service_root / ".schema.gql")
        field_qn = f"{schema_path}::Query.inboxes"
        edges = populated_store.get_edges_by_source(field_qn)
        kinds = {e.kind for e in edges}
        assert "RETURNS" in kinds

    def test_accepts_edge_created(self, service_root, populated_store):
        schema_path = str(service_root / ".schema.gql")
        field_qn = f"{schema_path}::Query.inboxes"
        edges = populated_store.get_edges_by_source(field_qn)
        kinds = {e.kind for e in edges}
        assert "ACCEPTS" in kinds

    def test_resolves_edge_created(self, service_root, populated_store):
        resolver_path = str(service_root / "app" / "resolvers" / "query.js")
        fn_qn = f"{resolver_path}::inboxes"
        edges = populated_store.get_edges_by_source(fn_qn)
        resolves = [e for e in edges if e.kind == "RESOLVES"]
        assert resolves, "RESOLVES edge from inboxes function should exist"

    def test_resolves_ref_edge_created(self, service_root, populated_store):
        resolver_path = str(service_root / "app" / "resolvers" / "study.js")
        fn_qn = f"{resolver_path}::__resolveReference"
        edges = populated_store.get_edges_by_source(fn_qn)
        resolves_ref = [e for e in edges if e.kind == "RESOLVES_REF"]
        assert resolves_ref, "RESOLVES_REF edge from __resolveReference should exist"

    def test_uses_loader_edge_created(self, service_root, populated_store):
        resolver_path = str(service_root / "app" / "resolvers" / "study.js")
        fn_qn = f"{resolver_path}::resolveLinkedStudies"
        edges = populated_store.get_edges_by_source(fn_qn)
        uses_loader = [e for e in edges if e.kind == "USES_LOADER"]
        assert uses_loader, "USES_LOADER edge from resolveLinkedStudies should exist"

    def test_no_schema_gql_returns_zero_services(self, tmp_path, store):
        (tmp_path / "src").mkdir()
        stats = extract_graphql_for_repo(tmp_path, store)
        assert stats["services"] == 0

    def test_multiple_services(self, tmp_path, store):
        _make_service(tmp_path / "service_a")
        _make_service(tmp_path / "service_b")
        stats = extract_graphql_for_repo(tmp_path, store)
        assert stats["services"] == 2

    def test_gqlfield_extra_has_auth(self, service_root, populated_store):
        schema_path = str(service_root / ".schema.gql")
        node = populated_store.get_node(f"{schema_path}::Query.inboxes")
        assert node is not None
        assert node.extra.get("auth", {}).get("groups") == ["ALL_CLINIC"]

    def test_gqltype_extra_has_type_kind(self, service_root, populated_store):
        schema_path = str(service_root / ".schema.gql")
        node = populated_store.get_node(f"{schema_path}::InboxFilterInput")
        assert node is not None
        assert node.extra.get("type_kind") == "input"

    def test_federation_entity_extra(self, service_root, populated_store):
        schema_path = str(service_root / ".schema.gql")
        node = populated_store.get_node(f"{schema_path}::Study")
        assert node is not None
        assert node.extra.get("is_federation_entity") is True
        assert node.extra.get("key_fields") == "id"
