"""RFC-218 §27 来源状态可见性契约测试 (DA_SOURCE_STATUS_FULL 独立开关).

被测对象在 skill 仓 (`/root/.hermes/skills/deep-stock-analysis/scripts/`), 不在本仓
→ 全文件以 `skill 不可用即 skip` 保护, 不让 CI 因外部依赖缺失而红.

病灶 (§15.7): 「## 数据源状态」仅 news/news_v2/research/sentiment 四 list 源;
dict 型源无 FAILED/EMPTY 区分; 资金分布"采集有数据但渲染门未通过→整段丢弃"
完全不可见 (审计判'完整'却无资金段, 矛盾不可见).
修复以独立开关 DA_SOURCE_STATUS_FULL 控制 (纯呈现层, 计算/采集/19维审计分类不动):
  =0(默认): 逐字节旧4源渲染 (当前生产行为不变);
  =1: 扩面至 dict 型源 FAILED/EMPTY + 渲染丢弃(RENDER_DROPPED)具名披露;
      资金丢弃判定复用 _render_capital_section 同源门; 不引入第二套覆盖计数.

覆盖点:
  1. OFF 逐字节 == 改前备份 (合成状态网格×DA_INPUT_FULL + 67 份真实冻结 checkpoint)
  2. ON 只增「## 数据源状态」段, 其余段落(含覆盖统计/质量审计)逐字节不动
  3. ON dict 源 FAILED/EMPTY 各分支; 资金 RENDER_DROPPED(整段/仅分时fallback)
  4. 资金丢弃与 DA_CAPITAL_CODE_FIX 同源: fix-OFF 丢弃→披露; fix-ON 正式块→无披露
  5. 完整旧数据(各源有效+资金正式块) → ON==OFF==备份 (不增噪)
  6. 与资金/短K/财报三态/DA_INPUT_FULL 开关互不牵连
  7. 消费链现状: 挑战者 _FULL 白名单不含「数据源状态」(独立批, 本批不改, 如实钉死)
  8. 回滚态: 删除开关属性 → getattr 默认 False → 旧行为

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

_BAK_PATH = Path(_SKILL) / "llm_synth.py.bak-20261127-rfc218-srcstatus"
_CKPT_DIRS = [Path("/opt/ftseek/FTSeek-US/data/da_checkpoint/2026-11-25"),
              Path("/opt/ftseek/FTSeek-US/data/da_checkpoint/2026-11-27")]

_MISSING = object()
_CAP_FIELDS = ("capital_in_super", "capital_out_super", "capital_in_big", "capital_out_big",
               "capital_in_mid", "capital_out_mid", "capital_in_small", "capital_out_small")


def _cap(code, val=100.0):
    d = {k: val for k in _CAP_FIELDS}
    d["code"] = code
    return d


def _load_bak():
    loader = importlib.machinery.SourceFileLoader("llm_synth_s27_bak", str(_BAK_PATH))
    spec = importlib.util.spec_from_loader("llm_synth_s27_bak", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _collected(**overrides):
    col = {"snapshot": {"code": "US.TEST", "name": "TEST", "last_price": 100.0},
           "kline": [{"time": "d000", "open": 99.0, "high": 101.0, "low": 98.0,
                      "close": 100.0, "volume": 1000}],
           "_timestamp": 1764200000.0}
    for k, v in overrides.items():
        if v is _MISSING:
            col.pop(k, None)
        else:
            col[k] = v
    return col


def _render(col, full, input_full=None, capital_fix=None):
    old = getattr(C, "DA_SOURCE_STATUS_FULL", None)
    old_full = getattr(C, "DA_INPUT_FULL", None)
    old_cap = getattr(C, "DA_CAPITAL_CODE_FIX", None)
    C.DA_SOURCE_STATUS_FULL = full
    if input_full is not None:
        C.DA_INPUT_FULL = input_full
    if capital_fix is not None:
        C.DA_CAPITAL_CODE_FIX = capital_fix
    try:
        return L._format_data_for_prompt(col)
    finally:
        for name, old_v in (("DA_SOURCE_STATUS_FULL", old), ("DA_INPUT_FULL", old_full),
                            ("DA_CAPITAL_CODE_FIX", old_cap)):
            if old_v is None:
                if hasattr(C, name):
                    delattr(C, name)
            else:
                setattr(C, name, old_v)


def _status_section(text):
    i = text.find("## 数据源状态")
    if i < 0:
        return ""
    j = text.find("\n## ", i + 3)
    return text[i:j] if j >= 0 else text[i:]


def _strip_status(text):
    sec = _status_section(text)
    if not sec:
        return text
    return text.replace(sec + "\n", "", 1) if text.count(sec + "\n") else text.replace(sec, "", 1)


def _section(text, marker):
    i = text.find(marker)
    if i < 0:
        return ""
    j = text.find("\n## ", i + 3)
    return text[i:j] if j >= 0 else text[i:]


# ── 1. 开关形态 + OFF 逐字节 ─────────────────────────────────────────

def test_flag_is_bool():
    assert isinstance(getattr(C, "DA_SOURCE_STATUS_FULL", None), bool)


def test_flag_default_off_fresh_import(monkeypatch):
    monkeypatch.delenv("DA_SOURCE_STATUS_FULL", raising=False)
    loader = importlib.machinery.SourceFileLoader(
        "config_s27_fresh", str(Path(_SKILL) / "config.py"))
    spec = importlib.util.spec_from_loader("config_s27_fresh", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    assert mod.DA_SOURCE_STATUS_FULL is False


_CAP_GRID = [
    ("missing", _MISSING), ("empty_dict", {}), ("error", {"_error": "futu 429"}),
    ("valid_code0", _cap(0)), ("valid_ticker", _cap("US.TEST")),
    ("partial", {"code": "US.TEST", "capital_in_super": 100.0}),
    ("allzero_code0", _cap(0, 0.0)), ("str", "x"), ("int", 42), ("list", []),
]


@pytest.mark.skipif(not _BAK_PATH.exists(), reason="srcstatus 改前备份不在本机")
@pytest.mark.parametrize("name,cap", _CAP_GRID, ids=[g[0] for g in _CAP_GRID])
@pytest.mark.parametrize("full", [True, False])
def test_off_byte_identical_synthetic(name, cap, full):
    bak = _load_bak()
    col = _collected(capital_distribution=cap)
    assert _render(col, False, input_full=full) == bak._format_data_for_prompt(col)


@pytest.mark.skipif(not _BAK_PATH.exists(), reason="srcstatus 改前备份不在本机")
def test_off_byte_identical_real_checkpoints():
    files = [p for d in _CKPT_DIRS for p in sorted(d.glob("*.raw.json"))]
    if not files:
        pytest.skip("冻结 checkpoint 不在本机")
    bak = _load_bak()
    for p in files:
        col = json.load(open(p))["collected"]
        assert _render(col, False) == bak._format_data_for_prompt(col), str(p)


# ── 2. ON 只增「## 数据源状态」段, 其余逐字节不动 ─────────────────────

@pytest.mark.skipif(not _BAK_PATH.exists(), reason="srcstatus 改前备份不在本机")
@pytest.mark.parametrize("name,cap", _CAP_GRID, ids=[g[0] for g in _CAP_GRID])
def test_on_only_touches_status_section(name, cap):
    col = _collected(capital_distribution=cap,
                     fundamentals={"_error": "x"}, analyst_consensus={})
    on = _render(col, True)
    off = _render(col, False)
    assert _strip_status(on) == _strip_status(off)


@pytest.mark.skipif(not _BAK_PATH.exists(), reason="srcstatus 改前备份不在本机")
def test_on_isolation_real_checkpoints():
    files = [p for d in _CKPT_DIRS for p in sorted(d.glob("*.raw.json"))]
    if not files:
        pytest.skip("冻结 checkpoint 不在本机")
    for p in files:
        col = json.load(open(p))["collected"]
        assert _strip_status(_render(col, True)) == _strip_status(_render(col, False)), str(p)


def test_on_does_not_change_coverage_or_audit():
    col = _collected(capital_distribution=_cap("US.TEST"), fundamentals={"_error": "x"})
    on, off = _render(col, True), _render(col, False)
    for marker in ("## 📊 数据覆盖统计", "## 数据质量审计"):
        assert marker in on and marker in off
        assert _section(on, marker) == _section(off, marker)


# ── 3. ON dict 源 FAILED/EMPTY 各分支 ────────────────────────────────

@pytest.mark.parametrize("key,label", [
    ("fundamentals", "基本面指标"), ("analyst_consensus", "分析师评级"),
    ("insider_transactions", "内部人交易"), ("insider_sentiment", "内部人情绪"),
    ("company_profile", "公司概况"), ("market_context", "大盘环境"),
    ("industry_peers", "行业对比"),
])
def test_on_dict_source_failed(key, label):
    col = _collected(**{key: {"_error": "provider 500"}})
    sec = _status_section(_render(col, True))
    assert f"- {label}: FAILED(采集失败" in sec


@pytest.mark.parametrize("key,label", [
    ("fundamentals", "基本面指标"), ("analyst_consensus", "分析师评级"),
    ("insider_transactions", "内部人交易"), ("insider_sentiment", "内部人情绪"),
    ("company_profile", "公司概况"), ("market_context", "大盘环境"),
    ("industry_peers", "行业对比"),
])
def test_on_dict_source_empty(key, label):
    col = _collected(**{key: {}})
    sec = _status_section(_render(col, True))
    assert f"- {label}: EMPTY(源成功但无结果)" in sec


@pytest.mark.parametrize("key,label", [
    ("fundamentals", "基本面指标"), ("analyst_consensus", "分析师评级"),
    ("company_profile", "公司概况"),
])
def test_on_dict_source_non_dict_failed(key, label):
    col = _collected(**{key: "not-a-dict"})
    sec = _status_section(_render(col, True))
    assert f"- {label}: FAILED(采集异常, 非dict" in sec


def test_on_dict_source_ok_no_note():
    col = _collected(fundamentals={"revenue_growth_yoy": 5.0},
                     analyst_consensus={"strong_buy": 3})
    sec = _status_section(_render(col, True))
    assert "基本面指标" not in sec and "分析师评级" not in sec


# ── 4. 资金 RENDER_DROPPED (核心病灶) + 与资金开关同源 ─────────────────

def test_on_capital_render_dropped_whole_section():
    # OFF 资金门 code==0 失败 (code=ticker) + 无分时 fallback → 整段丢弃 → 披露
    col = _collected(capital_distribution=_cap("US.TEST"), capital_flow={})
    txt = _render(col, True, capital_fix=False)
    sec = _status_section(txt)
    assert "资金分布: RENDER_DROPPED" in sec
    assert "整段未呈现" in sec and "模型实际无资金数据" in sec
    assert "- 主力净流入:" not in txt  # 正式资金面块确未渲染


def test_on_capital_render_dropped_fallback_only():
    col = _collected(capital_distribution=_cap("US.TEST"),
                     capital_flow={"code": "US.TEST", "data": [{"main_in_flow": 5}]})
    sec = _status_section(_render(col, True, capital_fix=False))
    assert "资金分布: RENDER_DROPPED" in sec
    assert "仅呈现分时fallback" in sec


def test_on_capital_fix_on_formal_renders_no_disclosure():
    # 资金 fix-ON → 八字段完整 → 正式块渲染 → 无 RENDER_DROPPED (同源门)
    col = _collected(capital_distribution=_cap("US.TEST"), capital_flow={})
    txt = _render(col, True, capital_fix=True)
    assert "## 资金面\n" in txt
    assert "RENDER_DROPPED" not in _status_section(txt)


def test_on_capital_valid_code0_no_disclosure():
    # 完整旧数据 code==0 → OFF 门通过 → 正式块 → 无披露
    col = _collected(capital_distribution=_cap(0),
                     capital_flow={"code": 0, "data": [{"main_in_flow": 1}]})
    txt = _render(col, True, capital_fix=False)
    assert "## 资金面\n" in txt
    assert "资金分布" not in _status_section(txt)


def test_on_capital_failed():
    col = _collected(capital_distribution={"_error": "futu 429"})
    sec = _status_section(_render(col, True))
    assert "资金分布: FAILED(采集失败" in sec


def test_on_capital_empty():
    col = _collected(capital_distribution={})
    sec = _status_section(_render(col, True))
    assert "资金分布: EMPTY(源成功但无结果)" in sec


def test_on_capital_flow_failed_and_empty():
    sec_f = _status_section(_render(_collected(capital_flow={"_error": "x"}), True))
    assert "资金分时: FAILED" in sec_f
    sec_e = _status_section(_render(_collected(capital_flow={}), True))
    assert "资金分时: EMPTY" in sec_e


def test_render_dropped_consistent_with_audit_ok():
    # RENDER_DROPPED 仅在审计判'完整'(原始payload有信息量)时触发 → 解释矛盾而非制造矛盾
    col = _collected(capital_distribution=_cap("US.TEST"))
    txt = _render(col, True, capital_fix=False)
    audit = _section(txt, "## 数据质量审计")
    missing_line = next((l for l in audit.splitlines() if l.startswith("- 缺失明细")), "")
    assert "capital_distribution" not in missing_line
    assert "RENDER_DROPPED" in _status_section(txt)


# ── 5. 完整旧数据 → ON==OFF==备份 (不增噪) ───────────────────────────

def _complete_collected():
    return _collected(
        capital_distribution=_cap(0),
        capital_flow={"code": 0, "data": [{"main_in_flow": 1}]},
        fundamentals={"revenue_growth_yoy": 5.0},
        analyst_consensus={"strong_buy": 3},
        insider_transactions={"recent_buy_count": 2},
        insider_sentiment={"avg_mspr_6m": 0.5},
        company_profile={"industry": "Tech"},
        market_context={"spy": {"current": 500.0}},
        industry_peers={"industry": "Tech", "peers": [{"symbol": "X", "pe": 20}]},
        news=[{"title": "n", "publish_time": "1764200000"}],
        news_v2=[{"headline": "h"}], research=[{"title": "r"}],
        sentiment=[{"text": "s"}],
    )


@pytest.mark.skipif(not _BAK_PATH.exists(), reason="srcstatus 改前备份不在本机")
def test_on_complete_old_data_unchanged():
    bak = _load_bak()
    col = _complete_collected()
    on = _render(col, True)
    assert on == _render(col, False) == bak._format_data_for_prompt(col)
    assert _status_section(on) == ""


# ── 6. 开关互不牵连 ──────────────────────────────────────────────────

def test_independent_from_earnings_tristate(monkeypatch):
    col = _collected(capital_distribution=_cap("US.TEST"))
    monkeypatch.setattr(C, "DA_EARNINGS_TRISTATE", True, raising=False)
    a = _render(col, True)
    monkeypatch.setattr(C, "DA_EARNINGS_TRISTATE", False, raising=False)
    b = _render(col, True)
    assert _status_section(a) == _status_section(b)


def test_independent_from_shortk(monkeypatch):
    col = _collected(capital_distribution=_cap("US.TEST"))
    monkeypatch.setattr(C, "DA_SHORTK_NA", True, raising=False)
    a = _status_section(_render(col, True))
    monkeypatch.setattr(C, "DA_SHORTK_NA", False, raising=False)
    b = _status_section(_render(col, True))
    assert a == b


@pytest.mark.parametrize("input_full", [True, False])
def test_status_notes_invariant_to_input_full(input_full):
    col = _collected(capital_distribution=_cap("US.TEST"), fundamentals={"_error": "x"})
    sec = _status_section(_render(col, True, input_full=input_full))
    assert "资金分布: RENDER_DROPPED" in sec and "基本面指标: FAILED" in sec


# ── 7. 消费链现状 (挑战者白名单缺口=独立批, 本批不改, 如实钉死) ────────

def test_challenger_whitelist_status_section_absent_current():
    # §15.7 挑战者事实一致性为独立批; 本批不动白名单. 钉死现状: 「数据源状态」不在 _FULL.
    import inspect
    src = inspect.getsource(L._challenger_data_view)
    assert "数据源状态" not in src
    assert "数据质量审计" in src  # 审计段在白名单 (互补口径挑战者可见)


# ── 8. 回滚态 ────────────────────────────────────────────────────────

@pytest.mark.skipif(not _BAK_PATH.exists(), reason="srcstatus 改前备份不在本机")
def test_rollback_flag_removed_defaults_off():
    bak = _load_bak()
    col = _collected(capital_distribution=_cap("US.TEST"), fundamentals={"_error": "x"})
    old = getattr(C, "DA_SOURCE_STATUS_FULL", None)
    if hasattr(C, "DA_SOURCE_STATUS_FULL"):
        del C.DA_SOURCE_STATUS_FULL
    try:
        assert L._format_data_for_prompt(col) == bak._format_data_for_prompt(col)
    finally:
        if old is not None:
            C.DA_SOURCE_STATUS_FULL = old
