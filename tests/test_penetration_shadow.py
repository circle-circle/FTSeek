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


def test_validate_hypotheses_drops_malformed_whole_hypothesis():
    """2026-10-24 事故钉: LLM 把条件写成自然语言字符串 ⇒ _match 的 c.get() 崩,
    连带确定性基线整份周报归零。纪律 = 整条假设丢弃 (不删单个条件: 删条件会放宽
    命中集, 等价于把毕业门变松), 且必须留计数。"""
    pla = _load_script("penetration_llm_analysis")
    good = {"conditions": [{"field": "depth_bps", "op": "ge", "value": "30"}], "predict": "DOWN"}
    str_cond = {"conditions": ["深度>300bps 且刷新低"], "predict": "DOWN"}
    mixed = {"conditions": [{"field": "side", "op": "eq", "value": "passive"},
                            "held_minutes < 5"], "predict": "V"}
    ok, dropped = pla.validate_hypotheses([good, str_cond, mixed, "not-a-dict"])
    assert ok == [good], f"合格集应只含形状合法项, 实得 {ok}"
    assert len(dropped) == 3
    # 形状不合法的绝不能进判分器 (修前: mixed/str_cond 会抛 AttributeError)
    assert pla.score_hypothesis(good, [{"depth_bps": 35.0, "recovered_5m": True}])["matched"] == 1


def test_validate_hypotheses_rejects_empty_unknown_and_bad_value():
    pla = _load_script("penetration_llm_analysis")
    bads = [
        {"conditions": []},                                            # 空条件 = 匹配全集
        {"conditions": [{"field": "vix", "op": "gt", "value": 20}]},    # 白名单外字段
        {"conditions": [{"field": "depth_bps", "op": "gt", "value": "深"}]},  # 数值不可转
        {"conditions": [{"field": "fresh_extreme", "op": "gt", "value": True}]},  # 类别字段只允许 eq
        {"conditions": "depth_bps>30"},                                # conditions 非数组
    ]
    ok, dropped = pla.validate_hypotheses(bads)
    assert ok == [] and len(dropped) == len(bads)


def test_main_survives_string_condition(tmp_path, monkeypatch):
    """端到端回归: 修前 main() 在此输入上抛 AttributeError, 报告文件不落盘。"""
    pla = _load_script("penetration_llm_analysis")
    samples = [{"depth_bps": 35.0, "side": "passive", "recovered_5m": True},
               {"depth_bps": 5.0, "side": "active", "recovered_5m": False}]
    monkeypatch.setattr(pla, "load_samples", lambda: samples)
    monkeypatch.setattr(pla, "ask_llm", lambda _s: [
        {"conditions": ["depth_bps > 30"], "predict": "DOWN"},          # 脏: 字符串条件
        {"conditions": [{"field": "depth_bps", "op": "ge", "value": 30}], "predict": "V"},
    ])
    monkeypatch.setattr(pla, "MILESTONE_STATE", tmp_path / "ms.json")
    monkeypatch.setattr(pla, "OUT_LATEST", tmp_path / "latest.json")
    monkeypatch.setattr(pla, "REPORT_MD", tmp_path / "report.md")
    monkeypatch.setattr(pla, "_push", lambda _m: True)
    pla.main()
    rep = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert rep["hypotheses_dropped"] == 1
    assert len(rep["hypotheses_scored"]) == 1
    assert rep["baseline"]["overall"]["n"] == 2      # 确定性基线不再随 LLM 脏输出归零
    assert "丢弃形状不合法假设: 1" in (tmp_path / "report.md").read_text(encoding="utf-8")


# ── 5b. 看板读数 (2026-10-24 用户裁「同意」: 少数类缺口 + 丢弃数上板) ─────
def _board_with_latest(tmp_path, monkeypatch, latest):
    pla = _load_script("shadow_board")
    monkeypatch.setattr(pla, "SHADOW_DIR", tmp_path)
    (tmp_path / "penetration_shadow.jsonl").write_text(
        json.dumps({"type": "penetration", "event_id": "E1", "et_date": "2026-10-24",
                    "symbol": "DELL", "side": "passive", "exit_reason": "SL_BREAKEVEN",
                    "depth_bps": 30.0}) + "\n"
        + json.dumps({"type": "outcome", "event_id": "E1", "et_date": "2026-10-24",
                      "status": "labeled", "recovered_5m": True}) + "\n",
        encoding="utf-8")
    (tmp_path / "penetration_analysis_latest.json").write_text(
        json.dumps(latest, ensure_ascii=False), encoding="utf-8")
    return pla


