"""
决策链路路由 — 让 DA决策脑 → playbook执行脑 的单链路在 UI 上可见 (2026-07-11)

三个端点回应 "感知进度 + 感知策略逻辑":
  /api/chain/funnel          今日漏斗: 候选→指令→触发→成交→持仓→退出 (计数+列表)
  /api/chain/trace/{did}     单笔时间线: 按 decision_id 拼 bridge→指令→成交→归因 逐跳
  /api/brain/health          决策脑健康: bridge 新鲜度/候选数/投票适配器/降级告警

设计: 核心联动键是 decision_id (三张表 playbook_instructions/trade_records/
signal_lifecycle 都带), 非 triggered/date。参见
docs/proposals/decision-chain-single-brain-cleanup-20260711.md
"""
import logging
import os
from datetime import datetime

from fastapi import APIRouter

from app.web_dashboard.data_service import get_data_service, safe_float, et_today

logger = logging.getLogger("ftseek_us.routes.chain")
router = APIRouter()

_BRIDGE_DIR = "/opt/ftseek/FTSeek-US/data"


def _bridge_path(date_str: str) -> str:
    return os.path.join(_BRIDGE_DIR, f"opportunity_scan_{date_str}.json")


def _load_bridge_raw(date_str: str):
    """直接读今日 bridge 文件 (比 _try_load_bridge 多拿 mtime/generated_at 等元信息)。"""
    path = _bridge_path(date_str)
    if not os.path.exists(path):
        return None, path, None
    try:
        import json
        with open(path) as f:
            data = json.load(f)
        mtime = os.path.getmtime(path)
        return data, path, mtime
    except Exception as e:
        logger.warning(f"[chain] bridge 读取失败: {e}")
        return None, path, None


