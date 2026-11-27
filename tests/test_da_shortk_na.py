"""RFC-218 §24 短K样本不足显式 N/A 契约测试 (DA_SHORTK_NA 独立开关).

被测对象在 skill 仓 (`/root/.hermes/skills/deep-stock-analysis/scripts/`), 不在本仓
→ 全文件以 `skill 不可用即 skip` 保护, 不让 CI 因外部依赖缺失而红.

病灶: K线不足窗口时 MA5/10/20/60、chg5(需6)/chg20(需21)、量比(需20) 被零填充 →
prompt 出现 "MA60: 0.00 / +0.0% / 0.00x" 伪值, LLM 可能按真实值推断趋势/震荡.
修复以独立开关 DA_SHORTK_NA 控制 (呈现层, 计算逻辑两态逐字一致):
  =0(默认): 逐字节旧零填充渲染 (当前生产行为不变);
  =1: 样本不足指标 "N/A(仅N根,需M)" + 实际根数标题 + 覆盖行 + 不得按0解读警示;
      量比在 ≥20 根但 20 日均量=0 时 "N/A(20日均量为0)".

覆盖点:
  1. OFF 逐字节 == 改前备份 (合成 n 网格 + 零量/零close + 35 份真实冻结 checkpoint)
  2. ON 各窗口边界分别正确 (n=3/6/21/61; 空K无段; 零量两种文案)
  3. ON 可算指标值与 OFF 完全一致 (计算不动) + K线明细行不动 + 非技术面段落不动
  4. DA_INPUT_FULL 截断正交 (ON 标题=实际根数, 明细仍按截断)
  5. 回滚态: 删除开关属性 → getattr 默认 False → 旧行为
  6. 与 DA_CAPITAL_CODE_FIX 互不牵连 (资金段两态一致)

注意: 本测试只验呈现层正确, 不证收益. 激活正式输入须按 RFC-218 §10.4 单变量 A/B
(n≥120 且 ≥20 独立 ET 日, 预注册 MDE), 不得以本测试代替经济裁决.
"""
import importlib.machinery
import importlib.util
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
except Exception as _e:  # pragma: no cover - 环境无 skill 时整体跳过
    L = None
    _IMPORT_ERR = str(_e)

pytestmark = pytest.mark.skipif(L is None, reason="deep-stock-analysis skill 不可用")

_BAK_PATH = Path(_SKILL) / "llm_synth.py.bak-20261127-rfc218-shortk"
_CKPT = Path("/opt/ftseek/FTSeek-US/data/da_checkpoint/2026-11-25")


