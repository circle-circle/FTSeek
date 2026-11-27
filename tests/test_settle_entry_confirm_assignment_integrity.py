# -*- coding: utf-8 -*-
"""P0-3 结算器 assignment 完整性契约 (DEB-20261128 §14.5/§14.6, RFC-158 §10.2)。

根因: gate 跨线程 stale-snapshot 孤儿重建竞态对同一 (inst_id,arm) 写重复/冲突终态行
(实测 cohort 200 行 / 193 唯一键 / 7 重复键全 treatment; inst5638 CONFIRMED+ABANDONED
冲突但 trade1902 +41.34 真实)。RFC-158 §10.2 预注册口径 = resolved **assignment**,
非 jsonl 行数 ⇒ 结算器必须:
  1. 按 (inst_id,arm) 折叠唯一 assignment (缺 inst_id 回落 decision_id, 皆缺按行);
  2. 实际已平 PLAYBOOK_SIM 成交 = ground truth 优先 (即使终态行含 ABANDONED);
  3. 无成交 + 全 ABANDONED (treatment) ⇒ abandoned 净 0;
  4. 无成交 + 终态冲突 ⇒ unresolved + 哨兵 (不补 0, 不入 resolved 分母);
  5. 同键多 decision_id / 多成交命中 ⇒ identity 冲突哨兵 + unresolved (防御);
  6. assignment_integrity 块逐臂披露, total = 分臂和;
  7. main 的 tail_joined 取全部 tail_rows 的 decision_id (含 ABANDONED 行);
  8. §15: open trade 存在但未平 ⇒ unresolved（不得先记弃单净 0）；
  9. §15: still_triggers 非 bool（None/缺键/字符串/0/1）⇒ invalid + unresolved。
本文件只修计量实现的契约, 不改 RFC-158 判据阈值文本。
"""
import json

import pytest

from scripts import settle_entry_confirm_forward as st


def _row(inst, arm, still, did, date="2026-11-10", status="ok"):
    return {"inst_id": inst, "arm": arm, "still_triggers": still,
            "decision_id": did, "date": date, "status": status}


def _integrity(report, arm):
    return report["assignment_integrity"][arm]


# ── ① 重复 CONFIRMED 有成交: 计一次, net/SL 不翻倍 ─────────────────────────────
def test_duplicate_confirmed_with_trade_counts_once():
    rows = [_row(5539, "treatment", True, "d-1"), _row(5539, "treatment", True, "d-1")]
    joined = {"d-1": {"net": -100.0, "exit_reason": "SL_INITIAL"}}
    report = st.tail_objective(rows, joined)
    arm = report["arms"]["treatment"]
    assert arm["assigned_n"] == 1
    assert arm["resolved_n"] == 1
    assert arm["closed_trade_n"] == 1
    assert arm["net_sum"] == -100.0          # 逐行计会翻成 -200
    assert arm["sl_initial_n"] == 1          # 逐行计会翻成 2
    ig = _integrity(report, "treatment")
    assert ig["duplicate_assignment_n"] == 1
    assert ig["duplicate_extra_rows_n"] == 1
    assert ig["terminal_conflict_n"] == 0


# ── ② 重复 ABANDONED 无成交: 计一次, 净 0 ─────────────────────────────────────
def test_duplicate_abandoned_without_trade_counts_once_zero():
    rows = [_row(5539, "treatment", False, "d-1"), _row(5539, "treatment", False, "d-1")]
    report = st.tail_objective(rows, {})
    arm = report["arms"]["treatment"]
    assert arm["assigned_n"] == 1
    assert arm["resolved_n"] == 1
    assert arm["abandoned_n"] == 1
    assert arm["closed_trade_n"] == 0
    assert arm["net_sum"] == 0.0
    ig = _integrity(report, "treatment")
    assert ig["duplicate_assignment_n"] == 1
    assert ig["duplicate_extra_rows_n"] == 1


# ── ③ 终态冲突但有成交 ⇒ closed_trade ground truth (inst5638 真实案例) ──────────
def test_terminal_conflict_with_trade_resolves_to_closed_trade():
    rows = [_row(5638, "treatment", True, "d-1"), _row(5638, "treatment", False, "d-1")]
    joined = {"d-1": {"net": 41.34, "exit_reason": "SL_TRAIL_LOCK"}}
    report = st.tail_objective(rows, joined)
    arm = report["arms"]["treatment"]
    assert arm["resolved_n"] == 1
    assert arm["closed_trade_n"] == 1
    assert arm["abandoned_n"] == 0           # 不得把冲突弃单错记净 0
    assert arm["net_sum"] == 41.34
    ig = _integrity(report, "treatment")
    assert ig["terminal_conflict_n"] == 1
    assert ig["terminal_conflict_resolved_by_trade_n"] == 1
    assert ig["terminal_conflict_unresolved_n"] == 0


