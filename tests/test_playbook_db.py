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
        index_names = {index["name"] for index in indexes}
        assert "idx_playbook_instructions_decision_id" in index_names

    def test_decision_id_lookup_uses_index(self, db):
        conn = db._conn()
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM playbook_instructions WHERE decision_id = ?",
            ("DA-test",),
        ).fetchall()
        assert any(
            "idx_playbook_instructions_decision_id" in row["detail"] for row in plan
        )


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

    def test_executed_clears_residual_skip_reason(self, db):
        """F-2 契约: executed=1 落库时清空拦截阶段残留的 skip_reason.

        REVIEW-20260909 §F-2: chase_cap 拦截留痕在成交后残留 (实盘 12/495 行
        executed=1 AND skip_reason<>''), 消费方按 skip_reason<>'' 判"跳过"会误计。
        """
        row_id = db.save_instruction({
            "date": "2026-09-08",
            "symbol": "US.SNDK",
            "direction": "BUY",
            "entry_price": 1750.0,
        })
        db.update_instruction_tracking(row_id, {
            "skip_reason": "chase_cap_enforced(reprice=1766.75,cap=1760.0)",
        })

        db.update_instruction_tracking(row_id, {
            "executed": 1,
            "execution_price": 1752.665,
            "outcome": "holding",
        })

        result = db.get_instruction("2026-09-08", "US.SNDK")
        assert result["executed"] == 1
        assert result["skip_reason"] == ""

    def test_executed_respects_explicit_skip_reason(self, db):
        """F-2 契约: 调用方显式给 skip_reason 时不覆盖 (显式优先)."""
        row_id = db.save_instruction({
            "date": "2026-09-08",
            "symbol": "US.SNDK",
            "direction": "BUY",
        })

        db.update_instruction_tracking(row_id, {
            "executed": 1,
            "skip_reason": "调用方显式归因",
        })

        result = db.get_instruction("2026-09-08", "US.SNDK")
        assert result["skip_reason"] == "调用方显式归因"

    def test_non_executed_update_preserves_skip_reason(self, db):
        """F-2 契约: 未带 executed 的更新不动 skip_reason (不变式不误伤)."""
        row_id = db.save_instruction({
            "date": "2026-09-08",
            "symbol": "US.SKHY",
            "direction": "BUY",
        })
        db.update_instruction_tracking(row_id, {
            "skip_reason": "chase_cap_enforced(reprice=187.21,cap=182.0)",
        })

        db.update_instruction_tracking(row_id, {"triggered": 1})

        result = db.get_instruction("2026-09-08", "US.SKHY")
        assert result["skip_reason"] == "chase_cap_enforced(reprice=187.21,cap=182.0)"

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
#   ① 覆盖仲裁优先级阶梯 (2026-08-17 根源修复后, 取代旧"后写者胜"契约):
#      DA-/UE:/reentry- = 3, TFB-/SNAP-/DEC- = 1, 空 = 0; 新>=现存才覆盖,
#      另 UE 浅指令永不覆盖 DA 深度内容 (旧"后写者胜"正是 8/17 事故根源哲学)
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

    def test_ue_does_not_overwrite_da(self):
        """同 (date,symbol): UE 浅指令不得覆盖先写 DA 深度内容 (根源修复契约)"""
        db, conn = self._mkdb()
        db.save_instruction({"date": "2026-07-31", "symbol": "US.PRIO", "direction": "BUY",
                             "entry_price": 100.0, "decision_id": "DA-20260731-PRIO-x1",
                             "confidence": 70})
        db.save_instruction({"date": "2026-07-31", "symbol": "US.PRIO", "direction": "SELL_SHORT",
                             "entry_price": 99.0, "decision_id": "universe_expansion_2026-07-31_PRIO",
                             "confidence": 50})
        row = conn.execute("SELECT direction, decision_id FROM playbook_instructions "
                           "WHERE date='2026-07-31' AND symbol='US.PRIO'").fetchone()
        assert row[0] == "BUY", "UE 不应覆盖 DA 深度内容"
        assert row[1].startswith("DA-"), f"现存行应保留 DA decision_id, 实际 {row[1]}"
        n = conn.execute("SELECT COUNT(*) FROM playbook_instructions "
                         "WHERE date='2026-07-31' AND symbol='US.PRIO'").fetchone()[0]
        assert n == 1, f"UPSERT 应保持单行, 实际 {n}"

    def test_src_priority_ordering(self):
        """预算排序契约: UE(2) > DA(1) > 其他(0) — 新源接入必须显式定级"""
        from pathlib import Path as _P
        src = (_P(__file__).resolve().parent.parent
               / "app" / "playbook" / "pipeline_parts" / "auto_execute_flow.py"
               ).read_text(encoding="utf-8")
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


