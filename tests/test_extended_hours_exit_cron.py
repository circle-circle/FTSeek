import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import error_alert
from scripts.extended_hours_exit_cron import _report_failure, collector_command, main


def test_collector_command_selects_session_specific_script():
    assert collector_command("premarket")[1].endswith("premarket_exit_shadow.py")
    assert collector_command("afterhours")[1].endswith("afterhours_exit_shadow.py")
    assert collector_command(None) is None
    assert collector_command("overnight") is None


TRACEBACK = (
    'Traceback (most recent call last):\n'
    '  File "/opt/ftseek/FTSeek-US/scripts/afterhours_exit_shadow.py", line 188, in collect\n'
    '    m = quote_metrics(s, direction)\n'
    'ValueError: The truth value of a Series is ambiguous\n'
)


def test_report_failure_emits_line_counted_by_error_alert(tmp_path, capsys):
    """回归钉 (INCIDENT-001 P1): 子进程失败必须发出**契约格式 ERROR 行**。

    失败模式: 原实现只把 rc 写进 JSON 块 ⇒ 行首无时间戳 ⇒ error_alert 视作 unparsed
    跳过, 叠加其 cron 窗不覆盖延长时段 ⇒ 退出链每分钟崩、连崩 7 轮仍**零告警**。
    本钉同时锁三件: ①有时间戳前缀 (error_alert `_parse_line` 契约) ②含 `] ERROR:` 标记
    ③带出崩点文件行 (便于一眼定位), 并用 error_alert.scan 实测计数 (复用既有仪表)。
    """
    _report_failure({"ok": False, "session": "afterhours", "returncode": 1,
                     "stderr_tail": TRACEBACK})
    out = capsys.readouterr().out.strip()
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}", out), out
    assert "] ERROR:" in out, out
    assert "ValueError: The truth value of a Series is ambiguous" in out, out
    assert "afterhours_exit_shadow.py" in out, out
    assert "session=afterhours" in out, out

    (tmp_path / "extended_hours_exit_cron.log").write_text(out + "\n", encoding="utf-8")
    got = error_alert.scan(sorted(tmp_path.glob("*.log")))
    assert got["count"] == 1, got
    assert got["unparsed"] == 0, got


def test_report_failure_timeout_branch_and_main_exit(capsys, monkeypatch):
    """超时分支 (无 stderr, 只有 error 键) 同样必须留痕; main() 失败即 rc=1 且发告警行。"""
    _report_failure({"ok": False, "session": "premarket",
                     "error": "collector_timeout_after_240s"})
    out = capsys.readouterr().out.strip()
    assert "] ERROR:" in out and "collector_timeout_after_240s" in out, out

    import scripts.extended_hours_exit_cron as cron

    monkeypatch.setattr(sys, "argv", ["extended_hours_exit_cron"])   # main() 走 argparse, 勿吃 pytest 参数
    monkeypatch.setattr(cron, "run_once",
                        lambda dry, timeout: {"ok": False, "session": "afterhours",
                                              "returncode": 1, "stderr_tail": TRACEBACK})
    try:
        main()
        raise AssertionError("main() 在子进程失败时必须 SystemExit(1)")
    except SystemExit as e:
        assert e.code == 1
    printed = capsys.readouterr().out
    assert "] ERROR:" in printed, printed
    assert '"returncode": 1' in printed, printed     # JSON 块保留 (机读/审计)
