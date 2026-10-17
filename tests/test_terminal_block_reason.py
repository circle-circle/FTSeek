"""终态门拦截原因归因 — 单元测试 + 源码契约守卫 (2026-10-17).

背景: 被门拦下且未成交的指令, EOD 兜底把 skip_reason 统一写成 "未触发 (价格未到)",
而多数门只在 trigger_miss_telemetry 留行 ⇒ 库里分不清"门拦截"与"价格未到"
(实测 2026-10-16 US.COIN/US.TSM: 终态标签与日志实据相反)。
修复 = 非成交行把当日拦截原因分布浅合并进 metadata_json (不动 skip_reason 字面)。

本文件钉三件事:
  1. 读遥测的口径正确 (分布/主因/末次; 主因剔除 after_cutoff; 缺行→None);
  2. 缓存按 (path, mtime, size) 失效, 追加行后不返回陈旧值;
  3. **源码契约**: EOD 写标签处仍接在 merge_instruction_metadata 上,
     且 skip_reason 的通用字面未被改动 (防重构静默摘掉接线)。

回滚: 删本文件即可 (纯测试, 零生产依赖)。
"""
import json
from pathlib import Path

import pytest

from app.playbook.shadow_tracker import _terminal_block_reason_for

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "app" / "playbook" / "shadow_tracker.py"


def _write(tmp_path, rows):
    p = tmp_path / "tm.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return str(p)


def _row(iid, d, reason, ts):
    return {"ts": ts, "date": d, "inst_id": iid, "symbol": "US.X",
            "direction": "BUY", "reason": reason}


def test_counts_primary_last(tmp_path):
    """分布按原因计数; primary 剔除 after_cutoff; last/ts 取文件末行。"""
    path = _write(tmp_path, [
        _row(10, "2026-10-16", "streak_ban", "2026-10-16T10:00:00-04:00"),
        _row(10, "2026-10-16", "streak_ban", "2026-10-16T10:05:00-04:00"),
        _row(10, "2026-10-16", "after_cutoff", "2026-10-16T15:58:25-04:00"),
        _row(11, "2026-10-16", "low_atr_block", "2026-10-16T10:00:00-04:00"),
    ])
    got = _terminal_block_reason_for(10, "2026-10-16", path=path)
    assert got["counts"] == {"streak_ban": 2, "after_cutoff": 1}
    assert got["primary"] == "streak_ban"      # 剔除 after_cutoff 后的最高频
    assert got["last"] == "after_cutoff"
    assert got["ts"] == "2026-10-16T15:58:25-04:00"


def test_primary_is_after_cutoff_when_only_cutoff(tmp_path):
    """只有 after_cutoff ⇒ primary=after_cutoff (诚实表达'到截止都没触发')。"""
    path = _write(tmp_path, [
        _row(20, "2026-10-16", "after_cutoff", "2026-10-16T14:05:00-04:00"),
        _row(20, "2026-10-16", "after_cutoff", "2026-10-16T15:58:25-04:00"),
    ])
    got = _terminal_block_reason_for(20, "2026-10-16", path=path)
    assert got["primary"] == "after_cutoff"
    assert got["counts"] == {"after_cutoff": 2}


def test_other_day_same_inst_isolated(tmp_path):
    """同 inst 不同交易日不串键 (date 是键的一部分)。"""
    path = _write(tmp_path, [
        _row(30, "2026-10-15", "vwap_gate_block", "2026-10-15T11:00:00-04:00"),
        _row(30, "2026-10-16", "runaway_up", "2026-10-16T11:00:00-04:00"),
    ])
    assert _terminal_block_reason_for(30, "2026-10-15", path=path)["primary"] == "vwap_gate_block"
    assert _terminal_block_reason_for(30, "2026-10-16", path=path)["primary"] == "runaway_up"


def test_missing_rows_and_missing_file(tmp_path):
    """无该 (inst,date) 行 → None; 文件不存在 → None (不抛)。"""
    path = _write(tmp_path, [_row(40, "2026-10-16", "streak_ban", "t")])
    assert _terminal_block_reason_for(999, "2026-10-16", path=path) is None
    assert _terminal_block_reason_for(40, "1999-01-01", path=path) is None
    assert _terminal_block_reason_for(40, "2026-10-16", path=str(tmp_path / "nope.jsonl")) is None


def test_bad_line_is_skipped(tmp_path):
    """坏行/缺字段行不得让整次读取失败。"""
    p = tmp_path / "tm.jsonl"
    p.write_text(
        "{not json}\n"
        + json.dumps({"ts": "t", "date": "2026-10-16", "inst_id": None, "reason": "x"}) + "\n"
        + json.dumps({"ts": "t2", "date": "2026-10-16", "inst_id": 50, "reason": "extreme_gap"}) + "\n",
        encoding="utf-8",
    )
    got = _terminal_block_reason_for(50, "2026-10-16", path=str(p))
    assert got["counts"] == {"extreme_gap": 1}


