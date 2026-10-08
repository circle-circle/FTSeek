"""[RFC-122 阶段0 2026-10-01] 偏斜门双口径只读影子契约钉。

病灶 (DEB-20260930-skew-gate-vision-gap): guardrail 分母只含本批构建侧候选,
DA skill 直写/UE/reentry 行绕过 (9月 active 279 中 195 条); 9/30 日志 86%=6B/1S
误砍 AMD/HPE(conf=70), 当日并集 9B/4S=69% 本不该触发。

本批 = 阶段0 只读影子: 判定点双算 batch-vision vs portfolio-vision (活读当日 DB),
jsonl 留痕, **判定仍用 batch-vision 零行为变更**。活读 DB 即 point-in-time 真值的
结构依据: builder 内不 save (return 后调用方才 save), 判定时刻 DB 当日行 = 先入库的
直写行 (8 日实测 created_at 恒早于 generate completed_at)。

测试纪律: 全部用例**零生产 I/O** — emit 驱动用 FakeDB + tmp_path 重定向 jsonl;
guardrail 级用例用 spy 替换 emit, 不真读 DB / 不真写 data/shadow/。
"""
import json
from pathlib import Path

import pytest

import app.playbook.pipeline as P

_SRC = (Path(__file__).resolve().parents[1] / "app" / "playbook" / "pipeline.py").read_text(
    encoding="utf-8")


class _Inst:
    def __init__(self, symbol, direction, confidence=70, position_size=10):
        self.symbol = symbol
        self.direction = direction
        self.confidence = confidence
        self.position_size = position_size
        self.skip_reason = ""
        self.risk_reward = 2.0
        self.sector = ""


def _skewed_batch():
    """6 BUY / 1 SHORT = 86% > 80% → 批次口径必砍 (9/30 形态)。"""
    return ([_Inst(s, "BUY", confidence=50 + n)
             for n, s in enumerate(["US.AMAT", "US.AMD", "US.HOOD", "US.HPE", "US.LITE", "US.META"])]
            + [_Inst("US.TSLA", "SELL_SHORT", confidence=70)])


# ── ① 零行为变更 (最高优先: 影子绝不可改判定) ──────────────────────────
class TestShadowZeroBehaviorChange:
    def test_judgment_identical_with_and_without_today(self, monkeypatch):
        """today="" (影子跳过) 与 today="date" (影子跑) 的判定结果逐字一致。"""
        # [DEB-20261008 F1] 摘闸后默认不砍单; 本类钉的是「影子不改判定」,
        # 显式打开开关以保留对「砍单判定」本身的观察对象。
        monkeypatch.setattr(P, "portfolio_guardrail_enabled", lambda: True)
        calls = []
        monkeypatch.setattr(P, "_emit_skew_gate_vision_shadow",
                            lambda *a, **k: calls.append(a))
        a, b = _skewed_batch(), _skewed_batch()
        out_a = P._apply_portfolio_guardrails(a)                       # today="" → emit 内部跳过
        out_b = P._apply_portfolio_guardrails(b, today="2026-09-30")   # today 非空 → 影子实算
        assert len(calls) == 2, "emit 每次判定都调用 (空 today 由 emit 内部跳过)"
        assert calls[0][1] == "" and calls[1][1] == "2026-09-30"
        for x, y in zip(out_a, out_b):
            assert x.direction == y.direction, f"{x.symbol} 方向判定被影子改变"
            assert x.skip_reason == y.skip_reason
            assert x.position_size == y.position_size, "RFC-071: size 不得被改动"
        assert sum(1 for i in out_b if i.direction == "SKIP") >= 1, "86% 必须仍触发砍单"

    def test_shadow_exception_does_not_break_guardrails(self, monkeypatch):
        """影子抛任何异常 → 判定照常完成 (调用点保险丝 + emit 自吞错双层)。"""
        monkeypatch.setattr(P, "portfolio_guardrail_enabled", lambda: True)  # F1 摘闸后为关, 钉旧判定须显式开
        def _boom(*a, **k):
            raise RuntimeError("simulated shadow failure")
        monkeypatch.setattr(P, "_emit_skew_gate_vision_shadow", _boom)
        insts = _skewed_batch()
        out = P._apply_portfolio_guardrails(insts, today="2026-09-30")
        assert sum(1 for i in out if i.direction == "SKIP") >= 1, "影子炸了判定仍须完成"

    def test_judgment_still_batch_vision_source_contract(self):
        """源级钉: majority_pct 判定仍用 batch 口径 (影子不得改判定分母)。"""
        assert "majority_pct = max(buy_count, short_count) / total if total > 0 else 0" in _SRC
        assert "if majority_pct > max_direction_skew:" in _SRC
        assert "_trim_majority_direction(valid, majority_dir, max_direction_skew)" in _SRC


