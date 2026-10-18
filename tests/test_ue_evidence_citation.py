"""[P2.4 / P2.6 2026-09-13, proud-slope-falcon] UE 引证三态 + 输入空转守卫。

P2.4 病灶: `evidence_grounded` 是**一个 bool**, 只回答"这段引证出自今日材料吗"。
  它答不了计划 P2.4 要的那一问 —— 命中的那条材料**自己就带这个标的**吗 (直接关联实体),
  还是模型从宏观/板块/同业材料**推**到这只票 (间接传导假设)。两者在账上同一个 True ⇒
  "引证已核验"被下游读成"因果已核实"; 错实体 (拿 A 公司的事件给 B 票背书) 也落进 True 侧,
  在既有传感器上完全不可见。而 `evidence_grounding_enforce` 的预注册开启判据
  ("5 交易日人工抽检误杀率 <10%") 恰恰需要能把这两类分开才抽检得动。

P2.6 病灶: 材料索引空转时旧行为照旧调 LLM —— 拿一份近空白 brief 去问模型, 模型只能凭
  参数记忆编标的 (幻觉), 且照样烧满 max_tokens=16000。

红线 (两条都在这批里刻意守住):
  · 三态是**纯附加留痕** —— 不改 evidence_grounded 的算法与取值, 不改 Track A 夹逼,
    不改 confidence, 不打开 evidence_grounding_enforce (计划 P2.4 原文);
  · 空转守卫**宁可漏跳不可错跳** —— P2 验收② ("背景事件和榜外发现未被错误清空")
    比省一次调用重要, 故判据窄到只剩"索引一条事实都没有"且"brief 管线确实跑过"。
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scripts.llm_universe_scan as ue  # noqa: E402
from app.playbook.ue_batch import latest_premarket  # noqa: E402


def _idx(*entries):
    """按 _take_facts 的口径给事实补 evidence_id (索引里每条都有)。"""
    out = []
    for i, e in enumerate(entries, start=1):
        out.append({"evidence_id": f"E{i:03d}", **e})
    return out


HARD = "[CRITICAL/REGULATION] FDA 拒绝批准主力药 → 关联: VRTX"
INSIDER = "[HIGH/INSIDER_CLUSTER] BABA 内部人集群买入信号 x1 → 看多"
MACRO = "非农新增 15.2 万 (预期 13.0 万) → 利率敏感板块承压"
PEER = "电动车峰群 verdict=CONFIRM_DOWN: NIO-3.7%(量比2.80)"


def _index():
    return _idx(
        {"source": "hard_event_classified", "text": HARD, "symbols": ["VRTX"],
         "published_at": "2026-09-13", "event_key": "CRITICAL/REGULATION"},
        {"source": "insider_cluster", "text": INSIDER, "symbols": ["BABA"],
         "published_at": "2026-09-13", "event_key": "INSIDER_CLUSTER"},
        {"source": "macro_brief", "text": MACRO, "symbols": [],
         "published_at": "2026-09-13", "event_key": "macro"},
        {"source": "peer_surge", "text": PEER, "symbols": ["NIO"],
         "published_at": "2026-09-13", "event_key": "peer_surge/电动车"},
    )


# ══ 1. 三态判定 (纯函数) ══════════════════════════════════════════════════════
class TestCitationStates:
    def test_no_ref_when_evidence_empty(self):
        for ev in ("", "   ", None):
            got = ue._classify_evidence_citation(ev, "VRTX", _index())
            assert got["state"] == "no_ref", got
            assert got["evidence_id"] is None

    def test_entity_linked_when_fact_names_the_symbol(self):
        got = ue._classify_evidence_citation(HARD, "VRTX", _index())
        assert got["state"] == "entity_linked"
        assert got["evidence_id"] == "E001"
        assert got["matched_symbols"] == ["VRTX"]
        assert got["matched_source"] == "hard_event_classified"

    def test_macro_material_is_transmission_not_entity(self):
        """宏观材料**没有**具名标的 ⇒ 引用它给某票背书只能是传导假设, 不得记直接关联。"""
        got = ue._classify_evidence_citation(MACRO, "KRE", _index())
        assert got["state"] == "transmission_assumed"
        assert got["evidence_id"] == "E003"
        assert got["matched_symbols"] == []

    def test_other_entity_event_is_transmission_not_verified(self):
        """错实体/同业传导: 拿 NIO 的峰群行给 TSLA 背书 ⇒ **不是**已核验的直接关联。

        这一格在既有 bool 传感器上是 True (材料里确实有这句), 完全看不出问题。
        """
        got = ue._classify_evidence_citation(PEER, "TSLA", _index())
        assert got["state"] == "transmission_assumed"
        assert got["matched_symbols"] == ["NIO"]
        assert "TSLA" not in got["matched_symbols"]

    def test_fabricated_reference_is_not_found(self):
        got = ue._classify_evidence_citation(
            "公司宣布获得国防部 12 亿美元订单", "LMT", _index())
        assert got["state"] == "not_found"
        assert got["evidence_id"] is None

    def test_empty_index_is_not_found_not_no_ref(self):
        """给了引证但没有任何材料可对 ⇒ 编造/未知, 不得记作已核验证据 (计划原文)。"""
        got = ue._classify_evidence_citation(HARD, "VRTX", [])
        assert got["state"] == "not_found"
        assert got != ue._classify_evidence_citation("", "VRTX", [])

    def test_symbol_prefix_and_case_normalized(self):
        for sym in ("VRTX", "US.VRTX", "us.vrtx", " vrtx "):
            assert ue._classify_evidence_citation(HARD, sym, _index())["state"] == \
                "entity_linked", sym

    def test_state_always_in_declared_enum(self):
        """量纲=枚举: 取值域固定 (AGENT.md 军规"新增落库字段必带单位/量纲声明")。"""
        cases = [("", "VRTX"), (HARD, "VRTX"), (MACRO, "KRE"), (PEER, "TSLA"),
                 ("凭空编造的一句", "ZZZ"), (HARD, "OTHER")]
        for ev, sym in cases:
            for index in (_index(), [], None):
                got = ue._classify_evidence_citation(ev, sym, index)
                assert got["state"] in ue.CITATION_STATES, got
                assert set(got) == {"state", "evidence_id", "matched_symbols",
                                    "matched_source", "matched_event_meta"}

    def test_cross_line_quote_is_conservative_not_entity_linked(self):
        """跨行拼引: 池化匹配过阈但定位不到单条事实 ⇒ 保守落传导假设且 evidence_id=None。

        不回退成 not_found (那会与 evidence_grounded=True 自相矛盾), 也不宣称
        entity_linked (定位不到事实就无从判断实体关联)。
        """
        stitched = HARD + " " + MACRO
        assert ue._evidence_grounded(stitched, [e["text"] for e in _index()]) is True
        got = ue._classify_evidence_citation(stitched, "VRTX", _index())
        assert got["state"] in ("entity_linked", "transmission_assumed")
        if got["evidence_id"] is None:
            assert got["state"] == "transmission_assumed"

    def test_match_is_reproducible_on_ties(self):
        """同分取索引靠前的一条 ⇒ 重复调用结果稳定 (可复现是审计前提)。"""
        dup = _idx({"source": "a", "text": HARD, "symbols": ["VRTX"]},
                   {"source": "b", "text": HARD, "symbols": []})
        first = [ue._classify_evidence_citation(HARD, "VRTX", dup)["evidence_id"]
                 for _ in range(5)]
        assert first == ["E001"] * 5


# ══ 2. 接线: 纯附加, 不动任何既有判定 ════════════════════════════════════════
@pytest.fixture
def index_loaded(monkeypatch):
    """把材料索引装进模块态 (生产中由 _build_clue_brief 在 gather 现场登记)。"""
    monkeypatch.setattr(ue, "_BRIEF_EVIDENCE", _index())
    monkeypatch.setattr(ue, "_LAST_BRIEF_MATERIALS",
                        [e["text"] for e in _index()])
    monkeypatch.setattr(ue, "_cfg", lambda k, d=None: d)


def _raw(symbol="VRTX", ev=HARD, tier="HARD_EVENT", conf=0.8):
    return [{"symbol": symbol, "direction": "LONG", "confidence": conf,
             "evidence_ref": ev, "self_tier": tier}]


class TestNormalizeSurfacedWiring:
    def test_fields_present_with_tier(self, index_loaded):
        got = ue._normalize_surfaced(_raw(), with_tier=True)[0]
        assert got["evidence_citation_state"] == "entity_linked"
        assert got["evidence_id"] == "E001"
        assert got["evidence_matched_symbols"] == ["VRTX"]
        assert got["evidence_matched_source"] == "hard_event_classified"

    def test_v1_path_unchanged(self, index_loaded):
        """with_tier=False (V1) 不得多出任何引证字段 — 那条臂的字段集不适用。"""
        got = ue._normalize_surfaced(_raw(), with_tier=False)[0]
        for key in ("evidence_citation_state", "evidence_id",
                    "evidence_matched_symbols", "evidence_matched_source",
                    "evidence_grounded"):
            assert key not in got, key

    def test_entity_and_not_found_do_not_change_confidence(self, index_loaded):
        """红线: 非传导三态是附加留痕, 不是新闸门。"""
        for ev, sym in ((HARD, "VRTX"), (MACRO, "KRE"), ("凭空编造的一句", "ZZZ")):
            got = ue._normalize_surfaced(_raw(symbol=sym, ev=ev), with_tier=True)[0]
            assert got["evidence_grounded"] == ue._evidence_grounded(
                ev, [e["text"] for e in _index()])
            assert got["self_tier"] == "HARD_EVENT"
            if got["evidence_citation_state"] != "transmission_assumed":
                assert got["confidence"] == 0.8
                assert got["clamped"] is False

    def test_transmission_assumed_confidence_is_calibrated(self, index_loaded):
        got = ue._normalize_surfaced(_raw(symbol="KRE", ev=MACRO), with_tier=True)[0]
        assert got["evidence_citation_state"] == "transmission_assumed"
        assert got["confidence"] == 0.55
        assert got["raw_confidence"] == 0.8
        assert got["confidence_calibration"]["reasons"] == [
            "transmission_assumed_cap_0.55",
            "transmission_requires_market_confirmation",
        ]

    def test_fabricated_ref_still_passes_through_when_enforce_off(self, index_loaded):
        """not_found 在 enforce=false 下**不拦不改** — 本轮不借此打开强制门。"""
        got = ue._normalize_surfaced(
            _raw(symbol="LMT", ev="公司宣布获得国防部 12 亿美元订单"),
            with_tier=True)[0]
        assert got["evidence_citation_state"] == "not_found"
        assert got["evidence_grounded"] is False
        assert got["evidence_ref"] != ""          # 未被清空
        assert got["self_tier"] == "HARD_EVENT"   # 未落 Track A 夹逼
        assert got["confidence"] == 0.8

    def test_classification_uses_pre_enforce_reference(self, index_loaded, monkeypatch):
        """enforce=true 会把 ev 清空; 三态必须用**清空前**的引证, 否则"未过核验"
        会被误记成"模型没给引证"(no_ref), 两种缺陷在账上混成一类。"""
        # 两个键都要 True: `evidence_grounding=false` 会整段跳过核验, enforce 也就无从触发。
        monkeypatch.setattr(ue, "_cfg", lambda k, d=None: (
            True if k in ("universe_expansion.evidence_grounding",
                          "universe_expansion.evidence_grounding_enforce") else d))
        got = ue._normalize_surfaced(
            _raw(symbol="LMT", ev="公司宣布获得国防部 12 亿美元订单"),
            with_tier=True)[0]
        assert got["evidence_ref"] == ""                     # 夹逼已接管
        assert got["self_tier"] == "THESIS_ONLY"
        assert got["evidence_citation_state"] == "not_found"  # 不是 no_ref

    def test_classifier_failure_is_not_an_accusation(self, index_loaded, monkeypatch):
        """传感器坏了不能赖模型编造: 异常 ⇒ 不落 not_found, 并标 classifier_error。"""
        def _boom(*a, **k):
            raise RuntimeError("index exploded")

        monkeypatch.setattr(ue, "_classify_evidence_citation", _boom)
        got = ue._normalize_surfaced(_raw(), with_tier=True)[0]
        assert got["evidence_citation_state"] == "transmission_assumed"
        assert got["evidence_matched_source"] == "classifier_error"
        assert got["evidence_id"] is None

    def test_classifier_failure_without_ref_is_no_ref(self, index_loaded, monkeypatch):
        monkeypatch.setattr(ue, "_classify_evidence_citation",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        got = ue._normalize_surfaced(_raw(ev=""), with_tier=True)[0]
        assert got["evidence_citation_state"] == "no_ref"
        assert got["evidence_matched_source"] is None

    def test_empty_index_yields_not_found_for_any_ref(self, monkeypatch):
        monkeypatch.setattr(ue, "_BRIEF_EVIDENCE", [])
        monkeypatch.setattr(ue, "_cfg", lambda k, d=None: d)
        got = ue._normalize_surfaced(_raw(), with_tier=True)[0]
        assert got["evidence_citation_state"] == "not_found"


# ══ 3. 基数自检登记 (AGENT.md 军规: 新落库字段必带基数自检 + 消费方登记) ══════
class TestCardinalityRegistration:
    def test_field_registered_in_shadow_cardinality(self):
        import scripts.shadow_field_cardinality as sfc
        src = open(sfc.__file__, encoding="utf-8").read()
        assert '"evidence_citation_state"' in src
        # 设计低基数枚举必须放宽 (默认 min_unique=5 会天天误报)
        assert "evidence_citation_state" in sfc.FIELD_RULES
        rule = sfc.FIELD_RULES["evidence_citation_state"]
        assert rule["min_unique"] == 2
        assert sfc.DEFAULT_RULE["min_unique"] > rule["min_unique"]

    def test_v214_enum_fields_registered_in_shadow_cardinality(self):
        import scripts.shadow_field_cardinality as sfc
        for field in ("confidence_basis", "event_phase", "move_basis",
                      "borrow_data_basis"):
            assert field in sfc.FIELD_RULES
            assert sfc.FIELD_RULES[field]["min_unique"] == 2
            assert sfc.FIELD_RULES[field]["min_unique"] < (
                sfc.DEFAULT_RULE["min_unique"])

    def test_consumer_registered_in_source(self):
        """无消费方的字段一律删 (P7 军规) ⇒ 消费方必须写在代码里, 不只写在 DEB。"""
        src = open(ue.__file__, encoding="utf-8").read()
        assert "消费方登记 (AGENT.md P7)" in src
        assert "shadow_field_cardinality" in src


# ══ 4. [P2.6] 输入可用性判定 (纯函数) ═════════════════════════════════════════
class TestScanInputActionable:
    def test_empty_index_is_not_actionable(self):
        got = ue._scan_input_is_actionable([], {"status": "no_named_symbol"},
                                           {"brief_content_hash": "x"})
        assert got["actionable"] is False
        assert got["reason"] == "empty_material_index"

    def test_named_only_is_actionable(self):
        got = ue._scan_input_is_actionable(_index()[:1], {"status": "ok"}, {"h": 1})
        assert got["actionable"] is True
        assert got["reason"] == "has_named_material"

    def test_macro_only_is_actionable(self):
        """只有宏观/板块材料也**允许一次扫描** — 计划 P2.6 后半句。"""
        got = ue._scan_input_is_actionable([_index()[2]], {"status": "no_named_symbol"},
                                           {"h": 1})
        assert got["actionable"] is True
        assert got["reason"] == "has_macro_material"

    def test_context_only_named_material_still_actionable(self):
        """具名标的哪怕**全被判 context_only 也不跳** — 不可交易公司的事件保留作产业链
        推导 (P2.2), 属计划原文里"有发现价值的背景材料"。这是守卫最容易被做窄错的一格。"""
        got = ue._scan_input_is_actionable(
            _index()[:1],
            {"status": "ok", "tradeable": [], "context_only": [{"symbol": "VRTX"}],
             "unknown": []}, {"h": 1})
        assert got["actionable"] is True
        assert got["named_material_count"] == 1

    def test_degraded_prescreen_never_skips(self):
        """缺数 ≠ 没有机会 (计划 P1.3): 初筛炸了不能顺手把当天扫描也跳掉。"""
        got = ue._scan_input_is_actionable([], {"status": "unavailable"}, {"h": 1})
        assert got["actionable"] is True
        assert got["reason"] == "input_degraded_fail_open"

    def test_unobserved_brief_pipeline_never_skips(self):
        """索引空 + 指纹也空 ⇒ brief 不是 `_build_clue_brief` 产的 (桩/被 patch)。

        实测教训 (2026-09-13): tests/playbook/offline_chain_driver.py 把
        `_build_clue_brief` 换成返回冻结串的桩, 索引自然为空, 守卫据此跳过 LLM ⇒
        整条 scan→inject→execute 链断在第一节 (18 例 setup ERROR)。缺这道观测前提,
        "索引空"与"没跑 gather"在代码里是同一个形态。
        """
        got = ue._scan_input_is_actionable([], {"status": "no_named_symbol"}, {})
        assert got["actionable"] is True
        assert got["reason"] == "brief_pipeline_not_observed"
        assert got["brief_observed"] is False

    def test_manual_clue_counts_as_material(self):
        """人工线索在 _build_clue_brief 里登记成一条无 symbols 的事实 ⇒ 落 macro 桶,
        人明确要求扫的时候绝不会被判空转跳过。"""
        clue = ue._fact("manual_clue", "关注 NVDA 财报后的半导体链", event_key="manual")
        got = ue._scan_input_is_actionable([clue], {"status": "no_named_symbol"}, {"h": 1})
        assert got["actionable"] is True

    def test_blank_text_facts_do_not_count(self):
        got = ue._scan_input_is_actionable(
            [{"text": "   ", "symbols": []}], {"status": "ok"}, {"h": 1})
        assert got["actionable"] is False
        assert got["material_count"] == 0

    def test_counts_are_reported_for_audit(self):
        got = ue._scan_input_is_actionable(_index(), {"status": "ok"}, {"h": 1})
        assert got["material_count"] == 4
        assert got["named_material_count"] == 3
        assert got["macro_material_count"] == 1
        assert got["prescreen_status"] == "ok"
        assert got["reason"] == "has_named_and_macro_material"


# ══ 5. [P2.6] 接线: 跳批写的是**完成态空批**, 且 LLM 一次没调 ═════════════════
def _stub_llm(monkeypatch, calls, surfaced=None):
    """把 `app.llm.model_call.call_llm` 换成**记账**桩 (零网络)。

    ⚠ 必须在桩里 `calls.append("llm")` —— scan_env 里那支桩被本函数覆盖后就再也不记账了,
      "LLM 有没有被调"这条断言会假红 (2026-09-13 实测踩过)。
    """
    import app.llm.model_call as mlm

    class _Res:
        success = True
        response = {"surfaced": [] if surfaced is None else surfaced}
        model_used = "stub"
        error = None

    def _spy(*a, **k):
        calls.append("llm")
        return _Res()

    monkeypatch.setattr(mlm, "call_llm", _spy)


class _Recorder:
    def __init__(self):
        self.rows = []

    def __call__(self, path, record):
        self.rows.append(record)


@pytest.fixture
def scan_env(monkeypatch, tmp_path):
    """`_cmd_scan_impl` 最小可跑环境: 零网络、零生产 data/、零通知。"""
    rec = _Recorder()
    calls = []
    monkeypatch.setattr(ue, "_append_jsonl", rec)
    monkeypatch.setattr(ue, "_ROOT", tmp_path)
    monkeypatch.setattr(ue, "_SCAN_PATH", tmp_path / "scan.jsonl")
    monkeypatch.setattr(ue, "_MIDDAY_PATH", tmp_path / "midday.jsonl")
    monkeypatch.setattr(ue, "_today", lambda: "2026-09-14")
    monkeypatch.setattr(ue, "_scan_fail_alert",
                        lambda *a, **k: calls.append("alert"))

    import app.llm.model_call as mlm

    def _spy(*a, **k):
        calls.append("llm")
        raise AssertionError("LLM 被调用了 — 空转守卫没生效")

    monkeypatch.setattr(mlm, "call_llm", _spy)
    cfg = {"universe_expansion.midday_shadow_enabled": True,
           "universe_expansion.prompt_version": "v2",
           "universe_expansion.scan_skip_on_empty_input": True}
    monkeypatch.setattr(ue, "_cfg", lambda k, d=None: cfg.get(k, d))
    monkeypatch.setattr(ue, "_build_clue_brief", lambda manual="": "BRIEF")
    now = datetime.now().astimezone()
    batch = {"batch_id": "UE:test", "date": "2026-09-14", "scan_window": "premarket",
             "started_at": now.isoformat(), "policy_version": "da_ue_v1"}
    return {"rec": rec, "calls": calls, "batch": batch, "cfg": cfg,
            "now": now, "monkeypatch": monkeypatch}


def _set_index(monkeypatch, entries, *, observed=True, prescreen=None):
    monkeypatch.setattr(ue, "_BRIEF_EVIDENCE", list(entries))
    monkeypatch.setattr(ue, "_LAST_BRIEF_FINGERPRINTS",
                        {"brief_content_hash": "abc", "brief_template_hash": "def",
                         "brief_material_stats": {}} if observed else {})
    monkeypatch.setattr(ue, "_LAST_INPUT_PRESCREEN",
                        prescreen if prescreen is not None else
                        {"status": "no_named_symbol"})


class TestScanSkipWiring:
    def test_empty_input_skips_llm_and_writes_complete_empty_batch(self, scan_env):
        _set_index(scan_env["monkeypatch"], [])
        result = {}
        rc = ue._cmd_scan_impl("", False, False, scan_env["batch"], result)
        assert rc == 0
        assert scan_env["calls"] == [], "既没调 LLM 也不该告警"
        assert result["scan_written"] is True
        rows = [r for r in scan_env["rec"].rows if r["type"] == "scan"]
        assert len(rows) == 1
        row = rows[0]
        assert row["batch_status"] == "complete"
        assert row["empty_batch"] is True
        assert row["llm_skipped"] is True
        assert row["skip_reason"] == ue.SCAN_SKIP_NO_INPUT
        assert row["surfaced"] == []
        assert row["model_used"] is None
        assert row["prompt_version"] == ue.PROMPT_VERSION
        assert row["brief_template_version"] == ue.BRIEF_TEMPLATE_VERSION
        assert row["input_actionability"]["reason"] == "empty_material_index"

    def test_skipped_batch_is_consumable_as_normal_empty_by_inject(self, scan_env):
        """跳批必须是 inject 认得的**完成态**批次 ⇒ 正常收工, 不回用早先候选。"""
        _set_index(scan_env["monkeypatch"], [])
        ue._cmd_scan_impl("", False, False, scan_env["batch"], {})
        row = [r for r in scan_env["rec"].rows if r["type"] == "scan"][0]
        got, reason = latest_premarket([row], "2026-09-14",
                                       scan_env["now"] + timedelta(minutes=5), 3.0)
        assert reason == "complete"
        assert got is row
        assert (got.get("surfaced") or []) == []

    def test_skip_is_distinguishable_from_llm_empty_and_from_failure(self, scan_env):
        """三种终态互斥可分: 没问模型 / 问了但空 / 失败。混成一类就没法归因。"""
        _set_index(scan_env["monkeypatch"], [])
        ue._cmd_scan_impl("", False, False, scan_env["batch"], {})
        row = [r for r in scan_env["rec"].rows if r["type"] == "scan"][0]
        assert row.get("llm_skipped") is True and row.get("empty_batch") is True
        assert row.get("batch_status") != "failed"
        # "问了模型但 surfaced 空" 那一态由既有 P1.5 路径落盘, 不带 llm_skipped
        assert "llm_skipped" not in row or row["llm_skipped"] is True

    def test_non_empty_index_still_calls_llm(self, scan_env, monkeypatch):
        _set_index(scan_env["monkeypatch"], [_index()[0]])

        _stub_llm(monkeypatch, scan_env["calls"])
        scan_env["calls"].clear()
        rc = ue._cmd_scan_impl("", False, False, scan_env["batch"], {})
        assert rc == 0
        assert scan_env["calls"] == ["llm"]

    def test_config_off_restores_legacy_behaviour(self, scan_env, monkeypatch):
        scan_env["cfg"]["universe_expansion.scan_skip_on_empty_input"] = False
        _set_index(scan_env["monkeypatch"], [])

        _stub_llm(monkeypatch, scan_env["calls"])
        scan_env["calls"].clear()
        assert ue._cmd_scan_impl("", False, False, scan_env["batch"], {}) == 0
        assert scan_env["calls"] == ["llm"]

    def test_unobserved_pipeline_does_not_skip(self, scan_env, monkeypatch):
        """brief 由桩产生 (指纹为空) ⇒ 守卫不得据此跳批 (离线全链断链的根因)。"""
        _set_index(scan_env["monkeypatch"], [], observed=False)

        _stub_llm(monkeypatch, scan_env["calls"])
        scan_env["calls"].clear()
        assert ue._cmd_scan_impl("", False, False, scan_env["batch"], {}) == 0
        assert scan_env["calls"] == ["llm"]

    def test_degraded_prescreen_does_not_skip(self, scan_env, monkeypatch):
        _set_index(scan_env["monkeypatch"], [], prescreen={"status": "unavailable"})

        _stub_llm(monkeypatch, scan_env["calls"])
        scan_env["calls"].clear()
        assert ue._cmd_scan_impl("", False, False, scan_env["batch"], {}) == 0
        assert scan_env["calls"] == ["llm"]

    def test_skip_record_carries_window_and_batch_identity(self, scan_env):
        """批次身份/窗口随跳批记录落盘 —— 否则 P4 全漏斗对不上批次。"""
        _set_index(scan_env["monkeypatch"], [])
        ue._cmd_scan_impl("", False, False, scan_env["batch"], {})
        row = [r for r in scan_env["rec"].rows if r["type"] == "scan"][0]
        assert row["scan_window"] == "premarket"
        assert row["batch_id"] == "UE:test"
        assert row["date"] == "2026-09-14"
        assert row["policy_version"] == "da_ue_v1"
        assert row["source_type"] == ue.SOURCE_TYPE
        assert row["strategy_id"] == ue.STRATEGY_ID
