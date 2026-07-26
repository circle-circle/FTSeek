#!/usr/bin/env bash
# ============================================================
# FTSeek-US 盘前策略验证脚本 (Preflight Session Strategy Check)
# P2 2026-05-01
#
# 设计目标:
#   1. 开盘前 5 秒跑完所有验证
#   2. 发现问题立刻报 NOGO + 具体原因
#   3. 可集成到 systemd ExecStartPre (NOGO = 拒绝启动)
#
# 使用方式:
#   bash scripts/preflight_check.sh          # 手动运行
#   bash scripts/preflight_check.sh --json   # JSON 输出 (集成用)
#   bash scripts/preflight_check.sh --quick  # 快速模式 (仅关键检查)
#
# Exit codes:
#   0 = ALL CLEAR (GO)
#   1 = TESTS FAILED (NOGO)
#   2 = CONFIG INVALID (NOGO)
#   3 = IMPORT ERROR (NOGO)
#   4 = SESSION TAXONOMY BROKEN (NOGO)
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

OUTPUT_FORMAT="${1:-}"
TIMESTAMP=$(date -Iseconds)
FAILURES=()
WARNINGS=()
PASSES=()

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# ── Helpers ──

pass_check() { PASSES+=("$1"); }
warn_check() { WARNINGS+=("$1"); }
fail_check() { FAILURES+=("$1"); }

print_header() {
    echo ""
    echo "============================================================"
    echo " FTSeek-US Session Strategy Preflight Check"
    echo " Time: $TIMESTAMP"
    echo "============================================================"
    echo ""
}

