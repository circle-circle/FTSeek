#!/usr/bin/env python3
"""盘前/盘后事件驱动开仓安全入口 (2026-09-17, SIM fail-closed).

信号四层过滤 (宁缺毋滥, 用户原话"确定性因素第一时间执行"):
  1. 事件: Finnhub company_earnings 最近财报超预期 (surprisePercent>0) — "确定性"
  2. 价格: pre/after_price 相对昨收涨 ≥ +2% — 市场认可事件
  3. 量:   pre/after_volume ≥ 200,000 股 — 非薄量假动作
  4. 价格下限: ≥ MIN_PRICE ($5, 2026-09-17 用户裁方案A) — 挡仙股/妖股
     (实证: CTNT $0.073 +75.06% / GIPR $0.435 +19.75% 曾透过前 3 层; 仙股的"超预期>0"是季末噪声,
      而涨幅/量闸对妖股天然放行 ⇒ 日配额被噪声票用尽, 当晚真催化票开不出来)
当前 Futu SIM 不支持 STOP/STOP_LIMIT。没有券商侧止损时本脚本拒绝真实开仓；
dry 模式仅输出候选，不写 state、不下单。

护栏 (开仓风险高于锁盈, 更严):
  - 单笔仓位 $5000 (用户 9/17 拍板; 高低价股都覆盖)
  - 每日盘前+盘后合计 ≤MAX_PER_DAY 笔新仓 (2026-09-18 观察期=3; 防连环开仓)
  - 下单前盘口硬闸: spread/买一深度/买一漂移/预估往返费用 (数据缺失 fail-closed)
  - SIM 无券商 STOP/STOP_LIMIT 时真实开仓 fail-closed
  - REAL 环境未授权, 一律拒绝
  - 每标的每日一次 attempt, 失败不消耗开仓配额

与锁盈执行器共用: 独立 OpenQuoteContext (主服务 quote ctx 被占用, 勿复用).
用法: PYTHONPATH=. venv/bin/python3.11 scripts/extended_hours_entry_executor.py \
        --session premarket [--dry]
回滚: 删本脚本 + 对应 cron 行.
"""
import argparse
import contextlib
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

from app.core.trade_identity import ensure_test_symbol_not_in_production_db, is_test_symbol

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env.us")
except Exception:
    pass

# ── 用户 9/17 拍板参数 ──
POSITION_USD = 5000        # 单笔仓位
MAX_PER_DAY = int(os.getenv("EXT_ENTRY_MAX_PER_DAY", "3"))
# ↑ 盘前+盘后**合计**每日新仓上限. 2026-09-17 用户裁: 2 → 5 (原话"配额可以提升到5，再观察").
#   动机: 延长时段机会面比预期宽(官方榜 top50), 2 笔在盘前窗早段就被用尽 ⇒ 当晚真催化票开不出来;
#   叠加同日上线的价格下限闸($5 挡仙股)后, 单笔噪声风险已降, 放宽数量换取样本速度。
#   2026-09-18 观察期: env/代码默认均压到 3, 暴露上限 = 3 × POSITION_USD($5000)。
#   回滚: env EXT_ENTRY_MAX_PER_DAY=2 或 EXT_ENTRY_ALLOW_SIM=false。
# 盘前涨幅门槛。2026-09-17 回放: **边际跳空档 (涨幅 <3%) 是唯一负档** ——
#   现役口径 n=46: <3% 档 均值 -0.65%/WR 42.9%/最差 -6.86%; ≥3% 档 均值 +1.41%(含6-10%档 +3.27%)。
#   放宽口径 n=161 同向复现: <3% 档 均值 -0.33%/WR 39.4%/最差 -8.93%; ≥3% 档 均值 +1.32%。
#   ⇒ 提到 3.0 理论上把每笔均值 +0.26% → +1.32% (供给 161→57 笔; 现役口径 46→约 32 笔)。
#   ⚠️ 两样本**同窗口**(06-22→09-16) ⇒ 非真样本外; 按既定转正门槛(双窗口)未擅自改默认值。
#   开: EXT_ENTRY_CHANGE_PCT=3.0 (无需改码/重启)
ENTRY_CHANGE_PCT = float(os.getenv("EXT_ENTRY_CHANGE_PCT", "2.0"))
MIN_VOLUME = 200000        # 盘前量门槛
MIN_PRICE = float(os.getenv("EXT_ENTRY_MIN_PRICE", "5.0"))
MAX_SPREAD_PCT = float(os.getenv("EXT_ENTRY_MAX_SPREAD_PCT", "1.0"))
MAX_ROUND_TRIP_FEE_PCT = float(os.getenv("EXT_ENTRY_MAX_ROUND_TRIP_FEE_PCT", "1.0"))
MAX_PRICE_DRIFT_PCT = float(os.getenv("EXT_ENTRY_MAX_PRICE_DRIFT_PCT", "0.5"))
MIN_ASK_DEPTH_RATIO = float(os.getenv("EXT_ENTRY_MIN_ASK_DEPTH_RATIO", "1.0"))
# ↑ 价格下限闸 (2026-09-17 用户裁方案A): 挡 $5 以下仙股/妖股.
#   实证: CTNT $0.073(+75.06%, 量2217万, Finnhub超预期+105%) 与 GIPR $0.435(+19.75%)
#   曾透过"事件+涨幅+量"三闸 —— 仙股的第1闸(超预期>0)是季末噪声, 第2/3闸对妖股天然放行。
#   事故代价: 日配额 2 笔被噪声票用尽, 当晚真催化票开不出来。回滚=env EXT_ENTRY_MIN_PRICE=0。

