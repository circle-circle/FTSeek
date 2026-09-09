"""DA 输出契约单测 (2026-08-25, DEB-20260825 第13轮 P1/P3a/P6).

被测对象在 skill 仓 (`/root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py`),
不在本仓 → 全文件以 `skill 不可用即 skip` 保护, 不让 CI 因外部依赖缺失而红。

覆盖点 (按"最容易出事"排序):
  1. **定向补全绝不改交易决策** — 即使模型响应夹带 direction/rating/score 改写也无效 (最重要的安全不变量)
  2. `DA_SCHEMA_ENFORCE=0` 回滚开关双向有效 (关掉后连校验都不写, 但 prompt 指纹仍落盘)
  3. 接入点真的会被执行 (端到端跑 synthesize, 非只测函数 — 本议题主线病灶就是"写了但没执行")
  4. 合规输入不额外花 repair 调用 (成本上界 = 违约率 × 1 次)
  5. repair 失败不伪装成修好 (violations_after 仍留违约)
  6. 方向词表归一 (LONG/SHORT ↔ BUY/SELL_SHORT): 不归一会让校验器静默退化为空操作,
     且对外表现是"零违约"的假通过 — 第13轮回放实测踩过
  7. 异常/空输入零崩溃 (校验器跑在生产主路径上)
  8. P3a: 挑战者调用 3→1 (H5 采样影子已结案下线)

复核用途: DEB 第13轮 §E 预注册判据要求 n≥30 后复算 `tripwires_lt2` 占比 (修前基线 17%),
`test_replay_baseline_if_data_present` 即该基线的复算入口。
"""
import json
import os
import sys
from pathlib import Path

import pytest

_SKILL = "/root/.hermes/skills/deep-stock-analysis/scripts"
_SKILL_ROOT = Path(_SKILL).parent
if _SKILL not in sys.path:
    sys.path.insert(0, _SKILL)
try:
    import llm_synth as L
except Exception as _e:  # pragma: no cover - 环境无 skill 时整体跳过
    L = None
    _IMPORT_ERR = str(_e)

pytestmark = pytest.mark.skipif(L is None, reason="deep-stock-analysis skill 不可用")


def _ok_long():
    """一份完全合规的有向输出."""
    return {
        "symbol": "TEST", "direction": "BUY", "rating": "看多", "score": 7,
        "thesis": "t", "confidence": 60,
        "execution_plan": {
            "entry_price": 100, "stop_loss_price": 97, "take_profit_price": 108,
            "tripwires": [{"metric": "收盘价", "threshold": "跌破97"},
                          {"metric": "EPS", "threshold": "低于1.2"}],
            "invalidation_triggers": [{"metric": "收盘价", "operator": "<", "value": "97"},
                                      {"metric": "5日涨跌", "operator": "<", "value": "-5"}],
        },
        "bull_case": [
            {"claim_type": "earnings_beat", "thesis": "营收增长12%", "evidence": "输入营收同比12%"},
            {"claim_type": "technical_breakout", "thesis": "股价站上100", "evidence": "现价100"},
        ],
        "bear_case": [
            {"claim_type": "valuation_expensive", "thesis": "PE达到35", "evidence": "输入PE 35"},
            {"claim_type": "macro_headwind", "thesis": "利率维持5%", "evidence": "输入利率5%"},
        ],
        "risks": [],
    }


def _violating():
    """仅 tripwires 不足 (= 修前基线 17% 的那一类)."""
    bad = json.loads(json.dumps(_ok_long()))
    bad["execution_plan"]["tripwires"] = [{"metric": "情绪", "threshold": ""}]
    return bad


CHALLENGER = {"my_direction": "BUY", "my_score": 7, "verdict": "AGREE", "agreed": True,
              "independent_direction": "BUY",
              "validation": {"data": "通过", "logic": "通过", "market": "通过",
                             "risk": "不通过 缺财报日", "levels": "通过"},
              "missed_risks": ["r1"], "counter_thesis": "x"}

