#!/usr/bin/env python3
"""[RFC-196 2026-11-10] 盘前供给闸 + 长步护栏 契约测试。

守的是什么 (每一条都对着 11-09 实盘事故的一个环节, logs/ftseek_us.log-20261110):
  1. 完成标记存在 → 跳过 (既有 #22 语义, 回归保护)
  2. DA 供给已完整落库 → 视作完成 + 写 source='da_supply' 标记 (新: 不再白跑 300-400s)
  3. DA 供给只落了一部分 → **仍跑生成** (保留 8/17 事故的自愈/补行能力, 不许静默缩供给)
  4. 组合闸 (方向偏斜/sector) 开启 → **不许跳过** (该闸只在生成侧执行, 跳过=静默摘风控)
  5. ET 过了启动截止 → 不启动长步 (长步跨开盘 = 阻塞入场)
  6. 当日尝试达上限 → 不再重试 (11-09 崩 18 轮/94min 的放大器)
  7. force=True → 一律放行 (人工应急通道)

纪律: 这些用例不碰生产库/不跑 MarketRegimeGate/不联网 —— 全部依赖经注入回调,
  "重活是否开跑"用第一个被调用的注入回调 (check_bridge_health_and_alert) 抛异常来判定。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from app.playbook.pipeline_parts import generation_flow as gf
from app.playbook.pipeline_parts import supply_gate as sg

TODAY = "2026-11-09"


class _FakeDB:
    def __init__(self, completed=False, rows=None):
        self._completed = completed
        self._rows = rows or []

    def is_generate_completed(self, date):
        return self._completed

    def get_instructions_by_date(self, date):
        return self._rows


class _Heavy:
    """记录"重活是否真的开跑" — 第一个注入回调即抛, 不进入真实筛选/LLM。"""

    def __init__(self):
        self.started = 0
        self.marked = []

    def bridge_health(self, context):
        self.started += 1
        raise RuntimeError("heavy-path-entered")

    def mark_done(self, date, source="pipeline"):
        self.marked.append((date, source))


@pytest.fixture(autouse=True)
def _reset_module_state():
    gf._GENERATE_ATTEMPTS.clear()
    gf._GENERATE_CAPPED.clear()
    gf._GENERATE_NOTICE_ONCE.clear()
    yield
    gf._GENERATE_ATTEMPTS.clear()
    gf._GENERATE_CAPPED.clear()
    gf._GENERATE_NOTICE_ONCE.clear()


def _run(monkeypatch, *, marker=False, supply_state=None, guardrail=False,
         deadline=920, now_hhmm="09:05", force=False):
    heavy = _Heavy()
    monkeypatch.setattr(
        "app.playbook.db.PlaybookDB", lambda *a, **k: _FakeDB(completed=marker)
    )
    if supply_state is not None:
        monkeypatch.setattr(
            sg, "da_supply_state",
            lambda date: {"state": supply_state, "want": 22, "have": 22,
                          "missing": [] if supply_state == "complete" else ["US.XYZ"]},
        )
    else:  # 未显式给供给态 → 视作 absent (与无 bridge 文件同日语义)
        monkeypatch.setattr(
            sg, "da_supply_state",
            lambda date: {"state": "absent", "want": 0, "have": 0, "missing": []},
        )
    monkeypatch.setattr(sg, "guardrail_blocks_skip", lambda: guardrail)
    monkeypatch.setattr(sg, "latest_generate_start_hhmm", lambda: deadline)

    hh, mm = (int(x) for x in now_hhmm.split(":"))
    now = datetime(2026, 11, 9, hh, mm, tzinfo=timezone.utc)

    class _Scheduler:
        voting_coordinator = object()  # 非 NullVotingAdapter

    result = gf.run_playbook_generate(
        _Scheduler(),
        force=force,
        today_provider=lambda: TODAY,
        playbook_enabled=lambda: True,
        trading_day_today=lambda: True,
        reload_bridge_if_landed=lambda s: None,
        check_bridge_health_and_alert=heavy.bridge_health,
        build_runtime_context=lambda s: (_ for _ in ()).throw(AssertionError("unreached")),
        build_instructions_from_votes=lambda **k: [],
        instruction_to_db_dict=lambda *a, **k: {},
        mark_generate_done=heavy.mark_done,
        logger=logging.getLogger("test.generation_flow"),
        now_et_provider=lambda: now,
    )
    return result, heavy


def test_marker_present_skips(monkeypatch):
    result, heavy = _run(monkeypatch, marker=True)
    assert result is None
    assert heavy.started == 0


def test_da_supply_complete_skips_and_writes_source(monkeypatch):
    result, heavy = _run(monkeypatch, marker=False, supply_state="complete", guardrail=False)
    assert result is None
    assert heavy.started == 0, "供给已完整落库仍跑了全量生成 (RFC-196 未生效)"
    assert heavy.marked == [(TODAY, "da_supply")], "缺 source='da_supply' 标记 (审计留痕)"


def test_da_supply_partial_still_generates(monkeypatch):
    result, heavy = _run(monkeypatch, marker=False, supply_state="partial")
    assert heavy.started == 1, "供给只落了一部分时必须重跑生成补行 (不许静默缩供给)"
    assert heavy.marked == []


def test_guardrail_enabled_blocks_skip(monkeypatch):
    """组合闸开启 ⇒ 不许跳过 (该闸只在生成侧执行)。"""
    result, heavy = _run(monkeypatch, marker=False, supply_state="complete", guardrail=True)
    assert heavy.started == 1
    assert heavy.marked == []


def test_deadline_blocks_late_start(monkeypatch):
    result, heavy = _run(monkeypatch, supply_state="absent", deadline=920, now_hhmm="09:21")
    assert result is None
    assert heavy.started == 0, "过了启动截止仍启动长步 (会跨开盘阻塞入场)"


def test_start_allowed_before_deadline(monkeypatch):
    result, heavy = _run(monkeypatch, supply_state="absent", deadline=920, now_hhmm="09:19")
    assert heavy.started == 1


def test_attempt_cap_stops_crash_loop(monkeypatch):
    """11-09 放大器: 每轮崩→每轮重来。上限后当日不再重试。"""
    starts = 0
    for _ in range(5):
        result, heavy = _run(monkeypatch, supply_state="absent", deadline=2359)
        starts += heavy.started
    assert starts == gf._GENERATE_MAX_ATTEMPTS, (
        f"崩溃循环未被上限拦住: 启动 {starts} 次 (上限 {gf._GENERATE_MAX_ATTEMPTS})"
    )


def test_force_bypasses_all_guards(monkeypatch):
    result, heavy = _run(
        monkeypatch, marker=True, supply_state="complete", guardrail=True,
        deadline=920, now_hhmm="10:30", force=True,
    )
    assert heavy.started == 1, "force=True 是人工应急通道, 不得被任何护栏拦住"


# ── supply_gate 判据本体 ─────────────────────────────────────────────────────
def _rows(symbols):
    return [{"symbol": s, "decision_id": f"DA-20261109-US.{s}-x",
             "row_kind": "instruction"} for s in symbols]


def test_da_supply_state_complete(monkeypatch):
    monkeypatch.setattr(sg, "bridge_candidate_symbols", lambda d: ["US.BA", "US.XOM"])
    monkeypatch.setattr("app.playbook.db.PlaybookDB",
                        lambda *a, **k: _FakeDB(rows=_rows(["BA", "XOM"])))
    st = sg.da_supply_state(TODAY)
    assert (st["state"], st["want"], st["have"]) == ("complete", 2, 2)


def test_da_supply_state_partial_lists_missing(monkeypatch):
    monkeypatch.setattr(sg, "bridge_candidate_symbols", lambda d: ["US.BA", "US.XOM"])
    monkeypatch.setattr("app.playbook.db.PlaybookDB",
                        lambda *a, **k: _FakeDB(rows=_rows(["BA"])))
    st = sg.da_supply_state(TODAY)
    assert st["state"] == "partial" and st["missing"] == ["XOM"]  # 内部比对键去 US. 前缀


def test_placeholder_rows_do_not_count_as_supply(monkeypatch):
    """占位行是"未提名"标记, 不能算已落库 (否则供给缺口被占位掩埋)。"""
    monkeypatch.setattr(sg, "bridge_candidate_symbols", lambda d: ["US.BA"])
    monkeypatch.setattr("app.playbook.db.PlaybookDB", lambda *a, **k: _FakeDB(rows=[
        {"symbol": "US.BA", "decision_id": "", "row_kind": "supply_placeholder"},
    ]))
    assert sg.da_supply_state(TODAY)["state"] == "partial"


def test_absent_when_no_bridge(monkeypatch):
    monkeypatch.setattr(sg, "bridge_candidate_symbols", lambda d: None)
    assert sg.da_supply_state(TODAY)["state"] == "absent"


def test_guardrail_read_failure_keeps_generating(monkeypatch):
    """组合闸开关读失败 → 保守按"开启"处理 (宁可多跑一轮, 不可静默少一层风控)。"""
    def _boom():
        raise RuntimeError("config down")
    monkeypatch.setattr(
        "app.playbook.pipeline_parts.portfolio_guardrails.portfolio_guardrail_enabled", _boom
    )
    assert sg.guardrail_blocks_skip() is True