# ── ④ 终态冲突无成交 ⇒ unresolved: 不补 0, 不入 resolved 分母 ──────────────────
def test_terminal_conflict_without_trade_is_unresolved_never_zero():
    rows = [_row(5638, "treatment", True, "d-1"), _row(5638, "treatment", False, "d-1")]
    report = st.tail_objective(rows, {})
    arm = report["arms"]["treatment"]
    assert arm["assigned_n"] == 1
    assert arm["resolved_n"] == 0
    assert arm["unresolved_n"] == 1
    assert arm["abandoned_n"] == 0
    assert arm["closed_trade_n"] == 0
    assert arm["resolution_coverage"] == 0.0
    ig = _integrity(report, "treatment")
    assert ig["terminal_conflict_n"] == 1
    assert ig["terminal_conflict_unresolved_n"] == 1
    assert ig["terminal_conflict_resolved_by_trade_n"] == 0


# ── ⑤ 全 ABANDONED 但有成交 ⇒ closed_trade (HON trade1843 型) ──────────────────
def test_all_abandoned_with_trade_uses_ground_truth():
    rows = [_row(5547, "treatment", False, "d-1"), _row(5547, "treatment", False, "d-1")]
    joined = {"d-1": {"net": -168.27, "exit_reason": "OVERNIGHT_GAP_STOP"}}
    report = st.tail_objective(rows, joined)
    arm = report["arms"]["treatment"]
    assert arm["resolved_n"] == 1
    assert arm["closed_trade_n"] == 1
    assert arm["abandoned_n"] == 0
    assert arm["net_sum"] == -168.27
    ig = _integrity(report, "treatment")
    assert ig["terminal_conflict_n"] == 0    # 全 False 无冲突, 只是重复
    assert ig["duplicate_assignment_n"] == 1


# ── ⑥ 同键多 decision_id ⇒ identity 冲突哨兵 + unresolved (防御, 实测 0) ────────
def test_same_key_multiple_decision_ids_is_identity_conflict():
    rows = [_row(9001, "treatment", True, "d-1"), _row(9001, "treatment", True, "d-2")]
    joined = {"d-1": {"net": 10.0, "exit_reason": "TP"}}
    report = st.tail_objective(rows, joined)
    arm = report["arms"]["treatment"]
    assert arm["assigned_n"] == 1
    assert arm["resolved_n"] == 0
    assert arm["unresolved_n"] == 1
    assert _integrity(report, "treatment")["identity_conflict_n"] == 1


# ── ⑦ 缺 inst_id 回落 decision_id: 唯一 did 不折叠 / 同 did 折叠 ────────────────
def test_missing_inst_id_falls_back_to_decision_id():
    # 旧 fixture 形态: 无 inst_id, 每行唯一 did ⇒ 逐行语义不变 (既有 5 tail 测试的基础)
    rows = [{"arm": "treatment", "status": "ok", "date": "2026-11-10",
             "decision_id": f"d-{i}", "still_triggers": False} for i in range(3)]
    report = st.tail_objective(rows, {})
    arm = report["arms"]["treatment"]
    assert arm["assigned_n"] == 3
    assert arm["abandoned_n"] == 3
    assert _integrity(report, "treatment")["duplicate_assignment_n"] == 0
    # 同 did 重复行 (无 inst_id) ⇒ 仍按 did 折叠
    dup = [dict(rows[0]), dict(rows[0])]
    report2 = st.tail_objective(dup, {})
    assert report2["arms"]["treatment"]["assigned_n"] == 1
    assert report2["arms"]["treatment"]["abandoned_n"] == 1
    assert _integrity(report2, "treatment")["duplicate_extra_rows_n"] == 1


# ── ⑧ 缺 inst_id 且缺 decision_id ⇒ 按行, 不折叠 ───────────────────────────────
def test_missing_both_ids_keeps_per_row_semantics():
    rows = [{"arm": "treatment", "status": "ok", "date": "2026-11-10",
             "still_triggers": False} for _ in range(2)]
    report = st.tail_objective(rows, {})
    arm = report["arms"]["treatment"]
    assert arm["assigned_n"] == 2
    assert arm["abandoned_n"] == 2
    assert _integrity(report, "treatment")["duplicate_assignment_n"] == 0


