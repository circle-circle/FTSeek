"""盘前经济日历的输入真实性契约 (2026-11-09)。

背景: 原 `_get_economic_calendar` 按星期几猜 CPI/NFP/FOMC 并硬编码日期, 查不到就
输出 "No major scheduled events this week" —— 把"本函数没覆盖"谎成"本周无事件"。
守三条:
  1. 事件必须逐条带 title/ts_et/star/source;
  2. 取数失败只能写"不可用", 不得出现"无事件"的断言;
  3. 真源里不得再有硬编码日期与"本周无重要事件"兜底。
"""
from pathlib import Path

import pytest

from app.data import economic_calendar as ec
from app.data import premarket_enrichment as pe


def _patch_schedule(monkeypatch, payload):
    monkeypatch.setattr(ec, "fetch_us_event_schedule", lambda now: payload)


def test_events_render_with_fields_and_source(monkeypatch):
    _patch_schedule(monkeypatch, {
        "status": "ok",
        "events": [
            {"title": "美国批发销售月率", "ts_et": "2026-11-09 10:00", "star": "HIGH"},
            {"title": "美国纽约联储1年通胀预期", "ts_et": "2026-11-09 11:00", "star": "MEDIUM"},
        ],
    })
    text = pe._get_economic_calendar()
    assert "美国批发销售月率" in text and "10:00 ET" in text
    assert "title=" in text and "star=HIGH" in text and "source=" in text
    assert text.count("⚡") == 2
    assert "no major" not in text.lower()


def test_failure_says_unavailable_not_no_events(monkeypatch):
    _patch_schedule(monkeypatch, {"status": "opend_unreachable", "events": []})
    text = pe._get_economic_calendar()
    assert "unavailable" in text.lower()
    assert "do not infer no events" in text.lower()
    assert "no major" not in text.lower()
    assert "⚡" not in text


def test_zero_events_is_reported_as_source_zero_not_no_events(monkeypatch):
    _patch_schedule(monkeypatch, {"status": "ok", "events": []})
    text = pe._get_economic_calendar()
    assert "0" in text and "source=" in text
    assert "no major" not in text.lower()


def test_exception_path_is_visible(monkeypatch):
    def _boom(now):
        raise RuntimeError("futu down")
    monkeypatch.setattr(ec, "fetch_us_event_schedule", _boom)
    text = pe._get_economic_calendar()
    assert "unavailable" in text.lower() and "RuntimeError" in text


def test_calendar_module_has_no_hardcoded_dates():
    """文档/注释可引用旧行为作为证据; 可执行代码里不得复活硬编码或"无事件"断言。"""
    import ast
    tree = ast.parse(Path(ec.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body[0].value.value = ""
    code = ast.unparse(tree)
    for needle in ("2026, 5, 6", "2026, 11, 4", "2026, 12, 16",
                   "No major scheduled events", "likely this week"):
        assert needle not in code


def test_disabled_config_returns_disabled(monkeypatch):
    monkeypatch.setattr(ec, "_config_enabled", lambda: False)
    assert ec.fetch_us_event_schedule.__module__ == "app.data.economic_calendar"
    out = ec.fetch_us_event_schedule(__import__("datetime").datetime.now())
    assert out["status"] == "disabled" and out["events"] == []