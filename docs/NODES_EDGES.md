# Nodes & Edges Reference

Complete reference for all node kinds and edge relationship types in the code-review-graph knowledge graph.

**Summary:** 8 node kinds · 16 edge kinds

---

## Nodes

### Code Structure Nodes (parser)

| Kind | Ý nghĩa | Nguồn |
|------|---------|-------|
| `File` | Source file | Tất cả ngôn ngữ |
| `Function` | Function / method / arrow fn / lambda | Tất cả ngôn ngữ |
| `Class` | Class / struct / module definition | Tất cả ngôn ngữ |
| `Test` | Test function (giống Function, `is_test=True`) | File trong test directory |

### GraphQL Nodes (graphql_extractor)

| Kind | Ý nghĩa | Nguồn |
|------|---------|-------|
| `GQLType` | GraphQL type definition (Query, Mutation, Input, type, enum, interface, union, scalar) | `.schema.gql` |
| `GQLField` | Field trên một GQLType | `.schema.gql` |
| `GQLField` (synthetic) | Placeholder cho federation field không có trong schema local (`is_external=True`) | Được tạo khi emit `RESOLVES_EXTERNAL` |
| `Loader` | DataLoader instance | `utils/loaders/index.js` |

---

### Node Properties

#### File
| Property | Type | Mô tả |
|----------|------|-------|
| `name` | string | Absolute file path |
| `file_path` | string | Giống `name` |
| `language` | string | Ngôn ngữ phát hiện (python, typescript, javascript, …) |
| `line_start` | int | Luôn = 1 |
| `line_end` | int | Tổng số dòng |

#### Function / Test
| Property | Type | Mô tả |
|----------|------|-------|
| `name` | string | Tên function |
| `file_path` | string | File chứa function |
| `line_start` | int | Dòng bắt đầu |
| `line_end` | int | Dòng kết thúc |
| `language` | string | Ngôn ngữ |
| `parent_name` | string? | Class chứa (nếu là method) |
| `params` | string? | Danh sách params dạng text |
| `return_type` | string? | Return type annotation |
| `is_test` | bool | `True` nếu là Test node |

#### Class
| Property | Type | Mô tả |
|----------|------|-------|
| `name` | string | Tên class |
| `file_path` | string | File chứa class |
| `line_start` | int | Dòng bắt đầu |
| `line_end` | int | Dòng kết thúc |
| `parent_name` | string? | Class cha (nested class) |

#### GQLType
| Property | Type | Mô tả |
|----------|------|-------|
| `name` | string | Tên type (e.g. `Query`, `HolterProfile`, `UpdateHolterProfileInput`) |
| `file_path` | string | Path của `.schema.gql` |
| `type_kind` | string | `type` / `input` / `enum` / `interface` / `union` / `scalar` |
| `is_federation_entity` | bool | Có directive `@key` |
| `key_fields` | string | Giá trị `@key(fields: "...")` |
| `is_shareable` | bool | Có directive `@shareable` |
| `is_external` | bool | Có directive `@external` |

#### GQLField
| Property | Type | Mô tả |
|----------|------|-------|
| `name` | string | Tên field (e.g. `holterProfile`, `updateHolterProfile`) |
| `file_path` | string | Path của `.schema.gql` |
| `parent_name` | string | Type chứa field (e.g. `Query`, `Mutation`, `HolterProfile`) |
| `return_type` | string | Return type của field |
| `operation` | string | `query` / `mutation` / `subscription` / `type_field` |
| `return_type_is_list` | bool | Return type có phải list |
| `return_type_nullable` | bool | Có nullable |
| `auth` | dict (JSON) | Auth directive `{groups: [...], roles: [...]}` |
| `directives` | list (JSON) | Danh sách directives (e.g. `["@auth", "@deprecated"]`) |
| `is_external` | bool | `True` nếu là synthetic placeholder (federation field) |

#### Loader
| Property | Type | Mô tả |
|----------|------|-------|
| `name` | string | Tên loader (e.g. `holterProfile`) |
| `file_path` | string | Path của `loaders/index.js` |
| `parent_name` | string | Luôn = `"loaders"` |
| `batch_function` | string | Tên batch function |

---

## Edges

### Code Structure Edges (parser)

| Kind | Source → Target | Ý nghĩa |
|------|----------------|---------|
| `CALLS` | Function → Function | Function gọi function khác |
| `IMPORTS_FROM` | File → File | Import / require |
| `CONTAINS` | File/Class → Function/Class | File chứa function; Class chứa method |
| `INHERITS` | Class → Class | Kế thừa / extends |
| `TESTED_BY` | Function → Test | Function được cover bởi test |
| `DEPENDS_ON` | File → File | Dependency không phải import trực tiếp |
| `REFERENCES` | Function → Function/Class | Tham chiếu không phải call |

### GraphQL Schema Edges

| Kind | Source → Target | Ý nghĩa |
|------|----------------|---------|
| `FIELD_OF` | GQLField → GQLType | Field thuộc về type nào |
| `RETURNS` | GQLField → GQLType | Return type của field |
| `ACCEPTS` | GQLField → GQLType | Argument type (Input type) của field |

### GraphQL Runtime Edges (resolver / datasource)

| Kind | Source → Target | Ý nghĩa |
|------|----------------|---------|
| `RESOLVES` | Function → GQLField | Resolver xử lý field trong schema local |
| `RESOLVES_EXTERNAL` | Function → GQLField (synthetic) | Resolver xử lý field từ federated service khác |
| `RESOLVES_REF` | Function → GQLType | `__resolveReference` — federation entity resolver |
| `DELEGATES_TO` | Function → Function | Resolver gọi datasource / controller |
| `USES_LOADER` | Function → Loader | Resolver dùng DataLoader |
| `BACKED_BY` | Loader → Function | Loader backed by batch function |

---

## Full Flow Example

```
Mutation.updateHolterProfile  [GQLField, operation=mutation]
    │
    ├─ ACCEPTS ──────────────────→ UpdateHolterProfileInput  [GQLType, type_kind=input]
    │                                   └─ FIELD_OF ←── id, ecgDisclosure, ...  [GQLField]
    │
    ├─ RETURNS ──────────────────→ MutationResponse  [GQLType, type_kind=type]
    │
    └─ ← RESOLVES ───────────────── updateHolterProfile  [Function, mutation.js]
                                         │
                                         └─ DELEGATES_TO ──→ updateHolterProfile  [Function, holterProfileCommand.js]
                                                                  │
                                                                  └─ CALLS ──→ ... [Function]
```

```
HolterProfile.study  [GQLField, is_external=True, synthetic]
    │
    └─ ← RESOLVES_EXTERNAL ──── resolveStudy  [Function, ecgBookmark.js]
```

---

## Qualified Name Format

```
# File node
/absolute/path/to/file.js

# Top-level function
/absolute/path/to/file.js::functionName

# Method in class
/absolute/path/to/file.py::ClassName.method_name

# GQLType
/path/to/.schema.gql::TypeName

# GQLField
/path/to/.schema.gql::TypeName.fieldName

# Loader
/path/to/loaders/index.js::loaders.loaderName
```