def test_board_shows_minority_gap_of_closest_hypothesis(tmp_path, monkeypatch):
    """板上必须直接给出「还差几条少数类」, 且按缺口排序 — 强度序会挑出 nonv 更少的
    假设, 让缺口看起来更远 (2026-10-24 实测: 强度序 nonv=1 / 缺口序 nonv=5)。"""
    pla = _board_with_latest(tmp_path, monkeypatch, {
        "generated_at": "2026-10-24T08:17:15", "hypotheses_dropped": 0,
        "hypotheses_scored": [
            {"matched": 17, "v_rate": 0.941, "nonv_count": 1, "predict": "V"},
            {"matched": 18, "v_rate": 0.722, "nonv_count": 5, "predict": "V"},
            {"matched": 3, "v_rate": 1.0, "nonv_count": 0, "predict": "V"},   # 未过 matched 门
        ]})
    out = "\n".join(pla._sec_penetration())
    assert "matched=18" in out and "少数类5/8" in out
    assert "差3条到少数类门" in out


def test_board_distinguishes_dropped_from_no_pattern(tmp_path, monkeypatch):
    """零判分有两种成因 (真无规律 / LLM 输出被丢弃), 板上必须能区分 —— 2026-09-13
    就曾把「LLM 出题环零可判分假设」读成基线里没有规律。"""
    pla = _board_with_latest(tmp_path, monkeypatch, {
        "generated_at": "2026-10-24T08:17:15", "hypotheses_dropped": 2,
        "hypotheses_scored": []})
    out = "\n".join(pla._sec_penetration())
    assert "判分0条/丢弃2" in out and "非'无规律'" in out


