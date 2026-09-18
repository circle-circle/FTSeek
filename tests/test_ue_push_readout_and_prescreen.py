"""UE 推送读数对齐 + 输入侧资格口径标注 回归钉 (2026-09-18, 用户令「动」→「登记问题做修复」)。

覆盖四件 (每件都对应一个已发生过的失真/缺口, 同类改动敢回来就先红):
  ① `_gated_breakdown` 的分桶口径 — direction(SKIP) 必须排除、batch_decision 行必须排除、
     跨日行必须排除 (推送"执行闸"行的数据源);
  ② `_prescreen_marker` — 只有 REJECT 才出标记 (PASS/缺字段 → 空串), 理由原样透传;
  ③ `_annotate_prescreen_eligibility` — 已具名者按输入初筛三态标注; **材料外浮现者**
     补跑同一资格口径并标注 (`material_external`), REJECT 者能被标记看见; 异常时 fail-open
     (不标注、不崩、不改 direction);
  ④ `cmd_push` 端到端渲染 (临时 scan jsonl, dry-run 不推送) — 扫描期行含"结构不可交易N"、
     逐条体带上"⛔资格预筛未过(…)", 且注入计数剔除 SKIP 行。
"""
import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts import llm_universe_scan as m  # noqa: E402


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ── ① 执行闸分解 ────────────────────────────────────────────────────────────
class TestGatedBreakdown:
    def _patch_path(self, tmp_path, rows, date="2026-09-18"):
        p = tmp_path / "inject.jsonl"
        _write_jsonl(p, rows)
        return mock.patch.object(m, "_INJECT_REJECT_PATH", p)

    def test_excludes_direction_and_batch_rows_and_other_dates(self, tmp_path):
        rows = [
            {"date": "2026-09-18", "symbol": "A", "reject_reason": "streak_gate(3>2)", "stage": "filter"},
            {"date": "2026-09-18", "symbol": "B", "reject_reason": "low_atr(0.02<0.03, x)", "stage": "build"},
            {"date": "2026-09-18", "symbol": "C", "reject_reason": "direction(SKIP)", "stage": "filter"},
            {"date": "2026-09-18", "type": "batch_decision", "stage": "inject_batch", "reason": "complete"},
            {"date": "2026-09-17", "symbol": "D", "reject_reason": "conf(0.4<0.5)", "stage": "filter"},
        ]
        with self._patch_path(tmp_path, rows):
            line = m._gated_breakdown("2026-09-18")
        # 只剩 streak 1 + 低ATR 1; direction/批决策/跨日全被排除
        assert line == "执行闸: 拦2只 (streak闸1 / 低ATR闸1)"

    def test_empty_when_only_direction_rows(self, tmp_path):
        rows = [{"date": "2026-09-18", "symbol": "C", "reject_reason": "direction(SKIP)", "stage": "filter"}]
        with self._patch_path(tmp_path, rows):
            assert m._gated_breakdown("2026-09-18") == ""

    def test_missing_file_is_silent(self, tmp_path):
        with mock.patch.object(m, "_INJECT_REJECT_PATH", tmp_path / "nope.jsonl"):
            assert m._gated_breakdown("2026-09-18") == ""


# ── ② 逐条预筛标记 ──────────────────────────────────────────────────────────
class TestPrescreenMarker:
    def test_reject_renders_reasons(self):
        assert m._prescreen_marker({"prescreen_status": "REJECT", "prescreen_reasons": ["low_atr"]}) \
            == " ⛔资格预筛未过(low_atr)"

    def test_reject_without_reasons_still_marked(self):
        assert "⛔资格预筛未过(REJECT)" in m._prescreen_marker({"prescreen_status": "REJECT"})

    def test_pass_and_missing_are_silent(self):
        assert m._prescreen_marker({"prescreen_status": "PASS"}) == ""
        assert m._prescreen_marker({"prescreen_status": "UNKNOWN"}) == ""
        assert m._prescreen_marker({}) == ""


# ── ③ 资格口径标注 ──────────────────────────────────────────────────────────
_PS_IN = {
    "status": "ok",
    "tradeable": ["UBER"],
    "context_only": [{"symbol": "BABA", "status": "REJECT", "reasons": ["low_atr"]}],
    "unknown": [{"symbol": "XYZ", "status": "UNKNOWN", "reasons": []}],
}