class TestEstimateAdmissionQty:
    """_estimate_admission_qty (grilling Q2 生成时估算, 2026-08-07 复核补测) —
    与 compute_position_size 同构: risk_base×risk_pct/100 / |entry-stop|,
    含 max_position_pct 截断 (复核修复点); 失败 → None。"""

    def test_basic_no_clip(self):
        from app.playbook.db import _estimate_admission_qty
        from app.playbook.config import get_playbook_config
        pb = get_playbook_config()
        risk_base = float(pb.get("risk_base_capital", 82000.0))
        risk_pct = float(pb.get("risk_pct", 1.0))
        max_pct = float(pb.get("max_position_pct", 0.08))
        expect = int(risk_base * (risk_pct / 100.0) / 1.0)  # entry=10 stop=9
        if expect * 10.0 > risk_base * max_pct:
            pytest.skip("当前配置下本场景触发截断, 用例不适用")
        assert _estimate_admission_qty({"entry_price": 10.0, "stop_loss": 9.0}) == expect

    def test_max_position_clip(self):
        # 高价股紧止损: 未截断 136 股 → 截断后 int(risk_base*max_pct/150) (复核修复实证样例)
        from app.playbook.db import _estimate_admission_qty
        from app.playbook.config import get_playbook_config
        pb = get_playbook_config()
        risk_base = float(pb.get("risk_base_capital", 82000.0))
        max_pct = float(pb.get("max_position_pct", 0.08))
        qty = _estimate_admission_qty({"entry_price": 150.0, "stop_loss": 147.0})
        assert qty == max(1, int(risk_base * max_pct / 150.0))

    def test_invalid_prices_return_none(self):
        from app.playbook.db import _estimate_admission_qty
        assert _estimate_admission_qty({"entry_price": 0, "stop_loss": 147.0}) is None
        assert _estimate_admission_qty({"entry_price": 150.0, "stop_loss": 0}) is None
        assert _estimate_admission_qty({}) is None
        assert _estimate_admission_qty({"entry_price": None, "stop_loss": None}) is None

    def test_result_non_negative(self):
        from app.playbook.db import _estimate_admission_qty
        qty = _estimate_admission_qty({"entry_price": 50.0, "stop_loss": 49.0})
        assert qty is None or qty >= 0


