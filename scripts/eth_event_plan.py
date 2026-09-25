#!/usr/bin/env python3
"""RFC-104 阶段 0: ETH 事件执行计划生成器 (只读工件, 零交易行为变更).

## 9/24 阶段 0 补齐批 (RFC-104 addendum)
- **单一真源**: parse_earnings_phase / session_event_pairs 直接复用 executor 函数对象,
  消灭双实现漂移面 (RFC-104 §5 历史坑).
- **多源并集**: 默认 futu ∪ Finnhub; --finnhub-only 保留作离线降级.
- **刷新模式** (--refresh): 与既有计划合并, state 只前进不回退, ambiguous 自愈.
- **actual 到达观测**: actual_arrived_at / actual_value 字段 (观测非策略, §2.2 宁少勿多).
- **每日对账** (--reconcile): 覆盖率度量 + 漏票检出, 产物 reconcile_<date>.json.

## 职责
从既有日历源 (futu + Finnhub) 生成 T 日事件计划: 每票一行, 含状态机字段、源标注、
ambiguous 处理。计划**不含**方向/SL/TP/费用/可执行性数字 (三处收紧 §3)。

## 产物
- 文件工件: `data/loops/ext_entry_plan/<et_date>_<session>.json`
- 幂等键: `(et_date, symbol, session, phase)`
- 原子写: `app/utils/atomic_write.py`

## 三处硬不变量 (写进代码注释 + 测试钉死)
1. **不预筛**: 计划行不含 price/volume/mcap 预筛字段; gate_snapshot 仅作审计
2. **不择优**: 源冲突 (日期/相位不一致) → state=ambiguous; 消费侧 fail-closed
3. **不是策略卡**: 不含方向/SL/TP/预期收益/费用; 状态机只推进
   planned → armed → waiting_data → ordered/rejected/expired

## 调度
- 盘前计划: T-1 20:30 ET (CST 周二-六 08:30)
- 盘后计划: T-0 15:45 ET (CST 周二-六 03:45)
- 刷新: T-0 04:00 / 07:00 ET (CST 16:00 / 19:00; 夏令时各 -1h)
- 对账: T+0 21:00 ET (CST 周二-六 09:00)
- 回滚: 删 cron 行 (计划只读工件, 删除即静默)

## CLI 示例
- 生成: ``PYTHONPATH=. venv/bin/python scripts/eth_event_plan.py --session premarket --dry``
- 刷新: ``PYTHONPATH=. venv/bin/python scripts/eth_event_plan.py --session premarket --refresh``
- 对账: ``PYTHONPATH=. venv/bin/python scripts/eth_event_plan.py --session premarket --reconcile --date 2026-09-24``
- 降级: ``PYTHONPATH=. venv/bin/python scripts/eth_event_plan.py --session premarket --finnhub-only``
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env.us", override=True)

# ── 单一真源: 导入 executor (模块级只读 env, 不开连接; 先例: overnight_earnings_carry_shadow.py:70) ──
import scripts.extended_hours_entry_executor as EXEC  # noqa: E402

# ── EDGAR 8-K 观测源 (9/24 增强批: 只读, 不升格, 习惯先验 hint) ──
import scripts.eth_edgar_source as EDGAR  # noqa: E402

from app.utils.atomic_write import atomic_write_json  # noqa: E402

ET = ZoneInfo("America/New_York")
PLAN_DIR = ROOT / "data" / "loops" / "ext_entry_plan"

# ── 状态机常量 (RFC-104 §2.3) ──
STATE_PLANNED = "planned"
STATE_ARMED = "armed"
STATE_WAITING_DATA = "waiting_data"
STATE_ORDERED = "ordered"
STATE_REJECTED = "rejected"
STATE_EXPIRED = "expired"
STATE_AMBIGUOUS = "ambiguous"

VALID_STATES = {STATE_PLANNED, STATE_ARMED, STATE_WAITING_DATA,
                STATE_ORDERED, STATE_REJECTED, STATE_EXPIRED, STATE_AMBIGUOUS}

# 状态序数: 只前进不回退 (刷新合并硬不变量)
_STATE_ORDER = {
    STATE_PLANNED: 0,
    STATE_AMBIGUOUS: 1,
    STATE_ARMED: 2,
    STATE_WAITING_DATA: 3,
    STATE_ORDERED: 4,
    STATE_REJECTED: 4,
    STATE_EXPIRED: 4,
}

# ── 相位常量 ──
PHASE_BEFORE = "before"  # BMO
PHASE_AFTER = "after"    # AMC


# ── 时间工具 ──

def et_now() -> datetime:
    """当前 ET 时刻 (zoneinfo, DST 正确)."""
    return datetime.now(ET)


def et_today() -> str:
    """当前 ET 日期 ISO 格式."""
    return et_now().date().isoformat()


def shift_et_day(day_str: str, offset: int) -> str:
    """ET 日期偏移."""
    d = datetime.strptime(day_str, "%Y-%m-%d").date()
    return (d + timedelta(days=offset)).isoformat()


# ── 单一真源薄别名 (RFC-104 §5: 消灭双实现漂移面) ──
# 契约测试断言 ``plan_mod.parse_earnings_phase is EXEC._parse_earnings_phase``;
# 任何对 executor 逻辑的修改自动传导到计划生成器, 零漂移面.

parse_earnings_phase = EXEC._parse_earnings_phase
"""日历相位 → (before, after). 与 EXEC._parse_earnings_phase **同一函数对象**."""

session_event_pairs = EXEC._session_event_pairs
"""session → 允许的 (日偏移, 相位) 列表. 与 EXEC._session_event_pairs **同一函数对象**.