# ── ⑨ holdout 永不 abandoned-resolved ─────────────────────────────────────────
def test_holdout_never_resolves_as_abandoned():
    rows = [_row(7001, "holdout", False, "h-1"),      # 无成交 ⇒ unresolved, 不补 0
            _row(7002, "holdout", True, "h-2")]
    joined = {"h-2": {"net": 1.0, "exit_reason": "TP"}}
    report = st.tail_objective(rows, joined)
    arm = report["arms"]["holdout"]
    assert arm["assigned_n"] == 2
    assert arm["resolved_n"] == 1
    assert arm["unresolved_n"] == 1
    assert arm["abandoned_n"] == 0
    assert arm["closed_trade_n"] == 1


# ── ⑩ holdout 终态冲突无成交 ⇒ unresolved + 哨兵 ───────────────────────────────
def test_holdout_terminal_conflict_without_trade_is_unresolved():
    rows = [_row(7001, "holdout", True, "h-1"), _row(7001, "holdout", False, "h-1")]
    report = st.tail_objective(rows, {})
    arm = report["arms"]["holdout"]
    assert arm["resolved_n"] == 0
    assert arm["unresolved_n"] == 1
    assert arm["abandoned_n"] == 0
    ig = _integrity(report, "holdout")
    assert ig["terminal_conflict_n"] == 1
    assert ig["terminal_conflict_unresolved_n"] == 1


# ── ⑪ 配对日用去重 assignment: 重复 SL 不得稀释 treatment 亏损负担 ──────────────
def test_paired_day_uses_deduped_assignments():
    # treatment 日 D: instA 重复 CONFIRMED SL -100 (2 行) + instB ABANDONED 净 0 (1 行)
    # holdout 日 D: -100 SL (1 行)
    # 去重: t 负担 = 100/2 = 50 ⇒ daily SL reduction = 100-50 = 50
    # 逐行 (变异): t 负担 = 200/3 ≈ 66.67 ⇒ reduction ≈ 33.33 (假改善)
    rows = [_row(8001, "treatment", True, "tA"), _row(8001, "treatment", True, "tA"),
            _row(8002, "treatment", False, "tB"),
            _row(8003, "holdout", True, "hH")]
    joined = {"tA": {"net": -100.0, "exit_reason": "SL_INITIAL"},
              "hH": {"net": -100.0, "exit_reason": "SL_INITIAL"}}
    report = st.tail_objective(rows, joined)
    assert report["arms"]["treatment"]["resolved_n"] == 2
    assert report["arms"]["treatment"]["sl_initial_loss_per_resolved_assignment"] == 50.0
    daily = report["daily_paired"]
    assert daily["sl_initial_loss_burden_reduction"]["n_days"] == 1
    assert daily["sl_initial_loss_burden_reduction"]["mean"] == 50.0
    assert daily["total_net_per_assignment_delta"]["mean"] == 50.0


# ── ⑫ integrity total = 分臂和 ────────────────────────────────────────────────
def test_integrity_total_equals_sum_of_arms():
    rows = [_row(8001, "treatment", True, "tA"), _row(8001, "treatment", True, "tA"),
            _row(8003, "holdout", True, "hH"), _row(8003, "holdout", False, "hH")]
    joined = {"tA": {"net": -100.0, "exit_reason": "SL_INITIAL"}}
    report = st.tail_objective(rows, joined)
    ig = report["assignment_integrity"]
    for key in ig["treatment"]:
        assert ig["total"][key] == ig["treatment"][key] + ig["holdout"][key]
    # holdout 冲突组同为 2 行同键 ⇒ 也计 duplicate; total = treatment 1 + holdout 1
    assert ig["total"]["duplicate_assignment_n"] == 2
    assert ig["total"]["terminal_conflict_n"] == 1
    assert ig["total"]["terminal_conflict_unresolved_n"] == 1
    assert "DEB-20261128" in ig["rule"]


# ── ⑬ 空 cohort: integrity 全 0, verdict TOO_EARLY ─────────────────────────────
def test_empty_cohort_integrity_all_zero_and_too_early():
    report = st.tail_objective([], {})
    assert report["verdict"] == "TOO_EARLY"
    ig = report["assignment_integrity"]
    for arm in ("treatment", "holdout", "total"):
        assert all(v == 0 for v in ig[arm].values())
    assert report["arms"]["treatment"]["assigned_n"] == 0
    assert report["arms"]["treatment"]["resolution_coverage"] is None