# 恶意 repair 响应: 结构补丁合法, 但夹带方向/评级/评分改写
MALICIOUS_REPAIR = {
    "tripwires": [{"metric": "收盘价", "threshold": "跌破97"},
                  {"metric": "VIX", "threshold": ">25"}],
    "direction": "SELL_SHORT", "rating": "强烈看空", "score": 1, "bull_case": [],
}

COLLECTED = {"symbol": "TEST",
             "snapshot": {"last_price": 100.0, "effective_price": 100.0,
                          "change_rate": 1.2, "volume": 1000000},
             "news": [], "financials": {}, "technical": {}}


@pytest.fixture
def restore_call_llm():
    orig = L._call_llm
    yield
    L._call_llm = orig


@pytest.fixture(autouse=True)
def clean_env():
    old = os.environ.pop("DA_SCHEMA_ENFORCE", None)
    yield
    os.environ.pop("DA_SCHEMA_ENFORCE", None)
    if old is not None:
        os.environ["DA_SCHEMA_ENFORCE"] = old


def _seq_stub(responses, log):
    """按调用顺序返回 responses[i]; 越界则复用最后一个."""
    n = {"i": 0}

    def stub(system, user, **kw):
        r = responses[min(n["i"], len(responses) - 1)]
        log.append(n["i"])
        n["i"] += 1
        return json.loads(json.dumps(r))
    return stub


# ── 1. 安全不变量: 补全不改交易决策 ────────────────────────────────────────
def test_repair_never_changes_direction(restore_call_llm):
    """最关键的一条: 补丁白名单不含 direction/rating/score → 夹带改写结构上无效."""
    L._call_llm = lambda *a, **k: json.loads(json.dumps(MALICIOUS_REPAIR))
    tgt = _violating()
    before = (tgt["direction"], tgt["rating"], tgt["score"], len(tgt["bull_case"]))

    rep = L._repair_output_contract(tgt, "sys", "data", ["tripwires"])

    assert rep["applied"] == ["tripwires"]
    assert len(tgt["execution_plan"]["tripwires"]) == 2
    assert (tgt["direction"], tgt["rating"], tgt["score"], len(tgt["bull_case"])) == before


def test_end_to_end_direction_preserved(restore_call_llm):
    """端到端也要成立 (证明接入点被执行, 且不变量在真实调用链里保持)."""
    log = []
    L._call_llm = _seq_stub([_violating(), CHALLENGER, MALICIOUS_REPAIR], log)

    res = L.synthesize(COLLECTED)

    assert len(log) == 3, "应为 分析师+挑战者+repair 三次调用"
    assert "_contract" in res, "接入点未被执行"
    assert (res["direction"], res["rating"], res["score"]) == ("BUY", "看多", 7)
    assert "ERROR_direction_changed" not in res["_contract"]
    # _violating() 继承 _ok_long() 的合规 invalidation_triggers (2条), 仅 tripwires 不足;
    #   repair 补齐 tripwires 后 violations_after 应为空
    assert res["_contract"]["violations_after"] == []


# ── 2. 回滚开关 ───────────────────────────────────────────────────────────
def test_enforce_off_is_fully_silent(restore_call_llm):
    log = []
    L._call_llm = _seq_stub([_violating(), CHALLENGER, MALICIOUS_REPAIR], log)
    os.environ["DA_SCHEMA_ENFORCE"] = "0"

    res = L.synthesize(COLLECTED)

    assert len(log) == 2, "关闭后不应发生 repair 调用"
    assert "_contract" not in res
    assert len(res["execution_plan"]["tripwires"]) == 1, "关闭后不得改动任何字段"
    # P6 的指纹独立于 P1 开关 — 关掉契约不应丢失基线切分能力
    assert res.get("_prompt_hash") == L.PROMPT_HASH


# ── 3. 成本上界 & 失败降级 ────────────────────────────────────────────────
def test_compliant_input_costs_no_extra_call(restore_call_llm):
    log = []
    L._call_llm = _seq_stub([_ok_long(), CHALLENGER], log)

    res = L.synthesize(COLLECTED)

    assert len(log) == 2, "合规输入不应触发 repair"
    assert res["_contract"]["violations"] == []
    assert "repair" not in res["_contract"]


