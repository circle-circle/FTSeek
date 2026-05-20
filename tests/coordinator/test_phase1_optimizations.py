"""Phase 1 数据驱动优化回归测试 (2026-05-19, 今晚开盘前)

5 项基于 5/12-5/18 真实交易数据的优化:
- A.1: 删除 short_min_strength<20 (124 笔被错拒 SHORT)
- B.1: scalping 提前止损 6min 起 (减 <6min 摩擦灾难 -$973)
- B.2: scalping 排除 < $20 低价股 (LCID/TME 21+15 笔 -$1097)
- C.1: scalping regime=TREND_DOWN 禁 LONG (减 LONG 73 笔 2.7% 胜率灾难)
- C.2: 熔断 -0.0% 不计 sl_hit (减 5/18 3 次 60min 熔断浪费)
"""
import pytest
from pathlib import Path


def _read_src(rel_path: str) -> str:
    return (Path(__file__).resolve().parent.parent.parent / rel_path).read_text()


# ─────────────────────────────────────────────────────
# A.1: short_min_strength 绝对底限删除
# ─────────────────────────────────────────────────────

def test_a1_short_min_strength_removed():
    """A.1: order_executor 删除 short_min_strength<20 绝对底限.
    保留 effective_strength<65 门 (反转后强度兜底).
    """
    src = _read_src("app/pipeline/order_executor.py")

    # 不应再有 short_min_strength = 20 / 30 这样的绝对值
    assert "short_min_strength = 20" not in src, "A.1: 不应再有 short_min_strength=20"
    assert "short_min_strength = 30" not in src, "A.1: 不应再有 short_min_strength=30"

    # SHORT_STRENGTH_WEAK 这个 reason category 应该不再被 record (只剩 SHORT_EFFECTIVE_WEAK)
    # 注意: 字符串可能还在某个老 .bak 文件, 但主代码不应有
    weak_lines = [l for l in src.split('\n') if 'SHORT_STRENGTH_WEAK' in l and not l.strip().startswith('#')]
    assert len(weak_lines) == 0, f"A.1: 不应再有 SHORT_STRENGTH_WEAK 拒绝点 (5/12-18 错拒 124 笔), 实际: {weak_lines}"

    # effective 门必须保留
    assert "short_effective < 65" in src, "A.1: effective_strength<65 门必须保留"
    assert "SHORT_EFFECTIVE_WEAK" in src, "A.1: SHORT_EFFECTIVE_WEAK reason category 必须保留"


# ─────────────────────────────────────────────────────
# B.1: scalping 提前止损 6min 起
# ─────────────────────────────────────────────────────

def test_b1_breakeven_after_6min():
    """B.1: scalping/exits.py 的 breakeven_after_minutes 应为 6 (从 2 改).

    5/18 LCID/TME 持仓 2-3min 提前止损全是 0% gross + 摩擦 → -0.09% net.
    改 6min 给市场反应时间.
    """
    src = _read_src("app/scalping/exits.py")
    assert "breakeven_after_minutes: int = 6" in src, (
        "B.1: TimeExit 默认 breakeven_after_minutes 应为 6 (从 2 改)"
    )


# ─────────────────────────────────────────────────────
# B.2: scalping 排除 < $20 低价股
# ─────────────────────────────────────────────────────

def test_b2_low_price_blacklist():
    """B.2: scalping/_execute_trade 拒绝 price < $20 入场.
    LCID($5.75) 摩擦 0.09%, TP 距离 0.4% — 利润空间不够.
    """
    src = _read_src("app/scalping/engine.py")

    assert "price < 20.0" in src, "B.2: scalping 必须有 price<20 拒绝点"
    assert "price_too_low" in src, "B.2: 拒绝原因应是 price_too_low"
    assert "Scalping·低价股门" in src, "B.2: 拒绝日志必须可观测"


# ─────────────────────────────────────────────────────
# C.1: regime=TREND_DOWN 禁 LONG
# ─────────────────────────────────────────────────────

def test_c1_long_regime_gate():
    """C.1: scalping/_execute_trade 在 LONG + regime=TREND_DOWN 时拒绝.
    5/12-18 LONG 73 笔 2.7% 胜率 = 系统在熊市被强迫做多.
    """
    src = _read_src("app/scalping/engine.py")

    assert "TREND_DOWN" in src, "C.1: 必须有 TREND_DOWN regime 检查"
    assert "long_in_downtrend" in src, "C.1: 拒绝原因应是 long_in_downtrend"
    # 检查支持多种命名
    assert "DOWNTREND" in src or "BEAR" in src, "C.1: 应识别多种 regime 名 (TREND_DOWN/DOWNTREND/BEAR)"


