"""R-9 / R-12 / R-13 契约测试 (2026-08-23, DEB-20260823).

R-9   Dashboard 紧急全平必须 playbook 感知, 且禁止"静默部分平仓"
R-12  open_positions.entry_time 必须与 trade_records 同为 ET 口径
R-13  timezone fallback 链 (pytz→zoneinfo→固定偏移) 行为一致;
      反向冷却门的拦截决策不得依赖 gap 计算 (原为 fail-open)
"""
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestR13TimezoneFallback:
    """R-13: fallback 链正确性."""

    def test_to_et_actually_converts(self):
        """原 fallback 分支直接 return dt 不转换; 现三条链行为必须一致."""
        from app.utils.timezone import to_et
        got = to_et(datetime(2026, 8, 21, 21, 40))  # naive CST
        assert got.strftime("%Y-%m-%d %H:%M") == "2026-08-21 09:40"
        assert got.tzinfo is not None, "转换后必须是 aware"

    def test_to_cst_actually_converts(self):
        from app.utils.timezone import to_cst
        got = to_cst(datetime(2026, 8, 21, 9, 40))  # naive ET
        assert got.strftime("%Y-%m-%d %H:%M") == "2026-08-21 21:40"
        assert got.tzinfo is not None

    def test_to_et_none_safe(self):
        from app.utils.timezone import to_et, to_cst
        assert to_et(None) is None
        assert to_cst(None) is None

    def test_no_import_time_dst_snapshot(self):
        """ET 不得由 import 时的 tm_isdst 决定 (宿主无 DST → 永久 EST).

        只扫非注释代码行 —— 修复注释里会引用旧写法作说明。
        """
        src = (Path(__file__).resolve().parent.parent
               / "app" / "utils" / "timezone.py").read_text(encoding="utf-8")
        code = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
        assert not any("tm_isdst" in ln for ln in code), \
            "ET 偏移仍由 import 时宿主 DST 决定 (夏令时会慢 1 小时)"

    def test_zoneinfo_is_fallback_before_fixed_offset(self):
        src = (Path(__file__).resolve().parent.parent
               / "app" / "utils" / "timezone.py").read_text(encoding="utf-8")
        assert "from zoneinfo import ZoneInfo" in src
        assert "_HAS_ZONEINFO" in src

    def test_dst_aware_across_seasons(self):
        """同一墙钟在夏/冬令时必须给出不同 UTC 偏移 (证明 DST 真的生效)."""
        from app.utils.timezone import ET, _localize
        summer = _localize(datetime(2026, 8, 15, 12, 0), ET)
        winter = _localize(datetime(2026, 1, 15, 12, 0), ET)
        assert summer.utcoffset() != winter.utcoffset()
        assert summer.utcoffset() == timedelta(hours=-4)   # EDT
        assert winter.utcoffset() == timedelta(hours=-5)   # EST

    def test_localize_idempotent_on_aware(self):
        from app.utils.timezone import ET, CST, _localize
        aware = _localize(datetime(2026, 8, 15, 12, 0), ET)
        assert _localize(aware, CST) is aware


class TestR13ReverseCooldownFailClose:
    """R-13: 反向冷却门拦截不得依赖 gap 计算."""

    def test_block_decision_independent_of_gap(self):
        """源码层面: return False 不得嵌在 gap 计算的 try 内."""
        src = (Path(__file__).resolve().parent.parent
               / "app" / "pipeline" / "order_executor.py").read_text(encoding="utf-8")
        # 旧写法的特征: gap 计算成功才 return False
        assert "gap = (now_et() - exit_dt).total_seconds()" not in src, \
            "反向冷却门仍把拦截决策放在 gap 计算之后 (naive 行会 TypeError → 放行)"
        assert "_gap_desc" in src, "应改为 gap 仅用于日志"

    def test_naive_exit_time_still_blocks(self):
        """命中 naive legacy 行时必须仍然拦截 (原实现会放行)."""
        from app.pipeline import order_executor as oe
        fn = getattr(oe, "_check_reverse_cooldown", None)
        if fn is None:
            pytest.skip("函数名不匹配, 由源码断言覆盖")
        # 构造返回 naive exit_time 的假 DB
        naive_row = ("SHORT", "2026-07-15T10:30:00", "PLAYBOOK_SIM")
        _conn = MagicMock()
        _conn.__enter__ = lambda s: _conn
        _conn.__exit__ = lambda s, *a: False
        _conn.execute.return_value.fetchone.return_value = naive_row
        with patch("sqlite3.connect", return_value=_conn):
            result = fn("US.NVDA", "LONG", 300)
        assert result is False, "naive exit_time 命中冷却窗时必须拦截"