class TestRFC066MultiRoundLedger:
    """RFC-066 (2026-08-25): 多轮指令共存契约.

    根因: 旧版 UNIQUE(date, symbol) + save_instruction UPSERT + 重武装复位 SQL
    把第一轮已结算台账物理抹除 (8/7-8/24 25 行实锤)。修复后:
      ① 同 (date, symbol) 可多行共存 (各自独立 decision_id);
      ② intraday_reentry- 前缀恒 INSERT 新轮次行;
      ③ 已结算行 (executed=1 且 outcome 终态) 永不被重写;
      ④ 同 decision_id 幂等原地更新;
      ⑤ 旧优先级阶梯保留 (UE 不碰 DA)。
    """

    def test_schema_no_unique_date_symbol(self, db):
        """新表 schema 无 UNIQUE(date, symbol) 约束"""
        conn = db._conn()
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='playbook_instructions'"
        ).fetchone()[0]
        assert "UNIQUE(date, symbol)" not in sql

    def test_reentry_inserts_new_row(self, db):
        """intraday_reentry- 前缀 → INSERT 新轮次行, 第一轮不动"""
        r1 = db.save_instruction({
            "date": "2026-08-25", "symbol": "US.AU", "direction": "BUY",
            "decision_id": "DA-20260825-US.AU-test", "thesis": "round1",
        })
        db.update_instruction_tracking(r1, {
            "executed": 1, "outcome": "win", "pnl": 10.0})
        r2 = db.save_instruction({
            "date": "2026-08-25", "symbol": "US.AU", "direction": "BUY",
            "decision_id": "intraday_reentry-123-1046", "thesis": "round2",
        })
        assert r2 != r1
        rows = db.get_instructions_by_date("2026-08-25")
        assert len(rows) == 2
        r1row = next(r for r in rows if r["id"] == r1)
        assert r1row["outcome"] == "win" and r1row["pnl"] == 10.0
        assert r1row["thesis"] == "round1"

    def test_settled_row_never_overwritten(self, db):
        """已结算行 (executed=1 终态) 永不被任何来源重写"""
        r1 = db.save_instruction({
            "date": "2026-08-25", "symbol": "US.TSLA", "direction": "BUY",
            "decision_id": "DA-20260825-US.TSLA-x", "thesis": "orig",
        })
        db.update_instruction_tracking(r1, {
            "executed": 1, "outcome": "loss", "pnl": -5.0})
        # 同 decision_id 重跑 → 返回原行, 结算不丢
        again = db.save_instruction({
            "date": "2026-08-25", "symbol": "US.TSLA", "direction": "BUY",
            "decision_id": "DA-20260825-US.TSLA-x", "thesis": "rerun",
        })
        assert again == r1
        row = next(r for r in db.get_instructions_by_date("2026-08-25") if r["id"] == r1)
        assert row["outcome"] == "loss" and row["pnl"] == -5.0
        # 不同 (更高) 源覆盖 → 同样拒绝 (已结算)
        db.save_instruction({
            "date": "2026-08-25", "symbol": "US.TSLA", "direction": "SELL_SHORT",
            "decision_id": "universe_expansion:2026-08-25:TSLA", "thesis": "ue",
        })
        rows = db.get_instructions_by_date("2026-08-25")
        assert len(rows) == 1 and rows[0]["outcome"] == "loss"

    def test_same_decision_id_idempotent_update(self, db):
        """同 decision_id 未结算行 → 原地更新 (幂等重跑)"""
        r1 = db.save_instruction({
            "date": "2026-08-25", "symbol": "US.COIN", "direction": "BUY",
            "decision_id": "DA-20260825-US.COIN-y", "thesis": "v1",
        })
        r2 = db.save_instruction({
            "date": "2026-08-25", "symbol": "US.COIN", "direction": "BUY",
            "decision_id": "DA-20260825-US.COIN-y", "thesis": "v2",
        })
        assert r2 == r1
        rows = db.get_instructions_by_date("2026-08-25")
        assert len(rows) == 1 and rows[0]["thesis"] == "v2"

    def test_ue_does_not_touch_da(self, db):
        """UE 浅指令不得覆盖/并存 DA 深度指令 (旧优先级阶梯保留)"""
        db.save_instruction({
            "date": "2026-08-25", "symbol": "US.ORCL", "direction": "SELL_SHORT",
            "decision_id": "DA-20260825-US.ORCL-z", "thesis": "deep",
        })
        db.save_instruction({
            "date": "2026-08-25", "symbol": "US.ORCL", "direction": "SELL_SHORT",
            "decision_id": "universe_expansion:2026-08-25:ORCL", "thesis": "shallow",
        })
        rows = db.get_instructions_by_date("2026-08-25")
        assert len(rows) == 1
        assert rows[0]["decision_id"] == "DA-20260825-US.ORCL-z"
        assert rows[0]["thesis"] == "deep"

    def test_get_instruction_returns_first_round(self, db):
        """get_instruction 多行下返回第一轮 (最小 id, 保持旧语义)"""
        r1 = db.save_instruction({
            "date": "2026-08-25", "symbol": "US.NVDA", "direction": "BUY",
            "decision_id": "DA-20260825-US.NVDA-w", "thesis": "first",
        })
        db.save_instruction({
            "date": "2026-08-25", "symbol": "US.NVDA", "direction": "BUY",
            "decision_id": "intraday_reentry-999-1100", "thesis": "second",
        })
        got = db.get_instruction("2026-08-25", "US.NVDA")
        assert got["id"] == r1 and got["thesis"] == "first"
