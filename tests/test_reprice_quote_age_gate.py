"""[RFC-185 2026-11-06] 执行前报价陈旧度门 + re-price 审计首跳。

三块被测:
  1. `data_health_guard.check_reprice_quote_age` — 纯函数 (行为用例, 修前不存在 ⇒ 红)。
  2. auto_execute 里的接线位置 — 门必须排在"算 entry/SL/TP"**之前** (否则拦不住已起价的单)。
  3. re-price 审计的 first/rounds — 让累计漂移可从库重建 (原实现每轮整块覆盖只剩末跳)。

口径提醒: 这里量的是**交易所更新龄** (`lv3_quote_age_ms`), 不是 `check_quote_freshness`
量的"我方取价时刻"(那个恒 ~0)。
"""
from pathlib import Path

import pytest

from app.risk.data_health_guard import (
    check_reprice_quote_age,
    reprice_quote_age_ms,
)

_FLOW = (Path(__file__).resolve().parents[1]
         / "app" / "playbook" / "pipeline_parts" / "auto_execute_flow.py")
_CFG = Path(__file__).resolve().parents[1] / "config" / "unified_config.yaml"


# ══════════════════════════════════════════════════════════════════
# 1. 判定本体
# ══════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("quote,expect_ok,expect_age", [
    ({"lv3_quote_age_ms": 45000}, False, 45000),     # 过龄 ⇒ 拦
    ({"lv3_quote_age_ms": 30000}, True, 30000),      # 恰等于阈值 ⇒ 放 (严格 >)
    ({"lv3_quote_age_ms": 1200}, True, 1200),
    ({"quote_age_ms": 31000}, False, 31000),        # 兼容裸键名
    ({"lv3_quote_age_ms": "8000"}, True, 8000),      # 字符串数字
])
def test_check_reprice_quote_age_verdicts(quote, expect_ok, expect_age):
    ok, age, _why = check_reprice_quote_age(quote, 30000)
    assert ok is expect_ok
    assert age == expect_age


@pytest.mark.parametrize("quote", [
    {},                                   # 字段缺失
    {"lv3_quote_age_ms": None},
    {"lv3_quote_age_ms": "not-a-number"},  # 非法值
    None,                                  # 无 ctx
])
def test_age_unknown_fails_open(quote):
    """取不到 age 一律放行 — 取不到数据不阻断交易, 与全文件口径一致。"""
    ok, age, why = check_reprice_quote_age(quote, 30000)
    assert ok is True and age is None and why == "age_unknown"


def test_zero_max_disables_gate():
    """cap<=0 = 关闸 (不等价于"全拦"), 防 `or default` 类误配把门焊死。"""
    ok, age, _ = check_reprice_quote_age({"lv3_quote_age_ms": 999999}, 0)
    assert ok is True and age == 999999


def test_age_prefers_lv3_field():
    """两个字段都在时以 lv3_quote_age_ms 为准 (那是交易所 update_time 口径)。"""
    assert reprice_quote_age_ms({"lv3_quote_age_ms": 500, "quote_age_ms": 90000}) == 500


# ══════════════════════════════════════════════════════════════════
# 2. 接线契约 (源码级 — 位置比"存在"更重要)
# ══════════════════════════════════════════════════════════════════
def test_gate_is_wired_before_repricing():
    src = _FLOW.read_text(encoding="utf-8")
    gate = src.index("check_reprice_quote_age")
    pricing = src.index("_new_entry = _ask")
    assert gate < pricing, "陈旧度门必须在起价之前 — 否则拦下的是已算完 SL/TP 的单"
    assert "skipped_stale_quote" in src
    assert "stale_quote_gate(" in src


def test_gate_reads_its_own_config_keys():
    flow = _FLOW.read_text(encoding="utf-8")
    cfg = _CFG.read_text(encoding="utf-8")
    for key in ("reprice_quote_age_gate_enabled", "reprice_quote_age_max_ms"):
        assert key in flow, f"{key} 未被消费 = 装饰性配置"
        assert key + ":" in cfg, f"{key} 不在 unified_config.yaml"


def test_reprice_audit_keeps_first_hop_and_rounds():
    src = _FLOW.read_text(encoding="utf-8")
    assert '"first": _rp_first' in src, "首跳未随审计落库 ⇒ 累计漂移不可重建"
    assert '"rounds": _rp_rounds' in src
    assert '_prev_rp.get("first")' in src, "首跳必须从既有 metadata 继承, 否则每轮被重置"