# ── 6. 源级契约 ────────────────────────────────────────────
def test_pipeline_hook_present():
    src = (REPO / "app" / "playbook" / "pipeline_parts"
           / "exit_monitor_flow.py").read_text(encoding="utf-8")
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
    """penetration 段必须注册进看板 (RFC-109③: 段改表驱动 _SECTION_TABLE, 由
    build_board 迭代渲染; 原「build_board 后 600 字符内出现」的源码邻近锚点随重构失效,
    改为功能性断言 — 加载模块核对段确在渲染表内, 意图不变)。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_sb_pen", REPO / "scripts" / "shadow_board.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m._init_table()
    secs = [sec for sec, _entries in m._SECTION_TABLE]
    assert m._sec_penetration in secs, "penetration 段必须注册进看板渲染表"
    # 且该段挂的注册 id 必须真实存在于 shadow_registry (SSOT)
    pen_entries = dict(m._SECTION_TABLE)[m._sec_penetration]
    assert any(rid == "penetration_shadow" for rid, _a, _t in pen_entries)


# ── 6. 写读契约 (2026-09-03 复盘批): 落盘行必须带 status, 否则研究环/看板结构性静默零 ──
#    病灶: label_one 返回 {"status": "labeled", ...} 被 lab.pop("status") 摘掉后才 **lab 落盘,
#    而三个消费端 (analysis.load_samples / shadow_board 段11 / 本脚本 v_recovery 汇总) 都按
#    status=="labeled" 选行 → cron 照跑、jsonl 照长、看板照渲染, 但永远 0/20 不毕业。
def _run_backfill(tmp_path, monkeypatch, *, pattern="v_at_3", with_event=True):
    pob = _load_script("penetration_outcome_backfill")
    # 文件名必须与生产一致 — shadow_board 段11 按 SHADOW_DIR/"penetration_shadow.jsonl" 读
    shadow = tmp_path / "penetration_shadow.jsonl"
    latest = tmp_path / "latest.json"
    monkeypatch.setattr(pob, "SHADOW", shadow)
    monkeypatch.setattr(pob, "OUT_LATEST", latest)
    monkeypatch.setattr(pob, "load_bars", lambda s, d: _mk_bars(pattern=pattern))
    monkeypatch.setattr(sys, "argv", ["x"])
    if with_event:
        shadow.write_text(json.dumps({
            "type": "penetration", "event_id": "X-1", "et_date": "2026-09-02",
            "et_hm": "14:58", "symbol": "US.DELL", "direction": "LONG", "side": "passive",
            "path": "marketable_limit", "exit_reason": "SL_BREAKEVEN", "depth_bps": 20.0,
            "floor_px": 100.0, "trigger_price": 99.8, "entry": 99.0,
            "mfe_pct": 1.3, "giveback_bps": 30.0, "fresh_extreme": True,
            "held_minutes": 6.4, "minutes_since_open": 18, "atr_pct": 2.0,
        }) + "\n")
    pob.main()
    return pob, shadow, latest


def test_labeled_outcome_row_carries_status(tmp_path, monkeypatch):
    _, shadow, _ = _run_backfill(tmp_path, monkeypatch)
    rows = [json.loads(l) for l in shadow.read_text().splitlines() if l.strip()]
    oc = [r for r in rows if r.get("type") == "outcome"]
    assert len(oc) == 1
    assert oc[0].get("status") == "labeled", (
        "落盘的 labeled 结局行必须自带 status — 三个消费端都按它选行")
    assert oc[0].get("recovered_5m") is True


def test_analysis_load_samples_sees_backfilled_rows(tmp_path, monkeypatch):
    """写读配对: 回填一行 → 分析环 load_samples 必须看到 1 个可判分样本 (修复前恒 0)。"""
    _, shadow, _ = _run_backfill(tmp_path, monkeypatch)
    pla = _load_script("penetration_llm_analysis")
    monkeypatch.setattr(pla, "SHADOW", shadow)
    samples = pla.load_samples()
    assert len(samples) == 1, "研究环永远 n=0 → n≥8 不调 LLM、n≥20 不毕业 = 环空转"
    assert samples[0]["recovered_5m"] is True
    assert "cf_floor_fill_saved_bps" in samples[0], "反事实地板节省必须进入判分样本 (值可为 null)"


def test_shadow_board_section_counts_labeled_rows(tmp_path, monkeypatch):
    """看板配对: 段11 攒样计数必须随行增长 (修复前恒 0/20 = 防遗忘看板自身在说谎)。"""
    _, shadow, _ = _run_backfill(tmp_path, monkeypatch)
    sb = _load_script("shadow_board")
    monkeypatch.setattr(sb, "SHADOW_DIR", shadow.parent)
    lines = sb._sec_penetration()
    assert any("攒样: 1/20" in l for l in lines), lines


def test_summary_v_recovery_not_structurally_zero(tmp_path, monkeypatch):
    _, _, latest = _run_backfill(tmp_path, monkeypatch)
    summary = json.loads(latest.read_text())["summary"]
    assert summary["labeled"] == 1
    assert summary["v_recovery_5m_this_run"] == 1, "汇总口径与落盘行同源, 不得结构性恒零"


def test_backfill_heartbeat_creates_carrier(tmp_path, monkeypatch):
    """载体心跳: 无事件也要保证 jsonl 存在 (稀有事件影子"无文件"≠"环死了")。"""
    pob, shadow, latest = _run_backfill(tmp_path, monkeypatch, with_event=False)
    assert shadow.exists(), "回填环每日 touch 载体 → 注册表产出核对面才有意义"
    summary = json.loads(latest.read_text())["summary"]
    assert summary["events_total"] == 0 and summary["written"] == 0
    first_mtime = shadow.stat().st_mtime
    import os
    os.utime(shadow, (first_mtime - 86400, first_mtime - 86400))  # 模拟昨日心跳
    monkeypatch.setattr(sys, "argv", ["x"])
    pob.main()
    assert shadow.stat().st_mtime > first_mtime - 86400, "每次跑必须推进 mtime (断流才可检)"


def test_registry_entry_present_and_no_double_count(tmp_path):
    """三登记之①: 注册表在册 (SSOT), 且计数口径不把 outcome 行重复计入样本量。"""
    from app.playbook import shadow_registry as sr
    item = next((s for s in sr.REGISTRY if s.id == "penetration_shadow"), None)
    assert item is not None, "新影子必须注册 (注册表明文: 否则无人跟踪 = 等于没做)"
    assert item.status == sr.ShadowStatus.OBSERVING
    assert item.min_n == 20 and item.config_key == "llm_features.penetration_shadow.enabled"
    assert item.eval_script == "scripts/penetration_llm_analysis.py"
    assert item.count_mode == "where:type=penetration"
    assert "B17" in item.graduation_criteria, "毕业判据必须指向判据字典条目"
    p = tmp_path / "data" / "shadow" / "penetration_shadow.jsonl"   # _count_samples 以 base/data_path 拼
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join([
        json.dumps({"type": "penetration", "event_id": "A", "et_date": "2026-09-03"}),
        json.dumps({"type": "outcome", "event_id": "A", "status": "labeled"}),
    ]) + "\n")
    assert sr._count_samples(item, tmp_path) == 1, "1 事件 + 1 结局 = 1 个样本, 不是 2"


def test_source_pins_status_kept_in_row():
    src = (REPO / "scripts" / "penetration_outcome_backfill.py").read_text(encoding="utf-8")
    assert 'lab.pop("status")' not in src, "pop 掉 status 会让三个消费端永远选到 0 行"
    assert 'lab.get("status")' in src
    assert "SHADOW.touch()" in src, "载体心跳不得被移除 (稀有事件影子的黄灯误报防线)"


# ── 7. 幂等守卫 (2026-09-11 重试风暴污染根因修, 用户裁「按建议执行」) ──────────
#    病灶: 平仓失败重试每 ~10s 重新触发埋点, 一夜灌 786 行重复 (日均 8-22),
#    min_n=20 毕业门一夜假达标。契约"每事件 1 行" ⇒ 同 (symbol, inst_id, exit_reason)
#    只落首行。风暴备份实证: 821 行按本键去重=40, 与去重后生产文件 40 行逐一对应。
def test_retry_storm_dedup_same_event_only_first_row(tmp_path, monkeypatch):
    """同键重试 N 轮 → 只落 1 行 (首行=真实触发瞬间), 后续返回 False。"""
    monkeypatch.setattr(ps, "_PEN_PATH", tmp_path / "pen.jsonl")
    monkeypatch.setattr(ps, "_atr_pct", lambda s, p: 2.0)
    kw = dict(symbol="US.CTRN", direction="SHORT", exit_reason="SL_TRAIL_LOCK",
              side="passive", path="marketable_limit", trigger_price=59.9,
              floor_px=59.5, entry_price=60.2, peak_price=59.8, trough_price=59.85,
              held_minutes=37.0, quote_src="futu_lv3_push", inst_id=2251)
    assert ps.record_penetration(**kw) is True
    for _ in range(5):  # 模拟快环 10s×5 轮重试 (触发价随行情微动)
        assert ps.record_penetration(**{**kw, "trigger_price": 59.88}) is False
    rows = [json.loads(l) for l in (tmp_path / "pen.jsonl").read_text().splitlines()]
    assert len(rows) == 1, "重试风暴不得灌重复行 (毕业门假达标根因)"
    assert rows[0]["trigger_price"] == 59.9, "保留首行=真实触发瞬间"


def test_dedup_distinct_events_all_pass(tmp_path, monkeypatch):
    """不同 (symbol/inst_id/exit_reason) 是不同事件, 全部放行 — 守卫不得误杀。"""
    monkeypatch.setattr(ps, "_PEN_PATH", tmp_path / "pen.jsonl")
    monkeypatch.setattr(ps, "_atr_pct", lambda s, p: 2.0)
    base = dict(direction="LONG", side="passive", path="marketable_limit",
                trigger_price=99.0, floor_px=100.0, entry_price=98.0,
                peak_price=101.0, trough_price=98.5, held_minutes=5.0, quote_src="x")
    events = [
        dict(base, symbol="US.AAAA", inst_id=1, exit_reason="SL_BREAKEVEN"),
        dict(base, symbol="US.AAAA", inst_id=2, exit_reason="SL_BREAKEVEN"),  # 同票不同指令
        dict(base, symbol="US.BBBB", inst_id=1, exit_reason="SL_BREAKEVEN"),  # 不同票
        dict(base, symbol="US.AAAA", inst_id=1, exit_reason="SL_TRAIL_LOCK"),  # 同指令不同退出
    ]
    for ev in events:
        assert ps.record_penetration(**ev) is True
    rows = [json.loads(l) for l in (tmp_path / "pen.jsonl").read_text().splitlines()]
    assert len(rows) == 4


def test_dedup_corrupt_line_failopen(tmp_path, monkeypatch):
    """文件含坏行 → 判重不崩 (坏行跳过); 读档整体失败 → fail-open 放行 (宁多勿漏)。"""
    pen = tmp_path / "pen.jsonl"
    pen.write_text("{坏行json\n" + json.dumps(
        {"type": "penetration", "symbol": "US.CCCC", "inst_id": 9,
         "exit_reason": "SL_BREAKEVEN"}) + "\n")
    monkeypatch.setattr(ps, "_PEN_PATH", pen)
    monkeypatch.setattr(ps, "_atr_pct", lambda s, p: None)
    kw = dict(symbol="US.CCCC", direction="LONG", exit_reason="SL_BREAKEVEN",
              side="passive", path="marketable_limit", trigger_price=99.0,
              floor_px=100.0, entry_price=98.0, peak_price=101.0, trough_price=98.5,
              held_minutes=5.0, quote_src="x", inst_id=9)
    assert ps.record_penetration(**kw) is False  # 已有同键 → 拦
    # 读档异常 (目录占位使 open 抛错) → 守卫 fail-open 不崩; 写盘也抛 → 外层吞掉返回 False
    monkeypatch.setattr(ps, "_PEN_PATH", tmp_path)  # tmp_path 是目录, open() 必抛
    assert ps.record_penetration(**kw) is False  # 不抛出异常即合格 (record-only 铁律)
