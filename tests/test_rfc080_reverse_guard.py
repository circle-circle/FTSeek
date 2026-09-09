# -*- coding: utf-8 -*-
"""RFC-080 S3b: 手动下单反向守卫单测 (缺口4).

正向 (auto 避手动) 已有 ManualHoldRegistry; 反向 (手动避 auto-live) 此前无守卫。
冲突机制: open_positions PK=symbol 单列 ⇒ 同标的 auto-live 行与 manual 行物理无法
共存 → 手动单成交后 broker 净额合并 → 一行两主 → auto 仓孤儿化。

现状 (TRADE_ENV=1): auto 仓在 sim 库/模拟账户, live 库无 owner=auto 行 ⇒ 守卫恒 no-op。
cutover (单库翻转) 后: auto-live 行落 live 库 ⇒ 守卫生效。
"""
import sqlite3

import pytest


def _mk_live_db(tmp_path, rows):
    """建临时 live 库 + open_positions (rows = [(symbol, owner, size)])。"""
    db = tmp_path / "ftseek_us_live.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE open_positions (symbol TEXT PRIMARY KEY, owner TEXT, "
                 "owner_strategy TEXT, size REAL)")
    conn.executemany("INSERT INTO open_positions (symbol, owner, owner_strategy, size) "
                     "VALUES (?, ?, 'playbook_live', ?)", rows)
    conn.commit()
    conn.close()
    return db


def _guard_decision(db, symbol):
    """复刻 executor.submit_order 1b 反向守卫判定 (RFC-080 S3b), 与实现同构。"""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
    try:
        row = conn.execute(
            "SELECT owner_strategy FROM open_positions "
            "WHERE symbol=? AND owner='auto' AND COALESCE(size,0) > 0 LIMIT 1",
            (symbol,),
        ).fetchone()
    finally:
        conn.close()
    return row  # None = 放行; 非 None = 拦截


class TestReverseGuard:
    def test_auto_held_symbol_blocked(self, tmp_path):
        """cutover 核心: auto-live 持有 (owner=auto, size>0) → 手动单拦截."""
        db = _mk_live_db(tmp_path, [("US.TSLA", "auto", 50)])
        assert _guard_decision(db, "US.TSLA") is not None

    def test_manual_held_symbol_passes(self, tmp_path):
        """owner=manual 行不触发反向守卫 (那是正向 ManualHoldRegistry 的职责)."""
        db = _mk_live_db(tmp_path, [("US.MU", "manual", 10)])
        assert _guard_decision(db, "US.MU") is None

    def test_stale_auto_size0_passes(self, tmp_path):
        """size=0 陈旧 auto 行不拦 (与 S3a 教训同向: size 陈旧不应误判)."""
        db = _mk_live_db(tmp_path, [("US.OLD", "auto", 0.0)])
        assert _guard_decision(db, "US.OLD") is None

    def test_no_row_passes(self, tmp_path):
        """现状常态: live 库无该 symbol auto 行 → 放行 (no-op)."""
        db = _mk_live_db(tmp_path, [])
        assert _guard_decision(db, "US.ANY") is None


class TestGuardWiredInSource:
    """防漂移钉桩: 反向守卫必须真实存在于 submit_order 的 validate_order 之后。"""

    def test_guard_present_in_submit_order(self):
        import inspect
        from app.web_dashboard.order import executor
        src = inspect.getsource(executor.submit_order)
        assert "反向守卫" in src, "S3b 守卫段被删除/漂移"
        assert "auto_position_conflict" in src
        assert "owner='auto'" in src
        # 必须在 validate_order (步骤1) 之后、connect broker (步骤2) 之前
        assert src.find("validate_order") < src.find("反向守卫") < src.find("# 2. 连接 broker")

    def test_guard_fail_open(self):
        """fail-open 哲学钉桩: 协调门 DB 异常不阻断手动交易 (与 ManualHoldRegistry 同款)."""
        import inspect
        from app.web_dashboard.order import executor
        src = inspect.getsource(executor.submit_order)
        idx = src.find("反向守卫")
        seg = src[idx:idx + 1400]
        assert "fail-open" in seg