def test_repair_failure_does_not_fake_success(restore_call_llm):
    log = []
    L._call_llm = _seq_stub([_violating(), CHALLENGER, {"_error": "timeout"}], log)

    res = L.synthesize(COLLECTED)

    assert "_error" not in res and res["direction"] == "BUY", "主流程必须完整返回"
    assert res["_contract"]["repair"]["ok"] is False
    assert any("tripwires_lt2" in v for v in res["_contract"]["violations_after"]), \
        "补全失败时违约必须仍然可见"


# ── 4. 校验器判别正确性 ───────────────────────────────────────────────────
def test_compliant_sample_has_no_violation():
    assert L._validate_output_contract(_ok_long())["violations"] == []


def test_generic_claim_requires_numeric_anchor():
    sample = _ok_long()
    sample["bull_case"][0] = {
        "claim_type": "product_catalyst",
        "thesis": "公司旗舰产品获批",
        "evidence": "新闻披露旗舰产品获批",
    }
    violations = L._validate_output_contract(sample)["violations"]
    assert violations.count("generic_claim: bull_case 论据无数字锚点") == 1


def test_numeric_anchor_may_be_in_evidence():
    sample = _ok_long()
    sample["bull_case"][0] = {
        "claim_type": "product_catalyst",
        "thesis": "公司旗舰产品获批",
        "evidence": "公告日期为2026-09-08",
    }
    assert not any(v.startswith("generic_claim")
                   for v in L._validate_output_contract(sample)["violations"])


def test_empty_threshold_tripwire_not_counted():
    r = L._validate_output_contract(_violating())
    assert any(v.startswith("tripwires_lt2") for v in r["violations"])
    assert "tripwires" in r["repairable"]


@pytest.mark.parametrize("direction,sl,tp,tag", [
    ("BUY", 110, 108, "price_order_invalid_long"),     # 多头止损高于入场
    ("SELL_SHORT", 96, 92, "price_order_invalid_short"),  # 空头止损低于入场
])
def test_price_order_must_match_direction(direction, sl, tp, tag):
    s = _ok_long()
    s["direction"] = direction
    s["execution_plan"]["stop_loss_price"] = sl
    s["execution_plan"]["take_profit_price"] = tp
    assert any(v.startswith(tag) for v in L._validate_output_contract(s)["violations"])


def test_claim_type_enum_enforced():
    s = _ok_long()
    s["bull_case"] = [{"claim_type": "made_up"}, {"claim_type": "earnings_beat"}]
    assert any(v.startswith("claim_type_invalid")
               for v in L._validate_output_contract(s)["violations"])


def test_skip_requires_reason():
    r = L._validate_output_contract({"direction": "SKIP", "rating": "观望", "score": 4})
    assert "skip_reason_missing" in r["violations"]
    assert "skip_reason" in r["repairable"]
    # SKIP 不应因缺价位/tripwire 被判违约
    assert not any(v.startswith("prices_missing") for v in r["violations"])
    assert L._validate_output_contract(
        {"direction": "SKIP", "skip_reason": "缺盘前量能确认"})["violations"] == []


# ── 5. 方向词表归一 (静默失效防护) ────────────────────────────────────────
@pytest.mark.parametrize("alias,canonical", [("LONG", "BUY"), ("SHORT", "SELL_SHORT"),
                                             ("SELL", "SELL_SHORT")])