# ── 候选源开关 (2026-09-17 用户令「需要」: 接入常见 watchlist 与财报机会) ──
#   架构: 每个源只产出候选结构 {symbol,px,chg,vol,prev_close}; 下游四闸/价格下限/配额/下单/
#   平仓/台账**零改动**。默认只有现役异动榜开; 新源等回放证据
#   (scripts/replay_ext_entry_sources.py, 用 K_5M+extended_time=True 取历史盘前 bar) 出来再开。
SOURCE_RANK_BOARD = os.getenv("EXT_ENTRY_SOURCE_RANK", "true").lower() == "true"
SOURCE_BEAT_RANK = os.getenv("EXT_ENTRY_SOURCE_BEAT_RANK", "false").lower() == "true"
SOURCE_WATCHLIST = os.getenv("EXT_ENTRY_SOURCE_WATCHLIST", "false").lower() == "true"
WATCHLIST_GROUP = os.getenv("EXT_ENTRY_WATCHLIST_GROUP", "美股")
BEAT_TOP_N = int(os.getenv("EXT_ENTRY_BEAT_TOP_N", "200"))
BEAT_RECENT_DAYS = int(os.getenv("EXT_ENTRY_BEAT_RECENT_DAYS", "7"))
BEAT_MIN_MCAP = float(os.getenv("EXT_ENTRY_BEAT_MIN_MCAP", "1000000000"))
SNAPSHOT_BATCH = 200
STOP_PCT = -5.0            # 止损距离 (%) — 开盘写入 initial_sl, 由规则引擎盘中兜底
SCAN_TOP_N = 50            # 盘前/盘后榜扫描深度 (9/17 扩覆盖面: 官方榜 top50, 旧为15只活跃股)
# 2026-09-17 用户裁方案1: SIM 无券商STOP → 接受规则引擎兜底, 开关恢复下单.
#   止损保障: ①开盘 monitor_playbook_sim_exits 读 initial_sl 兜底 ②盘前 tighten 信号由 exit executor 平仓.
# 2026-09-18 (Hermes §23 R8): 代码默认由 "true" 翻为 **"false"** (fail-closed) —
#   原默认是 fail-open: `.env.us` 缺失/未加载/键名写错 = **静默开仓**。开关现在真的在下单,
#   故默认方向必须偏安全侧 (宁不交易, 不乱交易)。当前行为不变: `.env.us:135` 显式
#   `EXT_ENTRY_ALLOW_SIM=true` 且 load_dotenv 先于本行执行 ⇒ 观察期照常开仓;
#   回滚 = 把本行默认改回 "true"。
ALLOW_SIM_ENTRY = os.getenv("EXT_ENTRY_ALLOW_SIM", "false").lower() == "true"
AUDIT = ROOT / "data" / "extended_hours_entry_executions.jsonl"
# 价格闸拦下的候选打点 (按**运行**聚合一行: n + 明细). 用户 2026-09-17 令: 回答
#   "$5 闸到底挡掉了多少噪声票". 逐候选整行会膨胀(同票每 5 分钟重复出现) ⇒ 按运行聚合.
PRICE_GATE_REJECTS = ROOT / "data" / "extended_hours_price_gate_rejects.jsonl"
PRETRADE_GATE_REJECTS = ROOT / "data" / "extended_hours_pretrade_gate_rejects.jsonl"
STATE = ROOT / "data" / "extended_hours_entry_state.json"
LOCK = ROOT / "data" / "extended_hours_entry_state.lock"
DB = ROOT / "data" / "ftseek_us.db"
SESSION_WINDOWS = {
    "premarket": (400, 929),
    "afterhours": (1600, 1959),
}

from scripts.extended_hours_order_lifecycle import (
    ACTIVE_ORDER_STATUSES,
    query_order_rows,
    wait_order_fill,
)


def _now_hhmm_et():
    now = _now_et()
    return now.hour * 100 + now.minute


def _in_session_window(session):
    start, end = SESSION_WINDOWS[session]
    return start <= _now_hhmm_et() <= end


def _is_trading_day():
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "trading_day_check.py"), "--market", "US"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def _now_et():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York"))


def _today_et():
    return _now_et().strftime("%Y-%m-%d")


def _load_state():
    if not STATE.exists():
        return {"version": 2, "opened_today": {}, "attempts": {}}
    try:
        state = json.loads(STATE.read_text())
    except Exception as exc:
        raise RuntimeError(f"extended-hours entry state unreadable: {exc}") from exc
    if not isinstance(state, dict):
        raise RuntimeError("extended-hours entry state is not a JSON object")
    return state


