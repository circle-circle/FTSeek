"""RFC-218 §8 P0 资金分布 code 错配修复契约测试 (DA_CAPITAL_CODE_FIX 独立开关).

被测对象在 skill 仓 (`/root/.hermes/skills/deep-stock-analysis/scripts/`), 不在本仓
→ 全文件以 `skill 不可用即 skip` 保护, 不让 CI 因外部依赖缺失而红.

病灶: Futu provider get_capital_distribution.py 返回 {"code": <ticker字符串>, 八字段},
而 llm_synth/deep_analyzer 两条消费路径误以成功码 `code==0` 判定 → 35/35 份 11-25
有效 payload 正式「## 资金面」块被丢. 修复以独立开关 DA_CAPITAL_CODE_FIX 控制:
  =0(默认): 逐字节保留旧门 (当前生产行为不变);
  =1: 以八字段存在判定有效分布 payload, 正式块恢复.

覆盖点 (按 RFC-218 §8 验收):
  1. 开关双向有效 — flag-off 逐字节复现旧渲染 (含冻结 raw 回放对比真实保存 prompt)
  2. 四态分别正确 — 八字段有效 / 全零(字段在) / 缺字段·空壳 / _error
  3. 分时 fallback 不受开关影响 (main_in_flow 缺省 'N/A' 字符串保留)
  4. deep_analyzer 规则降级路径对称 (flag-on 正式分支写 net_main_flow)
  5. 默认关闭 (回滚态) — 删除开关属性即回旧行为

注意: 本测试只验「搬运层」schema/渲染正确, 不证收益. 激活正式输入须按 RFC-218
§10.4 单变量 A/B (n≥120 且 ≥20 独立 ET 日, 预注册 MDE), 不得以本测试代替经济裁决.
"""
import json
import sys
from pathlib import Path

import pytest

_SKILL = "/root/.hermes/skills/deep-stock-analysis/scripts"
if _SKILL not in sys.path:
    sys.path.insert(0, _SKILL)
try:
    import llm_synth as L
    import config as C
    import deep_analyzer as D
except Exception as _e:  # pragma: no cover - 环境无 skill 时整体跳过
    L = None
    _IMPORT_ERR = str(_e)

pytestmark = pytest.mark.skipif(L is None, reason="deep-stock-analysis skill 不可用")

_CKPT = Path("/opt/ftseek/FTSeek-US/data/da_checkpoint/2026-11-25")

# ── 冻结 payload (BABA 11-25 真实有效分布, 净流入 ≈ +$2.53M) ──
_FIELDS = ("capital_in_super", "capital_out_super", "capital_in_big", "capital_out_big",
           "capital_in_mid", "capital_out_mid", "capital_in_small", "capital_out_small")
VALID = {"code": "US.BABA", "capital_in_super": 5999961.56, "capital_out_super": 2266348.96,
         "capital_in_big": 23731185.321, "capital_out_big": 24934696.359,
         "capital_in_mid": 45391350.571, "capital_out_mid": 46259871.016,
         "capital_in_small": 102309070.871, "capital_out_small": 75841487.155}
ZERO = {"code": "US.ZERO", **{k: 0 for k in _FIELDS}}
EMPTY_SHELL = {"code": "US.EMPTY", "data": {}}          # provider is_empty 形态
ERROR = {"_error": "获取资金分布请求超时"}
FLOW = {"code": "US.BABA", "data": [{"main_in_flow": 123, "super_in_flow": 10, "big_in_flow": 20}]}
FLOW_NA = {"code": "US.BABA", "data": [{"super_in_flow": 0, "big_in_flow": 0}]}  # 日内无 main_in_flow

_FORMAL_HEAD = "## 资金面\n- 主力净流入:"
_FALLBACK_HEAD = "## 资金面 (分时)"


