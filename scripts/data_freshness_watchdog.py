#!/usr/bin/env python3
"""数据新鲜度看门狗 (2026-07-23)。

回答: 那些"本应持续写入"的数据流, 是否有谁静默停更了 ——
      LLM 审计库空转两个月无人知 / shadow 墓地(4 个停更无人察觉)的根因,
      都是"降级不报警"。本脚本对关键数据源做 last_write_age 检查, 超期即飞书告警。

设计:
  - 纯只读 stat mtime (或 jsonl 末行时间), 零 live 行为变更。
  - 每个源配 max_age_hours + 仅工作日选项 (盘后才更新的源, 周末不告警)。
  - 失败安全: 单源异常不影响其他源; 推送异常吞掉。
  - 自我兜底: 本脚本也在监控 llm_audit.db, 形成"观测的观测"。

2026-08-28 主动暴露机制 (D1/D2, 回答"为什么尺子问题总是顺手才发现"):
  上方 WATCHLIST 是**人工登记**的 —— 登记不到的源死了就永远无人知
  (plan_feedback 三条输入死 3 周 / daemon_heartbeat 空表 4 个月实锤)。
  D1/D2 改为**自动枚举**, 把"清单外"这个结构性盲区关掉:
  D1 表级载体活性: 从 sqlite_master 枚举全部表 × 仓内 SQL 写入方普查,
     告警"有写入方却零行 / 长期停更"的载体 (生产者静默死亡)。
  D2 调度应跑未跑: 从 Hermes jobs.json 的 cron 表达式反推最近应跑槽位,
     与 executions.db 实跑记录对账 (连续 ≥2 槽缺席才告, 未到期不算)。
  两者都用基线棘轮抑制既有墓地噪音: 新出现的键才告警, 已知项静默计数。
  冻结基线: --update-liveness-baseline (只许减少不许新增, 同 config_key_census)。
运行: PYTHONPATH=. ./venv/bin/python scripts/data_freshness_watchdog.py [--push] [--dry]
      [--update-liveness-baseline]
"""
import argparse
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / ".env.us")
except Exception:
    pass

# ── 监控清单: (相对路径, 最大静默小时, 说明, 仅工作日) ──
#   max_age_hours: 超过此时长未更新 → 告警
#   weekday_only: True 时周末(周六日)不告警 (盘后才写的源)
WATCHLIST = [
    ("data/llm_audit.db",                          30, "LLM 审计库 (成本观测地基)",        False),
    ("data/opportunity_scan_{today}.json",         30, "DA 指令生成 bridge",               True),
    ("data/factor_ic_latest.json",                 50, "因子 IC 回填 (Tue-Sat cron, 周一 gap 49h 为计划内)", True),
    ("data/shadow/llm_universe_scan.jsonl",        30, "UE 宇宙扫描 shadow",               True),
    ("data/shadow/premarket_peer_shadow.jsonl",    30, "盘前 peer shadow",                 True),
    ("data/shadow/divergence_arbitration_shadow.jsonl", 48, "分歧仲裁 shadow",             True),
    ("data/shadow/exec_gate_audit.jsonl",          30, "执行闸门审计",                     True),
    ("data/shadow/sltp_verify_shadow.jsonl",       30, "止盈止损校验 shadow",             True),
    # 2026-08-26 复查加固: U11 确认门兜底告警 (主防线=双季cron+当日幂等+窗口守卫日志)。
    #   74h 实测口径 (8/26 独立复核修正原"假日不误报"注释, 证伪见 DEB-20260825 复核段):
    #   美国假日后第一个交易日 06:25 检查必误报一次 (周一假日→周二 80.75h; 连休两天→104.75h),
    #   当晚 confirm 跑后自恢复, 零数据污染 — 误报本身证明链路活着, 接受为已知噪音。
    #   代价: 真静默死亡最坏 ~5 天检出 — 可接受, 本条只是兜底中的兜底。
    ("data/shadow/ue_confirm_gate.jsonl",         74, "UE 开盘确认门 shadow (U11)",       True),
    # 已知墓地 (DEB-20260723 time-exit-censoring 实证6): 若已裁决停用应从清单移除, 否则持续告警提醒清理
    # 2026-08-26: prompt_eval + shadow_technical_direction 已归档至
    #   data/shadow/archive_20260826/ (上游停产实锤, 全影子普查裁决), 条目移除。
    ("data/shadow/playbook_direction_audit.jsonl", 72, "playbook 方向审计 (活跃, 8/26普查4消费方)", True),
    ("data/shadow/edge_gate_shadow.jsonl",         72, "[疑墓地] edge gate shadow",        True),
    # 2026-07-26 减法R2-Q6 (裁决: 不建新注册表, 高危文件入现有 watchdog): "生产者死了消费者不知道"
    #   price_fail_closed (责任 DEB-20260726 Q2, 已清空保留机制)。
    #   [2026-08-23] SHORT 门控两件 (hard_to_borrow/short_interest.json) 已随死门删除移出清单 —
    #   用户裁"不值得" (88天空文件静默 fail-open 实案, 责任同 R2 Q1), 消费门已删, 文件不再需要。
    ("data/price_fail_closed.json",               999, "[人工名单] 期权跳过集 (仅人工维护, 静止正常)", False),
]


