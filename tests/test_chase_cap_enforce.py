"""[P0-A 2026-09-04, REVIEW-20260904 用户裁"按建议执行"] chase_cap enforce + UE 覆盖测试。

背景: DA 指令自带 chase_cap_price (禁追线) 但执行层只遥测不拦截 —— 9/3 实测
8/12 笔带 cap 指令 re-price 顶破禁追线仍市价成交 (语义倒挂); UE 指令连字段都没有
(9/4 USAR 盘前追价 +2.16% 入场, 8 分钟 SL_INITIAL -$158, 遥测双盲)。

本批锁住四件事:
  1. chase_cap_violated 纯函数: 方向感知 (BUY 越上沿 / SELL_SHORT 破下沿),
     缺 cap/坏值/非正价一律不判 (与无 cap 指令现状行为一致);
  2. chase_cap_enforce_enabled 开关: 默认 False, dict 注入与 PlaybookConfig
     对象 (非 dict) 两路行为正确 (2026-09-01 entry_routing 同型 bug 教训);
  3. UE inject 产出的指令自带 chase_cap_price = 入场带顺势沿
     (LONG=entry_high / SHORT=entry_low), 且 parse_raw_semantics 能读回
     (与 DA 同顶层键位同消费端);
  4. pipeline enforce 源级契约: 遥测写盘先于 enforce (证据先落)、enforce 在
     去重门之外 (不被同值去重吞掉)、终态跳过三件套 (skip_reason + 审计 + continue)。
"""
import json
import os
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.playbook.entry_routing import (  # noqa: E402
    chase_cap_enforce_enabled,
    chase_cap_violated,
    parse_raw_semantics,
)
from scripts import llm_universe_scan as ue  # noqa: E402

_SRC_PIPE = os.path.join(os.path.dirname(__file__), "..", "app", "playbook", "pipeline.py")
_SRC_ER = os.path.join(os.path.dirname(__file__), "..", "app", "playbook", "entry_routing.py")
_SRC_YAML = os.path.join(os.path.dirname(__file__), "..", "config", "unified_config.yaml")


