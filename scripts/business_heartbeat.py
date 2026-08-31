#!/usr/bin/env python3
"""业务心跳 (2026-08-24, 复查A6②): 交易日盘中 playbook 指令数=0 → 告警输出.

为何需要: health_check 只管"进程活着", 管不了"活着但没在交易" — LLM 链路/
信号管线/调度环任何一环静默卡死时, 进程健康检查全绿, 但全天一条指令都不产生,
盘中缺席无交易。本心跳以"业务产出"为信号源补上这一盲区。

判定: 交易日 ET 11:00-15:30 窗口内 (早盘应已产出指令), 当日 playbook_instructions
(direction != 'SKIP', 投票失败占位行不计) 计数为 0 → 打印告警文本 (exit 2);
其余情况静默 exit 0。是否推送/冷却由外层 business_heartbeat.sh 负责。

DST 自护模式 (tripwire 同款): crontab 用宽 CST 窗口 (21-23,0-5), 本脚本用
zoneinfo America/New_York 自判 ET 真实时刻 — 冬令时切换不改 crontab。
注意: 不用 timezone(timedelta(hours=-4)) 硬编码 EDT (部分 hermes 脚本如此,
11 月切冬令时会整体偏移一小时)。

用法:
  venv/bin/python scripts/business_heartbeat.py           # 正常 (cron 调)
  venv/bin/python scripts/business_heartbeat.py --force   # 绕过时间/假日窗验证用
"""
import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parents[1]
DB_PATH = BASE / "data" / "ftseek_us.db"

# 告警窗: 早盘过半仍未产出任何指令即视为异常; 15:30 后临近收盘不再打扰
ALERT_FROM_ET = (11, 0)
ALERT_TO_ET = (15, 30)


# ── 影子覆盖心跳 (2026-08-31, DEB-20260831 P1②, 用户裁"按建议执行") ──────────
# E13 教训: 影子写入从未挂生产链, 覆盖率 42% 静默三周无人知 (结算时才发现)。
# 本检查抓**新增断流**: 最近一个已完结 bridge 扫描日若无 edge_gate_shadow 写入
# → 告警。存量覆盖率 (判据 F2⑤ ≥0.8) 归结算脚本管, 本心跳不重复告已知债。
# 产物: data/shadow_coverage_latest.json (可审计)。
_BRIDGE_GLOB = "opportunity_scan_*.json"
_EDGE_SHADOW = BASE / "data" / "shadow" / "edge_gate_shadow.jsonl"
_COVERAGE_OUT = BASE / "data" / "shadow_coverage_latest.json"


def _shadow_coverage_check(today: str):
    """返回 (告警文本或 None, 观测 dict)。检测器自身异常不伪装全绿。"""
    try:
        bridge_days = sorted(
            p.name[len("opportunity_scan_"):-len(".json")]
            for p in (BASE / "data").glob(_BRIDGE_GLOB))
        bridge_days = [d for d in bridge_days if d < today]  # 只算已完结日
        shadow_dates = set()
        if _EDGE_SHADOW.exists():
            for line in _EDGE_SHADOW.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    shadow_dates.add(str(json.loads(line).get("ts") or "")[:10])
                except Exception:
                    continue
        recent = bridge_days[-10:]
        covered = [d for d in recent if d in shadow_dates]
        coverage = round(len(covered) / len(recent), 3) if recent else None
        latest_bridge = bridge_days[-1] if bridge_days else None
        obs = {"latest_bridge_day": latest_bridge, "recent_bridge_days": recent,
               "covered_days": covered, "coverage_10d": coverage,
               "criterion_ref": "F2⑤ ≥0.8 为结算判据 (settlement_cap_f2.py); 本心跳只抓新增断流"}
        alert = None
        if latest_bridge and latest_bridge not in shadow_dates:
            alert = (f"bridge 扫描日 {latest_bridge} edge_gate_shadow 写入=0 — "
                     f"影子挂载疑似静默死亡 (E13 族, 近10桥日覆盖={coverage})。"
                     f"排查: 写入方挂载点与运行路径 (参考 pipeline :1662 旁路)")
        return alert, obs
    except Exception as e:
        return (f"影子覆盖检查自身异常: {type(e).__name__}: {e} (非全绿, 需人工核)"), {}


def main() -> int:
    force = "--force" in sys.argv
    now_et = datetime.now(ZoneInfo("America/New_York"))
    today = now_et.strftime("%Y-%m-%d")

    if not force:
        if now_et.weekday() >= 5:
            return 0
        # 假日守卫 (与 intraday_diagnose 同款: 交易日历脚本非零退出=休市)
        try:
            r = subprocess.run(
                [sys.executable, str(BASE / "scripts" / "trading_day_check.py"),
                 "--market", "US"],
                capture_output=True, text=True, timeout=15, cwd=str(BASE))
            if r.returncode != 0:
                return 0
        except Exception:
            return 0  # 守卫自身故障 → 静默 (宁漏报不误报, health_check 另有覆盖)
        hm = now_et.hour * 60 + now_et.minute
        lo = ALERT_FROM_ET[0] * 60 + ALERT_FROM_ET[1]
        hi = ALERT_TO_ET[0] * 60 + ALERT_TO_ET[1]
        if not (lo <= hm <= hi):
            return 0

    # 影子覆盖心跳 (与指令心跳同窗运行, 独立判定)
    coverage_alert, coverage_obs = _shadow_coverage_check(today)
    if coverage_obs:
        try:
            coverage_obs["checked_at_et"] = now_et.isoformat(timespec="seconds")
            _COVERAGE_OUT.write_text(json.dumps(coverage_obs, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
        except Exception as _obs_e:
            print(f"⚠️ [业务心跳] 覆盖观测落盘失败: {_obs_e}")

    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN triggered THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN executed THEN 1 ELSE 0 END) "
            "FROM playbook_instructions WHERE date = ? AND direction != 'SKIP'",
            (today,),
        ).fetchone()
        conn.close()
    except Exception as e:
        print(f"🔴 [业务心跳] 指令表查询失败: {e} — 心跳数据源不可达本身即异常")
        return 2

    total, trig, ex = (row[0], row[1] or 0, row[2] or 0)
    if total > 0 and not coverage_alert:
        return 0

    if total == 0:
        print(
            f"🔴 [业务心跳] {today} 盘中指令数 = 0 — 服务可能在'空转缺席'\n"
            f"ET 现在: {now_et.strftime('%H:%M')} | 今日触发={trig} 执行={ex}\n"
            f"排查: systemctl status ftseek-us → logs/ftseek_us.log 近段 ERROR → "
            f"信号管线 (news/DA/scheduler) 是否卡死\n"
            f"注: 全天无合格信号的合法安静日会误报, 人工判断即可"
        )
    if coverage_alert:
        print(f"🔴 [业务心跳·影子覆盖] {coverage_alert}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
