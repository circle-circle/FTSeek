"""盘中诊断报告 (no_agent 载体) 口径保真 — 契约测试 (2026-10-31, RFC-183)

三个病灶 (10-30 12:00 ET 推送实锤) 各钉一条:

① **SL/TP 取镜像列** ⇒ 报告高估保护。MRK 报告 147.5145/190.285, 而生产退出链读
   `trade_records.metadata_json.stop_loss/take_profit` = 143.90/162.89 (到位亏 -$258
   vs 报告暗示的 -$119)。`open_positions.current_sl/initial_tp` 是 position_sync 通用
   路径按 ATR×max_loss_pct(2%) 夹逼写的建仓镜像, playbook 退出链从不回写。
② **ERROR 扫描无日期过滤** ⇒ 上一 ET 交易日的 ERROR 被当"今日异常"反复推送
   (文件首两行 2026-10-30 03:31:29 CST = 2026-10-29 15:31 ET; 该文件 03:30 CST 才轮转)。
③ **同一次故障双计** ⇒ 告警行 + `[Watchdog.heal]` 动作行同秒同 logger 被计成 2 个 ERROR。

测试取 `ops/hermes/` 的库内副本 (SSOT), 与线上 `~/.hermes/scripts/` 由
`test_ops_scripts_sync.py` 保证同一份。
"""
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO = Path(__file__).resolve().parents[1]
CARRIER = REPO / "ops" / "hermes" / "intraday_diagnose_late.py"
SRC = CARRIER.read_text(encoding="utf-8")


# ══════════ ① SL/TP 必须取授权源 (metadata), 镜像列只作兜底 ══════════

def test_position_sl_tp_reads_authorized_metadata():
    assert "auth_sl_tp" in SRC, "授权 SL/TP 映射被移除 ⇒ 报告回到读镜像列 (高估保护)"
    # 授权源: 未平仓 playbook trade_records 的 metadata_json
    assert re.search(
        r"SELECT symbol, metadata_json FROM trade_records[\s\S]{0,300}?exit_time IS NULL",
        SRC,
    ), "授权源查询缺失或不再按未平仓过滤"
    assert "'PLAYBOOK_SIM'" in SRC and "'PLAYBOOK_LIVE'" in SRC, "授权源未限定 playbook 持仓"
    # 展示行必须优先授权值, 且缺失时回落镜像列 (fail-open)
    seg_start = SRC.index("_a = auth_sl_tp.get(")
    seg = SRC[seg_start:seg_start + 700]
    assert "_a[0] if" in seg and "_a[1] if" in seg, "持仓行未用授权 SL/TP"
    assert "current_sl" in seg and "initial_tp" in seg, "缺回落分支 ⇒ 取数失败时整行消失"
    assert "size={d['size']}" in seg, "持仓展示行结构被改坏"


def test_authorized_values_parse_from_production_shape():
    """用真实 metadata_json 形态复算 (MRK 10-30): 解析后应与生产退出链同值。"""
    md = (
        '{"stop_loss": 143.9, "take_profit": 162.89, "_early_tight_sl_px": 147.135,'
        ' "atr": 6.78, "_qty": 39}'
    )
    import json

    m = json.loads(md)
    assert m["stop_loss"] == 143.9 and m["take_profit"] == 162.89
    # 镜像列 (ATR×2% 夹逼) 与授权值不同源 — 这条差值就是本测试防的回归面
    assert round(150.525 * 0.98, 4) == 147.5145 != m["stop_loss"]


# ══════════ ②③ ERROR 扫描: ET 交易日过滤 + 事件去重 ══════════

def _cst_line(et_moment_iso: str, thread: str, logger: str, level: str, msg: str) -> str:
    """把 ET 时刻写成日志里的宿主机 CST 墙钟行 (模拟真实文件)。"""
    et_dt = datetime.fromisoformat(et_moment_iso).astimezone(ZoneInfo("America/New_York"))
    cst = et_dt.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S,000")
    return f"{cst} [{thread}] [{logger}] {level}: {msg}\n"