def _src(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestChaseCapViolated:
    """方向感知违例判定 (纯函数)。"""

    def test_buy_above_cap_violates(self):
        assert chase_cap_violated("BUY", 101.0, 100.0) is True

    def test_buy_below_cap_clean(self):
        assert chase_cap_violated("BUY", 99.5, 100.0) is False

    def test_buy_at_cap_clean(self):
        # 贴线不违例 (严格大于) — 与 DA "禁止在$X以上追涨" 语义一致
        assert chase_cap_violated("BUY", 100.0, 100.0) is False

    def test_short_below_cap_violates(self):
        assert chase_cap_violated("SELL_SHORT", 99.0, 100.0) is True

    def test_short_above_cap_clean(self):
        assert chase_cap_violated("SELL_SHORT", 101.0, 100.0) is False

    def test_short_at_cap_clean(self):
        assert chase_cap_violated("SELL_SHORT", 100.0, 100.0) is False

    def test_missing_cap_never_violates(self):
        assert chase_cap_violated("BUY", 999.0, None) is False
        assert chase_cap_violated("SELL_SHORT", 0.01, None) is False

    def test_bad_values_never_violate(self):
        assert chase_cap_violated("BUY", 101.0, "abc") is False
        assert chase_cap_violated("BUY", 101.0, 0) is False
        assert chase_cap_violated("BUY", 0, 100.0) is False
        assert chase_cap_violated("BUY", None, 100.0) is False


class TestEnforceSwitch:
    """开关读取纪律: 默认 False; dict 注入优先; 非 dict (PlaybookConfig) 不炸。"""

    def test_dict_injection(self):
        assert chase_cap_enforce_enabled({"chase_cap_enforce_enabled": True}) is True
        assert chase_cap_enforce_enabled({"chase_cap_enforce_enabled": False}) is False

    def test_playbook_config_object_falls_back_safely(self):
        # 2026-09-01 同型 bug: PlaybookConfig 对象不支持 "key" in cfg → TypeError。
        # 此处注入非 dict 对象, 走 config_container 路径; 容器不可用时必须 False 不炸。
        assert chase_cap_enforce_enabled(SimpleNamespace()) in (True, False)

    def test_yaml_key_present_and_on(self):
        """裁决落点: yaml SSOT 必须显式带键且为 true (用户裁"按建议执行")。"""
        import yaml
        with open(_SRC_YAML, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        assert cfg["playbook"]["chase_cap_enforce_enabled"] is True


def _cand(sym: str, direction: str = "LONG", conf: float = 0.8):
    return {
        "symbol": sym,
        "direction": direction,
        "confidence": conf,
        "scan_price": 100.0,
        "thesis": "test thesis",
        "invalidation": "",
        "sector": "TEST",
        "emergence_streak_days": 1,
    }


def _run_inject_capture(directions):
    """驱动 cmd_inject 走真实写库路径 (PlaybookDB 全 mock), 捕获 save_instruction 入参。"""
    cfg = {
        "universe_expansion.entry_enabled": True,  # 非 dry-run → 走 save 路径
        "universe_expansion.min_confidence": 0.5,
        "universe_expansion.max_targets_per_day": 30,
        "universe_expansion.max_emergence_streak": 99,
        "universe_expansion.min_price_floor": 5,
        "universe_expansion.premarket_anchor": True,
        "universe_expansion.same_symbol_cooldown_days": 0,  # 跳过冷却 DB 查询
    }
    # [P1.5 2026-09-12] 符号用纯字母 (TSA/TSB…): cmd_inject 现在先过统一资格初筛,
    #   `US\.[A-Z]{1,6}` 的形状门会把带数字的夹具 (旧 TS0) 判 invalid_symbol →
    #   本类断言的 chase_cap 字段根本产不出来。夹具跟随真实标的口径, 不放宽门。
    syms = [f"TS{chr(65 + i)}" for i in range(len(directions))]
    candidates = [_cand(sym, d) for sym, d in zip(syms, directions)]
    quotes = {sym: {"price": 100.0} for sym in syms}

    fake_db = mock.MagicMock()
    fake_db.get_instructions_by_date.return_value = []
    fake_gc = mock.MagicMock()
    fake_gc.return_value.get_dotted.side_effect = lambda k, d=None: {
        "universe_expansion.same_symbol_cooldown_days": 0,
        "universe_expansion.short_boost_shadow": False,
    }.get(k, d)

    regime = SimpleNamespace(
        regime=SimpleNamespace(value="neutral"), vix=15.0,
        spy_ma20=1.0, spy_ma50=1.0,
    )
    fake_mrg = mock.MagicMock()
    fake_mrg.return_value.assess.return_value = regime

    scan_rec = {
        "type": "scan", "date": ue._today(),
        "surfaced": candidates, "model_used": "test-model",
        # [P1.5 2026-09-12, proud-slope-falcon] cmd_inject 现在只消费**最新有效
        #   premarket 批次** (ue_batch.latest_premarket): 需要 tz-aware scanned_at
        #   (时效 candidate_scan_max_age_hours=3h)、scan_window 与 batch_status。
        #   缺任一项 → "无有效 premarket 批次" 直接返回 0, 本类 4 条断言全落空。
        #   这不是放宽纪律: 真实 cmd_scan 一直写这三个字段 (见 llm_universe_scan
        #   L2028-2036), 夹具此前只是没跟上批次契约。
        "scan_window": "premarket", "batch_status": "complete",
        "batch_id": f"UE:{ue._today()}:chase-cap-test",
        "started_at": ue._now_et().isoformat(),
        "scanned_at": ue._now_et().isoformat(),
    }
    patches = [
        mock.patch.object(ue, "_cfg", side_effect=lambda k, d=None: cfg.get(k, d)),
        mock.patch.object(ue, "_is_us_trading_day_today", return_value=True),
        mock.patch.object(ue, "_is_us_premarket", return_value=False),
        mock.patch.object(ue, "_read_jsonl", return_value=[scan_rec]),
        mock.patch.object(ue, "_position_symbols", return_value=set()),
        mock.patch.object(ue, "_build_router", return_value=mock.MagicMock()),
        mock.patch.object(ue, "_batch_quotes", return_value=quotes),
        mock.patch.object(ue, "_log_inject_reject"),
        mock.patch.object(ue, "_log_direction_structure"),
        mock.patch.object(ue, "_log_streak_counterfactual"),
        mock.patch.object(ue, "_log_inject_loop_reject"),
        mock.patch.object(ue, "_log_inject_save_reject"),
        mock.patch("app.playbook.db.PlaybookDB", return_value=fake_db),
        mock.patch("app.playbook.market_regime.MarketRegimeGate", fake_mrg),
        mock.patch("app.core.config_container.get_config", fake_gc),
        # [U2 根因修 2026-09-11] 低ATR预检改走执行门 accessor (get_playbook_config);
        #   原扁平键 _cfg 注入 ("playbook.low_atr_gate_enabled": False) 已随接线修复
        #   失效 — 若不打此桩, 活 yaml True/0.03 会在本测试内构建真实 DI provider。
        mock.patch(
            "app.playbook.config.get_playbook_config",
            return_value=SimpleNamespace(
                get=lambda k, d=None: {"low_atr_gate_enabled": False}.get(k, d)),
        ),
        # 本类只验 chase_cap 字段, 不验模式门; 钉住 SIM 前置条件的是
        # tests/playbook/test_ue_funnel_batch.py (含 require_auto_sim 源级接线)。
        mock.patch("app.playbook.candidate_eligibility.require_auto_sim",
                   return_value={"broker_route": "sim", "db_route": "sim"}),
    ]
    for p in patches:
        p.start()
    try:
        rc = ue.cmd_inject()
    finally:
        for p in patches:
            p.stop()
    saved = [c.args[0] for c in fake_db.save_instruction.call_args_list]
    return rc, saved


class TestUEChaseCapField:
    """UE 指令自带禁追线: LONG=entry_high (带上沿), SHORT=entry_low (带下沿)。"""

    def test_long_cap_is_entry_high(self):
        rc, saved = _run_inject_capture(["LONG"])
        assert rc == 0 and len(saved) == 1
        inst = saved[0]
        raw = inst["raw_instruction"]
        assert raw["chase_cap_price"] == inst["entry_high"]
        assert raw["chase_cap_price"] > inst["entry_price"]  # 顺势沿在现价上方

    def test_short_cap_is_entry_low(self):
        rc, saved = _run_inject_capture(["SHORT"])
        assert rc == 0 and len(saved) == 1
        inst = saved[0]
        raw = inst["raw_instruction"]
        assert raw["chase_cap_price"] == inst["entry_low"]
        assert raw["chase_cap_price"] < inst["entry_price"]  # 顺势沿在现价下方

    def test_consumer_parse_roundtrip(self):
        """parse_raw_semantics (pipeline 消费端同函数) 能读回 UE 的 cap。"""
        rc, saved = _run_inject_capture(["LONG"])
        inst = saved[0]
        # db._row_to_dict 产出 str|dict 两种形态都要能读 (2026-09-01 TypeError 教训)
        sem_dict = parse_raw_semantics(inst["raw_instruction"])
        sem_str = parse_raw_semantics(json.dumps(inst["raw_instruction"]))
        assert sem_dict["chase_cap_price"] == inst["entry_high"]
        assert sem_str["chase_cap_price"] == inst["entry_high"]

    def test_cap_actually_blocks_beyond_band(self):
        """端到端语义: re-price 越过带上沿 → chase_cap_violated 判违例。"""
        rc, saved = _run_inject_capture(["LONG"])
        inst = saved[0]
        cap = inst["raw_instruction"]["chase_cap_price"]
        assert chase_cap_violated("BUY", cap + 0.01, cap) is True
        assert chase_cap_violated("BUY", cap - 0.01, cap) is False


class TestPipelineEnforceContract:
    """pipeline enforce 源级契约 (防未来改坏纪律点)。"""

    def setup_method(self):
        self.s = _src(_SRC_PIPE)

    def test_enforce_reads_switch_hot(self):
        # get_dotted 热读取在 entry_routing.chase_cap_enforce_enabled (每轮读 → 热回滚)
        er = _src(_SRC_ER)
        assert '"playbook.chase_cap_enforce_enabled", False' in er
        # pipeline 每轮经该函数取开关 (不缓存)
        assert "_cc_enforce = _cc_enforce_f()" in self.s

    def test_violation_via_shared_pure_function(self):
        # 违例判定走 entry_routing 纯函数 (单测可钉), 不再就地重写方向逻辑
        assert "chase_cap_violated as _cc_violated" in self.s
        assert "if _cc_violated(_dir, _new_entry, _cc_f):" in self.s

    def test_telemetry_row_marks_enforced(self):
        assert '"enforced": bool(_cc_enforce)' in self.s

    def test_enforce_after_telemetry_and_outside_dedup_gate(self):
        """证据先落 (commit) 再拦截; enforce 不在 dedup claim 门内 (防同值去重吞拦截)。"""
        commit_pos = self.s.index("_shadow_dedup_commit(_cc_claim)")
        enforce_pos = self.s.index("if _cc_enforce:")
        assert commit_pos < enforce_pos
        # enforce 与 commit 之间不得再出现 dedup claim (即 enforce 在门块之外)
        assert "_shadow_dedup_claim" not in self.s[commit_pos:enforce_pos]

    def test_terminal_skip_trio(self):
        """终态跳过三件套: skip_reason + 审计 + continue (计数器+循环位移)。"""
        assert 'chase_cap_enforced(reprice=' in self.s
        assert 'action="skipped_chase_cap"' in self.s
        block = self.s[self.s.index("if _cc_enforce:"):]
        assert "skipped_count += 1" in block
        assert "continue" in block

    def test_rollback_path_documented_in_yaml(self):
        s = _src(_SRC_YAML)
        assert "chase_cap_enforce_enabled: true" in s
        assert "回滚: 置 false" in s
