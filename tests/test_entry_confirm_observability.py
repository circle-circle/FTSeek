"""entry_confirm 可观测性回归 (2026-11-05)。

三件事, 都是**记录面**问题, 不改任何入场判定:

1. **EXPIRED 不留行**: TTL 超期的 arm 过去只在日志出现, 结算器按 status=="ok" 过滤 ⇒
   这些笔在 assigned/resolved/unresolved 三栏里消失, 覆盖率虚高。现在必须落一行
   status="expired"。变异: 去掉该写入 ⇒ 本文件转红。

2. **前瞻 jsonl 并发写**: arm(auto_execute 线程) 与 sweep(10s 快循环线程) 同时追加同一
   文件; 无锁时可能交错出半行 ⇒ 整份文件后续解析失败 (一次性毁掉整个 cohort 的读数)。
   本文件用 8 线程 × N 行验证每行都是合法 JSON。

3. **两臂都必须在 jsonl 里可区分**: holdout 行必须带 arm="holdout", treatment 带
   arm="treatment" —— 否则结算器无法配对, 归因直接失效。
"""
import json
import threading
from datetime import datetime, timedelta

from app.playbook import entry_confirm_gate as G


class _FakeDB:
    def __init__(self):
        self.writes = []

    def merge_instruction_metadata(self, inst_id, patch):
        self.writes.append((inst_id, patch))
        return True


def _arm(expire_offset_sec=-1):
    now = datetime(2026, 11, 5, 10, 0, 0)
    return {
        "inst_id": 42, "symbol": "US.TEST", "decision_id": "DA-20261105-US.TEST-1",
        "direction": "LONG", "anchor": 100.0,
        "armed_at": now - timedelta(seconds=700),
        "expire_at": now + timedelta(seconds=expire_offset_sec),
        "notional": 6000.0, "session_min": 30, "delay_sec": 120,
        "ruler": "mid", "grace_sweeps": 3, "grace_votes": 2,
        "entry_archetype": "TREND_PULLBACK", "planned_entry": 99.5,
        "plan_gap_pct": 0.5,
    }


def test_expired_writes_forward_row(monkeypatch):
    rows = []
    monkeypatch.setattr(G, "_write_forward_row", lambda r: rows.append(r))
    out = {"confirmed": 0, "abandoned": 0, "expired": 0, "pending": 0}
    G._judge_one(None, _FakeDB(), 42, _arm(), datetime(2026, 11, 5, 10, 0, 0), out)

    assert out["expired"] == 1
    assert len(rows) == 1, "超期必须留一行, 否则覆盖率虚高"
    r = rows[0]
    assert r["status"] == "expired" and r["outcome"] == G.ST_EXPIRED
    assert r["arm"] == "treatment"
    assert r["expire_reason"] == "ttl"
    assert r["still_triggers"] is None and r["opp_cost_pct"] is None
    assert r["delay_actual_sec"] >= 700


def test_forward_write_is_thread_safe(tmp_path, monkeypatch):
    p = tmp_path / "forward.jsonl"
    monkeypatch.setattr(G, "_FORWARD_PATH", p)
    monkeypatch.setattr(G, "_FORWARD_LOCK", threading.Lock())

    def worker(k):
        for i in range(25):
            G._write_forward_row({"inst_id": k * 100 + i, "arm": "treatment",
                                  "pad": "x" * 900})   # 行长 ≫ 单次写原子性保证

    ts = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 200, f"丢行: {len(lines)}"
    ids = set()
    for line in lines:                      # 任一半行都会在这里抛
        ids.add(json.loads(line)["inst_id"])
    assert len(ids) == 200, "出现重复/损坏行"


def test_holdout_and_treatment_arms_are_distinguishable():
    """协议级不变量: 两臂必须都带 arm 字段 (结算器按它配对)。

    holdout 行在 _gate_inner 里写, treatment 行在 _judge_one 里写 ⇒ 查整个模块。
    """
    import inspect
    src = inspect.getsource(G)
    assert '"arm": "holdout"' in src, "对照臂缺 arm 字段 ⇒ 无法配对"
    assert '"arm": "treatment"' in src, "门臂缺 arm 字段 ⇒ 无法配对"