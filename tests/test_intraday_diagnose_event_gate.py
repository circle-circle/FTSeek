"""盘中诊断事件门控契约钉 (2026-10-09 推送治理批, 用户裁方案A).

病灶: job 36cc015242ee (`ops/hermes/intraday_diagnose.py`) 旧版每 30min 无条件推
全量诊断 — 一晚 6 条, 10 行指令表原样重播; 而成交/平仓富卡已实时双发飞书
(broadcast_trade_events=on), outcome/pnl/持仓块全是迟到的二手信息。
另有死指标: `grep -c 'ERROR|Traceback'` 少 -E ⇒ 恒 0 (同一日志 -E 真数 21)。

改造 (方案A): 交易日首轮推「计划快照」, 其后仅白名单事件才推
(E1 服务宕机每轮重复 / E2 触发未执行持续>10min / E3 水位变化 / E4 退出卡单 /
E5 ERROR 突增≥3 或任意 CRITICAL / E6 新增指令), 无事件 ⇒ 空 stdout = SILENT。

本文件两层钉:
  A. 源码级: 死指标不得回归; 静默契约/fail-open/宕机白名单/E2 计时机制在位。
  B. 功能级: 沙盒副本 (常量替换到 tmp_path + systemctl→echo) 实跑, 用真 sqlite
     构造事件, 验「首轮推/无事静默/各事件触发一次且仅一次/损坏 state fail-open」。

⚠️ E2 的教训 (2026-10-09 实测): 不得用 playbook_instructions.updated_at 判挂起龄 —
chase_cap 每轮重定价会刷新它 (ARM 挂起 2h 而龄恒 ~2min) ⇒ 必须用 state 记
「连续见到 pending 的首次时刻」(trig_pending_first_seen)。
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CARRIER = REPO / "ops" / "hermes" / "intraday_diagnose.py"
SRC = CARRIER.read_text(encoding="utf-8")


# ══════════════════════ A. 源码级契约 ══════════════════════

def test_dead_error_metric_never_returns():
    """旧版 `grep -c` 匹配字面量 'ERROR|Traceback' (少 -E) 恒 0 = 死指标, 不得回归.

    ⚠️ 只查代码体 — 载体 docstring 里记着这个历史教训的字面量, 全文匹配会假红
    (与 test_hardcoded_utc4_ratchet 排除 docstring 同族)。
    """
    body = SRC.split('"""', 2)[2]  # 剥掉模块 docstring
    assert "ERROR|Traceback" not in body, (
        "字面量 'ERROR|Traceback' grep 回归 — 该判据自上线起恒 0 (缺 -E), 是死指标")
    assert "] ERROR:" in body and "] CRITICAL:" in body, "ERROR/CRITICAL 增量扫描缺失"


def test_silence_contract_and_fail_open():
    assert "Empty stdout = SILENT" in SRC, "no_agent 静默契约标记缺失"
    assert "_load_state" in SRC and "_save_state" in SRC
    # fail-open: 损坏 state 必须照发而非崩 (rc≠0 会触发 Hermes 失败告警, 更糟)
    assert "fail-open" in SRC


def test_service_down_is_always_delivered():
    """服务宕机不参与去重 — 每轮重复提醒是特性 (同 late 档 _ALWAYS_DELIVER_MARKERS 语义)."""
    assert "服务未运行" in SRC


def test_e2_uses_state_timer_not_updated_at():
    """E2 挂起龄必须来自 state 计时, 不得回退到 updated_at (chase_cap 会刷新它)."""
    assert "trig_pending_first_seen" in SRC
    e2_block = SRC[SRC.index("# E2."):SRC.index("# E3.")]
    # 剥注释行 — 教训注释里写着 updated_at 字面量 (与死指标 docstring 同族假红)
    code_only = "\n".join(l for l in e2_block.splitlines() if not l.strip().startswith("#"))
    assert "updated_at" not in code_only, (
        "E2 又用 updated_at 判龄 — 2026-10-09 实测 chase_cap 每轮重定价刷新该列, "
        "ARM 挂起 2h 而龄恒 ~2min ⇒ 判据对最典型挂单永不触发")


def test_trading_day_gate_by_stdout():
    """假日判据契约 (test_trading_day_check_contract 同款, 此处防改写时误回退)."""
    assert '"NON_TRADING_DAY" in' in SRC
    assert "returncode != 0" not in SRC


# ══════════════════════ B. 功能级沙盒实跑 ══════════════════════

