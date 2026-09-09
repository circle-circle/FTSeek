"""daily_alert_summary 跨轮转档告警覆盖回归钉子 (2026-09-09)。

背景: 盘后告警汇总原只读 logs/ftseek_us.log 一个文件。/etc/logrotate.d/ftseek-us 用
daily + copytruncate + dateext(-%Y%m%d), 由 /etc/cron.daily/logrotate 触发, 近 7 个
交易日实测轮转时刻落在 03:12-03:48 CST —— 正在美股会话 (21:30-04:00 CST) **内部**
⇒ 会话前 ~5.5 小时的告警全部看不见。实证: 9/8 chase_cap拦截 轮转档 145 条 / 当前档
3 条, 推送报「0 错误 + 6 警告」= 真相的 4%; 9/4 同病 (轮转档 142 条 / 报告 2 条)。

本钉锁三件事: ①轮转档必须被发现且排在当前档之前 (时间升序); ②跨档合并不得重复计数
(copytruncate 读写竞态防御); ③覆盖诊断必须落 stdout —— 盲区复发时读数上直接可见,
不靠人事后对账。

回滚: 删本文件 + resolve_log_sources/parse_log_alerts_window 两函数, main() 改回
parse_log_alerts(us_log, ...) 单文件调用。
"""
import importlib.util
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest


_spec = importlib.util.spec_from_file_location(
    "daily_alert_summary", PROJECT_ROOT / "scripts" / "daily_alert_summary.py"
)
das = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(das)


# 会话窗: ET 交易日 2026-09-08 → CST 9/8 21:00 起, 9/9 06:00 止
SINCE = datetime(2026, 9, 8, 21, 0)
UNTIL = datetime(2026, 9, 9, 6, 0)


def _line(ts: str, msg: str) -> str:
    return f"{ts} [app.playbook.pipeline] WARNING: {msg}\n"


