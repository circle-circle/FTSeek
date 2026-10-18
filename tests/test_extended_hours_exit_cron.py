import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.extended_hours_exit_cron as cron
from scripts import error_alert
from scripts.extended_hours_exit_cron import (
    _report_failure,
    _report_guard_failure,
    collector_command,
    main,
    run_once,
)


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


# ── INCIDENT-002 根因修: 守卫字段必须全路径在场 ──────────────────────────────
# 病灶: 守卫早于 session 门控执行, 但结果只回填到「窗外」分支; 窗内
# (premarket/afterhours/next_open) 的三个 return 全丢该字段 ⇒ 探针自身故障
# (ok:false) 在最需要它的交易时段**无人可见**, 与 INCIDENT-001 同型。


def _stub_guards(monkeypatch, *, closekey=None, orphan=None):
    import scripts.extended_hours_exit_cron as cron

    monkeypatch.setattr(cron, "scan_orphan_orders",
                        lambda dry=False: orphan or {"ok": True, "skip": "stub"})
    monkeypatch.setattr(cron, "scan_closekey_health",
                        lambda dry=False: closekey or {"ok": True, "deadlocks": []})


def _stub_positions(monkeypatch, positions):
    import scripts.extended_hours_exit_cron as cron

    monkeypatch.setattr(cron, "_load_open_positions_for_gate", lambda: positions)


class TestGuardFieldsPresentOnEveryPath:
    """run_once 的所有返回路径都必须带守卫字段 (单一出口契约)。"""

    def test_outside_hours_carries_both_guards(self, monkeypatch):
        _stub_guards(monkeypatch)
        monkeypatch.setattr(
            "scripts.extended_hours_exit_cron.get_exit_cron_session", lambda now=None: None)
        r = run_once()
        assert r["skip"] == "outside_extended_hours"
        assert "orphan" in r and "closekey_health" in r

    def test_in_session_dry_carries_both_guards(self, monkeypatch, tmp_path):
        """窗内 dry 分支 — 首版正是这里丢字段。"""
        _stub_guards(monkeypatch)
        _stub_positions(monkeypatch, [{"symbol": "US.TEST"}])
        monkeypatch.setattr(
            "scripts.extended_hours_exit_cron.get_exit_cron_session",
            lambda now=None: "premarket")
        # 2026-10-09: 锁路径隔入 tmp — 与生产每分钟 cron 共用真实锁文件时,
        #   cron tick 持锁瞬间本用例假红 (实测一次), 兄弟用例早已隔离。
        monkeypatch.setattr("scripts.extended_hours_exit_cron.LOCK_FILE",
                            tmp_path / "lock.file")
        r = run_once(dry=True)
        assert r["session"] == "premarket" and r.get("dry") is True
        assert "orphan" in r, "窗内分支必须带 orphan"
        assert "closekey_health" in r, "窗内分支必须带 closekey_health"

    def test_in_session_exec_carries_both_guards(self, monkeypatch, tmp_path):
        """窗内真跑分支 (subprocess 完成)。"""
        _stub_guards(monkeypatch)
        _stub_positions(monkeypatch, [{"symbol": "US.TEST"}])
        monkeypatch.setattr(
            "scripts.extended_hours_exit_cron.get_exit_cron_session",
            lambda now=None: "afterhours")
        monkeypatch.setattr("scripts.extended_hours_exit_cron.LOCK_FILE",
                            tmp_path / "lock.file")

        class _Done:
            returncode = 0
            stdout = "collector-ok"
            stderr = ""

        monkeypatch.setattr("subprocess.run", lambda *a, **k: _Done())
        r = run_once()
        assert r["ok"] is True and r["returncode"] == 0
        assert "orphan" in r and "closekey_health" in r

    def test_timeout_branch_carries_both_guards(self, monkeypatch, tmp_path):
        """超时分支 — 首版连 orphan 都丢 (最严重的信息丢失路径)。"""
        import subprocess as _sp

        _stub_guards(monkeypatch)
        _stub_positions(monkeypatch, [{"symbol": "US.TEST"}])
        monkeypatch.setattr(
            "scripts.extended_hours_exit_cron.get_exit_cron_session",
            lambda now=None: "premarket")
        monkeypatch.setattr("scripts.extended_hours_exit_cron.LOCK_FILE",
                            tmp_path / "lock.file")

        def _boom(*a, **k):
            raise _sp.TimeoutExpired(cmd="x", timeout=1)

        monkeypatch.setattr("subprocess.run", _boom)
        r = run_once()
        assert r["ok"] is False and "collector_timeout" in r["error"]
        assert "orphan" in r and "closekey_health" in r, "超时分支不得丢守卫字段"


