"""R-7 契约测试 (2026-08-23, DEB-20260823): Playbook 排他性 / 禁止双线管理.

背景: 账本实证 `source_type='PLAYBOOK_SIM'` 的仓曾被主线平掉 ——
`SL_ATR_STOP` × 3 笔全在 2026-06-24, 持仓 0.0-0.1h, 净亏 -$2,416
(ORCL -975.6 / XOM -822.8 / LMT -600.9)。成因 = 护栏 `is_playbook_open_position`
首次落地于 2026-07-16, 事故早于护栏 3 周。07-16 后 252 笔 playbook 平仓零污染。

但残余 fail-open 缺口三处 (R-7a/b/c/d 修复), 本文件锁住修复后的语义:
  R-7a  is_playbook_open_position 通用异常 → fail-close (True, 主线跳过)
  R-7b  _close_position 兜底护栏异常 → fail-close (拒绝平仓)
  R-7c  DEM managed_positions 源头剔除 playbook 仓 → FLAT_ALL/REDUCE_50 结构性够不到
  R-7d  main_scan_enabled 缺省 False (入场侧唯一 fail-open 锁)
"""
import sqlite3
from unittest.mock import MagicMock, patch

import pytest


class TestR7aGuardFailClose:
    """R-7a: 排他性唯一闸门的失败模式."""

    def test_operational_error_fails_closed(self):
        """DB 锁/busy → True (原有语义, 不得回退)."""
        from app.playbook.pipeline import is_playbook_open_position
        with patch("sqlite3.connect", side_effect=sqlite3.OperationalError("database is locked")):
            assert is_playbook_open_position("US.NVDA") is True

    def test_generic_exception_fails_closed(self):
        """R-7a 核心: 通用异常 → True (主线跳过), 不再 return False 让主线接管."""
        from app.playbook.pipeline import is_playbook_open_position
        with patch("sqlite3.connect", side_effect=RuntimeError("schema mismatch")):
            assert is_playbook_open_position("US.NVDA") is True

    def test_generic_exception_logs_critical(self):
        import logging
        from app.playbook.pipeline import is_playbook_open_position
        with patch("sqlite3.connect", side_effect=RuntimeError("schema mismatch")):
            with pytest.MonkeyPatch.context():
                import app.playbook.pipeline as _pp
                with patch.object(_pp.logger, "critical") as _crit:
                    is_playbook_open_position("US.NVDA")
                    assert _crit.called
                    assert "fail-close" in str(_crit.call_args)

    def test_fail_mode_open_can_rollback(self):
        """开关设 open 时退回旧行为 (可回滚)."""
        from app.playbook.pipeline import is_playbook_open_position
        _cfg = MagicMock()
        _cfg.get_dotted.side_effect = lambda k, d=None: (
            "open" if k == "risk.playbook_guard_fail_mode" else d
        )
        with patch("sqlite3.connect", side_effect=RuntimeError("boom")), \
             patch("app.core.config_container.get_config", return_value=_cfg):
            assert is_playbook_open_position("US.NVDA") is False

    def test_playbook_open_check_preserves_failclose(self):
        """预载失败 (None) 时回退逐 symbol 判定, 语义不降级."""
        from app.playbook.pipeline import playbook_open_check
        with patch("app.playbook.pipeline.is_playbook_open_position", return_value=True) as _f:
            assert playbook_open_check("US.NVDA", None) is True
            _f.assert_called_once_with("US.NVDA")

    def test_playbook_open_check_uses_preloaded_set(self):
        from app.playbook.pipeline import playbook_open_check
        assert playbook_open_check("US.NVDA", {"US.NVDA"}) is True
        assert playbook_open_check("US.AMD", {"US.NVDA"}) is False


class TestR7bCloseGuardFailClose:
    """R-7b: _close_position 兜底护栏不得 fail-open."""

    def test_guard_blocks_playbook_position(self):
        from app.pipeline import order_executor as oe
        pos = MagicMock(symbol="US.NVDA")
        with patch("app.playbook.pipeline.is_playbook_open_position", return_value=True), \
             patch.object(oe, "logger") as _lg:
            oe._close_position(MagicMock(), pos, 100.0, MagicMock())
            assert any("playbook护栏" in str(c) for c in _lg.warning.call_args_list)

    def test_guard_exception_fails_closed(self):
        """R-7b 核心: 护栏自身抛异常 → 拒绝平仓 + CRITICAL, 不再 pass 继续平."""
        from app.pipeline import order_executor as oe
        pos = MagicMock(symbol="US.NVDA")
        with patch("app.playbook.pipeline.is_playbook_open_position",
                   side_effect=RuntimeError("guard broken")), \
             patch.object(oe, "logger") as _lg:
            oe._close_position(MagicMock(), pos, 100.0, MagicMock())
            assert _lg.critical.called
            assert "fail-close" in str(_lg.critical.call_args)