class TestPrescreenEligibilityAnnotation:
    def test_covered_symbols_take_input_tristate(self):
        surfaced = [{"symbol": "UBER", "direction": "LONG"},
                    {"symbol": "BABA", "direction": "SKIP"}]
        with mock.patch.object(m, "_prescreen_input_symbols", side_effect=AssertionError("不该补跑")):
            m._annotate_prescreen_eligibility(surfaced, ps_in=_PS_IN)
        assert (surfaced[0]["prescreen_scope"], surfaced[0]["prescreen_status"]) == ("in_materials", "PASS")
        assert (surfaced[1]["prescreen_scope"], surfaced[1]["prescreen_status"]) == ("in_materials", "REJECT")
        assert surfaced[1]["prescreen_reasons"] == ["low_atr"]

    def test_material_external_reject_is_annotated_and_visible(self):
        """9/18 ZIM 实况型: 材料未具名 → 补跑同一口径 → 低ATR REJECT."""
        surfaced = [{"symbol": "ZIM", "direction": "LONG"}]
        fake = {"status": "ok", "tradeable": [], "context_only": [
            {"symbol": "ZIM", "status": "REJECT", "reasons": ["low_atr"]}], "unknown": []}
        with mock.patch.object(m, "_prescreen_input_symbols", return_value=fake) as mm:
            m._annotate_prescreen_eligibility(surfaced, ps_in=_PS_IN)
        assert mm.call_args[0][0] == ["ZIM"]          # 只对未覆盖者补跑
        s = surfaced[0]
        assert (s["prescreen_scope"], s["prescreen_status"]) == ("material_external", "REJECT")
        assert "⛔资格预筛未过(low_atr)" in m._prescreen_marker(s)

    def test_material_external_unknown_stays_unknown(self):
        """缺数 ≠ 拒绝: 补跑取不到数时标 UNKNOWN, 不得写成 REJECT."""
        surfaced = [{"symbol": "AAA", "direction": "LONG"}]
        fake = {"status": "unavailable", "tradeable": [], "context_only": [], "unknown": []}
        with mock.patch.object(m, "_prescreen_input_symbols", return_value=fake):
            m._annotate_prescreen_eligibility(surfaced, ps_in=_PS_IN)
        assert surfaced[0]["prescreen_status"] == "UNKNOWN"

    def test_fail_open_on_exception(self):
        surfaced = [{"symbol": "ZIM", "direction": "LONG"}]
        with mock.patch.object(m, "_prescreen_input_symbols", side_effect=RuntimeError("router down")):
            m._annotate_prescreen_eligibility(surfaced, ps_in=_PS_IN)  # 不得抛
        assert surfaced[0]["direction"] == "LONG"
        assert "prescreen_status" not in surfaced[0]

    def test_no_uncovered_symbols_skips_second_pass(self):
        surfaced = [{"symbol": "UBER", "direction": "LONG"}]
        with mock.patch.object(m, "_prescreen_input_symbols", side_effect=AssertionError("不该补跑")):
            m._annotate_prescreen_eligibility(surfaced, ps_in=_PS_IN)
        assert surfaced[0]["prescreen_scope"] == "in_materials"


# ── ④ cmd_push 端到端渲染 (dry-run, 不推送) ─────────────────────────────────
class TestPushRendering:
    def test_push_shows_structural_reject_and_excludes_skip_from_inject_count(self, tmp_path, capsys):
        target = "2026-09-19"   # 用未来日期: 生产库必无该日 universe_expansion 行 → 注入计数 0
        scan = tmp_path / "scan.jsonl"
        _write_jsonl(scan, [{
            "type": "scan", "date": target, "model_used": "glm-5.3", "prompt_version": "v2.13",
            "surfaced": [
                {"symbol": "UBER", "direction": "LONG", "confidence": 0.6, "self_tier": "HARD_EVENT",
                 "prescreen_scope": "in_materials", "prescreen_status": "PASS", "prescreen_reasons": []},
                {"symbol": "ZIM", "direction": "LONG", "confidence": 0.55, "self_tier": "HARD_EVENT",
                 "is_universe_external": True, "winner_loser": "WINNER",
                 "prescreen_scope": "material_external",
                 "prescreen_status": "REJECT", "prescreen_reasons": ["low_atr"]},
                {"symbol": "NFLX", "direction": "SKIP", "confidence": 0.35, "self_tier": "MARKET_CORROBORATED"},
            ],
        }])
        reject = tmp_path / "inject.jsonl"
        _write_jsonl(reject, [
            {"date": target, "symbol": "UBER", "reject_reason": "streak_gate(3>2)", "stage": "filter"},
            {"date": target, "symbol": "ZIM", "reject_reason": "low_atr(0.0262<0.03, x)", "stage": "build"},
        ])
        with mock.patch.object(m, "_SCAN_PATH", scan), \
                mock.patch.object(m, "_INJECT_REJECT_PATH", reject):
            rc = m.cmd_push(dry=True, date=target)
        out = capsys.readouterr().out
        assert rc == 0
        assert "扫描期可交易: 2只 (多2/空0) · 池外1 · SKIP1 · 结构不可交易1" in out
        assert "执行闸: 拦2只 (streak闸1 / 低ATR闸1)" in out
        assert "LONG ZIM 🌍外 WINNER 📌硬事件 ⛔资格预筛未过(low_atr)" in out
        assert "UBER" in out and "⛔" not in [ln for ln in out.splitlines() if "LONG UBER" in ln][0]
        assert "[dry-run] 未推送" in out    # 验证期间零推送副作用

    def test_push_skips_when_no_scan_record(self, tmp_path, capsys):
        with mock.patch.object(m, "_SCAN_PATH", tmp_path / "empty.jsonl"):
            assert m.cmd_push(dry=True, date="2026-09-19") == 0
        assert "无 scan 记录" in capsys.readouterr().out