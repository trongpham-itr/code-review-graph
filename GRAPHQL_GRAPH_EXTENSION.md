# GraphQL Graph Extension — Design Document

Tài liệu mô tả thiết kế mở rộng `graph.db` để hỗ trợ phân tích GraphQL resolvers,
bao gồm các node/edge mới, nguồn dữ liệu, và logic parse tương ứng.

---

## 1. Bối cảnh

### Hiện trạng

`graph.db` hiện tại phân tích **3,138 files** (JavaScript, Go, Python) với các node:
`File`, `Function`, `Class`, `Test` và edges: `CALLS`, `CONTAINS`, `IMPORTS_FROM`,
`INHERITS`, `TESTED_BY`.

**Vấn đề:** Toàn bộ GraphQL schema bị bỏ qua — 0 nodes được index.
Graph biết resolver function tồn tại, nhưng không biết nó resolve field gì,
nhận input type nào, trả về type nào, hay dùng loader nào.

### Cấu trúc project GraphQL (ví dụ: `clinic_api`, `study_api`)

```
(root)/
  .schema.gql           ← compiled/merged schema — SOURCE DUY NHẤT để parse GQL nodes
app/
  schemas/              ← source gốc của từng .graphql (không parse trực tiếp)
    queries/
    mutations/
    types/
    inputs/
    responses/
    directives/
  resolvers/
    index.js            ← { Query: queryResolver, Mutation: mutResolver, Study: studyResolver }
    query.js            ← module.exports = { inboxes, physicians, ... }
    mutation.js         ← module.exports = { markInbox, editUserSetting, ... }
    study.js            ← module.exports = { linkedStudies: resolveLinkedStudies, ... }
    custom.js           ← module.exports = { DateTime: GraphQLDateTime }
  utils/loaders/
    index.js            ← createLoaders() { study: new DataLoader(batchStudies), ... }
  datasources/loaders/
    study.js            ← async function batchStudies(keys) { ... }
    studySetting.js     ← async function batchStudySetting(keys) { ... }
  typeDefs.js           ← load và merge các .graphql file → sinh ra .schema.gql
```

### Tại sao dùng `.schema.gql` thay vì parse từng `.graphql`?

`.schema.gql` là file **compiled/merged** — kết quả sau khi `typeDefs.js` load và
merge toàn bộ các file con theo thứ tự `directives → types → inputs → mutations → queries → responses`.

| | `.schema.gql` | Từng `.graphql` file |
| --- | --- | --- |
| Số file cần đọc | **1 file/service** | ~70 file/service |
| Merge/load order | Không cần xử lý | Phải đọc `typeDefs.js` |
| Schema đầy đủ | Có — kể cả Federation directives | Cần ghép thủ công |
| Luôn tồn tại | Có trong tất cả GraphQL services | Luôn có |
| `file_path` của node | `.schema.gql` (chấp nhận được) | Đúng file gốc |
| Có thể stale | Nếu quên regenerate | Không — là source gốc |

→ **Quyết định:** Parse `.schema.gql` làm nguồn duy nhất cho GQLField và GQLType nodes.
`file_path` của tất cả GQL nodes trỏ về `.schema.gql` — đủ để biết node thuộc service nào
thông qua `qualified_name`.

---

## 2. Node Kinds mới

### 2.1 `GQLField`

Một field trong `type Query`, `type Mutation`, hoặc một custom type **có explicit resolver**.

| Column | Giá trị |
|--------|---------|
| `kind` | `"GQLField"` |
| `name` | `"inboxes"`, `"linkedStudies"` |
| `qualified_name` | `"clinic_api::Query.inboxes"`, `"study_api::Study.linkedStudies"` |
| `parent_name` | `"Query"`, `"Mutation"`, `"Study"` |
| `file_path` | path tới `.schema.gql` của service |
| `language` | `"graphql"` |
| `extra` (JSON) | xem bên dưới |

**`extra` schema:**
```json
{
  "operation":           "query | mutation | type_field",
  "return_type":         "InboxesResponse",
  "return_type_is_list": false,
  "return_type_nullable": true,
  "auth": {
    "groups": ["ALL_CLINIC"],
    "roles":  ["CLINIC_PHYSICIAN"]
  },
  "directives":          ["@auth", "@constraint"],
  "is_federation_key":   false
}
```

**Nguồn:** Parse `.schema.gql` — lấy tất cả fields trong `type Query`, `type Mutation`,
và fields của custom types **có key tương ứng trong resolver file exports**.

---

### 2.2 `GQLType`

Một type definition trong GraphQL schema: response type, input type, entity type.

