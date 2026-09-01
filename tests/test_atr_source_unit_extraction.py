"""[2026-09-02 用户裁"同意执行"·C1 冻结显式豁免·DEB-20260831 §8.8] ATR 提取单位契约.

缘起: Codex §8.7 复核牵出单位混装 bug — instruction_generator 9/1 起新格式
"VOL_*: atr_pct=3.13% ..." 是**百分比**, 旧格式 "technical.atr_14=8.35" 是**绝对价**;
`_extract_atr_from_instruction` 解析器不分单位 → 百分比直落 metadata.atr 绝对值槽
→ pipeline 规则0.5 读回 LLY(entry≈1178) 被算成 atr%=0.27% → tight 落 1% 地板,
V 型日误杀行为复活 (9/1 4 笔实流: LLY/META/BABA/BWXT, 均在规则部署前平仓 = 零已实现影响).

锁住的语义:
  U1  新格式 (atr_pct=) → 百分比乘入场价转**绝对价**入库 (str|dict 双入参同值)
  U2  旧格式 (atr_14= / price×2%=) 绝对价原样返回, 不动
  U3  vix_widen= 后缀拼接的新格式仍正确识别为百分比
  U4  代理路径 (无 atr_source, SL 距离反算) 零回归
  U5  百分比格式但 entry_price 缺失 → fail-safe 落代理/0 (= 规则0.5 回退固定 1%)
  U6  端到端: 提取值喂规则0.5 — LLY 单位修复后 -1.3% 下探存活, 修复前 (百分比
      直落绝对槽) 同一笔回退 1% 地板被杀
"""
import json
from pathlib import Path

import app.playbook.config as pb_config_mod
from app.playbook.playbook_service import _extract_atr_from_instruction
from app.playbook.pipeline import _check_playbook_exit_rules

REPO = Path(__file__).resolve().parents[1]

_LLY_RAW = json.dumps({
    "symbol": "US.LLY", "entry_price": 1179.99,
    "atr_source": "VOL_medium: atr_pct=3.13% sl=4.50% rr=1.80",
})
_LLY_INST = {"entry_price": 1179.99, "stop_loss": 1126.89, "raw_instruction_json": _LLY_RAW}


# ── U1: 新格式百分比 → 绝对价 ────────────────────────────────────────────────

def test_new_percent_format_converts_to_absolute():
    """LLY 实况复刻: 3.13% × 1179.99 = 36.93 绝对价 (修复前错误返回 3.13)."""
    assert _extract_atr_from_instruction(dict(_LLY_INST)) == 36.93


def test_new_percent_format_dict_input_same_value():
    """db._row_to_dict 会把 raw_instruction_json 解析成 dict 进 pipeline — 双入参同值."""
    inst = dict(_LLY_INST)
    inst["raw_instruction_json"] = json.loads(_LLY_RAW)
    assert _extract_atr_from_instruction(inst) == 36.93


def test_vix_widen_suffix_still_percent():
    """vix_monitor 拼接 " vix_widen=1.5%" 后缀后仍是百分比格式 (首个 "=" 拆出 atr_pct 值)."""
    raw = json.dumps({
        "entry_price": 113.28,
        "atr_source": "VOL_high: atr_pct=3.88% sl=5.00% rr=1.80 vix_widen=1.5%",
    })
    assert _extract_atr_from_instruction(
        {"entry_price": 113.28, "raw_instruction_json": raw}) == 4.4


# ── U2: 旧格式绝对价不动 ────────────────────────────────────────────────────

def test_old_technical_format_unchanged():
    raw = json.dumps({"atr_source": "technical.atr_14=8.35 (quality=good)"})
    assert _extract_atr_from_instruction({"raw_instruction_json": raw}) == 8.35


def test_old_daily_kline_format_unchanged():
    raw = json.dumps({"atr_source": "daily_kline.atr_14=8.35 (quality=good)"})
    assert _extract_atr_from_instruction({"raw_instruction_json": raw}) == 8.35