def test_direction_aliases_normalized(alias, canonical):
    """不归一的后果不是报错而是静默走 SKIP 分支 = 校验器空操作 + "零违约"假通过."""
    src = _ok_long() if canonical == "BUY" else {
        "direction": canonical, "rating": "看空", "score": 7,
        "execution_plan": {"entry_price": 100, "stop_loss_price": 104,
                           "take_profit_price": 92,
                           "tripwires": [{"metric": "a", "threshold": "b"},
                                         {"metric": "c", "threshold": "d"}],
                           "invalidation_triggers": [{"metric": "收盘价", "operator": "<", "value": "92"},
                                                      {"metric": "5日涨跌", "operator": "<", "value": "-5"}]},
        "bull_case": [
            {"claim_type": "earnings_beat", "thesis": "营收增长12%"},
            {"claim_type": "macro_tailwind", "evidence": "利率回落至4%"},
        ],
        "bear_case": [
            {"claim_type": "technical_breakdown", "thesis": "跌破92"},
            {"claim_type": "insider_bearish", "evidence": "内部人减持10%"},
        ],
    }
    src["direction"] = alias
    assert L._validate_output_contract(src)["violations"] == []

    # 归一后仍须能抓出结构缺失 (不是一律放过)
    bad = json.loads(json.dumps(src))
    bad["execution_plan"]["tripwires"] = []
    assert any(v.startswith("tripwires_lt2") for v in L._validate_output_contract(bad)["violations"])


def test_unknown_direction_treated_as_skip():
    """未登记的词表按 SKIP 处理 (保守), 但必须留下 skip_reason 违约痕迹 — 不静默放过."""
    assert L._validate_output_contract(
        {"direction": "HOLD_XYZ"})["violations"] == ["skip_reason_missing"]


# ── 6. 鲁棒性 (校验器在生产主路径上) ──────────────────────────────────────
@pytest.mark.parametrize("bad_input", [
    {}, {"direction": None}, {"direction": "BUY"},
    {"direction": "BUY", "execution_plan": None},
    {"direction": "BUY", "execution_plan": {"tripwires": "notalist"}},
    {"direction": "BUY", "execution_plan": {"tripwires": [None, 5]}, "bull_case": None},
])
def test_validator_never_raises(bad_input):
    L._validate_output_contract(bad_input)


# ── 7. P6 观测 & P3a 减法 ─────────────────────────────────────────────────
def test_prompt_fingerprint_properties():
    import re
    assert re.fullmatch(r"[0-9a-f]{12}", L.PROMPT_HASH)
    assert L._prompt_fingerprint("a") != L._prompt_fingerprint("b")
    assert L._prompt_fingerprint("a", "b") == L._prompt_fingerprint("a", "b")


def test_challenger_called_once_not_thrice(restore_call_llm):
    """P3a: DEB-20260723 H5 采样影子 (flip 0/107, 零消费方) 已结案下线."""
    log = []
    L._call_llm = _seq_stub([CHALLENGER], log)

    res = L._run_challenger({"direction": "BUY", "score": 7}, "原始数据段")

    assert len(log) == 1, "采样影子未下线, 仍在多打调用"
    assert res["_direction_samples"] == ["BUY"], "下游字段契约必须保留 (单元素)"
    assert res.get("_challenger_prompt_hash")


def test_call_llm_records_finish_reason_and_usage(restore_call_llm):
    """截断诊断的关键判别式: finish_reason == 'length' (区分"模型不听话"与"字数不够")."""
    import urllib.request as ur

    class _Resp:
        def __init__(self, body):
            self._b = body.encode("utf-8")

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    orig = ur.urlopen
    try:
        ur.urlopen = lambda req, timeout=60: _Resp(json.dumps({
            "model": "m", "usage": {"total_tokens": 33},
            "choices": [{"finish_reason": "stop",
                         "message": {"content": '{"direction":"BUY"}'}}]}))
        out = L._call_llm("s", "u")
        assert out["_finish_reason"] == "stop"
        assert out["_usage"]["total_tokens"] == 33

        # 截断: JSON 解析失败, 但元信息必须带回来 (此前是静默 fallback)
        ur.urlopen = lambda req, timeout=60: _Resp(json.dumps({
            "model": "m", "usage": {"total_tokens": 3000},
            "choices": [{"finish_reason": "length",
                         "message": {"content": '{"direction":"BU'}}]}))
        out2 = L._call_llm("s", "u")
        assert "_error" in out2
        assert out2["_finish_reason"] == "length"
        assert out2["_usage"]["total_tokens"] == 3000
    finally:
        ur.urlopen = orig


