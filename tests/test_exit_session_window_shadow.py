"""退出侧时段窗影子 — 契约测试 (record-only 保证 + 接线位置 + 分类正确)。

守三条:
  ① 写方**只记不改**: 模块源码不得出现任何下单/平仓 API; 记录后调用方继续平仓。
  ② 接线点必须在 `exit_reason` 判定之后、真实平仓之前 (否则影子记录不到真实决策)。
  ③ 分类/隔夜判定边界正确, 且任何异常都不外抛 (影子绝不拖垮退出循环)。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.playbook import exit_session_window_shadow as sw

REPO = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")


def _et(h, m, s=0, day=4):
    return datetime(2026, 11, day, h, m, s, tzinfo=ET)


# ── ① record-only 保证 ──────────────────────────────────────────────
def test_module_has_no_order_or_close_api():
    src = (REPO / "app/playbook/exit_session_window_shadow.py").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    for forbidden in ("place_order", "close_position", "submit_order", "cancel_order",
                      "execute_exit", "dual_broker"):
        assert forbidden not in code, f"影子模块出现下单相关符号: {forbidden}"


def test_hook_sits_between_decision_and_close():
    src = (REPO / "app/playbook/pipeline_parts/exit_monitor_flow.py").read_text(encoding="utf-8")
    hook = src.index("record_exit_session_window_shadow as _rec_sess_win")
    gate = src.rindex("if not exit_reason:", 0, hook)   # 取挂接点之前最近的一处
    close = src.index("_row_target = \"real\" if r[\"source_type\"]", hook)
    assert gate < hook < close, "影子必须在 exit_reason 判定之后、真实平仓之前"


def test_hook_is_wrapped_and_does_not_short_circuit():
    """记完必须继续: 影子代码块内不得出现 continue/return, 且外层有 try/except。"""
    src = (REPO / "app/playbook/pipeline_parts/exit_monitor_flow.py").read_text(encoding="utf-8")
    i = src.index("record_exit_session_window_shadow as _rec_sess_win")
    block = src[i - 800:i + 1200]
    assert "except Exception" in block
    body = block[block.index("_rec_sess_win("):block.index("except Exception")]
    assert not re.search(r"^\s*(continue|return)\b", body, re.M), "影子块内出现控制流短路"


# ── ③ 分类边界 ──────────────────────────────────────────────────────
@pytest.mark.parametrize("h,m,expect", [
    (4, 0, "premarket"), (8, 54, "premarket"), (9, 29, "premarket"),
    (9, 30, "rth_open"), (9, 30, "rth_open"), (9, 34, "rth_open"),
    (9, 35, None), (10, 0, None), (15, 59, None),
])
def test_classify_exit_window(h, m, expect):
    assert sw.classify_exit_window(_et(h, m)) == expect


def test_overnight_true_for_previous_day():
    assert sw._is_overnight("2026-11-03T15:52:00-05:00", _et(9, 30)) is True


def test_overnight_false_for_same_day():
    assert sw._is_overnight("2026-11-04T09:40:00-05:00", _et(9, 45)) is False


def test_overnight_false_on_garbage():
    assert sw._is_overnight("", _et(9, 30)) is False
    assert sw._is_overnight("not-a-date", _et(9, 30)) is False


# ── ② 记录行为 ──────────────────────────────────────────────────────
@pytest.fixture
def shadow_file(tmp_path, monkeypatch):
    p = tmp_path / "exit_session_window_shadow.jsonl"
    monkeypatch.setattr(sw, "_SHADOW_PATH", p)
    monkeypatch.setattr(sw, "_SEEN", {})
    return p


def _call(**over):
    kw = dict(now_et=_et(9, 30, 20), symbol="US.LMT", direction="LONG", qty=9,
              exit_reason="OVERNIGHT_GAP_STOP", price=603.33,
              entry_time="2026-10-30T15:31:00-04:00", source_type="PLAYBOOK_SIM")
    kw.update(over)
    return sw.record_exit_session_window_shadow(**kw)


def test_records_one_line_with_schema(shadow_file):
    assert _call() is True
    lines = shadow_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    for k in ("ts_et", "date_et", "symbol", "direction", "qty", "exit_reason",
              "price_at_decision", "entry_time", "overnight", "window",
              "window_end_et", "source_type"):
        assert k in row, f"缺字段 {k}"
    assert row["window"] == "rth_open"
    assert row["window_end_et"] == "09:35"
    assert row["overnight"] is True


def test_premarket_window_recorded(shadow_file):
    assert _call(now_et=_et(5, 2, 41), symbol="US.LLY", exit_reason="SL_BREAKEVEN") is True
    row = json.loads(shadow_file.read_text(encoding="utf-8").strip())
    assert row["window"] == "premarket"


def test_outside_window_writes_nothing(shadow_file):
    assert _call(now_et=_et(9, 45, 33)) is False
    assert not shadow_file.exists()


def test_intraday_position_skipped(shadow_file):
    assert _call(entry_time="2026-11-04T09:40:00-05:00") is False
    assert not shadow_file.exists()


def test_dedup_within_ttl(shadow_file):
    assert _call() is True
    assert _call() is False           # 同一 (date,symbol,reason) 10 分钟内只记一次
    assert len(shadow_file.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_never_raises_on_bad_input(shadow_file):
    assert _call(qty=None, price=None) in (True, False)      # 不抛即可
    assert _call(symbol=None, exit_reason=None) is False
    assert _call(now_et=None) is False


def test_disabled_by_config(shadow_file, monkeypatch):
    monkeypatch.setattr(sw, "_enabled", lambda: False)
    assert _call() is False
    assert not shadow_file.exists()