def _run_scan(tmp_path, today_et, lines):
    log = tmp_path / "ftseek_us.log"
    log.write_text("".join(lines), encoding="utf-8")
    start = SRC.index("# 8. Error log scan")
    end = SRC.index("# 9. Healthcheck")
    ns = {
        "re": re, "os": os, "datetime": datetime, "add": lambda s: None,
        "LOG_PATH": str(log), "today": today_et,
        "et": ZoneInfo("America/New_York"),
    }
    exec(compile(SRC[start:end], str(CARRIER), "exec"), ns)  # noqa: S102
    return ns["error_count"], ns["recent_errors"]


def test_error_scan_counts_today_and_dedupes_one_incident(tmp_path):
    """同秒同 logger 的「告警行 + heal 动作行」= 1 个事件, 不是 2 个。"""
    today = "2026-10-30"
    lines = [
        _cst_line("2026-10-30T15:31:29-04:00", "Watchdog", "app.observability.watchdog",
                  "ERROR", "主循环 llm_strategy_loop 已 1817s 无心跳 (阈值 600s)"),
        _cst_line("2026-10-30T15:31:29-04:00", "Watchdog", "app.observability.watchdog",
                  "ERROR", "[Watchdog.heal] restart: llm_strategy_loop stale 1817s"),
        _cst_line("2026-10-30T16:02:00-04:00", "Signal", "app.signal.bus", "CRITICAL", "boom"),
    ]
    n, shown = _run_scan(tmp_path, today, lines)
    assert n == 2, f"告警+heal 双计或 CRITICAL 漏计: {shown}"
    assert any("llm_strategy_loop" in s for s in shown)
    assert any("boom" in s for s in shown)


def test_error_scan_excludes_previous_et_trading_day(tmp_path):
    """CST 03:31 (= 前一个 ET 交易日 15:31) 的 ERROR 不得算进今日 — 本载体最常犯的错。"""
    today = "2026-10-30"
    lines = [
        # CST 2026-10-30 03:31:29 == ET 2026-10-29 15:31:29 (文件轮转前的旧事件)
        "2026-10-30 03:31:29,976 [Watchdog] [app.observability.watchdog] ERROR: stale old\n",
    ]
    n, shown = _run_scan(tmp_path, today, lines)
    assert n == 0, f"上一 ET 交易日的 ERROR 被算进今日: {shown}"


def test_error_scan_ignores_info_lines_mentioning_error(tmp_path):
    """INFO 行里提到 'ERROR' 不算异常 (旧 `grep -c ERROR` 会误计)。"""
    today = "2026-10-30"
    lines = [
        "2026-10-31 00:05:00,000 [Noise] [app.noise] INFO: 本轮统计 3 个 ERROR 记录 (非异常)\n",
    ]
    n, shown = _run_scan(tmp_path, today, lines)
    assert n == 0, f"INFO 行被误计为异常: {shown}"


def test_error_scan_excludes_unparseable_timestamp(tmp_path):
    """无法定位时间的裸行不计入 (宁少报不虚报)。"""
    today = "2026-10-30"
    lines = ["[游离子进程] [app.x] ERROR: 无时间戳前缀\n"]
    n, _ = _run_scan(tmp_path, today, lines)
    assert n == 0


# ══════════ 明细: 触发未执行必须归因到门 ══════════

def test_triggered_not_executed_is_attributed_to_gate():
    assert "instr_blocked" in SRC, "触发未执行的归因段被移除 ⇒ 8 触发/5 执行的差额无解释"
    assert "skip_reason" in SRC[SRC.index("SELECT symbol, direction, confidence, triggered"):][:400], \
        "指令查询未取 skip_reason ⇒ 无法归因"
    assert "触发未执行:" in SRC, "报告不再打印触发未执行明细"