def _now_et():
    """[v0.70 审计修复 2026-08-26] {today} 占位符必须按 ET 日期解析.

    主机是 CST: 06:25 CST 运行时 ET 还是前一天 (17:25 ET), bridge 文件由生产端
    按 ET 日期命名 → 用 CST 日期会找"今天"的文件 (19:00CST 后才产出) → 每天必报
    一次假 MISS (实测 8/26 06:25 告警实锤, 当时昨日文件仅 11h 新鲜).
    """
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        try:
            import pytz
            return datetime.now(pytz.timezone("America/New_York"))
        except Exception:
            return datetime.now()  # 最后兜底: 维持旧行为 (已知有每日假MISS)


def _resolve(rel: str) -> Path:
    today = _now_et().strftime("%Y-%m-%d")
    return ROOT / rel.format(today=today)


def _age_hours(path: Path) -> float:
    return (time.time() - path.stat().st_mtime) / 3600.0


def _is_weekend() -> bool:
    return datetime.now().weekday() >= 5  # 5=周六 6=周日


# ══════════════════════════════════════════════════════════════════════
# D1 表级载体活性 (自动枚举, 无需人工登记)
#   动机: daemon_heartbeat 空表 4 个月 / earnings_calendar 永久零行 (财报 ATR 上限
#   门从未生效) 都是"载体建好、写入方存在、但数据从未流进来" — 人工清单永远登记不到。
#   判据: 表在 sqlite_master 里 + 仓内有 INSERT/UPDATE/REPLACE 写入方 ⇒ 该表本应有数据。
#     · 零行           → 生产者从未喂数
#     · MAX(时间列) 超期 → 生产者某刻静默死亡
#   无写入方的表不告 (可能由外部/历史写入, 属人工裁决面, 只在 JSON 里列观测)。
# ══════════════════════════════════════════════════════════════════════

TABLE_STALE_DAYS = 21  # 周级环路留双倍余量 (周日复盘/每周 ADF 均 ≤7 天)
_WRITE_SQL_RE = re.compile(
    r"(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO|UPDATE)\s+[`\"\[]?([A-Za-z_]\w*)",
    re.I,
)
# 时间列优选顺序 (同名多列时取先出现的; 均为各表实际写入语义里的"发生时刻")
_TIME_COL_HINTS = (
    "trade_date", "exit_time", "entry_time", "bar_date", "check_date", "executed_at",
    "finished_at", "updated_at", "created_at", "timestamp", "claimed_at", "submit_time",
    "snapshot_time", "date", "dt", "ts", "day", "week",
)

LIVENESS_BASELINE = DATA / "watchdog_liveness_baseline.json"
HERMES_JOBS_JSON = Path("/root/.hermes/cron/jobs.json")
HERMES_EXEC_DB = Path("/root/.hermes/cron/executions.db")
HERMES_MISS_SLOTS = 2  # 连续缺席槽位数 (1 槽缺席多为认领延迟/边界, 不打扰)


def _load_liveness_baseline() -> dict:
    try:
        return json.loads(LIVENESS_BASELINE.read_text()).get("keys", {})
    except Exception:
        return {}


