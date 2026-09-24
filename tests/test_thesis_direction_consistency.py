"""[DEB-20260924-indexcaliber R4] 指令记录语义一致性契约测试.

病根 (9/24 AXTI 实例): SELL_SHORT 指令落库 thesis="均值回归: above_ma20,
macd_golden_cross, 超跌反弹机会" + conditions=[above_ma20, macd_golden_cross],
而 DA checkpoint 真实决策是 FADE_SHORT (PE 2432x / below_ma20 / 盘前 -4.36%
利好出尽, conditions=[pe_above_industry, insider_net_sell, below_ma20])。
两条病灶: ① THESIS_TEMPLATES 六个非 skip 模板全是多头文案, SHORT 无路可走;
② _select_thesis_template 对非 BUY 一律映射多头模板。

本文件钉住: SHORT 永不落多头文案 / BUY 映射零回归 / 审计行显式携带飞刀门判定值。
任何改动必须先打碎这些钉子。
"""
import json

import pytest

from app.playbook.instruction_generator import THESIS_TEMPLATES, build_thesis
from app.playbook.pipeline import _select_thesis_template

# 多头文案模板 (SHORT 方向禁用集)
_LONG_TEMPLATES = {
    "trend_pullback", "breakout", "value_reversal",
    "momentum_continuation", "insider_confidence", "mean_reversion",
}
_SHORT_TEMPLATES = {"trend_short", "fade_short", "mean_reversion_short"}
# 多头文案特征短语 (渲染后文本级断言)
_LONG_PHRASES = ("超跌反弹机会", "低风险入场机会", "动量确认", "管理层用行动投票",
                 "基本面低估+技术面企稳", "趋势持续")


class TestThesisTemplateDirectionConsistency:
    """_select_thesis_template 方向感知 (R4-B①)."""

    @pytest.mark.parametrize("factors,direction,expected", [
        # ── SHORT 侧: 新映射 ──
        ({"macd_golden_cross": 1}, "SELL_SHORT", "fade_short"),          # AXTI 病根实例
        ({"insider_net_buy": 1}, "SELL_SHORT", "fade_short"),            # 逆内部人信号做空
        ({"rsi_overbought": 1}, "SELL_SHORT", "mean_reversion_short"),
        ({}, "SELL_SHORT", "trend_short"),
        ({"volume_expansion": 1}, "SELL_SHORT", "trend_short"),
        (None, "SELL_SHORT", "trend_short"),
        # ── BUY 侧: 与改动前逐条一致 (零回归) ──
        ({"macd_golden_cross": 1}, "BUY", "breakout"),
        ({"insider_cluster_buy": 1}, "BUY", "insider_confidence"),
        ({"insider_net_buy": 1}, "BUY", "insider_confidence"),
        ({"rsi_oversold": 1}, "BUY", "trend_pullback"),
        ({}, "BUY", "momentum_continuation"),
        ({"volume_expansion": 1}, "BUY", "momentum_continuation"),
        # ── SKIP: 原缺陷会落 mean_reversion (多头文案), 现在显式 skip ──
        ({"macd_golden_cross": 1}, "SKIP", "skip"),
    ])
    def test_mapping(self, factors, direction, expected):
        assert _select_thesis_template(factors, direction) == expected

    @pytest.mark.parametrize("factors", [
        {}, None, {"macd_golden_cross": 1}, {"rsi_overbought": 1},
        {"insider_net_buy": 1}, {"news_negative": 1, "below_ma20": 1},
    ])
    @pytest.mark.parametrize("direction", ["SELL_SHORT", "SHORT"])
    def test_short_never_gets_long_template(self, factors, direction):
        key = _select_thesis_template(factors, direction)
        assert key not in _LONG_TEMPLATES, f"SHORT 落多头模板 {key}"
        assert key in _SHORT_TEMPLATES
        rendered = build_thesis(key, ["below_ma20", "pe_above_industry"])
        for phrase in _LONG_PHRASES:
            assert phrase not in rendered

    def test_templates_and_schema_extended(self):
        assert _SHORT_TEMPLATES <= set(THESIS_TEMPLATES)
        from app.playbook.instruction_generator import DECISION_SCHEMA
        enum = DECISION_SCHEMA["items"]["properties"]["thesis_template"]["enum"]
        assert _SHORT_TEMPLATES <= set(enum)

    def test_build_thesis_short_renders_conditions(self):
        t = build_thesis("fade_short", ["pe_above_industry", "below_ma20"])
        assert "pe_above_industry" in t and "反弹分批做空" in t


class TestExecGateAuditKnifeField:
    """飞刀门判定值显式留痕 (R4-D): 评估即写入, 无论是否拦截."""

    def _emit(self, tmp_path, monkeypatch, inst):
        import app.playbook.pipeline as pl
        monkeypatch.setattr(pl, "__file__", str(tmp_path / "app" / "playbook" / "pipeline.py"))
        pl._emit_exec_gate_audit(inst, action="executed", entry=100.0, size=1, notional=100.0)
        p = tmp_path / "data" / "shadow" / "exec_gate_audit.jsonl"
        assert p.exists(), "审计文件未写出"
        return json.loads(p.read_text(encoding="utf-8").strip().splitlines()[-1])

    def test_knife_audit_present_when_evaluated(self, tmp_path, monkeypatch):
        kg = {"evaluated": True, "fired": False, "rt_price": 138.48,
              "prev_close": 144.56, "chg_pct": -4.2, "threshold_pct": -5.0}
        row = self._emit(tmp_path, monkeypatch,
                         {"symbol": "US.TEST", "direction": "BUY", "_knife_audit": kg})
        assert row["knife_gate"] == kg
        assert row["knife_gate"]["fired"] is False  # E16 场景: 评估未触发也有直接记录

    def test_no_knife_field_when_not_evaluated(self, tmp_path, monkeypatch):
        row = self._emit(tmp_path, monkeypatch, {"symbol": "US.TEST2", "direction": "SELL_SHORT"})
        assert "knife_gate" not in row
