"""RFC-218 §26 健康空财报三态契约测试 (DA_EARNINGS_TRISTATE 独立开关).

被测对象在 skill 仓 (`/root/.hermes/skills/deep-stock-analysis/scripts/`), 不在本仓
→ 全文件以 `skill 不可用即 skip` 保护, 不让 CI 因外部依赖缺失而红.

病灶 (§15.7): next_earnings 为空时「## 财报日历」整段静默省略, 模型无法区分
「窗口内未发现事件(源健康)／源失败或降级／未提供」. 11-25/11-27 归档 65/67 为
健康空 (内部日历健康+窗口内无财报=有效信息), 全部被当缺失丢弃.
修复以独立开关 DA_EARNINGS_TRISTATE 控制 (呈现层, 计算/采集/覆盖分类两态一致):
  =0(默认): 逐字节旧渲染 (当前生产行为不变);
  =1: 空态渲染三态状态行 + 查询窗口(采集器声明值) + as-of(采集时点ET);
      next_earnings 存在时两态均逐字节旧渲染.

覆盖点:
  1. OFF 逐字节 == 改前备份 (合成状态网格×DA_INPUT_FULL + 67 份真实冻结 checkpoint)
  2. ON next_earnings 存在 → 逐字节 == OFF (合成 + 真实)
  3. ON 三态各分支文案/窗口/as-of/警示正确; 非法窗口不编造; 非法时点 N/A 不崩
  4. ON 只增财报段, 其余段落逐字节不动; 与短K/资金开关互不牵连
  5. 消费链: 挑战者 _FULL 白名单放行新段; 溯源校验 full 模式新行数字可验证
  6. 回滚态: 删除开关属性 → getattr 默认 False → 旧行为

注意: 本测试只验呈现层正确, 不证收益. 激活正式输入须按 RFC-218 §10.4 单变量 A/B
(n≥120 且 ≥20 独立 ET 日, 预注册 MDE), 不得以本测试代替经济裁决.
"""
import importlib.machinery
import importlib.util
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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

_BAK_PATH = Path(_SKILL) / "llm_synth.py.bak-20261127-rfc218-earnings"
_CKPT_DIRS = [Path("/opt/ftseek/FTSeek-US/data/da_checkpoint/2026-11-25"),
              Path("/opt/ftseek/FTSeek-US/data/da_checkpoint/2026-11-27")]

_MISSING = object()  # sentinel: collected 不含 earnings_calendar 键

_HEALTHY = {"next_earnings": None, "all_upcoming": [],
            "_source": "ftseek/earnings_calendar", "_quality": "ok",
            "lookahead_days": 7,
            "_note": "internal calendar healthy; no earnings in lookahead window"}
_DEGRADED = {"next_earnings": None, "all_upcoming": [],
             "_source": "finnhub/calendar/earnings", "_quality": "degraded",
             "_degraded_reason": "无未来财报日期(API仅返回已过期记录)"}
_UNMARKED = {"next_earnings": None, "all_upcoming": [],
             "_source": "finnhub/calendar/earnings"}
_ERROR = {"_error": "finnhub 429 rate limit"}
_HAS_NEXT = {"next_earnings": {"date": "2026-12-10", "epsEstimate": 1.23,
                               "revenueEstimate": 456000000},
             "all_upcoming": [{"date": "2026-12-10"}],
             "_source": "finnhub/calendar/earnings"}
_HAS_NEXT_MIN = {"next_earnings": {"date": "2026-12-10"},
                 "_source": "finnhub/calendar/earnings"}

_TS = 1764200000.0  # 固定采集时点 → as-of 可独立复算


