#!/usr/bin/env python3
"""WeeklyOptimizer 契约测试 (2026-10-03 DEB-20261003 P0-3, 用户裁「同意」).

背景: Task#16 周频参数优化器连续 7 周输出常量建议 (sl=-0.4), 实为两个缺陷的伪影 —
  ① 列名错配: load_trade_rows 请求 `max_drawdown_during_trade`, 生产 trade_records
     真实列名是 `max_drawdown` ⇒ pick() 静默回退 NULL ⇒ _replay_trade 的止损分支
     `row.max_drawdown_during_trade >= abs(sl)` 恒 False ⇒ stop_loss_pct 不可识别;
  ② 虚构基线: LIVE_CURRENT_PARAMS 自称「与 unified_config.yaml 对齐」, 实测
     stop_loss_pct=-0.045 / take_profit_pct=0.025 / trailing_stop_trigger=0.01
     在 config/*.yaml 与 .env.us 全部零命中 (trailing_stop_trigger 键名不存在,
     且全仓零消费点 = 死参数; min_signal_strength playbook 链不读)。

本文件两条契约均为 **xfail(strict=True) 绊线**: 当前生产代码红着 (已知缺陷),
修复后变 XPASS 立即报警 ⇒ 强制同步删除 xfail 标记并复核 P2-6 重建方案。
禁止把 xfail 改成普通 skip — skip 不会在修复时报警, 绊线语义就丢了。

推送已由 _TASK16_WEEKLY_OPTIMIZER_ENABLED=False 停掉 (P0-1), 本测试不依赖推送状态;
Task#16 恢复 (置 True) 的前提之一就是这两条契约转绿 + P2-6 架构收敛落地。
"""
import os
import re
import sqlite3
import sys
import tempfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from app.backtest import weekly_optimizer as wo  # noqa: E402

# 生产 DDL 真源 (app/storage/trade_store.py:171 CREATE TABLE trade_records)
_TRADE_STORE_DDL = os.path.join(REPO, "app", "storage", "trade_store.py")


def _prod_trade_records_columns():
    """从生产 DDL 真源提取 trade_records 列集合 (CREATE 块 + ALTER 增量)。"""
    with open(_TRADE_STORE_DDL, encoding="utf-8") as f:
        src = f.read()
    m = re.search(
        r"CREATE TABLE IF NOT EXISTS trade_records \((.*?)\n\s*\)\s*\n", src, re.S
    )
    assert m, "trade_store.py 中找不到 trade_records CREATE TABLE 块 (DDL 真源漂移)"
    cols = set(re.findall(r"^\s*(\w+)\s+(?:INTEGER|TEXT|REAL)", m.group(1), re.M))
    cols.update(re.findall(r"ADD COLUMN (\w+)\s", src))
    return cols


class TestColumnNameContract:
    """契约①: 列名存在性 — pick() 不得对核心列静默回退 NULL。"""

    def test_ddl_source_has_max_drawdown(self):
        """前提自检: 生产 DDL 真源确有 max_drawdown 列 (本契约的锚)。"""
        cols = _prod_trade_records_columns()
        assert "max_drawdown" in cols
        assert "max_drawdown_during_trade" not in cols

    @pytest.mark.xfail(
        strict=True,
        reason="P0-①列名错配: load_trade_rows 请求 max_drawdown_during_trade, "
               "生产列名是 max_drawdown ⇒ 静默 NULL ⇒ SL 反事实从未触发。"
               "修复后本测试转 XPASS: 删 xfail 标记并走 DEB-20261003 P2-6 复核。",
    )
    def test_requested_columns_exist_in_production_ddl(self):
        """load_trade_rows 请求的每一列必须存在于生产 DDL (无静默 NULL 回退)。"""
        requested = [
            "trade_date", "symbol", "entry_price", "exit_price",
            "pnl_pct", "realized_pnl", "signal_strength",
            "max_drawdown_during_trade", "holding_hours",
        ]
        cols = _prod_trade_records_columns()
        missing = [c for c in requested if c not in cols]
        assert not missing, f"请求列在生产 DDL 缺失 (pick() 会静默回退 NULL): {missing}"

    @pytest.mark.xfail(
        strict=True,
        reason="同契约①: mdd 恒 NULL→0.0 ⇒ 止损分支恒 False。修复后 XPASS 报警。",
    )
    def test_load_trade_rows_returns_nonzero_mdd(self):
        """用生产 DDL 建表 + 造 max_drawdown>0 的行, load 后 mdd 必须保真非零。"""
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "t.db")
            store_src_cols = _prod_trade_records_columns()
            conn = sqlite3.connect(db)
            # 按生产 DDL 最小列集建表 (含 max_drawdown, 不含 max_drawdown_during_trade)
            conn.execute(
                "CREATE TABLE trade_records ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, symbol TEXT,"
                "direction TEXT, entry_time TEXT, exit_time TEXT, entry_price REAL,"
                "exit_price REAL, quantity INTEGER, realized_pnl REAL, pnl_pct REAL,"
                "exit_reason TEXT, holding_hours REAL, signal_strength INTEGER,"
                "signal_confidence REAL, futu_order_id TEXT, max_drawdown REAL,"
                "trace_id TEXT, created_at TEXT)"
            )
            conn.execute(
                "INSERT INTO trade_records (trade_date, symbol, direction, entry_price,"
                " exit_price, pnl_pct, realized_pnl, signal_strength, max_drawdown,"
                " holding_hours, created_at) VALUES"
                " ('2026-09-30','US.TEST','LONG',100.0,98.0,-0.02,-20.0,60,2.5,1.0,'x'),"
                " ('2026-09-30','US.TEST2','SHORT',100.0,102.0,-0.02,-20.0,60,3.1,2.0,'x')"
            )
            conn.commit()
            conn.close()
            rows = wo.load_trade_rows(db_path=__import__("pathlib").Path(db))
            assert rows, "load_trade_rows 返回空 (建表/插行失败?)"
            mdds = [r.max_drawdown_during_trade for r in rows]
            assert all(m > 0 for m in mdds), (
                f"max_drawdown>0 的行 load 后 mdd 全为 {mdds} ⇒ 列名错配静默丢数"
            )


