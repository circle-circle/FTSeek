"""SSOT 守卫: 持仓极值口径只有一份实现 (2026-09-18, 复核 Codex 批时发现的双实现)。

背景: `app/playbook/pipeline.py` 与 `scripts/repair_trade_exit_invariants.py` 曾各写一份
"把观测入场/出场价并入极值"的实现, 且对**同一类输入**动作相反 ——
极值字段存在但不可解析时: 管线版按缺失处理并覆盖, 修补版 `continue` 跳过保留原值。
两处共用同一份是本测试存在的理由; 本文件钉三件事:

  ① 唯一实现 (app/core/trade_extremes) 的口径: LONG/SHORT 方向语义、0/负=缺失、只在变了才写。
  ② 管线薄壳与唯一实现**逐例同值** —— 行为等价, 不是"看着像"。
  ③ 修补脚本里**不再有本地副本**, 且调用点解析到同一函数对象 (防双实现复活)。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from app.core.trade_extremes import include_observed_prices
from app.playbook.pipeline import _include_observed_prices_in_extremes

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


REPAIR = _load("repair_exit_invariants_ssot_test", "scripts/repair_trade_exit_invariants.py")

# 覆盖"缺失 / 0 / 正常 / 不可解析 / 只有 bar 键 / 已是最紧"各形态的用例语料
CORPUS = [
    ("LONG", 100.0, 110.0, {}),
    ("LONG", 100.0, 90.0, {"peak_price": 0, "trough_price": 0.0}),
    ("LONG", 10.0, 12.0, {"peak_price": 11.5, "trough_price": 9.5}),
    ("LONG", 10.0, 12.0, {"peak_price": "oops", "trough_price": None}),
    ("LONG", 10.0, 12.0, {"peak_price_bar": 10.5, "trough_price_bar": 10.5}),
    ("LONG", 5.0, 5.0, {"peak_price": 5.0, "trough_price": 5.0}),
    ("SHORT", 39.835, 39.49, {}),
    ("SHORT", 39.835, 39.49, {"peak_price": 39.835, "trough_price": 39.835}),
    ("SHORT", 50.0, 48.0, {"peak_price": "x", "trough_price": 51.0}),
    ("SELL_SHORT", 20.0, 21.0, {"peak_price": 19.0, "trough_price": 19.0}),
]


def test_long_fills_missing_with_observed_extremes():
    md = {}
    changed = include_observed_prices(md, "LONG", 100.0, 110.0)
    assert changed is True
    assert md["peak_price"] == 110.0 and md["trough_price"] == 100.0
    assert md["peak_price_bar"] == 110.0 and md["trough_price_bar"] == 100.0


def test_long_tightens_existing_extremes_only_outward():
    md = {"peak_price": 11.5, "trough_price": 9.5}
    include_observed_prices(md, "LONG", 10.0, 12.0)
    assert md["peak_price"] == 12.0  # 只抬高
    assert md["trough_price"] == 9.5  # 观测低 10.0 不低于已有谷 9.5 ⇒ 不动


def test_short_semantics_peak_is_lowest_price():
    md = {}
    include_observed_prices(md, "SHORT", 39.835, 39.49)
    assert md["peak_price"] == 39.49  # 空头最有利=最低价 (与 atr_stop_engine 注释同口径)
    assert md["trough_price"] == 39.835
    assert include_observed_prices(md, "SHORT", 39.835, 39.49) is False  # 已是最紧 ⇒ 不再改写


def test_corrupt_extreme_treated_as_missing_not_skipped():
    """合并时的**口径裁决**: 不可解析的极值按缺失处理并被观测值覆盖 (取管线原语义)。

    旧修补副本的行为是 `continue` 跳过保留原值 —— 两版在此类输入上相反。实盘窗口
    (2026-09-11 起 47 行) 非数值极值 0 处 ⇒ 今天合并对现网数据零行为差异; 但口径必须选一个,
    选"覆盖"是因为静默保留脏值会让极值永远卡在垃圾值上。
    """
    md = {"peak_price": "oops", "trough_price": None}
    changed = include_observed_prices(md, "LONG", 10.0, 12.0)
    assert changed is True
    assert md["peak_price"] == 12.0 and md["trough_price"] == 10.0


def test_noop_inputs_are_rejected_without_touching_dict():
    assert include_observed_prices(None, "LONG", 1.0, 1.0) is False
    assert include_observed_prices([], "LONG", 1.0, 1.0) is False
    md = {}
    assert include_observed_prices(md, "LONG", 0, 12.0) is False  # 入价 <=0
    assert include_observed_prices(md, "LONG", 10.0, None) is False  # 出价不可解析
    assert md == {}


def test_already_tightest_returns_false():
    md = {"peak_price": 12.0, "trough_price": 3.0, "peak_price_bar": 12.0, "trough_price_bar": 3.0}
    assert include_observed_prices(md, "LONG", 10.0, 11.0) is False
    assert md["peak_price"] == 12.0 and md["trough_price"] == 3.0


def test_pipeline_delegate_is_behaviorally_identical():
    """管线薄壳必须与唯一实现逐例同值 (返回 + 结果 dict)。"""
    for direction, entry, exit_, seed in CORPUS:
        a = dict(seed)
        b = dict(seed)
        ra = include_observed_prices(a, direction, entry, exit_)
        rb = _include_observed_prices_in_extremes(b, direction, entry, exit_)
        assert ra == rb, (direction, entry, exit_, seed)
        assert a == b, (direction, entry, exit_, seed, a, b)


def test_repair_script_uses_shared_implementation_not_a_copy():
    """防双实现复活: 修补脚本不得再定义本地极值函数, 且引用的是同一函数对象。"""
    assert not hasattr(REPAIR, "_include_observed_prices"), "本地副本已复活 — 口径会静默分叉"
    assert REPAIR.include_observed_prices is include_observed_prices