| Column | Giá trị |
|--------|---------|
| `kind` | `"GQLType"` |
| `name` | `"InboxesResponse"`, `"InboxFilterInput"`, `"Study"` |
| `qualified_name` | `"clinic_api::InboxesResponse"`, `"study_api::Study"` |
| `file_path` | path tới `.schema.gql` của service |
| `language` | `"graphql"` |
| `extra` (JSON) | xem bên dưới |

**`extra` schema:**
```json
{
  "type_kind":            "type | input | scalar | enum | interface",
  "is_shareable":         true,
  "is_external":          false,
  "is_federation_entity": true,
  "key_fields":           "id"
}
```

**Nguồn:** Parse `.schema.gql` — tất cả `type`, `input`, `enum`, `scalar` definitions.
Bỏ qua built-in scalars (`String`, `Int`, `Float`, `Boolean`, `ID`) và
Federation internals (`_Entity`, `_Service`, `_Any`, `link__*`, `federation__*`).

---

### 2.3 `Loader`

Một DataLoader instance được tạo trong `createLoaders()`, đại diện cho
một batch-loading operation.

| Column | Giá trị |
|--------|---------|
| `kind` | `"Loader"` |
| `name` | `"study"`, `"lastStudyHistory"`, `"patientReturn"` |
| `qualified_name` | `"study_api::loaders.study"` |
| `file_path` | path tới `utils/loaders/index.js` |
| `language` | `"javascript"` |
| `extra` (JSON) | `{ "batch_function": "batchStudies" }` |

**Nguồn:** Parse `utils/loaders/index.js` — detect pattern `new DataLoader(keys => batchFn(keys))`.

---

## 3. Edge Kinds mới

### 3.1 `RESOLVES`

Hàm JavaScript xử lý (resolve) một GraphQL field.

| Field | Giá trị |
|-------|---------|
| `kind` | `"RESOLVES"` |
| `source_qualified` | qualified name của Function node |
| `target_qualified` | qualified name của GQLField node |

**Ví dụ:**
```
Function("clinic_api::resolvers/query.js::inboxes")
  ──RESOLVES──► GQLField("clinic_api::Query.inboxes")

Function("study_api::resolvers/study.js::resolveLinkedStudies")
  ──RESOLVES──► GQLField("study_api::Study.linkedStudies")
```

**Logic detect:**

*Query/Mutation resolver:*
```
1. resolvers/index.js: "Query: queryResolver" → file = ./query.js
2. query.js module.exports = { inboxes, physicians, ... }
   → key "inboxes" = tên field
3. .schema.gql: "extend type Query { inboxes(...) }" → xác nhận field tồn tại
4. Edge: Function(inboxes) ──RESOLVES──► GQLField(Query.inboxes)
```

*Type field resolver:*
```
1. resolvers/index.js: "Study: studyResolver" → type = Study, file = ./study.js
2. study.js module.exports = { linkedStudies: resolveLinkedStudies, ... }
   → key "linkedStudies" = tên field trên type Study
3. .schema.gql: "type Study { linkedStudies: [Study] }" → xác nhận field tồn tại
4. Edge: Function(resolveLinkedStudies) ──RESOLVES──► GQLField(Study.linkedStudies)
```

---

### 3.2 `RESOLVES_REF`

Hàm `__resolveReference` xử lý Apollo Federation entity resolution.

| Field | Giá trị |
|-------|---------|
| `kind` | `"RESOLVES_REF"` |
| `source_qualified` | qualified name của Function(`__resolveReference`) |
| `target_qualified` | qualified name của GQLType (entity type) |

**Ví dụ:**
```
Function("study_api::resolvers/study.js::__resolveReference")
  ──RESOLVES_REF──► GQLType("study_api::Study")
```

**Logic detect:**
```
1. Tìm key "__resolveReference" trong module.exports của resolver file
2. Parent type = key tương ứng trong resolvers/index.js
   ("Study: studyResolver" → type = Study)
3. Xác nhận type Study có @key directive trong .schema.gql
4. Edge: Function(__resolveReference) ──RESOLVES_REF──► GQLType(Study)
```

---

### 3.3 `FIELD_OF`

GQLField thuộc về GQLType nào.

| Field | Giá trị |
|-------|---------|
| `kind` | `"FIELD_OF"` |
| `source_qualified` | qualified name của GQLField |
| `target_qualified` | qualified name của GQLType (parent type) |

**Ví dụ:**
```
GQLField("study_api::Study.linkedStudies") ──FIELD_OF──► GQLType("study_api::Study")
GQLField("clinic_api::Query.inboxes")      ──FIELD_OF──► GQLType("clinic_api::Query")
```