def test_cache_invalidates_on_append(tmp_path):
    """同进程内追加行后必须看到新值 (缓存键含 size)。"""
    p = tmp_path / "tm.jsonl"
    p.write_text(json.dumps(_row(60, "2026-10-16", "streak_ban", "t1")) + "\n", encoding="utf-8")
    assert _terminal_block_reason_for(60, "2026-10-16", path=str(p))["counts"] == {"streak_ban": 1}
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(_row(60, "2026-10-16", "streak_ban", "t2")) + "\n")
    got = _terminal_block_reason_for(60, "2026-10-16", path=str(p))
    assert got["counts"] == {"streak_ban": 2}, "追加后仍返回陈旧缓存 → UTC 键失效逻辑坏了"


class _FakeDB:
    """最小 db 桩: 只记录两类写入 (tracking / metadata merge), 不碰真库。"""

    def __init__(self):
        self.tracking = {}
        self.merged = {}

    def update_instruction_tracking(self, iid, updates):
        self.tracking[iid] = dict(updates)

    def merge_instruction_metadata(self, iid, patch):
        self.merged.setdefault(iid, {}).update(patch)
        return True


def _tracker_with_stubs(monkeypatch, telemetry_path):
    """构造不跑 __init__ 的 tracker (免 network/router), 并把遥测路径指向临时文件。"""
    from app.playbook import shadow_tracker as _st

    monkeypatch.setattr(_st, "_TBR_TELEMETRY", Path(telemetry_path))
    tr = _st.ShadowTracker.__new__(_st.ShadowTracker)
    tr.db = _FakeDB()
    # 只桩掉外部依赖 (日线/成交查询), **不桩被测代码本身**。
    # 注意: ma20 故意设成高于 day_high ⇒ shadow 突破判定 = False (triggered=0),
    #   否则本桩会落进 "已触发未执行" 分支, 测不到"未触发"分支的通用标签。
    tr._get_day_data = lambda symbol, trade_date: {
        "low": 1.0, "high": 2.0, "close": 1.5, "ma20": 99.0, "volume_ratio": 2.0,
    }
    tr._find_executed_trade = lambda *a, **k: None
    tr._is_phantom_attach = lambda *a, **k: False
    tr._calc_shadow_pnl = lambda *a, **k: 0.0
    return tr


def _inst(**over):
    base = {
        "id": 7001, "symbol": "US.ZZZ", "direction": "BUY", "entry_price": 1.5,
        "executed": 0, "triggered": 0, "outcome": None, "skip_reason": "",
        "position_size": 100, "decision_id": "DA-TEST-7001", "created_at": "2026-10-16T09:00:00",
    }
    base.update(over)
    return base


def test_call_site_tags_non_executed_row(tmp_path, monkeypatch):
    """生产路径集成: 非成交行 → 落 terminal_block_reason, 且 skip_reason 通用字面一字未改。"""
    path = _write(tmp_path, [
        _row(7001, "2026-10-16", "streak_ban", "2026-10-16T10:00:00-04:00"),
        _row(7001, "2026-10-16", "after_cutoff", "2026-10-16T15:58:25-04:00"),
    ])
    tr = _tracker_with_stubs(monkeypatch, path)
    tr._track_instruction(_inst(), "2026-10-16")

    assert tr.db.tracking[7001]["outcome"] == "missed"
    assert tr.db.tracking[7001]["skip_reason"] == (
        "未触发 (no_trigger: 盘中价格未突破入场确认条件)")  # 未改字面
    merged = tr.db.merged[7001]
    assert merged["terminal_block_reason"] == "streak_ban"
    assert merged["terminal_block_reasons"] == {"streak_ban": 1, "after_cutoff": 1}
    assert merged["terminal_block_reason_last"] == "after_cutoff"


def test_call_site_skips_executed_row(tmp_path, monkeypatch):
    """成交行不得被打归因标签 (否则会误导'被拦'计数)。"""
    path = _write(tmp_path, [_row(7002, "2026-10-16", "streak_ban", "t")])
    tr = _tracker_with_stubs(monkeypatch, path)
    tr._track_instruction(_inst(id=7002, executed=1), "2026-10-16")
    assert 7002 not in tr.db.merged


def test_call_site_no_telemetry_no_merge(tmp_path, monkeypatch):
    """无遥测行的非成交指令: 保留原通用标签, 不写空归因 (宁可缺, 不可假)。"""
    path = _write(tmp_path, [_row(9999, "2026-10-16", "streak_ban", "t")])
    tr = _tracker_with_stubs(monkeypatch, path)
    tr._track_instruction(_inst(id=7003), "2026-10-16")
    assert 7003 not in tr.db.merged
    assert tr.db.tracking[7003]["skip_reason"].startswith("未触发 (no_trigger")


def test_source_contract_wiring_and_untouched_label():
    """源码契约: 接线仍在 (读遥测→merge_instruction_metadata), 且 skip_reason 通用字面未改。"""
    src = _SRC.read_text(encoding="utf-8")
    assert "_terminal_block_reason_for(" in src
    assert "merge_instruction_metadata(" in src
    assert '"terminal_block_reason"' in src
    # 负向: 不得回退成直接塞 tracking updates (白名单会静默丢弃, 见 db.py:822-845)
    assert 'updates["terminal_block_reason"]' not in src
    # skip_reason 的通用字面必须原样保留 (6+ 消费方按字面解析)
    assert "未触发 (no_trigger: 盘中价格未突破入场确认条件)" in src