def _render(cap_dist, cap_flow, fix):
    """在指定开关态下渲染资金面 (helper 调用时读 config, monkeypatch 即生效)."""
    old = getattr(C, "DA_CAPITAL_CODE_FIX", None)
    C.DA_CAPITAL_CODE_FIX = fix
    try:
        return L._render_capital_section(cap_dist, cap_flow)
    finally:
        if old is None:
            del C.DA_CAPITAL_CODE_FIX
        else:
            C.DA_CAPITAL_CODE_FIX = old


# ── 1. 开关双向 + 四态 ──────────────────────────────────────────────

def test_flag_off_valid_ticker_dropped_to_fallback():
    """flag-off: 有效 ticker payload 仍被旧 code==0 门丢弃 → 走分时 fallback (复现病灶)."""
    out = _render(VALID, FLOW, fix=False)
    assert len(out) == 1
    assert out[0].startswith(_FALLBACK_HEAD)
    assert _FORMAL_HEAD not in out[0]


def test_flag_on_valid_ticker_renders_formal():
    """flag-on: 有效 ticker payload 恢复正式块, 净流入数值正确 (≈+0.03亿)."""
    out = _render(VALID, FLOW, fix=True)
    assert len(out) == 1
    assert out[0].startswith(_FORMAL_HEAD)
    net_main = (VALID["capital_in_super"] + VALID["capital_in_big"]) - (
        VALID["capital_out_super"] + VALID["capital_out_big"])
    assert f"- 主力净流入: {net_main/1e8:.2f}亿" in out[0]
    assert "0.03亿" in out[0]


def test_flag_off_byte_identical_legacy_fallback():
    """flag-off 渲染与旧 f-string 逐字节一致 (回滚不变量)."""
    out = _render(ERROR, FLOW, fix=False)
    latest = FLOW["data"][-1]
    expected = (f"## 资金面 (分时)\n"
                f"- 最新主力流入: {latest.get('main_in_flow', 'N/A')}\n"
                f"- 超大单流入: {latest.get('super_in_flow', 0)}\n"
                f"- 大单流入: {latest.get('big_in_flow', 0)}")
    assert out == [expected]


def test_zero_fields_flag_on_renders_formal_zero():
    """零值(八字段在): flag-on 渲染正式块净0, 区别于缺值 (零是真实读数, 非缺失)."""
    out = _render(ZERO, {}, fix=True)
    assert len(out) == 1 and out[0].startswith(_FORMAL_HEAD)
    assert "- 主力净流入: 0.00亿" in out[0]
    assert "- 主力占比: 0.00%" in out[0]


def test_zero_fields_flag_off_dropped():
    """零值 flag-off: code!=0 且无 flow → 空 (旧行为)."""
    assert _render(ZERO, {}, fix=False) == []


def test_empty_shell_falls_back_both_flags():
    """空壳 {code,data:{}} 无八字段 → 两态都走 fallback (不误判为有效分布)."""
    for fix in (False, True):
        out = _render(EMPTY_SHELL, FLOW, fix=fix)
        assert len(out) == 1 and out[0].startswith(_FALLBACK_HEAD)


def test_error_falls_back_both_flags():
    """_error → 两态都排除正式块, 走 fallback."""
    for fix in (False, True):
        out = _render(ERROR, FLOW, fix=fix)
        assert len(out) == 1 and out[0].startswith(_FALLBACK_HEAD)


def test_no_data_returns_empty():
    """分布与分时皆错 → 无资金段 (不编数据)."""
    assert _render(ERROR, ERROR, fix=True) == []
    assert _render({}, {}, fix=True) == []


def test_flow_na_string_preserved():
    """分时 main_in_flow 缺省 'N/A' 字符串原样保留 (不被补 0)."""
    out = _render(ERROR, FLOW_NA, fix=True)
    assert "- 最新主力流入: N/A" in out[0]