# ── ⑭ main  spy: tail_joined 取全部 did (含 ABANDONED 行) ───────────────────────
def test_main_tail_joined_includes_abandoned_decision_ids(tmp_path, monkeypatch):
    # instA: 全 ABANDONED 重复行但 gate 竞态有成交 (HON trade1843 型) ⇒ 必须按 ground truth 对账
    rows = [_row(5547, "treatment", False, "ab-1"), _row(5547, "treatment", False, "ab-1"),
            _row(5539, "treatment", True, "cf-1"),
            _row(7001, "holdout", True, "h-1")]
    joined = {"ab-1": {"net": -168.27, "exit_reason": "OVERNIGHT_GAP_STOP"},
              "cf-1": {"net": 10.0, "exit_reason": "TP"},
              "h-1": {"net": 1.0, "exit_reason": "TP"}}
    for row in rows:
        row.update({"schema": st.SCHEMA_V2, "type": "entry_confirm", "symbol": "US.TAIL",
                    "direction": "LONG", "opp_cost_pct": 0.1})
    fwd = tmp_path / "forward.jsonl"
    fwd.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    out = tmp_path / "latest.json"
    monkeypatch.setattr(st, "FWD", fwd)
    monkeypatch.setattr(st, "OUT", out)
    monkeypatch.setattr(st, "load_sim_pnl", lambda _: [])
    monkeypatch.setattr(st, "load_fills", lambda _: {})
    calls = []

    def _spy(ids):
        ids = list(ids)
        calls.append(ids)
        return {d: joined[d] for d in ids if d in joined}

    monkeypatch.setattr(st, "load_trade_by_decision", _spy)
    monkeypatch.setattr(st, "load_trade_existence_by_decision",
                        lambda ids: set(joined).intersection(map(str, ids)))
    assert st.main() == 0
    # rfc153_progress 另有一次调用 (本 fixture 无 FADE_SHORT ⇒ 空列表);
    # 钉死: 含 confirmed did 的那次 tail 调用必须同时含 abandoned did。
    tail_calls = [ids for ids in calls if "cf-1" in ids]
    assert len(tail_calls) == 1
    assert "ab-1" in tail_calls[0]
    assert "h-1" in tail_calls[0]
    report = json.loads(out.read_text(encoding="utf-8"))
    tail = report["current_cohort"]["tail_objective"]
    arm = tail["arms"]["treatment"]
    assert arm["assigned_n"] == 2            # 3 行 treatment 折叠为 2 assignment
    assert arm["closed_trade_n"] == 2        # 全弃单有成交也按 ground truth 计
    assert arm["abandoned_n"] == 0
    assert arm["net_sum"] == pytest.approx(-158.27)
    assert tail["assignment_integrity"]["treatment"]["duplicate_assignment_n"] == 1


# ── ⑮ resolution_contract 钉死 dedup + ground-truth 口径 ───────────────────────
def test_resolution_contract_pins_dedup_and_ground_truth():
    report = st.tail_objective([], {})
    contract = report["resolution_contract"]
    assert "resolved assignment (not jsonl rows)" in contract
    assert "dedup by (inst_id,arm)" in contract
    assert "ground-truth priority" in contract
    assert "never zero" in contract


# ── ⑯ assigned_n = 唯一键数 (非行数); status!=ok 不进人群 ───────────────────────
def test_assigned_n_counts_unique_keys_not_rows():
    rows = [_row(8001, "treatment", False, "tA")] * 3          # 同键 3 行 ⇒ 1 assignment
    rows = [dict(r) for r in rows]
    rows.append(_row(8002, "treatment", False, "tB"))          # 第二键
    rows.append(_row(8001, "treatment", False, "tA", status="error"))  # 非 ok 不进人群
    rows += [_row(8003, "holdout", True, "hH"), _row(8003, "holdout", True, "hH")]
    joined = {"hH": {"net": 1.0, "exit_reason": "TP"}}
    report = st.tail_objective(rows, joined)
    assert report["arms"]["treatment"]["assigned_n"] == 2
    assert report["arms"]["holdout"]["assigned_n"] == 1
    assert report["arms"]["treatment"]["abandoned_n"] == 2
    assert _integrity(report, "treatment")["duplicate_extra_rows_n"] == 2


# ── ⑰ 全 ABANDONED + open trade ⇒ unresolved，不得先记实际净 0 ─────────────────
def test_all_abandoned_with_open_trade_is_unresolved():
    rows = [_row(9001, "treatment", False, "d-open"),
            _row(9001, "treatment", False, "d-open")]
    report = st.tail_objective(rows, {}, {"d-open"})
    arm = report["arms"]["treatment"]
    assert arm["assigned_n"] == 1
    assert arm["resolved_n"] == 0
    assert arm["unresolved_n"] == 1
    assert arm["abandoned_n"] == 0
    assert arm["net_sum"] == 0.0
    ig = _integrity(report, "treatment")
    assert ig["open_trade_unresolved_n"] == 1
    assert ig["terminal_state_invalid_n"] == 0