# ── ② 双口径计算正确性 (FakeDB + tmp jsonl, 零生产 I/O) ─────────────────
class TestShadowDualVision:
    def _emit(self, monkeypatch, tmp_path, prior_rows, valid):
        class _FakeDB:
            def get_instructions_by_date(self, d):
                assert d == "2026-09-30"
                return prior_rows

        monkeypatch.setattr("app.playbook.db.PlaybookDB", lambda *a, **k: _FakeDB())
        monkeypatch.setattr(P, "__file__",
                            str(tmp_path / "app" / "playbook" / "pipeline.py"))
        P._emit_skew_gate_vision_shadow(valid, "2026-09-30", 0.80)
        rec_path = tmp_path / "data" / "shadow" / "skew_gate_vision.jsonl"
        assert rec_path.exists(), "影子 jsonl 必须落盘"
        return json.loads(rec_path.read_text(encoding="utf-8").strip().splitlines()[-1])

    def test_930_replay_divergent(self, monkeypatch, tmp_path):
        """9/30 实况复刻: batch 6B/1S=86% 砍 vs portfolio 9B/4S=69% 放 → divergent。"""
        prior = [{"symbol": s, "direction": d, "skip_reason": ""} for s, d in [
            ("US.COIN", "BUY"), ("US.SNDK", "BUY"), ("US.JBL", "BUY"),
            ("US.KMX", "SELL_SHORT"), ("US.BE", "SELL_SHORT"), ("US.ARM", "SELL_SHORT")]]
        rec = self._emit(monkeypatch, tmp_path, prior, _skewed_batch())
        assert rec["batch_vision"]["buy"] == 6 and rec["batch_vision"]["short"] == 1
        assert rec["batch_vision"]["pct"] == 0.8571
        assert rec["batch_vision"]["violates"] is True
        pv = rec["portfolio_vision"]
        assert (pv["buy"], pv["short"]) == (9, 4)
        assert pv["pct"] == 0.6923
        assert pv["violates"] is False, "并集 69% ≤80% 本不该砍 — 这正是误杀证据"
        assert rec["divergent"] is True
        assert rec["note"].startswith("record-only")

    def test_symbol_dedup_no_double_count(self, monkeypatch, tmp_path):
        """本批 symbol 已在 DB (UPSERT 重跑场景) → prior 剔除, 不双计。"""
        prior = [{"symbol": "US.AMAT", "direction": "BUY", "skip_reason": ""}]
        valid = [_Inst("US.AMAT", "BUY"), _Inst("US.B1", "BUY"), _Inst("US.S0", "SELL_SHORT")]
        rec = self._emit(monkeypatch, tmp_path, prior, valid)
        assert rec["portfolio_vision"]["prior_buy"] == 0, "重叠 symbol 必须剔除"
        assert rec["portfolio_vision"]["buy"] == 2

    def test_invalidated_rows_excluded(self, monkeypatch, tmp_path):
        """终态作废行 (INVALIDATED_SKIP_REASONS) 不计入 portfolio prior。"""
        prior = [
            {"symbol": "US.X1", "direction": "BUY", "skip_reason": "thesis_drift_invalidate"},
            {"symbol": "US.X2", "direction": "BUY", "skip_reason": "TRIPWIRE_INVALIDATED"},
            {"symbol": "US.X3", "direction": "SELL_SHORT", "skip_reason": ""},
        ]
        valid = [_Inst("US.B1", "BUY"), _Inst("US.B2", "BUY"), _Inst("US.S0", "SELL_SHORT")]
        rec = self._emit(monkeypatch, tmp_path, prior, valid)
        assert rec["portfolio_vision"]["prior_buy"] == 0, "终态作废不得计入组合"
        assert rec["portfolio_vision"]["prior_short"] == 1

    def test_single_valid_no_false_violation(self, monkeypatch, tmp_path):
        """单条 valid: batch_pct=100% 但真实判定不砍 (len<=1 早返回) → violates 必须 False。"""
        rec = self._emit(monkeypatch, tmp_path, [], [_Inst("US.B1", "BUY")])
        assert rec["batch_vision"]["violates"] is False, "口径必须与真实 trim 行为对齐"
        assert rec["divergent"] is False

    def test_empty_today_skips(self, tmp_path, monkeypatch):
        """today 空 → 影子直接跳过, 不落盘不抛错 (fail-open)。"""
        monkeypatch.setattr(P, "__file__", str(tmp_path / "app" / "playbook" / "pipeline.py"))
        P._emit_skew_gate_vision_shadow([_Inst("US.B1", "BUY")], "", 0.80)
        assert not (tmp_path / "data" / "shadow" / "skew_gate_vision.jsonl").exists()

    def test_db_failure_fail_open(self, monkeypatch, tmp_path):
        """DB 读失败 → emit 自吞错, 不上穿 (guardrail 判定不受影响)。"""
        class _BoomDB:
            def get_instructions_by_date(self, d):
                raise sqlite3_error()

        def sqlite3_error():
            return RuntimeError("db locked")

        monkeypatch.setattr("app.playbook.db.PlaybookDB", lambda *a, **k: _BoomDB())
        monkeypatch.setattr(P, "__file__", str(tmp_path / "app" / "playbook" / "pipeline.py"))
        P._emit_skew_gate_vision_shadow(_skewed_batch(), "2026-09-30", 0.80)  # 不抛即过
        assert not (tmp_path / "data" / "shadow" / "skew_gate_vision.jsonl").exists()

    def test_append_only_idempotent_shape(self, monkeypatch, tmp_path):
        """append-only: 连续两次 emit → 2 行, 且 schema 键集稳定 (下游可解析)。"""
        class _FakeDB:
            def get_instructions_by_date(self, d):
                return []

        monkeypatch.setattr("app.playbook.db.PlaybookDB", lambda *a, **k: _FakeDB())
        monkeypatch.setattr(P, "__file__", str(tmp_path / "app" / "playbook" / "pipeline.py"))
        P._emit_skew_gate_vision_shadow(_skewed_batch(), "2026-09-30", 0.80)
        P._emit_skew_gate_vision_shadow(_skewed_batch(), "2026-09-30", 0.80)
        lines = (tmp_path / "data" / "shadow" / "skew_gate_vision.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2, "append-only 不得覆盖"
        r0, r1 = json.loads(lines[0]), json.loads(lines[1])
        assert set(r0) == set(r1) == {
            "ts", "date", "batch_vision", "portfolio_vision", "divergent",
            "batch_symbols", "threshold", "note"}
        assert r0["batch_symbols"] == sorted(r0["batch_symbols"])


# ── ③ 接线契约 (源级) ──────────────────────────────────────────────────
class TestWiringContract:
    def test_emit_called_after_early_return_before_skew_judgment(self):
        """影子调用点必须在 len(valid)<=1 早返回之后、真实偏斜判定之前。"""
        i_early = _SRC.find("if len(valid) <= 1:")
        i_emit = _SRC.find("_emit_skew_gate_vision_shadow(valid, today, max_direction_skew)")
        i_skew = _SRC.find("majority_pct = max(buy_count, short_count)")
        assert 0 < i_early < i_emit < i_skew, "影子须夹在早返回与真实判定之间"

    def test_builder_passes_today(self):
        """builder 调用 guardrail 必须传 today (否则影子恒跳过 = 死门)。"""
        assert "_apply_portfolio_guardrails(instructions, today=today)" in _SRC

    def test_call_site_has_fuse(self):
        """调用点必须包 try (双层防护: emit 自吞错 + 调用点保险丝)。"""
        i = _SRC.find("_emit_skew_gate_vision_shadow(valid, today, max_direction_skew)")
        ctx = _SRC[max(0, i - 200):i]
        assert "try:" in ctx, "调用点缺保险丝 — 遥测异常会杀交易路径"
