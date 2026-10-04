# -*- coding: utf-8 -*-
"""批次0 三标签扫描器 + lessons 三闸测试 (DEB-20260904 §10.2, task #17).

覆盖: 三标签纯函数判定 / run_scan 端到端 (tmp DB) / lessons 迁移幂等 /
闸1 PENDING→ACTIVE (n≥2) / 幂等增量 (同日重跑不重复计数) / 闸3 RETIRE+复活 /
闸2 注入排序 legacy 等价 / PYTEST_CURRENT_TEST 生产库守卫 / memory_injector 缺列回退。

全部使用 tmp_path 合成 DB — 不触生产 data/ (conftest 生产写入隔离闸内自然通过;
本文件无需 allow_production_data_write 豁免)。
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from daily_violation_scan import (  # noqa: E402
    CLUSTER_MIN_CONCURRENT,
    apply_lesson_gates,
    label_chase,
    risk_usd,
    run_scan,
    scan_window,
)
from app.review.lesson_injector import (  # noqa: E402
    LessonInjector,
    ensure_lessons_lifecycle_columns,
)
from utils.et_time import ET  # noqa: E402


# ─────────────────────────── 合成数据工具 ───────────────────────────

def _mkdb(tmp_path: Path) -> Path:
    """最小 schema 合成库: trade_records + playbook_instructions + lessons(经注入器)。"""
    db = tmp_path / "synthetic.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY, trade_date TEXT, symbol TEXT, direction TEXT,
            entry_time TEXT, exit_time TEXT, entry_price REAL, quantity INTEGER,
            pnl_net REAL, realized_pnl REAL, decision_id TEXT DEFAULT '',
            data_quality TEXT DEFAULT 'real')
    """)
    conn.execute("""
        CREATE TABLE playbook_instructions (
            id INTEGER PRIMARY KEY, decision_id TEXT, atr_pct REAL,
            raw_instruction_json TEXT)
    """)
    conn.commit()
    conn.close()
    LessonInjector(db_path=str(db))  # lessons 表 + 4 列幂等迁移 (SSOT)
    return db


def _add_trade(db, tid, day, symbol="US.TEST", direction="LONG", entry="10:00:00",
               exit_="11:00:00", price=100.0, qty=10, pnl_net=-5.0,
               decision_id="", data_quality="real"):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO trade_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, day, symbol, direction, f"{day}T{entry}-04:00",
         f"{day}T{exit_}-04:00" if exit_ else None,
         price, qty, pnl_net, pnl_net, decision_id, data_quality))
    conn.commit()
    conn.close()


def _add_pi(db, pid, decision_id, atr_pct=None, cap=None):
    raw = json.dumps({"chase_cap_price": cap})
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO playbook_instructions VALUES (?,?,?,?)",
                 (pid, decision_id, atr_pct, raw))
    conn.commit()
    conn.close()


def _lessons_rows(db, source_type="violation_scan"):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM lessons WHERE source_type=? ORDER BY id", (source_type,))]
    conn.close()
    return rows


# ─────────────────────────── 标签纯函数 ───────────────────────────

class TestChaseLabel:
    def test_long_above_cap_violates(self):
        assert label_chase({"chase_cap": 225.0, "entry_price": 226.96,
                            "direction": "LONG"}) == "violation"

    def test_short_below_cap_violates(self):
        # 9/3 实测锚点: AVGO SHORT 346 < cap 355 (glm 复现口径, 字面 fill>cap 会漏)
        assert label_chase({"chase_cap": 355.0, "entry_price": 346.0,
                            "direction": "SHORT"}) == "violation"

    def test_direction_aware_non_violations(self):
        # LONG 低于 cap / SHORT 高于 cap = 带内成交, 不违例
        assert label_chase({"chase_cap": 960.0, "entry_price": 937.6,
                            "direction": "LONG"}) == "ok"
        assert label_chase({"chase_cap": 150.0, "entry_price": 160.0,
                            "direction": "SHORT"}) == "ok"

    def test_missing_cap_records_null_not_violation(self):
        # 无 cap 记 null 不编造 (UE 链 9/4 前无字段)
        assert label_chase({"chase_cap": None, "entry_price": 100.0,
                            "direction": "LONG"}) == "no_cap"

    def test_missing_price_undecidable(self):
        assert label_chase({"chase_cap": 100.0, "entry_price": None,
                            "direction": "LONG"}) is None


