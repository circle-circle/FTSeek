# -*- coding: utf-8 -*-
"""RFC-111 (2026-09-26): 同日硬止损冷却闸 — 单元级验证。

9/24 ORCL 教训: 09:30:35 OVERNIGHT_GAP_STOP 割肉 → 09:38:27 DA 同向 BUY 重开
→ 09:51:47 SL_INITIAL 再割。_HARD_STOP_REASONS 此前只被 intraday_reentry 消费,
DA 主通道 auto_execute 执行前零查询。

被测: app.playbook.pipeline._check_same_day_hard_stop_cooldown (纯查询函数)
     + auto_execute 循环接线点存在性 (源码契约)。
"""
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture()
def tmp_db(tmp_path):
    """最小 trade_records 表 (只建被测查询用到的列)。"""
    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE trade_records ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " symbol TEXT, direction TEXT, source_type TEXT,"
        " entry_time TEXT, exit_time TEXT, exit_reason TEXT,"
        " entry_price REAL, quantity REAL, realized_pnl REAL)"
    )
    conn.commit()
    conn.close()
    return db


def _insert(db, **kw):
    conn = sqlite3.connect(str(db))
    cols = ", ".join(kw.keys())
    ph = ", ".join("?" for _ in kw)
    conn.execute(f"INSERT INTO trade_records ({cols}) VALUES ({ph})", tuple(kw.values()))
    conn.commit()
    conn.close()


def _fn(tmp_db):
    """把 DB 路径打桩后取被测函数。"""
    with patch("app.core.paths.get_trade_records_db", return_value=tmp_db):
        from app.playbook.pipeline import _check_same_day_hard_stop_cooldown
        return _check_same_day_hard_stop_cooldown