# ── 8. 修前基线复算入口 (DEB 第13轮 §E 判据用) ────────────────────────────
def test_replay_baseline_if_data_present():
    """回放真实候选算 tripwires<2 占比. 修前基线 = 18/107 = 17% (v2 窗 8/14-8/24).

    **必须按 8/14 切窗** (数据口径七律#2): 不切窗跑全史会得到 ~77% 的假象 —
    早期 opportunity_scan 的 bridge 格式根本不落 `tripwires` 字段, 缺字段被计为违约,
    与"模型没写 tripwire"是两回事。第13轮实测: 全史 299/388=77.1% vs v2 窗 18/107=17%。

    注: bridge 落库不含 bull_case/bear_case → 这两类检查无法由回放覆盖, 故只断言 tripwire 一项。
    数据缺失时跳过 (本测试不制造数据依赖)。
    """
    import glob
    import re

    V2_START = "2026-08-14"  # challenger v2 上线日 = 现行 prompt 形态起点

    def _window(paths, lo=None):
        out = []
        for f in paths:
            m = re.search(r"(\d{4}-\d{2}-\d{2})\.json$", f)
            if m and (lo is None or m.group(1) >= lo):
                out.append(f)
        return out

    all_files = glob.glob("data/opportunity_scan_*.json")
    files = _window(all_files, V2_START)
    if not files:
        pytest.skip("无 v2 窗 (>=%s) 的 opportunity_scan 数据" % V2_START)

    def _load(paths):
        cands = []
        for f in paths:
            try:
                d = json.load(open(f, encoding="utf-8"))
            except Exception:
                continue
            for key in ("candidates", "opportunities", "top_candidates"):
                if isinstance(d.get(key), list):
                    cands.extend(d[key])
                    break
        return cands

    def _lt2_rate(cands):
        lt2 = 0
        for c in cands:
            src = {"direction": str(c.get("direction") or "SKIP").upper(),
                   "skip_reason": c.get("skip_reason", ""),
                   "execution_plan": {"entry_price": c.get("entry_price"),
                                      "stop_loss_price": c.get("stop_loss"),
                                      "take_profit_price": c.get("take_profit"),
                                      "tripwires": c.get("tripwires") or []}}
            if any(v.startswith("tripwires_lt2")
                   for v in L._validate_output_contract(src)["violations"]):
                lt2 += 1
        return lt2, len(cands)

    cands = _load(files)
    if not cands:
        pytest.skip("v2 窗数据文件内无候选")
    lt2, n = _lt2_rate(cands)
    print("\n  [v2 窗 >=%s] tripwires<2 = %d/%d = %.1f%%  (修前基线 17%%)"
          % (V2_START, lt2, n, 100.0 * lt2 / n))

    # 有向分支必须真的被走到 — 否则"零违约"是校验器空操作导致的假通过 (第13轮踩过)
    directional = sum(1 for c in cands
                      if str(c.get("direction") or "").upper() in
                      ("BUY", "SELL_SHORT", "LONG", "SHORT", "SELL"))
    assert directional > 0, "回放未走有向分支, 结果不可信 (词表可能又变了)"