**Logic detect:**
```
Parse .schema.gql:
  "type Study { linkedStudies: [Study] }" → parent = Study
  "extend type Query { inboxes(...) }"    → parent = Query
  (extend type Query = same as type Query, merge về cùng GQLType(Query))
```

---

### 3.4 `RETURNS`

GQLField trả về GQLType nào.

| Field | Giá trị |
|-------|---------|
| `kind` | `"RETURNS"` |
| `source_qualified` | qualified name của GQLField |
| `target_qualified` | qualified name của GQLType (return type) |

**Ví dụ:**
```
GQLField("clinic_api::Query.inboxes")         ──RETURNS──► GQLType("clinic_api::InboxesResponse")
GQLField("study_api::Study.linkedStudies")    ──RETURNS──► GQLType("study_api::Study")
GQLField("study_api::Study.lastStudyHistory") ──RETURNS──► GQLType("study_api::StudyHistoryItem")
```

**Logic detect:**
```
Parse .schema.gql SDL:
  "inboxes(...): InboxesResponse"  → return type = InboxesResponse
  "linkedStudies: [Study]"         → return type = Study  (unwrap list [] và non-null !)
  Bỏ qua nếu return type là scalar (String, Int, Boolean, ID, DateTime, ...)
```

---

### 3.5 `ACCEPTS`

GQLField nhận input argument thuộc GQLType nào.

| Field | Giá trị |
|-------|---------|
| `kind` | `"ACCEPTS"` |
| `source_qualified` | qualified name của GQLField |
| `target_qualified` | qualified name của GQLType (input type) |

**Ví dụ:**
```
GQLField("clinic_api::Query.inboxes")
  ──ACCEPTS──► GQLType("clinic_api::InboxFilterInput")

GQLField("study_api::Query.getStudyByApp")
  ──ACCEPTS──► GQLType("study_api::GetStudyByAppInput")
```

**Logic detect:**
```
Parse .schema.gql args:
  "inboxes(filter: InboxFilterInput, limit: Int)"
  → chỉ lấy custom types, bỏ qua scalars (Int, String, ID, Boolean, DateTime)
```

---

### 3.6 `USES_LOADER`

Resolver function sử dụng Loader nào để batch-load data.

| Field | Giá trị |
|-------|---------|
| `kind` | `"USES_LOADER"` |
| `source_qualified` | qualified name của Function |
| `target_qualified` | qualified name của Loader |

**Ví dụ:**
```
Function("study_api::resolvers/study.js::resolveLinkedStudies")
  ──USES_LOADER──► Loader("study_api::loaders.study")

Function("study_api::resolvers/study.js::__resolveReference")
  ──USES_LOADER──► Loader("study_api::loaders.study")   ← shared loader
```

**Logic detect:**
```
AST/regex trong resolver .js files:
  pattern: loaders\.(\w+)\.(load|loadMany)\(
  capture group 1 = loader name
  hàm đang chứa call = source function
```

---

### 3.7 `BACKED_BY`

Loader được backed bởi batch function nào.

| Field | Giá trị |
|-------|---------|
| `kind` | `"BACKED_BY"` |
| `source_qualified` | qualified name của Loader |
| `target_qualified` | qualified name của Function (batch function) |

**Ví dụ:**
```
Loader("study_api::loaders.study")
  ──BACKED_BY──► Function("study_api::datasources/loaders/study.js::batchStudies")

Loader("study_api::loaders.lastStudyHistory")
  ──BACKED_BY──► Function("study_api::datasources/loaders/studySetting.js::batchLastStudyHistory")
```

**Logic detect:**
```
Parse utils/loaders/index.js AST:
  key = "study", value = NewExpression(DataLoader, ArrowFn(CallExpr("batchStudies")))
  → Loader(study) ──BACKED_BY──► Function(batchStudies)
```

---

## 4. Flow hoàn chỉnh — 3 trường hợp

### Case 1: Query đơn giản — `clinic_api::Query.inboxes`

