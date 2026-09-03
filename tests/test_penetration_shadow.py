#!/usr/bin/env python3
"""穿透影子 (penetration_shadow) 契约测试 (2026-09-03, DEB-20260903 议题二后续, 用户裁"A").

钉住的不变量:
  1. record-only 安全: 任何垃圾输入不抛异常、不落盘 (与 exit_shadow 同款铁律)
  2. TEST 符号守卫: 测试样本绝不污染生产影子 jsonl
  3. 特征口径: depth_bps 方向镜像符号 / fresh_extreme 判定
  4. 回填标签正确性: 手工构造 bar 验 recovered_Nm / 可见窗不足=null(防假零) / 幂等
  5. 源级契约: pipeline 埋点在场; 影子键不进 exit config 哈希 (KR2 红线);
     feature flag 默认 on; shadow_board 段已注册 (防遗忘)
"""
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.playbook import penetration_shadow as ps  # noqa: E402


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── 1. record-only 安全不变量 ──────────────────────────────
def test_record_never_raises_on_garbage(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "_PEN_PATH", tmp_path / "pen.jsonl")
    for kwargs in (
        dict(symbol="DELL", direction="LONG", exit_reason="SL_BREAKEVEN", side="passive",
             path="marketable_limit", trigger_price=None, floor_px=474.65, entry_price=470.0,
             peak_price=480.0, trough_price=472.0, held_minutes=6.4, quote_src="x"),
        dict(symbol="DELL", direction="LONG", exit_reason="SL_BREAKEVEN", side="passive",
             path="marketable_limit", trigger_price=473.12, floor_px=0, entry_price=470.0,
             peak_price=480.0, trough_price=472.0, held_minutes=6.4, quote_src="x"),
        dict(symbol=None, direction=None, exit_reason=None, side=None, path=None,
             trigger_price="abc", floor_px=[], entry_price={}, peak_price=None,
             trough_price=None, held_minutes=None, quote_src=None),
    ):
        assert ps.record_penetration(**kwargs) is False
    assert not (tmp_path / "pen.jsonl").exists()


# ── 2. TEST 符号守卫 ───────────────────────────────────────
def test_test_symbol_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "_PEN_PATH", tmp_path / "pen.jsonl")
    ok = ps.record_penetration(symbol="US.TEST", direction="LONG", exit_reason="SL_BREAKEVEN",
                               side="passive", path="marketable_limit", trigger_price=99.0,
                               floor_px=100.0, entry_price=98.0, peak_price=101.0,
                               trough_price=98.5, held_minutes=5, quote_src="t")
    assert ok is False and not (tmp_path / "pen.jsonl").exists()


# ── 3. 特征口径: depth_bps 方向镜像 + 字段完整性 ────────────
def _capture(tmp_path, monkeypatch, **over):
    monkeypatch.setattr(ps, "_PEN_PATH", tmp_path / "pen.jsonl")
    monkeypatch.setattr(ps, "_atr_pct", lambda s, p: 2.0)  # 隔离 DI provider
    kw = dict(symbol="DELL", direction="LONG", exit_reason="SL_TRAIL_LOCK", side="passive",
              path="marketable_limit", trigger_price=473.12, floor_px=474.65,
              entry_price=470.0, peak_price=480.0, trough_price=473.15,
              held_minutes=6.4, quote_src="futu_lv3_push", inst_id="i1",
              telemetry={"limit_px": 472.41, "pad_bps": 15.0})
    kw.update(over)
    assert ps.record_penetration(**kw) is True
    return json.loads((tmp_path / "pen.jsonl").read_text().splitlines()[-1])


def test_depth_bps_long_passive_positive(tmp_path, monkeypatch):
    row = _capture(tmp_path, monkeypatch)
    # LONG 被动侧: 地板474.65 > 触发473.12 → depth>0; giveback = (480-473.12)/470
    assert row["depth_bps"] > 0
    assert abs(row["giveback_bps"] - (480.0 - 473.12) / 470.0 * 10000) < 0.5
    for k in ("event_id", "et_date", "et_hm", "mfe_pct", "fresh_extreme",
              "minutes_since_open", "atr_pct", "depth_atr_ratio", "limit_px"):
        assert k in row, f"缺特征键 {k}"
    assert row["atr_pct"] == 2.0 and row["depth_atr_ratio"] is not None
    assert row["fresh_extreme"] is True  # 473.12 <= 473.15*1.0002(≈473.245) → 仍在刷低


def test_depth_bps_short_mirrored(tmp_path, monkeypatch):
    row = _capture(tmp_path, monkeypatch, direction="SHORT", side="active",
                   trigger_price=476.0, floor_px=474.65, trough_price=480.0)
    # SHORT 检测价高于地板=被动几何 → depth>0; 但 side 字段如实记录调用方判定
    assert row["depth_bps"] > 0
    assert row["direction"] == "SHORT"