# ══ 9. 第14轮 P8/P9/P12: score_band 契约 + 证伪挑战者 + merge 同向取低 ══════════
class TestRound14:
    """DEB-20260825 第14轮. 核心修复: score 从"模型自由点估值"(塌缩到7.0)改为
    三档可判定枚举+代码映射, 恢复 merge 的 |orig-chall|>3→SKIP 改判规则可触发."""

    def test_score_band_maps_to_number(self):
        """P8: score_band 三档 → 数字, 原始 score 保留为 score_legacy."""
        r = {"score": 7, "score_band": "HIGH"}
        L._apply_score_band(r)
        assert r["score"] == 8.0 and r["score_legacy"] == 7.0
        assert r["_score_band_applied"] == "HIGH"

    def test_score_band_low(self):
        r = {"score_band": "LOW"}
        L._apply_score_band(r)
        assert r["score"] == 3.0

    def test_score_band_absent_no_change(self):
        """向后兼容: 模型没给 band 时不动旧 score (不惩罚)."""
        r = {"score": 7}
        L._apply_score_band(r)
        assert r["score"] == 7 and "score_legacy" not in r

    def test_score_band_off_switch(self):
        """回滚开关 DA_SCORE_BAND=0 → 不应用映射."""
        os.environ["DA_SCORE_BAND"] = "0"
        try:
            r = {"score": 7, "score_band": "HIGH"}
            L._apply_score_band(r)
            assert r["score"] == 7
        finally:
            os.environ.pop("DA_SCORE_BAND", None)

    def test_merge_skip_rule_revived_by_band(self):
        """P8 核心: 分析师HIGH(8) vs 挑战者LOW(3) 真反向 → 档间差5>3 → SKIP.
        修复前: score 都塌缩到7.0, 差上界1.0, 该规则 0/107 结构性死亡."""
        init = {"direction": "BUY", "score_band": "HIGH", "rating": "看多",
                "confidence": "中", "bull_case": [{"thesis": "x", "claim_type": "earnings_beat"}],
                "bear_case": [], "risks": []}
        L._apply_score_band(init)
        ch = {"my_direction": "SELL_SHORT", "my_score_band": "LOW", "agreed": False,
              "verdict": "DISAGREE", "validation": {}, "missed_risks": [], "challenged_claims": []}
        out = L._merge_debate_results(init, ch)
        assert out["direction"] == "SKIP", "档间差应复活>3改判"
        assert out["score"] == 3.0

    def test_p12_challenger_more_conservative_lowers_score(self):
        """丙1 (PROPOSAL-20260905): 同向挑战者分更低 → 不覆写, 影子记录.
        修复前: 取低覆写把 HIGH 档 77% 拉平 8.0→6.0, 打分区分度归零."""
        init = {"direction": "BUY", "score": 7.0, "rating": "看多", "confidence": "中",
                "bull_case": [{"thesis": "x", "claim_type": "earnings_beat"}], "bear_case": [], "risks": []}
        ch = {"my_direction": "BUY", "my_score": 5.0, "agreed": True, "verdict": "AGREE",
              "validation": {}, "missed_risks": [], "challenged_claims": []}
        out = L._merge_debate_results(dict(init), dict(ch))
        assert out["score"] == 7.0
        assert out.get("_challenger_score_shadow") == 5.0
        assert out.get("_score_lowered_by_challenger") is True

    def test_p12_challenger_more_aggressive_no_lower(self):
        """丙1: 挑战者更激进 → 影子照记, 无覆写标记."""
        init = {"direction": "BUY", "score": 7.0, "rating": "看多", "confidence": "中",
                "bull_case": [{"thesis": "x", "claim_type": "earnings_beat"}], "bear_case": [], "risks": []}
        ch = {"my_direction": "BUY", "my_score": 8.0, "agreed": True, "verdict": "AGREE",
              "validation": {}, "missed_risks": [], "challenged_claims": []}
        out = L._merge_debate_results(dict(init), dict(ch))
        assert out["score"] == 7.0
        assert out.get("_challenger_score_shadow") == 8.0
        assert "_score_lowered_by_challenger" not in out

    def test_challenger_band_mapped(self):
        """P9d+丙1: 挑战者 my_score_band → my_score 数字映射进影子分, 不覆写分析师 score."""
        init = {"direction": "BUY", "score": 7.0, "rating": "看多", "confidence": "中",
                "bull_case": [{"thesis": "x", "claim_type": "earnings_beat"}], "bear_case": [], "risks": []}
        ch = {"my_direction": "BUY", "my_score_band": "LOW", "my_score": 7,
              "agreed": True, "verdict": "AGREE", "validation": {}, "missed_risks": [],
              "challenged_claims": []}
        out = L._merge_debate_results(dict(init), ch)
        # LOW→3.0 映射后仅进影子分, 分析师 score 保持 7.0
        assert out["score"] == 7.0
        assert out.get("_challenger_score_shadow") == 3.0
        assert out.get("_score_lowered_by_challenger") is True

    def test_invalidation_triggers_validator(self):
        """P10: 机器可核销失效条件 — 外部标的/自由文本不算, 本股+</>+纯数字才算."""
        base = {"direction": "BUY", "rating": "看多", "score": 7,
                "execution_plan": {"entry_price": 100, "stop_loss_price": 97,
                                   "take_profit_price": 108,
                                   "tripwires": [{"metric": "a", "threshold": "b"},
                                                 {"metric": "c", "threshold": "d"}]},
                "bull_case": [{"claim_type": "earnings_beat", "thesis": "营收增长12%"},
                              {"claim_type": "technical_breakout", "evidence": "股价站上100"}],
                "bear_case": [{"claim_type": "valuation_expensive", "thesis": "PE达到35"},
                              {"claim_type": "macro_headwind", "evidence": "利率维持5%"}]}
        # 外部标的 tripwires 不能回填 → 违约
        r = L._validate_output_contract(base)
        assert any(v.startswith("invalidation_triggers_lt2") for v in r["violations"])
        # 但只报告不repair (不进入定向补全白名单)
        assert "invalidation_triggers" not in r["repairable"]

    def test_invalidation_triggers_backfill_from_tripwires(self):
        """P10 兼容: 旧 tripwires 若符合本股可判定格式 (metric匹配+threshold=<数字) 则回填."""
        base = {"direction": "BUY", "rating": "看多", "score": 7,
                "execution_plan": {"entry_price": 100, "stop_loss_price": 97,
                                   "take_profit_price": 108,
                                   "tripwires": [{"metric": "收盘价", "threshold": "<97"},
                                                 {"metric": "盘前涨跌幅", "threshold": ">3"}]},
                "bull_case": [{"claim_type": "earnings_beat", "thesis": "营收增长12%"},
                              {"claim_type": "technical_breakout", "evidence": "股价站上100"}],
                "bear_case": [{"claim_type": "valuation_expensive", "thesis": "PE达到35"},
                              {"claim_type": "macro_headwind", "evidence": "利率维持5%"}]}
        r = L._validate_output_contract(base)
        assert not any(v.startswith("invalidation_triggers_lt2") for v in r["violations"]), \
            "可判定的旧 tripwires 应回填为满足 invalidation_triggers"

    def test_volume_ratio_metric_replaces_raw_volume(self):
        sample = _ok_long()
        sample["execution_plan"]["invalidation_triggers"] = [
            {"metric": "量比", "operator": ">", "value": 2.0},
            {"metric": "收盘价", "operator": "<", "value": 97},
        ]
        assert not any(v.startswith("invalidation_triggers_lt2")
                       for v in L._validate_output_contract(sample)["violations"])

        sample["execution_plan"]["invalidation_triggers"][0]["metric"] = "成交量"
        assert any(v.startswith("invalidation_triggers_lt2")
                   for v in L._validate_output_contract(sample)["violations"])

    def test_skill_version_sources_are_consistent(self):
        skill_text = (_SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        changelog = (_SKILL_ROOT / "references" / "CHANGELOG.md").read_text(encoding="utf-8")
        pipeline = (_SKILL_ROOT / "scripts" / "pipeline.py").read_text(encoding="utf-8")
        frontmatter_version = next(
            line.split(":", 1)[1].strip()
            for line in skill_text.splitlines() if line.startswith("version:"))
        latest_changelog_version = next(
            line.split()[1].removeprefix("v")
            for line in changelog.splitlines() if line.startswith("## v"))
        assert frontmatter_version == latest_changelog_version == "0.78"
        assert '.Path(__file__).resolve().parents[1] / "SKILL.md"' in pipeline
        assert "^version:" in pipeline