```
[.schema.gql]                               [resolvers/index.js]
─────────────                               ────────────────────
extend type Query {                         module.exports = {
  inboxes(                                    Query: queryResolver,   ← ./query.js
    filter: InboxFilterInput,                 ...
    limit: Int                              }
  ): InboxesResponse
  @auth(groups: [ALL_CLINIC])
}                                           [resolvers/query.js]
                                            ────────────────────
input InboxFilterInput { ... }              module.exports = {
type InboxesResponse { ... }                  inboxes,               ← key khớp field name
                                              physicians, ...
                                            }

Parse .schema.gql → nodes:                 Parse query.js exports → edges:
──────────────────                          ──────────────────────────────
[GQLField] Query.inboxes                    Function(inboxes) ──RESOLVES──► GQLField(Query.inboxes)
[GQLType]  InboxesResponse
[GQLType]  InboxFilterInput

Edges từ .schema.gql:
─────────────────────
GQLField(Query.inboxes) ──FIELD_OF──► GQLType(Query)
GQLField(Query.inboxes) ──RETURNS──►  GQLType(InboxesResponse)
GQLField(Query.inboxes) ──ACCEPTS──►  GQLType(InboxFilterInput)
```

---

### Case 2: Type field resolver + Loader — `study_api::Study.linkedStudies`

```
[.schema.gql]                        [resolvers/index.js]
─────────────                        ────────────────────
type Study @key(fields: "id") {      module.exports = {
  linkedStudies: [Study]               Study: studyResolver,  ← type=Study, file=./study.js
  device: Device                       ...
  lastStudyHistory: StudyHistoryItem }
}
                                     [resolvers/study.js]
                                     ────────────────────
                                     module.exports = {
                                       linkedStudies: resolveLinkedStudies,
                                       device:        resolveDevice,
                                       lastStudyHistory: resolveLastStudyHistory,
                                       __resolveReference,
                                     }

[utils/loaders/index.js]             [datasources/loaders/study.js]
────────────────────────             ──────────────────────────────
createLoaders() {                    async function batchStudies(keys) { ... }
  study: new DataLoader(
    keys => batchStudies(keys)       async function batchLastStudyHistory(keys) { ... }
  ),
  lastStudyHistory: new DataLoader(
    keys => batchLastStudyHistory(k)
  ),
}

Graph sinh ra:
─────────────────────────────────────────────────────────────────
Từ .schema.gql:
  [GQLType]  Study
  [GQLField] Study.linkedStudies   ──FIELD_OF──► GQLType(Study)
                                   ──RETURNS──►  GQLType(Study)
  [GQLField] Study.device          ──RETURNS──►  GQLType(Device)
  [GQLField] Study.lastStudyHistory ──RETURNS──► GQLType(StudyHistoryItem)

Từ resolvers/index.js + study.js:
  Function(resolveLinkedStudies)   ──RESOLVES──►    GQLField(Study.linkedStudies)
  Function(resolveDevice)          ──RESOLVES──►    GQLField(Study.device)
  Function(resolveLastStudyHistory)──RESOLVES──►    GQLField(Study.lastStudyHistory)

Từ utils/loaders/index.js:
  [Loader] loaders.study           ──BACKED_BY──►   Function(batchStudies)
  [Loader] loaders.lastStudyHistory──BACKED_BY──►   Function(batchLastStudyHistory)

Từ pattern loaders.X.load() trong study.js:
  Function(resolveLinkedStudies)   ──USES_LOADER──► Loader(loaders.study)
  Function(resolveLastStudyHistory)──USES_LOADER──► Loader(loaders.lastStudyHistory)
  Function(resolveDevice)          (không có USES_LOADER — direct compute)
```

---

### Case 3: Apollo Federation `__resolveReference`

```
[.schema.gql]                        [resolvers/study.js]
─────────────                        ────────────────────
type Study @key(fields: "id") {      function __resolveReference(study, { loaders }, info) {
  id: ID!                              return loaders.study.load(id);
  ...                                }
}
                                     module.exports = { ..., __resolveReference }

Graph sinh ra:
─────────────────────────────────────────────────────────────────
Từ .schema.gql: GQLType(Study) với extra.is_federation_entity = true

Từ resolvers/index.js + study.js:
  Function(__resolveReference) ──RESOLVES_REF──► GQLType(Study)

Từ pattern loaders.X.load() trong study.js:
  Function(__resolveReference) ──USES_LOADER──►  Loader(loaders.study)
                                                  (shared với resolveLinkedStudies)
```

---

## 5. Nguồn dữ liệu — Tổng hợp

```
File                          Sinh ra
─────────────────────────     ───────────────────────────────────────────────
.schema.gql                   [GQLField] nodes — tất cả Query/Mutation fields
                              [GQLField] nodes — type fields có explicit resolver
                              [GQLType]  nodes — tất cả types, inputs, responses
                              FIELD_OF, RETURNS, ACCEPTS edges

resolvers/index.js            Bản đồ: type name → resolver file
                              (không sinh node/edge trực tiếp, dùng để lookup)

resolvers/query.js            RESOLVES edges: Function → GQLField(Query.XXX)
resolvers/mutation.js         RESOLVES edges: Function → GQLField(Mutation.XXX)
resolvers/[type].js           RESOLVES edges: Function → GQLField(TypeName.field)
                              USES_LOADER edges: Function → Loader(X)
                              RESOLVES_REF edges: __resolveReference → GQLType

utils/loaders/index.js        [Loader] nodes
                              BACKED_BY edges: Loader → Function(batchXxx)
```

