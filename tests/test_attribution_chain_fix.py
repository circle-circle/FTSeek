#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
归因链路修复验证测试 (Phase 1 Verification)
"""

import sys
import os
import unittest
import sqlite3
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.strategy.base import Signal, SignalAction
from app.evidence.attribution_chain_fix import AttributionQueryEngine
from app.evidence.decision_log_trigger import DecisionLogTrigger
from app.core.decision_log import DecisionLog


class TestTraceIdInjection(unittest.TestCase):
    """测试 trace_id 生成与注入"""
    
    def test_signal_has_trace_id(self):
        """Signal 对象应包含 trace_id 字段"""
        signal = Signal(
            code="HK.00700",
            action=SignalAction.BUY,
            price=300.0,
            trace_id="TRC-TEST-001",
        )
        self.assertEqual(signal.trace_id, "TRC-TEST-001")
    
    def test_position_insert_with_trace_id(self):
        """Position 表应能正确写入 trace_id"""
        db_path = "data/ftseek.db"
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.cursor()
            test_trace = f"TRC-TEST-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            cursor.execute('''
                INSERT INTO positions 
                (code, action, quantity, entry_price, entry_time, status, trace_id, strategy)
                VALUES (?, 'BUY', 100, 100.0, ?, 'OPEN', ?, 'test_strategy')
            ''', ("HK.TEST", datetime.now().isoformat(), test_trace))
            conn.commit()
            
            cursor.execute("SELECT trace_id FROM positions WHERE trace_id = ?", (test_trace,))
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], test_trace)
            
            cursor.execute("DELETE FROM positions WHERE trace_id = ?", (test_trace,))
            conn.commit()
        finally:
            conn.close()


class TestAttributionQuery(unittest.TestCase):
    """测试归因查询引擎"""
    
    def test_query_engine_init(self):
        engine = AttributionQueryEngine()
        self.assertIsNotNone(engine)
    
    def test_full_chain_not_found(self):
        engine = AttributionQueryEngine()
        chain = engine.get_full_chain("NONEXISTENT-TRACE-ID")
        # 应返回包含空值的字典
        self.assertIsNotNone(chain)
        self.assertEqual(chain["trace_id"], "NONEXISTENT-TRACE-ID")
        self.assertIsNone(chain["position"])
    
    def test_query_trades(self):
        engine = AttributionQueryEngine()
        trades = engine.query_trades(limit=10)
        # 即使有 trace_id 的 positions，也可能没有归因数据
        self.assertIsInstance(trades, list)


class TestDecisionLogTrigger(unittest.TestCase):
    """测试决策日志触发"""
    
    def setUp(self):
        self.dl = DecisionLog()
        self.trigger = DecisionLogTrigger(self.dl)
    
    def test_log_signal_generated(self):
        self.trigger.log_signal_generated(
            trace_id="TRC-TEST-002",
            code="HK.00700",
            action="BUY",
            strategy="test",
            confidence=0.8,
        )
        entries = self.dl.query(decision_type="trade_decision", symbol="HK.00700", limit=5)
        self.assertGreater(len(entries), 0)
        self.assertEqual(entries[0]["signal_id"], "TRC-TEST-002")
    
    def test_log_risk_rejected(self):
        self.trigger.log_risk_rejected(
            trace_id="TRC-TEST-003",
            code="HK.00700",
            action="BUY",
            reason="test_reject",
            strategy="test",
        )
        entries = self.dl.query(symbol="HK.00700", limit=5)
        rejected = [e for e in entries if e.get("result") == "risk_rejected"]
        self.assertGreater(len(rejected), 0)
    
    def test_log_trade_executed(self):
        self.trigger.log_trade_executed(
            trace_id="TRC-TEST-004",
            code="HK.00700",
            action="BUY",
            price=300.0,
            quantity=100,
            strategy="test",
            confidence=0.85,
        )
        entries = self.dl.query(symbol="HK.00700", limit=5)
        executed = [e for e in entries if e.get("result") == "success"]
        self.assertGreater(len(executed), 0)
    
    def test_log_attribution_complete(self):
        self.trigger.log_attribution_complete(
            trace_id="TRC-TEST-005",
            code="HK.00700",
            pnl=2.5,
            overall_score=85,
            direction_correct=True,
            exit_reason="take_profit",
            strategy="test",
        )
        entries = self.dl.query(symbol="HK.00700", limit=5)
        attributed = [e for e in entries if e.get("result") == "closed"]
        self.assertGreater(len(attributed), 0)


class TestIntegration(unittest.TestCase):
    """集成测试"""
    
    def test_full_chain_workflow(self):
        """模拟完整工作流"""
        dl = DecisionLog()
        trigger = DecisionLogTrigger(dl)
        trace_id = f"TRC-INTEGRATION-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        
        # 1. 信号生成
        trigger.log_signal_generated(
            trace_id=trace_id, code="HK.00700", action="BUY",
            strategy="confluence_v2", confidence=0.75,
        )
        
        # 2. 风控通过
        trigger.log_risk_passed(
            trace_id=trace_id, code="HK.00700", action="BUY",
            confidence=0.75, strategy="confluence_v2",
        )
        
        # 3. 交易执行
        trigger.log_trade_executed(
            trace_id=trace_id, code="HK.00700", action="BUY",
            price=300.0, quantity=100, strategy="confluence_v2",
            confidence=0.75,
        )
        
        # 4. 归因完成
        trigger.log_attribution_complete(
            trace_id=trace_id, code="HK.00700",
            pnl=3.5, overall_score=88, direction_correct=True,
            exit_reason="trailing_stop", strategy="confluence_v2",
        )
        
        # 查询所有条目 (query 返回从新到旧)
        entries = dl.query(symbol="HK.00700", limit=20)
        trace_entries = [e for e in entries if e.get("signal_id") == trace_id]
        
        self.assertEqual(len(trace_entries), 4)
        # query 返回从新到旧，所以 [0] 是 attributed, [3] 是 generated
        self.assertEqual(trace_entries[0]["decision"], "attributed")
        self.assertEqual(trace_entries[1]["decision"], "executed")
        self.assertEqual(trace_entries[2]["decision"], "execute")
        self.assertEqual(trace_entries[3]["decision"], "generated")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    unittest.main(verbosity=2)
