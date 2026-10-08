"""出场日 gap 证据采集 (RFC-106 §5.2, record-only).

QCOM 2026-10-08 案: OVERNIGHT_GAP_STOP 出场后无法区分「真开盘跳空」与
「开盘后下杀」— 判据无 gap 校验, metadata 也不落 prev_close/today_open。
本组测试锁: ①字段口径与 entry_gap 对称; ②fail-open 缺价不写键;
③不覆盖既有值 (幂等防重复巡检刷写); ④缺 meta/quote 不抛 (绝不阻断平仓)。

回滚: 删 note_exit_gap + pipeline 两个调用点 + telemetry 镜像行。
"""

import os
import sys

sys.path.insert(0, str(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))))

from app.playbook.exit_engine import note_exit_gap  # noqa: E402


def test_records_gap_evidence_symmetric_with_entry_gap():
    meta = {}
    quote = {"prev_close": 177.02, "open": 173.90, "price": 174.17, "data_source": "futu_lv3"}
    note_exit_gap(meta, quote, "OVERNIGHT_GAP_STOP", 174.17)
    gap = meta["exit_gap"]
    assert gap["prev_close"] == 177.02
    assert gap["today_open"] == 173.90
    assert gap["gap_pct"] == round((173.90 - 177.02) / 177.02 * 100, 3)
    assert gap["decision_price"] == 174.17
    assert gap["decision_vs_open_pct"] == round((174.17 - 173.90) / 173.90 * 100, 3)
    assert gap["quote_source"] == "futu_lv3"
    assert gap["recorded_for"] == "OVERNIGHT_GAP_STOP"


def test_fail_open_when_prev_close_or_open_missing():
    for quote in ({}, {"prev_close": 177.02}, {"open": 173.90},
                  {"prev_close": 0, "open": 173.90}):
        meta = {}
        note_exit_gap(meta, quote, "SL_INITIAL", 174.17)
        assert "exit_gap" not in meta


def test_idempotent_no_overwrite_on_repeat_polls():
    meta = {"exit_gap": {"prev_close": 177.02, "recorded_for": "OVERNIGHT_GAP_STOP"}}
    note_exit_gap(meta, {"prev_close": 170.0, "open": 171.0}, "SL_INITIAL", 171.0)
    assert meta["exit_gap"]["prev_close"] == 177.02
    assert meta["exit_gap"]["recorded_for"] == "OVERNIGHT_GAP_STOP"


def test_never_raises_on_bad_inputs():
    note_exit_gap(None, {"prev_close": 177.02, "open": 173.90}, "SL_INITIAL", 174.17)
    note_exit_gap({}, None, "SL_INITIAL", 174.17)
    note_exit_gap({}, {"prev_close": "bad", "open": 173.90}, "SL_INITIAL", 174.17)


def test_telemetry_mirror_wiring():
    """exit_telemetry 构建处必须镜像 exit_gap — 单表切片不用翻顶层 metadata."""
    import inspect
    from app.playbook import pipeline as pb
    source = inspect.getsource(pb._close_trade_record)
    assert '"exit_gap": _tmeta.get("exit_gap")' in source