注意: 盘前 AMC 配对由 env ``EXT_ENTRY_PREMARKET_AMC_PAIRING`` 控制 (executor 读).
generate_plan 的 premarket_amc_pairing 参数会临时设置此 env, 确保测试隔离.
"""


# ── 日历源拉取 ──

def fetch_finnhub_calendar(day: str) -> list[dict]:
    """Finnhub 日历查询 (复用 EXEC._finnhub_today_calendar, 共享缓存与客户端).

    返回标准化行列表; 失败返回空列表 (fail-open at source level; 并集层兜底).
    """
    cal, error = EXEC._finnhub_today_calendar(day)
    if cal is None:
        if error:
            logger.warning("finnhub calendar fetch failed for %s: %s", day, error)
        return []
    now_iso = et_now().isoformat(timespec="seconds")
    rows = []
    for _ticker, row in cal.items():
        rows.append({
            "symbol": str(row.get("symbol", "")).upper(),
            "date": str(row.get("date", ""))[:10],
            "timeOfTheDay": str(row.get("hour", "") or ""),
            "epsActual": row.get("epsActual"),
            "epsEstimate": row.get("epsEstimate"),
            "_src": "finnhub",
            "_asof": now_iso,
        })
    return rows


def fetch_futu_calendar(start: str, end: str) -> list[dict]:
    """futu 日历查询 (短生命周期 OpenQuoteContext + finally close; 照抄 rth_fade_shadow.py 模式).

    失败返回空列表. 注意: 本函数会连接 futu/OpenD; --dry 模式或离线测试不应调用.
    """
    try:
        from futu import OpenQuoteContext, RET_OK
    except ImportError:
        logger.warning("futu 未安装, 跳过 futu 日历源", exc_info=True)
        return []

    try:
        ctx = OpenQuoteContext(
            host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
            port=int(os.getenv("FUTU_OPEND_PORT", "11111")),
        )
        try:
            ret, data = ctx.get_earnings_calendar(
                market="US", begin_date=start, end_date=end
            )
            if ret != RET_OK or data is None:
                return []
            now_iso = et_now().isoformat(timespec="seconds")
            rows = []
            for _, r in data.iterrows():
                rows.append({
                    "symbol": str(r.get("security")).replace("US.", ""),
                    "reportDate": str(r.get("earnings_date"))[:10],
                    "timeOfTheDay": {"BEFORE": "before", "AFTER": "after"}.get(
                        str(r.get("pub_type")), str(r.get("pub_type", "")).lower()
                    ),
                    "_src": "futu",
                    "_asof": now_iso,
                })
            return rows
        finally:
            ctx.close()
    except Exception as exc:
        logger.warning("futu 拉取失败 %s~%s: %s: %s", start, end, type(exc).__name__, exc, exc_info=True)
        return []


# ── 核心: 源合并与计划生成 ──

def _merge_sources(finnhub_rows: list[dict], futu_rows: list[dict]) -> dict[tuple[str, str], dict]:
    """合并两源日历, 按 (symbol, reportDate) 去重.

    返回 {(symbol, date): {"sources": [...], "phases": set()}} 结构.
    每源保留原始行供审计 (sources 字段).
    """
    acc: dict[tuple[str, str], dict] = {}
    for row in finnhub_rows:
        sym = str(row.get("symbol", "")).upper()
        if not sym:
            continue
        date = str(row.get("date", "") or row.get("reportDate", ""))[:10]
        if not date:
            continue
        key = (sym, date)
        entry = acc.setdefault(key, {"symbol": sym, "reportDate": date, "sources": [], "phases": set()})
        entry["sources"].append({
            "name": "finnhub",
            "reportDate": date,
            "timeOfTheDay": str(row.get("timeOfTheDay", "") or row.get("hour", "")),
            "asof": row.get("_asof", ""),
        })
        before, after = parse_earnings_phase(row)
        if before:
            entry["phases"].add(PHASE_BEFORE)
        if after:
            entry["phases"].add(PHASE_AFTER)

    for row in futu_rows:
        sym = str(row.get("symbol", "")).upper()
        if not sym:
            continue
        date = str(row.get("reportDate", ""))[:10]
        if not date:
            continue
        key = (sym, date)
        entry = acc.setdefault(key, {"symbol": sym, "reportDate": date, "sources": [], "phases": set()})
        entry["sources"].append({
            "name": "futu",
            "reportDate": date,
            "timeOfTheDay": str(row.get("timeOfTheDay", "")),
            "asof": row.get("_asof", ""),
        })
        before, after = parse_earnings_phase(row)
        if before:
            entry["phases"].add(PHASE_BEFORE)
        if after:
            entry["phases"].add(PHASE_AFTER)

    return acc


def _detect_source_conflict(entry: dict) -> bool:
    """检测源冲突: 同一 (symbol, date) 两源给出不同相位.

    冲突定义: 存在两个不同源, 各自的 timeOfTheDay 解析出的 (before, after) 不一致.
    例: finnhub 说 "BMO" (before=True, after=False), futu 说 "AMC" (before=False, after=True).
    """
    src_phases: dict[str, tuple[bool, bool]] = {}
    for src in entry.get("sources", []):
        name = src.get("name", "")
        fake_row = {"timeOfTheDay": src.get("timeOfTheDay", ""), "pub_type": ""}
        before, after = parse_earnings_phase(fake_row)
        if before or after:
            src_phases[name] = (before, after)

    if len(src_phases) < 2:
        return False
    phase_values = list(src_phases.values())
    return len(set(phase_values)) > 1


def query_actual_value(symbol: str, report_date: str) -> dict | None:
    """查询 Finnhub 日历行的 epsActual (复用 EXEC._finnhub_today_calendar).

    返回 {"epsActual": float, ...} 或 None (actual 未到或查询失败).
    此函数是"事件数据到达时间观测器"的数据源, 服务 RFC-102 复核判据与 DEB-20260923 A 十月对照.

    为什么复用 _finnhub_today_calendar 而非 _earnings_surprise_calendar_actual:
    后者耦合了 actual vs estimate 比较逻辑 (判据②), 本函数只需 actual 存在性.
    """
    cal, error = EXEC._finnhub_today_calendar(report_date)
    if cal is None:
        if error:
            logger.warning("query_actual_value: finnhub failed for %s on %s: %s",
                           symbol, report_date, error)
        return None
    ticker = symbol.replace("US.", "").upper()
    row = cal.get(ticker)
    if row is None:
        return None
    actual = row.get("epsActual")
    if actual is None:
        return None
    return {"epsActual": float(actual), "hour": row.get("hour")}


# ── EDGAR 观测源薄接入 (9/24 增强批: 只读, 不升格, fail-soft) ──

def _enrich_edgar(plan_rows: list[dict]) -> list[dict]:
    """为计划行追加 EDGAR 观测字段 (fail-soft: 任何异常只 warning 不崩).

    新增字段:
    - edgar_8k_filed_at: 该票该日 8-K 2.02 提交时刻 ET (只记首次, 与 actual_arrived_at 并列)
    - habit_hint: ambiguous 行的习惯先验 {phase, n_history, consistency, basis}
                  (只记录不改 state — ambiguous 仍 fail-closed)

    env ETH_PLAN_EDGAR_ENABLED=false → 跳过全部 EDGAR 调用 (一键断源).
    """
    if os.getenv("ETH_PLAN_EDGAR_ENABLED", "true").lower() == "false":
        return plan_rows

    for row in plan_rows:
        sym = row.get("symbol", "")
        et_date = row.get("et_date", "")
        if not sym or not et_date:
            continue

        try:
            # edgar_8k_filed_at: 只记首次 (已有非 null 值时不覆盖)
            if row.get("edgar_8k_filed_at") is None:
                arrival = EDGAR.edgar_8k_arrival(sym, et_date)
                if arrival is not None:
                    row["edgar_8k_filed_at"] = arrival.get("filed_at")

            # habit_hint: 只给 ambiguous 行 (只记录不改 state)
            if row.get("state") == STATE_AMBIGUOUS and row.get("habit_hint") is None:
                habit = EDGAR.edgar_phase_habit(sym)
                if habit is not None:
                    row["habit_hint"] = habit
        except Exception as exc:
            # fail-soft: 任何 EDGAR 异常只 warning, 不崩不阻塞计划生成
            logger.warning("EDGAR enrich failed for %s: %s: %s", sym, type(exc).__name__, exc,
                           exc_info=True)

    return plan_rows


def generate_plan(et_date: str, session: str, *, finnhub_rows: list[dict],
                  futu_rows: list[dict], premarket_amc_pairing: bool = False) -> list[dict]:
    """生成 T 日事件计划 (核心函数).

    参数:
        et_date: ET 日期 (YYYY-MM-DD)
        session: "premarket" | "afterhours"
        finnhub_rows: Finnhub 日历原始行
        futu_rows: futu 日历标准化行
        premarket_amc_pairing: 盘前是否接受昨日 AMC (设置 EXEC env 后调 session_event_pairs)

    返回:
        计划行列表, 每行符合 RFC-104 §2.2 schema (含 actual_arrived_at/actual_value).

    三处收紧合规:
        1. 不预筛: 不过滤 price/volume/mcap; gate_snapshot 为空占位
        2. 不择优: 源冲突 → state=ambiguous
        3. 不是策略卡: 不含 direction/sl_tp/expected_profit/fee
    """
    merged = _merge_sources(finnhub_rows, futu_rows)

    # 同步 EXEC.PREMARKET_AMC_PAIRING 模块级常量 (import time 已固化, 需运行时覆写)
    old_pairing = EXEC.PREMARKET_AMC_PAIRING
    EXEC.PREMARKET_AMC_PAIRING = premarket_amc_pairing
    try:
        pairs = session_event_pairs(session)
    finally:
        EXEC.PREMARKET_AMC_PAIRING = old_pairing

    # 允许的 (日期, 相位) 集合
    allowed: dict[str, set[str]] = {}
    for offset, phase in pairs:
        allowed.setdefault(shift_et_day(et_date, offset), set()).add(phase)

    plan_rows: list[dict] = []
    now_iso = et_now().isoformat(timespec="seconds")

    for (sym, report_date), entry in sorted(merged.items()):
        # 日期过滤: 只保留本 session 允许的日期
        phases_for_date = allowed.get(report_date)
        if not phases_for_date:
            continue

        # 相位过滤: 该票的相位必须与 session 允许相位有交集
        entry_phases = entry["phases"]
        matched_phases = entry_phases & phases_for_date

        if not matched_phases and not entry_phases:
            # 相位未知: 标记 ambiguous
            phase_str = "unknown"
            state = STATE_AMBIGUOUS
        elif not matched_phases:
            # 有相位但与本 session 不匹配: 不入计划 (非本 session 的票)
            continue
        else:
            phase_str = ",".join(sorted(matched_phases))
            state = STATE_PLANNED

        # 源冲突检测: 两源相位不一致 → ambiguous
        if _detect_source_conflict(entry):
            state = STATE_AMBIGUOUS

        # RFC-104 §2.2: 计划卡字段 (不含 direction/sl_tp/fee/expected_profit)
        plan_row: dict[str, Any] = {
            "symbol": sym,
            "et_date": et_date,
            "session": session,
            "phase": phase_str,
            "sources": entry["sources"],
            "data_expectation": _data_expectation(phase_str),
            "gate_snapshot": {},  # 仅作审计, 不作放行依据 (收紧 §1)
            "state": state,
            "state_since": now_iso,
            "attempts": 0,
            "reject_reason": None,
            "generated_at": now_iso,
            "refreshed_at": now_iso,
            "source_versions": {s["name"]: s["asof"] for s in entry["sources"]},
            "plan_hash": "",  # 下方计算
            "actual_arrived_at": None,  # 9/24 addendum: 事件数据到达时间观测 (非策略字段)
            "actual_value": None,       # 9/24 addendum: epsActual 值 (观测)
            # ── 9/24 增强批: EDGAR 观测源 (只读, 不升格) ──
            "edgar_8k_filed_at": None,  # 8-K Item 2.02 提交时刻 ET (只记首次, 与 actual_arrived_at 并列)
            "habit_hint": None,         # ambiguous 行的习惯先验 (只记录不改 state)
        }

        # plan_hash: 幂等键的指纹 (symbol+et_date+session+phase)
        hash_input = f"{sym}|{et_date}|{session}|{phase_str}"
        plan_row["plan_hash"] = hashlib.sha256(hash_input.encode()).hexdigest()[:16]

        plan_rows.append(plan_row)

    # ── 9/24 增强批: EDGAR 观测源薄接入 (fail-soft) ──
    _enrich_edgar(plan_rows)

    return plan_rows


def _data_expectation(phase: str) -> dict:
    """数据预期: BMO → actual 约 08:05 ET 可得; AMC → 约 16:00 ET.

    复用 RFC-102 §2.6 的语义; 此处只给期望值, 不给放行结论.
    """
    if PHASE_BEFORE in phase:
        return {
            "basis": "BMO earnings typically available by 08:00-08:10 ET",
            "expected_available_after_et": "08:05",
            "fallback_days": 1,
        }
    if PHASE_AFTER in phase:
        return {
            "basis": "AMC earnings typically available by 16:00-16:15 ET",
            "expected_available_after_et": "16:00",
            "fallback_days": 1,
        }
    return {
        "basis": "unknown phase — data availability uncertain",
        "expected_available_after_et": None,
        "fallback_days": 2,
    }


# ── 刷新合并 (RFC-104 §2.4 T-0 刷新轮) ──

def refresh_plan(existing: list[dict], et_date: str, session: str, *,
                 finnhub_rows: list[dict], futu_rows: list[dict],
                 premarket_amc_pairing: bool = False) -> list[dict]:
    """刷新既有计划: 与新拉日历合并.

    合并语义 (硬不变量, 测试钉死):
    1. state/attempts/state_since **只前进不回退** (按 _STATE_ORDER 序数);
       ambiguous 行拿到新相位证据 → 迁 planned (记 state_since 刷新 + resolution 来源).
    2. 新发现的票 (生成时缺源) append 为 planned.
    3. plan_hash/refreshed_at 更新; 幂等: 同数据重跑逐字节不变 (除 refreshed_at).
    4. actual_arrived_at **只记第一次**: 已有非 null 值时不覆盖.
    5. 新源未覆盖的旧行**保留** + `source_withdrawn_at` 只记首次 — 禁止静默删行
       (计划行只能以显式状态退场; 删行不可审计且违 fail-closed 哲学).
    """
    now_iso = et_now().isoformat(timespec="seconds")

    # 索引既有行 by (symbol, et_date, session)
    existing_idx: dict[tuple[str, str, str], dict] = {}
    for row in existing:
        key = (row["symbol"], row.get("et_date", ""), row.get("session", ""))
        existing_idx[key] = row

    # 生成新计划 (基于最新日历)
    fresh_rows = generate_plan(
        et_date, session,
        finnhub_rows=finnhub_rows, futu_rows=futu_rows,
        premarket_amc_pairing=premarket_amc_pairing,
    )

    result: list[dict] = []
    seen_keys: set[tuple[str, str, str]] = set()

    for fresh in fresh_rows:
        key = (fresh["symbol"], fresh.get("et_date", ""), fresh.get("session", ""))
        seen_keys.add(key)

        old = existing_idx.get(key)
        if old is not None:
            # ── 合并: 保留旧行 state 进度, 更新 sources/phase/refreshed_at ──
            merged_row = dict(old)
            merged_row["sources"] = fresh["sources"]
            merged_row["source_versions"] = fresh["source_versions"]
            merged_row["refreshed_at"] = now_iso

            old_state = old.get("state", STATE_PLANNED)
            new_state = fresh.get("state", STATE_PLANNED)
            old_ord = _STATE_ORDER.get(old_state, 0)
            new_ord = _STATE_ORDER.get(new_state, 0)

            if old_state == STATE_AMBIGUOUS and new_state != STATE_AMBIGUOUS:
                # ambiguous 自愈 → 迁 new_state, 记 resolution
                merged_row["state"] = new_state
                merged_row["state_since"] = now_iso
                merged_row["phase"] = fresh["phase"]
                merged_row["reject_reason"] = None
            elif new_ord > old_ord:
                # 正常前进
                merged_row["state"] = new_state
                merged_row["state_since"] = now_iso
            # else: 保持旧 state (不回退)

            # actual_arrived_at: 只记第一次 (不覆盖已有非 null 值)
            if merged_row.get("actual_arrived_at") is None:
                merged_row["actual_arrived_at"] = fresh.get("actual_arrived_at")
                merged_row["actual_value"] = fresh.get("actual_value")

            # edgar_8k_filed_at: 只记第一次 (与 actual_arrived_at 并列不覆盖)
            if merged_row.get("edgar_8k_filed_at") is None:
                merged_row["edgar_8k_filed_at"] = fresh.get("edgar_8k_filed_at")

            # habit_hint: 保留旧值 (ambiguous 行的习惯先验不覆盖)
            if merged_row.get("habit_hint") is None:
                merged_row["habit_hint"] = fresh.get("habit_hint")

            result.append(merged_row)
        else:
            # ── 新票: append ──
            fresh["refreshed_at"] = now_iso
            result.append(fresh)

    # ── 不变量5: 新源未覆盖的旧行**保留+标记**, 禁止静默删行 ──
    # 日历源波动 (免费层抖动/财报改期/窗口滚动) 会让票从源里消失; 计划行只能以
    # 显式状态 (expired/rejected) 退场, 静默消失 = fail-closed 哲学违例且不可审计
    # (2026-09-24 实测: 16 行刷新后变 15 行, 1 只 ambiguous 票被静默丢弃 — 本段为修复)。
    for key, old in existing_idx.items():
        if key not in seen_keys:
            kept = dict(old)
            if kept.get("source_withdrawn_at") is None:
                kept["source_withdrawn_at"] = now_iso  # 只记首次
            kept["refreshed_at"] = now_iso
            result.append(kept)

    result.sort(key=lambda r: str(r.get("symbol", "")))  # 确定性序 → plan_hash 稳定
    return result


# ── 状态机推进 (阶段 0 只推进 planned→armed, 由时刻窗触发) ──

def advance_state(row: dict, current_et_time: str) -> dict:
    """根据当前 ET 时刻推进状态机.

    阶段 0 的推进规则:
    - planned → armed: 相位窗到达 (BMO: 04:00 ET 之后; AMC: 16:00 ET 之后)
    - armed → waiting_data: 数据未到 (actual 未出现)
    - ambiguous: 保持不动, 等待源刷新或人工裁决

    其他状态 (ordered/rejected/expired) 由执行层推进 (阶段 1+).
    """
    if row.get("state") == STATE_AMBIGUOUS:
        return row  # ambiguous 不自动推进

    if row.get("state") not in (STATE_PLANNED, STATE_ARMED):
        return row  # 终态不由此函数推进

    phase = row.get("phase", "")
    try:
        now = datetime.fromisoformat(current_et_time)
        hour = now.hour
        minute = now.minute
    except (ValueError, TypeError):
        return row

    time_minutes = hour * 60 + minute

    if row["state"] == STATE_PLANNED:
        # BMO: 04:00 ET (240 min) 后 → armed
        # AMC: 16:00 ET (960 min) 后 → armed
        if PHASE_BEFORE in phase and time_minutes >= 240:
            row["state"] = STATE_ARMED
            row["state_since"] = current_et_time
        elif PHASE_AFTER in phase and time_minutes >= 960:
            row["state"] = STATE_ARMED
            row["state_since"] = current_et_time

    return row


# ── 文件 I/O ──

def plan_path(et_date: str, session: str) -> Path:
    """计划文件路径: data/loops/ext_entry_plan/<et_date>_<session>.json"""
    return PLAN_DIR / f"{et_date}_{session}.json"


def load_plan(et_date: str, session: str) -> list[dict]:
    """加载已有计划; 不存在返回空列表."""
    p = plan_path(et_date, session)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("load_plan failed %s: %s", p, exc, exc_info=True)
        return []


def save_plan(et_date: str, session: str, rows: list[dict]) -> Path:
    """原子写入计划文件."""
    p = plan_path(et_date, session)
    PLAN_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(p), rows)
    return p


def _load_jsonl_records(path: Path, trade_date: str) -> list[dict]:
    """从 JSONL 台账加载指定交易日的记录.

    支持两种格式:
    - 扁平记录 (含 symbol + ts 字段): extended_hours_entry_executions.jsonl
    - 批次记录 (含 trade_date + details[].symbol): extended_hours_*_gate_rejects.jsonl
    """
    if not path.exists():
        return []
    records = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as _exc:
                logger.warning("_load_jsonl_records: 忽略异常继续 (复核修复 2026-09-24, 控制流零变更): %s", _exc, exc_info=True)
                continue

            # 扁平格式: 检查 ts 日期
            ts = rec.get("ts", "")
            rec_date = ts[:10] if ts else ""
            td = rec.get("trade_date", rec_date)
            if td != trade_date:
                continue

            # 批次格式: 展开 details
            details = rec.get("details")
            if isinstance(details, list):
                for d in details:
                    if isinstance(d, dict):
                        records.append(d)
            else:
                records.append(rec)
    except OSError as exc:
        logger.warning("_load_jsonl_records failed %s: %s", path, exc, exc_info=True)
    return records


# ── 每日对账 (RFC-104 阶段 0 判据: 当日事件票 ⊆ 计划 = 100%) ──

def reconcile(et_date: str, session: str) -> dict:
    """每日对账: 比较计划 vs executor 真实台账.

    提取"当日实际出现的事件票"集合 (凡带 earnings 事件标记/进了财报门的票),
    与计划比较, 产出覆盖率与漏票列表.

    产物:
        - data/loops/ext_entry_plan/reconcile_<date>.json
        - append 一行到 data/loops/ext_entry_plan/reconcile_ledger.jsonl

    判据: missed==[] ⇒ PASS; 任何漏票 ⇒ FAIL (exit code 非 0).
    只读: 不修改任何生产台账.
    """
    plan = load_plan(et_date, session)
    plan_symbols = {r["symbol"] for r in plan}

    # 从三本台账提取事件票
    data_dir = ROOT / "data"
    earnings_rejects = _load_jsonl_records(
        data_dir / "extended_hours_earnings_gate_rejects.jsonl", et_date
    )
    price_rejects = _load_jsonl_records(
        data_dir / "extended_hours_price_gate_rejects.jsonl", et_date
    )
    executions = _load_jsonl_records(
        data_dir / "extended_hours_entry_executions.jsonl", et_date
    )

    # ── 事件票集合 (P1-1 修复, 2026-09-24 复核) ──────────────────────────
    # 原实现读 `rec["symbol"]`, 但两本 *_gate_rejects 台账是**批次记录**
    # (票在 details[] / rejects[] 里, 无顶层 symbol) ⇒ 集合恒空 ⇒ 覆盖率恒
    # 100.0 / verdict 恒 PASS ⇒ 验收仪器失活, 且该假 PASS 会被三影子的
    # 「reconcile PASS 日数 ≥ 7」自门控消费。现改为三路取票 + 日历同相位锚点。
    ledger_seen: set[str] = set()
    for rec in earnings_rejects:
        for d in (rec.get("details") or []):
            sym = str((d or {}).get("symbol", "")).replace("US.", "").upper()
            if sym:
                ledger_seen.add(sym)
        sym = str(rec.get("symbol", "")).replace("US.", "").upper()  # 兼容扁平记录
        if sym:
            ledger_seen.add(sym)
    for rec in executions:
        sym = str(rec.get("symbol", "")).replace("US.", "").upper()
        if sym:
            ledger_seen.add(sym)

    # 日历同相位锚点: 计划中已带**具体相位**的行 = 本 session 的应见事件票
    # (计划行本就由生产配对表+相位判据产出 ⇒ 此处不引入第三处相位实现)。
    calendar_tickets: set[str] = {
        str(r.get("symbol", "")).replace("US.", "").upper()
        for r in plan
        if r.get("state") == STATE_PLANNED
        and str(r.get("phase", "")) not in ("", "unknown")
    }
    # 价格闸拦下的票: rejects[] 无 earnings 字段 ⇒ 用与日历锚点的交集补回
    for rec in price_rejects:
        for d in (rec.get("rejects") or []):
            sym = str((d or {}).get("symbol", "")).replace("US.", "").upper()
            if sym and sym in calendar_tickets:
                ledger_seen.add(sym)

    event_tickets: set[str] = ledger_seen | calendar_tickets

    missed = sorted(event_tickets - plan_symbols)
    ambiguous_resolved = [
        r["symbol"] for r in plan
        if r.get("state") == STATE_PLANNED and r.get("reject_reason") is None
        and any(s.get("name") for s in r.get("sources", []))
    ]

    # actual 到达统计
    actual_times: list[float] = []
    for r in plan:
        if r.get("actual_arrived_at"):
            try:
                t = datetime.fromisoformat(r["actual_arrived_at"])
                actual_times.append(t.hour * 60 + t.minute)
            except (ValueError, TypeError) as _exc:
                logger.warning("reconcile: 忽略异常继续 (复核修复 2026-09-24, 控制流零变更): %s", _exc, exc_info=True)

    arrival_stats: dict[str, Any] = {"p50": None, "p90": None, "max": None, "n": len(actual_times)}
    if actual_times:
        actual_times.sort()
        n = len(actual_times)
        arrival_stats["p50"] = actual_times[n // 2]
        arrival_stats["p90"] = actual_times[int(n * 0.9)]
        arrival_stats["max"] = actual_times[-1]

    coverage_pct = (
        len(event_tickets - set(missed)) / len(event_tickets) * 100
        if event_tickets else 0.0
    )
    # NO_DATA 与 PASS 必须分开 (P1-1): 空真 PASS 会喂给三影子的
    # 「reconcile PASS 日数 ≥ 7」自门控 ⇒ 用"当日无可验票"打开影子。
    if missed:
        verdict = "FAIL"
    elif not event_tickets:
        verdict = "NO_DATA"
    else:
        verdict = "PASS"

    # 仪器自检 (可见告警, 不改 verdict): 日历有应见票但台账零命中 ⇒
    # 要么 executor 当日没评估到任何应见事件票, 要么提取链又失活。
    if calendar_tickets and not ledger_seen:
        print(
            f"[eth_plan] ⚠️ 仪器自检: 日历应见 {len(calendar_tickets)} 只 "
            f"{sorted(calendar_tickets)}, 台账命中 0 只 — 提取链可疑或当日未评估到; "
            f"计数已落台账供复查",
            file=sys.stderr,
        )

    # ── 9/24 增强批: counterfactual 统计 ──
    # habit_hint_would_resolve: 本日 ambiguous 行中习惯先验可解的只数
    # (consistency ≥ 0.75 且 n_history ≥ 3 — 先验足够强, 可作为升格裁决的读数载体)
    n_ambiguous = sum(1 for r in plan if r.get("state") == STATE_AMBIGUOUS)
    n_would_resolve = 0
    for r in plan:
        if r.get("state") != STATE_AMBIGUOUS:
            continue
        hint = r.get("habit_hint")
        if isinstance(hint, dict) and hint.get("consistency", 0) >= 0.75 and hint.get("n_history", 0) >= 3:
            n_would_resolve += 1

    result = {
        "et_date": et_date,
        "session": session,
        "coverage_pct": round(coverage_pct, 2),
        "plan_rows": len(plan),
        "event_tickets_seen": sorted(event_tickets),
        # ── P1-1 修复: 分开记录两路来源, 使"仪器是否活着"可复查 ──
        "ledger_seen": sorted(ledger_seen),
        "calendar_tickets": sorted(calendar_tickets),
        "missed": missed,
        "ambiguous_resolved": sorted(ambiguous_resolved),
        "actual_arrival_stats": arrival_stats,
        "verdict": verdict,
        # ── 9/24 增强批 ──
        "ambiguous_count": n_ambiguous,
        "habit_hint_would_resolve": n_would_resolve,
    }

    # 写产物
    PLAN_DIR.mkdir(parents=True, exist_ok=True)
    reconcile_path = PLAN_DIR / f"reconcile_{et_date}.json"
    atomic_write_json(str(reconcile_path), result)

    # append ledger
    ledger_path = PLAN_DIR / "reconcile_ledger.jsonl"
    ledger_line = json.dumps({
        "et_date": et_date,
        "session": session,
        "verdict": verdict,
        "coverage_pct": result["coverage_pct"],
        "plan_rows": len(plan),
        "event_tickets": len(event_tickets),
        "ledger_seen": len(ledger_seen),          # P1-1: 台账命中数 (仪器活性)
        "calendar_tickets": len(calendar_tickets),  # P1-1: 日历应见数 (真分母)
        "missed_count": len(missed),
        "reconciled_at": et_now().isoformat(timespec="seconds"),
    }, ensure_ascii=False) + "\n"
    try:
        with open(ledger_path, "a", encoding="utf-8") as f:
            f.write(ledger_line)
    except OSError as exc:
        logger.warning("reconcile ledger append failed: %s", exc, exc_info=True)

    return result


# ── CLI ──

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="RFC-104 阶段 0: ETH 事件执行计划生成器 (只读工件)"
    )
    ap.add_argument("--date", default=None,
                    help="ET 日期 YYYY-MM-DD (默认当日)")
    ap.add_argument("--session", required=True, choices=["premarket", "afterhours"],
                    help="交易时段")
    ap.add_argument("--dry", action="store_true",
                    help="只打印不落盘 (不连接 futu/OpenD)")
    ap.add_argument("--finnhub-only", action="store_true",
                    help="只拉 Finnhub (不连接 futu/OpenD; 离线降级用)")
    ap.add_argument("--refresh", action="store_true",
                    help="刷新模式: 重拉多源日历, 与既有计划合并 (state 只前进不回退)")
    ap.add_argument("--reconcile", action="store_true",
                    help="对账模式: 读台账 vs 计划, 产出覆盖率与漏票")
    ap.add_argument("--premarket-amc-pairing", action="store_true",
                    help="盘前接受昨日 AMC (同 executor 配置)")
    args = ap.parse_args(argv)

    et_date = args.date or et_today()
    session = args.session

    # ── 对账模式 ──
    if args.reconcile:
        print(f"[eth_plan] 对账 {et_date} {session}...", file=sys.stderr)
        result = reconcile(et_date, session)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        if result["verdict"] == "FAIL":
            print(f"[eth_plan] FAIL: {len(result['missed'])} 漏票 {result['missed']}",
                  file=sys.stderr)
            return 1
        if result["verdict"] == "NO_DATA":
            # P1-1: 当日无应见事件票 ⇒ 不是 PASS (不计入三影子的 PASS 日数)
            print("[eth_plan] NO_DATA: 当日无应见事件票 — 不计入 PASS 日数 "
                  "(仪器自检见 stderr)", file=sys.stderr)
            return 0
        print(f"[eth_plan] PASS: 覆盖率 {result['coverage_pct']}% "
              f"(应见 {len(result['event_tickets_seen'])} 只, 漏 0)", file=sys.stderr)
        return 0

    # ── 生成/刷新模式 ──
    print(f"[eth_plan] {'刷新' if args.refresh else '生成'} {et_date} {session}...",
          file=sys.stderr)

    # 同步 premarket_amc_pairing → EXEC 模块级常量 (import time 已固化)
    EXEC.PREMARKET_AMC_PAIRING = args.premarket_amc_pairing
    os.environ["EXT_ENTRY_PREMARKET_AMC_PAIRING"] = "true" if args.premarket_amc_pairing else "false"

    # ── 拉取日历源 ──
    prev_date = shift_et_day(et_date, -1)
    fh_rows = fetch_finnhub_calendar(et_date)
    if session == "premarket" and args.premarket_amc_pairing:
        fh_rows.extend(fetch_finnhub_calendar(prev_date))

    ft_rows: list[dict] = []
    if not args.dry and not args.finnhub_only:
        ft_rows = fetch_futu_calendar(prev_date, et_date)

    print(f"[eth_plan] 源: finnhub={len(fh_rows)} futu={len(ft_rows)}", file=sys.stderr)

    if args.refresh:
        # ── 刷新: 与既有计划合并 ──
        existing = load_plan(et_date, session)
        if not existing:
            print(f"[eth_plan] 无既有计划, 改为初次生成", file=sys.stderr)

        rows = refresh_plan(
            existing, et_date, session,
            finnhub_rows=fh_rows, futu_rows=ft_rows,
            premarket_amc_pairing=args.premarket_amc_pairing,
        )

        # 查询 actual 到达 (观测字段, 不影响策略)
        for row in rows:
            if row.get("actual_arrived_at") is None:
                actual = query_actual_value(row["symbol"], row.get("et_date", et_date))
                if actual is not None:
                    row["actual_arrived_at"] = et_now().isoformat(timespec="seconds")
                    row["actual_value"] = actual.get("epsActual")

        existing_keys = {(r["symbol"], r.get("et_date", ""), r.get("session", "")) for r in existing}
        n_new = sum(1 for r in rows
                    if (r["symbol"], r.get("et_date", ""), r.get("session", "")) not in existing_keys)
        n_withdrawn = sum(1 for r in rows if r.get("source_withdrawn_at"))
        n_updated = len(rows) - n_new
        print(f"[eth_plan] 刷新: {len(rows)} 行 (新增 {n_new}, 合并 {n_updated}, "
              f"源撤单保留 {n_withdrawn})", file=sys.stderr)
    else:
        # ── 正常生成 ──
        rows = generate_plan(
            et_date, session,
            finnhub_rows=fh_rows, futu_rows=ft_rows,
            premarket_amc_pairing=args.premarket_amc_pairing,
        )

        if not rows:
            print(f"[eth_plan] {et_date} {session} 无事件票 (两源皆空或无匹配相位)",
                  file=sys.stderr)
            if not args.dry:
                save_plan(et_date, session, [])
            return 0

        # 查询 actual 到达 (观测字段)
        for row in rows:
            actual = query_actual_value(row["symbol"], row.get("et_date", et_date))
            if actual is not None:
                row["actual_arrived_at"] = et_now().isoformat(timespec="seconds")
                row["actual_value"] = actual.get("epsActual")

    # ── 统计 ──
    n_ambiguous = sum(1 for r in rows if r["state"] == STATE_AMBIGUOUS)
    n_planned = sum(1 for r in rows if r["state"] == STATE_PLANNED)
    sources_count = {s for r in rows for s in (src["name"] for src in r["sources"])}
    n_actual = sum(1 for r in rows if r.get("actual_arrived_at") is not None)

    print(f"[eth_plan] 计划 {len(rows)} 行: planned={n_planned} ambiguous={n_ambiguous} "
          f"actual_arrived={n_actual}", file=sys.stderr)
    print(f"[eth_plan] 源覆盖: {sources_count}", file=sys.stderr)

    # ── 断言: 三处收紧合规 ──
    for r in rows:
        assert "price" not in r, "计划行不得含 price 字段 (收紧 §1)"
        assert "volume" not in r, "计划行不得含 volume 字段 (收紧 §1)"
        assert "mcap" not in r, "计划行不得含 mcap 字段 (收紧 §1)"
        assert "direction" not in r, "计划行不得含 direction 字段 (收紧 §3)"
        assert "sl_tp" not in r, "计划行不得含 sl_tp 字段 (收紧 §3)"
        assert "fee" not in r, "计划行不得含 fee 字段 (收紧 §3)"
        assert "expected_profit" not in r, "计划行不得含 expected_profit 字段 (收紧 §3)"

    # ── 落盘或打印 ──
    if args.dry:
        print(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
    else:
        p = save_plan(et_date, session, rows)
        print(f"[eth_plan] 已写入 {p}", file=sys.stderr)

    if args.refresh:
        print("[eth_plan] 注: actual 到达为观测字段, 不影响策略 (RFC-104 §2.2 宁少勿多)",
              file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
