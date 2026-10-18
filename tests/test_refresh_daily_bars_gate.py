"""refresh_daily_bars 执行级日门契约 (2026-09-10, DEB-20260910 §1 议题一 A 案).

病灶与立法理由 (全部日志实测, 非推断):
  原"幂等"只是**数据级**(不重复写行), 无执行级门 ⇒ AFTER_HOURS 每轮重跑, 一晚 32 轮
  × 281 票 = ~8992 次 Futu 历史K线请求, 有效仅首轮 ~281 次 (32× 配额浪费), 单轮 7m45s
  ≈ 占满整个盘后窗 (ET 16:00-20:00)。

本文件钉住三条不可退让的语义 —— 任何一条被"优化"掉都必须红:
  ① 干净夜 (fail==0) 只跑 1 轮, 后续轮**零** do_refresh 调用 (省配额的全部意义所在);
  ② 失败夜必须**有限重试**且只重试失败票 —— 不是 32 轮无脑全量重试(原病), 也不是
     "一日一次"零重试(会让盘后首轮遇 OpenD 抖动当天彻底不刷新, 反而加速复发 7/24 停更三周);
  ③ 跨日必须重新全量; 状态文件损坏必须 fail-open 向"**做刷新**"方向(不是向跳过方向)。
"""
import json
import sys
from datetime import datetime

import pytest

import scripts.refresh_daily_bars as rdb


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """把状态文件指到 tmp, 并接管 do_refresh / 交易日, 返回可控探针。"""
    monkeypatch.setattr(rdb, "STATE", tmp_path / "refresh_daily_bars_state.json")
    calls = []
    box = {"date": "2026-09-10", "result": {"ok": 0, "skip": 281, "fail": 0,
                                             "failed_syms": []}}
    monkeypatch.setattr(rdb, "_today_et", lambda: box["date"])

    def fake_do_refresh(limit=0, dry_run=False, only_syms=None):
        calls.append({"limit": limit, "dry_run": dry_run,
                      "only_syms": set(only_syms) if only_syms else None})
        return dict(box["result"])

    monkeypatch.setattr(rdb, "do_refresh", fake_do_refresh)
    return {"calls": calls, "box": box, "state_path": rdb.STATE}


def test_clean_night_runs_once_then_skips(gate):
    """①干净夜: 模拟盘后窗 31 轮 ⇒ do_refresh 只被调 1 次, 且首轮是全量(only_syms=None)。"""
    for _ in range(31):
        rdb.run_check(None)
    assert len(gate["calls"]) == 1, f"执行门失效: 31 轮跑了 {len(gate['calls'])} 次 (原病灶=32次)"
    assert gate["calls"][0]["only_syms"] is None, "首轮必须全量, 不得只刷子集"
    st = json.loads(gate["state_path"].read_text(encoding="utf-8"))
    assert st["done"] is True and st["attempts"] == 1 and st["fail"] == 0


def test_fail_night_retries_only_failed_symbols(gate):
    """②失败夜: 第 2 轮只重试失败票 (成本 ~1.65s/票, 不是 281 票全量重扫)。"""
    gate["box"]["result"] = {"ok": 280, "skip": 0, "fail": 1, "failed_syms": ["US.SKYT"]}
    rdb.run_check(None)
    gate["box"]["result"] = {"ok": 1, "skip": 0, "fail": 0, "failed_syms": []}
    rdb.run_check(None)

    assert len(gate["calls"]) == 2, "失败后必须重试 (不得一轮定生死)"
    assert gate["calls"][0]["only_syms"] is None, "首轮全量"
    assert gate["calls"][1]["only_syms"] == {"US.SKYT"}, \
        f"第2轮必须只重试失败票, 实际={gate['calls'][1]['only_syms']} (全量重试=原病灶)"
    st = json.loads(gate["state_path"].read_text(encoding="utf-8"))
    assert st["done"] is True and st["attempts"] == 2 and st["failed_syms"] == []


def test_persistent_failure_stops_after_max_attempts(gate):
    """②永久失败票: 至多 MAX_ATTEMPTS_ON_FAIL 轮后停 (不是 32 轮无脑重试)。"""
    gate["box"]["result"] = {"ok": 280, "skip": 0, "fail": 1, "failed_syms": ["US.SKYT"]}
    for _ in range(10):
        rdb.run_check(None)
    assert len(gate["calls"]) == rdb.MAX_ATTEMPTS_ON_FAIL, \
        f"重试未封顶: {len(gate['calls'])} 轮 (上限 {rdb.MAX_ATTEMPTS_ON_FAIL})"
    st = json.loads(gate["state_path"].read_text(encoding="utf-8"))
    assert st["done"] is True, "封顶后必须写 done, 否则后续轮继续白跑"
    assert st["failed_syms"] == ["US.SKYT"], "停止时必须留失败票名单供人工根因"


