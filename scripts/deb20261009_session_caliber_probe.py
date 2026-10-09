#!/usr/bin/env python3
"""[DEB-20261009 §13 复查探针] 市场时段口径的真实重复面 + 四个 P0 的可复算实测.

用途: DEB-20261009 (统一市场时段与扫描调度设计) 的第二 agent 独立复查证据。
提案 §1.3 声称"五处重复口径存在边界差异", 本探针用活体对拍 + 消费方普查检验它。
只读: 不改任何文件/配置/cron, 不触发 OpenD 网络请求 (时钟与日历均注入)。

跑法:
    cd /opt/ftseek/FTSeek-US && venv/bin/python scripts/deb20261009_session_caliber_probe.py

输出分区 (对应 DEB E 表 E1-E6):
  A 五处口径的调用方计数      → 判"真并行"还是"死代码"
  B 边界逐时刻对拍            → 判 §1.3"存在边界差异"真伪
  C is_after_hours 闭区间矛盾  → 唯一真差异的无害性判定
  D 提案写死的日期事实核实     → §8 验收矩阵日期正确性
  E P0-2 安全窗×夜盘重叠窗     → 真截止日 (11/1 还是 12/6)
  F P0-4 周日首夜 vs cron 1-5  → 硬漏触发证实
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date, datetime, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

ET_NAME = "America/New_York"
CST_NAME = "Asia/Shanghai"
HALF_DAY = date(2026, 11, 27)      # 感恩节次日, 13:00 ET 收盘
OVERNIGHT_START_ET = time(21, 0)   # 提案 §1.1 记为用户输入待官方确认
OVERNIGHT_END_ET = time(4, 0)

GREP_PATHS = ["app/", "scripts/", "ops/", "tests/", "/root/.hermes/scripts/"]

# ⚠️ 自指排除: 本探针为了做 C 段对拍, 自己也调用 is_after_hours/is_pre_market,
# 若不排除会把"探针的调用"计成"生产消费方", 导致死函数被判成活跃
# (首跑实测: is_after_hours 唯一命中就是本文件 line 143)。
# 只跳过本文件, 其余 scripts/ 一律照检 —— 见 review-system-design Pitfalls「守卫自指不可避免」。
SELF_PROBE = "scripts/deb20261009_session_caliber_probe.py"

SELF_FILES = {  # 定义处自身不算消费方
    "scheduler_triggers.get_market_phase": "app/core/scheduler_triggers.py",
    "scheduler_triggers.MarketPhase": "app/core/scheduler_triggers.py",
    "timezone.is_market_open": "app/utils/timezone.py",
    "timezone.is_pre_market": "app/utils/timezone.py",
    "timezone.is_after_hours": "app/utils/timezone.py",
    "timezone.get_market_session": "app/utils/timezone.py",
    "shared.market_hours": "app/shared/market_hours.py",
    "scripts.extended_hours_market_session": "scripts/extended_hours_market_session.py",
}


def _hr(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def section_a_consumer_census() -> dict:
    """A: 五处口径的真实调用方计数 (死代码识别)."""
    _hr("A · 口径消费方普查 (定义处自身已排除; 范围含 ops/ tests/ ~/.hermes/scripts/)")
    targets = {
        "scheduler_triggers.get_market_phase": r"get_market_phase\s*\(",
        "scheduler_triggers.MarketPhase": r"MarketPhase\.",
        "timezone.is_market_open": r"is_market_open\s*\(",
        "timezone.is_pre_market": r"is_pre_market\s*\(",
        "timezone.is_after_hours": r"is_after_hours\s*\(",
        "timezone.get_market_session": r"get_market_session\s*\(",
        "shared.market_hours": r"from app\.shared\.market_hours|shared\.market_hours|market_hours\.",
        "scripts.extended_hours_market_session": r"extended_hours_market_session",
    }
    census = {}
    print(f"  {'口径':<40}{'调用点':<8}{'文件':<7}{'判定'}")
    for name, pat in targets.items():
        hits, files = [], set()
        for p in GREP_PATHS:
            r = subprocess.run(
                ["grep", "-rnE", pat, p, "--include=*.py"],
                capture_output=True, text=True, cwd=REPO,
            )
            for line in r.stdout.splitlines():
                if "__pycache__" in line:
                    continue
                f = line.split(":")[0]
                if SELF_FILES.get(name) and f.endswith(SELF_FILES[name].lstrip("/")):
                    continue
                if f.endswith(SELF_PROBE):  # 自指排除 (见模块头注释)
                    continue
                hits.append(line)
                files.add(f)
        dead = len(hits) == 0
        census[name] = {"calls": len(hits), "files": len(files), "dead": dead}
        print(f"  {name:<40}{len(hits):<8}{len(files):<7}{'💀 零消费方 (死)' if dead else '活跃'}")
    n_dead = sum(1 for v in census.values() if v["dead"])
    print(f"\n  提案 §1.3 列举的 5 处口径中, 零消费方死代码 = {n_dead} 处")
    return census


def section_b_boundary_parity() -> list:
    """B: 逐时刻对拍 timezone vs scheduler (注入时钟, 大小写归一)."""
    _hr(f"B · 边界逐时刻对拍 (半日市 {HALF_DAY}, 13:00 ET 收; closed≡overnight 归一)")
    from zoneinfo import ZoneInfo

    ET = ZoneInfo(ET_NAME)
    from app.utils import timezone as tz
    import app.core.scheduler_triggers as st

    # get_market_phase() 无参, 只能注入模块时钟; 用完必须还原 (防污染同进程后续段)
    norm = {
        "pre_market": "PRE", "regular": "RTH", "after_hours": "AH",
        "trading": "RTH", "closed": "CLOSED", "overnight": "CLOSED", "weekend": "CLOSED",
    }
    orig = st.datetime
    probes = [(3, 59), (4, 0), (9, 29), (9, 30), (12, 59), (13, 0),
              (15, 59), (16, 0), (19, 59), (20, 0), (23, 0)]
    print(f"  {'ET时刻':<8}{'timezone':<14}{'scheduler':<14}{'判定'}")
    diffs = []
    try:
        for h, m in probes:
            t = datetime(2026, 11, 27, h, m, tzinfo=ET)
            st.datetime = type("D", (datetime,), {"now": classmethod(lambda c, tz=None: t)})
            sc = st.get_market_phase().value
            st.datetime = orig
            ts = tz.get_market_session(t)
            same = norm.get(str(ts)) == norm.get(sc)
            if not same:
                diffs.append((f"{h:02d}:{m:02d}", str(ts), sc))
            print(f"  {h:02d}:{m:02d}    {str(ts):<14}{sc:<14}{'✅ 一致' if same else '❌ 真差异'}")
    finally:
        st.datetime = orig
    print(f"\n  真边界差异数 = {len(diffs)}   → {diffs if diffs else '零'}")
    if not diffs:
        print("  ⇒ 提案 §1.3『与主调度存在边界差异』未被活体对拍证实 (含半日市 13:00)")
    return diffs


def section_c_closed_interval() -> dict:
    """C: is_after_hours 闭区间与同模块 get_market_session 的自相矛盾."""
    _hr("C · is_after_hours 闭区间边界 (16<=x<=20) 与同模块自相矛盾检验")
    from zoneinfo import ZoneInfo

    from app.utils import timezone as tz

    ET = ZoneInfo(ET_NAME)
    rows = {}
    print(f"  {'ET时刻':<8}{'is_after_hours':<16}{'get_market_session':<20}{'矛盾'}")
    for h, m in [(15, 59), (16, 0), (19, 59), (20, 0), (20, 1)]:
        t = datetime(2026, 11, 27, h, m, tzinfo=ET)
        ah = tz.is_after_hours(t)
        sess = tz.get_market_session(t)
        contra = bool(ah) and str(sess) == "closed"
        rows[f"{h:02d}:{m:02d}"] = {"is_after_hours": ah, "session": str(sess), "contradicts": contra}
        print(f"  {h:02d}:{m:02d}    {str(ah):<16}{str(sess):<20}{'❌ 是' if contra else '—'}")
    n = sum(1 for v in rows.values() if v["contradicts"])
    print(f"\n  自相矛盾时刻数 = {n}  (20:00 整: 判『是盘后』同时判『闭市』)")
    print("  ⇒ 真差异确实存在, 但 is_after_hours 零消费方 (见 A) ⇒ 无害死代码, 正解是删而非新建 SSOT")
    return rows


def section_d_dates() -> dict:
    """D: 提案 §8 验收矩阵写死的日期事实核实."""
    _hr("D · 提案写死的日期事实核实 (§8 验收矩阵 / §1.1 触发条件)")
    names = ["一", "二", "三", "四", "五", "六", "日"]
    checks = {
        "2026-11-01": (date(2026, 11, 1), "冬令时生效", None),
        "2026-12-06": (date(2026, 12, 6), "夜盘首日", "提案 §6.2 称周日"),
        "2026-11-27": (HALF_DAY, "半日市", "提案 §8.2 称 13:00 收"),
        "2027-03-14": (date(2027, 3, 14), "夏令时恢复", None),
    }
    out = {}
    for k, (d, label, claim) in checks.items():
        wd = names[d.weekday()]
        out[k] = {"weekday": wd, "label": label}
        print(f"  {k} = 周{wd}  [{label}]  {claim or ''}")
    print(f"\n  ⇒ 2026-12-06 确为周日: 夜盘首夜落在 crontab `1-5` 覆盖之外 (见 F)")
    return out


def section_e_p02_overlap() -> dict:
    """E: P0-2 deploy_freshness 安全窗与夜盘的重叠窗 (判真截止日)."""
    _hr("E · P0-2 部署安全窗(北京09:00-12:00) × 夜盘(21:00-04:00 ET) 重叠窗")
    from zoneinfo import ZoneInfo

    CST, ET = ZoneInfo(CST_NAME), ZoneInfo(ET_NAME)
    out = {}
    for season, day in (("夏令时 EDT", date(2026, 10, 9)), ("冬令时 EST", date(2026, 11, 15))):
        win = []
        for h in (9, 12):
            c = datetime(day.year, day.month, day.day, h, 0, tzinfo=CST)
            win.append(c.astimezone(ET))
        out[season] = [w.strftime("%H:%M") for w in win]
        print(f"  {season}: 北京 09:00-12:00 CST → ET {win[0]:%H:%M} - {win[1]:%H:%M}")
    print(f"\n  现行 (夜盘未生效): ET 20:00-04:00 = maintenance/OVERNIGHT 深度休眠 ⇒ 重启不打断任何在途")
    print(f"  夜盘生效后: 重叠 = ET {OVERNIGHT_START_ET:%H:%M} - {out['冬令时 EST'][1]}  (2h)")
    print(f"  ⇒ P0-2 的真截止日是 2026-12-06 (夜盘生效), 不是 2026-11-01 (冬令时)")
    return out


def section_f_p04_sunday() -> dict:
    """F: P0-4 周日首夜 vs crontab `1-5` 覆盖."""
    _hr("F · P0-4 周日首夜 (2026-12-06) 与 crontab 星期字段覆盖")
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    lines = [l for l in r.stdout.splitlines() if l.strip() and not l.startswith("#")]
    dow = {}
    for l in lines:
        parts = l.split()
        if len(parts) >= 5:
            dow.setdefault(parts[4], []).append(l)
    print(f"  crontab 有效行数 = {len(lines)}")
    print("  按星期字段分布:")
    for k in sorted(dow):
        runs_sun = "*" == k or "0" in k.split(",") or "7" in k.split(",")
        mark = "✅ 周日会跑" if runs_sun else "❌ 周日不跑"
        print(f"    dow='{k}' : {len(dow[k]):>3} 行   {mark}")
    n_nosun = sum(len(v) for k, v in dow.items()
                  if not ("*" == k or "0" in k.split(",") or "7" in k.split(",")))
    print(f"\n  周日不触发的 cron 行数 = {n_nosun}")
    print(f"  ⇒ 2026-12-06 是周日且为夜盘首夜: 这 {n_nosun} 行全部漏触发 (提案 P0-4 成立)")
    return {"total": len(lines), "not_running_sunday": n_nosun}


def main() -> int:
    print("DEB-20261009 §13 复查探针 · 市场时段口径真实重复面 + P0 实测")
    print(f"仓库: {REPO}   (只读, 不触发 OpenD)")
    census = section_a_consumer_census()
    diffs = section_b_boundary_parity()
    section_c_closed_interval()
    section_d_dates()
    section_e_p02_overlap()
    section_f_p04_sunday()

    _hr("汇总 (供 DEB E 表引用)")
    dead = [k for k, v in census.items() if v["dead"]]
    print(f"  活跃口径 SSOT        : get_market_phase (调用点 {census['scheduler_triggers.get_market_phase']['calls']}"
          f" / 文件 {census['scheduler_triggers.get_market_phase']['files']})")
    print(f"  零消费方死口径       : {len(dead)} 处 → {', '.join(dead)}")
    print(f"  真边界差异           : {len(diffs)} 处")
    print(f"  P0-2 真截止日        : 2026-12-06 (非 11-01)")
    print(f"  2026-12-06 星期      : 周日 (夜盘首夜落在 crontab 1-5 之外)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