class TestOversizedRisk:
    def test_risk_formula(self):
        # qty × 1.5 × atr_pct(小数) × entry_price
        t = {"atr_pct": 0.03, "quantity": 100, "entry_price": 50.0}
        assert risk_usd(t) == pytest.approx(100 * 1.5 * 0.03 * 50.0)

    def test_atr_missing_returns_none(self):
        assert risk_usd({"atr_pct": None, "quantity": 10, "entry_price": 100.0}) is None
        assert risk_usd({"atr_pct": 0.0, "quantity": 10, "entry_price": 100.0}) is None


class TestClusterLabel:
    def test_concurrent_at_entry_ge_4_flags(self):
        # 4 笔同向 LONG 区间重叠, 第 4 笔开仓时刻并发=4 (含自身) → 打标
        trades = []
        for i in range(4):
            trades.append({
                "id": i, "trade_date": "2026-09-01", "symbol": f"US.S{i}",
                "direction": "LONG", "quantity": 10, "entry_price": 100.0,
                "pnl_net": -1.0, "realized_pnl": -1.0, "atr_pct": None,
                "chase_cap": None, "decision_id": "",
                "entry_time": f"2026-09-01T09:{30 + i}:00-04:00",
                "exit_time": "2026-09-01T15:00:00-04:00",
            })
        res = scan_window(trades, days=30)
        flagged = res["2026-09-01"]["CLUSTER_BET"]["violation"]
        assert len(flagged) == 1  # 仅第 4 笔达到并发≥4
        assert flagged[0]["concurrent_at_entry"] == CLUSTER_MIN_CONCURRENT

    def test_opposite_direction_not_counted(self):
        trades = []
        for i, d in enumerate(["LONG", "LONG", "LONG", "SHORT"]):
            trades.append({
                "id": i, "trade_date": "2026-09-01", "symbol": f"US.S{i}",
                "direction": d, "quantity": 10, "entry_price": 100.0,
                "pnl_net": None, "realized_pnl": None, "atr_pct": None,
                "chase_cap": None, "decision_id": "",
                "entry_time": f"2026-09-01T09:{30 + i}:00-04:00",
                "exit_time": "2026-09-01T15:00:00-04:00",
            })
        res = scan_window(trades, days=30)
        assert res["2026-09-01"]["CLUSTER_BET"]["violation"] == []


# ─────────────────────────── 端到端 + 三闸 ───────────────────────────

