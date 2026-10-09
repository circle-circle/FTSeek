"""[RFC-120 2026-09-30] chase_cap 语义倒挂修复钉子 — 归一化 SSOT + 执行层接线 + 回写。

病灶 (DB 实证, 用户裁"执行修复"): 9/4 enforce 上线后 born-violated 空头 18/18 零执行
(对照合规组 24/33、无cap组 8/10), 9/30 BE(cap=302/entry=298)/KMX(63/60.5)/TSLA(358/355)
三条空单全灭, TSLA 单指令空转 37+ 轮; BUY 侧同病 18 条零执行。根因: LLM 把 SHORT 的
cap 填成 invalidation 上破失效位 (上界语义), 而门语义 (entry_routing.chase_cap_violated)
SHORT cap=下限 → 下单价天然低于 cap → 出生即违例 = 结构性死锁。

本批锁四件事:
  1. normalize_chase_cap 纯函数 (entry_band, 双侧同源 SSOT): 倒挂→入场带顺势沿,
     合规/缺失/畸形一律原样 (零行为变更);
  2. 执行层 chase 门接线: 归一锚必须是**生成时**价 (planned_entry → band沿反推 →
     confirmation), 禁用被 reprice 逐轮覆盖的 DB entry_price 列当锚 (cap 追价 = 静默废闸);
  3. 9/30 三条真实死指令回放: TSLA (锚=confirmation 355) 归一后放行; BE/KMX 仅在
     价格仍低于真实追空下限时拦截 (语义正确且可解除, 非死锁);
  4. _write_round_skip_reason: 非终态回写 (不入 INVALIDATED_SKIP_REASONS) + 同前缀幂等
     + VWAP/证伪斜率两门接线。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.playbook.entry_band import (  # noqa: E402
    CHASE_ZONE_PCT,
    normalize_chase_cap,
)
from app.playbook.entry_routing import chase_cap_violated  # noqa: E402
from app.playbook import pipeline as pl  # noqa: E402

_SRC_PIPE = os.path.join(os.path.dirname(__file__), "..", "app", "playbook", "pipeline.py")
# [RFC-060 phase1] pipeline 大重构后:
#   - 倒挂归一化块 (norm/band-edge/orig_cap) 迁入 pipeline_parts/auto_execute_flow.py
#   - _write_round_skip_reason 定义留 pipeline.py, 两处调用迁入 pipeline_parts/trigger_flow.py
# 双源拼接保住所有锚字符串 (def + 调用两侧)。
_SRC_AEF = os.path.join(
    os.path.dirname(__file__), "..", "app", "playbook",
    "pipeline_parts", "auto_execute_flow.py",
)
_SRC_TF = os.path.join(
    os.path.dirname(__file__), "..", "app", "playbook",
    "pipeline_parts", "trigger_flow.py",
)


def _src(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _src_pipe_all() -> str:
    """pipeline.py + 两个新宿主的拼接 (供源级契约断言)。"""
    return _src(_SRC_PIPE) + "\n" + _src(_SRC_AEF) + "\n" + _src(_SRC_TF)


# ── ① normalize_chase_cap 纯函数 ─────────────────────────────────────
class TestNormalizePure:
    def test_short_inverted_normalized_to_band_floor(self):
        """9/30 BE 真实值: cap=302 (invalidation 上破位误填), 生成入场 298 → 298×0.95。"""
        cap, flipped = normalize_chase_cap("SELL_SHORT", 298.0, 302.0)
        assert flipped is True
        assert cap == pytest.approx(298.0 * (1 - CHASE_ZONE_PCT))
        assert cap < 298.0  # 归一后满足 SHORT cap=下限语义

    def test_short_alias_direction(self):
        cap, flipped = normalize_chase_cap("SHORT", 100.0, 105.0)
        assert flipped is True and cap == pytest.approx(95.0)

    def test_buy_inverted_normalized_to_band_ceiling(self):
        """BUY 同病 (9/4后 18 条零执行): cap=支撑位误填 → entry×1.05。"""
        cap, flipped = normalize_chase_cap("BUY", 100.0, 93.0)
        assert flipped is True
        assert cap == pytest.approx(100.0 * (1 + CHASE_ZONE_PCT))

    def test_compliant_cap_untouched(self):
        """合规值零行为变更 — ARM 9/30 真实对照: SHORT entry=291.28 cap=285 (下界, 合规)。"""
        cap, flipped = normalize_chase_cap("SELL_SHORT", 291.28, 285.0)
        assert flipped is False and cap == 285.0
        cap, flipped = normalize_chase_cap("BUY", 291.28, 295.0)
        assert flipped is False and cap == 295.0

    def test_boundary_equality_is_inverted(self):
        """cap == entry: SHORT 判倒挂 (价=cap 时 chase_cap_violated 不拦, 但零追空空间
        = 无信息量), BUY 同。与门语义 (严格不等式) 对齐取保守侧。"""
        _, flipped = normalize_chase_cap("SELL_SHORT", 100.0, 100.0)
        assert flipped is True
        _, flipped = normalize_chase_cap("BUY", 100.0, 100.0)
        assert flipped is True

    @pytest.mark.parametrize("cap", [None, "", "null", 0, -1, "abc"])
    def test_missing_or_bad_cap_passthrough(self, cap):
        out, flipped = normalize_chase_cap("SELL_SHORT", 100.0, cap)
        assert flipped is False and out == cap

    @pytest.mark.parametrize("entry", [None, "", 0, -5, "abc"])
    def test_missing_or_bad_entry_passthrough(self, entry):
        out, flipped = normalize_chase_cap("SELL_SHORT", entry, 105.0)
        assert flipped is False and out == 105.0

    def test_normalized_cap_releases_gate_semantics(self):
        """端到端语义: 归一后 chase_cap_violated 对"价在生成入场附近"的单不再恒违例。"""
        cap, _ = normalize_chase_cap("SELL_SHORT", 355.0, 358.0)   # TSLA 9/30
        assert chase_cap_violated("SELL_SHORT", 351.49, cap) is False  # 现价放行
        assert chase_cap_violated("SELL_SHORT", cap - 0.01, cap) is True  # 真追空仍拦

    def test_direction_alias_contract(self):
        """LONG 与 BUY、SHORT 与 SELL_SHORT 必须同侧, 防写入方别名错位。"""
        assert chase_cap_violated("BUY", 100.01, 100.0) is True
        assert chase_cap_violated("LONG", 100.01, 100.0) is True
        assert chase_cap_violated("LONG", 99.99, 100.0) is False
        assert chase_cap_violated("SELL_SHORT", 99.99, 100.0) is True
        assert chase_cap_violated("SHORT", 99.99, 100.0) is True


# ── ② 执行层锚纪律 (源级契约) ────────────────────────────────────────
class TestGateAnchorContract:
    def setup_method(self):
        self.s = _src_pipe_all()

    def test_normalization_wired_before_violation_check(self):
        """归一必须在违例判定之前 (否则死锁原样)。"""
        norm_pos = self.s.index("normalize_chase_cap as _cc_norm")
        viol_pos = self.s.index("if _cc_violated(_dir, _new_entry, _cc_f):")
        assert norm_pos < viol_pos

    def test_anchor_never_uses_live_entry_price_column(self):
        """锚禁用 DB entry_price 列 (reprice 逐轮覆盖 → cap 追价 = 静默废闸)。

        归一块内不得出现 inst.get("entry_price") 当锚; 合法锚 = planned_entry /
        band沿反推 (entry_low|entry_high ÷ (1∓CHASE)) / confirmation.price_level。
        """
        start = self.s.index("normalize_chase_cap as _cc_norm")
        block = self.s[start:start + 3200]
        assert '_rawj_cc.get("planned_entry")' in block
        assert 'inst.get("entry_low")' in block and 'inst.get("entry_high")' in block
        assert '_conf_cc.get("price_level")' in block
        # 锚区禁用活价列: 归一块内不得把 entry_price 列当锚 (判定用 _new_entry 是另一回事)
        assert 'inst.get("entry_price")' not in block.split("_cc_norm(")[0]

    def test_band_edge_anchor_unfolds_chase_pct(self):
        """band 沿反推生成价: entry_low 已含 ×(1-CHASE), 直接当锚会二次折价。"""
        assert "/ (1 - _cc_chase_pct)" in self.s and "/ (1 + _cc_chase_pct)" in self.s

    def test_missing_anchor_keeps_original_cap(self):
        """锚全缺失 → 不归一 (维持原 cap 原行为, 宁可继续拦也不静默放行)。"""
        assert "if _gen_cc:\n" in self.s or "if _gen_cc:" in self.s

    def test_telemetry_marks_inversion(self):
        """影子行/审计带 cap_inverted + orig_cap_price — replay 可切分两人群。"""
        assert '"cap_inverted": bool(_cc_normed)' in self.s
        assert '"orig_cap_price": (_cc_orig if _cc_normed else None)' in self.s

    def test_direction_aware_release_copy(self):
        assert '"涨回帽上即放行"' in self.s and '"回落帽下即放行"' in self.s


# ── ③ 9/30 三条真实死指令回放 ────────────────────────────────────────
def _gate_eval(inst: dict, market_price: float):
    """复刻执行层归一+判定链 (锚优先级同 pipeline 块), 返回 (violated, cap, normed)。"""
    raw = inst.get("raw_instruction_json") or {}
    if isinstance(raw, str):
        raw = json.loads(raw)
    direction = inst["direction"]
    cap = raw.get("chase_cap_price")
    is_short = "SHORT" in direction.upper()
    gen = raw.get("planned_entry")
    if not gen:
        edge = inst.get("entry_low") if is_short else inst.get("entry_high")
        gen = (edge / (1 - CHASE_ZONE_PCT) if is_short
               else edge / (1 + CHASE_ZONE_PCT)) if edge else None
    if not gen:
        conf = raw.get("confirmation") or {}
        gen = conf.get("price_level") if isinstance(conf, dict) else None
    if cap is not None and gen:
        cap, normed = normalize_chase_cap(direction, gen, cap)
    else:
        normed = False
    return chase_cap_violated(direction, market_price, cap), cap, normed


class TestRealDeadlockReplay:
    """9/30 DB 真实行 (id 3480/3479/3483) 的归一前后行为。"""

    BE = {"id": 3480, "symbol": "US.BE", "direction": "SELL_SHORT", "entry_price": 279.41,
          "entry_low": 283.1, "entry_high": 306.94,
          "raw_instruction_json": {"chase_cap_price": 302.0, "planned_entry": 298.0,
                                   "confirmation": {"price_level": 298.0}}}
    KMX = {"id": 3479, "symbol": "US.KMX", "direction": "SELL_SHORT", "entry_price": 56.64,
           "entry_low": 57.475, "entry_high": 62.315,
           "raw_instruction_json": {"chase_cap_price": 63.0, "planned_entry": 60.5,
                                    "confirmation": {"price_level": 60.5}}}
    TSLA = {"id": 3483, "symbol": "US.TSLA", "direction": "SELL_SHORT", "entry_price": 353.74,
            "entry_low": None, "entry_high": None,
            "raw_instruction_json": {"chase_cap_price": 358.0, "planned_entry": None,
                                     "confirmation": {"price_level": 355.0}}}

    def test_old_behavior_was_deadlock(self):
        """修前语义回放: 三条全部出生即违例 (cap 原值, 任何价格 <= 失效位都拦)。"""
        for inst, px in ((self.BE, 279.41), (self.KMX, 56.64), (self.TSLA, 351.49)):
            raw = inst["raw_instruction_json"]
            assert chase_cap_violated(inst["direction"], px, raw["chase_cap_price"]) is True

    def test_tsla_released_by_confirmation_anchor(self):
        """TSLA: band/planned 全缺 → confirmation=355 锚 → cap=337.25 → 现价 351 放行。"""
        violated, cap, normed = _gate_eval(self.TSLA, 351.49)
        assert normed is True and cap == pytest.approx(355.0 * 0.95)
        assert violated is False, "结构性死锁必须解除"

    def test_be_kmx_blocked_only_while_true_chase(self):
        """BE/KMX: 现价已跌破生成入场 -5% (真追空) → 仍拦; 回升初位 → 放行。

        这是语义正确的拦截 (窗口非空、可解除), 与修前"任何价格恒拦"的死锁本质不同。
        """
        violated, cap, normed = _gate_eval(self.BE, 279.41)
        assert normed is True and cap == pytest.approx(283.1)
        assert violated is True          # 279.41 < 283.1 = 已追空 6%+
        assert _gate_eval(self.BE, 284.0)[0] is False   # 回升放行

        violated, cap, normed = _gate_eval(self.KMX, 56.64)
        assert normed is True and cap == pytest.approx(57.475)
        assert violated is True
        assert _gate_eval(self.KMX, 58.0)[0] is False

    def test_normalized_cap_aligns_with_trigger_band_floor(self):
        """归一 cap = band 顺势沿 ⇒ chase 门与触发带同界, 不再互相矛盾
        (触发带要求价 >= entry_low, chase 门修前要求价 >= 302 > band 上沿 = 空集)。"""
        for inst in (self.BE, self.KMX):
            _, cap, _ = _gate_eval(inst, 0)
            assert cap == pytest.approx(inst["entry_low"], rel=1e-3)


# ── ④ _write_round_skip_reason ──────────────────────────────────────
class TestRoundSkipReasonWriteback:
    def test_value_not_terminal(self):
        """回写值不得进 INVALIDATED_SKIP_REASONS (非终态, 下轮必须复评)。"""
        for v in ("vwap_gate_blocked", "falsification_slope_blocked"):
            assert v not in pl.INVALIDATED_SKIP_REASONS
            assert not any(v in r for r in pl.INVALIDATED_SKIP_REASONS)

    def test_same_prefix_idempotent(self, monkeypatch):
        """既有值同前缀 → 不重复写库 (每轮重评不刷 DB)。"""
        calls = []
        import app.playbook.db as dbmod

        class _FakeDB:
            def update_instruction_tracking(self, iid, upd):
                calls.append((iid, upd))

        monkeypatch.setattr(dbmod, "PlaybookDB", _FakeDB)
        inst = {"id": 1, "symbol": "US.TEST",
                "skip_reason": "vwap_gate_blocked(dev=-1.3%,逆BUY,下轮复评)"}
        pl._write_round_skip_reason(inst, "vwap_gate_blocked(dev=-0.9%,逆BUY,下轮复评)")
        assert calls == []

    def test_write_and_overwrite_stale_prefix(self, monkeypatch):
        calls = []
        import app.playbook.db as dbmod

        class _FakeDB:
            def update_instruction_tracking(self, iid, upd):
                calls.append((iid, upd))

        monkeypatch.setattr(dbmod, "PlaybookDB", _FakeDB)
        inst = {"id": 7, "symbol": "US.TEST", "skip_reason": ""}
        pl._write_round_skip_reason(inst, "vwap_gate_blocked(dev=-1.3%,逆BUY,下轮复评)")
        assert calls == [(7, {"skip_reason": "vwap_gate_blocked(dev=-1.3%,逆BUY,下轮复评)"})]
        assert inst["skip_reason"].startswith("vwap_gate_blocked")
        # 陈旧前缀可被新门覆盖
        pl._write_round_skip_reason(inst, "falsification_slope_blocked(slope=+0.004,下轮复评)")
        assert len(calls) == 2

    def test_db_failure_does_not_raise(self, monkeypatch):
        """回写失败只 warn, 绝不影响拦截 (fail-open 属观测面)。"""
        import app.playbook.db as dbmod

        class _BoomDB:
            def update_instruction_tracking(self, iid, upd):
                raise RuntimeError("db locked")

        monkeypatch.setattr(dbmod, "PlaybookDB", _BoomDB)
        inst = {"id": 9, "symbol": "US.TEST", "skip_reason": ""}
        pl._write_round_skip_reason(inst, "vwap_gate_blocked(dev=-1%,逆BUY,下轮复评)")  # 不抛

    def test_wired_into_both_gates(self):
        s = _src_pipe_all()
        assert s.count("_write_round_skip_reason(") >= 3  # def + VWAP门 + 证伪斜率门
        assert 'f"vwap_gate_blocked(dev=' in s
        assert 'f"falsification_slope_blocked(slope=' in s
