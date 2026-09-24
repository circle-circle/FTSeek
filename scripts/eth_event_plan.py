#!/usr/bin/env python3
"""RFC-104 阶段 0: ETH 事件执行计划生成器 (只读工件, 零交易行为变更).

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
- 回滚: 删 cron 行 (计划只读工件, 删除即静默)

## 首跑说明
人工首跑: `PYTHONPATH=. venv/bin/python scripts/eth_event_plan.py --session premarket --dry`
验证: 输出行数 7-9 (非 44), 每行含 sources/state/phase, 不含 direction/sl/tp/fee。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env.us", override=True)

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

# ── 相位常量 ──
PHASE_BEFORE = "before"  # BMO
PHASE_AFTER = "after"    # AMC


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


# ── 日历源拉取 (复用 rth_fade_shadow.py 模式, 不触 futu/OpenD) ──

def fetch_finnhub_calendar(day: str) -> list[dict]:
    """Finnhub HTTP 日历查询 (不占 OpenD 槽位).

    返回原始行列表, 每行含 symbol/date/timeOfTheDay/asof。
    失败返回空列表 (fail-open at source level; 并集层兜底)。
    """
    import requests
    key = os.getenv("FINNHUB_API_KEY")
    if not key:
        return []
    try:
        r = requests.get("https://finnhub.io/api/v1/calendar/earnings",
                         params={"from": day, "to": day, "token": key}, timeout=30)
        rows = r.json().get("earningsCalendar", []) or []
        # 标准化: 加 asof 时间戳 (生成时刻)
        now_iso = et_now().isoformat(timespec="seconds")
        for row in rows:
            row["_src"] = "finnhub"
            row["_asof"] = now_iso
        return rows
    except Exception as exc:
        print(f"[eth_plan] finnhub 拉取失败 {day}: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return []


def fetch_futu_calendar(start: str, end: str) -> list[dict]:
    """futu 日历查询 (需 OpenD 连接; 阶段 0 验证用 --dry 跳过).

    返回标准化行列表。失败返回空列表。
    注意: 本函数会连接 futu/OpenD; --dry 模式或离线测试不应调用。
    """
    try:
        from futu import OpenQuoteContext, RET_OK
    except ImportError:
        print("[eth_plan] futu 未安装, 跳过 futu 日历源", file=sys.stderr)
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
        print(f"[eth_plan] futu 拉取失败 {start}~{end}: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return []


# ── 相位解析 (复用 extended_hours_entry_executor._parse_earnings_phase 内核) ──

def parse_earnings_phase(row: dict) -> tuple[bool, bool]:
    """日历相位 → (before, after)。空/未知相位两者皆 False。

    与 scripts/extended_hours_entry_executor.py:_parse_earnings_phase 逐字同式,
    防双实现漂移 (RFC-104 §5 历史坑: 双实现漂移)。
    """
    phase = str(
        row.get("timeOfTheDay", "") or row.get("pub_type", "")
    ).lower()
    before = "before" in phase or "pre" in phase or "bmo" in phase
    after = "after" in phase or "post" in phase or "amc" in phase
    return before, after


# ── session 配对表 (复用 extended_hours_entry_executor._session_event_pairs 内核) ──

def session_event_pairs(session: str, premarket_amc_pairing: bool = False) -> list[tuple[int, str]]:
    """session → 允许的 (日偏移, 相位) 列表。

    与 scripts/extended_hours_entry_executor.py:_session_event_pairs 逐字同式。
    """
    if session == "premarket":
        pairs = [(0, PHASE_BEFORE)]
        if premarket_amc_pairing:
            pairs.append((-1, PHASE_AFTER))
        return pairs
    return [(0, PHASE_AFTER)]


# ── 核心: 计划生成 ──

def _merge_sources(finnhub_rows: list[dict], futu_rows: list[dict]) -> dict[tuple[str, str], dict]:
    """合并两源日历, 按 (symbol, reportDate) 去重。

    返回 {(symbol, date): {"sources": [...], "phases": set()}} 结构。
    每源保留原始行供审计 (sources 字段)。
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
    """检测源冲突: 同一 (symbol, date) 两源给出不同相位。

    冲突定义: 存在两个不同源, 各自的 timeOfTheDay 解析出的 (before, after) 不一致。
    例: finnhub 说 "BMO" (before=True, after=False), futu 说 "AMC" (before=False, after=True)。
    """
    src_phases: dict[str, tuple[bool, bool]] = {}
    for src in entry.get("sources", []):
        name = src.get("name", "")
        # 构造临时行给 parse_earnings_phase
        fake_row = {"timeOfTheDay": src.get("timeOfTheDay", ""), "pub_type": ""}
        before, after = parse_earnings_phase(fake_row)
        if before or after:
            src_phases[name] = (before, after)

    if len(src_phases) < 2:
        return False
    # 两源相位不一致 = 冲突
    phase_values = list(src_phases.values())
    return len(set(phase_values)) > 1