def _load_bak():
    loader = importlib.machinery.SourceFileLoader("llm_synth_s26_bak", str(_BAK_PATH))
    spec = importlib.util.spec_from_loader("llm_synth_s26_bak", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _collected(earnings=_MISSING, ts=_TS, n_bars=3):
    bars = [{"time": f"d{i:03d}", "open": 99.0 + i, "high": 101.0 + i,
             "low": 98.0 + i, "close": 100.0 + i, "volume": 1000 + i * 10}
            for i in range(n_bars)]
    col = {"snapshot": {"code": "US.TEST", "name": "TEST", "last_price": 100.0},
           "kline": bars}
    if ts is not _MISSING:
        col["_timestamp"] = ts
    if earnings is not _MISSING:
        col["earnings_calendar"] = earnings
    return col


def _render(col, fix, input_full=None):
    old = getattr(C, "DA_EARNINGS_TRISTATE", None)
    old_full = getattr(C, "DA_INPUT_FULL", None)
    C.DA_EARNINGS_TRISTATE = fix
    if input_full is not None:
        C.DA_INPUT_FULL = input_full
    try:
        return L._format_data_for_prompt(col)
    finally:
        if old is None:
            del C.DA_EARNINGS_TRISTATE
        else:
            C.DA_EARNINGS_TRISTATE = old
        if input_full is not None:
            if old_full is None:
                del C.DA_INPUT_FULL
            else:
                C.DA_INPUT_FULL = old_full


def _earn_section(text):
    i = text.find("## 财报日历")
    if i < 0:
        return ""
    j = text.find("\n## ", i + 3)
    return text[i:j] if j >= 0 else text[i:]


def _expected_asof(ts):
    return datetime.fromtimestamp(ts, ZoneInfo("America/New_York")).strftime(
        "%Y-%m-%d %H:%M") + "(ET)"


# ── 1. 开关形态 + OFF 逐字节 ─────────────────────────────────────────

def test_flag_is_bool():
    assert isinstance(getattr(C, "DA_EARNINGS_TRISTATE", None), bool)


def test_flag_default_off_fresh_import(monkeypatch):
    monkeypatch.delenv("DA_EARNINGS_TRISTATE", raising=False)
    loader = importlib.machinery.SourceFileLoader(
        "config_s26_fresh", str(Path(_SKILL) / "config.py"))
    spec = importlib.util.spec_from_loader("config_s26_fresh", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    assert mod.DA_EARNINGS_TRISTATE is False


_GRID = [
    ("missing", _MISSING), ("none", None), ("empty_dict", {}),
    ("healthy", _HEALTHY),
    ("healthy_no_window", {k: v for k, v in _HEALTHY.items() if k != "lookahead_days"}),
    ("degraded", _DEGRADED), ("unmarked", _UNMARKED), ("error", _ERROR),
    ("has_next", _HAS_NEXT), ("has_next_min", _HAS_NEXT_MIN),
    ("next_empty_dict", {"next_earnings": {}}),
    ("list", []), ("str", "x"), ("int", 42),
]


@pytest.mark.skipif(not _BAK_PATH.exists(), reason="earnings 改前备份不在本机")
@pytest.mark.parametrize("name,earnings", _GRID, ids=[g[0] for g in _GRID])
@pytest.mark.parametrize("full", [True, False])
def test_off_byte_identical_synthetic(name, earnings, full):
    bak = _load_bak()
    col = _collected(earnings)
    assert _render(col, False, input_full=full) == bak._format_data_for_prompt(col)


@pytest.mark.skipif(not _BAK_PATH.exists(), reason="earnings 改前备份不在本机")
def test_off_byte_identical_real_checkpoints():
    files = [p for d in _CKPT_DIRS for p in sorted(d.glob("*.raw.json"))]
    if not files:
        pytest.skip("冻结 checkpoint 不在本机")
    bak = _load_bak()
    for p in files:
        col = json.load(open(p))["collected"]
        assert _render(col, False) == bak._format_data_for_prompt(col), str(p)


# ── 2. ON next_earnings 存在 → 逐字节旧渲染 ─────────────────────────

@pytest.mark.skipif(not _BAK_PATH.exists(), reason="earnings 改前备份不在本机")
@pytest.mark.parametrize("earnings", [_HAS_NEXT, _HAS_NEXT_MIN], ids=["full", "min"])
def test_on_has_next_byte_identical_synthetic(earnings):
    bak = _load_bak()
    col = _collected(earnings)
    on = _render(col, True)
    assert on == _render(col, False) == bak._format_data_for_prompt(col)
    assert "下次财报: 2026-12-10" in on


@pytest.mark.skipif(not _BAK_PATH.exists(), reason="earnings 改前备份不在本机")
def test_on_has_next_byte_identical_real_checkpoints():
    files = [p for d in _CKPT_DIRS for p in sorted(d.glob("*.raw.json"))]
    if not files:
        pytest.skip("冻结 checkpoint 不在本机")
    bak = _load_bak()
    checked = 0
    for p in files:
        col = json.load(open(p))["collected"]
        e = col.get("earnings_calendar")
        if isinstance(e, dict) and "_error" not in e and e.get("next_earnings"):
            assert _render(col, True) == bak._format_data_for_prompt(col), str(p)
            checked += 1
    assert checked >= 1, "两日归档应至少 1 份 has_next"


# ── 3. ON 三态各分支 ─────────────────────────────────────────────────

def test_on_healthy_empty():
    col = _collected(_HEALTHY)
    sec = _earn_section(_render(col, True))
    assert "查询窗口内未发现财报事件" in sec
    assert "源健康" in sec and "有效信息" in sec
    assert "查询窗口: 未来7天" in sec
    assert "数据源: ftseek/earnings_calendar" in sec
    assert f"as-of: {_expected_asof(_TS)}" in sec
    assert "不得编造日期" in sec
    assert "窗口之外的财报日期未知" in sec
    # 健康空=已确认信息, 不得带「未知≠无财报」警示, 也不得出现旧「下次财报」行
    assert "未知≠无财报" not in sec
    assert "下次财报" not in sec


@pytest.mark.parametrize("bad", [True, 0, -1, 7.0, "7", None, 400],
                         ids=["bool", "zero", "neg", "float", "str", "none", "gt366"])
def test_on_healthy_window_invalid_not_fabricated(bad):
    e = dict(_HEALTHY, lookahead_days=bad)
    sec = _earn_section(_render(_collected(e), True))
    assert "查询窗口内未发现财报事件" in sec
    assert "未声明" in sec
    assert "未来7天" not in sec and "未来0天" not in sec


def test_on_healthy_window_366_valid():
    sec = _earn_section(_render(_collected(dict(_HEALTHY, lookahead_days=366)), True))
    assert "查询窗口: 未来366天" in sec


def test_on_degraded():
    sec = _earn_section(_render(_collected(_DEGRADED), True))
    assert "财报日历降级" in sec and "财报风险未知" in sec
    assert "无未来财报日期(API仅返回已过期记录)" in sec
    assert "数据源: finnhub/calendar/earnings" in sec
    assert "未知≠无财报" in sec and "不得编造财报日期" in sec


def test_on_unmarked_empty():
    sec = _earn_section(_render(_collected(_UNMARKED), True))
    assert "财报日期未确认" in sec
    assert "采集器未声明源健康" in sec
    assert "未知≠无财报" in sec


def test_on_error():
    sec = _earn_section(_render(_collected(_ERROR), True))
    assert "财报日历源失败" in sec and "财报风险未知" in sec
    assert "finnhub 429 rate limit" in sec
    assert "未知≠无财报" in sec


def test_on_error_truncated_120():
    sec = _earn_section(_render(_collected({"_error": "x" * 300}), True))
    assert "x" * 120 in sec
    assert "x" * 121 not in sec


def test_on_source_truncated_80():
    e = dict(_HEALTHY, _source="s" * 200)
    sec = _earn_section(_render(_collected(e), True))
    assert "s" * 80 in sec
    assert "s" * 81 not in sec


def test_on_missing_key():
    sec = _earn_section(_render(_collected(_MISSING), True))
    assert "未提供财报日历数据" in sec
    assert "未知≠无财报" in sec


def test_on_none_value():
    sec = _earn_section(_render(_collected(None), True))
    assert "未提供财报日历数据" in sec


@pytest.mark.parametrize("val", [[], "x", 42], ids=["list", "str", "int"])
def test_on_non_dict(val):
    sec = _earn_section(_render(_collected(val), True))
    assert "未提供财报日历数据" in sec


def test_on_provided_empty_dict_is_unconfirmed_not_missing():
    sec = _earn_section(_render(_collected({}), True))
    assert "财报日期未确认" in sec
    assert "未提供" not in sec


# ── 4. as-of ─────────────────────────────────────────────────────────

def test_asof_from_collection_timestamp():
    sec = _earn_section(_render(_collected(_HEALTHY, ts=_TS), True))
    assert f"as-of: {_expected_asof(_TS)}" in sec


@pytest.mark.parametrize("ts", [_MISSING, None, True, float("nan"), float("inf"),
                                -5.0, "1764200000", 0],
                         ids=["missing", "none", "bool", "nan", "inf", "neg", "str", "zero"])
def test_asof_invalid_na_no_crash(ts):
    sec = _earn_section(_render(_collected(_HEALTHY, ts=ts), True))
    assert "as-of: N/A" in sec
    assert "查询窗口内未发现财报事件" in sec


# ── 5. 段落隔离 + 开关正交 ───────────────────────────────────────────

def test_on_only_adds_earnings_section():
    col = _collected(_HEALTHY)
    on = _render(col, True)
    off = _render(col, False)
    sec = L._earnings_tristate_section(col, _HEALTHY)
    assert sec in on
    assert on.replace(sec + "\n\n", "", 1) == off


def test_independent_from_shortk_and_capital():
    col = _collected(_HEALTHY)
    base = _earn_section(_render(col, True))
    olds = {a: getattr(C, a, None) for a in ("DA_SHORTK_NA", "DA_CAPITAL_CODE_FIX")}
    C.DA_SHORTK_NA = True
    C.DA_CAPITAL_CODE_FIX = True
    try:
        assert _earn_section(_render(col, True)) == base
    finally:
        for a, v in olds.items():
            if v is None:
                delattr(C, a)
            else:
                setattr(C, a, v)


def test_tech_section_unaffected_by_earnings_flag():
    col = _collected(_HEALTHY, n_bars=3)
    on = _render(col, True)
    off = _render(col, False)
    i_on, i_off = on.find("## 技术面"), off.find("## 技术面")
    j_on, j_off = on.find("\n## ", i_on + 3), off.find("\n## ", i_off + 3)
    assert on[i_on:j_on] == off[i_off:j_off]


# ── 6. 消费链: 挑战者视图 + 溯源校验 ─────────────────────────────────

def test_challenger_view_passes_healthy_empty(monkeypatch):
    monkeypatch.setenv("DA_CHALLENGER_FULL_DATA", "1")
    on = _render(_collected(_HEALTHY), True)
    view = L._challenger_data_view(on)
    assert "查询窗口内未发现财报事件" in view
    assert "查询窗口: 未来7天" in view


def test_challenger_view_passes_error(monkeypatch):
    monkeypatch.setenv("DA_CHALLENGER_FULL_DATA", "1")
    on = _render(_collected(_ERROR), True)
    assert "财报日历源失败" in L._challenger_data_view(on)


def test_verifier_window_number_sourced(monkeypatch):
    monkeypatch.setenv("DA_SOURCE_VERIFY_MODE", "full")
    col = _collected(_HEALTHY)
    on = _render(col, True)
    result = {"bull_case": [{"thesis": "未来7天内无财报事件", "evidence": "财报日历状态行"}]}
    v = L._verify_sourced_numbers(result, col, data_text=on)
    assert v is not None
    assert v["unverified_count"] == 0
    assert v["verified_count"] >= 1


# ── 7. 回滚态 ────────────────────────────────────────────────────────

@pytest.mark.skipif(not _BAK_PATH.exists(), reason="earnings 改前备份不在本机")
def test_rollback_attr_deleted_defaults_off():
    bak = _load_bak()
    col = _collected(_HEALTHY)
    old = getattr(C, "DA_EARNINGS_TRISTATE", None)
    if hasattr(C, "DA_EARNINGS_TRISTATE"):
        del C.DA_EARNINGS_TRISTATE
    try:
        assert L._format_data_for_prompt(col) == bak._format_data_for_prompt(col)
    finally:
        if old is not None:
            C.DA_EARNINGS_TRISTATE = old


# ── 8. 真实 checkpoint ON 态抽查 ─────────────────────────────────────

def test_on_real_checkpoints_state_matches_markers():
    files = [p for d in _CKPT_DIRS for p in sorted(d.glob("*.raw.json"))]
    if not files:
        pytest.skip("冻结 checkpoint 不在本机")
    checked = 0
    for p in files:
        col = json.load(open(p))["collected"]
        e = col.get("earnings_calendar")
        if not (isinstance(e, dict) and "_error" not in e and not e.get("next_earnings")):
            continue
        sec = _earn_section(_render(col, True))
        assert sec, str(p)
        if e.get("_quality") == "ok":
            assert "查询窗口内未发现财报事件" in sec, str(p)
            ld = e.get("lookahead_days")
            if isinstance(ld, int) and not isinstance(ld, bool) and 0 < ld <= 366:
                assert f"未来{ld}天" in sec, str(p)
        elif e.get("_quality") == "degraded":
            assert "财报日历降级" in sec, str(p)
        else:
            assert "财报日期未确认" in sec, str(p)
        ts = col.get("_timestamp")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and math.isfinite(ts) and ts > 0:
            assert f"as-of: {_expected_asof(ts)}" in sec, str(p)
        checked += 1
    assert checked >= 60, f"两日归档健康空/降级/未确认应≥60, 实际 {checked}"