@pytest.fixture
def logdir(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    return d


def test_rotated_file_inside_window_is_discovered_and_ordered(logdir):
    """轮转档 (dateext=轮转当日) 必须被发现, 且排在当前档之前。"""
    cur = logdir / "ftseek_us.log"
    cur.write_text(_line("2026-09-09 03:50:00", "当前档告警"), encoding="utf-8")
    rot = logdir / "ftseek_us.log-20260909"
    rot.write_text(_line("2026-09-08 22:00:00", "轮转档告警"), encoding="utf-8")

    srcs = das.resolve_log_sources(str(cur), SINCE, UNTIL)

    assert len(srcs) == 2, "轮转发生在会话窗内时必须读到两个档, 否则前 5.5 小时盲区复发"
    assert srcs[-1] == str(cur), "当前档必须排最后 (时间升序拼接)"
    assert srcs[0] == str(rot)


def test_gz_rotated_variant_is_discovered(logdir):
    """compress + delaycompress: 隔日档是 .gz, 必须同样被发现。"""
    cur = logdir / "ftseek_us.log"
    cur.write_text(_line("2026-09-09 03:50:00", "当前档"), encoding="utf-8")
    (logdir / "ftseek_us.log-20260909.gz").write_bytes(b"")

    srcs = das.resolve_log_sources(str(cur), SINCE, UNTIL)

    assert any(s.endswith("-20260909.gz") for s in srcs)


def test_no_rotation_yet_returns_only_current(logdir):
    """cron.daily 晚于本脚本时 (9/6-9/7 实测 06:28/06:44) 尚无轮转档 ⇒ 只返回当前档。"""
    cur = logdir / "ftseek_us.log"
    cur.write_text(_line("2026-09-08 22:00:00", "整个会话都在当前档"), encoding="utf-8")

    srcs = das.resolve_log_sources(str(cur), SINCE, UNTIL)

    assert srcs == [str(cur)]


def test_out_of_window_rotated_file_contributes_zero(logdir):
    """候选日期全覆盖会带进上一会话的档 ⇒ 时间戳过滤必须把它兜成 0 条, 不得污染计数。"""
    cur = logdir / "ftseek_us.log"
    cur.write_text(_line("2026-09-09 03:50:00", "本会话"), encoding="utf-8")
    # -20260908 档内容全在 since 之前 (上一会话)
    (logdir / "ftseek_us.log-20260908").write_text(
        _line("2026-09-08 03:20:00", "上一会话的告警"), encoding="utf-8"
    )

    alerts, diag = das.parse_log_alerts_window(str(cur), "US", SINCE, UNTIL)

    assert len(alerts) == 1
    assert "上一会话" not in alerts[0]["content"]
    assert "ftseek_us.log-20260908=0" in diag[0], "覆盖诊断必须逐档报数, 0 条也要显式"


def test_cross_file_duplicate_counted_once(logdir):
    """copytruncate 不重叠, 但脚本运行途中正好轮转会产生读写竞态 ⇒ 同一行只计一次。"""
    dup = _line("2026-09-09 03:45:00", "竞态重复行")
    cur = logdir / "ftseek_us.log"
    cur.write_text(dup, encoding="utf-8")
    (logdir / "ftseek_us.log-20260909").write_text(
        _line("2026-09-08 22:00:00", "轮转档独有") + dup, encoding="utf-8"
    )

    alerts, _ = das.parse_log_alerts_window(str(cur), "US", SINCE, UNTIL)

    assert sum(1 for a in alerts if "竞态重复行" in a["content"]) == 1
    assert len(alerts) == 2


def test_merged_alerts_sorted_ascending_across_files(logdir):
    """跨档合并后必须按时间升序 —— 去重展示取 first_time, 乱序会把「首次」记成最后一次。"""
    cur = logdir / "ftseek_us.log"
    cur.write_text(_line("2026-09-09 03:50:00", "晚"), encoding="utf-8")
    (logdir / "ftseek_us.log-20260909").write_text(
        _line("2026-09-08 22:00:00", "早"), encoding="utf-8"
    )

    alerts, _ = das.parse_log_alerts_window(str(cur), "US", SINCE, UNTIL)

    assert [a["time"] for a in alerts] == sorted(a["time"] for a in alerts)
    assert "早" in alerts[0]["content"]


def test_coverage_diagnostic_exposes_source_count_and_span(logdir):
    """覆盖诊断是盲区复发的唯一读数面: 必须含档数、逐档条数、告警跨度、窗口边界。"""
    cur = logdir / "ftseek_us.log"
    cur.write_text(_line("2026-09-09 03:50:00", "尾段"), encoding="utf-8")
    (logdir / "ftseek_us.log-20260909").write_text(
        _line("2026-09-08 21:35:00", "首段"), encoding="utf-8"
    )

    _, diag = das.parse_log_alerts_window(str(cur), "US", SINCE, UNTIL)

    line = diag[0]
    assert "日志源 2 个" in line
    assert "ftseek_us.log-20260909=1" in line and "ftseek_us.log=1" in line
    assert "21:35:00→03:50:00" in line
    assert "09-08 21:00→09-09 06:00" in line


def test_missing_log_file_does_not_raise(logdir):
    """日志文件不存在时静默返回空 (脚本头部原则: 失败不影响主系统)。"""
    srcs = das.resolve_log_sources(str(logdir / "nope.log"), SINCE, UNTIL)
    assert srcs == []

    alerts, diag = das.parse_log_alerts_window(str(logdir / "nope.log"), "US", SINCE, UNTIL)
    assert alerts == []
    assert "无告警" in diag[0]


# ---------- 诊断行必须进推送正文 (不只 stdout) ----------

def _summaries():
    return ({"trades": 9, "pnl": -165.53, "positions": 3},
            {"trades": 0, "pnl": 0.0, "positions": 0})


def test_coverage_diagnostic_rendered_into_report_body():
    """诊断行必须渲染进**推送正文**: 只进 stdout 时, 正文里那句「✅ 盘中系统运行正常」
    在覆盖不全的场合仍会说谎 (9/8 实证: 正文报 6 条, 真相 603 条)。"""
    us, hk = _summaries()
    diag = ["US 日志源 3 个 (ftseek_us.log-20260908.gz=0, ftseek_us.log-20260909=592, "
            "ftseek_us.log=44) | 告警跨度 21:01:41→05:43:42 | 窗 09-08 21:00→09-09 06:00"]

    report = das.build_summary_report([], [], us, hk, "2026-09-08", log_coverage=diag)

    assert "日志覆盖自检" in report
    assert diag[0] in report, "诊断行必须原样进正文, 不得被压缩或改写"
    assert report.index("盘中系统运行正常") < report.index("日志覆盖自检"), \
        "覆盖自检必须与告警判级同屏且紧随其后, 否则读者先看到结论再看不到前提"


def test_report_body_without_coverage_kwarg_unchanged():
    """旧调用面 (不传 log_coverage) 不得多出覆盖段 —— 向后兼容, 不牵连既有钉子。"""
    us, hk = _summaries()

    report = das.build_summary_report([], [], us, hk, "2026-09-08")

    assert "日志覆盖自检" not in report
    assert report.rstrip().endswith("- ✅ 盘中系统运行正常，无异常告警")