def generate_plan(et_date: str, session: str, *, finnhub_rows: list[dict],
                  futu_rows: list[dict], premarket_amc_pairing: bool = False) -> list[dict]:
    """生成 T 日事件计划 (核心函数)。

    参数:
        et_date: ET 日期 (YYYY-MM-DD)
        session: "premarket" | "afterhours"
        finnhub_rows: Finnhub 日历原始行
        futu_rows: futu 日历标准化行
        premarket_amc_pairing: 盘前是否接受昨日 AMC (同 executor 配置)

    返回:
        计划行列表, 每行符合 RFC-104 §2.2 schema。

    三处收紧合规:
        1. 不预筛: 不过滤 price/volume/mcap; gate_snapshot 为空占位
        2. 不择优: 源冲突 → state=ambiguous
        3. 不是策略卡: 不含 direction/sl_tp/expected_profit/fee
    """
    merged = _merge_sources(finnhub_rows, futu_rows)
    pairs = session_event_pairs(session, premarket_amc_pairing)

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
            # 相位未知: 双窗待命或标记 ambiguous
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
        }

        # plan_hash: 幂等键的指纹 (symbol+et_date+session+phase)
        hash_input = f"{sym}|{et_date}|{session}|{phase_str}"
        plan_row["plan_hash"] = hashlib.sha256(hash_input.encode()).hexdigest()[:16]

        plan_rows.append(plan_row)

    return plan_rows


def _data_expectation(phase: str) -> dict:
    """数据预期: BMO → actual 约 08:05 ET 可得; AMC → 约 16:00 ET。

    复用 RFC-102 §2.6 的语义; 此处只给期望值, 不给放行结论。
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


# ── 文件 I/O ──

def plan_path(et_date: str, session: str) -> Path:
    """计划文件路径: data/loops/ext_entry_plan/<et_date>_<session>.json"""
    return PLAN_DIR / f"{et_date}_{session}.json"


def load_plan(et_date: str, session: str) -> list[dict]:
    """加载已有计划; 不存在返回空列表。"""
    p = plan_path(et_date, session)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_plan(et_date: str, session: str, rows: list[dict]) -> Path:
    """原子写入计划文件。"""
    p = plan_path(et_date, session)
    PLAN_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(p), rows)
    return p


# ── 状态机推进 (阶段 0 只推进 planned→armed, 由时刻窗触发) ──

def advance_state(row: dict, current_et_time: str) -> dict:
    """根据当前 ET 时刻推进状态机。

    阶段 0 的推进规则:
    - planned → armed: 相位窗到达 (BMO: 04:00 ET 之后; AMC: 16:00 ET 之后)
    - armed → waiting_data: 数据未到 (actual 未出现)
    - ambiguous: 保持不动, 等待源刷新或人工裁决

    其他状态 (ordered/rejected/expired) 由执行层推进 (阶段 1+)。
    """
    if row.get("state") == STATE_AMBIGUOUS:
        return row  # ambiguous 不自动推进

    if row.get("state") not in (STATE_PLANNED, STATE_ARMED):
        return row  # 终态不由此函数推进

    phase = row.get("phase", "")
    # 解析当前时间
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
                    help="只拉 Finnhub (不连接 futu/OpenD; 适合首跑验证)")
    ap.add_argument("--premarket-amc-pairing", action="store_true",
                    help="盘前接受昨日 AMC (同 executor 配置)")
    args = ap.parse_args(argv)

    et_date = args.date or et_today()
    session = args.session

    print(f"[eth_plan] 生成 {et_date} {session} 计划...", file=sys.stderr)

    # ── 拉取日历源 ──
    # Finnhub: HTTP 查询, 不占 OpenD 槽位
    prev_date = shift_et_day(et_date, -1)
    fh_rows = fetch_finnhub_calendar(et_date)
    if session == "premarket" and args.premarket_amc_pairing:
        fh_rows.extend(fetch_finnhub_calendar(prev_date))

    # futu: 需要 OpenD 连接; --dry/--finnhub-only 跳过
    ft_rows: list[dict] = []
    if not args.dry and not args.finnhub_only:
        ft_rows = fetch_futu_calendar(prev_date, et_date)

    print(f"[eth_plan] 源: finnhub={len(fh_rows)} futu={len(ft_rows)}", file=sys.stderr)

    # ── 生成计划 ──
    rows = generate_plan(
        et_date, session,
        finnhub_rows=fh_rows,
        futu_rows=ft_rows,
        premarket_amc_pairing=args.premarket_amc_pairing,
    )

    if not rows:
        print(f"[eth_plan] {et_date} {session} 无事件票 (两源皆空或无匹配相位)",
              file=sys.stderr)
        # 仍写空计划 (fail-closed: 消费侧看到空文件 = 不下单)
        if not args.dry:
            save_plan(et_date, session, [])
        return 0

    # ── 统计 ──
    n_ambiguous = sum(1 for r in rows if r["state"] == STATE_AMBIGUOUS)
    n_planned = sum(1 for r in rows if r["state"] == STATE_PLANNED)
    sources_count = {s for r in rows for s in (src["name"] for src in r["sources"])}

    print(f"[eth_plan] 计划 {len(rows)} 行: planned={n_planned} ambiguous={n_ambiguous}",
          file=sys.stderr)
    print(f"[eth_plan] 源覆盖: {sources_count}", file=sys.stderr)

    # ── 断言: 三处收紧合规 ──
    for r in rows:
        # 收紧 §1: 不含 price/volume/mcap 预筛字段
        assert "price" not in r, "计划行不得含 price 字段 (收紧 §1)"
        assert "volume" not in r, "计划行不得含 volume 字段 (收紧 §1)"
        assert "mcap" not in r, "计划行不得含 mcap 字段 (收紧 §1)"
        # 收紧 §3: 不含方向/SL/TP/费用
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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