def _sandbox(tmp_path: Path, svc: str = "active") -> Path:
    """常量替换出沙盒副本: 不触生产 DB/state, 时钟与周末门测试无关化."""
    src = SRC
    repl = {
        "'/opt/ftseek/FTSeek-US/data/ftseek_us.db'": f"'{tmp_path}/ftseek_us.db'",
        "'/opt/ftseek/FTSeek-US/data/playbook_exits.db'": f"'{tmp_path}/playbook_exits.db'",
        "'/opt/ftseek/FTSeek-US/logs/ftseek_us.log'": f"'{tmp_path}/ftseek_us.log'",
        "'/opt/ftseek/FTSeek-US/venv/bin/python3'": f"'{tmp_path}/no_python'",
        "'/opt/ftseek/FTSeek-US/scripts/trading_day_check.py'": f"'{tmp_path}/no_tdc.py'",
        "'/opt/ftseek/FTSeek-US/data/.intraday_diagnose_events.json'": f"'{tmp_path}/state.json'",
        "['systemctl', 'is-active', 'ftseek-us']": "['echo', '%s']" % svc,
        "if weekday >= 5:": "if False:  # 沙盒: 时钟无关化",
    }
    for old, new in repl.items():
        assert old in src, f"沙盒替换锚点丢失: {old} — 生产脚本结构变了, 同步更新本测试"
        src = src.replace(old, new)
    p = tmp_path / "carrier.py"
    p.write_text(src, encoding="utf-8")
    (tmp_path / "ftseek_us.log").write_text("", encoding="utf-8")
    return p


def _run(carrier: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(carrier)],
                          capture_output=True, text=True, timeout=60)


def _mk_main_db(tmp_path: Path, today: str):
    db = sqlite3.connect(tmp_path / "ftseek_us.db")
    db.executescript("""
    CREATE TABLE playbook_instructions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, symbol TEXT, direction TEXT, confidence INTEGER, entry_price REAL,
        stop_loss REAL, take_profit REAL, position_size INTEGER, decision_id TEXT,
        triggered INTEGER DEFAULT 0, executed INTEGER DEFAULT 0,
        skip_reason TEXT, execution_state TEXT);
    CREATE TABLE capital_state (
        current_equity REAL, drawdown_pct REAL, water_level TEXT,
        is_locked INTEGER DEFAULT 0, lock_reason TEXT);
    CREATE TABLE trade_records (
        symbol TEXT, direction TEXT, quantity REAL, entry_price REAL,
        source_type TEXT, exit_price REAL, exit_time TEXT);
    """)
    db.execute("INSERT INTO playbook_instructions "
               "(date,symbol,direction,confidence,entry_price,stop_loss,take_profit,"
               "position_size,decision_id,triggered,executed) VALUES (?,?,?,?,?,?,?,?,?,0,0)",
               (today, "US.TEST1", "BUY", 70, 100.0, 95.0, 110.0, 10, "DA-x"))
    db.execute("INSERT INTO capital_state VALUES (100000, -0.001, 'green', 0, NULL)")
    db.commit()
    db.close()


def _mk_exit_db(tmp_path: Path):
    db = sqlite3.connect(tmp_path / "playbook_exits.db")
    db.execute("CREATE TABLE exit_intents (intent_id TEXT, symbol TEXT, state TEXT, "
               "last_error TEXT, updated_at REAL)")
    db.commit()
    db.close()