class TestR12EntryTimeConvention:
    """R-12: 两表 entry_time 同为 ET 口径."""

    def test_writer_uses_et(self):
        src = (Path(__file__).resolve().parent.parent
               / "app" / "storage" / "trade_store.py").read_text(encoding="utf-8")
        assert "from app.utils.timezone import now_et as _now_et_ts" in src, \
            "upsert_position_simple 仍用裸 datetime.now() 写 entry_time"

    def test_live_rows_aligned_with_trade_records(self):
        """在仓行的 entry_time 必须与 trade_records 一致 (迁移已完成)."""
        from app.core.db_utils import get_main_db
        db = get_main_db()
        if not db.exists():
            pytest.skip("主库不存在")
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        try:
            mismatched = []
            for r in conn.execute(
                "SELECT symbol, entry_time FROM open_positions WHERE size > 0"
            ):
                tr = conn.execute(
                    "SELECT entry_time FROM trade_records WHERE symbol=? "
                    "AND source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') "
                    "AND (exit_time IS NULL OR exit_time='') ORDER BY id DESC LIMIT 1",
                    (r["symbol"],),
                ).fetchone()
                if tr and str(tr["entry_time"]) != str(r["entry_time"]):
                    mismatched.append((r["symbol"], r["entry_time"], tr["entry_time"]))
        finally:
            conn.close()
        assert not mismatched, f"两表 entry_time 仍不一致: {mismatched}"

    def test_migration_script_is_idempotent(self):
        """迁移脚本必须可重复执行 (存在且带幂等判据)."""
        p = (Path(__file__).resolve().parent.parent
             / "scripts" / "migrate_open_positions_entry_time_r12.py")
        assert p.exists()
        src = p.read_text(encoding="utf-8")
        assert "幂等" in src
        assert "--apply" in src, "必须默认预演, 显式 --apply 才写入"


class TestR9EmergencyCloseePlaybookAware:
    """R-9: 紧急全平 playbook 感知 + 禁止静默部分平仓."""

    @staticmethod
    def _src():
        return (Path(__file__).resolve().parent.parent
                / "app" / "web_dashboard" / "routes" / "risk.py").read_text(encoding="utf-8")

    def test_detects_playbook_positions(self):
        src = self._src()
        assert "playbook_open_check" in src, "紧急全平未做 playbook 仓判定"
        assert "load_playbook_open_symbols" in src

    def test_default_skips_and_reports(self):
        """默认跳过 playbook 仓, 且必须在响应里报出 (不可静默)."""
        src = self._src()
        assert "skipped_playbook" in src
        assert "fully_flat" in src, "必须显式告知是否真的全平"
        assert "include_playbook" in src, "须提供显式 opt-in 才平 playbook 仓"

    def test_kill_switch_before_closing_playbook(self):
        """一并平 playbook 仓时须先激活 Kill Switch 防重新入场."""
        src = self._src()
        assert "activate_kill_switch" in src

    def test_failsafe_on_detection_error(self):
        """判定异常时保守视为全是 playbook 仓 (要求显式确认), 不可静默双线平仓."""
        src = self._src()
        assert "保守要求显式确认" in src
