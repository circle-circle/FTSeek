"""日报「日志前缀剥离」钉子 (DEB-20260910-logging §11.14, 2026-09-11)

病灶 (实测, 非推演): `scripts/daily_alert_summary.py` 的 `_normalize` 与两处 bullet 渲染
各自写死 `content[50:]` 来剥日志前缀。那个 **50 是巧合常量** —— 只在 `[线程名]` 恰好
22 字符 (如 `[ThreadPoolExecutor-0_1]`) 时对齐。四种真实格式里**三种被切坏**:

| 真实行前缀 | logger 起始位 | `content[50:]` 渲染出 |
|---|---|---|
| `…03:38:02,733 [app.playbook.pipeline] WARNING:` (无线程字段, 旧格式) | 25 | `RNING: [playbook] …` ← **级别字符串被拦腰截断** |
| `…03:21:29,776 [LLMStrategyLoop] [app.playbook.pipeline] WARNING:` | 43 | `ybook.pipeline] WARNING: …` |
| `…21:40:05,531 [ThreadPoolExecutor-0_1] [app.data.finnhub_provider] WARNING:` | 50 | `app.data.finnhub_provider] WARNING: …` ✅ (唯一对齐的) |
| `…12:02:17,069 [MainThread] [app.core.scheduler_core] INFO:` | 38 | `eduler_core] INFO: …` |

两个后果 (均已实测):
  ① **展示**: 9/10 夜日报 **8 条 bullet 全部畸形** —— 7 条 logger 名被截断
     (`ybook.pipeline]` ×2 / `ybook.intraday_reentry]` ×5, 照着 grep 不到),
     1 条只差左括号 (`app.data.finnhub_provider]`, 恰是唯一对齐的线程名宽度)。
     对照原件 `data/reports/daily_alert_summary_2026-09-10.md`; 该报告还被 Hermes cron
     `7a40dd7c9e94` (`0 4 * * 2-6`) 的 LLM 读走并转述 ⇒ 残缺串进推送正文。
  ② **去重键**: `_normalize` 用同一切片, 于是**同一模板换个线程名就裂成多组** ——
     与 §11.11「69 轮炸成 69 组」同一病族 (那次是裸整数没归一, 这次是前缀宽度不稳)。

修法: 正则剥前缀 `_strip_log_prefix()`, 键与展示**同源同函数**; 解析不出来就原样返回
(宁可多显示不可少显示); 刻意不保留线程名 (否则它进键就会让同一模板按线程裂组)。
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "daily_alert_summary_for_prefix", PROJECT_ROOT / "scripts" / "daily_alert_summary.py"
)
das = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(das)

_SRC = (PROJECT_ROOT / "scripts" / "daily_alert_summary.py").read_text(encoding="utf-8")

LOGGER = "[app.playbook.pipeline] WARNING: [playbook] chase_cap拦截: US.AMD SELL_SHORT"


def _line(thread: str | None, logger_level_msg: str = LOGGER,
          ts: str = "2026-09-11 03:21:29,776") -> str:
    mid = f"[{thread}] " if thread else ""
    return f"{ts} {mid}{logger_level_msg}"


def test_old_format_without_thread_field_is_parsed():
    """旧格式 (无 `[线程名]` 字段) 仍在轮转档里大量存在, 不得被切成 `RNING: …`。"""
    got = das._strip_log_prefix(_line(None))
    assert got == LOGGER
    assert "WARNING:" in got and got.startswith("[app."), f"级别/logger 被切坏: {got!r}"


@pytest.mark.parametrize("thread", [
    "MainThread",                  # 10 字符 — 实测被切成 `eduler_core] INFO:`
    "LLMStrategyLoop",             # 15 字符 — 实测被切成 `ybook.pipeline]`
    "PlaybookExitFastLoop",        # 20 字符
    "ThreadPoolExecutor-0_1",      # 22 字符 — 唯一原本对齐的宽度
    "subscription-us-watchdog",    # 含连字符
])
def test_thread_name_width_does_not_change_output(thread):
    """核心不变量: 剥前缀的结果**与线程名长度无关**。"""
    assert das._strip_log_prefix(_line(thread)) == LOGGER


def test_all_thread_widths_share_one_dedup_key():
    """同一模板从不同线程发出 ⇒ 必须是**一组**, 不得按线程名裂组。"""
    alerts = [{"level": "WARN", "content": _line(t), "time": f"2026-09-11 03:2{i}:00"}
              for i, t in enumerate(
                  ["MainThread", "LLMStrategyLoop", "ThreadPoolExecutor-0_1",
                   "PlaybookExitFastLoop"])]
    grouped = das._dedup_alerts(alerts)
    assert len(grouped) == 1, f"同一模板被拆成 {len(grouped)} 组 ⇒ 日报炸组"
    assert grouped[0]["count"] == 4


def test_different_loggers_stay_separate():
    """剥离不得过头: 同一句话从两个 logger 发出仍是两组 (否则日报漏报)。"""
    other = LOGGER.replace("app.playbook.pipeline", "app.playbook.intraday_reentry")
    grouped = das._dedup_alerts([
        {"level": "WARN", "content": _line("MainThread"), "time": "t1"},
        {"level": "WARN", "content": _line("MainThread", other), "time": "t2"},
    ])
    assert len(grouped) == 2


def test_levels_all_recognised():
    for lvl in ("DEBUG", "INFO", "WARNING", "WARN", "ERROR", "CRITICAL"):
        raw = _line("LLMStrategyLoop", f"[app.x.y] {lvl}: 正文")
        assert das._strip_log_prefix(raw) == f"[app.x.y] {lvl}: 正文", lvl


def test_unparsable_lines_pass_through_unchanged():
    """解析不出来就原样返回 —— 宁可多显示, 不可少显示 (traceback 续行不得被吃掉)。"""
    for raw in ("Traceback (most recent call last):",
                '  File "/opt/ftseek/FTSeek-US/app/x.py", line 134',
                "ModuleNotFoundError: No module named 'config.config_center'",
                "",
                "没有前缀的裸文本"):
        assert das._strip_log_prefix(raw) == raw


def test_rendered_bullet_carries_intact_logger_name():
    """端到端: 渲染进日报正文的那一行必须带完整 logger 名 (9/10 夜的 `ybook.pipeline]` 不得重现)。"""
    alert = {"market": "US", "time": "2026-09-11 03:21:29", "level": "WARN",
             "content": _line("LLMStrategyLoop"), "is_trading_hours": True}
    summary = {"trades": 1, "pnl": -1.0, "positions": 1, "errors": 0}
    report = das.build_summary_report([alert], [], summary, dict(summary), "2026-09-11")
    bullet = next(ln for ln in report.splitlines() if "chase_cap拦截" in ln)
    assert "[app.playbook.pipeline] WARNING:" in bullet, bullet
    # 注意: 残缺形态 `ybook.pipeline]` 是正确形态 `app.playbook.pipeline]` 的**子串**,
    # 直接断言 not in 会自我打脸 ⇒ 钉"级别标记后紧跟完整 `[app.`"这个形状。
    assert "[WARN] [app." in bullet, f"logger 名仍被切坏 (级别后未紧跟完整 logger): {bullet}"
    assert "[WARN] ybook" not in bullet, f"9/10 夜的残缺形态重现: {bullet}"


def test_no_hardcoded_50_slice_left_in_code():
    """源级棘轮 (AST 判定, 不被注释/docstring 里的字面量绊红 — §11.11 方法论坑)。"""
    tree = ast.parse(_SRC)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            sl = node.slice
            if isinstance(sl, ast.Slice) and isinstance(sl.lower, ast.Constant) \
                    and sl.lower.value == 50 and sl.upper is None:
                offenders.append(getattr(node, "lineno", -1))
    assert not offenders, f"仍有 [50:] 硬切片 (行 {offenders}) — 前缀宽度是变量, 不得写死"
    assert _SRC.count("_strip_log_prefix(") >= 4, (
        "剥前缀必须**同源同函数**: 1 处定义 + _normalize + 两处渲染 (US/HK)")