def test_new_trading_day_resets_to_full_scan(gate):
    """③跨日: 必须重新全量, 不得沿用昨日 done 标记 (否则日K永久停更 = 7/24 病灶复发)。"""
    rdb.run_check(None)
    assert len(gate["calls"]) == 1
    gate["box"]["date"] = "2026-09-11"
    rdb.run_check(None)
    assert len(gate["calls"]) == 2, "跨日未重置 ⇒ 缓存永久停更"
    assert gate["calls"][1]["only_syms"] is None, "跨日首轮必须全量"
    assert json.loads(gate["state_path"].read_text(encoding="utf-8"))["date"] == "2026-09-11"


def test_corrupt_state_fails_open_toward_refresh(gate, caplog):
    """③状态文件损坏: fail-open 方向必须是"**做刷新**", 不是"跳过" (守卫不得只能报健康)。"""
    gate["state_path"].write_text("{ 不是 JSON", encoding="utf-8")
    rdb.run_check(None)
    assert len(gate["calls"]) == 1, "状态损坏却跳过刷新 = 静默停更"
    assert any("状态文件不可读" in r.message for r in caplog.records), \
        "损坏必须吼一声 (WARNING), 不得静默吞掉"


def test_state_write_failure_does_not_block(gate, monkeypatch, tmp_path, caplog):
    """状态写失败不得冒泡: 最坏后果是下轮重刷, 不能让盘后钩子整段炸掉。

    必须**真实**触发写失败 (父目录不存在), 不能整体替换 `_save_state` —— 保护就在该函数
    内部的 try 里, 替换掉它等于把被测保护一起拆了 (首版此用例即犯此错, 假绿风险反向)。
    """
    monkeypatch.setattr(rdb, "STATE", tmp_path / "no_such_dir" / "state.json")
    rdb.run_check(None)          # 不得抛
    assert len(gate["calls"]) == 1
    assert any("状态文件写失败" in r.message for r in caplog.records), \
        "写失败必须吼一声 (WARNING), 不得静默吞掉"


def test_cli_path_bypasses_gate(gate, monkeypatch):
    """CLI 手工运行走 do_refresh, 不经执行门 (显式意图必须能强制刷)。"""
    seen = {}

    def fake(limit=0, dry_run=False, only_syms=None):
        seen.update({"limit": limit, "dry_run": dry_run})
        return {"ok": 0, "skip": 0, "fail": 0, "failed_syms": []}

    monkeypatch.setattr(rdb, "do_refresh", fake)
    monkeypatch.setattr("sys.argv", ["refresh_daily_bars.py", "--limit", "5", "--dry-run"])
    assert rdb.main() == 0
    assert seen == {"limit": 5, "dry_run": True}, "CLI 参数必须原样透传"
    assert not gate["state_path"].exists(), "CLI 手工跑不得写执行门状态 (会误挡当晚调度)"


def test_cron_window_skips_before_1600_et(gate, monkeypatch):
    """双季 cron 的另一季会落在 ET 15:05 — 必须跳过, 不能把未收盘日K当完整bar。"""
    monkeypatch.setattr(rdb, "_now_et",
                        lambda: datetime(2026, 9, 10, 15, 5, tzinfo=rdb.ET))
    rdb.run_check(None, enforce_window=True)
    assert gate["calls"] == []


def test_cron_window_runs_after_1600_et(gate, monkeypatch):
    monkeypatch.setattr(rdb, "_now_et",
                        lambda: datetime(2026, 9, 10, 16, 5, tzinfo=rdb.ET))
    rdb.run_check(None, enforce_window=True)
    assert len(gate["calls"]) == 1


def test_cli_cron_uses_execution_gate(gate, monkeypatch):
    """--cron 必须走 run_check(enforce_window=True), 不能走裸 do_refresh。"""
    called = {}
    monkeypatch.setattr(rdb, "run_check",
                        lambda scheduler=None, **kw: called.update(kw))
    monkeypatch.setattr(sys, "argv", ["refresh_daily_bars.py", "--cron"])
    assert rdb.main() == 0
    assert called.get("enforce_window") is True
