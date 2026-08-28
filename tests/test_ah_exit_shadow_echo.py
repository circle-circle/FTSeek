"""AH影子 日志回声抑制回归测试 (2026-08-28)。

背景: 调度器 AFTER_HOURS/OVERNIGHT 每 2min tick 调 run_check → run_pass →
_write_summary 无条件打 INFO, 数据静止时同一组汇总行整夜重放 (实测 8/28 晨
99 轮完全相同的 "red_cut_1pct: 触发3/3 Σdelta=+118 p=1.0" 回声), 真事件被埋
噪声。修复 = _echo(tag, line): 同 tag 内容未变降 debug, 变化 (价格/计数移动)
恢复 INFO。纯日志呈现层, 评估/结算数学零改动。

旧行为对照: 修复前每次调用必打 INFO (本测试同时锁"首打不抑制", 防修过头吞
掉每个 tick 的第一次真实信号)。

回滚: 删本文件即可, 零生产代码依赖。
"""
import importlib.util
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "ah_exit_shadow", PROJECT_ROOT / "scripts" / "ah_exit_shadow.py"
)
aes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aes)

LOGGER = "ftseek.ah_exit_shadow"


@pytest.fixture(autouse=True)
def _clear_echo():
    aes._ECHO_CACHE.clear()
    yield
    aes._ECHO_CACHE.clear()


@pytest.fixture
def tmp_summary(tmp_path, monkeypatch):
    p = tmp_path / "summary.json"
    monkeypatch.setattr(aes, "SUMMARY", p)
    return p


def _info_lines(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == LOGGER and r.levelno == logging.INFO]


# ── _echo 机制 ──────────────────────────────────────────

def test_echo_first_call_fires_and_repeat_suppressed(caplog):
    """首打必 INFO (旧行为: 每 tick 必打); 同 tag 同行 → 只剩 debug."""
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        aes._echo("t", "line-A")
        assert len(_info_lines(caplog)) == 1
        caplog.clear()
        aes._echo("t", "line-A")  # 数据静止 → 抑制
        assert _info_lines(caplog) == []
        assert any("回声抑制" in r.getMessage() for r in caplog.records
                   if r.levelno == logging.DEBUG)


def test_echo_content_change_refires(caplog):
    """变数进 line: 价格移动 (920.09→919.70) 必须重新 INFO, 不是永久静音."""
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        aes._echo("arm:US.MU:2026-08-27:red_cut_1pct", "⚡ @ ah_close=920.09")
        aes._echo("arm:US.MU:2026-08-27:red_cut_1pct", "⚡ @ ah_close=919.70")
        lines = _info_lines(caplog)
        assert len(lines) == 2 and "919.70" in lines[-1]


def test_echo_tags_isolated(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        aes._echo("a", "same")
        aes._echo("b", "same")  # 不同 tag 互不抑制
        assert len(_info_lines(caplog)) == 2


# ── _write_summary 回声 (用户实见的 3 行) ────────────────

def _rows():
    """构造 1 夜已结算: red_cut 触发 delta=+100; 水下且 touch_be 触及."""
    rules = {
        "red_cut_1pct": {"triggered": True, "would_price": 99.0},
        "touch_be": {"hit": True, "line": 99.0},
        "touch_entry": {"hit": False},
        "touch_tp": {"hit": False},
        "in_water_at_open": True,
        "ah_best_fav_pct": -1.5,
    }
    return [dict(night="2026-08-27", symbol="US.TEST", direction="LONG",
                 entry_time="2026-08-26T10:00:00", entry_price=100.0, qty=10,
                 ah_close=98.0, tp=None, rules=rules, settled=True,
                 delta_red_cut_1pct=100.0, delta_touch_be=-60.0,
                 delta_touch_entry=None, delta_touch_tp=None)]


def test_summary_steady_state_is_quiet_and_change_refires(caplog, tmp_summary):
    rows = _rows()
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        aes._write_summary(rows)
        first = _info_lines(caplog)
        # rule 行 + touch_be:underwater 观测行 (touched>0 才打)
        assert any("red_cut_1pct: 触发1/1" in m and "+100" in m for m in first)
        assert any("观测 touch_be:underwater" in m and "-60" in m for m in first)

        caplog.clear()
        aes._write_summary(rows)  # 同数重算 — 修复前此处整组回声
        assert _info_lines(caplog) == []
        assert tmp_summary.exists()  # 文件照写 (保新度), 只是不刷日志

        rows[0]["delta_red_cut_1pct"] = 150.0  # 新结算 → 数字变 → 恢复 INFO
        caplog.clear()
        aes._write_summary(rows)
        refired = _info_lines(caplog)
        assert any("Σdelta=+150" in m for m in refired)
        assert not any("touch_be" in m for m in refired)  # 观测行数字没变, 仍静


# ── run_check stats 行 ─────────────────────────────────

def test_run_check_stats_line_deduped(caplog, monkeypatch, tmp_summary):
    """实时窗 '更新3 触发2' 连打 4 次的回声: 同数第 2 次起抑制, 结算数变必打."""
    monkeypatch.setattr(aes, "_enabled", lambda: True)
    calls = iter([dict(evaluated=0, updated=3, settled=0, triggered=2),
                  dict(evaluated=0, updated=3, settled=0, triggered=2),
                  dict(evaluated=0, updated=0, settled=3, triggered=2)])
    monkeypatch.setattr(aes, "run_pass", lambda live=False: next(calls))
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        aes.run_check()
        aes.run_check()  # 同数 → 抑制
        aes.run_check()  # 结算3 → 重新 INFO
    lines = _info_lines(caplog)
    assert len(lines) == 2
    assert "结算0" in lines[0] and "结算3" in lines[1]