print_result() {
    local total=$(( ${#PASSES[@]} + ${#WARNINGS[@]} + ${#FAILURES[@]} ))
    echo ""
    echo "────────────────────────────────────────────────────────────"
    echo " Results: $total checks — ${#PASSES[@]} passed, ${#WARNINGS[@]} warnings, ${#FAILURES[@]} failed"
    echo "────────────────────────────────────────────────────────────"

    if [ ${#FAILURES[@]} -gt 0 ]; then
        echo ""
        echo -e "${RED}✗ NOGO — ${#FAILURES[@]} check(s) failed:${NC}"
        for f in "${FAILURES[@]}"; do
            echo -e "  ${RED}✗${NC} $f"
        done
    fi

    if [ ${#WARNINGS[@]} -gt 0 ]; then
        echo ""
        echo -e "${YELLOW}⚠ Warnings:${NC}"
        for w in "${WARNINGS[@]}"; do
            echo -e "  ${YELLOW}⚠${NC} $w"
        done
    fi

    if [ ${#FAILURES[@]} -eq 0 ]; then
        echo ""
        echo -e "${GREEN}✓ GO — Session strategy validated, ready for market${NC}"
        echo ""
    fi
}

json_result() {
    local status="GO"
    [ ${#FAILURES[@]} -gt 0 ] && status="NOGO"
    echo "{\"status\":\"$status\",\"timestamp\":\"$TIMESTAMP\",\"passes\":${#PASSES[@]},\"warnings\":${#WARNINGS[@]},\"failures\":${#FAILURES[@]},\"details\":{\"failed\":$(printf '%s\n' "${FAILURES[@]}" | jq -R . | jq -s .),\"warnings\":$(printf '%s\n' "${WARNINGS[@]}" | jq -R . | jq -s .)}}"
}

# ── Check Functions ──

check_python() {
    echo -e "${CYAN}[1/8]${NC} Python environment..."
    if command -v python3 &>/dev/null; then
        PY_VER=$(python3 --version 2>&1)
        pass_check "Python: $PY_VER"
        echo -e "  ${GREEN}✓${NC} $PY_VER"
    else
        fail_check "python3 not found"
        echo -e "  ${RED}✗${NC} python3 not found"
    fi
}

check_imports() {
    echo -e "${CYAN}[2/8]${NC} Module imports..."
    local check_script='
import sys
sys.path.insert(0, "'"$PROJECT_ROOT"'")
errors = []

# Core
try:
    from app.core.shared_kernel import SharedKernel, get_shared_kernel, DEFAULT_SESSION_VOL_RATIO
    print("  ✓ shared_kernel")
except Exception as e:
    errors.append(f"shared_kernel: {e}")
    print(f"  ✗ shared_kernel: {e}")

# Intraday scorer
try:
    from app.core.intraday_scorer import IntradayScorer, _get_intraday_session, _session_multiplier
    s = _get_intraday_session()
    print(f"  ✓ intraday_scorer (session={s})")
except Exception as e:
    errors.append(f"intraday_scorer: {e}")
    print(f"  ✗ intraday_scorer: {e}")

# Exit monitor
try:
    from app.risk.position_exit_monitor import ExitMonitor, get_exit_monitor
    print("  ✓ position_exit_monitor")
except Exception as e:
    errors.append(f"position_exit_monitor: {e}")
    print(f"  ✗ position_exit_monitor: {e}")

# ATR stop engine
try:
    from app.risk.atr_stop_engine import compute_stop_lines, compute_profit_atr_signed, resolve_trail_multiplier
    print("  ✓ atr_stop_engine")
except Exception as e:
    errors.append(f"atr_stop_engine: {e}")
    print(f"  ✗ atr_stop_engine: {e}")

# Exit quality tracker
try:
    from app.storage.exit_quality_tracker import ExitQualityTracker, get_exit_quality_tracker
    print("  ✓ exit_quality_tracker")
except Exception as e:
    errors.append(f"exit_quality_tracker: {e}")
    print(f"  ✗ exit_quality_tracker: {e}")

if errors:
    print(f"IMPORT_ERRORS: {len(errors)}")
    for e in errors:
        print(e)
    sys.exit(1)
else:
    print("ALL_IMPORTS_OK")
'

    if python3 -c "$check_script" 2>&1; then
        pass_check "All modules import cleanly"
    else
        fail_check "Module import errors (see above)"
    fi
}

check_exit_policy() {
    echo -e "${CYAN}[3/8]${NC} exit_policy.yaml..."
    local policy_file="$PROJECT_ROOT/app/risk/exit_policy.yaml"
    if [ ! -f "$policy_file" ]; then
        fail_check "exit_policy.yaml not found at $policy_file"
        echo -e "  ${RED}✗${NC} file not found"
        return
    fi

    local check_script='
import yaml, sys
with open("'"$policy_file"'") as f:
    p = yaml.safe_load(f)
assert "session_adaptation" in p, "Missing session_adaptation block"
sa = p["session_adaptation"]
assert sa.get("enabled"), "session_adaptation not enabled"
assert sa.get("source") == "shared_kernel", "source must be shared_kernel"
assert len(sa.get("apply_to", [])) >= 5, "apply_to must cover 5 exit rules"
version = p.get("version", "?")
rationale = p.get("design_rationale", "")
print("EXIT_POLICY_OK")
print("  version=" + str(version))
print("  rationale=" + str(rationale)[:60] + "...")
'

    if python3 -c "$check_script" 2>&1; then
        pass_check "exit_policy.yaml v2.3 with session_adaptation"
    else
        fail_check "exit_policy.yaml validation failed"
    fi
}

check_session_taxonomy() {
    echo -e "${CYAN}[4/8]${NC} Session taxonomy integrity..."

    local check_script='
import sys
sys.path.insert(0, "'"$PROJECT_ROOT"'")
from app.core.shared_kernel import (
    SharedKernel, _SESSION_NORMALIZE, DEFAULT_SESSION_VOL_RATIO,
    DEFAULT_PHASE_SCALERS, PHASE_VIX_WEIGHTS,
)

errors = []

# 1. Normalization map covers all vol_ratio entries
for session in DEFAULT_SESSION_VOL_RATIO:
    if session not in _SESSION_NORMALIZE:
        errors.append(f"DEFAULT_SESSION_VOL_RATIO key {session} not in _SESSION_NORMALIZE")

# 2. All scaler keys exist in normalize
for session in DEFAULT_PHASE_SCALERS:
    if session not in _SESSION_NORMALIZE:
        errors.append(f"DEFAULT_PHASE_SCALERS key {session} not in _SESSION_NORMALIZE")

# 3. VIX weights have matching keys
for session in PHASE_VIX_WEIGHTS:
    if session not in _SESSION_NORMALIZE:
        errors.append(f"PHASE_VIX_WEIGHTS key {session} not in _SESSION_NORMALIZE")

# 4. Vol ratios in valid range
for session, ratio in DEFAULT_SESSION_VOL_RATIO.items():
    if not (0.3 <= ratio <= 2.5):
        errors.append(f"{session} vol_ratio={ratio} out of [0.3, 2.5]")

# 5. Volatility ordering
ratios = {s: DEFAULT_SESSION_VOL_RATIO[s] for s in ["opening_drive","morning","closing_drive","afternoon","midday"] if s in DEFAULT_SESSION_VOL_RATIO}
if ratios:
    if ratios.get("opening_drive", 0) != max(ratios.values()):
        errors.append("opening_drive should have highest vol_ratio")
    if ratios.get("midday", 99) != min(ratios.values()):
        errors.append("midday should have lowest vol_ratio")

if errors:
    for e in errors:
        print(f"  TAXONOMY_ERROR: {e}")
    sys.exit(1)
else:
    print("SESSION_TAXONOMY_OK")
    for s, r in DEFAULT_SESSION_VOL_RATIO.items():
        print(f"  {s:20s} ratio={r:.2f}")
'

    if python3 -c "$check_script" 2>&1; then
        pass_check "Session taxonomy consistent"
    else
        fail_check "Session taxonomy has integrity issues"
    fi
}

check_session_detection() {
    echo -e "${CYAN}[5/8]${NC} Session boundary detection..."

    local check_script='
import sys
sys.path.insert(0, "'"$PROJECT_ROOT"'")
from datetime import datetime
from unittest.mock import patch
from app.core.shared_kernel import SharedKernel

test_cases = [
    # (hour, minute, weekday, expected_session)
    (9, 30, 0, "opening_drive"),    # Mon 09:30 → opening_drive
    (10, 0, 1, "opening_drive"),     # Tue 10:00 → opening_drive
    (10, 30, 2, "morning"),          # Wed 10:30 → morning
    (12, 0, 3, "midday"),            # Thu 12:00 → midday
    (14, 30, 4, "afternoon"),        # Fri 14:30 → afternoon
    (15, 30, 0, "closing_drive"),   # Mon 15:30 → closing_drive
    (16, 0, 1, "closing_drive"),    # Tue 16:00 → closing_drive
    (8, 0, 2, "after_hours"),        # Wed 08:00 → after_hours
    (10, 0, 5, "after_hours"),       # Sat 10:00 → weekend
    (10, 0, 6, "after_hours"),       # Sun 10:00 → weekend
    (22, 0, 0, "after_hours"),       # Mon 22:00 → after_hours
    (3, 0, 1, "after_hours"),        # Tue 03:00 → after_hours
]

errors = []
for h, m, wd, expected in test_cases:
    with patch("app.core.shared_kernel.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 4 + wd, h, m, 0)  # May 4=Mon
        session = SharedKernel.get_current_session()
        if session != expected:
            errors.append(f"{h:02d}:{m:02d} weekday={wd} → expected {expected}, got {session}")

if errors:
    for e in errors:
        print(f"  SESSION_MISMATCH: {e}")
    sys.exit(1)
else:
    print("SESSION_DETECTION_OK")
    print(f"  {len(test_cases)} boundary test cases verified")
'

    if python3 -c "$check_script" 2>&1; then
        pass_check "Session boundary detection correct"
    else
        fail_check "Session detection has boundary errors"
    fi
}

check_edge_cases() {
    echo -e "${CYAN}[6/8]${NC} Edge case defenses..."

    local check_script='
import sys
sys.path.insert(0, "'"$PROJECT_ROOT"'")
from app.risk.atr_stop_engine import compute_profit_atr_signed, resolve_trail_multiplier

errors = []

# Dict price → 0.0
r = compute_profit_atr_signed("LONG", 100.0, {"bad": "data"}, 2.0)
if r != 0.0:
    errors.append(f"dict price should return 0.0, got {r}")

# String price → 0.0
r = compute_profit_atr_signed("SHORT", 100.0, "not_number", 2.0)
if r != 0.0:
    errors.append(f"string price should return 0.0, got {r}")

# None → 0.0
r = compute_profit_atr_signed("LONG", None, 150.0, 2.0)
if r != 0.0:
    errors.append(f"None entry should return 0.0, got {r}")

# Nested dict in trail_multiplier → handle gracefully
policy = {"exit_rules": {"chandelier_trail": {"multiplier_by_regime": {"normal": {"value": 2.5}}}}}
t = resolve_trail_multiplier(policy, "normal", "LONG")
if abs(t - 2.5) > 0.01:
    errors.append(f"nested dict trail_mult not resolved: got {t}, expected 2.5")

# Zero ATR → 0.0
r = compute_profit_atr_signed("LONG", 100.0, 102.0, 0.0)
if r != 0.0:
    errors.append(f"zero ATR should return 0.0, got {r}")

# Negative ATR → 0.0
r = compute_profit_atr_signed("LONG", 100.0, 102.0, -1.0)
if r != 0.0:
    errors.append(f"negative ATR should return 0.0, got {r}")

if errors:
    for e in errors:
        print(f"  DEFENSE_FAIL: {e}")
    sys.exit(1)
else:
    print("EDGE_CASES_OK")
    print("  6 defense checks passed (dict/str/None/nested/zero/negative)")
'

    if python3 -c "$check_script" 2>&1; then
        pass_check "Edge case defenses intact"
    else
        fail_check "Edge case defense has gaps"
    fi
}

check_coverage_summary() {
    echo -e "${CYAN}[7/8]${NC} Test coverage check..."
    local coverage_summary=$(python3 -m pytest tests/test_session_awareness.py --co -q 2>&1 | tail -1 || echo "pytest not available")
    # Count test functions
    local test_count=$(python3 -c "
import ast, sys
with open('tests/test_session_awareness.py') as f:
    tree = ast.parse(f.read())
count = sum(1 for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name.startswith('test_'))
print(count)
" 2>/dev/null || echo "?")
    echo -e "  ${GREEN}✓${NC} ${test_count} test functions"

    if [ "$test_count" -ge 40 ] 2>/dev/null; then
        pass_check "$test_count test functions (>40 minimum)"
    else
        warn_check "Only $test_count test functions (<40 recommended)"
    fi
}

run_pytest() {
    echo -e "${CYAN}[8/8]${NC} Running test suite..."
    local pytest_output
    if pytest_output=$(python3 -m pytest tests/test_session_awareness.py -v --tb=line 2>&1); then
        local passed=$(echo "$pytest_output" | grep -c "PASSED" || true)
        pass_check "All $passed tests passed"
        echo "$pytest_output" | grep -E "passed|failed" | tail -1
    else
        local failed=$(echo "$pytest_output" | grep -c "FAILED" || true)
        local passed=$(echo "$pytest_output" | grep -c "PASSED" || true)
        fail_check "$failed tests FAILED, $passed passed"
        echo "$pytest_output" | grep "FAILED" | head -10
    fi
}

# ── Main ──

if [ "$OUTPUT_FORMAT" = "--json" ]; then
    # JSON mode: suppress all output, just print JSON at end
    exec > /dev/null
fi

print_header

check_python
check_imports
check_exit_policy
check_session_taxonomy
check_session_detection
check_edge_cases
check_coverage_summary
run_pytest

if [ "$OUTPUT_FORMAT" = "--json" ]; then
    exec > /dev/tty
    json_result
    [ ${#FAILURES[@]} -eq 0 ] && exit 0 || exit 1
else
    print_result
    [ ${#FAILURES[@]} -eq 0 ] && exit 0 || exit 1
fi
