"""TAKE_PROFIT counterfactual replay — hermetic 单元测试.

覆盖:
1. analyze_trades 纯函数: 多阈值触发判定 + PnL 复算
2. LONG/SHORT direction_sign 正确性
3. net_delta 累计正确性
4. 只读 DB 连接 (写入应失败)
5. mfe_pct=0.05 (5%) 在阈值 ≤0.05 时全部触发
6. mfe_pct=0.001 (0.1%) 在所有候选阈值下均不触发
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _load_mod():
    """加载 take_profit_counterfactual_replay 模块 (避免 conftest 冲突)."""
    spec = importlib.util.spec_from_file_location(
        "take_profit_counterfactual_replay",
        ROOT / "scripts" / "take_profit_counterfactual_replay.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tp_mod():
    return _load_mod()


# ── Fixture 数据 ──

def _make_trades():
    """构造 8 笔覆盖不同场景的交易数据。"""
    return [
        # T1: LONG, mfe_pct=0.05 (5%), 盈利单
        {
            "trade_id": 1, "symbol": "AAPL", "direction": "LONG",
            "entry_price": 100.0, "quantity": 10.0,
            "mfe_pct": 0.05, "pnl_net": 30.0, "fees_est": 2.0,
        },
        # T2: SHORT, mfe_pct=0.03 (3%), 盈利单
        {
            "trade_id": 2, "symbol": "TSLA", "direction": "SHORT",
            "entry_price": 200.0, "quantity": 5.0,
            "mfe_pct": 0.03, "pnl_net": 20.0, "fees_est": 1.5,
        },
        # T3: LONG, mfe_pct=0.001 (0.1%), 亏损单 — 不触发任何阈值
        {
            "trade_id": 3, "symbol": "NVDA", "direction": "LONG",
            "entry_price": 300.0, "quantity": 3.0,
            "mfe_pct": 0.001, "pnl_net": -15.0, "fees_est": 1.0,
        },
        # T4: LONG, mfe_pct=0.02 (2%), 微利
        {
            "trade_id": 4, "symbol": "MSFT", "direction": "LONG",
            "entry_price": 150.0, "quantity": 8.0,
            "mfe_pct": 0.02, "pnl_net": 5.0, "fees_est": 2.0,
        },
        # T5: SHORT, mfe_pct=0.04 (4%), 亏损单 (实际止损出场)
        {
            "trade_id": 5, "symbol": "META", "direction": "SHORT",
            "entry_price": 250.0, "quantity": 4.0,
            "mfe_pct": 0.04, "pnl_net": -10.0, "fees_est": 1.5,
        },
        # T6: LONG, mfe_pct=0.015 (1.5%), 微利
        {
            "trade_id": 6, "symbol": "GOOG", "direction": "LONG",
            "entry_price": 120.0, "quantity": 10.0,
            "mfe_pct": 0.015, "pnl_net": 8.0, "fees_est": 2.5,
        },
        # T7: LONG, mfe_pct=0.0 (无浮盈记录) — 不触发
        {
            "trade_id": 7, "symbol": "AMZN", "direction": "LONG",
            "entry_price": 180.0, "quantity": 5.0,
            "mfe_pct": 0.0, "pnl_net": -5.0, "fees_est": 1.0,
        },
        # T8: SHORT, mfe_pct=0.05 (5%), 大盈利
        {
            "trade_id": 8, "symbol": "NFLX", "direction": "SHORT",
            "entry_price": 400.0, "quantity": 2.0,
            "mfe_pct": 0.05, "pnl_net": 25.0, "fees_est": 1.0,
        },
    ]


# ── Tests: analyze_trades 核心逻辑 ──

class TestAnalyzeTrades:
    """测试 analyze_trades 纯函数。"""

    def test_mfe_5pct_triggers_all_thresholds(self, tp_mod):
        """mfe_pct=0.05 (5%) 的交易应在所有阈值 ≤0.05 时触发。"""
        trades = [_make_trades()[0]]  # T1: LONG, mfe_pct=0.05
        thresholds = [0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05]
        report = tp_mod.analyze_trades(trades, thresholds)

        for tp_str, data in report["thresholds"].items():
            assert data["n_triggered"] == 1, (
                f"TP={tp_str}: mfe_pct=0.05 should trigger at all thresholds ≤0.05"
            )

    def test_mfe_0_1pct_triggers_none(self, tp_mod):
        """mfe_pct=0.001 (0.1%) 的交易不应在任何候选阈值下触发。"""
        trades = [_make_trades()[2]]  # T3: LONG, mfe_pct=0.001
        thresholds = [0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05]
        report = tp_mod.analyze_trades(trades, thresholds)

        for tp_str, data in report["thresholds"].items():
            assert data["n_triggered"] == 0, (
                f"TP={tp_str}: mfe_pct=0.001 should not trigger any threshold"
            )

    def test_short_direction_sign(self, tp_mod):
        """SHORT 方向: direction_sign=-1, hypo_pnl 应为负值 (空头盈利靠价格下跌)。"""
        trades = [_make_trades()[1]]  # T2: SHORT, mfe_pct=0.03
        # 仅测 0.02 阈值 (mfe_pct=0.03 >= 0.02, 会触发)
        report = tp_mod.analyze_trades(trades, [0.02])
        data = report["thresholds"]["0.02"]
        assert data["n_triggered"] == 1

        detail = data["details"][0]
        # SHORT: hypo_pnl = 200 * 0.02 * 5 * (-1) - 1.5 = -20 - 1.5 = -21.5
        expected_hypo = 200.0 * 0.02 * 5.0 * (-1.0) - 1.5
        assert abs(detail["hypo_pnl_net"] - expected_hypo) < 0.01, (
            f"SHORT hypo_pnl should be {expected_hypo}, got {detail['hypo_pnl_net']}"
        )

    def test_long_direction_hypo_pnl(self, tp_mod):
        """LONG 方向: hypo_pnl 正值验证。"""
        trades = [_make_trades()[0]]  # T1: LONG, entry=100, qty=10, mfe=0.05
        report = tp_mod.analyze_trades(trades, [0.03])
        data = report["thresholds"]["0.03"]
        assert data["n_triggered"] == 1

        detail = data["details"][0]
        # LONG: hypo_pnl = 100 * 0.03 * 10 * 1 - 2.0 = 30 - 2 = 28
        expected_hypo = 100.0 * 0.03 * 10.0 * 1.0 - 2.0
        assert abs(detail["hypo_pnl_net"] - expected_hypo) < 0.01

    def test_net_delta_calculation(self, tp_mod):
        """net_delta = sum(hypo_pnl - actual_pnl_net) for all triggered trades."""
        trades = _make_trades()
        thresholds = [0.02]  # 只有 mfe_pct >= 0.02 的会触发
        report = tp_mod.analyze_trades(trades, thresholds)
        data = report["thresholds"]["0.02"]

        # 手动计算: T1(mfe=0.05), T2(mfe=0.03), T4(mfe=0.02), T5(mfe=0.04), T8(mfe=0.05)
        # T1: hypo = 100*0.02*10*1 - 2 = 18; delta = 18 - 30 = -12
        # T2: hypo = 200*0.02*5*(-1) - 1.5 = -21.5; delta = -21.5 - 20 = -41.5
        # T4: hypo = 150*0.02*8*1 - 2 = 22; delta = 22 - 5 = 17
        # T5: hypo = 250*0.02*4*(-1) - 1.5 = -21.5; delta = -21.5 - (-10) = -11.5
        # T8: hypo = 400*0.02*2*(-1) - 1 = -17; delta = -17 - 25 = -42
        expected_delta = -12 + (-41.5) + 17 + (-11.5) + (-42)
        assert abs(data["net_delta"] - expected_delta) < 0.1, (
            f"net_delta should be ~{expected_delta}, got {data['net_delta']}"
        )
        assert data["n_triggered"] == 5

    def test_improved_degraded_counts(self, tp_mod):
        """验证 improved/degraded 计数。"""
        trades = _make_trades()
        thresholds = [0.02]
        report = tp_mod.analyze_trades(trades, thresholds)
        data = report["thresholds"]["0.02"]

        # T1: delta=-12 → degraded; T2: delta=-41.5 → degraded
        # T4: delta=17 → improved; T5: delta=-11.5 → degraded; T8: delta=-42 → degraded
        assert data["n_improved"] == 1  # T4 only
        assert data["n_degraded"] == 4  # T1, T2, T5, T8

    def test_empty_trades(self, tp_mod):
        """空交易列表应返回零触发。"""
        report = tp_mod.analyze_trades([], [0.01, 0.02, 0.05])
        assert report["n_trades"] == 0
        for data in report["thresholds"].values():
            assert data["n_triggered"] == 0
            assert data["net_delta"] == 0.0

    def test_pct_improved(self, tp_mod):
        """pct_improved = n_improved / n_triggered * 100."""
        # 构造 2 笔: 1 improved + 1 degraded at tp=0.01
        trades = [
            {"trade_id": 1, "symbol": "A", "direction": "LONG",
             "entry_price": 100.0, "quantity": 10.0,
             "mfe_pct": 0.05, "pnl_net": 5.0, "fees_est": 1.0},
            # hypo at 0.01 = 100*0.01*10*1 - 1 = 9; delta = 9-5 = 4 → improved
            {"trade_id": 2, "symbol": "B", "direction": "LONG",
             "entry_price": 100.0, "quantity": 10.0,
             "mfe_pct": 0.05, "pnl_net": 50.0, "fees_est": 1.0},
            # hypo at 0.01 = 9; delta = 9-50 = -41 → degraded
        ]
        report = tp_mod.analyze_trades(trades, [0.01])
        data = report["thresholds"]["0.01"]
        assert data["n_triggered"] == 2
        assert data["n_improved"] == 1
        assert data["n_degraded"] == 1
        assert abs(data["pct_improved"] - 50.0) < 0.01

    def test_mfe_zero_triggers_none(self, tp_mod):
        """mfe_pct=0.0 不应触发任何阈值。"""
        trades = [_make_trades()[6]]  # T7: mfe_pct=0.0
        report = tp_mod.analyze_trades(trades, [0.01, 0.02, 0.05])
        for data in report["thresholds"].values():
            assert data["n_triggered"] == 0

    def test_unknown_direction_skipped(self, tp_mod):
        """未知方向 (非 LONG/SHORT) 的交易: 触发但不计入 improved/degraded。"""
        trades = [
            {"trade_id": 99, "symbol": "X", "direction": "UNKNOWN",
             "entry_price": 100.0, "quantity": 10.0,
             "mfe_pct": 0.05, "pnl_net": 10.0, "fees_est": 1.0},
        ]
        report = tp_mod.analyze_trades(trades, [0.01])
        data = report["thresholds"]["0.01"]
        # mfe >= threshold → n_triggered += 1
        assert data["n_triggered"] == 1
        # sign == 0 → skip hypo calculation
        assert data["n_improved"] == 0
        assert data["n_degraded"] == 0
        assert data["details"] == []


# ── Tests: 只读 DB 连接 ──

class TestReadOnlyDB:
    """验证 DB 只读连接。"""

    @pytest.fixture
    def tmp_db(self):
        """创建临时 SQLite DB with sample trade_records."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE trade_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT,
                symbol TEXT,
                direction TEXT,
                entry_time TEXT,
                exit_time TEXT,
                entry_price REAL,
                exit_price REAL,
                quantity INTEGER,
                realized_pnl REAL,
                pnl_net REAL,
                fees_est REAL,
                mfe_pct REAL,
                trade_mode TEXT DEFAULT 'simulate',
                source_type TEXT DEFAULT 'PLAYBOOK_SIM'
            )
        """)
        # 插入测试数据
        for tr in _make_trades():
            conn.execute(
                """INSERT INTO trade_records
                   (trade_date, symbol, direction, entry_time, exit_time,
                    entry_price, quantity, realized_pnl, pnl_net, fees_est,
                    mfe_pct, trade_mode, source_type)
                   VALUES (?, ?, ?, '2026-09-24T09:30:00', '2026-09-24T15:00:00',
                           ?, ?, ?, ?, ?, ?, 'simulate', 'PLAYBOOK_SIM')""",
                ("2026-09-24", tr["symbol"], tr["direction"],
                 tr["entry_price"], tr["quantity"],
                 tr["pnl_net"] + tr["fees_est"],  # realized_pnl = pnl_net + fees
                 tr["pnl_net"], tr["fees_est"], tr["mfe_pct"])
            )
        conn.commit()
        conn.close()
        yield path
        os.unlink(path)

    def test_read_only_connection(self, tmp_db, tp_mod):
        """只读连接应能成功读取数据。"""
        trades = tp_mod.load_closed_playbook_sim_trades(tmp_db)
        assert len(trades) == 8

    def test_write_fails_on_readonly_db(self, tmp_db):
        """写入只读 DB 应抛 OperationalError。"""
        conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO trade_records (trade_date, symbol, direction) "
                "VALUES ('2026-01-01', 'FAKE', 'LONG')"
            )
        conn.close()

    def test_load_trades_field_mapping(self, tmp_db, tp_mod):
        """验证 load_closed_playbook_sim_trades 的字段映射正确。

        mfe_pct 从 DB 百分比值 (0.05 = 0.05%) 转换为小数 (0.0005)。
        """
        trades = tp_mod.load_closed_playbook_sim_trades(tmp_db)
        t1 = trades[0]  # AAPL
        assert t1["symbol"] == "AAPL"
        assert t1["direction"] == "LONG"
        assert t1["entry_price"] == 100.0
        assert t1["quantity"] == 10.0
        # DB stores 0.05 (= 0.05%), load converts to decimal: 0.05/100 = 0.0005
        assert abs(t1["mfe_pct"] - 0.0005) < 1e-6
        assert t1["pnl_net"] == 30.0
        assert t1["fees_est"] == 2.0

    def test_filters_non_simulate_trades(self, tmp_db):
        """trade_mode != 'simulate' 的交易应被排除。"""
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            """INSERT INTO trade_records
               (trade_date, symbol, direction, exit_time, pnl_net,
                trade_mode, source_type)
               VALUES ('2026-09-24', 'LIVE1', 'LONG', '2026-09-24T15:00:00',
                       100.0, 'live', 'PLAYBOOK_LIVE')"""
        )
        conn.commit()
        conn.close()

        tp_mod_loaded = _load_mod()
        trades = tp_mod_loaded.load_closed_playbook_sim_trades(tmp_db)
        symbols = [t["symbol"] for t in trades]
        assert "LIVE1" not in symbols
        assert len(trades) == 8  # 只有原始 8 笔 simulate 交易

    def test_filters_open_trades(self, tmp_db):
        """exit_time IS NULL 的未平仓交易应被排除。"""
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            """INSERT INTO trade_records
               (trade_date, symbol, direction, exit_time, pnl_net,
                trade_mode, source_type)
               VALUES ('2026-09-24', 'OPEN1', 'LONG', NULL,
                       NULL, 'simulate', 'PLAYBOOK_SIM')"""
        )
        conn.commit()
        conn.close()

        tp_mod_loaded = _load_mod()
        trades = tp_mod_loaded.load_closed_playbook_sim_trades(tmp_db)
        symbols = [t["symbol"] for t in trades]
        assert "OPEN1" not in symbols


# ── Tests: _direction_sign 辅助函数 ──

class TestDirectionSign:
    """测试 _direction_sign 函数。"""

    def test_long(self, tp_mod):
        assert tp_mod._direction_sign("LONG") == 1.0

    def test_short(self, tp_mod):
        assert tp_mod._direction_sign("SHORT") == -1.0

    def test_case_insensitive(self, tp_mod):
        assert tp_mod._direction_sign("long") == 1.0
        assert tp_mod._direction_sign("Short") == -1.0

    def test_unknown(self, tp_mod):
        assert tp_mod._direction_sign("UNKNOWN") == 0.0

    def test_empty(self, tp_mod):
        assert tp_mod._direction_sign("") == 0.0

    def test_none(self, tp_mod):
        assert tp_mod._direction_sign(None) == 0.0


# ── Tests: 端到端集成 (DB% → load/100 → analyze) ──

class TestEndToEndPipeline:
    """验证 DB 存储百分比 → load 除以 100 → analyze 正确触发。"""

    @pytest.fixture
    def realistic_db(self):
        """创建含真实百分比 mfe_pct 值的临时 DB。

        DB 中 mfe_pct 为百分比: 5.0 = 5%, 0.1 = 0.1%, 2.5 = 2.5%.
        load_closed_playbook_sim_trades 会将其转换为小数。
        """
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE trade_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT, symbol TEXT, direction TEXT,
                entry_time TEXT, exit_time TEXT,
                entry_price REAL, exit_price REAL, quantity INTEGER,
                realized_pnl REAL, pnl_net REAL, fees_est REAL,
                mfe_pct REAL, trade_mode TEXT DEFAULT 'simulate',
                source_type TEXT DEFAULT 'PLAYBOOK_SIM'
            )
        """)
        # DB 百分比值: 5.0%=5%, 0.1%=0.1%, 2.5%=2.5%, 1.5%=1.5%, 4.0%=4%
        realistic_trades = [
            ("2026-09-24", "AAPL", "LONG", 100.0, 10, 30.0, 2.0, 5.0),
            ("2026-09-24", "TSLA", "SHORT", 200.0, 5, 20.0, 1.5, 3.0),
            ("2026-09-24", "NVDA", "LONG", 300.0, 3, -15.0, 1.0, 0.1),
            ("2026-09-24", "MSFT", "LONG", 150.0, 8, 5.0, 2.0, 2.5),
            ("2026-09-24", "META", "SHORT", 250.0, 4, -10.0, 1.5, 4.0),
        ]
        for t in realistic_trades:
            conn.execute(
                """INSERT INTO trade_records
                   (trade_date, symbol, direction, entry_time, exit_time,
                    entry_price, quantity, realized_pnl, pnl_net, fees_est,
                    mfe_pct, trade_mode, source_type)
                   VALUES (?, ?, ?, '2026-09-24T09:30:00', '2026-09-24T15:00:00',
                           ?, ?, ?, ?, ?, ?, 'simulate', 'PLAYBOOK_SIM')""",
                (t[0], t[1], t[2], t[3], t[4],
                 t[5] + t[6], t[5], t[6], t[7])
            )
        conn.commit()
        conn.close()
        yield path
        os.unlink(path)

    def test_db_5pct_triggers_at_5pct_threshold(self, realistic_db, tp_mod):
        """DB mfe_pct=5.0 (5%) → 转换后 0.05 → 应在 0.05 阈值触发。"""
        trades = tp_mod.load_closed_playbook_sim_trades(realistic_db)
        # AAPL: mfe_pct=5.0 in DB → 0.05 after load
        aapl = [t for t in trades if t["symbol"] == "AAPL"][0]
        assert abs(aapl["mfe_pct"] - 0.05) < 1e-6

        report = tp_mod.analyze_trades([aapl], [0.05])
        assert report["thresholds"]["0.05"]["n_triggered"] == 1

    def test_db_0_1pct_no_trigger(self, realistic_db, tp_mod):
        """DB mfe_pct=0.1 (0.1%) → 转换后 0.001 → 不应在任何阈值触发。"""
        trades = tp_mod.load_closed_playbook_sim_trades(realistic_db)
        nvda = [t for t in trades if t["symbol"] == "NVDA"][0]
        assert abs(nvda["mfe_pct"] - 0.001) < 1e-6

        report = tp_mod.analyze_trades([nvda], [0.01, 0.02, 0.05])
        for data in report["thresholds"].values():
            assert data["n_triggered"] == 0

    def test_db_2_5pct_triggers_at_2pct_not_3pct(self, realistic_db, tp_mod):
        """DB mfe_pct=2.5 (2.5%) → 转换后 0.025 → 在 0.02 触发, 在 0.03 不触发。"""
        trades = tp_mod.load_closed_playbook_sim_trades(realistic_db)
        msft = [t for t in trades if t["symbol"] == "MSFT"][0]
        assert abs(msft["mfe_pct"] - 0.025) < 1e-6

        report = tp_mod.analyze_trades([msft], [0.02, 0.03])
        assert report["thresholds"]["0.02"]["n_triggered"] == 1
        assert report["thresholds"]["0.03"]["n_triggered"] == 0

    def test_full_pipeline_trigger_counts(self, realistic_db, tp_mod):
        """端到端: 5 笔交易在 0.03 阈值的触发计数。"""
        trades = tp_mod.load_closed_playbook_sim_trades(realistic_db)
        report = tp_mod.analyze_trades(trades, [0.03])
        data = report["thresholds"]["0.03"]
        # 触发: AAPL(5%→0.05≥0.03✓), TSLA(3%→0.03≥0.03✓), META(4%→0.04≥0.03✓)
        # 不触发: NVDA(0.1%→0.001), MSFT(2.5%→0.025<0.03)
        assert data["n_triggered"] == 3
