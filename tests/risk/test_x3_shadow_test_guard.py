"""X3 契约测试 (2026-08-23, DEB-20260823-system-health-check).

背景: pipeline.py 已有 9 处 TEST 测试卫生守卫 (8/18-19 生产 jsonl 曾被单测数据污染),
但五个影子模块的落盘入口零守卫 —— pytest 直调 US.TEST* 会污染生产影子台账,
直接威胁 OKR-2026-09 KR2 冻结期 250 笔样本纯度. 本文件锁住收口后的语义:
**symbol 含 TEST → 不落盘、不写库**.

覆盖 (行为级, 落盘路径 patch 到 tmp, 不触生产 data/):
  exit_shadow.finalize_exit_shadow      (唯一 jsonl 写点)
  sltp_verify_shadow.record_sltp_shadow (force=True 绕门直写路径)
  overnight_hold_shadow.emit
  storage.shadow_signal_logger.record
  playbook.shadow_tracker._track_instruction (源码锚点: 守卫在 db 写之前)
"""
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import app.playbook.exit_shadow as exit_shadow_mod
import app.playbook.overnight_hold_shadow as overnight_mod
import app.playbook.sltp_verify_shadow as sltp_mod
from app.playbook.exit_shadow import finalize_exit_shadow
from app.playbook.overnight_hold_shadow import emit as overnight_emit
from app.playbook.sltp_verify_shadow import record_sltp_shadow
from app.storage.shadow_signal_logger import ShadowSignalLogger


@pytest.fixture
def tmp_shadow(tmp_path):
    """把三个模块的落盘路径指到 tmp, 返回路径字典."""
    p_exit = tmp_path / "exit_param_shadow.jsonl"
    p_sltp = tmp_path / "sltp_verify_shadow.jsonl"
    p_overnight = tmp_path / "overnight_hold_shadow.jsonl"
    with patch.object(exit_shadow_mod, "_SHADOW_PATH", p_exit), \
         patch.object(sltp_mod, "_SHADOW_PATH", p_sltp), \
         patch.object(overnight_mod, "_OUTPUT", p_overnight):
        yield {"exit": p_exit, "sltp": p_sltp, "overnight": p_overnight}


def _exit_finalize_kwargs(symbol):
    return dict(
        symbol=symbol, direction="LONG", entry_price=100.0,
        live_exit_price=101.0, peak_price=102.0, trough_price=99.0,
        live_exit_reason="SL_TRAIL_LOCK", held_minutes=30.0, meta={},
    )


class TestX3TestSymbolNeverPersists:
    def test_exit_shadow_finalize_rejects_test(self, tmp_shadow):
        res = finalize_exit_shadow(**_exit_finalize_kwargs("US.TEST1"))
        assert res is None
        assert not tmp_shadow["exit"].exists()

    def test_exit_shadow_finalize_writes_real_symbol(self, tmp_shadow):
        """对照: 正常 symbol 照常落盘 (确认守卫没把门改死)."""
        with patch("app.playbook.exit_shadow.is_enabled", return_value=True):
            res = finalize_exit_shadow(**_exit_finalize_kwargs("US.NVDA"))
        assert res is not None
        assert tmp_shadow["exit"].exists()

    def test_sltp_record_rejects_test_even_forced(self, tmp_shadow):
        """force=True 绕启用门, 但绕不过 TEST 卫生守卫."""
        res = record_sltp_shadow(symbol="US.TEST1", force=True)
        assert res is None
        assert not tmp_shadow["sltp"].exists()

    def test_sltp_record_writes_real_symbol(self, tmp_shadow):
        res = record_sltp_shadow(symbol="US.NVDA", force=True)
        assert res is not None
        assert tmp_shadow["sltp"].exists()

    def test_overnight_emit_rejects_test(self, tmp_shadow):
        with patch.object(overnight_mod, "_enabled", return_value=True):
            ok = overnight_emit("US.TEST1", "LONG", "PRE_CLOSE",
                                exit_price=101.0, entry_price=100.0, pnl_net=5.0)
        assert ok is False
        assert not tmp_shadow["overnight"].exists()

    def test_signal_logger_rejects_test(self, tmp_path):
        logger = ShadowSignalLogger(log_path=str(tmp_path / "shadow_signals.jsonl"))
        cand = MagicMock(symbol="US.TEST1", is_long=True, strength=80,
                         confidence=70, strategy_id="s", source="t", rationale="r")
        logger.record(cand, {"price": 100})
        assert not (tmp_path / "shadow_signals.jsonl").exists()

    def test_signal_logger_writes_real_symbol(self, tmp_path):
        """对照: 正常 symbol 照常落盘."""
        path = tmp_path / "shadow_signals.jsonl"
        logger = ShadowSignalLogger(log_path=str(path))
        cand = MagicMock(symbol="US.NVDA", is_long=True, strength=80,
                         confidence=70, strategy_id="s", source="t", rationale="r")
        logger.record(cand, {"price": 100})
        assert path.exists()


class TestX3ShadowTrackerSourceContract:
    """_track_instruction 依赖 DB 实例, 行为测试成本高; 源码锚点锁守卫位置."""

    def test_guard_precedes_db_writes(self):
        src = Path("app/playbook/shadow_tracker.py").read_text(encoding="utf-8")
        assert 'if "TEST" in str(symbol' in src, "shadow_tracker 缺 TEST 守卫"
        guard_at = src.index('if "TEST" in str(symbol')
        first_db_write = src.index("self.db.update_instruction_tracking")
        assert guard_at < first_db_write, "TEST 守卫必须位于任何 DB 写之前"

    def test_no_unguarded_shadow_writer_remains(self):
        """五个影子落盘模块必须全部含 TEST 守卫锚点."""
        for rel in (
            "app/playbook/exit_shadow.py",
            "app/playbook/sltp_verify_shadow.py",
            "app/playbook/overnight_hold_shadow.py",
            "app/playbook/shadow_tracker.py",
            "app/storage/shadow_signal_logger.py",
        ):
            src = Path(rel).read_text(encoding="utf-8")
            assert 'if "TEST" in str(' in src, f"{rel} 缺 TEST 卫生守卫"