class TestParamIdentifiabilityContract:
    """契约②: 参数可识别性 — 每个网格参数至少 2 个值必须产生不同 pnl 序列。

    合成人群设计 (每行钉一个分支; mdd 以小数直插, 避开 >1.5 量纲启发式的错读侧,
    使本契约只钉「列名错配」单因 — 量纲 bug 属 P0-2②, 由 P2-6 重建一并消除):
      A pnl=-0.10 mdd=0.12 strength=90  → SL 网格中段不触发/末段触发
      B pnl=-0.18 mdd=0.20 strength=90  → SL=-0.20/-0.15 触发, 更宽档不触发
      C pnl=+0.09 mdd=0.01 strength=90  → TP 各档逐一命中
      D pnl=+0.04 mdd=0.005 strength=90 → TP=0.03 命中, 更宽档走 trail
      E pnl=+0.01 mdd=0.002 strength=40 → mss 40/50 边界
      F pnl=-0.02 mdd=0.008 strength=65 → mss 60/70 边界
      G pnl=+0.022 mdd=0.02 strength=90 → 落在 [trail 网格 0.02, base tp 0.025) 窄带,
                                          trig=0.02 与 trig≥0.03 结果不同 ⇒ trail 可识别
    """

    _ROWS = [
        # (symbol, pnl_pct, max_drawdown, signal_strength)
        ("A", -0.10, 0.12, 90),
        ("B", -0.18, 0.20, 90),
        ("C", +0.09, 0.01, 90),
        ("D", +0.04, 0.005, 90),
        ("E", +0.01, 0.002, 40),
        ("F", -0.02, 0.008, 65),
        ("G", +0.022, 0.02, 90),
    ]

    @pytest.mark.xfail(
        strict=True,
        reason="P0-②参数不可识别: 生产 _replay_trade 依赖的 mdd 在真实数据链上恒 0 "
               "(契约①同根因) ⇒ stop_loss_pct 5 网格值 1 个 pnl 序列, 报告 sensitivity "
               "五点全等被误读为「参数稳健」。修复后 XPASS 报警。",
    )
    def test_every_grid_param_is_identifiable_via_production_loader(self):
        """端到端: 生产 DDL 建表 → load_trade_rows → replay, 每参数 ≥2 值序列不同。

        与纯合成 TradeRow 单测的区别: 本测试走**生产数据链** (DDL→load→replay),
        列名错配/量纲猜测任一环节丢数都会让 stop_loss_pct 退化为常量序列。
        """
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "t.db")
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE trade_records ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, symbol TEXT,"
                "direction TEXT, entry_price REAL, exit_price REAL, pnl_pct REAL,"
                "realized_pnl REAL, signal_strength INTEGER, max_drawdown REAL,"
                "holding_hours REAL, created_at TEXT)"
            )
            conn.executemany(
                "INSERT INTO trade_records (trade_date, symbol, direction, entry_price,"
                " exit_price, pnl_pct, realized_pnl, signal_strength, max_drawdown,"
                " holding_hours, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    ("2026-09-30", sym, "LONG", 100.0, 100.0 * (1 + pnl), pnl,
                     pnl * 5000, strength, mdd, 1.0, "x")
                    for sym, pnl, mdd, strength in self._ROWS
                ],
            )
            conn.commit()
            conn.close()
            rows = wo.load_trade_rows(db_path=__import__("pathlib").Path(db))
            assert len(rows) == len(self._ROWS)

            base = dict(wo.LIVE_CURRENT_PARAMS)
            dead = []
            for pname, grid in wo.PARAM_GRID.items():
                series = []
                for v in grid:
                    p = dict(base)
                    p[pname] = v
                    series.append(wo.replay_pnl_series(rows, p))
                distinct = {tuple(s) for s in series}
                if len(distinct) < 2:
                    dead.append(pname)
            assert not dead, f"参数对 pnl 序列零影响 (不可识别): {dead}"


class TestPushStoppedContract:
    """契约③ (非 xfail, 常绿守卫): Task#16 停推送状态必须显式且可追。

    P0-1 已停推送; 本测试钉住 kill switch 的当前裁决状态, 防止无声恢复。
    恢复推送 = 有意识的变更: 必须同时改本测试 + 满足 DEB-20261003 恢复条件。
    """

    def test_task16_kill_switch_is_off(self):
        from app.core import scheduler_triggers as st
        assert st._TASK16_WEEKLY_OPTIMIZER_ENABLED is False, (
            "Task#16 被重新启用 — 按 DEB-20261003 恢复条件, 须先落地 P2-6 重建"
            "并使契约①②转绿 (删 xfail), 再同批更新本测试"
        )

    def test_scheduler_guard_wired_before_imports(self):
        """守卫必须挡在 run_weekly_optimization import 之前 (源码顺序契约)。"""
        src_path = os.path.join(REPO, "app", "core", "scheduler_triggers.py")
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
        guard = src.index("_TASK16_WEEKLY_OPTIMIZER_ENABLED:")
        imp = src.index("from app.backtest.weekly_optimizer import run_weekly_optimization")
        assert guard < imp, "kill switch 守卫必须在 weekly_optimizer import 之前"