def test_old_fallback_format_unchanged():
    raw = json.dumps({"atr_source": "FALLBACK: price×2%=8.35 (ATR missing)"})
    assert _extract_atr_from_instruction({"raw_instruction_json": raw}) == 8.35


# ── U4: 代理路径零回归 ──────────────────────────────────────────────────────

def test_proxy_path_unchanged():
    """无 atr_source → SL 距离反算 (2026-07-16 行为): 100×5% = 5.0 绝对价."""
    assert _extract_atr_from_instruction(
        {"entry_price": 100.0, "stop_loss": 95.0}) == 5.0


# ── U5: fail-safe ───────────────────────────────────────────────────────────

def test_percent_format_zero_entry_falls_safe():
    """百分比格式但 entry_price 缺失 → 0.0 (百分比转换与代理路径都需要入场锚),
    规则0.5 回退固定 1% — fail-safe, 绝不产生量纲错误的值."""
    raw = json.dumps({"atr_source": "VOL_medium: atr_pct=3.13% sl=4.50% rr=1.80"})
    assert _extract_atr_from_instruction(
        {"entry_price": 0, "stop_loss": 95.0, "raw_instruction_json": raw}) == 0.0
    assert _extract_atr_from_instruction(
        {"entry_price": 0, "raw_instruction_json": raw}) == 0.0


# ── U6: 端到端 — 提取值喂规则0.5 ────────────────────────────────────────────

class _CfgOverride:
    """委托真实配置, 仅覆写 early_tight_* 键 (与 test_early_tight_sl_atr.py 同款隔离)."""

    _OVER = {
        "early_tight_sl_enabled": True,
        "early_tight_sl_minutes": 30,
        "early_tight_sl_pct": 1.0,
        "early_tight_sl_mfe_gate": 0.3,
        "early_tight_sl_atr_enabled": True,
        "early_tight_sl_atr_mult": 0.5,
        "early_tight_sl_pct_max": 2.5,
    }

    def __init__(self, real):
        self._real = real

    def get(self, key, default=None):
        if key in self._OVER:
            return self._OVER[key]
        return self._real.get(key, default)

    def get_horizon_top(self, *a, **kw):
        return self._real.get_horizon_top(*a, **kw)

    def get_horizon_params(self, *a, **kw):
        return self._real.get_horizon_params(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _run_rule(meta, current_price, entry=1178.0):
    """LLY 9/1 场景: VOL_medium (sl 4.5%), MFE=0, 开盘后 100min (过冷却窗)."""
    _orig = pb_config_mod.get_playbook_config
    pb_config_mod.get_playbook_config = lambda: _CfgOverride(_orig())
    try:
        return _check_playbook_exit_rules(
            direction="LONG", entry_price=entry, current_price=current_price,
            initial_sl=entry * 0.955, peak_price=entry, meta=dict(meta),
            held_minutes=10, minutes_to_close=300, minutes_from_open=100,
            entry_time_str="2026-09-01T10:00:00",
        )
    finally:
        pb_config_mod.get_playbook_config = _orig


def test_end_to_end_fixed_atr_survives_dip():
    """单位修复后: LLY meta.atr=36.93 → atr%=3.14 → tight=1.57% → -1.3% 下探存活."""
    reason, *_ = _run_rule({"atr": 36.93, "_symbol": "US.LLY"}, current_price=1178.0 * 0.987)
    assert reason == "", f"修复后 -1.3% 下探应存活 (tight≈1.57%), 实际退出: {reason}"


def test_end_to_end_buggy_percent_in_absolute_slot_killed():
    """回归钉子: 修复前形态 (百分比 3.13 直落绝对槽) → atr%=0.27 → 1% 地板 → 同一笔被杀.

    本例锁住 bug 的危害方向: 若解析器回退, 该笔高波动单会在 V 型日被 1% 地板扫掉.
    """
    reason, *_ = _run_rule({"atr": 3.13, "_symbol": "US.LLY"}, current_price=1178.0 * 0.987)
    assert reason == "SL_INITIAL", f"bug 形态下 -1.3% 下探应触发 1% 地板 SL_INITIAL, 实际: {reason}"
