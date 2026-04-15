# Benchmark Design — code-review-graph

Tài liệu này mô tả chiến lược benchmark cho hai tầng chính:

1. **Graph accuracy** — edge sinh ra có đúng không (parser + graphql_extractor)
2. **Impact analysis quality** — BFS direct/indirect có match thực tế không

---

## Tổng quan

```
┌─────────────────────────────────────────────────────────┐
│                     Benchmark Suite                      │
│                                                         │
│  Layer 1: Edge Accuracy          Layer 2: Impact Quality│
│  ┌─────────────────────┐         ┌────────────────────┐ │
│  │ Synthetic fixtures  │         │ Annotated cases    │ │
│  │ - CALLS             │         │ - expected_direct  │ │
│  │ - DELEGATES_TO      │         │ - expected_indirect│ │
│  │ - RESOLVES          │         │ - forbidden ops    │ │
│  │ - IMPORTS_FROM      │         └────────────────────┘ │
│  └─────────────────────┘                                │
│                                                         │
│  Layer 3: Regression snapshots (anti-degradation)       │
└─────────────────────────────────────────────────────────┘
```

---

## Layer 1 — Edge Accuracy (Synthetic Fixtures)

### Mục tiêu

Kiểm tra parser và graphql_extractor sinh đúng edges với từng pattern JS/TS phổ biến. Mỗi fixture là một **mini-repo** tự chứa với cấu trúc biết trước → ground truth 100%.

### Fixture structure

```
tests/fixtures/benchmark/
  js_direct_call/
    resolvers/query.js
    datasources/studyCommand.js
    expected_edges.json
  js_alias_export/
    resolvers/mutation.js
    datasources/reportCommand.js       # module.exports = { fn: actualFn }
    expected_edges.json
  js_destructured_datasource/
    resolvers/mutation.js              # const { mongodb } = dataSources
    expected_edges.json
  js_spread_barrel/
    resolvers/mutation.js              # module.exports = { ...eventMutation }
    resolvers/mutation/event.js
    expected_edges.json
  ts_delegates/
    resolvers/query.resolver.ts
    controllers/study.controller.ts
    expected_edges.json
  gql_federation/
    schema/.schema.gql                 # @key(fields: "id") + @external
    resolvers/reference.js             # __resolveReference
    expected_edges.json
```

### Format `expected_edges.json`

```json
{
  "must_exist": [
    {
      "kind": "DELEGATES_TO",
      "source_suffix": "query.js::getStudy",
      "target_suffix": "studyCommand.js::getStudy"
    },
    {
      "kind": "RESOLVES",
      "source_suffix": "query.js::getStudy",
      "target_suffix": ".schema.gql::Query.study"
    }
  ],
  "must_not_exist": [
    {
      "kind": "DELEGATES_TO",
      "source_suffix": "query.js::getStudy",
      "target_suffix": "query.js::getStudy"
    }
  ]
}
```

### Metrics — Layer 1

| Metric | Định nghĩa | Target |
|--------|-----------|--------|
| **Edge precision** | `must_exist edges found / must_exist total` | ≥ 95% |
| **False positive rate** | `must_not_exist edges found / must_not_exist total` | = 0% |
| **Pattern coverage** | Số fixture patterns pass / tổng | 100% |

### Patterns cần cover

| Pattern | Fixture | Độ khó |
|---------|---------|--------|
| Direct call `dataSources.fn()` | `js_direct_call` | Cơ bản |
| Destructured `const { mongodb } = dataSources` | `js_destructured_datasource` | Trung bình |
| CJS alias export `{ fn: actualFn }` | `js_alias_export` | Trung bình |
| Spread barrel `{ ...childModule }` | `js_spread_barrel` | Cao |
| Federation `__resolveReference` | `gql_federation` | Cao |
| TypeScript `@Resolver()` decorator | `ts_delegates` | Trung bình |
| Async `Promise.all([fn1(), fn2()])` | `js_promise_all` | Cao |
| Conditional delegation `if/else ds.X()` | `js_conditional` | Cao |

---

## Layer 2 — Impact Analysis Quality (Annotated Cases)

### Mục tiêu

Với một function thực trong real repo, kiểm tra BFS tìm đúng các GraphQL operations bị ảnh hưởng (direct + indirect).

### Annotation format

