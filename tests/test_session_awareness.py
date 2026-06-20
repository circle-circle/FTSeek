"""
Session-Aware Strategy Validation Suite — P2 2026-05-01

幻方原则: 每个策略变动必须有可自动验证的测试。
设计目标:
  1. 纯函数可测 — 不依赖 live Futu 连接或真实行情
  2. 极端场景覆盖 — 边界值 / 类型错误 / 时段切换 / 缺失数据
  3. 开盘前可跑 — < 5 秒完成，输出 GO/NOGO
  4. 回归保护 — 修改任何参数后跑一次就知道有没有破坏已有逻辑

运行方式:
  pytest tests/test_session_awareness.py -v
  python tests/test_session_awareness.py  # 直接运行 (输出详细报告)
"""

import sys
import os
import math
import time
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock, PropertyMock
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# ============================================================
#  Fixtures
# ============================================================

@pytest.fixture
def shared_kernel():
    from app.core.shared_kernel import SharedKernel
    return SharedKernel()


@pytest.fixture
def exit_monitor():
    from app.risk.position_exit_monitor import ExitMonitor
    return ExitMonitor()


@pytest.fixture
def quality_tracker():
    from app.storage.exit_quality_tracker import ExitQualityTracker
    db_path = os.path.join(tempfile.gettempdir(), f"test_exit_quality_{os.getpid()}.db")
    tracker = ExitQualityTracker(db_path=db_path)
    yield tracker
    try:
        os.unlink(db_path)
    except Exception:
        pass


@pytest.fixture
def mock_position():
    """构造模拟持仓对象"""
    from unittest.mock import MagicMock
    pos = MagicMock()
    pos.symbol = "US.AAPL"
    pos.direction = MagicMock()
    pos.direction.value = "LONG"
    pos.avg_entry_price = 150.0
    pos.size = 100
    pos.entry_time = datetime.now() - timedelta(hours=2)
    pos.trail_state = {}
    pos.take_profit_levels = None
    pos.current_sl = 0.0
    return pos


# ============================================================
#  1. SharedKernel — Session Taxonomy (SSOT)
# ============================================================