def test_atr_failopen_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "_PEN_PATH", tmp_path / "pen.jsonl")
    monkeypatch.setattr(ps, "_atr_pct", lambda s, p: None)
    kw = dict(symbol="DELL", direction="LONG", exit_reason="SL_BREAKEVEN", side="passive",
              path="marketable_limit", trigger_price=473.12, floor_px=474.65,
              entry_price=470.0, peak_price=480.0, trough_price=473.0,
              held_minutes=6.4, quote_src="x")
    assert ps.record_penetration(**kw) is True
    row = json.loads((tmp_path / "pen.jsonl").read_text().splitlines()[-1])
    assert row["atr_pct"] is None and row["depth_atr_ratio"] is None


# ── 4. 回填标签正确性 ──────────────────────────────────────
def _mk_bars(start_hm="14:58", n=12, pattern="v_at_3"):
    """手工构造 bar: [time,o,h,l,c]。pattern: v_at_3=第3根high回摆过地板; down=一路阴跌;
    pre_trigger_spike=触发分钟bar自身high过地板(触发前价格, 端标口径下属污染源)。"""
    bars = []
    h, m = int(start_hm[:2]), int(start_hm[3:5])
    for i in range(n):
        mm = h * 60 + m + i + 1
        t = f"2026-09-02 {mm//60:02d}:{mm % 60:02d}:00"
        if pattern == "v_at_3":
            high = 101.0 if i == 2 else 99.5  # 第3根(after索引2) high 触100地板
            low = 99.0
        elif pattern == "pre_trigger_spike":
            # 第1根=触发分钟bar(端标14:59覆盖14:58分钟): 其high 101为触发前价格
            high, low = (101.0, 99.0) if i == 0 else (99.5, 99.0)
        else:
            high, low = 99.5 - i * 0.1, 99.0 - i * 0.1
        bars.append([t, 99.8, high, low, 99.2])
    return bars


def test_backfill_v_recovery_labels():
    pob = _load_script("penetration_outcome_backfill")
    ev = {"direction": "LONG", "floor_px": 100.0, "trigger_price": 99.8,
          "entry": 99.0, "et_hm": "14:58", "limit_px": 99.65}
    lab = pob.label_one(ev, _mk_bars(pattern="v_at_3"))
    assert lab["status"] == "labeled"
    assert lab["recovered_1m"] is False and lab["recovered_3m"] is True
    assert lab["recovered_5m"] is True and lab["recovered_10m"] is True
    # cf = (100-99.65)/99*10000 ≈ 35.35
    assert abs(lab["cf_floor_fill_saved_bps"] - (100.0 - 99.65) / 99.0 * 10000) < 0.5


def test_backfill_continued_down_labels():
    pob = _load_script("penetration_outcome_backfill")
    ev = {"direction": "LONG", "floor_px": 100.0, "trigger_price": 99.8,
          "entry": 99.0, "et_hm": "14:58", "limit_px": 99.65}
    lab = pob.label_one(ev, _mk_bars(pattern="down"))
    assert lab["recovered_10m"] is False
    assert lab["continued_bps_10m"] < 0
    assert lab["cf_floor_fill_saved_bps"] is None  # 未回摆 → 无挽回可计


def test_backfill_partial_window_null_not_false():
    pob = _load_script("penetration_outcome_backfill")
    ev = {"direction": "LONG", "floor_px": 100.0, "trigger_price": 99.8,
          "entry": 99.0, "et_hm": "14:58"}
    lab = pob.label_one(ev, _mk_bars(n=4, pattern="down"))
    # n=4 根 bar (端标14:59-15:02), 触发分钟bar'14:59'被跳过 → 可见 3 根
    assert lab["partial"] is True and lab["n_bars_scanned"] == 3
    # 可见窗<10 → recovered_10m 必须 null 而非 False (防假零, DEB-20260902 口径)
    assert lab["recovered_10m"] is None
    assert lab["recovered_1m"] is False and lab["recovered_3m"] is False


def test_backfill_trigger_minute_bar_not_counted():
    """端标 off-by-one 钉 (DELL 9/2 复盘核查发现): 触发分钟 bar 的 high 先于触发时刻,
    绝不可计入 V-recovery 标签 — 否则被动侧击穿样本被系统性误标为 V 型。"""
    pob = _load_script("penetration_outcome_backfill")
    ev = {"direction": "LONG", "floor_px": 100.0, "trigger_price": 99.8,
          "entry": 99.0, "et_hm": "14:58", "limit_px": 99.65}
    lab = pob.label_one(ev, _mk_bars(pattern="pre_trigger_spike"))
    for n in (1, 3, 5, 10):
        assert lab[f"recovered_{n}m"] is False, f"recovered_{n}m 被触发分钟bar污染"
    assert lab["cf_floor_fill_saved_bps"] is None  # 未回摆 → 无挽回可计
    assert lab["n_bars_scanned"] == 10 and lab["partial"] is False