# ── ⑱ 终态冲突 + open trade ⇒ unresolved，同时保留两类哨兵 ─────────────────────
def test_terminal_conflict_with_open_trade_is_unresolved():
    rows = [_row(9002, "treatment", True, "d-open"),
            _row(9002, "treatment", False, "d-open")]
    report = st.tail_objective(rows, {}, {"d-open"})
    arm = report["arms"]["treatment"]
    assert arm["resolved_n"] == 0
    assert arm["unresolved_n"] == 1
    ig = _integrity(report, "treatment")
    assert ig["open_trade_unresolved_n"] == 1
    assert ig["terminal_conflict_n"] == 1
    assert ig["terminal_conflict_unresolved_n"] == 1


# ── ⑲ holdout + open trade 同样 unresolved ────────────────────────────────────
def test_holdout_with_open_trade_is_unresolved():
    report = st.tail_objective(
        [_row(9003, "holdout", True, "h-open")], {}, {"h-open"})
    arm = report["arms"]["holdout"]
    assert arm["resolved_n"] == 0
    assert arm["unresolved_n"] == 1
    assert arm["abandoned_n"] == 0
    assert _integrity(report, "holdout")["open_trade_unresolved_n"] == 1


# ── ⑳ closed trade 与 existence 同时存在时，已平净额仍优先 ─────────────────────
def test_closed_trade_takes_priority_over_open_existence_marker():
    joined = {"d-1": {"net": -25.0, "exit_reason": "SL_INITIAL"}}
    report = st.tail_objective(
        [_row(9004, "treatment", False, "d-1")], joined, {"d-1"})
    arm = report["arms"]["treatment"]
    assert arm["resolved_n"] == 1
    assert arm["closed_trade_n"] == 1
    assert arm["net_sum"] == -25.0
    assert _integrity(report, "treatment")["open_trade_unresolved_n"] == 0


# ── ㉑ 非 bool still_triggers 一律 invalid + unresolved，不归零 ─────────────────
@pytest.mark.parametrize("bad", [None, "false", "true", 0, 1, []])
def test_non_bool_terminal_state_is_invalid_unresolved(bad):
    report = st.tail_objective([_row(9005, "treatment", bad, "d-bad")], {})
    arm = report["arms"]["treatment"]
    assert arm["resolved_n"] == 0
    assert arm["unresolved_n"] == 1
    assert arm["abandoned_n"] == 0
    assert arm["net_sum"] == 0.0
    assert _integrity(report, "treatment")["terminal_state_invalid_n"] == 1


def test_missing_terminal_state_is_invalid_unresolved():
    row = _row(9006, "treatment", False, "d-missing")
    del row["still_triggers"]
    report = st.tail_objective([row], {})
    assert report["arms"]["treatment"]["resolved_n"] == 0
    assert report["arms"]["treatment"]["abandoned_n"] == 0
    assert _integrity(report, "treatment")["terminal_state_invalid_n"] == 1


# ── ㉒ main 同时接 closed PnL 与 open existence，且 open 不进 resolved ──────────
def test_main_tail_uses_open_trade_existence(tmp_path, monkeypatch):
    rows = [_row(9007, "treatment", False, "open-1")]
    for row in rows:
        row.update({"schema": st.SCHEMA_V2, "type": "entry_confirm", "symbol": "US.TAIL",
                    "direction": "LONG", "opp_cost_pct": 0.1})
    fwd = tmp_path / "forward.jsonl"
    fwd.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    out = tmp_path / "latest.json"
    monkeypatch.setattr(st, "FWD", fwd)
    monkeypatch.setattr(st, "OUT", out)
    monkeypatch.setattr(st, "load_sim_pnl", lambda _: [])
    monkeypatch.setattr(st, "load_fills", lambda _: {})
    monkeypatch.setattr(st, "load_trade_by_decision", lambda ids: {})
    existence_calls = []

    def _existence(ids):
        existence_calls.append(list(ids))
        return {"open-1"}

    monkeypatch.setattr(st, "load_trade_existence_by_decision", _existence)
    assert st.main() == 0
    assert existence_calls == [["open-1"]]
    tail = json.loads(out.read_text(encoding="utf-8"))["current_cohort"]["tail_objective"]
    arm = tail["arms"]["treatment"]
    assert arm["assigned_n"] == 1
    assert arm["resolved_n"] == 0
    assert arm["unresolved_n"] == 1
    assert arm["abandoned_n"] == 0
    assert tail["assignment_integrity"]["treatment"]["open_trade_unresolved_n"] == 1
    assert "open trade" in tail["resolution_contract"]