class TestRunScanAndGates:
    def test_end_to_end_labels_and_promotion(self, tmp_path):
        db = _mkdb(tmp_path)
        # 相对日期 (本文件既有约定, 见 test_retire_after_30d_and_revival 的 now-45d):
        #   闸3 RETIRE_AFTER_DAYS=30 用 run_scan 内的 now_et=datetime.now(ET) 判 age,
        #   硬编码日期会随日历增长把本用例 (测闸1 promote) 拖进闸3 retire 而翻红 —
        #   2026-10-04 实测: 原 "2026-09-03" 起 age=31 > 30 ⇒ status_after=RETIRED。
        #   retire 语义由 test_retire_after_30d_and_revival 专测, 本用例只需 age 恒 < 30。
        day = (datetime.now(ET) - timedelta(days=2)).strftime("%Y-%m-%d")
        # NVDA LONG 226.96 > cap 225 (违例) + UBER LONG 78.09 > cap 78 (违例)
        _add_trade(db, 1, day, "US.NVDA", "LONG", price=226.96,
                   pnl_net=-10.0, decision_id="DA-1")
        _add_trade(db, 2, day, "US.UBER", "LONG", price=78.09,
                   pnl_net=5.0, decision_id="DA-2")
        _add_pi(db, 1, "DA-1", atr_pct=0.03, cap=225.0)
        _add_pi(db, 2, "DA-2", atr_pct=0.03, cap=78.0)
        rep = run_scan(db_path=str(db), days=30, write=True)
        assert rep["label_totals_window"]["CHASE_ENTRY"] == 2
        gates = rep["lesson_gates"]["CHASE_ENTRY"]
        assert gates["created"] and gates["promoted"]
        assert gates["status_after"] == "ACTIVE"
        assert gates["recurrence_count"] == 2
        # 带符号净烧钱: -(-10) + -(5) = +10 - 5 = 5.0
        assert gates["cost_since"] == pytest.approx(5.0)
        rows = _lessons_rows(db)
        assert len(rows) == 1 and rows[0]["status"] == "ACTIVE"

    def test_single_day_stays_pending(self, tmp_path):
        db = _mkdb(tmp_path)
        _add_trade(db, 1, "2026-09-03", "US.NVDA", "LONG", price=226.96,
                   decision_id="DA-1")
        _add_pi(db, 1, "DA-1", cap=225.0)
        rep = run_scan(db_path=str(db), days=30, write=True)
        g = rep["lesson_gates"]["CHASE_ENTRY"]
        assert g["status_after"] == "PENDING" and not g["promoted"]
        assert g["recurrence_count"] == 1

    def test_idempotent_same_day_rerun(self, tmp_path):
        db = _mkdb(tmp_path)
        _add_trade(db, 1, "2026-09-03", "US.NVDA", "LONG", price=226.96,
                   decision_id="DA-1")
        _add_trade(db, 2, "2026-09-03", "US.UBER", "LONG", price=78.09,
                   decision_id="DA-2")
        _add_pi(db, 1, "DA-1", cap=225.0)
        _add_pi(db, 2, "DA-2", cap=78.0)
        r1 = run_scan(db_path=str(db), days=30, write=True)
        r2 = run_scan(db_path=str(db), days=30, write=True)
        assert r1["lesson_gates"]["CHASE_ENTRY"]["added_recurrence"] == 2
        assert r2["lesson_gates"]["CHASE_ENTRY"]["added_recurrence"] == 0
        rows = _lessons_rows(db)
        assert rows[0]["recurrence_count"] == 2  # 未翻倍

    def test_duplicate_decision_id_join_no_fanout(self, tmp_path):
        # 库内实测 5 个重复 decision_id → JOIN 取 MAX(id), 行数不得放大
        db = _mkdb(tmp_path)
        _add_trade(db, 1, "2026-09-03", "US.NVDA", "LONG", price=226.96,
                   decision_id="DA-DUP")
        _add_pi(db, 1, "DA-DUP", cap=999.0)   # 旧行: 不会违例的 cap
        _add_pi(db, 2, "DA-DUP", cap=225.0)   # MAX(id) 行: 真 cap
        rep = run_scan(db_path=str(db), days=30, write=False)
        assert rep["daily"]["2026-09-03"]["n_trades"] == 1
        assert rep["label_totals_window"]["CHASE_ENTRY"] == 1

    def test_data_quality_filter(self, tmp_path):
        db = _mkdb(tmp_path)
        # synthetic 违例笔不得计入; 同库放一笔 real 无违例笔保证窗口非空
        _add_trade(db, 1, "2026-09-03", "US.NVDA", "LONG", price=226.96,
                   decision_id="DA-1", data_quality="synthetic")
        _add_trade(db, 2, "2026-09-03", "US.MU", "LONG", price=937.0,
                   decision_id="DA-2", data_quality="real")
        _add_pi(db, 1, "DA-1", cap=225.0)
        _add_pi(db, 2, "DA-2", cap=960.0)
        rep = run_scan(db_path=str(db), days=30, write=False)
        assert rep["daily"]["2026-09-03"]["n_trades"] == 1  # 仅 real 笔
        assert rep["label_totals_window"]["CHASE_ENTRY"] == 0

    def test_retire_after_30d_and_revival(self, tmp_path):
        db = _mkdb(tmp_path)
        old_day = (datetime.now(ET) - timedelta(days=45)).strftime("%Y-%m-%d")
        conn = sqlite3.connect(str(db))
        conn.execute(
            """INSERT INTO lessons (lesson_text, lesson_category, lesson_subcategory,
               source_type, status, recurrence_count, last_triggered_at, cost_since)
               VALUES ('t','violation','CHASE_ENTRY','violation_scan','ACTIVE',
                       5, ?, 100.0)""",
            (f"{old_day}T23:59:59",))
        conn.commit()
        conn.close()
        # 无新违例 → 闸3 退休
        gates = apply_lesson_gates(str(db), {}, datetime.now(ET))
        assert gates["CHASE_ENTRY"]["retired"]
        assert gates["CHASE_ENTRY"]["status_after"] == "RETIRED"
        # 退休后新违例日 → 复活为新生命周期 (计数重置)
        today = datetime.now(ET).strftime("%Y-%m-%d")
        day_results = {today: {
            "CHASE_ENTRY": {"violation": [{"pnl_net": -20.0}]},
            "OVERSIZED_RISK": {"violation": []},
            "CLUSTER_BET": {"violation": []}}}
        gates2 = apply_lesson_gates(str(db), day_results, datetime.now(ET))
        assert gates2["CHASE_ENTRY"]["revived"]
        row = _lessons_rows(db)[0]
        assert row["recurrence_count"] == 1 and row["cost_since"] == pytest.approx(20.0)
        assert row["status"] == "PENDING"  # n=1 < 2 → 重新走闸1

    def test_legacy_rows_untouched(self, tmp_path):
        db = _mkdb(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.execute(
            """INSERT INTO lessons (lesson_text, lesson_category, lesson_subcategory,
               source_type, status, impact_score) VALUES
               ('legacy1','signal','high_signal_loss','auto','active',50.0)""")
        conn.commit()
        conn.close()
        day_results = {"2026-09-03": {
            "CHASE_ENTRY": {"violation": [{"pnl_net": -1.0}]},
            "OVERSIZED_RISK": {"violation": []},
            "CLUSTER_BET": {"violation": []}}}
        apply_lesson_gates(str(db), day_results, datetime.now(ET))
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        legacy = dict(conn.execute(
            "SELECT * FROM lessons WHERE source_type='auto'").fetchone())
        conn.close()
        assert legacy["status"] == "active"
        assert (legacy["recurrence_count"] or 0) == 0
        assert legacy["last_triggered_at"] in ("", None)

    def test_migration_idempotent(self, tmp_path):
        db = _mkdb(tmp_path)
        conn = sqlite3.connect(str(db))
        added1 = ensure_lessons_lifecycle_columns(conn)
        added2 = ensure_lessons_lifecycle_columns(conn)
        conn.commit()
        conn.close()
        assert added1 == [] and added2 == []  # _mkdb 已迁移, 重跑零新增

    def test_pytest_guard_rejects_prod_db(self):
        # pytest 运行时 PYTEST_CURRENT_TEST 天然已设 → 指向生产 data/ 必须拒写
        assert os.environ.get("PYTEST_CURRENT_TEST")
        prod = str(ROOT / "data" / "ftseek_us.db")
        with pytest.raises(RuntimeError, match="PYTEST_CURRENT_TEST"):
            apply_lesson_gates(prod, {}, datetime.now(ET))


# ─────────────────────────── 闸2 注入排序等价 ───────────────────────────

class TestInjectorRanking:
    def test_legacy_order_preserved_and_violation_ranked(self, tmp_path):
        db = _mkdb(tmp_path)
        conn = sqlite3.connect(str(db))
        for i, (txt, imp) in enumerate([("A", 50.0), ("B", 70.0), ("C", 30.0)]):
            conn.execute(
                """INSERT INTO lessons (lesson_text, lesson_category, lesson_subcategory,
                   source_type, status, impact_score, expiry_date)
                   VALUES (?, 'signal', 'high_signal_loss', 'auto', 'active', ?, '')""",
                (txt, imp))
        # 正 rank 违例教训 (ACTIVE) + 负 rank 违例教训 (复发但盈利 → 沉底)
        conn.execute(
            """INSERT INTO lessons (lesson_text, lesson_category, lesson_subcategory,
               source_type, status, impact_score, expiry_date,
               recurrence_count, cost_since)
               VALUES ('OVER', 'violation', 'OVERSIZED_RISK', 'violation_scan',
                       'ACTIVE', 0.0, '', 9, 200.0)""")
        conn.execute(
            """INSERT INTO lessons (lesson_text, lesson_category, lesson_subcategory,
               source_type, status, impact_score, expiry_date,
               recurrence_count, cost_since)
               VALUES ('CHASE', 'violation', 'CHASE_ENTRY', 'violation_scan',
                       'ACTIVE', 0.0, '', 9, -10.0)""")
        # PENDING/RETIRED 不得注入
        conn.execute(
            """INSERT INTO lessons (lesson_text, lesson_category, lesson_subcategory,
               source_type, status, expiry_date, recurrence_count, cost_since)
               VALUES ('PEND', 'violation', 'CLUSTER_BET', 'violation_scan',
                       'PENDING', '', 1, 50.0)""")
        conn.commit()
        conn.close()

        lessons = LessonInjector(db_path=str(db)).load_active_lessons()
        texts = [l["lesson_text"] for l in lessons]
        # ABS 排序 (2026-09-05 修正): OVER 9×|200|=1800 > CHASE 9×|-10|=90 > legacy 0;
        # 负成本违例教训不再沉底, PENDING 仍不注入
        assert texts == ["OVER", "CHASE", "B", "A", "C"]
        assert "PEND" not in texts

    def test_constraints_no_hard_gate_from_violation_lessons(self, tmp_path):
        db = _mkdb(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.execute(
            """INSERT INTO lessons (lesson_text, lesson_category, lesson_subcategory,
               source_type, status, expiry_date, recurrence_count, cost_since)
               VALUES ('OVER', 'violation', 'OVERSIZED_RISK', 'violation_scan',
                       'ACTIVE', '', 9, 200.0)""")
        conn.commit()
        conn.close()
        inj = LessonInjector(db_path=str(db))
        c = inj.generate_trade_constraints(symbol="US.NVDA", session="morning")
        # 铁律: 不自动晋升任何教训为硬门
        assert c["allow"] is True
        assert c["extra_confidence"] == 0.0
        assert c["max_position_pct"] == 1.0

    def test_memory_injector_sign_aware_display(self, tmp_path):
        # 符号裁决实证 (2026-09-05): cost_since=−Σpnl_net → 正=烧钱, 负=违例净赚
        db = _mkdb(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.execute(
            """INSERT INTO lessons (lesson_text, lesson_category, lesson_subcategory,
               source_type, status, expiry_date, recurrence_count, cost_since)
               VALUES ('LOSS', 'violation', 'OVERSIZED_RISK', 'violation_scan',
                       'ACTIVE', '', 9, 220.34)""")
        conn.execute(
            """INSERT INTO lessons (lesson_text, lesson_category, lesson_subcategory,
               source_type, status, expiry_date, recurrence_count, cost_since)
               VALUES ('PROFIT', 'violation', 'CHASE_ENTRY', 'violation_scan',
                       'ACTIVE', '', 9, -9.14)""")
        conn.commit()
        conn.close()
        from app.agents.debate.memory_injector import MemoryInjector
        txt = MemoryInjector(db_path=str(db))._load_lessons()
        assert "烧$220" in txt          # 正 cost = 真烧钱
        assert "违例笔净赚$9" in txt     # 负 cost = 矛盾信号, 不得显示"烧"
        assert "烧$-" not in txt and "烧$9]" not in txt.replace("违例笔净赚$9", "")

    def test_memory_injector_fallback_on_legacy_schema(self, tmp_path):
        # 旧 schema (无新列) → _load_lessons 必须回退 legacy 查询而非报错
        db = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db))
        conn.execute("""
            CREATE TABLE lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT, lesson_text TEXT NOT NULL,
                lesson_category TEXT NOT NULL, lesson_subcategory TEXT NOT NULL,
                impact_score REAL DEFAULT 0.0, confidence REAL DEFAULT 0.5,
                symbols TEXT DEFAULT '[]', source_type TEXT DEFAULT 'auto',
                status TEXT DEFAULT 'active', expiry_date TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                evidence TEXT DEFAULT '{}', validation_count INTEGER DEFAULT 0)
        """)
        conn.execute(
            "INSERT INTO lessons (lesson_text, lesson_category, lesson_subcategory, status) "
            "VALUES ('old', 'signal', 'x', 'active')")
        conn.commit()
        conn.close()
        from app.agents.debate.memory_injector import MemoryInjector
        mi = MemoryInjector(db_path=str(db))
        txt = mi._load_lessons()
        assert "old" in txt and "加载失败" not in txt
        assert mi._count_lessons() == 1