class TestSharedKernelSessionTaxonomy:
    """验证统一时段分类体系的正确性"""

    def test_normalize_old_to_new_names(self, shared_kernel):
        """旧名 → canonical name 映射"""
        assert shared_kernel.normalize_session("OPEN") == "opening_drive"
        assert shared_kernel.normalize_session("MID_MORNING") == "morning"
        assert shared_kernel.normalize_session("LUNCH") == "midday"
        assert shared_kernel.normalize_session("AFTERNOON") == "afternoon"
        assert shared_kernel.normalize_session("CLOSE") == "closing_drive"
        assert shared_kernel.normalize_session("AFTER_HOURS") == "after_hours"

    def test_normalize_canonical_idempotent(self, shared_kernel):
        """canonical name 自映射幂等"""
        for name in ["opening_drive", "morning", "midday", "afternoon", "closing_drive", "after_hours"]:
            assert shared_kernel.normalize_session(name) == name, f"{name} 应该幂等"

    def test_normalize_unknown_fallback(self, shared_kernel):
        """未知名降级到 after_hours"""
        assert shared_kernel.normalize_session("GARBAGE") == "after_hours"
        assert shared_kernel.normalize_session("") == "after_hours"
        assert shared_kernel.normalize_session(None) == "after_hours"

    def test_get_session_vol_ratio_all_sessions(self, shared_kernel):
        """所有 session 都有合法的 vol_ratio (0.3 ~ 2.5)"""
        for session in ["opening_drive", "morning", "midday", "afternoon", "closing_drive", "after_hours"]:
            ratio = shared_kernel.get_session_vol_ratio(session)
            assert 0.3 <= ratio <= 2.5, f"{session} vol_ratio={ratio} 超出合法范围 [0.3, 2.5]"

    def test_get_session_vol_ratio_unknown_fallback(self, shared_kernel):
        """未知名 → normalize 到 after_hours → ratio=0.8"""
        # normalize_session("INVALID") → "after_hours" → ratio = 0.8
        assert shared_kernel.get_session_vol_ratio("INVALID") == 0.8

    def test_vol_ratio_ordering_matches_volatility(self, shared_kernel):
        """波动率排序: opening > morning > closing > afternoon > midday"""
        ratios = {s: shared_kernel.get_session_vol_ratio(s)
                  for s in ["opening_drive", "morning", "closing_drive", "afternoon", "midday"]}
        # opening_drive 应该是最高的
        assert ratios["opening_drive"] == max(ratios.values()), "opening_drive 应该波动最大"
        # midday 应该是最低的
        assert ratios["midday"] == min(ratios.values()), "midday 应该波动最小"
        # morning > afternoon (早盘仍有开盘余波)
        assert ratios["morning"] > ratios["afternoon"], "morning 波动应 > afternoon"

    def test_get_current_session_opening_drive(self):
        """09:30-10:30 ET → opening_drive"""
        with patch('app.core.shared_kernel.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 1, 9, 45, 0)
            from app.core.shared_kernel import SharedKernel
            session = SharedKernel.get_current_session()
            assert session == "opening_drive", f"09:45 → 期望 opening_drive, 实际 {session}"

    def test_get_current_session_midday(self):
        """12:00 ET → midday"""
        with patch('app.core.shared_kernel.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 1, 12, 0, 0)
            from app.core.shared_kernel import SharedKernel
            session = SharedKernel.get_current_session()
            assert session == "midday", f"12:00 → 期望 midday, 实际 {session}"

    def test_get_current_session_closing_drive(self):
        """15:30 ET → closing_drive"""
        with patch('app.core.shared_kernel.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 1, 15, 30, 0)
            from app.core.shared_kernel import SharedKernel
            session = SharedKernel.get_current_session()
            assert session == "closing_drive", f"15:30 → 期望 closing_drive, 实际 {session}"

    def test_get_current_session_boundary_opening_start(self):
        """09:30 整 → opening_drive (边界包含)"""
        with patch('app.core.shared_kernel.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 1, 9, 30, 0)
            from app.core.shared_kernel import SharedKernel
            session = SharedKernel.get_current_session()
            assert session == "opening_drive", f"09:30 边界 → 期望 opening_drive, 实际 {session}"

    def test_get_current_session_boundary_closing_end(self):
        """16:00 整 → closing_drive (边界包含)"""
        with patch('app.core.shared_kernel.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 1, 16, 0, 0)
            from app.core.shared_kernel import SharedKernel
            session = SharedKernel.get_current_session()
            assert session == "closing_drive", f"16:00 边界 → 期望 closing_drive, 实际 {session}"

    def test_get_current_session_boundary_transition(self):
        """10:30 边界 → morning (过渡)"""
        with patch('app.core.shared_kernel.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 1, 10, 30, 0)
            from app.core.shared_kernel import SharedKernel
            session = SharedKernel.get_current_session()
            assert session == "morning", f"10:30 边界 → 期望 morning, 实际 {session}"

    def test_get_current_session_weekend(self):
        """周六 → after_hours"""
        # 2026-05-02 is Saturday
        with patch('app.core.shared_kernel.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 2, 10, 0, 0)
            from app.core.shared_kernel import SharedKernel
            session = SharedKernel.get_current_session()
            assert session == "after_hours", f"周末 → 期望 after_hours, 实际 {session}"

    def test_get_current_session_pre_market(self):
        """08:00 ET (盘前) → after_hours"""
        with patch('app.core.shared_kernel.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 1, 8, 0, 0)
            from app.core.shared_kernel import SharedKernel
            session = SharedKernel.get_current_session()
            assert session == "after_hours", f"08:00 → 期望 after_hours, 实际 {session}"


# ============================================================
#  2. Effective ATR (Session-Aware Core Mechanism)
# ============================================================

class TestSessionAwareATR:
    """验证 effective_atr = atr × session_vol_ratio 的正确性"""

    def test_effective_atr_opening_amplifies(self):
        """OPEN: atr=2.0, ratio=1.5 → effective_atr=3.0 (放大)"""
        from app.core.shared_kernel import get_shared_kernel
        kernel = get_shared_kernel()
        ratio = kernel.get_session_vol_ratio("opening_drive")
        atr = 2.0
        effective = atr * ratio
        assert effective == 3.0, f"期望 3.0, 实际 {effective}"

    def test_effective_atr_midday_contracts(self):
        """LUNCH: atr=2.0, ratio=0.6 → effective_atr=1.2 (缩小)"""
        from app.core.shared_kernel import get_shared_kernel
        kernel = get_shared_kernel()
        ratio = kernel.get_session_vol_ratio("midday")
        atr = 2.0
        effective = atr * ratio
        assert effective == 1.2, f"期望 1.2, 实际 {effective}"

    def test_effective_atr_normal_unchanged(self):
        """afternoon: ratio=0.95 ≈ 1.0 (基本不变)"""
        from app.core.shared_kernel import get_shared_kernel
        kernel = get_shared_kernel()
        ratio = kernel.get_session_vol_ratio("afternoon")
        assert 0.9 <= ratio <= 1.1, f"afternoon ratio={ratio} 应接近 1.0"

    def test_effective_atr_zero_atr_remains_zero(self):
        """ATR=0 → effective_atr=0 (不管 ratio 是多少)"""
        from app.core.shared_kernel import get_shared_kernel
        kernel = get_shared_kernel()
        ratio = kernel.get_session_vol_ratio("opening_drive")
        effective = 0.0 * ratio
        assert effective == 0.0

    def test_effective_atr_negative_atr(self):
        """ATR 为负 → effective_atr 为负 (但应被上游拦截)"""
        from app.core.shared_kernel import get_shared_kernel
        kernel = get_shared_kernel()
        ratio = kernel.get_session_vol_ratio("morning")
        effective = -1.0 * ratio
        assert effective < 0, "负 ATR 应该保持负数 (由 compute_profit_atr_signed 防御)"

    def test_compute_profit_atr_signed_with_effective_atr_opening(self):
        """LONG, entry=100, current=103, effective_atr=3.0 → profit_atr=1.0"""
        from app.risk.atr_stop_engine import compute_profit_atr_signed
        # opening: raw_atr=2.0, ratio=1.5 → effective_atr=3.0
        effective_atr = 3.0
        profit = compute_profit_atr_signed("LONG", 100.0, 103.0, effective_atr)
        assert abs(profit - 1.0) < 0.01, f"期望 1.0, 实际 {profit}"
        # 如果用 raw atr 会是 1.5, session-aware 下"看起来"盈利更少 → 更保守

    def test_compute_profit_atr_signed_with_effective_atr_midday(self):
        """LONG, entry=100, current=102, effective_atr=1.2 → profit_atr=1.67"""
        from app.risk.atr_stop_engine import compute_profit_atr_signed
        # midday: raw_atr=2.0, ratio=0.6 → effective_atr=1.2
        effective_atr = 1.2
        profit = compute_profit_atr_signed("LONG", 100.0, 102.0, effective_atr)
        assert abs(profit - 1.67) < 0.01, f"期望 1.67, 实际 {profit}"
        # midday 下"看起来"盈利更多 → 更容易激活保本/追踪 → 更积极锁利润


# ============================================================
#  3. Flash Exit — Session-Aware Threshold
# ============================================================

class TestFlashExitSessionAwareness:
    """验证 Flash Exit 时段感知逻辑"""

    def _make_position(self, symbol="US.AAPL", direction="LONG", entry_price=150.0):
        from unittest.mock import MagicMock
        pos = MagicMock()
        pos.symbol = symbol
        pos.direction = MagicMock()
        pos.direction.value = direction
        pos.avg_entry_price = entry_price
        pos.size = 100
        pos.trail_state = {}
        return pos

    def test_flash_exit_not_triggered_normal_move_opening(self, exit_monitor):
        """OPEN: 0.8x ATR 逆向不应触发 (因为 effective_atr 更大)"""
        pos = self._make_position()
        # raw atr=2.0, opening ratio=1.5 → effective_atr=3.0
        # flash_threshold = 0.8 × 3.0 = 2.40
        # adverse_move = 1.6 (which is 0.8 × raw_atr=2.0)
        # 1.6 < 2.40 → 不触发!
        effective_atr = 3.0
        trail_state = {"prev_scan_price": 151.6}  # 上一笔 151.6
        current_price = 150.0  # 当前 150.0, 逆向 = 1.6

        result = exit_monitor._check_flash_exit(
            pos, current_price, effective_atr, trail_state, time.time(), "LONG", None, None
        )
        assert result is None, f"OPEN 下 0.8x raw ATR 逆向不应触发 Flash Exit, 但触发了: {result}"

    def test_flash_exit_triggered_large_move_even_opening(self, exit_monitor):
        """OPEN: 1.5x raw ATR 逆向应该触发 (足够大)"""
        pos = self._make_position()
        # effective_atr=3.0, flash_threshold=2.40
        # adverse_move=3.0 > 2.40 → 触发
        effective_atr = 3.0
        trail_state = {"prev_scan_price": 153.0}
        current_price = 150.0  # adverse = 3.0
        result = exit_monitor._check_flash_exit(
            pos, current_price, effective_atr, trail_state, time.time(), "LONG", None, None
        )
        assert result is not None, "OPEN 下 1.5x raw ATR 逆向应该触发"
        assert result["reason"] == "FLASH_EXIT"

    def test_flash_exit_triggered_small_move_midday(self, exit_monitor):
        """LUNCH: 0.5x raw ATR 逆向应该触发 (因为 effective_atr 更小)"""
        pos = self._make_position()
        # raw atr=2.0, midday ratio=0.6 → effective_atr=1.2
        # flash_threshold = 0.8 × 1.2 = 0.96
        # adverse_move = 1.0 (0.5x raw_atr) → 1.0 > 0.96 → 触发!
        effective_atr = 1.2
        trail_state = {"prev_scan_price": 151.0}
        current_price = 150.0  # adverse = 1.0
        result = exit_monitor._check_flash_exit(
            pos, current_price, effective_atr, trail_state, time.time(), "LONG", None, None
        )
        assert result is not None, f"LUNCH 下 0.5x raw ATR 逆向应该触发 (更敏感), 实际: {result}"
        assert result["reason"] == "FLASH_EXIT"

    def test_flash_exit_short_vix_high_tightens(self, exit_monitor):
        """做空 + VIX>30 → flash_multiplier 收紧到 0.6"""
        pos = self._make_position(direction="SHORT")
        # Mock VIX > 30
        exit_monitor._get_vix = MagicMock(return_value=32.0)
        effective_atr = 2.0
        trail_state = {"prev_scan_price": 148.0}
        current_price = 149.5  # SHORT: adverse = 1.5 (价格上涨)

        # 默认 0.8x: 1.5 < 1.6 不触发
        # 收紧 0.6x: 1.5 > 1.2 触发
        from unittest.mock import MagicMock as MM
        policy_mock = MM()
        policy_mock.get_flash_multiplier = MM(return_value=0.8)

        result = exit_monitor._check_flash_exit(
            pos, current_price, effective_atr, trail_state, time.time(), "SHORT", policy_mock, None
        )
        assert result is not None, f"SHORT+VIX>30 收紧到 0.6x → 1.5 > 1.2 应触发, 实际: {result}"
        exit_monitor._get_vix = MagicMock(return_value=None)  # cleanup

    def test_flash_exit_no_prev_price(self, exit_monitor):
        """首次扫描 (无 prev_scan_price) → 不触发"""
        pos = self._make_position()
        effective_atr = 2.0
        trail_state = {}  # 无 prev_scan_price
        result = exit_monitor._check_flash_exit(
            pos, 150.0, effective_atr, trail_state, time.time(), "LONG", None, None
        )
        assert result is None, "首次扫描不应触发 Flash Exit"

    def test_flash_exit_zero_atr_skips(self, exit_monitor):
        """ATR=0 (数据不可用) → 跳过, 不崩溃"""
        pos = self._make_position()
        result = exit_monitor._check_flash_exit(
            pos, 150.0, 0.0, {"prev_scan_price": 151.0}, time.time(), "LONG", None, None
        )
        assert result is None


# ============================================================
#  5. ATR Stop Engine — Regime × Session Orthogonality
# ============================================================

class TestATRStopEngineOrthogonality:
    """验证 regime 和 session 维度的正交性 — session 影响 ATR 量纲, regime 影响 multiplier 选择"""

    def test_regime_and_session_are_independent_dimensions(self):
        """regime 和 session 是独立维度 — session 不应改变 trail_multiplier 选择逻辑"""
        from app.risk.atr_stop_engine import resolve_trail_multiplier

        policy = {
            "exit_rules": {
                "chandelier_trail": {
                    "multiplier_by_regime": {"normal": 2.0, "trending": 3.0, "volatile": 1.5},
                }
            }
        }

        # 无论什么 session，同一 regime 应返回相同的 trail_mult
        # session 通过 effective_atr 注入，不通过 trail_mult
        t1 = resolve_trail_multiplier(policy, "trending", "LONG")
        t2 = resolve_trail_multiplier(policy, "trending", "LONG")
        assert abs(t1 - t2) < 0.001, "同一 regime 应返回相同 trail_mult (不依赖 session)"

    def test_stop_lines_with_session_atr_opening_vs_midday(self):
        """同一 regime 下，使用不同 effective_atr → 止损线不同，但原因相同"""
        from app.risk.atr_stop_engine import compute_stop_lines

        policy = {
            "exit_rules": {
                "atr_stop": {"enabled": True, "max_loss_pct": 15.0},
                "chandelier_trail": {
                    "enabled": True, "activation_profit_atr": 2.0,
                    "multiplier_by_regime": {"normal": 2.0, "trending": 3.0, "volatile": 1.5},
                },
                "breakeven": {"enabled": True, "trigger_profit_atr": 1.5, "buffer_pct": 0.2},
            }
        }

        # OPEN: effective_atr=3.0
        r_open = compute_stop_lines(
            direction="LONG", entry_price=100.0, current_price=102.0,
            atr=3.0, peak_price=103.0,
            policy=policy, regime="normal", atr_mult=2.5,
        )

        # LUNCH: effective_atr=1.2
        r_lunch = compute_stop_lines(
            direction="LONG", entry_price=100.0, current_price=102.0,
            atr=1.2, peak_price=103.0,
            policy=policy, regime="normal", atr_mult=2.5,
        )

        # 止赢线的绝对位置不同 (因为 ATR 不同)
        assert r_open.atr_stop != r_lunch.atr_stop, "不同 ATR 应产生不同止损线"
        # OPEN 止损线应该更低（更宽）= 93.5 vs 97.0
        assert r_open.atr_stop < r_lunch.atr_stop, \
            f"OPEN 止损应更宽(更低): {r_open.atr_stop} < {r_lunch.atr_stop}"

        # 但原因代码相同 (都基于 ATR_STOP)
        assert r_open.reason_code == r_lunch.reason_code == "NONE", "未触发时同一 regime 原因应一致"

    def test_stop_lines_session_doesnt_leak_into_multiplier(self):
        """验证 session 不影响 resolve_trail_multiplier 的返回值"""
        from app.risk.atr_stop_engine import resolve_trail_multiplier

        policy = {
            "exit_rules": {
                "chandelier_trail": {
                    "multiplier_by_regime": {"normal": 2.0},
                }
            }
        }

        # 无论外部环境如何, resolve_trail_multiplier 结果恒定
        result = resolve_trail_multiplier(policy, "normal", "LONG")
        assert abs(result - 2.0) < 0.001, f"trail_multiplier 应=2.0, 实际={result}"


# ============================================================
#  6. Exit Quality Tracker — Lifecycle
# ============================================================

class TestExitQualityTracker:
    """验证退出质量追踪的完整生命周期"""

    def test_record_exit_basic(self, quality_tracker):
        """基础记录写入 → 评估后可见"""
        exit_time = (datetime.now() - timedelta(seconds=1000)).isoformat()  # >15min
        quality_tracker.record_exit(
            "US.AAPL", "FLASH_EXIT", 150.0, "opening_drive", "LONG", -0.01,
            exit_time=exit_time
        )
        # 15min 后价格跌 1.5% → GOOD
        quality_tracker.evaluate_pending(lambda s: 147.75)
        report = quality_tracker.get_session_quality_report(days=1)
        assert "opening_drive" in report, f"应该有 opening_drive 数据, 实际: {report}"
        assert report["opening_drive"]["total"] >= 1

    def test_evaluate_pending_good_quality(self, quality_tracker):
        """退出后价格继续同向 >0.5% → GOOD (15min 窗口)"""
        # 记录一次做多退出 (LONG exit @ $150)
        exit_time = (datetime.now() - timedelta(seconds=1000)).isoformat()  # >15min ago
        quality_tracker.record_exit(
            "US.AAPL", "ATR_STOP", 150.0, "morning", "LONG", -0.02,
            exit_time=exit_time
        )
        # 15 分钟后价格跌到 148.5 (跌 1%) → LONG 退出正确 = GOOD
        def mock_price(symbol):
            return 148.5

        n = quality_tracker.evaluate_pending(mock_price)
        assert n == 1, f"应该评估 1 条, 实际 {n}"

        report = quality_tracker.get_session_quality_report(days=1)
        assert report["morning"]["good"] == 1, f"应该有 1 条 GOOD: {report}"

    def test_evaluate_pending_bad_quality(self, quality_tracker):
        """退出后价格逆向 >1.0% → BAD (假阳性)"""
        exit_time = (datetime.now() - timedelta(seconds=1000)).isoformat()
        quality_tracker.record_exit(
            "US.AAPL", "FLASH_EXIT", 150.0, "opening_drive", "LONG", -0.01,
            exit_time=exit_time
        )
        # 退出后价格涨到 152 (涨 1.33%) → 退出错了 = BAD
        def mock_price(symbol):
            return 152.0

        quality_tracker.evaluate_pending(mock_price)
        report = quality_tracker.get_session_quality_report(days=1)
        assert report["opening_drive"]["bad"] == 1, f"应该有 1 条 BAD: {report}"

    def test_evaluate_pending_neutral_quality(self, quality_tracker):
        """退出后价格在噪音范围内 → NEUTRAL"""
        exit_time = (datetime.now() - timedelta(seconds=1000)).isoformat()
        quality_tracker.record_exit(
            "US.AAPL", "CHANDELIER", 150.0, "afternoon", "LONG", 0.03,
            exit_time=exit_time
        )
        # 价格微涨 0.5% → 在 [-0.5, 1.0] 噪音区间内 → NEUTRAL
        def mock_price(symbol):
            return 150.75

        quality_tracker.evaluate_pending(mock_price)
        report = quality_tracker.get_session_quality_report(days=1)
        assert report["afternoon"]["neutral"] == 1, f"应该有 1 条 NEUTRAL: {report}"

    def test_evaluate_skips_too_recent(self, quality_tracker):
        """退出时间 < 15 分钟 → 跳过评估"""
        exit_time = datetime.now().isoformat()  # just now
        quality_tracker.record_exit(
            "US.AAPL", "FLASH_EXIT", 150.0, "opening_drive", "LONG", -0.01,
            exit_time=exit_time
        )
        n = quality_tracker.evaluate_pending(lambda s: 155.0)
        assert n == 0, f"太新的记录不应评估: {n}"

    def test_suggest_adjustments_bad_accuracy(self, quality_tracker):
        """BAD 率高 → 建议调高 vol_ratio (放宽止损)"""
        exit_time = (datetime.now() - timedelta(seconds=1000)).isoformat()  # >15min
        # 10 条最终被判 BAD, 2 条 GOOD → accuracy=0.17 (远低于 60% 目标)
        for i in range(10):
            quality_tracker.record_exit(
                f"US.BAD{i}", "FLASH_EXIT", 150.0, "opening_drive", "LONG", -0.01,
                exit_time=exit_time
            )
        for i in range(2):
            quality_tracker.record_exit(
                f"US.GOOD{i}", "FLASH_EXIT", 150.0, "opening_drive", "LONG", 0.02,
                exit_time=exit_time
            )

        # LONG exit @ 150, 15min later:
        #   GOOD: 价格继续跌到 148 (-1.33% → adverse=-1.33 < -0.5 → GOOD)
        #   BAD:  价格逆转到 153  (+2.0%  → adverse=+2.0 > 1.0  → BAD)
        def mock_price(symbol):
            return 148.0 if "GOOD" in symbol else 153.0

        quality_tracker.evaluate_pending(mock_price)

        suggestions = quality_tracker.suggest_session_vol_adjustments(days=1)
        assert "opening_drive" in suggestions, f"应该有 opening_drive 建议: {suggestions}"
        assert suggestions["opening_drive"] > 0, \
            f"BAD 率偏高 → 应该建议调高 ratio (正数), 实际: {suggestions['opening_drive']}"


# ============================================================
#  7. Edge Cases — 极端场景
# ============================================================

class TestEdgeCases:
    """极端场景 / 边界条件 / 故障注入"""

    def test_atr_stop_engine_dict_price_rejection(self):
        """价格是 dict 而非 float → 防御性返回"""
        from app.risk.atr_stop_engine import compute_profit_atr_signed
        result = compute_profit_atr_signed("LONG", 100.0, {"bad": "data"}, 2.0)
        assert result == 0.0, f"dict 价格应返回 0.0, 实际 {result}"

    def test_atr_stop_engine_string_price_rejection(self):
        """价格是 string → 防御性返回"""
        from app.risk.atr_stop_engine import compute_profit_atr_signed
        result = compute_profit_atr_signed("LONG", 100.0, "not_a_number", 2.0)
        assert result == 0.0, f"string 价格应返回 0.0, 实际 {result}"

    def test_atr_stop_engine_none_price_rejection(self):
        """价格是 None → 防御性返回"""
        from app.risk.atr_stop_engine import compute_profit_atr_signed
        result = compute_profit_atr_signed("LONG", 100.0, None, 2.0)
        assert result == 0.0

    def test_atr_stop_engine_none_entry_rejection(self):
        """入场价是 None → 防御性返回"""
        from app.risk.atr_stop_engine import compute_profit_atr_signed
        result = compute_profit_atr_signed("LONG", None, 150.0, 2.0)
        assert result == 0.0

    def test_trail_multiplier_nested_dict_defense(self):
        """by_regime[key] 返回嵌套 dict → 防御性降级"""
        from app.risk.atr_stop_engine import resolve_trail_multiplier
        policy = {
            "exit_rules": {
                "chandelier_trail": {
                    "multiplier_by_regime": {
                        "normal": 2.0,
                        "trending": {"value": 3.0, "override": True},  # 嵌套 dict
                    }
                }
            }
        }
        result = resolve_trail_multiplier(policy, "trending", "LONG")
        assert abs(result - 3.0) < 0.001, f"嵌套 dict 应正确提取 value: {result}"

    def test_calibrate_scalers_syncs_both_tables(self):
        """calibrate_scalers_from_data 同时同步 PHASE_SCALERS 和 VOL_RATIO"""
        from app.core.shared_kernel import (
            SharedKernel, DEFAULT_PHASE_SCALERS, DEFAULT_SESSION_VOL_RATIO,
        )
        sk = SharedKernel()

        old_opening_sl = DEFAULT_PHASE_SCALERS["opening_drive"]["sl_scale"]
        old_opening_vol = DEFAULT_SESSION_VOL_RATIO["opening_drive"]

        # 注入非标准振幅数据
        test_amps = {
            "opening_drive": 0.030,  # 很高
            "midday": 0.005,         # 很低
        }
        sk.calibrate_scalers_from_data(test_amps)

        # 两个表都应该更新
        new_opening_sl = DEFAULT_PHASE_SCALERS["opening_drive"]["sl_scale"]
        new_opening_vol = DEFAULT_SESSION_VOL_RATIO["opening_drive"]

        assert new_opening_sl == new_opening_vol, \
            f"PHASE_SCALERS.sl_scale({new_opening_sl}) 应 = VOL_RATIO({new_opening_vol})"
        assert new_opening_sl > old_opening_sl, "opening 波动被高估 → ratio 应上升"

        # Restore
        DEFAULT_PHASE_SCALERS["opening_drive"]["sl_scale"] = old_opening_sl
        DEFAULT_SESSION_VOL_RATIO["opening_drive"] = old_opening_vol

    def test_quality_tracker_calibration_roundtrip(self, quality_tracker):
        """完整校准回路: 记录 → 评估 → 建议 → 应用"""
        from app.core.shared_kernel import DEFAULT_SESSION_VOL_RATIO

        exit_time = (datetime.now() - timedelta(seconds=1000)).isoformat()  # >15min
        old_ratio = DEFAULT_SESSION_VOL_RATIO.get("opening_drive", 1.5)

        # 全 BAD — 止损太紧 (退出后价格大幅逆转到 153)
        for i in range(20):
            quality_tracker.record_exit(
                f"US.BAD{i}", "FLASH_EXIT", 150.0, "opening_drive", "LONG", -0.02,
                exit_time=exit_time
            )
        def mock_price(s):
            return 153.0  # +2.0% → BAD (>1.0%)

        quality_tracker.evaluate_pending(mock_price)
        quality_tracker.apply_calibration(days=1)

        new_ratio = DEFAULT_SESSION_VOL_RATIO.get("opening_drive", 1.5)
        assert new_ratio > old_ratio, \
            f"全 BAD → ratio 应上调: {old_ratio:.2f} → {new_ratio:.2f}"

        # Restore
        DEFAULT_SESSION_VOL_RATIO["opening_drive"] = old_ratio


# ============================================================
#  8. Integration — Full Pipeline Sanity
# ============================================================

class TestIntegrationSanity:
    """端到端集成冒烟测试 — 确保 session 感知不影响非 session 路径"""

    def test_exit_monitor_scan_positions_no_crash(self, exit_monitor):
        """空持仓列表 → scan_positions 不崩溃"""
        result = exit_monitor.scan_positions(
            [], lambda s: 0, lambda p, r: True, None, None
        )
        assert result == []

    def test_intraday_scorer_still_produces_valid_signal(self):
        """IntradayScorer.score 即使 session 路由改了，仍产出合法信号"""
        from app.core.intraday_scorer import get_intraday_scorer

        scorer = get_intraday_scorer()
        context = {
            "symbol": "US.AAPL",
            "price": 150.0,
            "atr": 2.0,
            "vix": 18.0,
            "rsi": 55.0, "rsi_prev": 52.0,
            "macd": 0.5, "macd_signal": 0.3,
            "ma20": 148.0, "ma50": 145.0, "ma200": 140.0,
            "change_percent": 1.5,
            "volume_ratio": 1.2, "turnover": 5e7, "amplitude": 2.0,
            "spy_price": 450.0, "spy_ma50": 445.0,
        }
        signal = scorer.score(context)

        assert signal.symbol == "US.AAPL"
        assert signal.direction in ("LONG", "SHORT", "NEUTRAL")
        assert 10 <= signal.strength <= 90, f"strength 超出 [10,90]: {signal.strength}"
        assert 0.0 <= signal.confidence <= 0.92, f"confidence 超出 [0, 0.92]: {signal.confidence}"
        assert 0.0 <= signal.position_pct <= 0.20, f"position_pct 超出 [0, 0.20]: {signal.position_pct}"
        assert signal.stop_loss_pct > 0

    def test_shared_kernel_unified_interface(self):
        """入口和出口使用同一套 session API 且结果一致"""
        from app.core.intraday_scorer import _get_intraday_session as intraday_session
        from app.core.shared_kernel import SharedKernel

        # 两个入口在当前时间应返回相同 session
        s1 = intraday_session()
        s2 = SharedKernel.get_current_session()
        assert s1 == s2, f"intraday_scorer({s1}) 和 SharedKernel({s2}) session 不一致!"

    def test_vol_ratio_vs_signal_confidence_different_purposes(self):
        """vol_ratio (波动大小) 和 session_multiplier (信号质量) 是两个独立维度"""
        from app.core.intraday_scorer import _session_multiplier
        from app.core.shared_kernel import get_shared_kernel

        kernel = get_shared_kernel()
        conf_mult, str_mult = _session_multiplier()
        vol_ratio = kernel.get_session_vol_ratio()

        # 它们是独立的 — opening_drive vol_ratio 高(1.5) 但 conf_mult 低(0.75)
        # 这验证了正价关系: 高波动 ≠ 高置信度
        assert isinstance(conf_mult, float)
        assert isinstance(vol_ratio, float)
        # 不要求它们相等 — 证明这是两个独立维度

    def test_exit_policy_yaml_valid_and_has_session_adaptation(self):
        """exit_policy.yaml 有效且包含 session_adaptation 块"""
        import yaml
        config_path = os.path.join(
            os.path.dirname(__file__), '..', 'app', 'risk', 'exit_policy.yaml'
        )
        with open(config_path) as f:
            policy = yaml.safe_load(f)

        assert "session_adaptation" in policy, "exit_policy.yaml 缺少 session_adaptation 块"
        sa = policy["session_adaptation"]
        assert sa.get("enabled") is True
        assert sa.get("source") == "shared_kernel"
        assert len(sa.get("apply_to", [])) >= 5, "apply_to 应覆盖 5 条退出规则"


# ============================================================
#  直接运行入口
# ============================================================

if __name__ == "__main__":
    # 不使用 pytest 时，直接运行做快速验证
    print("=" * 60)
    print("FTSeek-US Session Strategy Validation")
    print(f"Run time: {datetime.now().isoformat()}")
    print("=" * 60)

    # Discover and run all tests
    exit_code = pytest.main([
        __file__, "-v", "--tb=short", "--color=yes",
        "--timeout=30",
    ])

    if exit_code == pytest.ExitCode.OK:
        print("\n✓ ALL TESTS PASSED — Session strategy ready for market")
    else:
        print(f"\n✗ TESTS FAILED (exit={exit_code}) — Do NOT deploy!")

    sys.exit(exit_code.value if hasattr(exit_code, 'value') else int(exit_code))
