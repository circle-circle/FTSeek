"""内部人集群推送 事件级去重 (2026-09-21 用户裁).

同一集群 (键 = symbol:first_buy_date:latest_buy_date) 跨日只推一次, 防 30 天窗口内
每天盘前重复推送同一事件; 集群扩展 (latest_buy_date 变) → 新键 → 允许再次推送.
"""
import json

from app.core.scheduler_core import _insider_push_seen, _insider_push_mark


def test_first_time_not_seen(tmp_path):
    p = tmp_path / "insider_push_dedup.json"
    assert _insider_push_seen("US.UBER:2026-09-14:2026-09-20", p) is False


def test_mark_then_seen(tmp_path):
    p = tmp_path / "insider_push_dedup.json"
    k = "US.UBER:2026-09-14:2026-09-20"
    _insider_push_mark(k, "2026-09-21", p)
    assert _insider_push_seen(k, p) is True


def test_different_event_not_suppressed(tmp_path):
    p = tmp_path / "insider_push_dedup.json"
    _insider_push_mark("US.UBER:2026-09-14:2026-09-20", "2026-09-21", p)
    # 集群扩展 (latest_buy_date 变) → 新键 → 不抑制
    assert _insider_push_seen("US.UBER:2026-09-14:2026-09-25", p) is False


def test_stale_keys_pruned(tmp_path):
    p = tmp_path / "insider_push_dedup.json"
    _insider_push_mark("OLD:2026-01-01:2026-01-02", "2026-01-02", p)
    _insider_push_mark("NEW:2026-09-14:2026-09-20", "2026-09-21", p)
    seen = json.loads(p.read_text(encoding="utf-8"))
    assert "NEW:2026-09-14:2026-09-20" in seen
    assert "OLD:2026-01-01:2026-01-02" not in seen  # >120 天旧键被清理


def test_atomic_no_tmp_left(tmp_path):
    p = tmp_path / "insider_push_dedup.json"
    _insider_push_mark("US.BABA:2026-09-19:2026-09-20", "2026-09-21", p)
    leftovers = [f for f in tmp_path.iterdir() if f.name.endswith(".tmp")]
    assert leftovers == []