# ─────────────────────────────────────────────────────
# C.2: 熔断 -0.0% 不计 sl_hit
# ─────────────────────────────────────────────────────

def test_c2_circuit_breaker_noise_filter():
    """C.2: ScalpingCircuitBreaker.record_loss 接受 gross_pnl_pct, 噪声不计.
    5/18 3 次熔断 = 9 笔 -0.0% 假亏 → 浪费 180min 交易窗口.
    """
    src = _read_src("app/scalping/circuit_breaker.py")

    # 签名变更
    assert "def record_loss(self, gross_pnl_pct: Optional[float] = None)" in src, (
        "C.2: record_loss 必须接受可选 gross_pnl_pct 参数"
    )
    # 噪声阈值
    assert "NOISE_LOSS_THRESHOLD" in src, "C.2: 必须定义 NOISE_LOSS_THRESHOLD 常量"
    assert "-0.001" in src, "C.2: 噪声阈值应为 -0.1% (-0.001)"


def test_c2_caller_passes_gross_pnl():
    """C.2: scalping/engine.py _close_trade 必须传 gross_pnl_pct 到 record_loss."""
    src = _read_src("app/scalping/engine.py")

    assert "record_loss(gross_pnl_pct=pnl_pct)" in src, (
        "C.2: engine 调 record_loss 必须传 gross_pnl_pct=pnl_pct"
    )


# ─────────────────────────────────────────────────────
# 集成: CircuitBreaker 单元行为验证 (运行时)
# ─────────────────────────────────────────────────────

def _load_circuit_breaker_directly():
    """绕过 app.scalping.__init__.py (依赖 pytz, 本地环境缺) 直接加载 circuit_breaker 模块.

    生产服务器装了 pytz, 这里仅是测试环境兼容.
    """
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "_cb_isolated",
        Path(__file__).resolve().parent.parent.parent / "app" / "scalping" / "circuit_breaker.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ScalpingCircuitBreaker


def test_c2_runtime_noise_does_not_increment():
    """C.2 运行时: gross_pnl=-0.05% (噪声) 调用 record_loss 不递增 consecutive_losses."""
    ScalpingCircuitBreaker = _load_circuit_breaker_directly()
    cb = ScalpingCircuitBreaker(max_consecutive_losses=3, cooldown_minutes=60)

    # 3 次噪声 (gross=-0.05%) → 不应触发熔断
    for _ in range(3):
        cb.record_loss(gross_pnl_pct=-0.0005)  # -0.05% (噪声)

    assert cb._consecutive_losses == 0, (
        "C.2: 噪声 (-0.05%) 不应递增 consecutive_losses, 实际 "
        f"consecutive_losses={cb._consecutive_losses}"
    )
    assert cb._state == cb.STATE_NORMAL, "C.2: 噪声不应触发熔断"


def test_c2_runtime_real_loss_still_triggers():
    """C.2 运行时: gross_pnl=-0.5% (真亏) 调用 3 次仍应触发熔断."""
    ScalpingCircuitBreaker = _load_circuit_breaker_directly()
    cb = ScalpingCircuitBreaker(max_consecutive_losses=3, cooldown_minutes=60)

    for _ in range(3):
        cb.record_loss(gross_pnl_pct=-0.005)  # -0.5% (真亏)

    assert cb._consecutive_losses >= 3, "C.2: 真亏必须递增 consecutive_losses"
    assert cb._state == cb.STATE_COOLDOWN, "C.2: 真亏 3 连必须触发熔断"


def test_c2_runtime_no_arg_backward_compat():
    """C.2 运行时: 不传 gross_pnl_pct (旧调用) 仍按原逻辑 (向后兼容)."""
    ScalpingCircuitBreaker = _load_circuit_breaker_directly()
    cb = ScalpingCircuitBreaker(max_consecutive_losses=3, cooldown_minutes=60)

    for _ in range(3):
        cb.record_loss()  # 不传参数, 旧行为

    assert cb._consecutive_losses >= 3, "C.2: 旧调用方不传参数必须保持原行为"