def test_backfill_hour_rollover_anchor():
    """触发分钟 14:59 → 锚点须跨小时到 15:00, 不可用字符串+1。"""
    pob = _load_script("penetration_outcome_backfill")
    ev = {"direction": "LONG", "floor_px": 100.0, "trigger_price": 99.8,
          "entry": 99.0, "et_hm": "14:59", "limit_px": 99.65}
    bars = _mk_bars(start_hm="14:59", n=6, pattern="v_at_3")  # 端标15:00-15:05
    lab = pob.label_one(ev, bars)
    # 触发分钟bar'15:00'被跳过, 窗口从'15:01'起 (5根); v_at_3的101在第3根(端标15:02) → 3m内回摆
    assert lab["status"] == "labeled" and lab["n_bars_scanned"] == 5
    assert lab["recovered_1m"] is False and lab["recovered_3m"] is True


def test_backfill_idempotent(tmp_path, monkeypatch):
    pob = _load_script("penetration_outcome_backfill")
    shadow = tmp_path / "pen.jsonl"
    latest = tmp_path / "latest.json"
    monkeypatch.setattr(pob, "SHADOW", shadow)
    monkeypatch.setattr(pob, "OUT_LATEST", latest)
    monkeypatch.setattr(pob, "load_bars", lambda s, d: _mk_bars(pattern="v_at_3"))
    ev = {"type": "penetration", "event_id": "X-1", "et_date": "2026-09-02",
          "et_hm": "14:58", "symbol": "DELL", "direction": "LONG", "side": "passive",
          "exit_reason": "SL_BREAKEVEN", "depth_bps": 20.0,
          "floor_px": 100.0, "trigger_price": 99.8, "entry": 99.0, "limit_px": 99.65}
    shadow.write_text(json.dumps(ev) + "\n")
    monkeypatch.setattr(sys, "argv", ["x"])
    pob.main()
    pob.main()  # 第二轮幂等
    rows = [json.loads(l) for l in shadow.read_text().splitlines()]
    assert sum(1 for r in rows if r.get("type") == "outcome") == 1


# ── 5. 判分器 (LLM出题代码判卷) ────────────────────────────
def test_scorer_match_and_bad_op():
    pla = _load_script("penetration_llm_analysis")
    samples = [
        {"depth_bps": 35.0, "fresh_extreme": True, "recovered_5m": False},
        {"depth_bps": 10.0, "fresh_extreme": False, "recovered_5m": True,
         "cf_floor_fill_saved_bps": 40.0},
    ]
    hyp = {"conditions": [{"field": "depth_bps", "op": "lt", "value": 20}], "predict": "V"}
    sc = pla.score_hypothesis(hyp, samples)
    assert sc["matched"] == 1 and sc["v_rate"] == 1.0 and sc["avg_cf_saved_bps"] == 40.0
    bad = {"conditions": [{"field": "depth_bps", "op": "between", "value": 1}]}
    assert pla.score_hypothesis(bad, samples)["matched"] == 0
    # None 特征值 → 不命中 (None 不是 0)
    none_sample = [{"depth_bps": None, "recovered_5m": True}]
    assert pla.score_hypothesis(hyp, none_sample)["matched"] == 0


# ── 6. 源级契约 ────────────────────────────────────────────
def test_pipeline_hook_present():
    src = (REPO / "app" / "playbook" / "pipeline.py").read_text(encoding="utf-8")
    idx = src.find("record_penetration")
    assert idx > 0, "pipeline 净保本限价块必须埋 record_penetration"
    block = src[max(0, idx - 800):idx]
    assert '_be_close_telemetry' in block, "埋点必须位于保本遥测块上下文 (在场景变量齐全)"


def test_penetration_keys_not_in_exit_config_snapshot():
    """KR2 红线: 影子键不进 exit config 哈希 (record-only 不影响已实现PnL)。"""
    src = (REPO / "app" / "playbook" / "config.py").read_text(encoding="utf-8")
    assert "penetration" not in src, "config.py 不得出现 penetration 键 (防进哈希)"


def test_feature_flag_default_on():
    from app.llm.features.feature_flags import _DEFAULT_FLAGS
    assert _DEFAULT_FLAGS.get("penetration_shadow") is True


def test_shadow_board_section_registered():
    src = (REPO / "scripts" / "shadow_board.py").read_text(encoding="utf-8")
    assert "_sec_penetration" in src
    idx = src.find("def build_board")
    assert "_sec_penetration" in src[idx:idx + 600], "段必须注册进 build_board"
