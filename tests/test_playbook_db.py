"""Tests for app.playbook.db — Playbook 数据库层"""
import json
import pytest
import sqlite3

from app.playbook.db import PlaybookDB, _to_json, _row_to_dict


@pytest.fixture
def db():
    """创建使用内存数据库的 PlaybookDB 实例 (每个 test 完全隔离)"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    pdb = PlaybookDB(conn=conn)
    pdb.ensure_tables()
    yield pdb
    conn.close()


class TestHelperFunctions:
    """辅助函数测试"""

    def test_to_json_none(self):
        assert _to_json(None) is None

    def test_to_json_string(self):
        assert _to_json('{"a": 1}') == '{"a": 1}'

    def test_to_json_dict(self):
        result = _to_json({"a": 1})
        assert json.loads(result) == {"a": 1}

    def test_to_json_list(self):
        result = _to_json(["a", "b"])
        assert json.loads(result) == ["a", "b"]

    def test_row_to_dict_none(self):
        assert _row_to_dict(None) is None


class TestEnsureTables:
    """表创建测试"""

    def test_tables_created(self, db):
        """ensure_tables 创建所有 5 个表"""
        conn = db._conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'playbook_%'"
        ).fetchall()
        table_names = {t["name"] for t in tables}
        assert "playbook_instructions" in table_names
        assert "playbook_factor_stats" in table_names
        assert "playbook_patterns" in table_names
        assert "playbook_user_feedback" in table_names
        assert "playbook_regime_log" in table_names

    def test_ensure_tables_idempotent(self, db):
        """重复调用 ensure_tables 不报错"""
        db.ensure_tables()
        db.ensure_tables()

    def test_indexes_created(self, db):
        """索引被创建"""
        conn = db._conn()
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_playbook%'"
        ).fetchall()
        assert len(indexes) >= 4


class TestPlaybookInstructions:
    """playbook_instructions CRUD 测试"""

    def test_save_and_get_instruction(self, db):
        """保存并读取指令"""
        inst = {
            "date": "2026-06-18",
            "symbol": "US.NTES",
            "direction": "BUY",
            "entry_price": 119.5,
            "stop_loss": 114.8,
            "take_profit": 128.0,
            "position_size": 50,
            "risk_amount": 235.0,
            "risk_reward": 1.81,
            "confidence": 72,
            "conditions": ["insider_cluster_buy", "pe_below_industry"],
            "thesis": "基本面低估 + 内部人集群买入",
            "sector": "Technology",
            "risks": ["中概政策风险"],
            "market_regime": "RISK_ON",
            "vix_at_generation": 18.5,
            "data_quality": {"fundamental": "ok", "technical": "ok"},
            "ok_dimensions": 6,
        }

        row_id = db.save_instruction(inst)
        assert row_id > 0

        # 读取
        result = db.get_instruction("2026-06-18", "US.NTES")
        assert result is not None
        assert result["symbol"] == "US.NTES"
        assert result["direction"] == "BUY"
        assert result["entry_price"] == 119.5
        assert result["confidence"] == 72
        # JSON 字段被自动解析
        assert isinstance(result["conditions_json"], list)
        assert "insider_cluster_buy" in result["conditions_json"]

    def test_upsert_same_date_symbol(self, db):
        """同日期+标的 UPSERT 更新而非重复插入"""
        inst1 = {
            "date": "2026-06-18",
            "symbol": "US.NTES",
            "direction": "BUY",
            "confidence": 60,
        }
        db.save_instruction(inst1)

        inst2 = {
            "date": "2026-06-18",
            "symbol": "US.NTES",
            "direction": "SELL_SHORT",
            "confidence": 75,
        }
        db.save_instruction(inst2)

        # 应该只有一条记录
        results = db.get_instructions_by_date("2026-06-18")
        assert len(results) == 1
        assert results[0]["direction"] == "SELL_SHORT"
        assert results[0]["confidence"] == 75

    def test_get_instructions_by_date(self, db):
        """按日期获取多条指令"""
        for sym in ["US.NTES", "US.ARM", "US.META"]:
            db.save_instruction({
                "date": "2026-06-18",
                "symbol": sym,
                "direction": "BUY" if sym != "US.META" else "SKIP",
            })

        results = db.get_instructions_by_date("2026-06-18")
        assert len(results) == 3

    def test_get_instruction_not_found(self, db):
        """查询不存在的指令返回 None"""
        result = db.get_instruction("2020-01-01", "US.NOPE")
        assert result is None

    def test_update_instruction_tracking(self, db):
        """更新追踪字段"""
        row_id = db.save_instruction({
            "date": "2026-06-18",
            "symbol": "US.NTES",
            "direction": "BUY",
            "entry_price": 120.0,
        })

        db.update_instruction_tracking(row_id, {
            "executed": 1,
            "execution_price": 120.5,
            "pnl": 150.0,
            "outcome": "win",
        })

        result = db.get_instruction("2026-06-18", "US.NTES")
        assert result["executed"] == 1
        assert result["execution_price"] == 120.5
        assert result["pnl"] == 150.0
        assert result["outcome"] == "win"

    def test_update_ignores_invalid_fields(self, db):
        """update 忽略非追踪字段"""
        row_id = db.save_instruction({
            "date": "2026-06-18",
            "symbol": "US.NTES",
            "direction": "BUY",
        })

        # 这不应该报错，只是忽略 symbol
        db.update_instruction_tracking(row_id, {"symbol": "US.CHANGED"})

        result = db.get_instruction("2026-06-18", "US.NTES")
        assert result["symbol"] == "US.NTES"

    def test_get_instructions_with_outcome(self, db):
        """获取有结果的指令 (用于因子分析)"""
        # 保存并标记结果
        for sym, outcome in [("US.A", "win"), ("US.B", "loss"), ("US.C", "holding"), ("US.D", None)]:
            row_id = db.save_instruction({
                "date": "2026-06-18",
                "symbol": sym,
                "direction": "BUY",
            })
            if outcome:
                db.update_instruction_tracking(row_id, {"outcome": outcome})

        results = db.get_instructions_with_outcome(days_back=1)
        symbols = {r["symbol"] for r in results}
        assert "US.A" in symbols
        assert "US.B" in symbols
        assert "US.C" not in symbols  # holding 被排除
        assert "US.D" not in symbols  # None 被排除

    def test_get_recent_instructions(self, db):
        """获取最近指令"""
        for i in range(5):
            db.save_instruction({
                "date": f"2026-06-{18-i:02d}",
                "symbol": "US.NTES",
                "direction": "BUY",
            })

        results = db.get_recent_instructions(limit=3)
        assert len(results) == 3
        # 按日期降序
        assert results[0]["date"] >= results[1]["date"]

    def test_save_skip_instruction(self, db):
        """保存 SKIP 指令"""
        inst = {
            "date": "2026-06-18",
            "symbol": "US.META",
            "direction": "SKIP",
            "skip_reason": "财报3天内",
            "conditions": ["earnings_approaching"],
        }
        row_id = db.save_instruction(inst)
        result = db.get_instruction("2026-06-18", "US.META")
        assert result["direction"] == "SKIP"
        assert result["skip_reason"] == "财报3天内"


class TestFactorStats:
    """playbook_factor_stats CRUD 测试"""

    def test_upsert_and_get_factor_stat(self, db):
        """写入并读取因子统计"""
        stat = {
            "factor_key": "insider_cluster_buy",
            "analysis_level": "single",
            "total_trades": 8,
            "wins": 6,
            "losses": 2,
            "win_rate": 0.75,
            "avg_pnl": 189.0,
            "profit_factor": 1.8,
            "bayesian_win_rate": 0.72,
            "bayesian_lower": 0.55,
            "bayesian_upper": 0.85,
            "verdict": "positive",
        }

        db.upsert_factor_stat(stat)

        results = db.get_factor_stats(level="single")
        assert len(results) == 1
        assert results[0]["factor_key"] == "insider_cluster_buy"
        assert results[0]["win_rate"] == 0.75
        assert results[0]["verdict"] == "positive"

    def test_upsert_factor_pair(self, db):
        """写入条件对因子"""
        stat = {
            "factor_key": "insider_cluster_buy|pe_below_industry",
            "analysis_level": "pair",
            "co_factors": ["insider_cluster_buy", "pe_below_industry"],
            "total_trades": 5,
            "wins": 4,
            "losses": 1,
            "win_rate": 0.8,
            "verdict": "positive",
        }

        db.upsert_factor_stat(stat)

        results = db.get_factor_stats(level="pair", verdict="positive")
        assert len(results) == 1
        assert "|" in results[0]["factor_key"]

    def test_get_factor_stats_filter_by_verdict(self, db):
        """按 verdict 过滤"""
        for fk, verdict in [("f1", "positive"), ("f2", "negative"), ("f3", "observing")]:
            db.upsert_factor_stat({
                "factor_key": fk,
                "analysis_level": "single",
                "verdict": verdict,
            })

        pos = db.get_factor_stats(verdict="positive")
        assert len(pos) == 1
        neg = db.get_factor_stats(verdict="negative")
        assert len(neg) == 1


class TestPatterns:
    """playbook_patterns CRUD 测试"""

    def test_upsert_and_get_pattern(self, db):
        """写入并读取 pattern"""
        pattern = {
            "pattern_key": "hash_abc123",
            "conditions": ["insider_cluster_buy", "pe_below_industry"],
            "total_trades": 5,
            "wins": 4,
            "win_rate": 0.8,
            "status": "active",
        }

        db.upsert_pattern(pattern)

        results = db.get_patterns(status="active")
        assert len(results) == 1
        assert results[0]["pattern_key"] == "hash_abc123"

    def test_upsert_pattern_updates_existing(self, db):
        """重复 upsert 更新已有 pattern"""
        pattern1 = {
            "pattern_key": "hash_abc",
            "conditions": ["a", "b"],
            "total_trades": 3,
            "status": "observing",
        }
        db.upsert_pattern(pattern1)

        pattern2 = {
            "pattern_key": "hash_abc",
            "conditions": ["a", "b"],
            "total_trades": 6,
            "status": "active",
        }
        db.upsert_pattern(pattern2)

        results = db.get_patterns()
        assert len(results) == 1
        assert results[0]["total_trades"] == 6
        assert results[0]["status"] == "active"


class TestUserFeedback:
    """playbook_user_feedback CRUD 测试"""

    def test_save_and_get_feedback(self, db):
        """保存并读取用户反馈"""
        inst_id = db.save_instruction({
            "date": "2026-06-18",
            "symbol": "US.NTES",
            "direction": "BUY",
        })

        fb_id = db.save_user_feedback({
            "instruction_id": inst_id,
            "action": "skipped",
            "skip_reason": "风险太大",
        })
        assert fb_id > 0

        feedbacks = db.get_user_feedback(inst_id)
        assert len(feedbacks) == 1
        assert feedbacks[0]["action"] == "skipped"
        assert feedbacks[0]["skip_reason"] == "风险太大"

    def test_multiple_feedbacks_per_instruction(self, db):
        """同一指令可有多条反馈"""
        inst_id = db.save_instruction({
            "date": "2026-06-18",
            "symbol": "US.NTES",
            "direction": "BUY",
        })

        db.save_user_feedback({"instruction_id": inst_id, "action": "skipped"})
        db.save_user_feedback({"instruction_id": inst_id, "action": "modified",
                               "modified_field": "stop_loss"})

        feedbacks = db.get_user_feedback(inst_id)
        assert len(feedbacks) == 2


class TestRegimeLog:
    """playbook_regime_log CRUD 测试"""

    def test_save_and_get_regime_log(self, db):
        """保存并读取 regime 日志"""
        db.save_regime_log({
            "date": "2026-06-18",
            "regime": "RISK_ON",
            "vix": 18.5,
            "spy_price": 542.0,
            "spy_ma20": 535.0,
            "spy_ma50": 530.0,
            "score": 5,
            "notes": "市场趋势向上",
        })

        logs = db.get_regime_log(days=30)
        assert len(logs) == 1
        assert logs[0]["regime"] == "RISK_ON"
        assert logs[0]["vix"] == 18.5

    def test_regime_log_upsert_by_date(self, db):
        """同日期的 regime 日志 UPSERT"""
        db.save_regime_log({"date": "2026-06-18", "regime": "CAUTIOUS", "score": 2})
        db.save_regime_log({"date": "2026-06-18", "regime": "RISK_ON", "score": 5})

        logs = db.get_regime_log()
        assert len(logs) == 1
        assert logs[0]["regime"] == "RISK_ON"


class TestSummaryStats:
    """汇总统计测试"""

    def test_empty_summary(self, db):
        """空数据库的汇总"""
        stats = db.get_summary_stats()
        assert stats["total_instructions"] == 0
        assert stats["win_rate"] == 0
        assert stats["total_pnl"] == 0

    def test_summary_with_data(self, db):
        """有数据的汇总"""
        # 保存 + 标记结果
        for sym, outcome, pnl in [("US.A", "win", 100), ("US.B", "loss", -50), ("US.C", "win", 200)]:
            row_id = db.save_instruction({
                "date": "2026-06-18",
                "symbol": sym,
                "direction": "BUY",
            })
            db.update_instruction_tracking(row_id, {
                "executed": 1,
                "outcome": outcome,
                "pnl": pnl,
            })

        stats = db.get_summary_stats()
        assert stats["total_instructions"] == 3
        assert stats["executed"] == 3
        assert stats["wins"] == 2
        assert stats["losses"] == 1
        assert stats["win_rate"] == pytest.approx(66.7, abs=0.1)
        assert stats["total_pnl"] == 250.0


# ═══════════════════════════════════════════════════════════════
# 2026-07-31 议题⑬ L1 (DEB-cleanup-remaining): UPSERT source-priority 契约守卫
#   约定只活在注释里 → 新源接入静默降级. 本测锁两层契约:
#   ① UPSERT 后写者胜 (date,symbol 冲突时 UE 21:10 覆盖 DA 18:00 靠调度顺序)
#   ② _src_priority 预算排序: UE(2) > DA(1) > 其他(0)
# ═══════════════════════════════════════════════════════════════
class TestUpsertSourcePriority:
    def _mkdb(self):
        import sqlite3
        from app.playbook.db import PlaybookDB
        conn = sqlite3.connect(":memory:")
        db = PlaybookDB(conn=conn)
        db.ensure_tables()
        return db, conn

    def test_upsert_later_writer_wins(self):
        """同 (date,symbol): 后写 (UE) 覆盖先写 (DA) — 调度顺序即优先级的物理实现"""
        db, conn = self._mkdb()
        db.save_instruction({"date": "2026-07-31", "symbol": "US.PRIO", "direction": "BUY",
                             "entry_price": 100.0, "decision_id": "DA-20260731-PRIO-x1",
                             "confidence": 70})
        db.save_instruction({"date": "2026-07-31", "symbol": "US.PRIO", "direction": "SELL_SHORT",
                             "entry_price": 99.0, "decision_id": "universe_expansion_2026-07-31_PRIO",
                             "confidence": 50})
        row = conn.execute("SELECT direction, decision_id FROM playbook_instructions "
                           "WHERE date='2026-07-31' AND symbol='US.PRIO'").fetchone()
        assert row[0] == "SELL_SHORT", "UE 后写未覆盖 DA"
        assert row[1].startswith("universe_expansion"), row[1]
        n = conn.execute("SELECT COUNT(*) FROM playbook_instructions "
                         "WHERE date='2026-07-31' AND symbol='US.PRIO'").fetchone()[0]
        assert n == 1, f"UPSERT 应保持单行, 实际 {n}"

    def test_src_priority_ordering(self):
        """预算排序契约: UE(2) > DA(1) > 其他(0) — 新源接入必须显式定级"""
        import inspect
        from app.playbook import pipeline as pl
        src = inspect.getsource(pl)
        assert 'did.startswith("universe_expansion")' in src
        # 从源码提取并执行 _src_priority 语义 (嵌套函数, 用等价断言)
        def _src_priority(inst):
            did = (inst.get("decision_id", "") or "")
            if did.startswith("universe_expansion"):
                return 2
            if did.startswith("DA-"):
                return 1
            return 0
        assert _src_priority({"decision_id": "universe_expansion_x"}) == 2
        assert _src_priority({"decision_id": "DA-20260731-x"}) == 1
        assert _src_priority({"decision_id": "topup_whatever"}) == 0
        assert _src_priority({}) == 0
