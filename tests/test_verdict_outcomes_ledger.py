#!/usr/bin/env python3
"""verdict_outcomes 判别力台账 契约测试 (DEB-20261114-selection-discrimination §六 P0).

这个台账存在的唯一理由是补一个**结构性盲区**: `ic_tracker.backfill_forward_returns`
对 `direction='SKIP'` 直接 continue ⇒ 从未成交/被拦的判定没有前向收益,
"模型挑得准不准""门拦得对不对"都不可测。所以本文件钉三件事:

  1. 纯函数口径: 锚点夹取、方向回填、同窗口超额 —— 错一个, 读数全成噪声;
  2. **SKIP 行必须被算出来** (盲区回归): 没有这条, 台账随时可退回"只测成交";
  3. 只读边界: 脚本源码不得写交易表 —— 台账越界改一个字段就能污染账本。

变异自证: 把 `recover_direction` 的跳过分支改回 `return "SKIP","none"`,
`test_skip_rows_are_measured` 必红; 把 `anchor_index` 的 `<1` 改成 `<0`,
`test_premarket_anchor_is_clamped_to_open` 必红。
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load():
    p = ROOT / "scripts" / "verdict_outcomes_ledger.py"
    spec = importlib.util.spec_from_file_location("verdict_outcomes_ledger_test", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


M = _load()


# ─────────────── 1. 纯函数口径 ───────────────

def test_minute_index_maps_open_and_close():
    assert M.minute_index(9, 31) == 1
    assert M.minute_index(10, 0) == 30
    assert M.minute_index(16, 0) == 390


def test_premarket_anchor_is_clamped_to_open():
    from datetime import datetime
    et = M._et()
    # DA 盘前 08:25 ET → 夹到 09:31 (下标 1), 且标记 clamped
    idx, clamped = M.anchor_index(datetime(2026, 11, 13, 8, 25, tzinfo=et))
    assert (idx, clamped) == (1, True)
    # 盘中 10:30 → 不夹
    idx, clamped = M.anchor_index(datetime(2026, 11, 13, 10, 30, tzinfo=et))
    assert (idx, clamped) == (60, False)


def test_recover_direction_from_skip_reason():
    # 组合层门把多数方改写成 SKIP: 受害行恒为多数方, 从文案恢复
    d, src = M.recover_direction("SKIP", "方向偏斜超限 guardrail 降级 (多数方 BUY, 原size=5900)")
    assert (d, src) == ("BUY", "skip_reason")
    d, src = M.recover_direction("SKIP", "方向偏斜 guardrail 降级 (多数方 SELL_SHORT, 原size=100)")
    assert (d, src) == ("SELL_SHORT", "skip_reason")
    # 纯弃权: 无方向 ⇒ 如实标 none, 不得拿多头冒充方向
    assert M.recover_direction("SKIP", "投票中性, 无方向信号") == ("SKIP", "none")
    # 正常方向行
    assert M.recover_direction("BUY", None) == ("BUY", "row")


def test_excess_never_fills_missing_with_zero():
    assert M.excess(0.01, 0.004) == 0.006
    assert M.excess(None, 0.004) is None
    assert M.excess(0.01, float("nan")) is None


def test_dir_sign():
    assert M.dir_sign("BUY") == 1.0
    assert M.dir_sign("SELL_SHORT") == -1.0
    assert M.dir_sign("SKIP") is None


# ─────────────── 2. 盲区回归: SKIP 行必须被算出来 ───────────────

def _fake_inputs():
    """两天宇宙 + 一条 BUY / 一条门降级 SKIP / 一条纯弃权 SKIP。"""
    bars1m = {}
    daily = {}
    for sym in [f"US_S{i}" for i in range(25)]:
        daily[sym] = {
            "2026-11-12": {"o": 100.0, "c": 101.0},
            "2026-11-13": {"o": 100.0, "c": 102.0},
            "2026-11-16": {"o": 102.0, "c": 103.0},
            "2026-11-18": {"o": 103.0, "c": 104.0},
        }
        bars1m[sym] = {"2026-11-13": {1: 100.0, 31: 100.5, 61: 101.0, 121: 101.5, 390: 102.0}}
    verdicts = [
        {"source": "instruction", "src_key": "1", "verdict_date": "2026-11-13",
         "symbol": "US.S0", "direction": "BUY", "skip_reason": None,
         "confidence": 80, "created_at": "2026-11-13T13:00:00+00:00"},          # 08:00 ET 盘前
        {"source": "instruction", "src_key": "2", "verdict_date": "2026-11-13",
         "symbol": "US.S1", "direction": "SKIP",
         "skip_reason": "方向偏斜超限 guardrail 降级 (多数方 BUY, 原size=5900)",
         "confidence": 70, "created_at": "2026-11-13T13:00:00+00:00"},
        {"source": "instruction", "src_key": "3", "verdict_date": "2026-11-13",
         "symbol": "US.S2", "direction": "SKIP", "skip_reason": "投票中性, 无方向信号",
         "confidence": 50, "created_at": "2026-11-13T13:00:00+00:00"},
    ]
    return verdicts, bars1m, daily


def test_skip_rows_are_measured():
    verdicts, bars1m, daily = _fake_inputs()
    uni = M.Universe(bars1m, daily)
    rows = M.build_rows(verdicts, bars1m, daily, uni)
    keys = {(r["src_key"], r["basis"], r["horizon"]) for r in rows}

    # 三条 verdict 都必须有 daily 观测 (含两条 SKIP) —— 这就是盲区回归
    for k in ("1", "2", "3"):
        assert ("%s" % k, "daily", "eod") in keys, f"verdict {k} 缺 daily/eod 观察行"
        assert ("%s" % k, "daily", "t1") in keys, f"verdict {k} 缺 daily/t1 观察行"

    by_key = {r["src_key"]: r for r in rows if r["basis"] == "daily" and r["horizon"] == "eod"}
    # 门降级的 SKIP 恢复成 BUY ⇒ 有方向调整超额
    assert by_key["2"]["direction"] == "BUY" and by_key["2"]["dir_source"] == "skip_reason"
    assert by_key["2"]["excess_dir"] is not None
    # 纯弃权: 保留 SKIP + excess_long, 但不用方向调整值冒充
    assert by_key["3"]["direction"] == "SKIP" and by_key["3"]["dir_source"] == "none"
    assert by_key["3"]["excess_dir"] is None
    assert by_key["3"]["excess_long"] is not None


def test_direction_sign_applied_to_excess():
    verdicts, bars1m, daily = _fake_inputs()
    verdicts.append({"source": "instruction", "src_key": "4", "verdict_date": "2026-11-13",
                     "symbol": "US.S3", "direction": "SELL_SHORT", "skip_reason": None,
                     "confidence": 60, "created_at": "2026-11-13T13:00:00+00:00"})
    rows = M.build_rows(verdicts, bars1m, daily, M.Universe(bars1m, daily))
    r = [x for x in rows if x["src_key"] == "4" and x["basis"] == "daily" and x["horizon"] == "eod"][0]
    assert r["excess_dir"] is not None and r["excess_long"] is not None
    # 空头 = 多头超额的相反数 (符号必须真反)
    assert abs(r["excess_dir"] + r["excess_long"]) < 1e-9


def test_universe_excludes_thin_days():
    """宇宙样本 <20 时返回 None (不许拿 3 只票当"全宇宙")。"""
    daily = {f"US_X{i}": {"2026-11-13": {"o": 100.0, "c": 101.0}} for i in range(5)}
    uni = M.Universe({}, daily)
    assert uni.daily_ret("2026-11-13", "eod") is None


# ─────────────── 3. 只读边界 (源码契约) ───────────────

def test_ledger_is_read_only_on_trading_tables():
    src = (ROOT / "scripts" / "verdict_outcomes_ledger.py").read_text(encoding="utf-8")
    # 剔整行注释再断言, 否则自己写的 tombstone/说明会把契约测咬红
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    for forbidden in ("INSERT INTO trade_records", "UPDATE trade_records",
                      "INSERT INTO open_positions", "UPDATE open_positions",
                      "UPDATE playbook_instructions", "INSERT INTO playbook_instructions"):
        assert forbidden not in body, f"台账越界写交易表: {forbidden}"
    # 台账唯一的写目标是自建表
    assert "CREATE TABLE IF NOT EXISTS verdict_outcomes" in body


def test_table_is_keyed_idempotently():
    src = (ROOT / "scripts" / "verdict_outcomes_ledger.py").read_text(encoding="utf-8")
    assert "PRIMARY KEY (source, src_key, basis, horizon)" in src
    assert "INSERT OR REPLACE INTO verdict_outcomes" in src