def save_liveness_baseline() -> dict:
    """冻结当前全部 D1/D2 发现为已知基线 (棘轮起点; 此后只新增才告警)。"""
    keys = {}
    for f in _check_table_liveness({})[0] + _check_hermes_jobs({})[0]:
        keys[f["key"]] = f["detail"]
    LIVENESS_BASELINE.write_text(json.dumps(
        {"frozen_at": datetime.now().isoformat(timespec="seconds"),
         "rule": "棘轮: 基线内的发现静默 (既有墓地/人工裁决面), 新键才告警; 只许减少不许新增",
         "keys": keys},
        ensure_ascii=False, indent=1))
    return keys


def _main_db() -> Path:
    """权威库按 TRADE_ENV 解析 (硬编码文件名会在切实盘那刻静默变成监控陈旧同名单)。"""
    for mod, fn in (("app.core.paths", "get_trade_records_db"),
                    ("app.core.db_utils", "get_main_db")):
        try:
            import importlib
            return Path(str(getattr(importlib.import_module(mod), fn)()))
        except Exception:
            continue
    return DATA / "ftseek_us.db"


def _repo_write_targets() -> dict:
    """表名 → 仓内写入方文件集合 (排除 archive/tests, 只看现役代码)。"""
    out = {}
    files = list((ROOT / "app").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py"))
    for p in files:
        sp = str(p)
        if "/archive/" in sp or "/tests/" in sp:
            continue
        try:
            txt = p.read_text(errors="ignore")
        except Exception:
            continue
        for m in _WRITE_SQL_RE.finditer(txt):
            out.setdefault(m.group(1).lower(), set()).add(p.name)
    return out


def _pick_time_col(cols):
    lows = {str(c).lower(): c for c in cols}
    for h in _TIME_COL_HINTS:
        if h in lows:
            return lows[h]
    for c in cols:
        if str(c).lower().endswith(("_date", "_time", "_at", "_ts")):
            return c
    return None


def _age_days(raw) -> int:
    s = str(raw).strip()
    for f in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f",
              "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return (datetime.now() - datetime.strptime(s[:26], f)).days
        except Exception:
            continue
    try:  # epoch 秒 (idempotency_keys.created_at 等)
        return (datetime.now() - datetime.fromtimestamp(float(s))).days
    except Exception:
        return -1  # 不可解析 → 不判 (宁漏不误)


def _check_table_liveness(baseline: dict):
    """返回 (待告警发现, 基线内静默数, 全量观测表)。任何异常 → ([], 0, [])。"""
    findings, known = [], 0
    observed = []
    try:
        import sqlite3
        db = _main_db()
        if not db.exists():
            return [{"key": "D1:main_db_missing", "kind": "D1",
                     "detail": f"权威库不存在: {db}",
                     "action": "监控面整体失明 (TRADE_ENV/路径异常)"}], 0, []
        writers = _repo_write_targets()
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            for t in tables:
                cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
                try:
                    n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                except Exception:
                    continue
                w = sorted(writers.get(t.lower(), ()))
                col = _pick_time_col(cols) if n else None
                age = -1
                if col:
                    try:
                        mx = conn.execute(f'SELECT MAX("{col}") FROM "{t}"').fetchone()[0]
                    except Exception:
                        mx = None
                    age = _age_days(mx) if mx is not None else -1
                obs = {"table": t, "rows": n, "time_col": col, "age_days": age,
                       "writers": len(w)}
                observed.append(obs)
                if not w:
                    continue  # 无现役写入方 → 人工裁决面, 不自动告
                if n == 0:
                    kind, detail = "never_fed", f"{len(w)} 个写入方 ({','.join(w[:3])}) 零行落库"
                    act = "生产者从未喂数 → 读该表的门/报表恒空"
                elif age > TABLE_STALE_DAYS:
                    kind, detail = "stalled", f"{col} 停更 {age}天 (阈值 {TABLE_STALE_DAYS})"
                    act = "生产者静默死亡 → 下游读陈旧快照"
                else:
                    continue
                key = f"D1:{t}:{kind}"
                if key in baseline:
                    known += 1
                else:
                    findings.append({"key": key, "kind": "D1", "detail": detail, "action": act})
        finally:
            conn.close()
    except Exception as e:
        return ([], known, observed) if baseline.get("D1:detector_error") else [
            {"key": "D1:detector_error", "kind": "D1",
             "detail": f"表级活性普查内部异常: {type(e).__name__}: {e}",
             "action": "D1 检测器自身失明, 需人工修复"}], 0, []
    return findings, known, observed


# ══════════════════════════════════════════════════════════════════════
# D2 调度应跑未跑 (Hermes cron 表达式 → 最近应跑槽位 vs executions.db 实跑)
#   动机: "影子组合每日结算/每周 ADF" 曾挂在已死的会话里从未实跑 —— 只看文件新鲜度
#   看不出"根本没跑"; 只看 jobs.json last_run_at 又分不清"还没到期"与"到期没跑"。
#   判据: 从 cron 表达式反推最近 N 个应跑时刻 (服务器本地时区 = Hermes 调度时区),
#     未到期 (created_at 晚于槽位) 与超出 executions.db 覆盖窗口的槽位一律跳过,
#     连续 ≥HERMES_MISS_SLOTS 槽无实跑记录才告。
# ══════════════════════════════════════════════════════════════════════

def _cron_field(spec, lo, hi) -> set:
    out = set()
    for part in str(spec).split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = max(1, int(s))
        part = part.strip()
        if part in ("*", "?", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
        out.update(range(a, b + 1, step))
    return {v for v in out if lo <= v <= hi}


def _cron_due_slots(expr: str, now: datetime, n: int = 8):
    """回溯最近 n 个应跑时刻 (分钟精度)。表达式非法 → []。"""
    try:
        mi, ho, dm, mo, dw = str(expr).split()
        mins, hrs = _cron_field(mi, 0, 59), _cron_field(ho, 0, 23)
        doms, mons = _cron_field(dm, 1, 31), _cron_field(mo, 1, 12)
        dows = _cron_field(dw, 0, 6)
    except Exception:
        return []
    t = now.replace(second=0, microsecond=0)
    out = []
    for _ in range(60 * 24 * 25):  # 最多回溯 25 天 (周级环路足够)
        t -= timedelta(minutes=1)
        if t.month not in mons or t.day not in doms:
            continue
        if t.hour not in hrs or t.minute not in mins:
            continue
        if (t.weekday() + 1) % 7 not in dows:  # cron: 0=周日
            continue
        out.append(t)
        if len(out) >= n:
            break
    return out


def _aware(ts_str, local_tz):
    """executions.db 的 ISO 串 → aware datetime (naive 按服务器本地时区解释)。"""
    try:
        d = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
    except Exception:
        return None
    return d if d.tzinfo else d.replace(tzinfo=local_tz)


def _check_hermes_jobs(baseline: dict, _now: datetime = None):
    """返回 (待告警发现, 基线内静默数, 已判定作业数)。

    _now 仅供测试注入 (D2 的判定全在"槽位 vs 此刻"上, 不注入就无法跨时辰扫一致性)。
    """
    findings, known, judged = [], 0, 0
    try:
        import sqlite3
        if not HERMES_JOBS_JSON.exists() or not HERMES_EXEC_DB.exists():
            return ([{"key": "D2:hermes_missing", "kind": "D2",
                      "detail": f"Hermes 调度面不可读 ({HERMES_JOBS_JSON.name}/{HERMES_EXEC_DB.name})",
                      "action": "调度对账失明"}]
                     if not baseline.get("D2:hermes_missing") else []), 0, 0
        jobs = json.loads(HERMES_JOBS_JSON.read_text()).get("jobs", [])
        now = _now or datetime.now()  # 服务器本地 = Hermes 调度时区 (CST, 无 DST)
        local_tz = now.astimezone().tzinfo
        conn = sqlite3.connect(f"file:{HERMES_EXEC_DB}?mode=ro", uri=True)
        try:
            last = {r[0]: r[1] for r in conn.execute(
                "SELECT job_id, MAX(claimed_at) FROM executions GROUP BY job_id")}
            # 覆盖窗 = 全库最旧一条实跑记录 (executions 是 1000 行环形缓冲), 不是
            # "各作业最后实跑里最早的那个" — 后者会把刚跑过的作业全部槽位推出窗外,
            # judged 恒 0, 与"检测器失明"不可区分 (2026-08-28 回归测试实锤)。
            floor = conn.execute("SELECT MIN(claimed_at) FROM executions").fetchone()[0]
        finally:
            conn.close()
        floor_dt = _aware(floor, local_tz) if floor else None
        for j in jobs:
            if not j.get("enabled"):
                continue
            expr = (j.get("schedule") or {}).get("expr") or j.get("schedule_display") or ""
            slots = _cron_due_slots(expr, now)
            created = _aware(j.get("created_at"), local_tz)
            le = _aware(last.get(j.get("id")), local_tz) if last.get(j.get("id")) else None
            missed = 0
            slot_seen = False
            for s in slots:
                # 槽位是本地挂钟 naive, created_at/claimed_at 是带 +08:00 的 aware —
                # 直接比较会 TypeError 并被外层 except 吞掉 (2026-08-28 回归测试实锤:
                # 整个 D2 因此恒报"全健康", 检测器自己静默失明)。统一转 aware 再比。
                sa = s.replace(tzinfo=local_tz)
                if created and sa < created:
                    break  # 作业当时尚未创建 → 不属漏跑
                if le and le >= sa - timedelta(minutes=3):
                    slot_seen = True
                    break  # 该槽有实跑 (±3min 认领抖动); 找得到跑记录即正面证据, 不受覆盖窗限制
                if floor_dt and sa < floor_dt:
                    break  # "没找到实跑"且超出覆盖窗 → 无法判定, 保守不计
                slot_seen = True
                missed += 1
            if not slot_seen:
                continue
            judged += 1  # 真正判过了 (有到期且落在覆盖窗内的槽), 与"告警数"分开计
            if missed < HERMES_MISS_SLOTS:
                continue
            name = str(j.get("name") or j.get("id"))[:36]
            detail = (f"连续 {missed} 槽未跑 (最近应跑 {slots[0]:%m-%d %H:%M}, "
                      f"expr={expr}, 实跑至 {le:%m-%d %H:%M} )" if le else
                      f"连续 {missed} 槽未跑 (最近应跑 {slots[0]:%m-%d %H:%M}, expr={expr}, 从无实跑)")
            key = f"D2:{j.get('id')}"
            if key in baseline:
                known += 1
            else:
                findings.append({"key": key, "kind": "D2", "name": name,
                                 "detail": detail, "action": "Hermes 作业静默停摆 → 下游数据/推送全空"})
    except Exception as e:
        # 检测器自身异常必须显性化 (返回 [] 会被读成"全部作业健康" = 二次失明)
        return ([], known, judged) if baseline.get("D2:detector_error") else [
            {"key": "D2:detector_error", "kind": "D2",
             "detail": f"调度对账内部异常: {type(e).__name__}: {e}",
             "action": "D2 检测器自身失明, 需人工修复"}], 0, 0
    return findings, known, judged


def check(dry=False):
    weekend = _is_weekend()
    stale, missing, ok = [], [], []
    for rel, max_age, desc, weekday_only in WATCHLIST:
        if weekday_only and weekend:
            continue
        path = _resolve(rel)
        try:
            if not path.exists():
                missing.append((rel, desc))
                continue
            age = _age_hours(path)
            if age > max_age:
                stale.append((rel, desc, age, max_age))
            else:
                ok.append((rel, age))
        except Exception as e:
            missing.append((rel, f"{desc} (stat异常:{e})"))

    return stale, missing, ok


def _build_message(stale, missing, liveness=None) -> str:
    liveness = liveness or {}
    alarms = liveness.get("alarms", [])
    if not stale and not missing and not alarms:
        return None  # 全新鲜 → 不推送 (无噪音)
    lines = ["🛰️ 数据新鲜度告警"]
    if stale:
        lines.append("── 静默停更 (超期未写入) ──")
        for rel, desc, age, max_age in stale:
            days = age / 24
            age_str = f"{age:.0f}h" if age < 48 else f"{days:.1f}天"
            lines.append(f"⏰ {desc}: 已 {age_str} 未更新 (阈值 {max_age}h)")
    if missing:
        lines.append("── 文件缺失 ──")
        for rel, desc in missing:
            # [v0.70] 显示解析后的真实路径 (原样输出 rel 会把 {today} 占位符直接暴露在告警里)
            try:
                shown = str(_resolve(rel))
            except Exception:
                shown = rel
            lines.append(f"❓ {desc}: {shown}")
    if alarms:
        lines.append("── 活性对账 (D1 表载体 / D2 调度应跑未跑) ──")
        for f in alarms:
            tag = "🫥" if f["kind"] == "D1" else "🕰"
            head = f.get("name") or f["key"].split(":")[-1]
            lines.append(f"{tag} [{f['kind']}] {head}: {f['detail']}")
    return "\n".join(lines)


def collect_liveness() -> dict:
    """D1+D2 一次性采集 (基线内发现静默计数, 新键进告警)。异常已在各检测器内吞掉。"""
    baseline = _load_liveness_baseline()
    t_find, t_known, observed = _check_table_liveness(baseline)
    h_find, h_known, h_judged = _check_hermes_jobs(baseline)
    return {
        "at": datetime.now().isoformat(timespec="seconds"),
        "baseline_keys": len(baseline),
        "alarms": t_find + h_find,
        "silenced_known": t_known + h_known,
        "tables": observed,
        "hermes_jobs_judged": h_judged,
    }


def main(push=False, dry=False, update_baseline=False):
    if update_baseline:
        keys = save_liveness_baseline()
        print(f"[baseline] 已冻结 {len(keys)} 项 D1/D2 发现 → {LIVENESS_BASELINE}")
        for k, v in sorted(keys.items()):
            print(f"  {k}: {v[:70]}")
        return 0

    stale, missing, ok = check(dry=dry)
    live = collect_liveness()
    print("=" * 60)
    print(f"数据新鲜度看门狗 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"正常 {len(ok)} | 停更 {len(stale)} | 缺失 {len(missing)}")
    for rel, desc, age, max_age in stale:
        print(f"  [STALE] {desc}: {age:.0f}h > {max_age}h")
    for rel, desc in missing:
        try:
            _shown = str(_resolve(rel))
        except Exception:
            _shown = rel
        print(f"  [MISS ] {desc}: {_shown}")
    for rel, age in ok:
        print(f"  [ok   ] {rel}: {age:.1f}h")
    print(f"[D1/D2] 表枚举 {len(live['tables'])} 张 | 新告 {len(live['alarms'])} | "
          f"基线内静默 {live['silenced_known']} | 调度已对账 {live['hermes_jobs_judged']} 作业")
    for f in live["alarms"]:
        print(f"  [LIVE ] {f['kind']} {f.get('name') or f['key']}: {f['detail']} → {f['action']}")
    print("=" * 60)
    try:  # 观测落盘 (只读采集, 供人工回看墓地全貌; 失败不影响告警链)
        (DATA / "watchdog_liveness_latest.json").write_text(
            json.dumps(live, ensure_ascii=False, indent=1))
    except Exception:
        pass

    msg = _build_message(stale, missing, live)
    if msg and push and not dry:
        # 2026-08-09 (告警风暴消除): 文件持久化冷却 — 30min 内同内容不重复推.
        #   看门狗是 cron 独立进程, 进程内状态无效; 持久化到 data/watchdog_last_push.json.
        #   若 OpenD/Futu 持续不可用, watchdog 每轮都检到停更 → 无冷却=每轮1条告警风暴.
        _state_file = DATA / "watchdog_last_push.json"
        _now = time.time()
        _suppress = False
        try:
            if _state_file.exists():
                with open(_state_file) as _f:
                    _prev = json.load(_f)
                if _prev.get("msg_hash") == _hash_msg(msg) and _now - _prev.get("ts", 0) < 1800:
                    _suppress = True
        except Exception:
            pass
        if _suppress:
            print("[cooldown] 30min 内同告警已推过, 跳过")
        else:
            try:
                from app.notifier.feishu import send_text
                send_text(msg)
                print("[pushed] 飞书")
                try:
                    with open(_state_file, "w") as _f:
                        json.dump({"ts": _now, "msg_hash": _hash_msg(msg)}, _f)
                except Exception:
                    pass
            except Exception as e:
                print(f"[warn] 推送失败: {e}")
    elif not msg:
        print("[ok] 全部新鲜, 不推送")


def _hash_msg(msg: str) -> str:
    import hashlib
    return hashlib.md5(msg.encode("utf-8")).hexdigest()[:16]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="数据新鲜度看门狗 (只读)")
    ap.add_argument("--push", action="store_true", help="有告警时推送飞书")
    ap.add_argument("--dry", action="store_true", help="只打印不推送")
    ap.add_argument("--update-liveness-baseline", action="store_true",
                    help="冻结当前 D1/D2 发现为已知基线 (棘轮: 此后只新增才告警)")
    a = ap.parse_args()
    raise SystemExit(main(push=a.push, dry=a.dry, update_baseline=a.update_liveness_baseline))