@pytest.fixture()
def sbx(tmp_path):
    from datetime import datetime, timezone, timedelta
    today = (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")
    _mk_main_db(tmp_path, today)
    _mk_exit_db(tmp_path)
    carrier = _sandbox(tmp_path)
    return carrier, tmp_path, today


def test_round1_plan_then_silence(sbx):
    """首轮推计划快照; 无变化次轮空 stdout = SILENT."""
    carrier, tmp_path, today = sbx
    r1 = _run(carrier)
    assert r1.returncode == 0
    assert "交易日计划" in r1.stdout and "US.TEST1" in r1.stdout
    assert "水位 green" in r1.stdout
    r2 = _run(carrier)
    assert r2.returncode == 0 and r2.stdout.strip() == "", (
        f"无事件轮未静默: {r2.stdout!r}")


def test_e2_pending_fires_once_after_aging(sbx):
    """触发未执行: 首见轮只播种 (避开正常执行延迟), 持续 >10min 才报, 且仅一次."""
    carrier, tmp_path, today = sbx
    _run(carrier)  # round1 seeds state
    db = sqlite3.connect(tmp_path / "ftseek_us.db")
    db.execute("UPDATE playbook_instructions SET triggered=1, skip_reason='chase_cap_enforced(reprice=1,cap=2)' "
               "WHERE symbol='US.TEST1'")
    db.commit(); db.close()
    r2 = _run(carrier)
    assert "⛔" not in r2.stdout, "首见轮不应报 (10min 龄未满足)"
    # 回拨 state 计时 11min
    sp = tmp_path / "state.json"
    s = json.loads(sp.read_text())
    s["trig_pending_first_seen"] = {"US.TEST1": time.time() - 660}
    sp.write_text(json.dumps(s))
    r3 = _run(carrier)
    assert "⛔ US.TEST1" in r3.stdout and "chase_cap_enforced" in r3.stdout
    r4 = _run(carrier)
    assert r4.stdout.strip() == "", "同一 (symbol,原因) 应去重, 只报一次"


def test_e4_stuck_intent_fires_once(sbx):
    carrier, tmp_path, today = sbx
    _run(carrier)
    db = sqlite3.connect(tmp_path / "playbook_exits.db")
    db.execute("INSERT INTO exit_intents VALUES ('intent-1','US.X','PENDING','exit_order_pending',?)",
               (time.time() - 400,))
    db.commit(); db.close()
    r2 = _run(carrier)
    assert "🚨 退出卡单: US.X" in r2.stdout
    r3 = _run(carrier)
    assert r3.stdout.strip() == "", "同一 intent 应去重"
    # 新鲜 intent (<5min) 不报
    db = sqlite3.connect(tmp_path / "playbook_exits.db")
    db.execute("INSERT INTO exit_intents VALUES ('intent-2','US.Y','HELD',NULL,?)", (time.time(),))
    db.commit(); db.close()
    r4 = _run(carrier)
    assert r4.stdout.strip() == "", "宽限期内 (<5min) 不应报卡单"


def test_e5_error_spike_threshold(sbx):
    carrier, tmp_path, today = sbx
    _run(carrier)  # round1 播种 err_offset (不回放历史)
    log = tmp_path / "ftseek_us.log"
    # +2 条 ERROR: 低于阈值 3, 静默
    with log.open("a", encoding="utf-8") as f:
        for i in range(2):
            f.write(f"2026-10-09 10:0{i}:00,000 [X] [a.b] ERROR: boom {i}\n")
    r2 = _run(carrier)
    assert r2.stdout.strip() == "", "2 条 ERROR < 阈值 3 应静默 (细粒度告警在 error_alert 钉钉侧)"
    # 再 +2 条 (本轮增量 2)? 不 — offset 已推进; 一次写 3 条才触发
    with log.open("a", encoding="utf-8") as f:
        for i in range(3):
            f.write(f"2026-10-09 10:1{i}:00,000 [X] [a.b] ERROR: same boom\n")
    r3 = _run(carrier)
    assert "ERROR +3" in r3.stdout and "same boom ×3" in r3.stdout
    r4 = _run(carrier)
    assert r4.stdout.strip() == "", "offset 已推进, 无新增应静默"


def test_e5_critical_always_fires(sbx):
    carrier, tmp_path, today = sbx
    _run(carrier)
    log = tmp_path / "ftseek_us.log"
    with log.open("a", encoding="utf-8") as f:
        f.write("2026-10-09 10:20:00,000 [X] [a.b] CRITICAL: meltdown\n")
    r2 = _run(carrier)
    assert "CRITICAL +1" in r2.stdout


def test_e3_water_level_change(sbx):
    carrier, tmp_path, today = sbx
    _run(carrier)
    db = sqlite3.connect(tmp_path / "ftseek_us.db")
    db.execute("INSERT INTO capital_state VALUES (97000, -0.03, 'yellow', 0, NULL)")
    db.commit(); db.close()
    r2 = _run(carrier)
    assert "🟡 水位变化: green → yellow" in r2.stdout
    r3 = _run(carrier)
    assert r3.stdout.strip() == "", "同一水位状态应去重"


def test_e6_new_instruction(sbx):
    carrier, tmp_path, today = sbx
    _run(carrier)
    db = sqlite3.connect(tmp_path / "ftseek_us.db")
    db.execute("INSERT INTO playbook_instructions "
               "(date,symbol,direction,confidence,entry_price,stop_loss,take_profit,"
               "position_size,decision_id,triggered,executed) VALUES (?,?,?,?,?,?,?,?,?,0,1)",
               (today, "US.TEST2", "SELL_SHORT", 70, 50.0, 52.0, 46.0, 20, "intraday_reentry-1"))
    db.commit(); db.close()
    r2 = _run(carrier)
    assert "➕ 新增指令: US.TEST2 SELL_SHORT" in r2.stdout and "[再入场]" in r2.stdout
    r3 = _run(carrier)
    assert "➕" not in r3.stdout


def test_e1_service_down_repeats(tmp_path):
    """独立沙盒 (svc=inactive), 不复用 sbx — 同 tmp_path 双建库会 already exists."""
    from datetime import datetime, timezone, timedelta
    today = (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")
    _mk_main_db(tmp_path, today)
    _mk_exit_db(tmp_path)
    carrier = _sandbox(tmp_path, svc="inactive")
    r1 = _run(carrier)
    assert "🔴 ftseek-us 服务未运行!" in r1.stdout
    r2 = _run(carrier)
    assert "🔴 ftseek-us 服务未运行!" in r2.stdout, "宕机告警必须每轮重复 (不去重)"


def test_corrupt_state_fail_open(sbx):
    """损坏 state ⇒ 不崩 (rc=0) 且按首轮语义照发 — 绝不因自身故障吞投递."""
    carrier, tmp_path, today = sbx
    (tmp_path / "state.json").write_text("{corrupt!!", encoding="utf-8")
    r = _run(carrier)
    assert r.returncode == 0
    assert "交易日计划" in r.stdout, "损坏 state 应 fail-open 到首轮快照而非静默"


def test_missing_exit_db_warns_once(sbx, tmp_path):
    """退出账本缺失本身是告警 (退出链未产生 intent 或旧版在跑), 且只报一次."""
    os.remove(tmp_path / "playbook_exits.db")
    r1 = _run(sbx[0])
    assert "退出账本 playbook_exits.db 不存在" in r1.stdout
    r2 = _run(sbx[0])
    assert "退出账本" not in r2.stdout
