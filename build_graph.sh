#!/bin/bash
# build_graph.sh
# Build code-review-graph for each repo, register it, then export all to FalkorDB.

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR="${BASE_DIR:-/Users/trongpham/Workes/itrvn/nodejs/microservices/btcy-bioflux-backend-search_codebase/scripts/repo/ignored/cloned-repos}"
CRG_DIR="$(cd "$(dirname "$0")" && pwd)"

FALKOR_GRAPH="${FALKOR_GRAPH:-bioflux_all}"
FALKOR_HOST="${FALKOR_HOST:-localhost}"
FALKOR_PORT="${FALKOR_PORT:-6380}"
FALKOR_PASSWORD="${FALKOR_PASSWORD:-}"

# ── Helpers ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
log_err()  { echo -e "  ${RED}✗${NC} $*"; }
log_info() { echo -e "  ${YELLOW}→${NC} $*"; }

run_crg() {
  uv --directory "$CRG_DIR" run code-review-graph "$@"
}

# ── Pre-flight ────────────────────────────────────────────────────────────────
if [ ! -d "$BASE_DIR" ]; then
  echo "ERROR: BASE_DIR not found: $BASE_DIR"
  exit 1
fi

if ! command -v uv &>/dev/null; then
  echo "ERROR: uv not found in PATH"
  exit 1
fi

PASS=()
FAIL=()

# ── Main loop ─────────────────────────────────────────────────────────────────
for repo_path in "$BASE_DIR"/*/; do
  repo_name=$(basename "$repo_path")
  echo ""
  echo "══════════════════════════════════════════"
  echo "  $repo_name"
  echo "══════════════════════════════════════════"

  # Step 1: Build
  log_info "Building graph..."
  if ! run_crg build --repo "$repo_path" 2>&1 | sed 's/^/    /'; then
    log_err "Build failed — skipping"
    FAIL+=("$repo_name (build)")
    continue
  fi
  log_ok "Build done"

  # Step 2: Register
  log_info "Registering repo..."
  if ! run_crg register "$repo_path" --alias "$repo_name" 2>&1 | sed 's/^/    /'; then
    log_err "Register failed — skipping export"
    FAIL+=("$repo_name (register)")
    continue
  fi
  log_ok "Registered as '$repo_name'"

  # Step 3: FalkorDB export
  log_info "Exporting to FalkorDB graph '$FALKOR_GRAPH'..."
  falkor_args=(
    falkordb-export
    --repo "$repo_path"
    --graph-name "$FALKOR_GRAPH"
    --host "$FALKOR_HOST"
    --port "$FALKOR_PORT"
  )
  if [ -n "$FALKOR_PASSWORD" ]; then
    falkor_args+=(--password "$FALKOR_PASSWORD")
  fi

  if ! run_crg "${falkor_args[@]}" 2>&1 | sed 's/^/    /'; then
    log_err "FalkorDB export failed"
    FAIL+=("$repo_name (falkordb-export)")
    continue
  fi
  log_ok "Exported to FalkorDB"

  PASS+=("$repo_name")
done

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════"
echo "  SUMMARY"
echo "══════════════════════════════════════════"
echo -e "  ${GREEN}✓ ${#PASS[@]} succeeded${NC}"
for r in "${PASS[@]}"; do echo "    - $r"; done

if [ ${#FAIL[@]} -gt 0 ]; then
  echo -e "  ${RED}✗ ${#FAIL[@]} failed${NC}"
  for r in "${FAIL[@]}"; do echo "    - $r"; done
  exit 1
fi