def _save_state(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(f".{os.getpid()}.tmp")
    with tmp.open("w") as handle:
        json.dump(s, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, STATE)


@contextlib.contextmanager
def _state_lock():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _push(text):
    try:
        from app.notifier.feishu import FeishuNotifier
        FeishuNotifier().send_text(text)
    except Exception:
        pass


_PUSH_DEDUP = ROOT / "data" / "ext_entry_push_dedup.json"


def _push_once(key, text):
    """同一 key **每个 ET 日只推一次** (2026-09-18)。

    病灶: _push 无去重, 而 US.TEST 这类**报废测试单**会被对账路径反复碰到 ⇒ 同一句拦截文案
    每轮都推 (用户反馈"一直持续推送虚拟标的")。本函数只做通知去重, 不改变拦截行为本身。
    """
    today = _today_et()
    try:
        data = json.loads(_PUSH_DEDUP.read_text()) if _PUSH_DEDUP.exists() else {}
    except Exception:
        data = {}
    if data.get(key) == today:
        return False
    _push(text)
    data[key] = today
    try:
        tmp = _PUSH_DEDUP.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        tmp.replace(_PUSH_DEDUP)
    except Exception:
        pass
    return True


def _audit(rec):
    rec["ts"] = datetime.utcnow().isoformat() + "Z"
    with AUDIT.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _opened_today_total(opened_today, today):
    """当日(ET)已开仓计数 = **盘前 + 盘后合并** (key 形如 '2026-09-17:premarket').

    用户 9/17 令"配额提升到 5"针对的是**每日合计**, 不是单时段各 5 ⇒ 两个时段共享同一个上限。
    跨日残留 key 必须忽略 (state 里会留历史日期的 key, 用 startswith 过滤)。
    """
    return sum(value for key, value in opened_today.items() if key.startswith(today))


def _record_price_gate_rejects(session, rejects):
    """价格闸拦下的候选打点 + 日志一行 (用户 2026-09-17 令: 量化"$5 闸挡掉多少噪声票").

    按**运行**聚合: 一行 = 一次运行, 字段 `n` + `rejects` 明细 (symbol/price/change_pct/volume)。
    这样既能按日求和(回答"挡了多少"), 又保留标的身份(回答"挡的是谁"), 且不会让 jsonl 因
    同一只票每 5 分钟重复出现而膨胀。异常不得影响主流程。
    """
    if not rejects:
        return
    try:
        PRICE_GATE_REJECTS.parent.mkdir(parents=True, exist_ok=True)
        with PRICE_GATE_REJECTS.open("a") as f:
            f.write(json.dumps({
                "ts": datetime.utcnow().isoformat() + "Z",
                "session": session,
                "n": len(rejects),
                "rejects": rejects,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass
    head = ", ".join(f"{r['symbol']} ${r['price']:g}" for r in rejects[:8])
    more = f" (+{len(rejects) - 8})" if len(rejects) > 8 else ""
    print(f"[ext_entry] 价格闸(<${MIN_PRICE:g}) 拦下 {len(rejects)} 只: {head}{more}")


def _price_in_range(price):
    """价格闸 (2026-09-17 用户裁方案A 加下限).

    下限 MIN_PRICE: 挡 $5 以下仙股/妖股 (实证 CTNT $0.073 / GIPR $0.435 曾透过前三闸)。
    上限 POSITION_USD: 单笔仓位 $5000, 超过则连 1 股都买不起。
    `price > 0` 在 MIN_PRICE 被 env 调成 0 时仍是必要的兜底。
    """
    return price > 0 and MIN_PRICE <= price <= POSITION_USD


def _snapshot_number(row, key):
    try:
        return float(row.get(key, 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _liquidity_quote(qctx, symbol):
    from futu import RET_OK
    try:
        ret, snapshot = qctx.get_market_snapshot([symbol])
    except Exception:
        return None
    if ret != RET_OK or snapshot is None or not hasattr(snapshot, "iloc") or snapshot.empty:
        return None
    row = snapshot.iloc[0]
    bid = _snapshot_number(row, "bid_price")
    ask = _snapshot_number(row, "ask_price")
    ask_volume = _snapshot_number(row, "ask_vol")
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    spread_pct = (ask - bid) / ((bid + ask) / 2.0) * 100.0
    return {
        "bid": bid,
        "ask": ask,
        "ask_volume": ask_volume,
        "spread_pct": round(spread_pct, 4),
    }


def _rounded_order_price(price):
    if price <= 0:
        return 0.0
    return round(price, 3 if price < 1.0 else 2)


def _pretrade_gate(decision_price, qty, quote, trade_date):
    if not quote:
        return None, "quote_missing"
    ask = float(quote["ask"])
    spread_pct = float(quote["spread_pct"])
    if spread_pct > MAX_SPREAD_PCT:
        return None, "spread_too_wide"
    if decision_price > 0 and ask > decision_price * (1.0 + MAX_PRICE_DRIFT_PCT / 100.0):
        return None, "price_drift_too_high"
    if qty > 0 and float(quote["ask_volume"]) < qty * MIN_ASK_DEPTH_RATIO:
        return None, "insufficient_ask_depth"

    from app.execution.fee_model import estimate_round_trip
    fee = estimate_round_trip(qty, ask, ask, "LONG", trade_date)
    notional = qty * ask
    fee_pct = fee["total"] / notional * 100.0 if notional > 0 else float("inf")
    if fee_pct > MAX_ROUND_TRIP_FEE_PCT:
        return None, "round_trip_fee_too_high"

    return {
        "order_price": _rounded_order_price(ask),
        "bid": quote["bid"],
        "ask": ask,
        "ask_volume": float(quote["ask_volume"]),
        "spread_pct": spread_pct,
        "round_trip_fee_est": fee["total"],
        "round_trip_fee_pct": round(fee_pct, 4),
    }, None


def _record_pretrade_gate_rejects(session, rejects):
    if not rejects:
        return
    try:
        PRETRADE_GATE_REJECTS.parent.mkdir(parents=True, exist_ok=True)
        with PRETRADE_GATE_REJECTS.open("a") as handle:
            handle.write(json.dumps({
                "ts": datetime.utcnow().isoformat() + "Z",
                "session": session,
                "n": len(rejects),
                "rejects": rejects,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _scan_movers(session, qctx):
    """全市场盘前/盘后榜 (富途官方接口, 2026-09-17 实测). 返回 [{symbol,px,chg,vol,prev_close}].
    替代旧"近5日活跃股"扫描 — 覆盖面从 15 只 → 全市场真实盘前/盘后异动榜.
    字段: security / pre_market_price / pre_market_change_ratio / pre_market_volume / close_price."""
    from futu import RET_OK
    try:
        if session == "premarket":
            ret, rd = qctx.get_us_pre_market_rank(count=SCAN_TOP_N)
            pk, ck, vk = "pre_market_price", "pre_market_change_ratio", "pre_market_volume"
        else:
            ret, rd = qctx.get_us_after_hours_rank(count=SCAN_TOP_N)
            pk, ck, vk = "after_hours_price", "after_hours_change_ratio", "after_hours_volume"
        if ret != RET_OK or not isinstance(rd, tuple) or len(rd) < 2:
            return []
        df = rd[1]
        if df is None or not hasattr(df, "columns") or df.empty:
            return []
        out = []
        for _, r in df.iterrows():
            sec = str(r.get("security", ""))
            if not sec.startswith("US."):
                continue
            try:
                out.append({
                    "symbol": sec,
                    "px": float(r.get(pk, 0) or 0),
                    "chg": float(r.get(ck, 0) or 0),
                    "vol": float(r.get(vk, 0) or 0),
                    "prev_close": float(r.get("close_price", 0) or 0),
                })
            except Exception:
                continue
        return out
    except Exception:
        return []


def _beat_rank_symbols(qctx):
    """事件源: 财报**超预期**榜 → 候选代码 (2026-09-17 实弹探针).

    实测: `get_earnings_beat_rank('US', BeatType.EPS)` total=1455 条, 字段含
    `released_date` / `market_cap` / `beat_ratio` / `earning_day_chg` / `detail_post_period`(BEFORE|AFTER)。
    取到的 200 条里 200/200 价格≥$5 且市值≥$10B ⇒ 该榜天然是大中盘 (与"异动榜=仙股盘"互补)。
    两道必须的服务端外过滤: ①`released_date` 近 BEAT_RECENT_DAYS 日 —— 不加就是跟过期事件交易
    (实测默认前几条是 1-2 个月前披露的 GOOGL/AAPL/NVDA); ②市值下限 —— 挡微盘噪声。
    """
    from futu import RET_OK, BeatType
    try:
        ret, rd = qctx.get_earnings_beat_rank("US", BeatType.EPS, count=BEAT_TOP_N)
        if ret != RET_OK or not isinstance(rd, tuple) or len(rd) < 2:
            return []
        df = rd[1]
        if df is None or not hasattr(df, "columns") or df.empty:
            return []
        cutoff = (_now_et().date() - timedelta(days=BEAT_RECENT_DAYS)).strftime("%Y-%m-%d")
        out = []
        for _, r in df.iterrows():
            sec = str(r.get("security", ""))
            if not sec.startswith("US."):
                continue
            if str(r.get("released_date", "")) < cutoff:
                continue
            try:
                if float(r.get("market_cap", 0) or 0) < BEAT_MIN_MCAP:
                    continue
            except Exception:
                continue
            out.append(sec)
        return out
    except Exception:
        return []


def _watchlist_symbols(qctx):
    """自选股源: 用户在富途 App 里的自选分组 (用户 curation, 非发现机制).

    实测: `get_user_security('美股')` ret=0, 51 只, 抽样 50/50 全部 ≥$5 (AMD/NVDA/AAPL/TSLA/MRNA/LITX…)
    ⇒ 零仙股噪声。注意选择性偏差: 它是你的注意力集合, 适合做"信任池"。
    """
    try:
        ret, df = qctx.get_user_security(WATCHLIST_GROUP)
        if ret != 0 or df is None or not hasattr(df, "columns") or df.empty:
            return []
        col = "code" if "code" in df.columns else df.columns[0]
        return [str(s) for s in df[col].tolist() if str(s).startswith("US.")]
    except Exception:
        return []


def _snapshot_candidates(session, qctx, symbols):
    """把任意代码列表 → 候选结构 {symbol,px,chg,vol,prev_close} (盘前取 pre_* 字段).

    与 `_scan_movers` 的口径一致: px = 盘前/盘后价, chg = vs 昨收 %, vol = 盘前/盘后量。
    """
    from futu import RET_OK
    out, syms = [], [s for s in dict.fromkeys(symbols) if s.startswith("US.")]
    for i in range(0, len(syms), SNAPSHOT_BATCH):
        batch = syms[i:i + SNAPSHOT_BATCH]
        try:
            ret, snap = qctx.get_market_snapshot(batch)
        except Exception:
            continue
        if ret != RET_OK or snap is None or not hasattr(snap, "columns"):
            continue
        for _, r in snap.iterrows():
            sec = str(r.get("code", ""))
            if not sec.startswith("US."):
                continue
            try:
                prev = float(r.get("prev_close_price", 0) or 0)
                if session == "premarket":
                    px = float(r.get("pre_price", 0) or 0) or float(r.get("last_price", 0) or 0)
                    vol = float(r.get("pre_volume", 0) or 0)
                else:
                    px = float(r.get("after_price", 0) or 0) or float(r.get("last_price", 0) or 0)
                    vol = float(r.get("after_volume", 0) or 0)
                chg = ((px / prev - 1) * 100) if (px > 0 and prev > 0) else 0.0
                out.append({"symbol": sec, "px": px, "chg": chg, "vol": vol, "prev_close": prev})
            except Exception:
                continue
    return out


def _scan_candidates(session, qctx):
    """多源候选合并 + 去重 (2026-09-17). 源顺序 = 优先级 (先到先得, 保留先出现的结构).

    这是"换源"的唯一改动点: 下游四闸 / 价格下限 / 配额 / 下单 / 平仓 / 台账全部复用, 一行不改。
    """
    merged, tag_of = [], {}
    skipped_test = []

    def _add(items, tag):
        for it in items:
            sym = it.get("symbol")
            if not sym or sym in tag_of:
                continue
            # 2026-09-18 (DEB-20260918 §23.5, 源头修): 占位标的 (TEST / US.TEST / *.TEST)
            #   在**候选源汇合口**就挡掉 —— 它们不是真候选股, 只可能来自单测 artifact 或券商
            #   账户里的报废测试单 (如 US.TEST order 8185070)。此前只在 `_execute` 深处挡,
            #   占位标的仍会走完四闸、占 attempt 键、进审计/推送路径。
            if is_test_symbol(sym):
                skipped_test.append(sym)
                continue
            tag_of[sym] = tag
            merged.append(it)

    if SOURCE_RANK_BOARD:
        _add(_scan_movers(session, qctx), "rank")
    extra = []
    if SOURCE_BEAT_RANK:
        extra += _beat_rank_symbols(qctx)
    if SOURCE_WATCHLIST:
        extra += _watchlist_symbols(qctx)
    new_syms = [s for s in dict.fromkeys(extra) if s not in tag_of]
    if new_syms:
        _add(_snapshot_candidates(session, qctx, new_syms), "snapshot")
    if skipped_test:
        print(f"[ext_entry] 候选源忽略占位标的: {', '.join(sorted(set(skipped_test)))}")
    if merged:
        n_extra = sum(1 for v in tag_of.values() if v != "rank")
        print(f"[ext_entry] 候选源合并: {len(merged)} 只 (异动榜 {len(merged) - n_extra} / "
              f"财报超预期榜+自选股 {n_extra}) [开关 rank={SOURCE_RANK_BOARD} "
              f"beat={SOURCE_BEAT_RANK} watchlist={SOURCE_WATCHLIST}]")
    return merged


def _held_symbols():
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
        rows = conn.execute("SELECT symbol FROM open_positions WHERE size > 0").fetchall()
        conn.close()
        return {r[0] for r in rows}
    except Exception as exc:
        raise RuntimeError(f"open_positions read failed, refusing entry: {exc}") from exc


def _open_trade_context():
    from futu import OpenSecTradeContext, TrdMarket, SecurityFirm

    return OpenSecTradeContext(
        host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
        port=int(os.getenv("FUTU_OPD_PORT", "11111")),
        filter_trdmarket=TrdMarket.US,
        security_firm=SecurityFirm.NONE,
    )


def _order_et_date(create_time):
    """broker 订单 create_time → ET 日期串.

    2026-09-17 实测钉死时区: 订单 8184987 的 broker create_time = '2026-09-16 23:14:45',
    对应审计 jsonl 的 UTC '2026-09-17T03:16:16Z' → 两者相差 4h15m ≈ ET(-4) 语义, 故 create_time 是 **ET**。
    解析失败返回 "" ⇒ 视为非当日 (绝不写 attempt, 宁可漏记不可误封锁)。
    """
    try:
        return datetime.strptime(str(create_time).strip()[:19], "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d")
    except Exception:
        return ""


def _order_session(row, fallback_session):
    create_time = row.get("create_time", "")
    try:
        hour = int(create_time[11:13])
        if 4 <= hour < 12:
            return "premarket"
        if 16 <= hour < 20:
            return "afterhours"
    except (ValueError, IndexError):
        pass
    return fallback_session


def _reconcile_broker_entry_orders(
    tctx, env, acc_id, fallback_session, state=None, today=None
):
    """Recover terminal ext-entry fills and expose active ext-entry orders.

    This is read-only for broker state. It never cancels an order. Filled orders
    without a local owner row are written once; conflicts are returned for triage.
    """
    rows = []
    skipped_test = []
    for row in query_order_rows(tctx, env, acc_id):
        if row.get("remark") != "ext_entry":
            continue
        # 2026-09-18 (§23.5): 占位标的订单在**对账口**就丢弃 —— 否则它们会被写成当日 attempt
        #   (静默封锁该标的)、计入 active_orders/conflicts、或在成交路径触发建仓回补。
        #   实测 US.TEST order 8185070 的报废单每天都出现在这里的全量订单表。
        if is_test_symbol(row.get("symbol")):
            skipped_test.append(f"{row.get('symbol')}:{row.get('order_id')}")
            continue
        rows.append(row)
    if skipped_test:
        print(f"[ext_entry] 对账忽略占位标的订单: {', '.join(skipped_test)}")
    active_orders = []
    conflicts = []
    for row in rows:
        # 2026-09-17 修复 (Hermes 复查实证): 跨日旧单不得写 {today}:{symbol} attempt.
        #   病灶: 本函数此前对**所有** remark=ext_entry 的历史单无条件写当日 attempt,
        #   而采集循环 431 行 `if attempt_key in attempts: continue` ⇒ 只要该标的历史上被下过
        #   ext_entry 单, 今天就被**静默跳过**(实测 US.NOK 因 ET 9/16 23:14 的旧测试单在 9/17 被封锁,
        #   且因每日重复回填而永久封锁 = 供给静默饿死). 修复: 只看订单自身 ET 日期是否为当日.
        #   活动单仍**无条件**计入 active_orders — 活单本来就该拦住新开仓, 与日期无关.
        # today 为空 = 调用方未要求按日过滤 (如 _execute 下单前的安全闸, 不传 state/today)
        #   ⇒ 保持旧行为(全量处理), 否则会把当天单也误判"跨日"而跳过。
        row_is_today = (not today) or (_order_et_date(row.get("create_time")) == today)
        attempt = None
        if row_is_today and isinstance(state, dict) and today:
            attempts = state.setdefault("attempts", {})
            attempt_key = f"{today}:{row['symbol']}"
            attempt = attempts.setdefault(attempt_key, {})
            attempt.update({
                "session": _order_session(row, fallback_session),
                "symbol": row["symbol"],
                "trade_date": today,
                "order_id": row["order_id"],
                "broker_status": row["status"],
                "updated_at": datetime.utcnow().isoformat() + "Z",
            })
        if row["status"] in ACTIVE_ORDER_STATUSES:
            if attempt is not None:
                attempt["status"] = "broker_active"
            active_orders.append(
                {
                    "symbol": row["symbol"],
                    "order_id": row["order_id"],
                    "status": row["status"],
                    "qty": row["qty"],
                    "filled_qty": row["filled_qty"],
                }
            )
            continue
        if not row_is_today:
            # 跨日旧单: 不写 attempt(防静默封锁), 也不按今日回填建仓(entry_time 会是错的).
            #   本链路未成交单 90s 内即撤, 正常不会有跨日成交; 真出现则只留痕, 由人工处置.
            print(f"[ext_entry] 对账跳过跨日旧单: {row['symbol']} {row['order_id']} "
                  f"status={row['status']} create_time={row.get('create_time')}")
            continue
        if row["filled_qty"] <= 0 or row["avg_fill_price"] <= 0:
            if attempt is not None:
                attempt["status"] = "failed"
                attempt["error"] = f"broker_{row['status']}"
            continue
        conn = sqlite3.connect(DB, timeout=30)
        try:
            existing = conn.execute(
                "SELECT id FROM trade_records WHERE futu_order_id=? "
                "AND owner_strategy='extended_hours_entry' LIMIT 1",
                (row["order_id"],),
            ).fetchone()
        finally:
            conn.close()
        if existing:
            if attempt is not None:
                attempt["status"] = "opened"
                attempt["filled_qty"] = row["filled_qty"]
                attempt["avg_fill_price"] = row["avg_fill_price"]
            continue
        try:
            position_id = _write_open_position(
                symbol=row["symbol"],
                qty=row["filled_qty"],
                entry_px=row["avg_fill_price"],
                decision_px=row["price"],
                session=_order_session(row, fallback_session),
                earn={},
                chg=0.0,
                vol=0.0,
                buy_order_id=row["order_id"],
            )
            if position_id and isinstance(state, dict) and today:
                reconciled_session = _order_session(row, fallback_session)
                state.setdefault("opened_today", {})
                reconciled_key = f"{today}:{reconciled_session}"
                state["opened_today"][reconciled_key] = (
                    state["opened_today"].get(reconciled_key, 0) + 1
                )
        except Exception as exc:
            if attempt is not None:
                attempt["status"] = "reconcile_conflict"
                attempt["error"] = str(exc)
            conflicts.append(
                {
                    "symbol": row["symbol"],
                    "order_id": row["order_id"],
                    "error": f"reconcile_write_failed:{exc}",
                }
            )
    return active_orders, conflicts


def _earnings_surprise(symbol):
    """Finnhub 最近财报超预期% (positive=beat). 事件门: 超预期>0 即过 (挡无财报的妖股).
    2026-09-17 用户裁方案A: 删新鲜度门 — period 是季度末非披露日, 新鲜度会误拒真催化;
    "当下正在反应"由价格门(涨≥2%)+量门(≥20万)承载, 财报只需证明"有基本面支撑"."""
    try:
        from app.data.premarket_enrichment import _get_finnhub_client
        c = _get_finnhub_client()
        if c is None:
            return None
        tick = symbol.replace("US.", "")
        e = c.company_earnings(tick, limit=1)
        if e and isinstance(e, list):
            sp = e[0].get("surprisePercent")
            period = e[0].get("period")
            return {"surprise_pct": sp, "period": period}
    except Exception:
        pass
    return None


def collect(session, dry=False):
    """扫描事件驱动开仓信号；当前 SIM 无券商 STOP 时 fail-closed。"""
    if session not in SESSION_WINDOWS:
        return [{"ok": False, "skip": "invalid_session"}]
    if not _is_trading_day() or not _in_session_window(session):
        return [{"ok": False, "skip": "outside_trading_window"}]

    # Futu SIM 已实测拒绝 STOP 与 STOP_LIMIT（DEB-20260805 §5.1）。
    # 2026-09-17 用户裁方案1: 接受规则引擎兜底止损, ALLOW_SIM_ENTRY 开关恢复下单.
    #   券商 STOP 不可用 → 止损改由 ①开盘规则引擎读 initial_sl ②盘前 tighten 由 exit executor 平仓.
    if not dry and not ALLOW_SIM_ENTRY and os.getenv("FUTU_TRD_ENV", "SIMULATE").upper() == "SIMULATE":
        return [{"ok": False, "skip": "simulate_stop_unsupported_fail_closed"}]

    if dry:
        held = _held_symbols()
        results = []
        pretrade_rejects = []
        today = _today_et()
        from futu import OpenQuoteContext
        qctx = OpenQuoteContext(host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
                                port=int(os.getenv("FUTU_OPD_PORT", "11111")))
        price_gate_rejects = []
        try:
            movers = _scan_candidates(session, qctx)
            for item in movers:
                symbol, price, change_pct, volume = item["symbol"], item["px"], item["chg"], item["vol"]
                if symbol in held:
                    continue
                if not _price_in_range(price):
                    price_gate_rejects.append({"symbol": symbol, "price": price,
                                               "change_pct": round(change_pct, 2), "volume": volume})
                    continue
                if change_pct < ENTRY_CHANGE_PCT or volume < MIN_VOLUME:
                    continue
                earnings = _earnings_surprise(symbol)
                if not earnings or not earnings.get("surprise_pct") or earnings["surprise_pct"] <= 0:
                    continue
                qty = int(POSITION_USD / price)
                quote = _liquidity_quote(qctx, symbol)
                gate, reject_reason = _pretrade_gate(price, qty, quote, today)
                if gate is None:
                    pretrade_rejects.append({
                        "symbol": symbol,
                        "decision_price": price,
                        "qty": qty,
                        "reason": reject_reason,
                        "quote": quote,
                    })
                    continue
                order_price = gate["order_price"]
                qty = int(POSITION_USD / order_price)
                results.append({
                    "ok": True, "dry": True, "symbol": symbol,
                    "qty": qty, "price": order_price, "decision_price": price,
                    "session": session, "change_pct": round(change_pct, 2),
                    "volume": volume, "earnings": earnings,
                    "pretrade": gate,
                })
            _record_price_gate_rejects(session, price_gate_rejects)
            _record_pretrade_gate_rejects(session, pretrade_rejects)
        finally:
            qctx.close()
        return results or [{"skip": "no_signal_passed_all_gates"}]

    # REAL 通道当前未被本脚本授权；避免误用 SIM account 下 REAL 单。
    if os.getenv("FUTU_TRD_ENV", "SIMULATE").upper() != "SIMULATE":
        return [{"ok": False, "skip": "real_not_authorized"}]

    today = _today_et()
    with _state_lock() as locked:
        if not locked:
            return [{"ok": False, "skip": "another_executor_running"}]
        state = _load_state()
        state.setdefault("version", 2)
        state.setdefault("attempts", {})
        state.setdefault("opened_today", {})
        from futu import TrdEnv
        reconciliation_tctx = _open_trade_context()
        try:
            active_orders, reconcile_conflicts = _reconcile_broker_entry_orders(
                reconciliation_tctx, TrdEnv.SIMULATE,
                int(os.getenv("SIMULATE_ACCOUNT_ID", "19930823")), session,
                state=state, today=today,
            )
        except Exception as exc:
            return [{
                "ok": False,
                "skip": "broker_order_reconcile_failed",
                "error": str(exc),
            }]
        finally:
            reconciliation_tctx.close()
        if reconcile_conflicts:
            _save_state(state)
            return [{
                "ok": False,
                "skip": "broker_order_reconcile_conflict",
                "orders": reconcile_conflicts,
            }]
        if active_orders:
            _save_state(state)
            return [{
                "ok": False,
                "skip": "active_ext_entry_order",
                "orders": active_orders,
            }]
        _save_state(state)
        attempts = state["attempts"]
        opened_today = state["opened_today"]
        day_key = f"{today}:{session}"
        total_today = _opened_today_total(opened_today, today)
        if total_today >= MAX_PER_DAY:
            return [{"skip": "daily_limit", "total_today": total_today}]

        held = _held_symbols()
        results = []
        price_gate_rejects = []
        pretrade_rejects = []
        from futu import OpenQuoteContext
        qctx = OpenQuoteContext(host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
                                port=int(os.getenv("FUTU_OPD_PORT", "11111")))
        try:
            movers = _scan_candidates(session, qctx)
            for item in movers:
                symbol, price, change_pct, volume = item["symbol"], item["px"], item["chg"], item["vol"]
                attempt_key = f"{today}:{symbol}"
                if attempt_key in attempts:
                    continue
                if symbol in held:
                    continue
                if not _price_in_range(price):
                    price_gate_rejects.append({"symbol": symbol, "price": price,
                                               "change_pct": round(change_pct, 2), "volume": volume})
                    continue
                if change_pct < ENTRY_CHANGE_PCT or volume < MIN_VOLUME:
                    continue
                earnings = _earnings_surprise(symbol)
                if not earnings or not earnings.get("surprise_pct") or earnings["surprise_pct"] <= 0:
                    continue

                qty = int(POSITION_USD / price)
                quote = _liquidity_quote(qctx, symbol)
                gate, reject_reason = _pretrade_gate(price, qty, quote, today)
                if gate is None:
                    pretrade_rejects.append({
                        "symbol": symbol,
                        "decision_price": price,
                        "qty": qty,
                        "reason": reject_reason,
                        "quote": quote,
                    })
                    continue
                order_price = gate["order_price"]
                qty = int(POSITION_USD / order_price)
                attempts[attempt_key] = {
                    "status": "reserved", "session": session, "symbol": symbol,
                    "trade_date": today, "reserved_at": datetime.utcnow().isoformat() + "Z",
                }
                _save_state(state)
                try:
                    result = _execute(symbol, qty, order_price, session, change_pct, volume, earnings, dry)
                    result["decision_price"] = price
                    result["pretrade"] = gate
                except Exception as exc:
                    result = {
                        "ok": False, "symbol": symbol, "session": session,
                        "error": f"executor_exception:{exc}",
                    }
                    _push(f"❌ 延长时段开仓执行器异常\n{symbol}\n{exc}")
                    _audit(result)
                result["symbol"] = symbol
                result["session"] = session
                attempts[attempt_key] = {
                    "status": "opened" if result.get("ok") else "failed",
                    "session": session,
                    "trade_date": today,
                    "order_id": result.get("buy_order_id"),
                    "stop_order_id": result.get("stop_order_id"),
                    "filled_qty": result.get("filled_qty"),
                    "avg_fill_price": result.get("avg_fill_price"),
                    "error": result.get("error") or result.get("skip"),
                    "updated_at": datetime.utcnow().isoformat() + "Z",
                }
                results.append(result)
                if result.get("ok"):
                    total_today += 1
                    opened_today[day_key] = opened_today.get(day_key, 0) + 1
                _save_state(state)
                if total_today >= MAX_PER_DAY:
                    break
            _record_price_gate_rejects(session, price_gate_rejects)
            _record_pretrade_gate_rejects(session, pretrade_rejects)
        finally:
            qctx.close()
    return results or [{"skip": "no_signal_passed_all_gates"}]





def _write_open_position(symbol, qty, entry_px, decision_px, session, earn, chg, vol, buy_order_id=""):
    """成交后写 open_positions + trade_records (盘中监控读 trade_records.metadata_json.stop_loss).
    返回 position_id. 幂等且不覆盖既有仓位."""
    import uuid
    ensure_test_symbol_not_in_production_db(symbol, DB)
    conn = sqlite3.connect(DB, timeout=30)
    try:
        now = _now_et().isoformat()
        pos_id = f"EXT-{uuid.uuid4().hex[:12]}"
        init_sl = round(entry_px * (1 + STOP_PCT / 100), 2)  # 规则引擎盘中兜底读这个
        meta = json.dumps({"session": session, "decision_px": decision_px,
                           "position_id": pos_id, "entry_order_id": buy_order_id,
                           "earnings_surprise_pct": earn.get("surprise_pct"),
                           "earnings_period": earn.get("period"),
                           "ext_change_pct": chg, "ext_volume": vol,
                           "cohort": "B",  # 2026-09-17 用户裁: EXT 仓可隔夜 (cohort B + PLAYBOOK_SIM 默认允许, 盈利跳过 PRE_CLOSE_EXIT)
                           "stop_loss": init_sl,           # 盘中 monitor_playbook_sim_exits 读此键做 SL
                           "stop_source": "rule_engine_initial_sl"}, ensure_ascii=False)
        conn.execute("BEGIN IMMEDIATE")
        existing_order = None
        if buy_order_id:
            existing_order = conn.execute(
                "SELECT id FROM trade_records WHERE futu_order_id=? "
                "AND owner_strategy='extended_hours_entry' LIMIT 1",
                (buy_order_id,),
            ).fetchone()
        if existing_order:
            conn.commit()
            existing_position = conn.execute(
                "SELECT position_id FROM open_positions WHERE symbol=? AND size>0",
                (symbol,),
            ).fetchone()
            return existing_position[0] if existing_position else pos_id
        existing_position = conn.execute(
            "SELECT 1 FROM open_positions WHERE symbol=? AND size>0", (symbol,)
        ).fetchone()
        if existing_position:
            conn.rollback()
            raise RuntimeError(f"open position already exists for {symbol}")
        conn.execute(
            "INSERT INTO open_positions (position_id, symbol, direction, size, avg_entry_price, entry_time, "
            " strategy_id, source, owner_strategy, owner, mode, account_type, trader_type, "
            " is_filled, version, updated_at, metadata_json, sync_status, "
            " initial_sl, current_sl, stop_loss_price) "
            "VALUES (?, ?, 'LONG', ?, ?, ?, 'extended_hours_entry', 'extended_hours_entry', "
            " 'extended_hours_entry', 'extended_hours_entry', 'normal', 'simulate', 'AUTO', 1, 1, ?, ?, 'pending', "
            " ?, ?, ?)",
            (pos_id, symbol, qty, entry_px, now, now, meta, init_sl, init_sl, init_sl))
        # P0-3 完整闭环: 写 trade_records — 盘中退出监控只读此表 (source_type PLAYBOOK_SIM 未平仓)
        conn.execute(
            "INSERT INTO trade_records (trade_date, symbol, direction, entry_time, entry_price, "
            " quantity, source_type, trade_mode, mode, owner_strategy, strategy_id, "
            " futu_order_id, metadata_json, created_at, exit_price, exit_time, config_version, "
            " entry_session) "
            "VALUES (?, ?, 'LONG', ?, ?, ?, 'PLAYBOOK_SIM', 'simulate', 'futu_simulate', "
            " 'extended_hours_entry', 'extended_hours_entry', ?, ?, ?, 0, '', 'v2', ?)",
            # config_version='v2': 与规范写入方 app/storage/trade_store.py 一致。该表 schema 是
            # DEFAULT 'v1'，本列不写会落默认值 ⇒ W1 冻结窗出现版本分裂假象 (2026-09-18 F7 修复)。
            (_today_et(), symbol, now, entry_px, qty, buy_order_id, meta, now, session))
        conn.commit()
        return pos_id
    finally:
        conn.close()


def _execute(symbol, qty, price, session, chg, vol, earn, dry):
    """SIM 真下单: 买入→等成交→写 open_positions(含 initial_sl 规则引擎兜底). 用户裁方案1.
    SIM 无券商STOP, 止损靠 ①开盘 monitor_playbook_sim_exits 读 initial_sl ②盘前 tighten exit executor."""
    trd_env = os.getenv("FUTU_TRD_ENV", "SIMULATE")
    acc_id = int(os.getenv("SIMULATE_ACCOUNT_ID", "19930823"))
    # 2026-09-18: 测试占位标的 (TEST/US.TEST/*.TEST) 在**源头**挡掉 —— 它们只可能来自单测 artifact 或
    #   券商账户里的报废测试单 (如 US.TEST order 8185070), 不是真候选股; 且不推告警(见下)。
    if is_test_symbol(symbol):
        return {"ok": False, "skip": "test_symbol"}
    if trd_env.upper() != "SIMULATE":
        # 通知去重: 同一标的一日一次 (原先每轮对账都推 → 用户看到"持续推送");
        #   拦截行为本身不变, 仍返 real_not_authorized。
        _push_once(f"real_blocked:{symbol}", f"🚫 延长时段开仓拦截: {symbol} REAL未授权")
        return {"ok": False, "skip": "real_not_authorized"}

    rec = {"symbol": symbol, "qty": qty, "price": price, "session": session,
           "change_pct": round(chg, 2), "volume": vol, "earnings": earn, "position_usd": POSITION_USD}
    from futu import TrdSide, OrderType, TrdEnv, TimeInForce, Session, RET_OK
    tctx = _open_trade_context()
    try:
        env = TrdEnv.SIMULATE
        # 显式传当日: 下单前安全闸只对**当日**已成交无主的单做建仓回补/冲突检测,
        #   跨日旧单不回补(entry_time 会是错的) — 与 collect() 同一口径。
        active_orders, reconcile_conflicts = _reconcile_broker_entry_orders(
            tctx, env, acc_id, session, today=_today_et()
        )
        if reconcile_conflicts:
            rec.update({
                "ok": False, "skip": "broker_order_reconcile_conflict",
                "orders": reconcile_conflicts,
            })
            _audit(rec)
            return rec
        if active_orders:
            rec.update({
                "ok": False, "skip": "active_ext_entry_order", "orders": active_orders,
            })
            _audit(rec)
            return rec
        ret, data = tctx.place_order(price=price, qty=qty, code=symbol, trd_side=TrdSide.BUY,
                                     order_type=OrderType.NORMAL, trd_env=env, acc_id=acc_id,
                                     time_in_force=TimeInForce.DAY, session=Session.ETH, remark="ext_entry")
        if ret != RET_OK:
            rec["ok"] = False
            rec["error"] = str(data)[:120]
            _push(f"❌ 盘前开仓下单失败\n{symbol} {qty}股 @ ${price:.2f}\n{rec['error']}")
            _audit(rec)
            return rec
        buy_oid = str(data["order_id"].values[0]) if data is not None else ""
        rec["buy_order_id"] = buy_oid

        # 等成交；部分成交则撤掉剩余并等待券商确认终态。
        fill = wait_order_fill(tctx, env, acc_id, buy_oid, qty, price)
        rec["fill"] = fill
        if not fill.get("cancel_ok"):
            rec["ok"] = False
            rec["error"] = f"buy_cancel_failed_{fill.get('status')}:{fill.get('error')}"
            _push(f"⚠️ 盘前开仓撤单未确认\n{symbol} 订单 {buy_oid}\n状态 {fill.get('status')}")
            _audit(rec)
            return rec
        if not fill.get("filled"):
            rec["ok"] = False
            rec["error"] = f"buy_not_filled_{fill.get('status')}"
            _push(f"⏳ 盘前开仓未成交已撤单\n{symbol} {qty}股 @ ${price:.2f}\n状态 {fill.get('status')}")
            _audit(rec)
            return rec

        fqty = float(fill["filled_qty"]); fpx = float(fill["avg_fill_price"])
        rec.update({"filled_qty": fqty, "avg_fill_price": fpx})
        # 成交 → 写 open_positions + trade_records (盘中监控闭环; metadata.stop_loss 供 SL 判定)
        try:
            pos_id = _write_open_position(symbol, fqty, fpx, price, session, earn, chg, vol, buy_oid)
        except Exception as exc:
            rec["ok"] = False
            rec["error"] = f"position_db_write_failed:{exc}"
            _push(f"🚨 盘前开仓已成交但本地建仓失败\n{symbol} 订单 {buy_oid}\n{exc}")
            _audit(rec)
            return rec
        rec["position_id"] = pos_id
        rec["initial_sl"] = round(fpx * (1 + STOP_PCT / 100), 2)
        rec["ok"] = True
        _audit(rec)
        _push(f"✅ 盘前事件开仓已成交\n{symbol} {fqty:g}股 @ ${fpx:.2f} (${fqty*fpx:,.0f})\n"
              f"财报超预期 +{earn['surprise_pct']:.1f}% | 盘前 {chg:+.2f}% 量{vol:,.0f}\n"
              f"止损(规则引擎) initial_sl=${rec['initial_sl']:.2f} (-5%)\n"
              f"⚠️ SIM无券商STOP, 盘中由 monitor_playbook_sim_exits 兜底")
        return rec
    finally:
        tctx.close()



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="premarket", choices=["premarket", "afterhours"])
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    rs = collect(args.session, args.dry)
    print(json.dumps(rs, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