class TestGuardFailureIsVisible:
    """守卫自身故障必须发契约 ERROR 行 (否则只躺在 JSON 块 ⇒ error_alert 扫不到)。"""

    def test_report_guard_failure_line_is_scannable(self, tmp_path, capsys):
        _report_guard_failure("closekey_health",
                              {"ok": False, "error": "sqlite3.DatabaseError: locked"})
        out = capsys.readouterr().out.strip()
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}", out), out
        assert "] ERROR:" in out, out
        assert "closekey_health" in out and "无人监视" in out, out
        assert "locked" in out, out

        # 复用既有仪表实测: error_alert 必须能数到它 (JSON 块是数不到的)
        (tmp_path / "extended_hours_exit_cron.log").write_text(out + "\n", encoding="utf-8")
        got = error_alert.scan(sorted(tmp_path.glob("*.log")))
        assert got["count"] == 1 and got["unparsed"] == 0, got

    def test_main_emits_guard_alert_without_failing_exit_chain(self, monkeypatch, capsys):
        """守卫降级不得把退出链一起标红 (否则真故障被淹没)。"""
        import scripts.extended_hours_exit_cron as cron

        monkeypatch.setattr(sys, "argv", ["extended_hours_exit_cron"])
        monkeypatch.setattr(cron, "run_once", lambda dry, timeout: {
            "ok": True, "session": "premarket", "returncode": 0,
            "orphan": {"ok": True},
            "closekey_health": {"ok": False, "error": "boom"}})
        main()                                  # 不抛 SystemExit (退出链本身是好的)
        printed = capsys.readouterr().out
        assert "] ERROR: 守卫 closekey_health 自身故障" in printed, printed
        assert '"ok": true' in printed, printed

    def test_guard_exception_degrades_not_crashes(self, monkeypatch):
        """守卫抛异常 ⇒ ok:false + error 回填, run_once 不炸 (退出链优先)。"""
        import scripts.extended_hours_exit_cron as cron

        def _boom(dry=False):
            raise RuntimeError("ledger db locked")

        monkeypatch.setattr(cron, "scan_orphan_orders", lambda dry=False: {"ok": True})
        monkeypatch.setattr(cron, "scan_closekey_health", _boom)
        monkeypatch.setattr(cron, "get_exit_cron_session", lambda now=None: None)
        r = run_once()
        assert r["ok"] is True, "守卫故障不得让整轮判失败"
        assert r["closekey_health"]["ok"] is False
        assert "RuntimeError" in r["closekey_health"]["error"]
        assert "ledger db locked" in r["closekey_health"]["error"]


class TestFlatSessionSkipsCollector:
    """2026-10-18 空跑治理: 无仓时不启动 collector, 但探针故障必须 fail-open."""

    def test_flat_session_skips_subprocess(self, monkeypatch, tmp_path):
        _stub_guards(monkeypatch)
        _stub_positions(monkeypatch, [])
        monkeypatch.setattr(
            "scripts.extended_hours_exit_cron.get_exit_cron_session",
            lambda now=None: "premarket")
        monkeypatch.setattr("scripts.extended_hours_exit_cron.LOCK_FILE",
                            tmp_path / "lock.file")

        def _must_not_spawn(*a, **k):
            raise AssertionError("无持仓时不应启动 collector")

        monkeypatch.setattr("subprocess.run", _must_not_spawn)
        r = run_once()
        assert r["collector"] == "skipped_no_positions"
        assert r["open_positions_n"] == 0
        assert "orphan" in r and "closekey_health" in r

    def test_probe_error_fails_open_to_collector(self, monkeypatch, tmp_path):
        _stub_guards(monkeypatch)

        def _boom():
            raise RuntimeError("db locked")

        monkeypatch.setattr(cron, "_load_open_positions_for_gate", _boom)
        monkeypatch.setattr(
            "scripts.extended_hours_exit_cron.get_exit_cron_session",
            lambda now=None: "afterhours")
        monkeypatch.setattr("scripts.extended_hours_exit_cron.LOCK_FILE",
                            tmp_path / "lock.file")

        class _Done:
            returncode = 0
            stdout = "collector-ok"
            stderr = ""

        monkeypatch.setattr("subprocess.run", lambda *a, **k: _Done())
        r = run_once()
        assert r["returncode"] == 0
        assert r["collector"] == "ok"
        assert r["open_positions_n"] is None
        assert "RuntimeError" in r["position_probe_error"]