def _load_bak():
    loader = importlib.machinery.SourceFileLoader("llm_synth_s24_bak", str(_BAK_PATH))
    spec = importlib.util.spec_from_loader("llm_synth_s24_bak", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _collected(n, zero_volume=False, zero_close_at=None):
    bars = []
    for i in range(n):
        c = 100.0 + i * 0.5
        if zero_close_at is not None and i == zero_close_at:
            c = 0
        bars.append({"time": f"d{i:03d}", "open": c - 0.2, "high": c + 0.6,
                     "low": c - 0.4, "close": c,
                     "volume": 0 if zero_volume else 1000 + i * 10})
    return {"snapshot": {"code": "US.TEST", "name": "TEST", "last_price": 100.0},
            "kline": bars, "_timestamp": 1764200000.0}


def _render(col, fix, input_full=None):
    """在指定开关态下渲染整段 prompt (helper 调用时读 config, monkeypatch 即生效)."""
    old = getattr(C, "DA_SHORTK_NA", None)
    old_full = getattr(C, "DA_INPUT_FULL", None)
    C.DA_SHORTK_NA = fix
    if input_full is not None:
        C.DA_INPUT_FULL = input_full
    try:
        return L._format_data_for_prompt(col)
    finally:
        if old is None:
            del C.DA_SHORTK_NA
        else:
            C.DA_SHORTK_NA = old
        if input_full is not None:
            if old_full is None:
                del C.DA_INPUT_FULL
            else:
                C.DA_INPUT_FULL = old_full


def _tech(text):
    i = text.find("## 技术面")
    if i < 0:
        return ""
    j = text.find("\n## ", i + 3)
    return text[i:j] if j >= 0 else text[i:]


# ── 1. 开关形态 + OFF 逐字节 ─────────────────────────────────────────

def test_flag_is_bool():
    assert isinstance(getattr(C, "DA_SHORTK_NA", None), bool)


@pytest.mark.skipif(not _BAK_PATH.exists(), reason="shortk 改前备份不在本机")
@pytest.mark.parametrize("n,zv,zc", [
    (0, False, None), (1, False, None), (3, False, None), (5, False, None),
    (6, False, None), (10, False, None), (20, False, None), (21, False, None),
    (60, False, None), (61, False, None),
    (25, True, None), (61, True, None), (61, False, 10),
])
def test_off_byte_identical_synthetic(n, zv, zc):
    bak = _load_bak()
    col = _collected(n, zv, zc)
    assert _render(col, False) == bak._format_data_for_prompt(col)


@pytest.mark.skipif(not _BAK_PATH.exists(), reason="shortk 改前备份不在本机")
@pytest.mark.parametrize("full", [True, False])
def test_off_byte_identical_input_full_truncation(full):
    bak = _load_bak()
    col = _collected(61)
    old_full = getattr(C, "DA_INPUT_FULL", None)
    C.DA_INPUT_FULL = full
    try:
        assert _render(col, False) == bak._format_data_for_prompt(col)
    finally:
        if old_full is None:
            del C.DA_INPUT_FULL
        else:
            C.DA_INPUT_FULL = old_full


@pytest.mark.skipif(not _BAK_PATH.exists(), reason="shortk 改前备份不在本机")
def test_off_byte_identical_real_checkpoints():
    files = sorted(_CKPT.glob("*.raw.json"))
    if not files:
        pytest.skip("11-25 冻结 checkpoint 不在本机")
    bak = _load_bak()
    for p in files:
        col = json.load(open(p))["collected"]
        assert _render(col, False) == bak._format_data_for_prompt(col), p.name


# ── 2. ON 窗口边界 ──────────────────────────────────────────────────

def test_on_n3_all_na():
    t = _tech(_render(_collected(3), True))
    assert "## 技术面 (3日K线)" in t
    for token in ("MA5: N/A(仅3根,需5)", "MA10: N/A(仅3根,需10)",
                  "MA20: N/A(仅3根,需20)", "MA60: N/A(仅3根,需60)",
                  "5日涨跌: N/A(仅3根,需6)", "20日涨跌: N/A(仅3根,需21)",
                  "量比(20日均量): N/A(仅3根,需20)"):
        assert token in t, token
    assert "K线覆盖: 3/3根close>0 | 末根: d002" in t
    assert "不得按0解读" in t
    # 伪值不得再出现在指标行
    ma_line = next(x for x in t.splitlines() if x.startswith("- MA5:"))
    assert "0.00" not in ma_line


def test_on_n6_partial():
    col = _collected(6)
    t = _tech(_render(col, True))
    closes = [k["close"] for k in col["kline"]]
    ma5 = sum(closes[-5:]) / 5
    chg5 = (closes[-1] - closes[-6]) / closes[-6] * 100
    assert f"MA5: {ma5:.2f}" in t
    assert f"5日涨跌: {chg5:+.1f}%" in t
    assert "MA10: N/A(仅6根,需10)" in t
    assert "MA60: N/A(仅6根,需60)" in t
    assert "20日涨跌: N/A(仅6根,需21)" in t
    assert "量比(20日均量): N/A(仅6根,需20)" in t


def test_on_n21_only_ma60_na():
    col = _collected(21)
    t = _tech(_render(col, True))
    closes = [k["close"] for k in col["kline"]]
    volumes = [k["volume"] for k in col["kline"]]
    assert f"MA5: {sum(closes[-5:]) / 5:.2f}" in t
    assert f"MA10: {sum(closes[-10:]) / 10:.2f}" in t
    assert f"MA20: {sum(closes[-20:]) / 20:.2f}" in t
    assert "MA60: N/A(仅21根,需60)" in t
    chg20 = (closes[-1] - closes[-21]) / closes[-21] * 100
    assert f"20日涨跌: {chg20:+.1f}%" in t
    vr = volumes[-1] / (sum(volumes[-20:]) / 20)
    assert f"量比(20日均量): {vr:.2f}x" in t


def test_on_n61_all_computable_values_equal_off():
    col = _collected(61)
    on = _tech(_render(col, True))
    off = _tech(_render(col, False))
    assert "N/A(" not in on
    assert "## 技术面 (61日K线)" in on
    assert "K线覆盖: 61/61根close>0 | 末根: d060" in on
    for line in off.splitlines():
        if line.startswith(("- MA5:", "- 5日涨跌:", "- 量比")):
            assert line in on, line


def test_on_zero_volume_wording():
    t25 = _tech(_render(_collected(25, zero_volume=True), True))
    assert "量比(20日均量): N/A(20日均量为0)" in t25
    assert "MA20: " in t25 and "N/A(仅25根" not in t25.split("MA20:")[1].split("|")[0]
    t61 = _tech(_render(_collected(61, zero_volume=True), True))
    assert "量比(20日均量): N/A(20日均量为0)" in t61
    assert "N/A(仅61根" not in t61


def test_empty_kline_no_section_both_modes():
    col = _collected(0)
    assert "## 技术面" not in _render(col, True)
    assert "## 技术面" not in _render(col, False)


def test_on_coverage_counts_positive_closes_only():
    col = _collected(61, zero_close_at=10)
    t = _tech(_render(col, True))
    assert "K线覆盖: 60/61根close>0" in t
    # 计算不动: MA60 仍按含 0 的原公式
    closes = [k["close"] for k in col["kline"]]
    assert f"MA60: {sum(closes[-60:]) / 60:.2f}" in t


# ── 3. 只动呈现层: 明细行/非技术面段落不动 ──────────────────────────

@pytest.mark.parametrize("n", [3, 61])
def test_kline_detail_rows_unchanged(n):
    col = _collected(n)
    on = _tech(_render(col, True))
    off = _tech(_render(col, False))
    marker = f"近{n}日K线:"
    assert marker in on and marker in off
    assert on[on.find(marker):] == off[off.find(marker):]


def test_non_tech_sections_unchanged():
    col = _collected(3)
    on = _render(col, True)
    off = _render(col, False)
    assert on.replace(_tech(on), "") == off.replace(_tech(off), "")


# ── 4. DA_INPUT_FULL 正交 ───────────────────────────────────────────

def test_input_full_truncation_orthogonal():
    col = _collected(61)
    t = _tech(_render(col, True, input_full=False))
    assert "## 技术面 (61日K线)" in t
    assert "近5日K线:" in t
    assert "近61日K线:" not in t


# ── 5. 回滚态 ───────────────────────────────────────────────────────

def test_delattr_rollback_old_behavior():
    old = getattr(C, "DA_SHORTK_NA", None)
    if hasattr(C, "DA_SHORTK_NA"):
        del C.DA_SHORTK_NA
    try:
        col = _collected(3)
        t = _tech(L._format_data_for_prompt(col))
        assert "## 技术面 (60日K线)" in t
        assert "MA60: 0.00" in t
        assert "N/A(" not in t
    finally:
        if old is not None:
            C.DA_SHORTK_NA = old


# ── 6. 与资金开关互不牵连 ───────────────────────────────────────────

def test_capital_section_independent():
    fields = ("capital_in_super", "capital_out_super", "capital_in_big",
              "capital_out_big", "capital_in_mid", "capital_out_mid",
              "capital_in_small", "capital_out_small")
    col = _collected(3)
    col["capital_distribution"] = {"code": "US.TEST", **{k: 1.0 for k in fields}}
    col["capital_flow"] = {"code": "US.TEST",
                           "data": [{"main_in_flow": 5, "super_in_flow": 1, "big_in_flow": 2}]}

    def cap_sec(text):
        for head in ("## 资金面\n", "## 资金面 (分时)"):
            i = text.find(head)
            if i >= 0:
                j = text.find("\n## ", i + 3)
                return text[i:j] if j >= 0 else text[i:]
        return ""

    assert cap_sec(_render(col, True)) == cap_sec(_render(col, False))
    assert cap_sec(_render(col, True)) != ""