**Không cần đọc:**

- `app/schemas/**/*.graphql` — đã được merge vào `.schema.gql`
- `typeDefs.js` — chỉ là loader script, không chứa schema thực

---

## 6. Heuristics & Edge Cases

| Tình huống | Xử lý |
|------------|-------|
| `...customResolver` spread trong `index.js` | Unfold, bỏ qua scalars (`DateTime`, `JSON`, `GraphQLDateTime`) — không tạo node |
| Type field không có key trong `module.exports` | Không tạo GQLField node — default resolver, không cần track |
| `__resolveReference` trong exports | Tạo RESOLVES_REF → parent type, không tạo GQLField |
| Return type là built-in scalar | Không tạo GQLType, không tạo RETURNS edge |
| Return type là `[Study]` hoặc `Study!` | Unwrap `[]` và `!` để lấy base type name `Study` |
| Arg type là scalar | Bỏ qua, chỉ tạo ACCEPTS với custom input types |
| `extend type Query` vs `type Query` | Merge thành cùng GQLType(Query) — cùng qualified_name |
| `.schema.gql` stale | Rebuild GQL nodes khi `file_hash` của `.schema.gql` thay đổi |
| Federation internals (`_Entity`, `_Service`, `link__*`) | Bỏ qua — không tạo GQLType node |

---

## 7. Queries hữu ích sau khi có graph

```sql
-- Resolver nào chưa có field tương ứng trong schema? (dead resolver)
SELECT n.qualified_name
FROM nodes n
WHERE n.kind = 'Function'
  AND n.qualified_name LIKE '%resolvers%'
  AND NOT EXISTS (
    SELECT 1 FROM edges e
    WHERE e.kind = 'RESOLVES' AND e.source_qualified = n.qualified_name
  );

-- Field nào trong schema chưa có resolver? (missing resolver)
SELECT n.qualified_name
FROM nodes n
WHERE n.kind = 'GQLField'
  AND NOT EXISTS (
    SELECT 1 FROM edges e
    WHERE e.kind = 'RESOLVES' AND e.target_qualified = n.qualified_name
  );

-- Resolver nào KHÔNG dùng loader khi resolve list? (N+1 risk)
SELECT n.qualified_name, gf.qualified_name as field
FROM nodes n
JOIN edges e1 ON e1.kind = 'RESOLVES' AND e1.source_qualified = n.qualified_name
JOIN nodes gf ON gf.qualified_name = e1.target_qualified
WHERE json_extract(gf.extra, '$.return_type_is_list') = 1
  AND NOT EXISTS (
    SELECT 1 FROM edges e2
    WHERE e2.kind = 'USES_LOADER' AND e2.source_qualified = n.qualified_name
  );

-- Loader nào được share bởi nhiều resolver nhất?
SELECT l.name, COUNT(*) as resolver_count
FROM nodes l
JOIN edges e ON e.kind = 'USES_LOADER' AND e.target_qualified = l.qualified_name
WHERE l.kind = 'Loader'
GROUP BY l.qualified_name
ORDER BY resolver_count DESC;

-- Nếu đổi InboxesResponse, ảnh hưởng đến Query fields nào?
SELECT e.source_qualified as gql_field
FROM edges e
WHERE e.kind = 'RETURNS'
  AND e.target_qualified LIKE '%::InboxesResponse';

-- Mutations nào require role CLINIC_PHYSICIAN?
SELECT qualified_name
FROM nodes
WHERE kind = 'GQLField'
  AND parent_name = 'Mutation'
  AND json_extract(extra, '$.auth.roles') LIKE '%CLINIC_PHYSICIAN%';

-- Trace đầy đủ từ API field xuống DB query (cross .graphql + .js)
-- "Query.inboxes được implement thế nào?"
SELECT e1.source_qualified as resolver_fn,
       e2.target_qualified as calls_fn
FROM edges e1
JOIN edges e2 ON e2.kind = 'CALLS' AND e2.source_qualified = e1.source_qualified
WHERE e1.kind = 'RESOLVES'
  AND e1.target_qualified = 'clinic_api::Query.inboxes';
```