```yaml
# tests/benchmark/cases.yaml

cases:
  - id: study_api_createSelectedFields
    repo: btcy-bioflux-backend-study_api
    function: createSelectedFields
    file_suffix: datasources/utils/others/selectFields.js
    expected_direct:
      - Study.info
      - Study.linkedStudies
      - Study.patientReturn
    expected_indirect_contains:
      - Query.study
      - Query.clinicStudies
      - Mutation.createStudy
      - Mutation.updateStudy
    expected_indirect_min: 20
    forbidden:          # ops không được xuất hiện (false positives)
      - Mutation.login
      - Query.users

  - id: support_api_assignCurrentDevices
    repo: btcy-bioflux-backend-support_api
    function: assignCurrentDevices
    file_suffix: datasources/deviceHistory/deviceHistoryCommand.js
    expected_direct_min: 10
    expected_indirect_max: 5   # function này không nên có nhiều indirect
    forbidden: []

  - id: study_api_handleGraphqlError
    repo: btcy-bioflux-backend-study_api
    function: handleGraphqlError
    file_suffix: utils/others/error.js
    expected_indirect_min: 50
    expected_direct_max: 0     # error handler không được gọi direct từ resolver

  - id: auth_api_isPermissionUser
    repo: btcy-bioflux-backend-auth_api
    function: isPermissionUser
    file_suffix: utils/controllers/userUtils.js
    expected_indirect_contains:
      - Mutation.login
      - Mutation.changePassword
    expected_indirect_min: 6
```

### Metrics — Layer 2

| Metric | Định nghĩa | Target |
|--------|-----------|--------|
| **Direct recall** | `expected_direct found / expected_direct total` | 100% |
| **Indirect recall** | `expected_indirect_contains found / total` | 100% |
| **Min coverage** | `actual_count >= expected_min` | Pass |
| **Max guard** | `actual_count <= expected_max` (nếu có) | Pass |
| **Zero false positives** | `forbidden ops not in actual` | 100% |

---

## Layer 3 — Regression Snapshots

### Mục tiêu

Phát hiện degradation sau khi thay đổi parser/extractor — không cần ground truth, chỉ cần output ổn định và không giảm.

### Snapshot format

```json
{
  "repo": "btcy-bioflux-backend-study_api",
  "captured_at": "2026-04-14",
  "stats": {
    "total_nodes": 1842,
    "total_edges": 3201,
    "resolves_edges": 156,
    "delegates_to_edges": 98,
    "op_coverage_pct": 94.2
  },
  "impact_samples": [
    {
      "function": "createSelectedFields",
      "total_affected": 27,
      "direct": 3,
      "indirect": 24
    }
  ]
}
```

### Regression rules

```
total_nodes      >= baseline * 0.98   # cho phép ±2%
total_edges      >= baseline * 0.98
resolves_edges   >= baseline          # edges không được giảm
delegates_to_edges >= baseline        # edges không được giảm
op_coverage_pct  >= baseline - 1.0   # coverage không giảm hơn 1%
impact_samples[*].total_affected >= baseline_value * 0.95
```

---

## Runner & CI integration

### Chạy benchmark

```bash
# Layer 1 — synthetic fixtures
uv run pytest tests/test_benchmark_edges.py -v

# Layer 2 — annotated impact cases
uv run pytest tests/test_benchmark_impact.py -v

# Layer 3 — regression check
uv run code-review-graph eval --snapshot-check

# Tất cả
uv run code-review-graph eval --benchmark
```

### Output report

```
Benchmark Results — 2026-04-14
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Layer 1: Edge Accuracy
  Fixtures passed:     12 / 14  (85.7%)   ← target ≥ 95%
  Edge precision:      96.3%              ✓
  False positive rate: 0.0%               ✓
  Failing patterns:    js_promise_all, js_conditional

Layer 2: Impact Analysis
  Cases passed:        6 / 7   (85.7%)    ← target 100%
  Direct recall:       100%               ✓
  Indirect recall:     91.4%              ✗  (missing 3 ops in study_api)
  False positives:     0                  ✓

Layer 3: Regression
  Snapshots checked:   14 repos
  Degraded:            0                  ✓
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Overall: PARTIAL PASS (2/3 layers green)
```

---

## Ưu tiên implement

| Bước | Việc cần làm | Effort |
|------|-------------|--------|
| 1 | Viết 4 synthetic fixtures cơ bản (direct call, alias, destructure, spread) | 1 ngày |
| 2 | Annotate 5 cases thực từ study_api + support_api | 0.5 ngày |
| 3 | Implement `test_benchmark_edges.py` chạy expected_edges.json | 1 ngày |
| 4 | Implement `test_benchmark_impact.py` chạy cases.yaml | 1 ngày |
| 5 | Capture regression snapshots cho 14 repos | 0.5 ngày |
| 6 | Wire vào `code-review-graph eval --benchmark` + CI | 1 ngày |

---

## Điểm yếu cần chú ý khi benchmark

1. **Graph phải được rebuild** trước khi chạy Layer 2/3 — kết quả BFS phụ thuộc vào graph.db hiện tại
2. **Indirect depth không ổn định** nếu có cycle trong CALLS — BFS đã có `visited` set nhưng depth có thể thay đổi tuỳ order
3. **Alias export** là pattern dễ miss nhất — cần fixture riêng để pin down
4. **Federation external fields** — synthetic placeholder nodes không có file thật → fixture phải mock đúng cấu trúc