class TestCooldownGate:
    def test_orcl_case_hard_stop_same_direction_blocked(self, tmp_db):
        """9/24 ORCL 复现: 当日 OVERNIGHT_GAP_STOP 出场 → 同向 BUY 必拦。"""
        _insert(tmp_db, symbol="US.ORCL", direction="LONG", source_type="PLAYBOOK_SIM",
                entry_time="2026-09-23T09:40:57-04:00",
                exit_time="2026-09-24T09:30:35-04:00",
                exit_reason="OVERNIGHT_GAP_STOP",
                entry_price=147.39, quantity=40, realized_pnl=-400.14)
        with patch("app.core.paths.get_trade_records_db", return_value=tmp_db):
            from app.playbook.pipeline import _check_same_day_hard_stop_cooldown as fn
            hit = fn("US.ORCL", "BUY", "2026-09-24")
        assert hit is not None
        assert hit["prior_exit_reason"] == "OVERNIGHT_GAP_STOP"

    def test_benign_exit_not_blocked(self, tmp_db):
        """良性出场 (SL_TRAIL_LOCK 锁盈) → 不拦 (与再入场通道 prior_exit_reenterable 同语义)。"""
        _insert(tmp_db, symbol="US.NVDA", direction="LONG", source_type="PLAYBOOK_SIM",
                entry_time="2026-09-24T09:35:00-04:00",
                exit_time="2026-09-24T11:00:00-04:00",
                exit_reason="SL_TRAIL_LOCK",
                entry_price=180.0, quantity=10, realized_pnl=55.0)
        with patch("app.core.paths.get_trade_records_db", return_value=tmp_db):
            from app.playbook.pipeline import _check_same_day_hard_stop_cooldown as fn
            assert fn("US.NVDA", "BUY", "2026-09-24") is None

    def test_opposite_direction_not_blocked(self, tmp_db):
        """当日 LONG 硬止损后, SHORT 指令不由本门管 (反手属 same_day_reversal_block 域)。"""
        _insert(tmp_db, symbol="US.ORCL", direction="LONG", source_type="PLAYBOOK_SIM",
                entry_time="2026-09-24T09:31:00-04:00",
                exit_time="2026-09-24T09:51:47-04:00",
                exit_reason="SL_INITIAL",
                entry_price=138.43, quantity=21, realized_pnl=-83.97)
        with patch("app.core.paths.get_trade_records_db", return_value=tmp_db):
            from app.playbook.pipeline import _check_same_day_hard_stop_cooldown as fn
            assert fn("US.ORCL", "SELL_SHORT", "2026-09-24") is None

    def test_prior_day_exit_not_blocked(self, tmp_db):
        """昨日硬止损不影响今日 (日期口径 = exit_time 的 ET 日)。"""
        _insert(tmp_db, symbol="US.ORCL", direction="LONG", source_type="PLAYBOOK_SIM",
                entry_time="2026-09-22T09:40:00-04:00",
                exit_time="2026-09-23T10:00:00-04:00",
                exit_reason="SL_INITIAL",
                entry_price=150.0, quantity=10, realized_pnl=-30.0)
        with patch("app.core.paths.get_trade_records_db", return_value=tmp_db):
            from app.playbook.pipeline import _check_same_day_hard_stop_cooldown as fn
            assert fn("US.ORCL", "BUY", "2026-09-24") is None

    def test_non_playbook_source_not_blocked(self, tmp_db):
        """外部/手动通道出场不构成 playbook 冷却依据 (S3 同口径)。"""
        _insert(tmp_db, symbol="US.AMD", direction="LONG", source_type="MANUAL",
                entry_time="2026-09-24T09:35:00-04:00",
                exit_time="2026-09-24T10:00:00-04:00",
                exit_reason="SL_INITIAL",
                entry_price=150.0, quantity=10, realized_pnl=-30.0)
        with patch("app.core.paths.get_trade_records_db", return_value=tmp_db):
            from app.playbook.pipeline import _check_same_day_hard_stop_cooldown as fn
            assert fn("US.AMD", "BUY", "2026-09-24") is None

    def test_open_position_no_exit_time_not_blocked(self, tmp_db):
        """未平仓行 (exit_time 空) 不参与判定 — NULL 语义显式排除。"""
        _insert(tmp_db, symbol="US.AVGO", direction="LONG", source_type="PLAYBOOK_SIM",
                entry_time="2026-09-23T09:44:24-04:00",
                exit_time="", exit_reason="",
                entry_price=300.0, quantity=5, realized_pnl=0.0)
        with patch("app.core.paths.get_trade_records_db", return_value=tmp_db):
            from app.playbook.pipeline import _check_same_day_hard_stop_cooldown as fn
            assert fn("US.AVGO", "BUY", "2026-09-24") is None

    def test_fail_open_on_db_error(self):
        """查询异常 → None (fail-open, 质量门非安全门)。"""
        with patch("app.core.paths.get_trade_records_db",
                   side_effect=RuntimeError("db gone")):
            from app.playbook.pipeline import _check_same_day_hard_stop_cooldown as fn
            assert fn("US.X", "BUY", "2026-09-24") is None

    def test_ssot_reasons_shared_with_reentry(self):
        """SSOT: 门引用的原因集与再入场通道是同一常量对象 (禁双份清单漂移)。"""
        import app.playbook.intraday_reentry as ir
        import inspect
        from app.playbook.pipeline_parts import risk_gates
        src = inspect.getsource(risk_gates._check_same_day_hard_stop_cooldown)
        assert "_HARD_STOP_REASONS" in src
        # 关键硬止损原因必须在集内 (9/24 两笔的出场原因)
        assert "OVERNIGHT_GAP_STOP" in ir._HARD_STOP_REASONS
        assert "SL_INITIAL" in ir._HARD_STOP_REASONS


class TestWiring:
    """源码契约: 门必须接在 auto_execute 循环内, 且受配置开关控制。"""

    def test_gate_wired_in_auto_execute(self):
        import inspect
        from app.playbook.pipeline_parts import auto_execute_flow
        src = inspect.getsource(auto_execute_flow.run_playbook_auto_execute)
        assert "same_day_hard_stop_cooldown" in src
        assert "_check_same_day_hard_stop_cooldown" in src
        assert "skipped_hard_stop_cooldown" in src  # 审计行可归因

    def test_config_key_present(self):
        import yaml
        cfg = yaml.safe_load(
            Path(__file__).resolve().parents[1]
            .joinpath("config", "unified_config.yaml").read_text())
        assert cfg["playbook"]["same_day_hard_stop_cooldown"] is True