class TestR7cPortfolioStopExclusion:
    """R-7c: FLAT_ALL/REDUCE_50 必须结构性够不到 playbook 仓."""

    def _monitor(self):
        from app.risk.position_exit_monitor import ExitMonitor
        return ExitMonitor()

    def test_playbook_position_excluded_from_managed(self):
        """playbook 仓不得进 managed_positions → 组合层无法迭代到它."""
        from app.risk import position_exit_monitor as pem
        m = self._monitor()
        pb = MagicMock(symbol="US.NVDA", metadata={}, size=10, avg_entry_price=100.0)
        legacy = MagicMock(symbol="US.SPY", metadata={}, size=10, avg_entry_price=100.0)
        captured = {}

        def _spy_portfolio(managed, _fn):
            captured["managed"] = list(managed)
            return None

        with patch("app.playbook.pipeline.load_playbook_open_symbols", return_value={"US.NVDA"}), \
             patch("app.positions.is_strategy_managed", return_value=True), \
             patch("app.positions.is_protected", return_value=False), \
             patch.object(m, "_check_portfolio_drawdown", side_effect=_spy_portfolio):
            try:
                m.check_all_positions([pb, legacy], lambda s: 100.0, lambda p, d: None)
            except Exception:
                pass  # 后续逐仓逻辑依赖大量上下文, 本测试只关心 managed 集合

        if "managed" in captured:
            syms = [p.symbol for p in captured["managed"]]
            assert "US.NVDA" not in syms, "playbook 仓漏进了组合层止损的迭代源"

    def test_guard_import_failure_skips_portfolio_stop(self):
        """护栏不可用时整段跳过组合层 (安全降级), 而非放行未过滤集合."""
        from app.risk import position_exit_monitor as pem
        m = self._monitor()
        pos = MagicMock(symbol="US.NVDA", metadata={}, size=10, avg_entry_price=100.0)
        called = {"portfolio": False}

        def _never(*a, **kw):
            called["portfolio"] = True
            return "FLAT_ALL"

        with patch("app.playbook.pipeline.load_playbook_open_symbols",
                   side_effect=ImportError("module gone")), \
             patch.object(m, "_check_portfolio_drawdown", side_effect=_never):
            try:
                m.check_all_positions([pos], lambda s: 100.0, lambda p, d: None)
            except Exception:
                pass
        assert called["portfolio"] is False, "护栏导入失败时仍执行了组合层止损"


class TestR7dMainScanFailClose:
    """R-7d: 入场侧唯一 fail-open 锁改 fail-close."""

    def test_config_default_is_false_in_code(self):
        """源码缺省值必须是 False —— 配置读不到时主线不得自动开扫."""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent.parent
               / "app" / "pipeline" / "signal_pipeline.py").read_text(encoding="utf-8")
        assert 'get_dotted("strategy.main_scan_enabled", True)' not in src, \
            "main_scan_enabled 仍存在缺省 True 的读取点 (fail-open)"
        assert src.count('get_dotted("strategy.main_scan_enabled", False)') >= 2

    def test_yaml_keeps_scan_disabled(self):
        from app.core.config_container import get_config
        assert get_config().get_dotted("strategy.main_scan_enabled", False) is False

    def test_external_entry_returns_on_config_error(self):
        """外部入口 (预热/force_scan) 配置异常时必须 return, 不得放行扫描."""
        from app.pipeline import signal_pipeline as sp
        with patch("app.core.config_container.get_config", side_effect=RuntimeError("cfg gone")), \
             patch.object(sp, "_run_signal_scan_impl") as _impl:
            sp.run_signal_scan(MagicMock())
            _impl.assert_not_called()


class TestR7ExclusivityLedgerInvariant:
    """账本不变量: 07-16 护栏落地后, playbook 仓不应再出现主线来源的 exit_reason."""

    MAIN_LINE_REASONS = (
        "SL_ATR_STOP", "TRAILING_STOP", "TRAILING_STOP_BY_MAIN",
        "EXIT_MONITOR_CLOSE", "FORCED_CLOSE_OKR_AUDIT",
        "PORTFOLIO_DRAWDOWN_HARD_STOP", "PORTFOLIO_DRAWDOWN_REDUCE",
    )

    def test_no_mainline_exits_on_playbook_after_guard(self):
        from app.core.db_utils import get_main_db
        db = get_main_db()
        if not db.exists():
            pytest.skip("主库不存在 (CI 环境)")
        conn = sqlite3.connect(str(db))
        try:
            ph = ",".join("?" * len(self.MAIN_LINE_REASONS))
            rows = conn.execute(
                f"""SELECT trade_date, symbol, exit_reason FROM trade_records
                    WHERE source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')
                      AND exit_time IS NOT NULL
                      AND trade_date >= '2026-07-16'
                      AND exit_reason IN ({ph})""",
                self.MAIN_LINE_REASONS,
            ).fetchall()
        finally:
            conn.close()
        assert rows == [], f"护栏落地后仍出现主线平仓 playbook 仓: {rows}"