def register(app, templates):
    """注册决策链路路由"""

    # ═══════════════════════════════════════════════════════════
    #  1. 今日链路漏斗 — 候选→指令→触发→成交→持仓→退出
    # ═══════════════════════════════════════════════════════════
    @router.get("/api/chain/funnel")
    async def chain_funnel(date: str = None):
        ds = get_data_service()
        day = date or et_today()

        # 候选: 今日 bridge 候选数 (DA 决策脑的输出)
        bridge_data, _, _ = _load_bridge_raw(day)
        candidates = len(bridge_data.get("candidates", [])) if bridge_data else 0

        # 指令 / 触发 / 成交 / 持仓 / 退出 / 跳过: 全部来自 playbook_instructions
        rows = await ds.db_query(
            "SELECT symbol, decision_id, direction, confidence, thesis, "
            "skip_reason, triggered, executed, outcome, pnl_pct, exit_reason, "
            "entry_price, execution_price, exit_price "
            "FROM playbook_instructions WHERE date = ? ORDER BY created_at",
            (day,),
        )

        def _is_skip(r):
            sr = (r.get("skip_reason") or "").strip()
            return bool(sr)

        def _has_outcome(r):
            return bool((r.get("outcome") or "").strip())

        actionable = [r for r in rows if not _is_skip(r)]
        skipped = [r for r in rows if _is_skip(r)]
        triggered = [r for r in actionable if r.get("triggered")]
        executed = [r for r in actionable if r.get("executed")]
        exited = [r for r in executed if _has_outcome(r)]
        holding = [r for r in executed if not _has_outcome(r)]

        def _card(r):
            return {
                "symbol": (r.get("symbol") or "").replace("US.", ""),
                "decision_id": r.get("decision_id") or "",
                "direction": r.get("direction") or "",
                "confidence": r.get("confidence"),
                "thesis": (r.get("thesis") or "")[:80],
                "triggered": bool(r.get("triggered")),
                "executed": bool(r.get("executed")),
                "outcome": r.get("outcome") or "",
                "pnl_pct": r.get("pnl_pct"),
                "exit_reason": r.get("exit_reason") or "",
                "skip_reason": (r.get("skip_reason") or "")[:40],
            }

        # 转化率 (相邻两跳)
        def _rate(a, b):
            return round(a / b * 100, 1) if b else 0.0

        return {
            "date": day,
            "stages": [
                {"key": "candidates", "label": "候选", "count": candidates},
                {"key": "instructions", "label": "指令", "count": len(actionable),
                 "rate": _rate(len(actionable), candidates)},
                {"key": "triggered", "label": "触发", "count": len(triggered),
                 "rate": _rate(len(triggered), len(actionable))},
                {"key": "executed", "label": "成交", "count": len(executed),
                 "rate": _rate(len(executed), len(triggered))},
                {"key": "holding", "label": "持仓", "count": len(holding)},
                {"key": "exited", "label": "退出", "count": len(exited)},
            ],
            "skipped_count": len(skipped),
            "instructions": [_card(r) for r in actionable],
            "skipped": [_card(r) for r in skipped],
        }

    # ═══════════════════════════════════════════════════════════
    #  2. 单笔链路追踪 — 按 decision_id 拼时间线
    # ═══════════════════════════════════════════════════════════
    @router.get("/api/chain/trace/{decision_id}")
    async def chain_trace(decision_id: str):
        ds = get_data_service()

        instr = await ds.db_query_one(
            "SELECT * FROM playbook_instructions WHERE decision_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (decision_id,),
        )
        trade = await ds.db_query_one(
            "SELECT * FROM trade_records WHERE decision_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (decision_id,),
        )
        life = await ds.db_query_one(
            "SELECT * FROM signal_lifecycle WHERE decision_id = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (decision_id,),
        )

        # bridge 候选 (决策脑原始产物): 优先按 date 找当日 bridge, 再按 decision_id/symbol 匹配
        symbol = (instr or {}).get("symbol") or (trade or {}).get("symbol") or ""
        day = (instr or {}).get("date") or et_today()
        bridge_cand = None
        bridge_data, _, _ = _load_bridge_raw(day)
        if bridge_data:
            for c in bridge_data.get("candidates", []):
                if c.get("decision_id") == decision_id or c.get("symbol") == symbol:
                    bridge_cand = c
                    break

        hops = []

        # HOP 0: 决策脑 (bridge 候选)
        if bridge_cand:
            hops.append({
                "stage": "决策脑", "icon": "🧠", "status": "done",
                "title": "DA 生成候选",
                "detail": {
                    "direction": bridge_cand.get("direction"),
                    "thesis": bridge_cand.get("thesis", ""),
                    "invalidation": bridge_cand.get("invalidation", ""),
                    "entry_price": bridge_cand.get("entry_price"),
                    "stop_loss": bridge_cand.get("stop_loss"),
                    "take_profit": bridge_cand.get("take_profit"),
                    "risks": bridge_cand.get("risks", []),
                },
            })

        # HOP 1: 指令 (playbook_instructions)
        if instr:
            hops.append({
                "stage": "指令", "icon": "📋",
                "status": "done" if instr.get("executed") else (
                    "skip" if (instr.get("skip_reason") or "").strip() else "pending"),
                "title": "生成 playbook 指令",
                "ts": instr.get("created_at"),
                "detail": {
                    "direction": instr.get("direction"),
                    "confidence": instr.get("confidence"),
                    "entry_price": instr.get("entry_price"),
                    "stop_loss": instr.get("stop_loss"),
                    "take_profit": instr.get("take_profit"),
                    "thesis": instr.get("thesis", ""),
                    "skip_reason": instr.get("skip_reason") or "",
                    "triggered": bool(instr.get("triggered")),
                },
            })

        # HOP 2: 成交 (trade_records)
        if trade:
            hops.append({
                "stage": "成交", "icon": "💹", "status": "done",
                "title": "入场成交",
                "ts": trade.get("entry_time"),
                "detail": {
                    "entry_price": trade.get("entry_price"),
                    "quantity": trade.get("quantity"),
                    "futu_order_id": trade.get("futu_order_id"),
                    "owner_strategy": trade.get("owner_strategy"),
                    "trade_mode": trade.get("trade_mode"),
                },
            })
            # HOP 3: 退出
            if trade.get("exit_time") or trade.get("exit_price"):
                pnl = trade.get("realized_pnl")
                hops.append({
                    "stage": "退出", "icon": "🚪",
                    "status": "loss" if (pnl or 0) < 0 else "win",
                    "title": f"平仓 · {trade.get('exit_reason') or '?'}",
                    "ts": trade.get("exit_time"),
                    "detail": {
                        "exit_price": trade.get("exit_price"),
                        "exit_reason": trade.get("exit_reason"),
                        "realized_pnl": pnl,
                        "pnl_pct": trade.get("pnl_pct"),
                        "holding_hours": trade.get("holding_hours"),
                    },
                })

        # HOP 4: 归因 (signal_lifecycle)
        if life:
            hops.append({
                "stage": "归因", "icon": "📝", "status": "done",
                "title": "写入 signal_lifecycle",
                "detail": {
                    "signal_confidence": life.get("signal_confidence"),
                    "trade_win": life.get("trade_win"),
                    "direction_aligned": life.get("direction_aligned"),
                    "fwd_ret_t1": life.get("fwd_ret_t1"),
                    "fwd_ret_t5": life.get("fwd_ret_t5"),
                },
            })

        # 一致性检查: confidence 跨表是否一致 (我们发现的 35 vs 0.70 问题)
        warnings = []
        if instr and life:
            ic = instr.get("confidence")
            lc = life.get("signal_confidence")
            if ic is not None and lc is not None:
                ic01 = ic / 100.0 if ic > 1.5 else ic
                if abs((ic01 or 0) - (lc or 0)) > 0.05:
                    warnings.append(
                        f"confidence 跨表不一致: 指令={ic} vs 归因={lc} "
                        f"(归因用的是 snapshot/consensus 而非 DA 原值)")

        return {
            "decision_id": decision_id,
            "symbol": (symbol or "").replace("US.", ""),
            "found": bool(instr or trade or life),
            "hops": hops,
            "warnings": warnings,
        }

    # ═══════════════════════════════════════════════════════════
    #  3. 决策脑健康 — bridge 新鲜度 / 候选数 / 投票适配器 / 降级
    # ═══════════════════════════════════════════════════════════
    @router.get("/api/brain/health")
    async def brain_health():
        day = et_today()
        bridge_data, path, mtime = _load_bridge_raw(day)

        candidates = 0
        generated_at = None
        trade_date = None
        market_condition = None
        if bridge_data:
            candidates = len(bridge_data.get("candidates", []))
            generated_at = bridge_data.get("generated_at") or bridge_data.get("timestamp")
            trade_date = bridge_data.get("trade_date")
            market_condition = (bridge_data.get("market_condition") or "")[:60]

        # 年龄 (小时)
        age_hours = None
        ref_ts = None
        if generated_at:
            try:
                gt = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
                ref_ts = gt.replace(tzinfo=None) if gt.tzinfo else gt
            except Exception:
                ref_ts = None
        if ref_ts is None and mtime:
            ref_ts = datetime.fromtimestamp(mtime)
        if ref_ts:
            age_hours = round((datetime.now() - ref_ts).total_seconds() / 3600, 1)

        # 投票适配器 mode 推断 (与 scheduler_core 一致的逻辑)
        fallback_mode = "technical"
        try:
            from app.core.config_container import get_config
            fallback_mode = str(get_config().get_dotted(
                "playbook.fallback_when_no_bridge", "technical")).lower()
        except Exception:
            pass

        if bridge_data and candidates >= 1:
            adapter = "bridge"          # BridgeVotingAdapter — DA 驱动
        elif fallback_mode == "technical":
            adapter = "technical"       # TechnicalFallbackAdapter — 确定性兜底 (降级)
        else:
            adapter = "null"            # NullVotingAdapter — 全 SKIP (停摆)

        # 健康判定 + 告警
        fresh = bool(trade_date == day) if trade_date else (
            bool(mtime and datetime.fromtimestamp(mtime).strftime("%Y-%m-%d") == day))
        warnings = []
        if adapter == "bridge" and candidates < 3:
            warnings.append(f"bridge 候选仅 {candidates} 个 (<3), 信号覆盖可能不足")
        if adapter == "technical":
            warnings.append("决策脑未产出 bridge → 已降级到 technical 兜底 (非 DA 驱动)")
        if adapter == "null":
            warnings.append("决策脑未产出 bridge 且 fallback=none → 全 SKIP 停摆")
        if not fresh and adapter != "bridge":
            warnings.append("今日无新鲜 bridge 文件")

        if adapter == "bridge" and not warnings:
            status = "healthy"
        elif adapter == "bridge":
            status = "degraded"
        else:
            status = "critical" if adapter == "null" else "degraded"

        return {
            "date": day,
            "status": status,          # healthy / degraded / critical
            "adapter": adapter,        # bridge / technical / null
            "bridge_exists": bool(bridge_data),
            "candidates": candidates,
            "generated_at": generated_at,
            "trade_date": trade_date,
            "age_hours": age_hours,
            "fresh": fresh,
            "market_condition": market_condition,
            "fallback_mode": fallback_mode,
            "warnings": warnings,
        }

    # ═══════════════════════════════════════════════════════════
    #  4. 退出画像 — exit_reason 分布 / 时钟类占比 / 幽灵平仓
    # ═══════════════════════════════════════════════════════════
    @router.get("/api/chain/exit-profile")
    async def exit_profile(days: int = 30):
        ds = get_data_service()
        rows = await ds.db_query(
            "SELECT exit_reason, COUNT(*) n, "
            "ROUND(AVG(pnl_pct)*100,2) avg_pct, "
            "SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins "
            "FROM trade_records "
            "WHERE trade_date >= date('now', ?) AND exit_price > 0 "
            "AND source_type NOT IN ('PLAYBOOK_SHADOW','SCALPING') "
            "AND COALESCE(exit_reason,'') NOT IN ('PHANTOM_RECONCILE_VOID','ORPHAN_CLEANUP') "
            "GROUP BY exit_reason ORDER BY n DESC",
            (f"-{int(days)} day",),
        )

        # 归类: 时钟(到点就走) / 止损 / 逻辑(止盈·信号) / 券商(对账漂移) / 其他
        # 2026-07-11 修正: logic 类原漏标主力止盈标签(DAY_TRADE_TP/SCALE_OUT/ORB_TAKE_PROFIT),
        #   曾误报"逻辑类仅7%/止盈仅1次"。ORB_BREAKDOWN 是破位止损→移到 stop。
        #   TRAILING_STOP 意图锁利但当前系统性亏损(22笔均-1.32%), 并入止损类并在 hint 标注。
        _PROFIT_REASONS = {"TAKE_PROFIT", "DAY_TRADE_TP", "SCALE_OUT",
                           "ORB_TAKE_PROFIT", "SIGNAL_EXIT"}
        CAT = {
            # 时钟类 (到点就走)
            "TIME_EXIT": "clock", "PRE_CLOSE_EXIT": "clock", "EOD_FORCE_CLOSE": "clock",
            "MAX_HOLD": "clock", "MAX_HOLD_T5": "clock", "DAY_TRADE_TIMEOUT": "clock",
            # 止损类 (保护性止损 + 跟踪止损; TRAILING 意图锁利但当前系统性亏损)
            # 2026-07-14: SL_FIXED_PCT 已拆为 SL_INITIAL/SL_BREAKEVEN/SL_TRAIL_LOCK (旧标签保留兼容历史).
            "SL_FIXED_PCT": "stop", "SL_INITIAL": "stop", "SL_BREAKEVEN": "stop",
            "SL_TRAIL_LOCK": "stop", "SL_ATR_STOP": "stop", "HARD_STOP": "stop",
            "BREAKEVEN_STOP": "stop", "TRAILING_STOP": "stop", "ORB_TRAILING_STOP": "stop",
            "ORB_FLOOR_STOP": "stop", "ORB_BREAKDOWN": "stop", "FLASH_EXIT": "stop",
            "OVERNIGHT_GAP_STOP": "stop",
            # 逻辑类 (主动止盈 / 信号驱动)
            "TAKE_PROFIT": "logic", "DAY_TRADE_TP": "logic", "SCALE_OUT": "logic",
            "ORB_TAKE_PROFIT": "logic", "SIGNAL_EXIT": "logic",
            # 券商类 (对账漂移 / 幽灵平仓, 含 ghost_cleanup 衍生)
            "BROKER_GHOST_CLOSE": "broker", "BROKER_GHOST_CLOSE_MANUAL": "broker",
            "external_close": "broker", "SCALE_OUT_ghost_cleanup": "broker",
            "TRAILING_STOP_ghost_cleanup": "broker",
            # 其余(EXIT_MONITOR_CLOSE / FORCED_CLOSE_OKR_AUDIT 等)→ other
        }
        CAT_META = {
            "clock": {"label": "时钟类", "hint": "到点就走·不看逻辑", "color": "#fbbf24"},
            "stop": {"label": "止损类", "hint": "保护性止损 + 跟踪止损(TRAILING)", "color": "#f87171"},
            "logic": {"label": "逻辑类", "hint": "止盈/信号驱动", "color": "#4ade80"},
            "broker": {"label": "券商类", "hint": "对账漂移·幽灵平仓", "color": "#a371f7"},
            "other": {"label": "其他", "hint": "管理/强平", "color": "#8b949e"},
        }

        cats = {k: {"n": 0, "pnl_sum": 0.0, "wins": 0} for k in CAT_META}
        total = 0
        reasons = []
        take_profit_n = 0
        ghost_n = 0
        for r in rows:
            er = r.get("exit_reason") or "(空)"
            n = r.get("n") or 0
            cat = CAT.get(er, "other")
            cats[cat]["n"] += n
            cats[cat]["pnl_sum"] += (r.get("avg_pct") or 0) * n
            cats[cat]["wins"] += r.get("wins") or 0
            total += n
            if er in _PROFIT_REASONS:
                take_profit_n += n
            if er == "BROKER_GHOST_CLOSE":
                ghost_n += n
            reasons.append({
                "reason": er, "category": cat, "n": n,
                "avg_pct": r.get("avg_pct"),
                "win_rate": round((r.get("wins") or 0) / n * 100, 1) if n else 0.0,
            })

        categories = []
        for k, meta in CAT_META.items():
            cc = cats[k]
            if cc["n"] == 0:
                continue
            categories.append({
                "key": k, "label": meta["label"], "hint": meta["hint"],
                "color": meta["color"], "n": cc["n"],
                "share": round(cc["n"] / total * 100, 1) if total else 0.0,
                "avg_pct": round(cc["pnl_sum"] / cc["n"], 2) if cc["n"] else 0.0,
                "win_rate": round(cc["wins"] / cc["n"] * 100, 1) if cc["n"] else 0.0,
            })
        categories.sort(key=lambda x: -x["n"])

        clock_share = round(cats["clock"]["n"] / total * 100, 1) if total else 0.0
        warnings = []
        if clock_share >= 40:
            warnings.append(f"{clock_share}% 是时钟类退出 (到点就走), 退出与入场论点脱钩")
        if total and take_profit_n / total < 0.05:
            warnings.append(f"止盈仅 {take_profit_n}/{total} 笔, 盈利多被时钟/兜底截断而非主动兑现")
        if ghost_n:
            warnings.append(f"{ghost_n} 笔券商幽灵平仓 (对账漂移), 污染退出控制与 PnL 归因")

        return {
            "days": days, "total": total,
            "clock_share": clock_share,
            "take_profit_n": take_profit_n,
            "ghost_n": ghost_n,
            "categories": categories,
            "reasons": reasons,
            "warnings": warnings,
        }

    # ═══════════════════════════════════════════════════════════
    #  5. 校准视图 — confidence 分桶 vs 胜率/收益 (confidence 已修为 DA 原值)
    # ═══════════════════════════════════════════════════════════
    @router.get("/api/chain/calibration")
    async def calibration(days: int = 120, owner: str = ""):
        ds = get_data_service()
        where = ["signal_confidence IS NOT NULL", "trade_win IS NOT NULL",
                 "trade_date >= date('now', ?)"]
        params = [f"-{int(days)} day"]
        if owner:
            where.append("owner_strategy = ?")
            params.append(owner)
        rows = await ds.db_query(
            "SELECT signal_confidence sc, trade_win tw, pnl_pct pp "
            "FROM signal_lifecycle WHERE " + " AND ".join(where),
            tuple(params),
        )

        # 分桶: <50% / 50-65% / 65-80% / 80%+ (与 check_decision_chain --calibration 对齐)
        BUCKETS = [
            ("<50%", 0.0, 0.5), ("50-65%", 0.5, 0.65),
            ("65-80%", 0.65, 0.80), ("80%+", 0.80, 1.01),
        ]
        out = []
        for label, lo, hi in BUCKETS:
            sub = [r for r in rows if lo <= (r.get("sc") or -1) < hi]
            n = len(sub)
            wins = sum(1 for r in sub if r.get("tw"))
            pnls = [r.get("pp") for r in sub if r.get("pp") is not None]
            out.append({
                "bucket": label, "n": n,
                "win_rate": round(wins / n * 100, 1) if n else 0.0,
                "avg_pnl_pct": round(sum(pnls) / len(pnls) * 100, 2) if pnls else 0.0,
            })

        total = len(rows)
        # 单调性: 胜率是否随 confidence 递增 (校准是否成立)
        wrs = [b["win_rate"] for b in out if b["n"] > 0]
        monotonic = all(wrs[i] <= wrs[i + 1] for i in range(len(wrs) - 1)) if len(wrs) > 1 else None

        note = ""
        if total < 50:
            note = f"样本仅 {total} 笔 (<50), 只看结构不下结论"
        elif monotonic is False:
            note = "胜率未随 confidence 单调递增 → DA confidence 尚未校准 (高信心≠高胜率)"
        elif monotonic:
            note = "胜率随 confidence 递增 → confidence 有区分度"

        return {
            "days": days, "total": total, "owner": owner or "全部",
            "buckets": out, "monotonic": monotonic, "note": note,
            "confidence_source": "DA 指令原值 (2026-07-11 修复后)",
        }

    app.include_router(router)