def test_default_off_rollback():
    """删除开关属性 → getattr 默认 False → 回旧行为 (独立回滚, 不借 DA_INPUT_FULL)."""
    old = getattr(C, "DA_CAPITAL_CODE_FIX", None)
    if hasattr(C, "DA_CAPITAL_CODE_FIX"):
        del C.DA_CAPITAL_CODE_FIX
    try:
        out = L._render_capital_section(VALID, FLOW)
        assert out[0].startswith(_FALLBACK_HEAD)
    finally:
        if old is not None:
            C.DA_CAPITAL_CODE_FIX = old


# ── 2. 冻结 raw 回放 (对比真实保存 prompt) ──────────────────────────

def _load_raw(sym):
    p = _CKPT / f"{sym}.raw.json"
    if not p.exists():
        pytest.skip(f"冻结 checkpoint 不存在: {p}")
    return json.load(open(p))


def test_frozen_raw_baba_flag_off_matches_saved_prompt():
    """BABA: flag-off 渲染的分时块逐字出现在真实保存 prompt 中 (复现历史实际消费)."""
    j = _load_raw("US.BABA")
    col = j["collected"]
    out = _render(col.get("capital_distribution"), col.get("capital_flow"), fix=False)
    assert out, "BABA 应有分时 fallback"
    assert out[0] in j["user_prompt"], "flag-off 渲染须与真实保存 prompt 逐字一致"
    # 历史正式块确实被丢:
    assert _FORMAL_HEAD not in j["user_prompt"]


def test_frozen_raw_baba_flag_on_restores_formal():
    """BABA: flag-on 恢复正式块 (有效 payload 不再被丢)."""
    j = _load_raw("US.BABA")
    col = j["collected"]
    out = _render(col.get("capital_distribution"), col.get("capital_flow"), fix=True)
    assert out[0].startswith(_FORMAL_HEAD)


def test_frozen_raw_uber_flag_on_restores_formal():
    """UBER: 分布有效但分时超时 → flag-off 无资金段, flag-on 恢复正式块."""
    j = _load_raw("US.UBER")
    col = j["collected"]
    off = _render(col.get("capital_distribution"), col.get("capital_flow"), fix=False)
    on = _render(col.get("capital_distribution"), col.get("capital_flow"), fix=True)
    assert off == []                       # 历史: 分布被丢 + 分时超时 → 完全无资金信息
    assert on and on[0].startswith(_FORMAL_HEAD)


# ── 3. deep_analyzer 规则降级路径对称 ───────────────────────────────

def _da_capital(dist, flow, fix):
    old = getattr(C, "DA_CAPITAL_CODE_FIX", None)
    C.DA_CAPITAL_CODE_FIX = fix
    try:
        an = D.DeepAnalyzer({"capital_distribution": dist, "capital_flow": flow})
        an._analyze_capital()
        return an.result.capital
    finally:
        if old is None:
            del C.DA_CAPITAL_CODE_FIX
        else:
            C.DA_CAPITAL_CODE_FIX = old


def test_deep_analyzer_flag_off_fallback_lacks_net_main():
    """flag-off: 有效分布被丢 → 降级分时, 不写 net_main_flow (规则 ±0.5 读 0 的病灶)."""
    cap = _da_capital(VALID, FLOW, fix=False)
    assert "recent_net_flows" in cap
    assert "net_main_flow" not in cap


def test_deep_analyzer_flag_on_formal_writes_net_main():
    """flag-on: 正式分支恢复, net_main_flow 正确 (≈+2530102)."""
    cap = _da_capital(VALID, FLOW, fix=True)
    assert "net_main_flow" in cap
    assert abs(cap["net_main_flow"] - 2530102) <= 1


def test_deep_analyzer_flag_off_byte_identical_legacy():
    """flag-off: 错误分布 + 空分时 → 旧 {_error: capital_flow data empty} 形态不变."""
    cap = _da_capital(ERROR, {"code": "US.X", "data": []}, fix=False)
    assert cap == {"_error": "capital_flow data empty"}
