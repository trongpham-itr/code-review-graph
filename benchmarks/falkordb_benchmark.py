"""
FalkorDB Cypher Benchmark Runner
Loads test cases from goldendataset.json (same directory).

Usage:
    python benchmarks/falkordb_benchmark.py
    python benchmarks/falkordb_benchmark.py --host localhost --port 6380 --graph bioflux_all
    python benchmarks/falkordb_benchmark.py --category node_counts
    python benchmarks/falkordb_benchmark.py --filter TC-001
    python benchmarks/falkordb_benchmark.py --output results.json
    python benchmarks/falkordb_benchmark.py --list
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional

try:
    from falkordb import FalkorDB
except ImportError:
    print("ERROR: falkordb package not installed. Run: pip install falkordb")
    sys.exit(1)

DATASET_PATH = Path(__file__).parent / "goldendataset.json"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class TestCase:
    id: str
    description: str
    category: str
    query: str
    expected: Any
    match_mode: str = "exact"


@dataclass
class TestResult:
    id: str
    description: str
    category: str
    status: str           # PASS | FAIL | ERROR
    expected: Any
    actual: Any
    duration_ms: float
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Load golden dataset
# ---------------------------------------------------------------------------

def load_dataset(path: Path) -> list[TestCase]:
    with open(path) as f:
        raw = json.load(f)
    return [
        TestCase(
            id=tc["id"],
            description=tc["description"],
            category=tc["category"],
            query=tc["query"],
            expected=tc["expected"],
            match_mode=tc.get("match_mode", "exact"),
        )
        for tc in raw
    ]


# ---------------------------------------------------------------------------
# Result extraction
# ---------------------------------------------------------------------------

def _extract_scalar(result_set: Any) -> Any:
    try:
        rows = list(result_set.result_set)
        if not rows:
            return None
        first_row = rows[0]
        if hasattr(first_row, "__iter__") and not isinstance(first_row, str):
            vals = list(first_row)
            return vals[0] if len(vals) == 1 else vals
        return first_row
    except Exception:
        return None


def _extract_column(result_set: Any) -> list:
    try:
        rows = list(result_set.result_set)
        out = []
        for row in rows:
            if hasattr(row, "__iter__") and not isinstance(row, str):
                vals = list(row)
                out.append(vals[0] if vals else None)
            else:
                out.append(row)
        return out
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Assertion
# ---------------------------------------------------------------------------

def _assert(tc: TestCase, actual: Any) -> tuple[str, str | None]:
    exp = tc.expected

    if tc.match_mode == "exact":
        if actual == exp:
            return "PASS", None
        try:
            if int(actual) == int(exp):
                return "PASS", None
        except (TypeError, ValueError):
            pass
        return "FAIL", f"expected={exp!r}  actual={actual!r}"

    if tc.match_mode == "gte":
        try:
            if int(actual) >= int(exp):
                return "PASS", None
            return "FAIL", f"expected>={exp}  actual={actual}"
        except (TypeError, ValueError):
            return "FAIL", f"cannot compare gte: actual={actual!r}"

    if tc.match_mode == "contains":
        haystack = actual if isinstance(actual, list) else [actual]
        if exp in haystack:
            return "PASS", None
        return "FAIL", f"expected to contain {exp!r}, got {haystack!r}"

    return "PASS", None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_test(graph: Any, tc: TestCase) -> TestResult:
    start = time.perf_counter()
    try:
        result = graph.query(tc.query)
        duration_ms = (time.perf_counter() - start) * 1000

        actual = _extract_column(result) if tc.match_mode == "contains" else _extract_scalar(result)
        status, error = _assert(tc, actual)

        return TestResult(
            id=tc.id,
            description=tc.description,
            category=tc.category,
            status=status,
            expected=tc.expected,
            actual=actual,
            duration_ms=round(duration_ms, 3),
            error=error,
        )
    except Exception as exc:
        duration_ms = (time.perf_counter() - start) * 1000
        return TestResult(
            id=tc.id,
            description=tc.description,
            category=tc.category,
            status="ERROR",
            expected=tc.expected,
            actual=None,
            duration_ms=round(duration_ms, 3),
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

ANSI = {
    "green":  "\033[92m",
    "red":    "\033[91m",
    "yellow": "\033[93m",
    "cyan":   "\033[96m",
    "bold":   "\033[1m",
    "reset":  "\033[0m",
}


def _c(text: str, *codes: str) -> str:
    return "".join(ANSI[c] for c in codes) + text + ANSI["reset"]


def print_result(r: TestResult, verbose: bool = False) -> None:
    icons = {"PASS": "✓", "FAIL": "✗", "ERROR": "!"}
    colors = {"PASS": "green", "FAIL": "red", "ERROR": "yellow"}
    icon  = icons.get(r.status, "?")
    color = colors.get(r.status, "reset")

    line = f"  {_c(icon, color)} [{r.id}] {r.description:<60} {r.duration_ms:>8.2f}ms"
    print(line)
    if r.status in ("FAIL", "ERROR"):
        print(f"      {_c(r.error or '', 'red')}")


def print_summary(results: list[TestResult]) -> None:
    total    = len(results)
    passed   = sum(1 for r in results if r.status == "PASS")
    failed   = sum(1 for r in results if r.status == "FAIL")
    errors   = sum(1 for r in results if r.status == "ERROR")
    total_ms = sum(r.duration_ms for r in results)
    avg_ms   = total_ms / total if total else 0

    pct      = passed / total * 100 if total else 0
    bar_len  = 40
    filled   = int(bar_len * passed / total) if total else 0
    bar      = _c("█" * filled, "green") + "░" * (bar_len - filled)

    print()
    print(_c("─" * 70, "bold"))
    print(_c("BENCHMARK SUMMARY", "bold"))
    print(_c("─" * 70, "bold"))
    print(f"  {bar}  {pct:.1f}%")
    print(f"  Total   : {total}")
    print(f"  {_c('Passed', 'green')}  : {passed}")
    print(f"  {_c('Failed', 'red')}  : {failed}")
    print(f"  {_c('Errors', 'yellow')}  : {errors}")
    print(f"  Total time : {total_ms:.1f}ms  |  Avg: {avg_ms:.1f}ms/query")

    # Per-category
    by_cat: dict[str, dict] = {}
    for r in results:
        s = by_cat.setdefault(r.category, {"pass": 0, "fail": 0, "error": 0, "total": 0, "ms": 0.0})
        s["total"] += 1
        s["ms"] += r.duration_ms
        if r.status == "PASS":
            s["pass"] += 1
        elif r.status == "FAIL":
            s["fail"] += 1
        elif r.status == "ERROR":
            s["error"] += 1

    print()
    print("  Per-category:")
    for cat, s in sorted(by_cat.items()):
        ok = s["fail"] == 0 and s["error"] == 0
        mark = _c("✓", "green") if ok else _c("✗", "red")
        print(f"    {mark} {cat:<25} {s['pass']}/{s['total']}  ({s['ms']:.0f}ms)")

    # Slowest 5
    slowest = sorted(results, key=lambda r: r.duration_ms, reverse=True)[:5]
    print()
    print("  Slowest queries:")
    for r in slowest:
        print(f"    [{r.id}] {r.description[:50]:<50}  {r.duration_ms:.1f}ms")
    print()


def save_json(results: list[TestResult], path: str) -> None:
    data = {
        "graph": "bioflux_all",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "summary": {
            "total":    len(results),
            "passed":   sum(1 for r in results if r.status == "PASS"),
            "failed":   sum(1 for r in results if r.status == "FAIL"),
            "errors":   sum(1 for r in results if r.status == "ERROR"),
            "total_ms": sum(r.duration_ms for r in results),
        },
        "results": [asdict(r) for r in results],
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  Results saved → {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="FalkorDB Cypher Benchmark Runner")
    parser.add_argument("--host",     default="localhost",   help="FalkorDB host (default: localhost)")
    parser.add_argument("--port",     default=6380, type=int, help="FalkorDB port (default: 6380)")
    parser.add_argument("--graph",    default="bioflux_all", help="Graph name (default: bioflux_all)")
    parser.add_argument("--dataset",  default=str(DATASET_PATH), help="Path to goldendataset.json")
    parser.add_argument("--category", default=None, help="Run only a specific category")
    parser.add_argument("--filter",   default=None, help="Run only a specific TC id (e.g. TC-001)")
    parser.add_argument("--output",   default=None, help="Save results to JSON file")
    parser.add_argument("--list",     action="store_true", help="List all test cases and exit")
    args = parser.parse_args()

    # Load dataset
    dataset_path = Path(args.dataset)
    if not dataset_path.exists():
        print(f"ERROR: dataset not found at {dataset_path}")
        sys.exit(1)
    cases = load_dataset(dataset_path)

    # --list
    if args.list:
        categories = sorted({tc.category for tc in cases})
        for cat in categories:
            print(f"\n[{cat}]")
            for tc in cases:
                if tc.category == cat:
                    print(f"  [{tc.id}] {tc.description}")
        return

    # Filter
    if args.category:
        cases = [tc for tc in cases if tc.category == args.category]
    if args.filter:
        cases = [tc for tc in cases if tc.id == args.filter.upper()]

    if not cases:
        print("No test cases matched. Use --list to see available cases.")
        sys.exit(1)

    # Connect
    print(_c(f"\nFalkorDB Benchmark  →  {args.host}:{args.port}  graph={args.graph}", "bold"))
    print(_c(f"Dataset: {dataset_path}  ({len(cases)} cases)", "cyan"))
    print(_c("─" * 70, "bold"))
    try:
        db = FalkorDB(host=args.host, port=args.port)
        graph = db.select_graph(args.graph)
        graph.query("MATCH (n) RETURN count(n) LIMIT 1")
        print(_c("  ✓ Connected\n", "green"))
    except Exception as exc:
        print(_c(f"  ✗ Connection failed: {exc}", "red"))
        sys.exit(1)

    # Run
    results: list[TestResult] = []
    current_cat = None
    for tc in cases:
        if tc.category != current_cat:
            current_cat = tc.category
            print(_c(f"\n  [{current_cat}]", "cyan"))
        r = run_test(graph, tc)
        results.append(r)
        print_result(r)

    # Summary + optional JSON export
    print_summary(results)
    if args.output:
        save_json(results, args.output)

    sys.exit(1 if any(r.status in ("FAIL", "ERROR") for r in results) else 0)


if __name__ == "__main__":
    main()
