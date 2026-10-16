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
  D3 跑了但失败且未恢复 (2026-09-14 补): D2 只看"槽位上有没有实跑记录", 而一条
     failed 记录同样占位 ⇒ DA 主作业 9/11 20:45 被 shutdown 打断后 last_status
     挂了 3 天零告警。D3 收"最新一条执行 = failed"的作业 (派发失败归 D2, 不重叠)。
  D4 孤儿进程 (2026-09-14 补): D3 只看得到"作业失败", 看不到"失败作业的进程还活着
     占资源" —— 9/11 那次 DA 挂死被 reparent 后存活 2天16小时, 握着生产库 3 个句柄
     + 1 条 FutuOpenD 连接。D4 按 cmdline 模式 × 存活时长收这类慢性泄漏。
  四者都用基线棘轮抑制既有墓地噪音: 新出现的键才告警, 已知项静默计数。
  冻结基线: --update-liveness-baseline (只许减少不许新增, 同 config_key_census)。
运行: PYTHONPATH=. ./venv/bin/python scripts/data_freshness_watchdog.py [--push] [--dry]
      [--update-liveness-baseline]
"""
import argparse
import json
import math
import re
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

_log = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / ".env.us")
except Exception:
    _log.warning("dotenv load failed", exc_info=True)

# ── 监控清单: (相对路径, 最大静默小时, 说明, 仅工作日) ──
#   max_age_hours: 超过此时长未更新 → 告警
#   weekday_only: True 时周末(周六日)不告警 (盘后才写的源)
WATCHLIST = [
    ("data/llm_audit.db",                          30, "LLM 审计库 (成本观测地基)",        False),
    ("data/opportunity_scan_{today}.json",         30, "DA 指令生成 bridge",               True),
    ("data/factor_ic_latest.json",                 50, "因子 IC 回填 (Tue-Sat cron, 周一 gap 49h 为计划内)", True),
    ("data/shadow/llm_universe_scan.jsonl",        30, "UE 宇宙扫描 shadow",               True),
    ("data/shadow/premarket_peer_shadow.jsonl",    30, "盘前 peer shadow",                 True),
    ("data/shadow/exec_gate_audit.jsonl",          30, "执行闸门审计",                     True),
    ("data/shadow/sltp_verify_shadow.jsonl",       30, "止盈止损校验 shadow",             True),
    # 2026-08-26 复查加固: U11 确认门兜底告警 (主防线=双季cron+当日幂等+窗口守卫日志)。
    #   74h 实测口径 (8/26 独立复核修正原"假日不误报"注释, 证伪见 DEB-20260825 复核段):
    #   美国假日后第一个交易日 06:25 检查必误报一次 (周一假日→周二 80.75h; 连休两天→104.75h),
    #   当晚 confirm 跑后自恢复, 零数据污染 — 误报本身证明链路活着, 接受为已知噪音。
    #   代价: 真静默死亡最坏 ~5 天检出 — 可接受, 本条只是兜底中的兜底。
    ("data/shadow/ue_confirm_gate.jsonl",         74, "UE 开盘确认门 shadow (U11)",       True),
    # 2026-09-11 断头传感器修复批: premarket_invalidation 采集侧 8/9 起写盘但 31 天无结算器
    #   (无 watchdog 无台账), 补结算器同时补新鲜度监控 — 采集停产必须当日可见。
    # 2026-09-11 同日 (DEB-20260911 用户裁「按建议执行」): A_short_chase/C_long_chase 退役,
    #   仅 B_long_knife 打标; 行仍按当日全部可执行指令落盘 ⇒ 新鲜度口径不变。
    ("data/premarket_invalidation_shadow.jsonl",  50, "盘前失效影子 (B 规则; A/C 9/11 退役, 09:35-09:59 ET/交易日)", True),
    ("data/shadow/ue_tier_gate_shadow.jsonl",     74, "UE tier 闸影子 (UE 扫描日批写, 周末+假日 74h 口径同 U11)", True),
    # 已知墓地 (DEB-20260723 time-exit-censoring 实证6): 若已裁决停用应从清单移除, 否则持续告警提醒清理
    # 2026-08-26: prompt_eval + shadow_technical_direction 已归档至
    #   data/shadow/archive_20260826/ (上游停产实锤, 全影子普查裁决), 条目移除。
    ("data/shadow/playbook_direction_audit.jsonl", 72, "playbook 方向审计 (活跃, 8/26普查4消费方)", True),
    ("data/shadow/edge_gate_shadow.jsonl",         72, "[疑墓地] edge gate shadow",        True),
    # 2026-07-26 减法R2-Q6 (裁决: 不建新注册表, 高危文件入现有 watchdog): "生产者死了消费者不知道"
    #   price_fail_closed (责任 DEB-20260726 Q2, 已清空保留机制)。
    #   [2026-08-23] SHORT 门控两件 (hard_to_borrow/short_interest.json) 已随死门删除移出清单 —
    #   用户裁"不值得" (88天空文件静默 fail-open 实案, 责任同 R2 Q1), 消费门已删, 文件不再需要。
    ("data/price_fail_closed.json",          math.inf, "[人工名单] 期权跳过集 (仅人工维护, 静止正常)", False),
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

# 锁存表 (2026-09-14, DEB-20260910 §11.8 选项(b), 用户裁"如果问题确认，请执行修复"):
#   事件驱动/锁存型表只在条件真成立时才写行 ⇒ 零行 = 条件从未成立 = **健康**,
#   不是"生产者从未跑"。D1 的 never_fed 判据把两者混为一谈, 于是每轮实跑都推一条
#   假阳性到飞书 (9/12 06:25 生产实证 `[pushed] 飞书`; 9/14 --dry 复现该键
#   `D1:playbook_risk_state:never_fed` 为当轮唯一新告),
#   真告警会被这条常态噪音掩掉 —— 与本文件"降级不报警"的立项初衷正好相反。
#   为何选 (b) 而非 (a) 补基线: DEB 原文已给理由 —— (a) 每建一张锁存表就要人工加
#   一次键, (b) 把知识写进判据本身且可被测试钉住 (tests/test_watchdog_liveness.py)。
#   登记纪律: 新增条目必须写明"零行为何等于健康" + 读侧在哪, 并补一条钉子测试。
#   回滚: 本 dict 置空即恢复旧行为 (playbook_risk_state 重新按 never_fed 告警)。
LATCH_TABLES = {
    # RiskCircuitBreaker._stop_until 的熔断锁存表 (58e3ce8b, DEB-20260830-timezone
    #   第 2 轮 TZ-1, 8/30 建表): 只在熔断真触发时写行。读侧
    #   app/playbook/risk_circuit_breaker.py:164 明确"无行/不可解析 → None = 未熔断",
    #   不是"门恒空"。liveness 基线冻结于 8/28 21:16, 比建表早 2 天 ⇒ 它被判"新键"。
    "playbook_risk_state": "熔断锁存表: 零行 = 从未熔断 = 健康 (读侧无行→None=未熔断)",
    # 2026-10-16 (推送面深查 A-1): open_positions 零行 = 账面全平 = 健康
    #   (10-14/10-16 实证: 清仓后零行被 D1 误报 never_fed, 还经 meta_audit 二次放大成 P0)。
    #   读侧=持仓/风险面, 空仓即无持仓可读; 写入方只在有持仓时写。
    "open_positions": "持仓锁存表: 零行 = 账面全平 = 健康 (清仓后零行是正常态, 非断供)",
}

# 事件驱动表: 有行但长时间无新行 = 条件未再成立, 不是生产者死亡。
# manual_order_audit 只在人工下单/面板审计时写入; 9/30 实证 25 天无手动单被 D1
# 误报 stalled。豁免不是放弃监控: 手动单链路另有提交/回报错误与对账监控。
EVENT_DRIVEN_TABLES = {
    "manual_order_audit": "人工下单审计表: 无新行 = 无人手动下单 = 健康",
}

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
    """冻结当前全部 D1/D2/D3/D4 发现为已知基线 (棘轮起点; 此后只新增才告警)。"""
    keys = {}
    for f in (_check_table_liveness({})[0] + _check_hermes_jobs({})[0]
              + _check_hermes_failures({})[0] + _check_orphan_processes({})[0]):
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
            _log.warning("_resolve_db_path: importlib import failed", exc_info=True)
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
            _log.warning("_table_writers: file read failed for %s", p, exc_info=True)
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
    parsed = None
    last_err = None
    for f in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f",
              "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(s[:26], f)
            break
        except Exception as _fe:
            # 逐格式失败是预期路径 (5 选 1), 不留痕; 全部失败才在下方统一告警一次。
            # 注: except 体写成裸 pass/continue 会计入 except:pass 棘轮 (只降不升)。
            last_err = _fe
    if parsed is not None:
        return (datetime.now() - parsed).days
    try:  # epoch 秒 (idempotency_keys.created_at 等)
        return (datetime.now() - datetime.fromtimestamp(float(s))).days
    except Exception as _ee:
        last_err = _ee
    # 告警放在 except 块外: W6 要求 except 内告警必带栈; 此处每值统一告警一次,
    # 噪声与留痕兼得 (更早的实现每试一个格式告一次, 单值最多刷 6 条)。
    _log.warning("_age_days: timestamp unparseable: %r (err=%s)", s, last_err)
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
                    _log.warning("_check_table: COUNT query failed for %s", t, exc_info=True)
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
                if t.lower() in EVENT_DRIVEN_TABLES:
                    continue  # 事件未发生 = 健康态, 见 EVENT_DRIVEN_TABLES
                if n == 0:
                    if t.lower() in LATCH_TABLES:
                        continue  # 锁存表零行是健康态, 不是生产者死亡 (见 LATCH_TABLES)
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
        # 检测器自身异常必须显性化 (静默返回 [] 会被读成"全部表健康" = 二次失明)。
        # 2026-09-14 修**优先级陷阱** (D1-D4 四处同病, 由 D4 新钉子测试抓出):
        #   原写 `return ([], known, observed) if cond else [ {...} ], 0, []` ——
        #   条件表达式比元组构造结合得更紧, 故 cond 为真时实际返回的是
        #   `(([], known, observed), 0, [])`, 即"发现位"是个**元组**而不是 list;
        #   下游 collect_liveness 的 `t_find + h_find + f_find + o_find` 遂成
        #   tuple + list → TypeError ⇒ 恰好在"detector_error 已入基线、本轮应静默"
        #   这条路径上把整个看门狗打崩 (四个维一起失明, 只剩 cron 的钉钉兜底吼一声)。
        #   改为显式 if/return, 语义不变且对优先级免疫。
        if baseline.get("D1:detector_error"):
            return [], known, observed
        return [{"key": "D1:detector_error", "kind": "D1",
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
#     "有实跑记录" = 该槽有 started_at 非空的执行行 (2026-09-14 修正): 派发失败的行
#     claimed_at 有值而 started_at IS NULL — 业务脚本从未启动, 不算跑过。
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
            # "实跑" = worker 真的启动了 (started_at 非空), 不等于"被认领过" (claimed_at)。
            #   实证 (2026-09-14 审计): 2026-09-06 10:00 → 09-07 22:30 `systemd-run --user
            #   --scope` 不可用, Hermes 写下 12 行派发失败记录 — claimed_at 有值、
            #   started_at IS NULL、error="cannot create restart-safe systemd scope for
            #   gateway child"。旧判据 MAX(claimed_at) 把这 12 行全当"已跑" ⇒
            #   ①「DEB-20260815 硬止损监控·上午段」盘中连缺 4 槽 (9/7 21:00/21:30/22:00/
            #     22:30) 零告警 — 风控兜底整段是纸面的;
            #   ② DA 主作业 9/7 20:00 同因失败 ⇒ da_gate_decisions.jsonl 该日零行。
            #   与本文件反复钉住的教训同源 (E13 影子空转 / e1_shadow_collector:122
            #   "9 次 last_status=ok 而文件 0 字节"): 调度面有记录 ≠ 业务真的跑了。
            #   started_at 缺列 (Hermes 旧 schema) → 回落 claimed_at 保持旧行为, 不炸 D2。
            _cols = {r[1] for r in conn.execute("PRAGMA table_info(executions)")}
            run_col = "started_at" if "started_at" in _cols else "claimed_at"
            last = {r[0]: r[1] for r in conn.execute(
                f"SELECT job_id, MAX({run_col}) FROM executions "
                f"WHERE {run_col} IS NOT NULL GROUP BY job_id")}
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
        # (显式 if/return: 见 D1 处 2026-09-14 优先级陷阱说明, 四处同修)
        if baseline.get("D2:detector_error"):
            return [], known, judged
        return [{"key": "D2:detector_error", "kind": "D2",
                 "detail": f"调度对账内部异常: {type(e).__name__}: {e}",
                 "action": "D2 检测器自身失明, 需人工修复"}], 0, 0
    return findings, known, judged


# ══════════════════════════════════════════════════════════════════════
# D3 跑了但失败且未恢复 (executions 最新一条 status=failed 且 started_at 非空)
#   动机 (2026-09-14 全链路审计实证): DA 主作业「deep-stock-analysis 每日盘前扫描+分析」
#   9/11 20:45 `Interrupted by shutdown before terminal completion` (跑满 45min 被中断),
#   其后 9/12-9/13 是周末 (cron `0 20 * * 1-5`) 无后续执行 ⇒ jobs.json last_status
#   挂着 'error' 整整 3 天, 到 9/14 审计才发现, 期间零告警。同类还有 9/9 04:08
#   盘后综合复盘 / 9/11 04:43 盘前趋势验证 (同 "Interrupted by shutdown")。
#   D2 补不上这一维: 它只问"槽位上有没有实跑记录", 而一条 failed 记录同样占位
#   ⇒ D2 判它"跑过了"。与本文件立项初衷同源 ("降级不报警"是墓地根因)。
#   与 D2 分工不重叠: started_at IS NULL (派发失败, worker 从未启动) 归 D2 —
#   那不是"跑了但失败"而是"根本没跑"; D3 只收 started_at 非空的失败, 故 9/7 那 12 行
#   派发失败不会被两面重复告。
#   判据: 该作业 executions 里**最新一条** (MAX(rowid)) 为 failed 即告 —— 最新即失败
#   ⇒ 其后必然没有成功执行覆盖它, "未恢复"由 MAX(rowid) 天然表达, 无需额外状态文件。
#   噪音边界: 真跑了但业务失败的作业 (如 8 月 alpha-backfill 连续 8 次 rc=124 超时)
#   也会告 —— 那是信号不是噪音 (同一作业连失败 8 次当时无人知)。确需静默走棘轮基线。
# ══════════════════════════════════════════════════════════════════════

def _check_hermes_failures(baseline: dict):
    """返回 (待告警发现, 基线内静默数, 已判定作业数)。异常显性化, 不静默少算。"""
    findings, known, judged = [], 0, 0
    try:
        import sqlite3
        if not HERMES_JOBS_JSON.exists() or not HERMES_EXEC_DB.exists():
            return [], 0, 0   # 调度面不可读由 D2:hermes_missing 负责吼, 不重复告
        enabled = {j.get("id"): j
                   for j in json.loads(HERMES_JOBS_JSON.read_text()).get("jobs", [])
                   if j.get("enabled")}
        conn = sqlite3.connect(f"file:{HERMES_EXEC_DB}?mode=ro", uri=True)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(executions)")}
            if not {"status", "started_at"} <= cols:
                # Hermes 旧 schema → 本维不可判。静默退出而不是抛错被外层吞成"全健康"
                # (与 D2 的 run_col 回落同理: 宁可少一维, 不可让检测器整体失明)。
                return [], 0, 0
            err_col = "error" if "error" in cols else "NULL"
            fin_col = "finished_at" if "finished_at" in cols else "claimed_at"
            rows = conn.execute(f"""
                SELECT e.job_id, e.status, e.started_at, e.{fin_col}, {err_col}
                FROM executions e
                JOIN (SELECT job_id, MAX(rowid) AS m FROM executions GROUP BY job_id) x
                  ON x.job_id = e.job_id AND x.m = e.rowid""").fetchall()
        finally:
            conn.close()
        for job_id, status, started, finished, error in rows:
            j = enabled.get(job_id)
            if not j:
                continue          # 已停用/一次性作业: 不再期望它跑, 不告
            judged += 1
            if str(status) != "failed" or not started:
                continue          # 成功 / 派发失败 (归 D2) → 本维不管
            name = str(j.get("name") or job_id)[:36]
            why = " ".join(str(error or "").split())[:120] or "(无 error 文本)"
            key = f"D3:{job_id}"
            detail = (f"最近一次执行 failed 且此后无成功执行覆盖 "
                      f"(止于 {str(finished)[:16]}): {why}")
            if key in baseline:
                known += 1
            else:
                findings.append({
                    "key": key, "kind": "D3", "name": name, "detail": detail,
                    "action": "作业跑了但没跑成且不会自愈 → 下游数据/推送缺口"})
    except Exception as e:
        # 检测器自身异常必须显性化 (返回 [] 会被读成"全部作业健康" = 二次失明)
        # (显式 if/return: 见 D1 处 2026-09-14 优先级陷阱说明, 四处同修)
        if baseline.get("D3:detector_error"):
            return [], known, judged
        return [{"key": "D3:detector_error", "kind": "D3",
                 "detail": f"失败对账内部异常: {type(e).__name__}: {e}",
                 "action": "D3 检测器自身失明, 需人工修复"}], 0, 0
    return findings, known, judged


# ══════════════════════════════════════════════════════════════════════
# D4 孤儿进程 (调度面已判死 / 进程仍活)
#   动机 (2026-09-14 全链路审计实证, DEB-20260910 §16.10(a)): DA cron 的 pipeline
#   调用**没有套 timeout** (对比 UE cron 每段 timeout 900/1800/600), 9/11 20:03 那次
#   挂死在对 FutuOpenD 的 socket 读上 (无超时); hermes 20:45 判 "Interrupted by
#   shutdown before terminal completion" 后, shell(PID 1075503)+python(1075525) 被
#   reparent 到 `systemd --user`(PID 995) 却**存活 2天16小时**, 期间持有
#   data/ftseek_us.db(+ -wal/-shm) 3 个句柄 + FutuOpenD ESTAB (127.0.0.1:50952→11111)
#   + Futu SDK 日志 fd。D3 只报"作业失败", 报不出"进程还活着占资源"; 而 cron 下次
#   照常 fork 新进程 ⇒ 同类挂死会**累积**, 行情连接/句柄单调增长。
#   (该孤儿已于 2026-09-14 12:40 SIGTERM 清理, 取证 /tmp/orphan_forensics_20260914.txt。
#    根因侧"给 DA cron 套 timeout"属 hermes 自动化层变更 (~/.hermes/cron/jobs.json),
#    按 handoff.md:1432 既有纪律不擅动, 待用户裁; 本维是仓内可落地的兜底观测。)
#   判据: (cmdline **或** cwd) 命中 ORPHAN_PATTERNS 且存活 > ORPHAN_MAX_HOURS。
#     阈值 2h = DA 历史最长实跑 51min (9/10 20:00→20:51) 的 2.3 倍余量。
#     为何必须带 cwd 一路 (2026-09-14 自测实证): 9/11 挂死的** python 子进程**
#     argv 是 `.../venv/bin/python pipeline.py --mode full --top 5 --symbols  --format json`
#     —— `cd /root/.hermes/skills/deep-stock-analysis/scripts` 发生在**父 shell**,
#     子进程 argv 里根本没有 skill 路径, 只按 cmdline 匹配会**恰好漏掉真正握着
#     生产库句柄与 FutuOpenD 连接的那一个** (只抓到无害的包装 shell)。cwd 是子进程
#     从父 shell 继承的, 故 `pat in cwd` 补上这一路; 两路取或, 单路失效仍有另一路。
#     覆盖面: 该 skill 目录下 3 个 cron 作业 (盘前扫描 e0d574cc1a3c / 盘后复盘
#     3a6c83bbd84c / alpha-backfill 6af0648e3eb3) **均未套 timeout**, 同属孤儿易感面。
#   零误伤边界: 模式只认 hermes skill 目录下的一次性批处理脚本, 不含任何常驻服务
#     (ftseek-us MainPID / scripts/hermes_worker.py / `hermes_cli.main gateway run`
#      的 cmdline 与 cwd 均不匹配) ⇒ 长寿命常驻进程不会被打成孤儿。
#   节奏: 本脚本 cron `25 6 * * 2-6` 每日一次 ⇒ 按 PID 建键不构成告警风暴。
#   回滚: ORPHAN_PATTERNS 置空 (本维静默不判, 其余维不受影响)。
# ══════════════════════════════════════════════════════════════════════

ORPHAN_MAX_HOURS = 2.0
ORPHAN_PATTERNS = {
    # cmdline/cwd 子串 → 它挂死为什么是风险 (登记纪律同 LATCH_TABLES: 必须写明理由)
    "deep-stock-analysis/scripts":
        "DA 盘前扫描 pipeline: cron 未套 timeout, 挂死即泄漏 FutuOpenD 连接 + 生产库句柄",
}
# /proc 采样降级阈值: 竞态退出 (listdir 与 read 之间进程消失) 是常态但占比极低;
#   超过一半读不到 = 结构性不可读 (权限/容器/proc 挂载异常), 此时"0 孤儿"是**假绿**
#   —— 与本文件"降级不报警"的立项初衷同源, 故必须显性化成告警而不是静默少算。
PROC_SCAN_SKIP_RATIO = 0.5


def _iter_processes(stats=None):
    """只读枚举 /proc → (pid, ppid, elapsed_hours, cmdline, cwd)。

    零新依赖 (不引 psutil): 存活时长由 /proc/uptime 与 /proc/<pid>/stat 第 22 字段
    (starttime, 单位 clock tick) 推出。单进程读取失败 (竞态退出/权限) 跳过不中断 ——
    与 D1/D2/D3 同款"宁可少一个样本, 不可让检测器整体失明"。

    stats: 可选 dict, 回填 {"seen": 候选数, "skipped": 读不到而丢弃数} —— 丢样本
      本身必须可观测, 否则大面积不可读时 D4 会静默报"0 孤儿"(假绿)。
    """
    import os
    clk = os.sysconf("SC_CLK_TCK") or 100
    with open("/proc/uptime") as f:
        boot = time.time() - float(f.read().split()[0])
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        if stats is not None:
            stats["seen"] = stats.get("seen", 0) + 1
        try:
            with open(f"/proc/{entry}/stat") as f:
                raw = f.read()
            # comm(第2字段) 可含空格与括号 → 必须从最后一个 ')' 之后再切
            rest = raw[raw.rindex(")") + 1:].split()
            ppid = int(rest[1])        # 第 4 字段
            started = int(rest[19])    # 第 22 字段 starttime
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
            hours = (time.time() - (boot + started / clk)) / 3600.0
        except Exception:
            # R5 留痕 (仓内纪律: 新增异常处理不得静默 pass/continue) — 计入降级分子,
            # 由调用方按 PROC_SCAN_SKIP_RATIO 判是否显性化, 不逐条打日志刷屏。
            if stats is not None:
                stats["skipped"] = stats.get("skipped", 0) + 1
            continue
        try:    # cwd 读不到 (僵尸/权限) 只丢这一路信号, 不丢整个样本
            cwd = os.readlink(f"/proc/{entry}/cwd")
        except Exception:
            cwd = ""
        yield int(entry), ppid, hours, cmd, cwd


def _check_orphan_processes(baseline: dict, procs=None):
    """返回 (待告警发现, 基线内静默数, 已判定进程数)。异常显性化, 不静默少算。"""
    findings, known, judged = [], 0, 0
    if not ORPHAN_PATTERNS:
        return findings, known, judged    # 回滚态: 本维不判 (不产 detector_error)
    try:
        stats = {}
        for pid, ppid, hours, cmd, cwd in (procs or (lambda: _iter_processes(stats)))():
            why = next((w for pat, w in ORPHAN_PATTERNS.items()
                        if pat in cmd or pat in cwd), None)
            if why is None:
                continue
            judged += 1
            if hours <= ORPHAN_MAX_HOURS:
                continue                  # 正常在跑 (历史最长 51min) → 不告
            age = f"{hours:.1f}h" if hours < 48 else f"{hours / 24:.1f}天"
            key = f"D4:{pid}"
            hit = "cmdline" if any(p in cmd for p in ORPHAN_PATTERNS) else "cwd"
            detail = (f"{why} | 已存活 {age} (阈值 {ORPHAN_MAX_HOURS:.0f}h, ppid={ppid}, "
                      f"命中 {hit}): "
                      f"{' '.join(cmd.split())[:110]}")
            if key in baseline:
                known += 1
            else:
                findings.append({
                    "key": key, "kind": "D4", "name": f"pid {pid}", "detail": detail,
                    "action": "调度面已判死但进程仍活 → 泄漏行情连接/库句柄, 需 kill 并给 cron 套 timeout"})
        # 采样降级显性化: 注入式 procs (测试) 不回填 stats ⇒ seen=0 ⇒ 本段自动跳过。
        seen, skipped = stats.get("seen", 0), stats.get("skipped", 0)
        if seen and skipped / seen > PROC_SCAN_SKIP_RATIO:
            findings.append({
                "key": "D4:proc_scan_degraded", "kind": "D4", "name": "/proc 采样降级",
                "detail": (f"{skipped}/{seen} 个进程读不到 "
                           f"({skipped / seen * 100:.0f}% > 阈值 {PROC_SCAN_SKIP_RATIO * 100:.0f}%)"),
                "action": "本轮'孤儿数'不可信 (可能假绿), 需查权限/proc 挂载后重跑"})
    except Exception as e:
        # 检测器自身异常必须显性化 (返回 [] 会被读成"没有孤儿" = 二次失明)
        # (显式 if/return: 见 D1 处 2026-09-14 优先级陷阱说明, 四处同修)
        if baseline.get("D4:detector_error"):
            return [], known, judged
        return [{"key": "D4:detector_error", "kind": "D4",
                 "detail": f"孤儿进程对账内部异常: {type(e).__name__}: {e}",
                 "action": "D4 检测器自身失明, 需人工修复"}], 0, 0
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
        lines.append("── 活性对账 (D1 表载体 / D2 应跑未跑 / D3 跑了但失败 / D4 孤儿进程) ──")
        for f in alarms:
            tag = {"D1": "🫥", "D2": "🕰", "D3": "💥", "D4": "👻"}.get(f["kind"], "❗")
            head = f.get("name") or f["key"].split(":")[-1]
            lines.append(f"{tag} [{f['kind']}] {head}: {f['detail']}")
    return "\n".join(lines)


def collect_liveness() -> dict:
    """D1+D2+D3+D4 一次性采集 (基线内发现静默计数, 新键进告警)。异常已在各检测器内吞掉。"""
    baseline = _load_liveness_baseline()
    t_find, t_known, observed = _check_table_liveness(baseline)
    h_find, h_known, h_judged = _check_hermes_jobs(baseline)
    f_find, f_known, f_judged = _check_hermes_failures(baseline)
    o_find, o_known, o_judged = _check_orphan_processes(baseline)
    return {
        "at": datetime.now().isoformat(timespec="seconds"),
        "baseline_keys": len(baseline),
        "alarms": t_find + h_find + f_find + o_find,
        "silenced_known": t_known + h_known + f_known + o_known,
        "tables": observed,
        "hermes_jobs_judged": h_judged,
        "hermes_failures_judged": f_judged,
        "orphan_procs_judged": o_judged,
    }


def main(push=False, dry=False, update_baseline=False):
    if update_baseline:
        keys = save_liveness_baseline()
        print(f"[baseline] 已冻结 {len(keys)} 项 D1/D2/D3/D4 发现 → {LIVENESS_BASELINE}")
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
    print(f"[D1/D2/D3/D4] 表枚举 {len(live['tables'])} 张 | 新告 {len(live['alarms'])} | "
          f"基线内静默 {live['silenced_known']} | 调度已对账 {live['hermes_jobs_judged']} 作业 | "
          f"失败已对账 {live['hermes_failures_judged']} 作业 | "
          f"孤儿已对账 {live.get('orphan_procs_judged', 0)} 进程")
    for f in live["alarms"]:
        print(f"  [LIVE ] {f['kind']} {f.get('name') or f['key']}: {f['detail']} → {f['action']}")
    print("=" * 60)
    try:  # 观测落盘 (只读采集, 供人工回看墓地全貌; 失败不影响告警链)
        (DATA / "watchdog_liveness_latest.json").write_text(
            json.dumps(live, ensure_ascii=False, indent=1))
    except Exception:
        _log.warning("watchdog liveness write failed", exc_info=True)

    msg = _build_message(stale, missing, live)
    if msg and push and not dry:
        # 2026-08-09 (告警风暴消除): 文件持久化冷却 — 30min 内同内容不重复推.
        #   看门狗是 cron 独立进程, 进程内状态无效; 持久化到 data/watchdog_last_push.json.
        #   若 OpenD/Futu 持续不可用, watchdog 每轮都检到停更 → 无冷却=每轮1条告警风暴.
        # 2026-10-16 (巡检 A-4 + 用户令「源头修复」): 30min 时间窗改为**内容指纹**:
        #   同内容告警 (hash 相同) 只推一次, 内容变化才再推 — 1800s 窗对日频作业
        #   等于无冷却 (实证: 同一 D1 误报连推多日)。「宁多勿漏」由 fail-loud 兜住:
        #   状态不可读 ⇒ _suppress=False 照推。
        _state_file = DATA / "watchdog_last_push.json"
        _now = time.time()
        _suppress = False
        try:
            if _state_file.exists():
                with open(_state_file) as _f:
                    _prev = json.load(_f)
                if _prev.get("msg_hash") == _hash_msg(msg):
                    _suppress = True
        except Exception:
            _log.warning("cooldown state read failed", exc_info=True)
        if _suppress:
            print("[cooldown] 告警内容与上次推送一致, 跳过 (内容变化即恢复推送)")
        else:
            try:
                from app.notifier.feishu import send_text
                send_text(msg)
                print("[pushed] 飞书")
                try:
                    with open(_state_file, "w") as _f:
                        json.dump({"ts": _now, "msg_hash": _hash_msg(msg)}, _f)
                except Exception as _e_state:
                    # 2026-09-10 (DEB-20260910-logging-observability §10.7, except:pass 清剿):
                    # 冷却状态写失败 → 下一轮同内容告警**重复推** (本文件 2026-08-09 消除的
                    # 告警风暴回归)。静默 pass 时值班人只看得到重复告警、看不到根因。
                    print(f"[warn] 冷却状态写入失败 ({_state_file}): {_e_state!r} → 下轮可能重复推送")
            except Exception as e:
                print(f"[warn] 推送失败: {e}")
    elif not msg:
        print("[ok] 全部新鲜, 不推送")


def _hash_msg(msg: str) -> str:
    """内容指纹: 抹掉数字漂移 (天数/小时数随时间变, 不应产生新 episode) — 2026-10-16
    与 shadow_board 的 episode 指纹同规 (re.sub 数字→#)。"""
    import hashlib
    import re as _re
    norm = _re.sub(r"[\d.]+", "#", msg)
    return hashlib.md5(norm.encode("utf-8")).hexdigest()[:16]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="数据新鲜度看门狗 (只读)")
    ap.add_argument("--push", action="store_true", help="有告警时推送飞书")
    ap.add_argument("--dry", action="store_true", help="只打印不推送")
    ap.add_argument("--update-liveness-baseline", action="store_true",
                    help="冻结当前 D1/D2 发现为已知基线 (棘轮: 此后只新增才告警)")
    a = ap.parse_args()
    raise SystemExit(main(push=a.push, dry=a.dry, update_baseline=a.update_liveness_baseline))
