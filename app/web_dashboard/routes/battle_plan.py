"""
战前计划路由 — 读取/编辑/确认/暂停 daily battle plan

数据源: data/daily_battle_plan_us.json
"""
import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, Request

from app.web_dashboard.data_service import get_data_service
from app.web_dashboard.api_response import api_ok, api_error, err_broker_unavailable

logger = logging.getLogger("ftseek_us.routes.battle_plan")
router = APIRouter()

_PLAN_PATH = Path(__file__).parent.parent.parent.parent / "data" / "daily_battle_plan_us.json"

# 并发写锁: 防止多个 Dashboard 操作同时 read-modify-write 导致数据丢失 (2026-06-12 修复)
_write_lock = asyncio.Lock()

# Task #11: target trigger_status 快照 (仅在内存, 进程重启后第一次不推)
_last_target_status: dict = {}


def _compute_trigger_status(target: dict) -> str:
    """轻量计算 trigger_status (与 _build_actions 逻辑保持一致)"""
    cur = target.get("_current_price", 0)
    entry_zone = target.get("entry_zone", [0, 0])
    direction = target.get("direction", "LONG")
    if not cur or not entry_zone or len(entry_zone) != 2:
        return "待触发"
    if entry_zone[0] <= cur <= entry_zone[1]:
        return "in_zone"
    if direction == "LONG" and cur > entry_zone[1]:
        return "above_zone"
    if direction == "SHORT" and cur < entry_zone[0]:
        return "below_zone"
    return "待触发"


def _emit_target_updates(targets: list):
    """Task #11: 检测 target trigger_status 变化并发布 target_update WS 事件

    轻量 delta 检测: 仅在状态变化时推送, 避免轮询频率刷屏。
    前端如未订阅该事件, 广播代价为 0 (连接管理器会过滤)。
    """
    try:
        from app.web_dashboard.routes.ws import publish_event_sync
    except ImportError:
        return
    for t in targets or []:
        sym = t.get("symbol", "")
        if not sym:
            continue
        new_status = _compute_trigger_status(t)
        old_status = _last_target_status.get(sym)
        if old_status is None:
            # 首次记录, 不推
            _last_target_status[sym] = new_status
            continue
        if old_status != new_status:
            _last_target_status[sym] = new_status
            try:
                publish_event_sync("target_update", {
                    "symbol": sym,
                    "direction": t.get("direction", "LONG"),
                    "trigger_status": new_status,
                    "current_price": t.get("_current_price", 0),
                    "entry_zone": t.get("entry_zone", [0, 0]),
                })
            except Exception:
                pass


async def _atomic_save_plan(plan: dict):
    """原子写入: 先写临时文件再 rename, 防止读到半写状态"""
    async with _write_lock:
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=_PLAN_PATH.parent, suffix=".tmp", prefix="plan_"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(plan, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, str(_PLAN_PATH))
        except Exception:
            # 清理临时文件
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


async def _refresh_position_actions(plan: dict):
    """实时刷新 position_actions 的 pnl/价格/方向 (每次API调用时执行)

    通过 DataService 共享缓存获取持仓 (5s TTL), 避免绕过缓存层直调 Futu。
    pipeline 只在盘前跑一次, JSON 里的 pnl_pct 是过时的。
    """
    position_actions = plan.get("position_actions", [])
    if not position_actions:
        return

    # 加载因子评分 (all_scores 包含全部股票, 不只是 top_n)
    factor_scores = {}
    try:
        _FACTOR_PATH = _PLAN_PATH.parent / "factor_screen_latest.json"
        if _FACTOR_PATH.exists():
            with open(_FACTOR_PATH) as ff:
                fdata = json.load(ff)
            for s in fdata.get("all_scores", []):
                sym = s.get("symbol", "")
                if sym:
                    factor_scores[sym] = round(s.get("composite_score", 0), 3)
            for c in fdata.get("candidates", []):
                sym = c.get("symbol", "")
                if sym and sym not in factor_scores:
                    factor_scores[sym] = round(c.get("composite_score", 0), 3)
    except Exception:
        pass

    # 通过 DataService 共享缓存获取持仓 (与 dashboard/trade 共用 5s TTL 缓存)
    live_positions = {}
    try:
        from app.web_dashboard.data_service import get_data_service
        ds = get_data_service()
        raw_positions = await ds.get_positions_raw()
        for row in raw_positions:
            code = str(row.get("code", ""))
            if not code or not code.startswith("US."):
                continue
            # Filter out options: codes like US.SNDK260620C75000 have digits after ticker
            bare = code[3:]  # strip "US."
            if any(c.isdigit() for c in bare):
                continue  # option code, skip
            avg_cost = float(row.get("average_cost", 0))
            cur_price = float(row.get("nominal_price", 0))
            qty = int(float(row.get("qty", 0)))
            # Calculate P&L% from price directly (more reliable than pl_ratio_avg_cost
            # which can include fees/dividends/adjustments and produce extreme values)
            is_short = qty < 0
            if avg_cost > 0 and cur_price > 0:
                pnl_pct = ((cur_price - avg_cost) / avg_cost * 100) if not is_short else ((avg_cost - cur_price) / avg_cost * 100)
            else:
                pnl_pct = float(row.get("pl_ratio_avg_cost", 0))
            live_positions[code] = {
                "qty": qty,
                "avg_cost": avg_cost,
                "current_price": cur_price,
                "pnl_pct": round(pnl_pct, 2),
                "unrealized_pl": float(row.get("unrealized_pl", 0)),
                "market_val": float(row.get("market_val", 0)),
            }
    except Exception as e:
        logger.debug(f"[BattlePlan] position refresh fetch error: {e}")
        return

    if not live_positions:
        return

    # 更新每个 position_action
    target_set = {t.get("symbol", "") for t in plan.get("targets", [])}
    restricted_set = set(plan.get("restricted", []))

    # Remove stale position_actions that no longer exist in live positions
    # (closed positions, or filtered options)
    stale_indices = []
    for i, pa in enumerate(position_actions):
        sym = pa.get("symbol", "")
        if sym not in live_positions:
            stale_indices.append(i)
    for i in reversed(stale_indices):
        position_actions.pop(i)

    for pa in position_actions:
        sym = pa.get("symbol", "")
        live = live_positions.get(sym)
        if live:
            pa["current_pnl_pct"] = round(live["pnl_pct"], 2)
            pa["current_price"] = round(live["current_price"], 2)
            pa["qty"] = live["qty"]
            pa["direction"] = "SHORT" if live["qty"] < 0 else "LONG"
            pa["unrealized_pl"] = round(live["unrealized_pl"], 2)
            pa["market_val"] = round(live["market_val"], 2)
        # 更新因子评分 (用全量 all_scores)
        if sym in factor_scores:
            pa["factor_score"] = factor_scores[sym]

        # 重新生成 reason (用实时数据, 不用pipeline的旧文本)
        fs = pa.get("factor_score", 0)
        pnl = pa.get("current_pnl_pct", 0)
        # Sanity: ignore P&L if absurdly high (data error from simulate env or options)
        if abs(pnl) > 200:
            pnl = 0
        if sym in restricted_set:
            pa["action"] = "EXIT"
            pa["urgency"] = "HIGH"
            pa["reason"] = "因子验证INVALID, 在restricted列表中"
        elif sym in target_set:
            # Find verdict/conf from plan targets
            tgt = next((t for t in plan.get("targets", []) if t.get("symbol") == sym), {})
            verdict = tgt.get("verdict", "")
            conf = tgt.get("confidence", 0)
            pa["action"] = "HOLD"
            pa["urgency"] = "LOW"
            pa["reason"] = f"因子{fs*100:.0f}分, LLM验证{verdict}(conf={conf}%), 在推荐列表中"
        elif fs < 0.3:
            pa["action"] = "REDUCE"
            pa["urgency"] = "MEDIUM"
            pa["reason"] = f"因子分仅{fs*100:.0f}, 不在Top候选中, 动量衰减"
        elif pnl < -5:
            pa["action"] = "REDUCE"
            pa["urgency"] = "MEDIUM"
            pa["reason"] = f"浮亏{pnl:.1f}%, 因子{fs*100:.0f}分, 考虑减仓止损"
        else:
            pa["action"] = "HOLD"
            pa["urgency"] = "LOW"
            pa["reason"] = f"因子{fs*100:.0f}分, {'LLM未验证但因子尚可' if fs >= 0.4 else '关注因子变化'}"

    # 检查是否有新持仓不在 position_actions 中 (盘中新开的仓)
    existing_syms = {pa.get("symbol", "") for pa in position_actions}

    for sym, live in live_positions.items():
        if sym in existing_syms:
            continue
        fs = factor_scores.get(sym, 0)
        direction = "SHORT" if live["qty"] < 0 else "LONG"
        pnl = round(live["pnl_pct"], 2)

        if sym in restricted_set:
            action = "EXIT"
            reason = "持仓在restricted列表中"
            urgency = "HIGH"
        elif sym in target_set:
            action = "HOLD"
            reason = f"因子{fs*100:.0f}分, 在今日计划targets中"
            urgency = "LOW"
        elif fs < 0.3:
            action = "REDUCE"
            reason = f"因子分仅{fs*100:.0f}, 不在Top候选中, 动量衰减"
            urgency = "MEDIUM"
        elif pnl < -5:
            action = "REDUCE"
            reason = f"浮亏{pnl:.1f}%, 因子{fs*100:.0f}分, 考虑减仓止损"
            urgency = "MEDIUM"
        else:
            action = "HOLD"
            reason = f"因子{fs*100:.0f}分, {'LLM未验证但因子尚可' if fs >= 0.4 else '关注因子变化'}"
            urgency = "LOW"

        position_actions.append({
            "symbol": sym,
            "action": action,
            "reason": reason,
            "urgency": urgency,
            "current_pnl_pct": pnl,
            "current_price": round(live["current_price"], 2),
            "qty": live["qty"],
            "direction": direction,
            "unrealized_pl": round(live["unrealized_pl"], 2),
            "market_val": round(live["market_val"], 2),
            "factor_score": round(fs, 3),
        })

    # Re-sort: EXIT first, then REDUCE, then HOLD
    _act_order = {"EXIT": 0, "REDUCE": 1, "HOLD": 2, "ADD": 3}
    position_actions.sort(key=lambda a: _act_order.get(a.get("action", "HOLD"), 9))


def register(app, templates):
    @router.get("/plan")
    async def battle_plan_page(request: Request):
        """作战计划独立页面"""
        import os
        from fastapi.responses import Response
        is_live = os.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower() == "real"
        html = templates.TemplateResponse("battle_plan.html", {
            "request": request,
            "is_live": is_live,
        })
        # 2026-07-04: 禁止浏览器缓存HTML (模板频繁更新)
        html.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        html.headers["Pragma"] = "no-cache"
        html.headers["Expires"] = "0"
        return html

    @router.get("/api/battle-plan")
    async def get_battle_plan():
        """读取战前计划 + 附加实时价格"""
        if not _PLAN_PATH.exists():
            return api_error("BATTLE_PLAN_ERROR", "今日无战前计划", 404)

        try:
            with open(_PLAN_PATH) as f:
                plan = json.load(f)
        except Exception as e:
            return api_error("BATTLE_PLAN_ERROR", f"读取失败: {e}", 500)

        # ── 判断是否为交易日 (基于 ET 时间, 非服务器本地时间) ──
        from app.web_dashboard.data_service import et_now
        _now_et = et_now()
        _is_weekend = _now_et.weekday() >= 5
        _plan_date = plan.get("date", "")
        _is_stale = _plan_date != _now_et.strftime("%Y-%m-%d") if _plan_date else False

        if _is_weekend or _is_stale:
            # 非交易日或过期计划: 跳过 enrichment/技术指标等外部 API
            # 但 position_actions 仍需刷新 (Futu 周末仍可查持仓)
            plan["_is_trading_day"] = False
            plan["_weekend"] = _is_weekend
            plan["_stale"] = _is_stale
            # 2026-07-30: 显式水印 — 旧 discovery 链已停用 (ADR-012), 本文件冻结,
            #   真实交易轨道 = DA/UE → playbook_instructions; 前端可直展此横幅
            if _is_stale:
                plan["_stale_notice"] = (
                    f"本计划日期 {_plan_date} 已过期 (旧作战计划链 07-13 停用, "
                    f"当前交易以 playbook 指令为准)"
                )
            plan["_confirmed"] = plan.get("_confirmed", False)
            plan["_paused"] = plan.get("_paused", False)
            plan["_is_live"] = os.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower() == "real"
            try:
                await _refresh_position_actions(plan)
            except Exception as e:
                logger.debug(f"[BattlePlan] non-trading-day position refresh failed: {e}")
            return plan

        # ── 以下仅在交易日执行 ──

        # 附加实时价格到每个 target (带超时保护)
        targets = plan.get("targets", [])
        symbols = [t["symbol"] for t in targets if t.get("symbol")]
        if symbols:
            ds = get_data_service()
            try:
                quotes = await asyncio.wait_for(ds.get_batch_quotes(symbols), timeout=8)
                for t in targets:
                    sym = t.get("symbol", "")
                    if sym in quotes:
                        t["_current_price"] = round(quotes[sym], 2)
            except Exception:
                pass

        # ── 新增 (2026-06-12, 更新 2026-06-18): 附加技术指标 + 板块 + 经济事件 ──
        _SCAN_CACHE = Path(__file__).parent.parent.parent.parent / "data" / "premarket_scan_cache.json"
        if _SCAN_CACHE.exists():
            try:
                with open(_SCAN_CACHE) as sf:
                    scan_data = json.load(sf)
                # 兼容新旧格式
                # 新格式 (PreMarketScanner): targets, sector_rotation, economic_events, no_trade_windows
                # 旧格式: technicals, sectors, sector_symbol_map
                scan_targets = scan_data.get("targets", scan_data.get("technicals", {}))
                sector_rotation = scan_data.get("sector_rotation", {})
                sector_map = scan_data.get("sector_symbol_map", {})
                
                for t in targets:
                    sym = t.get("symbol", "").replace("US.", "")
                    sym_full = t.get("symbol", "")
                    if sym in scan_targets:
                        t["_technical"] = scan_targets[sym]
                    elif sym_full in scan_targets:
                        t["_technical"] = scan_targets[sym_full]
                    if sym_full in sector_map:
                        t["_sector_info"] = sector_map[sym_full]
                
                plan["_sectors"] = sector_rotation
                plan["_economic_events"] = scan_data.get("economic_events", [])
                plan["_no_trade_windows"] = scan_data.get("no_trade_windows", [])
                plan["_scan_summary"] = scan_data.get("summary", "")
            except Exception:
                pass

        # 标记是否已确认/暂停
        plan["_confirmed"] = plan.get("_confirmed", False)
        plan["_paused"] = plan.get("_paused", False)

        # ── 2026-06-14: 附加 premarket enrichment 数据 (带超时保护) ──
        try:
            from app.data.premarket_enrichment import collect_premarket_data
            import concurrent.futures
            watchlist_syms = []
            for t in targets:
                sym = (t.get("symbol", "") or "").replace("US.", "")
                if sym:
                    watchlist_syms.append(sym)
            # Run in thread pool with 10s timeout (makes blocking HTTP calls)
            loop = asyncio.get_event_loop()
            with concurrent.futures.ThreadPoolExecutor() as pool:
                enrichment = await asyncio.wait_for(
                    loop.run_in_executor(pool, lambda: collect_premarket_data(watchlist_symbols=watchlist_syms[:15])),
                    timeout=10
                )

            # Extract key fields for frontend
            plan["_enrichment"] = {
                "sectors": enrichment.get("sector_rotation", {}).get("ranked", []),
                "indices": enrichment.get("premarket_movers", {}).get("market_indices", {}),
                "movers": {
                    "gainers": enrichment.get("premarket_movers", {}).get("gainers", [])[:5],
                    "losers": enrichment.get("premarket_movers", {}).get("losers", [])[:5],
                },
                "treasury": {
                    "y10": enrichment.get("treasury_yields", {}).get("10Y"),
                    "y2": enrichment.get("treasury_yields", {}).get("2Y"),
                    "spread": enrichment.get("treasury_yields", {}).get("spread_10y_2y"),
                    "curve": enrichment.get("treasury_yields", {}).get("curve_shape", ""),
                    "interpretation": enrichment.get("treasury_yields", {}).get("interpretation", ""),
                },
                "insider": {
                    "count": enrichment.get("insider_trading", {}).get("total_buys_7d", 0),
                    "notable": enrichment.get("insider_trading", {}).get("notable", [])[:5],
                },
                "news": [
                    {"headline": n.get("headline", ""), "source": n.get("source", "")}
                    for n in enrichment.get("market_news", [])[:5]
                ],
                "earnings": [],  # parsed from earnings_calendar string
                "economic": enrichment.get("economic_calendar", ""),
            }
            # Parse earnings calendar
            ec = enrichment.get("earnings_calendar", "")
            if ec and "notable" in ec:
                for line in ec.split("\n"):
                    parts = line.strip().split("|")
                    if len(parts) >= 2:
                        sym = parts[0].strip()
                        if sym and sym != "📊":
                            plan["_enrichment"]["earnings"].append({"symbol": sym, "detail": line.strip()})
            plan["_enrichment_time"] = enrichment.get("collected_at", "")
        except Exception as e:
            logger.debug(f"[BattlePlan] enrichment attach failed: {e}")

        # ── 实时刷新 position_actions (每次API调用都更新pnl/价格) ──
        try:
            await _refresh_position_actions(plan)
        except Exception as e:
            logger.debug(f"[BattlePlan] position_actions refresh failed: {e}")

        # Task #11: 检测 target trigger_status 变化并发 WS 事件 (轻量 delta)
        try:
            _emit_target_updates(targets)
        except Exception as _ws_err:
            logger.debug(f"[BattlePlan] target_update emit skipped: {_ws_err}")

        # Mark live mode for frontend modeTag sync
        import os as _os
        plan["_is_live"] = _os.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower() == "real"
        plan["_is_trading_day"] = True

        return plan

    @router.get("/api/pipeline/factor-scores")
    async def get_factor_scores():
        """从factor_screen_latest.json读取所有股票的因子评分，供watchlist等页面使用"""
        _FACTOR_PATH = Path(__file__).parent.parent.parent.parent / "data" / "factor_screen_latest.json"
        if not _FACTOR_PATH.exists():
            return api_ok({})
        try:
            with open(_FACTOR_PATH) as f:
                data = json.load(f)
            # 2026-08-03 (任务#4): 死文件防呆 — >2天陈旧返回空 (前端对 {} 已有处理),
            #   避免 watchlist 页面把数周前评分当当日值展示.
            _ts = str(data.get("timestamp", ""))
            if _ts:
                from datetime import datetime as _dt_fs, timezone as _tz_fs, timedelta as _td_fs
                if _dt_fs.now(_tz_fs.utc) - _dt_fs.fromisoformat(_ts.replace("Z", "+00:00")) > _td_fs(days=2):
                    return api_ok({})
            scores = {}
            for c in data.get("candidates", []):
                sym = c.get("symbol", "")
                if sym:
                    scores[sym] = round(c.get("composite_score", 0), 3)
            # Also include all scored symbols (not just top N)
            for s in data.get("all_scores", []):
                sym = s.get("symbol", "")
                if sym and sym not in scores:
                    scores[sym] = round(s.get("composite_score", 0), 3)
            return api_ok(scores)
        except Exception as e:
            logger.warning(f"[factor-scores] read error: {e}")
            return api_ok({})

    @router.put("/api/battle-plan/targets/{symbol:path}")
    async def update_target(symbol: str, request: Request):
        """修改单个 target 的参数"""
        try:
            body = await request.json()
        except Exception:
            return api_error("BATTLE_PLAN_ERROR", "请求无效", 400)

        if not _PLAN_PATH.exists():
            return api_error("BATTLE_PLAN_ERROR", "计划文件不存在", 404)

        try:
            with open(_PLAN_PATH) as f:
                plan = json.load(f)

            symbol = symbol.upper()
            if not symbol.startswith("US."):
                symbol = "US." + symbol

            updated = False
            for t in plan.get("targets", []):
                if t.get("symbol", "").upper() == symbol:
                    if "stop_loss" in body:
                        t["stop_loss"] = float(body["stop_loss"])
                    if "take_profit" in body:
                        t["take_profit"] = float(body["take_profit"])
                    if "entry_zone" in body and isinstance(body["entry_zone"], list) and len(body["entry_zone"]) == 2:
                        t["entry_zone"] = [float(body["entry_zone"][0]), float(body["entry_zone"][1])]
                    if "urgency" in body:
                        t["urgency"] = str(body["urgency"])
                    updated = True
                    break

            if not updated:
                return api_error("BATTLE_PLAN_ERROR", f"未找到标的 {symbol}", 404)

            await _atomic_save_plan(plan)

            logger.info(f"[BattlePlan] 更新 {symbol}: {body}")
            return {"success": True, "symbol": symbol}
        except Exception as e:
            logger.error(f"[BattlePlan] 更新失败: {e}")
            return api_error("BATTLE_PLAN_ERROR", "操作失败，请稍后重试", 500)

    @router.post("/api/battle-plan/confirm")
    async def confirm_plan():
        """确认战前计划"""
        if not _PLAN_PATH.exists():
            return api_error("BATTLE_PLAN_ERROR", "计划文件不存在", 404)

        try:
            with open(_PLAN_PATH) as f:
                plan = json.load(f)
            plan["_confirmed"] = True
            plan["_confirmed_at"] = datetime.now().isoformat()
            await _atomic_save_plan(plan)
            logger.info("[BattlePlan] 计划已确认")
            return {"success": True}
        except Exception as e:
            return api_error("BATTLE_PLAN_ERROR", "操作失败，请稍后重试", 500)

    @router.post("/api/battle-plan/pause")
    async def pause_plan():
        """暂停今日自动交易"""
        if not _PLAN_PATH.exists():
            return api_error("BATTLE_PLAN_ERROR", "计划文件不存在", 404)

        try:
            with open(_PLAN_PATH) as f:
                plan = json.load(f)
            plan["_paused"] = True
            plan["_paused_at"] = datetime.now().isoformat()
            await _atomic_save_plan(plan)
            logger.info("[BattlePlan] 计划已暂停")
            return {"success": True}
        except Exception as e:
            return api_error("BATTLE_PLAN_ERROR", "操作失败，请稍后重试", 500)

    @router.post("/api/battle-plan/resume")
    async def resume_plan():
        """取消暂停，恢复自动交易 (2026-06-12 新增)"""
        if not _PLAN_PATH.exists():
            return api_error("BATTLE_PLAN_ERROR", "计划文件不存在", 404)

        try:
            with open(_PLAN_PATH) as f:
                plan = json.load(f)
            plan["_paused"] = False
            plan["_paused_at"] = ""  # 清除暂停时间
            plan["_resumed_at"] = datetime.now().isoformat()
            await _atomic_save_plan(plan)
            logger.info("[BattlePlan] 计划已恢复")
            return {"success": True}
        except Exception as e:
            return api_error("BATTLE_PLAN_ERROR", "操作失败，请稍后重试", 500)

    # ═══════════════════════════════════════════════════════════
    #  Deep Analysis On-Demand API (Task #30)
    # ═══════════════════════════════════════════════════════════

    @router.post("/api/battle-plan/deep-analysis/generate")
    async def generate_deep_analysis_api(request: Request):
        """按需为当前 plan 的所有 targets 生成深度分析并回写 JSON。"""
        if not _PLAN_PATH.exists():
            return api_error("BATTLE_PLAN_ERROR", "计划文件不存在", 404)

        try:
            with open(_PLAN_PATH) as f:
                plan = json.load(f)
        except Exception as e:
            logger.error(f"[DeepAnalysis] 读取 plan 失败: {e}")
            return api_error("BATTLE_PLAN_ERROR", f"读取计划失败: {e}", 500)

        targets = plan.get("targets", []) or []
        if not targets:
            return api_error("BATTLE_PLAN_ERROR", "当前计划无 targets", 400)

        # ── 组装 macro：优先用 plan 已有字段，缺失走默认值 ──
        enrichment = plan.get("_enrichment", {}) or {}
        indices = enrichment.get("indices", {}) or {}
        treasury = enrichment.get("treasury", {}) or {}
        macro = {
            "market_regime": plan.get("market_regime", "NEUTRAL"),
            "vix": indices.get("VIX") or plan.get("vix") or "N/A",
            "vix_change": plan.get("vix_change", "N/A"),
            "spy_trend": plan.get("spy_trend", "N/A"),
            "spy_change": plan.get("spy_change_20d", "N/A"),
            "dxy": indices.get("DXY", "N/A"),
            "ust10y": treasury.get("y10", "N/A"),
            "upcoming_events": [
                e.get("detail", "") for e in (enrichment.get("earnings") or [])[:3]
            ],
        }

        # ── 组装当前持仓 symbol 列表 ──
        positions: list = []
        try:
            ds = get_data_service()
            raw = await ds.get_positions_raw()
            for p in raw or []:
                sym = (p.get("code") or p.get("symbol") or "").upper()
                if sym:
                    positions.append(sym)
        except Exception as e:
            logger.debug(f"[DeepAnalysis] 获取持仓失败 (忽略): {e}")

        # ── 调用引擎 ──
        try:
            from app.plan.deep_analysis_engine import generate_all_deep_analyses
            logger.info(
                f"[DeepAnalysis] 开始为 {len(targets)} 个 targets 生成深度分析"
            )
            results = await generate_all_deep_analyses(
                targets=targets,
                macro=macro,
                positions=positions,
            )
        except Exception as e:
            logger.exception(f"[DeepAnalysis] 引擎调用失败: {e}")
            return api_error("BATTLE_PLAN_ERROR", f"生成失败: {e}", 500)

        # ── 回写到对应 target ──
        result_map = {
            (r.get("symbol") or "").upper(): r
            for r in (results or [])
        }
        success_count = 0
        fail_count = 0
        for t in targets:
            sym = (t.get("symbol") or "").upper()
            r = result_map.get(sym)
            if r and r.get("deep_analysis"):
                t["deep_analysis"] = r["deep_analysis"]
                success_count += 1
            else:
                fail_count += 1

        plan["_deep_analysis_generated_at"] = datetime.now().isoformat()

        try:
            await _atomic_save_plan(plan)
        except Exception as e:
            logger.error(f"[DeepAnalysis] 保存 plan 失败: {e}")
            return api_error("BATTLE_PLAN_ERROR", f"保存失败: {e}", 500)

        logger.info(
            f"[DeepAnalysis] 完成: 成功 {success_count}, 失败 {fail_count}"
        )
        return {
            "success": True,
            "generated": success_count,
            "failed": fail_count,
            "total": len(targets),
        }

    # ═══════════════════════════════════════════════════════════
    #  Deep Research On-Demand API (v3 — 4 分析师 + 辩论 + 综合)
    # ═══════════════════════════════════════════════════════════

    @router.post("/api/battle-plan/deep-research/generate")
    async def generate_deep_research_api(request: Request):
        """按需为当前 plan 的 top 3 targets 生成 Deep Research 级别分析并回写 JSON。

        比 deep-analysis 更深: 4 分析师并行 → 多空辩论 → 综合决策
        每个 target 约 9 次 LLM 调用，总计 27 次 ≈ 5-6 分钟。
        已有有效 deep_research 的 target 自动跳过。
        """
        if not _PLAN_PATH.exists():
            return api_error("BATTLE_PLAN_ERROR", "计划文件不存在", 404)

        try:
            with open(_PLAN_PATH) as f:
                plan = json.load(f)
        except Exception as e:
            logger.error(f"[DeepResearch] 读取 plan 失败: {e}")
            return api_error("BATTLE_PLAN_ERROR", f"读取计划失败: {e}", 500)

        targets = plan.get("targets", []) or []
        if not targets:
            return api_error("BATTLE_PLAN_ERROR", "当前计划无 targets", 400)

        # 盘前数据: 用活的内存 collector (premarket_enrichment.json 已无写方, 2026-07-11)
        premarket_data = None
        try:
            from app.data.premarket_enrichment import collect_premarket_data
            _syms = [(t.get("symbol") or "").replace("US.", "") for t in targets]
            premarket_data = collect_premarket_data(watchlist_symbols=[s for s in _syms if s][:15])
        except Exception:
            pass

        # 调用 Deep Research Pipeline (内部会跳过已有 deep_research 的 target)
        try:
            from app.plan.deep_research_pipeline import run_deep_research_batch
            logger.info(
                f"[DeepResearch] 开始为 {min(len(targets), 3)} 个 targets 生成 Deep Research"
            )
            results = await run_deep_research_batch(
                targets=targets,
                premarket_data=premarket_data,
                max_targets=3,
            )
        except Exception as e:
            logger.exception(f"[DeepResearch] 引擎调用失败: {e}")
            return api_error("BATTLE_PLAN_ERROR", f"生成失败: {e}", 500)

        # 回写到对应 target
        result_map = {
            (r.get("symbol") or "").upper(): r
            for r in (results or [])
        }
        success_count = 0
        for t in targets:
            sym = (t.get("symbol") or "").upper()
            r = result_map.get(sym)
            if r and r.get("deep_research"):
                t["deep_research"] = r["deep_research"]
                if r["deep_research"].get("executive_summary"):
                    t["executive_summary"] = r["deep_research"]["executive_summary"]
                success_count += 1

        plan["_deep_research_generated_at"] = datetime.now().isoformat()

        try:
            await _atomic_save_plan(plan)
        except Exception as e:
            logger.error(f"[DeepResearch] 保存 plan 失败: {e}")
            return api_error("BATTLE_PLAN_ERROR", f"保存失败: {e}", 500)

        logger.info(
            f"[DeepResearch] 完成: {success_count}/{min(len(targets), 3)} targets"
        )
        return {
            "success": True,
            "generated": success_count,
            "total": min(len(targets), 3),
        }

    @router.post("/api/battle-plan/deep-research/{symbol}")
    async def generate_deep_research_single(symbol: str, request: Request):
        """按需为单个 target 生成 Deep Research 级别分析。

        如果 target 已有 deep_research 且非降级方案，跳过重新生成（避免重复 LLM 调用）。
        传入 ?force=true 可强制重新生成。
        """
        if not _PLAN_PATH.exists():
            return api_error("BATTLE_PLAN_ERROR", "计划文件不存在", 404)

        try:
            with open(_PLAN_PATH) as f:
                plan = json.load(f)
        except Exception as e:
            return api_error("BATTLE_PLAN_ERROR", f"读取计划失败: {e}", 500)

        # 找到对应 target
        target = None
        sym_full = symbol if symbol.startswith("US.") else f"US.{symbol}"
        for t in plan.get("targets", []):
            if t.get("symbol", "").upper() == sym_full.upper():
                target = t
                break

        if not target:
            return api_error("BATTLE_PLAN_ERROR", f"未找到 target: {symbol}", 404)

        # 缓存检查: 已有非降级 deep_research 则跳过
        force = request.query_params.get("force", "").lower() in ("1", "true", "yes")
        existing_dr = target.get("deep_research")
        if existing_dr and isinstance(existing_dr, dict) and not force:
            is_fallback = existing_dr.get("_meta", {}).get("synthesis_fallback", False)
            has_verdict = bool(existing_dr.get("verdict", {}).get("action"))
            if has_verdict and not is_fallback:
                logger.info(f"[DeepResearch] {sym_full} 已有有效 deep_research, 跳过 (force={force})")
                return {
                    "success": True,
                    "symbol": sym_full,
                    "cached": True,
                    "action": existing_dr.get("verdict", {}).get("action", ""),
                    "conviction": existing_dr.get("verdict", {}).get("conviction", 0),
                    "summary": existing_dr.get("executive_summary", ""),
                }

        # 盘前数据: 用活的内存 collector (premarket_enrichment.json 已无写方, 2026-07-11)
        premarket_data = None
        try:
            from app.data.premarket_enrichment import collect_premarket_data
            _sym = (target.get("symbol") or "").replace("US.", "")
            premarket_data = collect_premarket_data(watchlist_symbols=[_sym] if _sym else None)
        except Exception:
            pass

        try:
            from app.plan.deep_research_pipeline import run_deep_research_for_target
            logger.info(f"[DeepResearch] 为 {sym_full} 生成 Deep Research (force={force})")
            dr = await run_deep_research_for_target(target, premarket_data)
        except Exception as e:
            logger.exception(f"[DeepResearch] {sym_full} error: {e}")
            return api_error("BATTLE_PLAN_ERROR", f"生成失败: {e}", 500)

        if not dr:
            return api_error("BATTLE_PLAN_ERROR", "生成失败", 500)

        # 回写
        target["deep_research"] = dr
        if dr.get("executive_summary"):
            target["executive_summary"] = dr["executive_summary"]

        try:
            await _atomic_save_plan(plan)
        except Exception as e:
            logger.error(f"[DeepResearch] 保存失败: {e}")

        return {
            "success": True,
            "symbol": sym_full,
            "cached": False,
            "action": dr.get("verdict", {}).get("action", ""),
            "conviction": dr.get("verdict", {}).get("conviction", 0),
            "summary": dr.get("executive_summary", ""),
        }

    # ═══════════════════════════════════════════════════════════
    #  Action Synthesis API — 统一行动清单 (2026-06-14 重构)
    # ═══════════════════════════════════════════════════════════

    @router.get("/api/battle-plan/actions")
    async def get_action_plan():
        """合成统一行动清单: 4 层 (必须执行/今日机会/持仓监控/今日回避)

        纯本地数据: plan JSON + SQLite DB, 无 broker API 调用, 确保快速响应
        """
        # ── 1. 加载战前计划 ──
        plan = {}
        if _PLAN_PATH.exists():
            try:
                with open(_PLAN_PATH) as f:
                    plan = json.load(f)
            except Exception:
                pass

        # 过期护栏: 冻结的旧计划不作为今日"机会"展示 (仅清 targets, 保留持仓相关)
        from app.web_dashboard.data_service import battle_plan_is_stale
        if battle_plan_is_stale(plan):
            plan["_stale"] = True
            plan["targets"] = []

        targets = plan.get("targets", [])
        position_actions = plan.get("position_actions", [])
        restricted = set(plan.get("restricted", []))
        regime = plan.get("market_regime", "UNKNOWN")
        confidence = plan.get("confidence", 0)
        # ── 2. 从 SQLite 读取持仓 (纯本地, 无 broker 调用) ──
        positions = []
        stop_distances = []
        account = {}
        ds = get_data_service()
        try:
            rows = await ds.db_query(
                "SELECT symbol, direction, size, avg_entry_price, "
                "stop_loss_price, trailing_stop_price, "
                "stop_loss_triggered, trailing_stop_triggered, owner "
                "FROM open_positions WHERE size > 0"
            )
            for r in rows:
                sym = r.get("symbol", "")
                direction = r.get("direction", "LONG")
                size = float(r.get("size", 0) or 0)
                entry = float(r.get("avg_entry_price", 0) or 0)
                sl = float(r.get("stop_loss_price", 0) or 0)
                ts = float(r.get("trailing_stop_price", 0) or 0)
                # Use DataService quote cache if available (no broker API call)
                cur = 0
                try:
                    quotes = ds._quote_cache if hasattr(ds, '_quote_cache') else {}
                    cur = float(quotes.get(sym, 0) or 0)
                except Exception:
                    pass
                if cur <= 0:
                    cur = entry  # fallback to entry price

                pnl_pct = 0
                unrealized = 0
                if entry > 0 and cur > 0 and size > 0:
                    if direction == "LONG":
                        pnl_pct = (cur - entry) / entry * 100
                        unrealized = (cur - entry) * size
                    else:
                        pnl_pct = (entry - cur) / entry * 100
                        unrealized = (entry - cur) * size

                sl_dist = 0
                if sl > 0 and cur > 0:
                    if direction == "LONG":
                        sl_dist = (cur - sl) / cur * 100
                    else:
                        sl_dist = (sl - cur) / cur * 100

                positions.append({
                    "symbol": sym,
                    "direction": direction,
                    "qty": size,
                    "entry_price": entry,
                    "cur_price": cur,
                    "market_val": cur * size,
                    "pnl_pct": round(pnl_pct, 1),
                    "unrealized_pnl": round(unrealized, 2),
                    "stop_loss_price": sl,
                    "trailing_stop_price": ts,
                    "stop_loss_triggered": bool(r.get("stop_loss_triggered")),
                    "trailing_stop_triggered": bool(r.get("trailing_stop_triggered")),
                    "sl_distance_pct": round(sl_dist, 1),
                    "owner": r.get("owner", "auto"),
                })
        except Exception as e:
            logger.warning(f"[ActionPlan] DB position read failed: {e}")

        # ── 3. 风险预算 (纯内存, 无 broker 调用) ──
        risk_budget = {}
        try:
            from app.risk.capital_safety import get_capital_safety_system
            cs = get_capital_safety_system()
            equity = float(getattr(cs._state, 'current_equity', 0) or 0)
            buying_power = float(getattr(cs._state, 'buying_power', 0) or 0)
            water_level = cs._state.water_level.value if cs._state.water_level else "green"
            drawdown = float(getattr(cs._state, 'drawdown_pct', 0) or 0) * 100
            risk_budget = {
                "equity": round(equity, 2),
                "buying_power": round(buying_power, 2),
                "water_level": water_level,
                "drawdown_pct": round(drawdown, 2),
                "positions_count": len(positions),
                "max_positions": 8,
                "available_slots": max(0, 8 - len(positions)),
            }
            long_mv = sum(p["market_val"] for p in positions if p["direction"] == "LONG")
            short_mv = sum(p["market_val"] for p in positions if p["direction"] == "SHORT")
            gross = long_mv + short_mv
            risk_budget["long_mv"] = round(long_mv, 2)
            risk_budget["short_mv"] = round(short_mv, 2)
            risk_budget["gross_exposure"] = round(gross, 2)
            risk_budget["exposure_pct"] = round(gross / equity * 100, 1) if equity > 0 else 0
            risk_budget["can_open_new"] = risk_budget["available_slots"] > 0 and buying_power > 5000
        except Exception as e:
            logger.debug(f"[ActionPlan] risk budget failed: {e}")

        # ── 4. 加载 SignalValidator verdicts (可选, 失败不影响) ──
        validator_map = {}
        try:
            from app.recommendations.signal_validator import SignalValidator
            sv = SignalValidator()
            for t in targets:
                sym = (t.get("symbol", "") or "").replace("US.", "")
                direction = t.get("direction", "LONG")
                try:
                    verdict = sv.validate(sym, direction, regime)
                    if verdict and isinstance(verdict, dict):
                        validator_map[sym] = verdict
                except Exception:
                    pass
        except ImportError:
            pass  # SignalValidator not available
        except Exception:
            pass

        # ── 5. 合成行动清单 ──
        actions = {
            "must_do": [],      # Layer 1: 必须执行
            "opportunities": [], # Layer 2: 今日机会
            "monitor": [],       # Layer 3: 持仓监控
            "avoid": [],         # Layer 4: 今日回避
        }

        # 2026-07-14: 软删除已改为物理DELETE, closed_by_sync行不再存在.
        # 保留 is_anomalous 过滤 (stale_not_in_broker 仍用软标记 + size=0).
        ghost_symbols = set()
        try:
            import sqlite3
            # 2026-08-28 (C类·死库伪装): 原硬编码 data/ftseek_us_live.db — sim 模式下该文件
            #   是 2026-07-19 起零写入的陈旧同名单 (实测今日本库 anomalous=0, 该死库=1 笔
            #   US.MU @06-25), 过滤器一边过滤幽灵标的、一边对今天的真实异常完全失明。
            #   改走模式感知解析器 (TRADE_ENV=0 时它本身就返回 live 库)。
            from app.core.db_utils import get_main_db
            db_path = Path(get_main_db())
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))
                cur = conn.cursor()
                # SIM 隔离 (2026-09-05, DEB-20260905 §13.15 · 读侧): 这里取的是"幽灵标的"
                # 用来过滤**实盘**看板的目标池。TRADE_ENV=1 时本库是 sim 库 ⇒ 原查询读的是
                # **sim 行**的 is_anomalous, 拿 sim 的异常去过滤实盘标的 = 读侧串味。
                from app.web_dashboard.env_scope import real_only
                cur.execute(f"""
                    SELECT symbol FROM open_positions
                    WHERE is_anomalous=1 AND {real_only(db_path)}
                """)
                ghost_symbols = {row[0] for row in cur.fetchall()}
                conn.close()
                if ghost_symbols:
                    logger.info(f"[BattlePlan] Filtered {len(ghost_symbols)} anomalous positions: {ghost_symbols}")
        except Exception as e:
            logger.debug(f"[BattlePlan] Ghost position query error: {e}")
        
        # --- Layer 1: 必须执行 (保护触发 + 紧急退出) ---
        for p in positions:
            sym = p.get("symbol", "")
            bare = sym.replace("US.", "")
            
            # Skip ghost/anomalous positions (already closed by broker)
            if sym in ghost_symbols:
                continue
            
            if p.get("stop_loss_triggered"):
                actions["must_do"].append(_build_action(
                    "EXIT", sym, p.get("direction", "LONG"),
                    signal="STRONG",
                    reason="止损已触发",
                    detail=f"现价 ${p.get('cur_price',0):.2f} 已跌破止损 ${p.get('stop_loss_price',0):.2f}",
                    trigger="已触发",
                    evidence=["protection_monitor:止损触发"],
                    action_url=f"/trade?symbol={sym}&op=CLOSE",
                ))
            elif p.get("trailing_stop_triggered"):
                actions["must_do"].append(_build_action(
                    "EXIT", sym, p.get("direction", "LONG"),
                    signal="STRONG",
                    reason="追踪止盈已触发",
                    detail=f"现价 ${p.get('cur_price',0):.2f} 跌破追踪止盈线 ${p.get('trailing_stop_price',0):.2f}",
                    trigger="已触发",
                    evidence=["protection_monitor:追踪止盈触发"],
                    action_url=f"/trade?symbol={sym}&op=CLOSE",
                ))

        # Position actions (EXIT priority) — cross-check with SQLite open_positions
        # Filter out stale entries (closed positions, filtered options, ghost positions)
        live_symbols = {p["symbol"] for p in positions}
        
        for pa in position_actions:
            if pa.get("action", "").upper() in ("EXIT", "CLOSE", "REDUCE"):
                sym = pa.get("symbol", "")
                # Skip if position not in live DB (stale/option/closed)
                if sym not in live_symbols:
                    continue
                # Skip ghost/anomalous positions (already closed by broker)
                if sym in ghost_symbols:
                    continue
                if not any(a["symbol"] == sym for a in actions["must_do"]):
                    actions["must_do"].append(_build_action(
                        pa["action"].upper(), sym, pa.get("direction", "LONG"),
                        signal="MEDIUM",
                        reason=pa.get("reason", "建议退出"),
                        detail=pa.get("detail", ""),
                        trigger=pa.get("trigger", ""),
                        evidence=[f"plan:{pa.get('reason','')}"],
                        action_url=f"/trade?symbol={sym}&op=CLOSE",
                    ))

        # --- Layer 2: 今日机会 (新开仓 targets) ---
        for t in targets:
            sym = t.get("symbol", "")
            bare = sym.replace("US.", "")
            if sym in restricted or bare in restricted:
                continue  # goes to avoid layer

            # Signal strength calculation
            sources_agreeing = 0
            evidence = []

            # Source 1: Plan confidence (from debate)
            plan_conf = float(t.get("confidence", 0) or 0)
            # Normalize: target confidence may be 0-100 integer or 0-1 decimal
            if plan_conf > 1:
                plan_conf = plan_conf / 100
            if plan_conf >= 0.7:
                sources_agreeing += 1
                evidence.append(f"辩论置信度:{plan_conf:.0%}")
            elif plan_conf >= 0.5:
                evidence.append(f"辩论置信度:{plan_conf:.0%}(中等)")

            # Source 2: R:R ratio
            entry = t.get("entry_zone", [0, 0])
            sl = float(t.get("stop_loss", 0) or 0)
            tp = float(t.get("take_profit", 0) or 0)
            if entry and len(entry) == 2 and sl > 0 and tp > 0:
                mid = (entry[0] + entry[1]) / 2
                if t.get("direction") == "LONG":
                    reward = tp - mid
                    risk = mid - sl
                else:
                    reward = mid - tp
                    risk = sl - mid
                rr = reward / risk if risk > 0 else 0
                if rr >= 2.0:
                    sources_agreeing += 1
                    evidence.append(f"R:R={rr:.1f}:1")
                elif rr >= 1.0:
                    evidence.append(f"R:R={rr:.1f}:1(一般)")
            else:
                rr = 0

            # Source 3: SignalValidator
            sv = validator_map.get(bare, {})
            sv_verdict = sv.get("verdict", "")
            sv_n = sv.get("n", 0)
            sv_wr = sv.get("win_rate", 0)
            if sv_verdict == "GREEN":
                sources_agreeing += 1
                if sv_n >= 30:
                    evidence.append(f"历史验证:GREEN n={sv_n} WR={sv_wr:.0%}")
                elif sv_n >= 10:
                    evidence.append(f"历史验证:GREEN n={sv_n}(样本有限)")
                else:
                    evidence.append(f"历史验证:GREEN n={sv_n}")
            elif sv_verdict == "RED":
                # Hard block
                actions["avoid"].append(_build_action(
                    "AVOID", sym, t.get("direction", "LONG"),
                    signal="BLOCKED",
                    reason=f"历史模式RED (n={sv_n}, 胜率{sv_wr:.0%})",
                    detail=sv.get("reason", "不建议交易"),
                    evidence=[f"SignalValidator:RED n={sv_n}"],
                ))
                continue
            elif sv_verdict == "YELLOW":
                evidence.append(f"历史验证:YELLOW(谨慎)")

            # Source 4: Technical confirmation
            tech = t.get("_technical", {})
            if tech:
                rsi = tech.get("rsi", 50)
                trend = tech.get("trend", "")
                if t.get("direction") == "LONG" and rsi < 70 and trend in ("UP", "BULL"):
                    sources_agreeing += 1
                    evidence.append(f"技术确认:RSI={rsi:.0f} {trend}")
                elif t.get("direction") == "SHORT" and rsi > 30 and trend in ("DOWN", "BEAR"):
                    sources_agreeing += 1
                    evidence.append(f"技术确认:RSI={rsi:.0f} {trend}")

            # Determine signal strength
            if sources_agreeing >= 3:
                signal = "STRONG"
            elif sources_agreeing >= 2:
                signal = "MEDIUM"
            else:
                signal = "WEAK"

            # Risk budget check
            can_trade = risk_budget.get("can_open_new", False)
            if not can_trade:
                evidence.append("⛔ 风险预算: 敞口已满或购买力不足")

            # Urgency / timing
            urgency = str(t.get("urgency", "")).upper()
            timing = ""
            if urgency in ("HIGH", "IMMEDIATE"):
                timing = "开盘即执行"
            elif urgency == "MEDIUM":
                timing = "10:00后 (等opening_drive结束)"
            else:
                timing = "条件满足时"

            # Current price check
            cur = t.get("_current_price", 0)
            entry_zone = t.get("entry_zone", [0, 0])
            trigger_status = "待触发"
            if cur and entry_zone and len(entry_zone) == 2:
                if entry_zone[0] <= cur <= entry_zone[1]:
                    trigger_status = "✅ 在入场区间内"
                elif t.get("direction") == "LONG" and cur > entry_zone[1]:
                    trigger_status = f"⚠ 已超出入场区间 (现价${cur:.2f} > ${entry_zone[1]:.2f})"
                elif t.get("direction") == "SHORT" and cur < entry_zone[0]:
                    trigger_status = f"⚠ 已超出入场区间 (现价${cur:.2f} < ${entry_zone[0]:.2f})"

            actions["opportunities"].append(_build_action(
                "NEW", sym, t.get("direction", "LONG"),
                signal=signal,
                reason=t.get("rationale", "")[:100],
                detail=(
                    f"入场: ${entry_zone[0]:.2f}-${entry_zone[1]:.2f}" if entry_zone else ""
                    + (f" · SL ${sl:.2f}" if sl else "")
                    + (f" · TP ${tp:.2f}" if tp else "")
                    + (f" · R:R {rr:.1f}:1" if rr else "")
                    + f"\n{trigger_status}"
                ),
                trigger=timing,
                evidence=evidence,
                can_trade=can_trade,
                current_price=cur,
                plan_confidence=plan_conf,
            ))

        # --- Layer 3: 持仓监控 ---
        for p in positions:
            sym = p.get("symbol", "")
            bare = sym.replace("US.", "")
            # Skip if already in must_do
            if any(a["symbol"] == sym for a in actions["must_do"]):
                continue

            sl_dist = p.get("sl_distance_pct", 99)
            tp_dist = 0  # TP distance not computed in lightweight mode

            # Check if there's a position_action for this
            pa = next((pa for pa in position_actions if pa.get("symbol") == sym), None)
            pa_action = pa.get("action", "HOLD").upper() if pa else "HOLD"

            # Condition monitoring
            conditions = []
            if sl_dist < 3:
                conditions.append(f"⚠ 距止损仅{sl_dist:.1f}%")
            if pa_action in ("ADD", "ADJUST_SL"):
                conditions.append(f"建议: {pa.get('reason', pa_action)}")

            # Thesis status (simplified)
            thesis = "待填写"

            equity_val = risk_budget.get("equity", 1)

            actions["monitor"].append({
                "symbol": sym,
                "direction": p.get("direction", "LONG"),
                "quantity": p.get("qty", 0),
                "pnl_pct": round(p.get("pnl_pct", 0), 1),
                "pnl_usd": round(p.get("unrealized_pnl", 0), 2),
                "equity_pct": round(p.get("market_val", 0) / risk_budget.get("equity", 1) * 100, 1) if risk_budget.get("equity") else 0,
                "sl_distance_pct": round(sl_dist, 1),
                "tp_distance_pct": round(tp_dist, 1),
                "thesis_status": thesis,
                "conditions": conditions,
                "hold_days": p.get("hold_days"),
                "alert_level": "danger" if sl_dist < 2 else "warning" if sl_dist < 5 else "normal",
            })

        # Sort monitor by urgency (danger first)
        alert_order = {"danger": 0, "warning": 1, "normal": 2}
        actions["monitor"].sort(key=lambda x: alert_order.get(x.get("alert_level", "normal"), 2))

        # --- Layer 4: 今日回避 ---
        for sym in restricted:
            if not sym.startswith("US."):
                sym_full = "US." + sym
            else:
                sym_full = sym
            actions["avoid"].append(_build_action(
                "AVOID", sym_full, "",
                signal="BLOCKED",
                reason="在禁区名单中",
                evidence=["plan:restricted"],
            ))

        # Earnings-based avoidance
        try:
            earnings_data = plan.get("_enrichment", {}).get("earnings", [])
            for e in earnings_data:
                esym = e.get("symbol", "")
                if esym and not any(a["symbol"].replace("US.", "") == esym for a in actions["avoid"]):
                    actions["avoid"].append(_build_action(
                        "AVOID", f"US.{esym}", "",
                        signal="BLOCKED",
                        reason="今日有财报",
                        detail=e.get("detail", ""),
                        evidence=["earnings_calendar"],
                    ))
        except Exception:
            pass

        # HIGH_VOL regime warning
        if regime in ("HIGH_VOL", "DEGRADED"):
            actions["avoid"].append(_build_action(
                "AVOID", "(所有标的)", "LONG",
                signal="BLOCKED",
                reason=f"当前 regime={regime}，历史 LONG 胜率极低",
                evidence=[f"market_regime:{regime}"],
            ))

        # ── 6. Morning Brief (一句话总结) ──
        brief = _generate_morning_brief(regime, risk_budget, actions, plan)

        # ── 7. 建议观望? ──
        should_pass = False
        pass_reason = ""
        strong_or_medium = [a for a in actions["opportunities"] if a.get("signal") in ("STRONG", "MEDIUM")]
        if not strong_or_medium and not actions["must_do"]:
            should_pass = True
            pass_reason = "无高确信度机会，建议今日观望"
        elif not risk_budget.get("can_open_new", False) and not actions["must_do"]:
            should_pass = True
            pass_reason = "敞口已满且无紧急退出，建议观望为主"

        return {
            "date": plan.get("date", datetime.now().strftime("%Y-%m-%d")),
            "regime": regime,
            "brief": brief,
            "should_pass": should_pass,
            "pass_reason": pass_reason,
            "risk_budget": risk_budget,
            "actions": actions,
            "generated_at": datetime.now().isoformat(),
        }

    # ═══════════════════════════════════════════════════════════
    #  Decision Hub API — 统一决策仓 (Task #20, 2026-06-15)
    # ═══════════════════════════════════════════════════════════

    @router.get("/api/decision-hub")
    async def get_decision_hub(request: Request):
        """统一决策仓端点 - 聚合 5 个决策引擎输出

        - day_score:        今日交易适合度评分
        - primary_target:   主攻目标 (is_primary=true 的 target, 含 live_confidence)
        - targets:          全部 targets 含实时确信度
        - position_advice:  持仓动态管理建议
        - portfolio_health: 板块集中度 / 组合健康度
        """
        result: dict = {}

        # ── 0. 基础数据: account + positions (复用 DataService 缓存) ──
        ds = get_data_service()
        available_cash = 100000.0
        position_count = 0
        positions_data: list = []  # 给 position_intelligence 用
        positions_for_check: list = []  # 给 portfolio_check 用

        try:
            acc_raw = await ds.get_account_raw()
            if acc_raw:
                acc = ds.to_usd(acc_raw)
                available_cash = float(acc.get("avl_cash") or acc.get("cash") or 100000.0)
        except Exception as e:
            logger.debug(f"[decision-hub] account fetch error: {e}")

        # ── 持仓: 单一持仓网关 (Futu 为主 + SQLite 元数据 + 幽灵对账) ──
        try:
            positions_data = await ds.get_enriched_positions()
            for pos in positions_data:
                positions_for_check.append({
                    "symbol": pos["symbol"],
                    "direction": pos["direction"],
                })
            position_count = len(positions_data)
        except Exception as e:
            logger.debug(f"[decision-hub] positions fetch error: {e}")

        # ── 1. Day Score ──
        try:
            from app.plan.day_score import calculate_day_score
            day = calculate_day_score(
                available_cash=available_cash, position_count=position_count
            )
            result["day_score"] = {
                "score": day.score,
                "level": day.level,
                "reasons": day.reasons,
                "daily_risk_budget": day.daily_risk_budget,
                "risk_budget_pct": day.risk_budget_pct,
                "avoid_windows": day.avoid_windows,
                "suggestions": day.suggestions,
            }
        except Exception as e:
            logger.warning(f"[decision-hub] day_score error: {e}")
            result["day_score"] = None

        # ── 2. Plan: targets + Live Conviction ──
        plan: dict = {}
        try:
            if _PLAN_PATH.exists():
                with open(_PLAN_PATH) as f:
                    plan = json.load(f)
        except Exception as e:
            logger.debug(f"[decision-hub] plan load error: {e}")
            plan = {}

        # 过期护栏: 旧 discovery 链已停用, 冻结计划不作为今日交易目标展示
        from app.web_dashboard.data_service import battle_plan_is_stale
        _plan_stale = battle_plan_is_stale(plan)
        result["plan_stale"] = _plan_stale
        result["plan_date"] = plan.get("date", "")
        if _plan_stale:
            plan["targets"] = []

        # 实时报价补充到 targets
        targets_raw = plan.get("targets", []) or []
        try:
            symbols = [t.get("symbol", "") for t in targets_raw if t.get("symbol")]
            if symbols:
                quotes = await asyncio.wait_for(
                    ds.get_batch_quotes(symbols), timeout=4
                )
                for t in targets_raw:
                    sym = t.get("symbol", "")
                    if sym in quotes:
                        t["_current_price"] = round(quotes[sym], 2)
        except Exception:
            pass

        try:
            from app.plan.conviction_tracker import get_conviction_tracker
            tracker = get_conviction_tracker()
            # 若 tracker 尚未初始化, 用当前 plan 初始化
            if not tracker.get_all_convictions() and targets_raw:
                try:
                    tracker.init_from_plan(plan)
                except Exception:
                    pass

            primary = None
            all_targets = []
            for t in targets_raw:
                sym = t.get("symbol", "")
                conv = tracker.get_conviction(sym)
                # 触发实时刷新 (融入当前价格)
                try:
                    if conv is not None:
                        tracker.update_conviction(sym, market_context={
                            "current_price": t.get("_current_price", 0),
                        })
                        conv = tracker.get_conviction(sym)
                except Exception:
                    pass

                # R:R
                entry_zone = t.get("entry_zone") or []
                sl_v = float(t.get("stop_loss", 0) or 0)
                tp_v = float(t.get("take_profit", 0) or 0)
                rr = t.get("risk_reward", 0)
                if (not rr) and entry_zone and len(entry_zone) == 2 and sl_v > 0 and tp_v > 0:
                    mid = (float(entry_zone[0]) + float(entry_zone[1])) / 2
                    if t.get("direction") == "SHORT":
                        reward = mid - tp_v
                        risk = sl_v - mid
                    else:
                        reward = tp_v - mid
                        risk = mid - sl_v
                    rr = round(reward / risk, 2) if risk > 0 else 0

                base_conf_raw = t.get("confidence", 0) or 0
                base_conf = int(base_conf_raw * 100) if 0 < base_conf_raw <= 1 else int(base_conf_raw)

                target_info = {
                    "symbol": sym,
                    "direction": t.get("direction", "LONG"),
                    "rank_position": t.get("rank_position", 0),
                    "is_primary": bool(t.get("is_primary", False)),
                    "why_primary": t.get("why_primary", ""),
                    "base_confidence": base_conf,
                    "live_confidence": conv.live_confidence if conv else base_conf,
                    "conviction_status": conv.status if conv else "ACTIVE",
                    "decay_reasons": conv.decay_reasons if conv else [],
                    "entry_zone": entry_zone,
                    "stop_loss": sl_v or None,
                    "take_profit": tp_v or None,
                    "risk_reward": rr,
                    "trigger_condition": t.get("trigger_condition", ""),
                    "verdict": t.get("verdict", ""),
                    "current_price": t.get("_current_price", 0),
                    "trigger_status": _compute_trigger_status(t),
                }
                all_targets.append(target_info)
                if target_info["is_primary"]:
                    primary = target_info

            if not primary and all_targets:
                primary = all_targets[0]

            result["primary_target"] = primary
            result["targets"] = all_targets
        except Exception as e:
            logger.warning(f"[decision-hub] targets error: {e}")
            result["primary_target"] = None
            result["targets"] = []

        # ── 3. Position Intelligence ──
        try:
            from app.plan.position_intelligence import analyze_all_positions
            advices = analyze_all_positions(positions_data, plan)
            result["position_advice"] = [
                a.to_dict() if hasattr(a, "to_dict") else {
                    "symbol": getattr(a, "symbol", ""),
                    "direction": getattr(a, "direction", ""),
                    "action": getattr(a, "action", ""),
                    "urgency": getattr(a, "urgency", ""),
                    "reason": getattr(a, "reason", ""),
                    "conditions": [],
                    "remaining_rr": getattr(a, "remaining_rr", None),
                    "hold_days": getattr(a, "hold_days", 0),
                    "avg_hold_days": getattr(a, "avg_hold_days", 3),
                    "pnl_pct": getattr(a, "pnl_pct", 0),
                }
                for a in advices
            ]
        except Exception as e:
            logger.warning(f"[decision-hub] position_advice error: {e}")
            result["position_advice"] = []

        # ── 4. Portfolio Health ──
        try:
            from app.plan.portfolio_check import get_sector_concentration
            concentration = get_sector_concentration(positions_for_check) or {}
            if concentration:
                max_sector_name = max(concentration, key=concentration.get)
                max_sector = concentration[max_sector_name]
            else:
                max_sector_name = ""
                max_sector = 0.0
            health_level = "GOOD" if max_sector < 0.4 else ("CAUTION" if max_sector < 0.6 else "RISK")
            result["portfolio_health"] = {
                "sector_concentration": concentration,
                "max_sector_pct": round(max_sector * 100, 1),
                "max_sector_name": max_sector_name,
                "health_level": health_level,
                "position_count": position_count,
            }
        except Exception as e:
            logger.warning(f"[decision-hub] portfolio error: {e}")
            result["portfolio_health"] = None

        # ── 5. Must-do count (来自 position_advice 中 HIGH urgency) ──
        try:
            high_urgency = [a for a in result.get("position_advice", []) if a.get("urgency") == "HIGH"]
            result["must_do_count"] = len(high_urgency)
        except Exception:
            result["must_do_count"] = 0

        result["generated_at"] = datetime.now().isoformat(timespec="seconds")
        return result

    app.include_router(router)


def _build_action(action_type, symbol, direction, signal="MEDIUM", reason="",
                  detail="", trigger="", evidence=None, action_url="",
                  can_trade=True, current_price=0, plan_confidence=0):
    """构造统一行动项"""
    return {
        "type": action_type,
        "symbol": symbol,
        "direction": direction,
        "signal": signal,
        "reason": reason,
        "detail": detail,
        "trigger": trigger,
        "evidence": evidence or [],
        "action_url": action_url,
        "can_trade": can_trade,
        "current_price": current_price,
        "plan_confidence": plan_confidence,
    }


def _generate_morning_brief(regime, risk_budget, actions, plan):
    """生成一句话市场总结"""
    parts = []

    # Regime
    regime_label = {
        "RISK_ON": "偏多", "RISK_OFF": "偏空", "NEUTRAL": "震荡",
        "ROTATION": "轮动", "HIGH_VOL": "高波动", "DEGRADED": "数据不足",
    }.get(regime, regime)
    parts.append(f"市场{regime_label}")

    # Risk budget
    if risk_budget:
        slots = risk_budget.get("available_slots", 0)
        exp_pct = risk_budget.get("exposure_pct", 0)
        if slots == 0:
            parts.append(f"敞口已满({exp_pct:.0f}%)")
        else:
            parts.append(f"可用仓位 {slots}/{risk_budget.get('max_positions', 8)}")

    # Action summary
    must_count = len(actions.get("must_do", []))
    opp_count = len([a for a in actions.get("opportunities", []) if a.get("signal") in ("STRONG", "MEDIUM")])
    if must_count > 0:
        parts.append(f"{must_count}项紧急操作")
    if opp_count > 0:
        parts.append(f"{opp_count}个机会")
    elif must_count == 0:
        parts.append("建议观望")

    return " · ".join(parts)


# ════════════════════════════════════════════════════════════════════
# Task 6 (Spec): AI 辅助建议 API — 为手动交易者提供只读参考
# ════════════════════════════════════════════════════════════════════
@router.get("/api/ai-suggest/{symbol}")
async def get_ai_suggestion(symbol: str):
    """为手动交易者提供 AI 参考建议（纯只读，不影响任何交易逻辑）。

    返回：
    - signal_score: 自动系统对该标的的信号评分 (0-100)
    - direction_suggestion: LONG / SHORT / NEUTRAL
    - tech_summary: 技术面摘要（RSI/MA/趋势）
    - suggested_sl: 基于 ATR 的止损参考价
    - suggested_tp: 基于 ATR 的止盈参考价
    - auto_aligned: 与自动策略信号方向是否一致
    - risk_warnings: 风险提示列表
    - battle_plan_status: 该标的在当前作战计划中的状态
    """
    symbol = symbol.upper().strip()
    if not symbol.startswith("US."):
        symbol = "US." + symbol

    result = {
        "symbol": symbol,
        "signal_score": None,
        "direction_suggestion": "NEUTRAL",
        "tech_summary": "",
        "suggested_sl": None,
        "suggested_tp": None,
        "auto_aligned": False,
        "risk_warnings": [],
        "battle_plan_status": None,
        "current_price": None,
        "data_sources": {},
    }

    # ── 1. 获取当前报价 ──
    cur_price = 0
    try:
        from app.web_dashboard.manual_order_service import get_manual_order_service
        svc = get_manual_order_service()
        quote = svc.get_quote(symbol)
        cur_price = quote.get("cur_price", 0) if quote else 0
        result["current_price"] = round(cur_price, 2) if cur_price else None
        result["data_sources"]["quote"] = "ok" if cur_price else "failed"
    except Exception as e:
        logger.debug(f"[ai-suggest] 报价获取失败 {symbol}: {e}")
        result["data_sources"]["quote"] = "error"

    # ── 2. ATR 计算 SL/TP ──
    try:
        from app.core.di_container import DIContainer
        di = DIContainer()
        atr_prov = di.atr_provider()
        atr_val = atr_prov.get_atr(symbol)
        atr_pct = atr_prov.get_atr_pct(symbol, cur_price if cur_price else None)
        if atr_val and cur_price:
            # 保守止损: 2×ATR; 止盈: 3×ATR (R:R = 1.5:1)
            result["suggested_sl"] = round(cur_price - 2 * atr_val, 2)
            result["suggested_tp"] = round(cur_price + 3 * atr_val, 2)
            result["data_sources"]["atr"] = "ok"
        elif atr_val:
            result["data_sources"]["atr"] = "ok_but_no_price"
        else:
            result["data_sources"]["atr"] = "unavailable"
    except Exception as e:
        logger.debug(f"[ai-suggest] ATR 获取失败 {symbol}: {e}")
        result["data_sources"]["atr"] = "error"

    # ── 3. 作战计划状态 ──
    try:
        # 直接从文件读取计划（比单例更可靠，不依赖进程内状态）
        plan = {}
        if _PLAN_PATH.exists():
            with open(_PLAN_PATH) as _pf:
                plan = json.load(_pf)
        from app.web_dashboard.data_service import battle_plan_is_stale
        targets = [] if battle_plan_is_stale(plan) else plan.get("targets", [])
        plan_target = None
        for t in targets:
            t_sym = (t.get("symbol", "") or "").upper()
            check_sym = symbol.upper()
            check_bare = symbol.replace("US.", "").upper()
            if t_sym == check_sym or t_sym == check_bare:
                plan_target = t
                break

        if plan_target:
            direction = plan_target.get("direction", "NEUTRAL")
            confidence = plan_target.get("confidence", 0)
            verdict = plan_target.get("verdict", "")
            result["battle_plan_status"] = {
                "in_plan": True,
                "direction": direction,
                "confidence": confidence,
                "verdict": verdict,
                "rank_position": plan_target.get("rank_position"),
            }
            # 自动系统方向对齐
            result["signal_score"] = confidence
            result["auto_aligned"] = True
            result["data_sources"]["battle_plan"] = "in_plan"
        else:
            result["battle_plan_status"] = {
                "in_plan": False,
                "direction": "NEUTRAL",
                "confidence": 0,
            }
            result["data_sources"]["battle_plan"] = "not_in_plan"
    except Exception as e:
        logger.debug(f"[ai-suggest] 作战计划读取失败 {symbol}: {e}")
        result["data_sources"]["battle_plan"] = "error"

    # ── 4. 技术面摘要 ──
    tech_parts = []
    direction_signals = []
    try:
        from app.core.di_container import DIContainer
        di = DIContainer()
        atr_prov = di.atr_provider()
        atr_pct_val = atr_prov.get_atr_pct(symbol, cur_price if cur_price else None)
        atr_val_tech = atr_prov.get_atr(symbol)
        if atr_pct_val:
            tech_parts.append("ATR %.1f%%" % (atr_pct_val * 100))
            if atr_pct_val > 0.05:
                result["risk_warnings"].append("高波动 (ATR %.1f%%)" % (atr_pct_val * 100))
    except Exception:
        pass

    # 从作战计划中复用因子数据（如有）
    if result.get("battle_plan_status", {}).get("in_plan"):
        bp = result["battle_plan_status"]
        d = bp.get("direction", "NEUTRAL")
        c = bp.get("confidence", 0)
        result["direction_suggestion"] = d if d in ("LONG", "SHORT") else "NEUTRAL"
        tech_parts.append("计划方向 %s (conf=%d%%)" % (d, c))
        if c < 50:
            result["risk_warnings"].append("自动系统置信度偏低 (%d%%)" % c)

    # 手动持仓检查
    try:
        from app.coordinator.manual_hold_registry import manual_hold_registry
        if manual_hold_registry.is_auto_blocked(symbol):
            hold_info = manual_hold_registry.get_hold_info(symbol)
            result["risk_warnings"].append("该标的当前为手动持仓中(同账户)")
            if hold_info:
                tech_parts.append("手动持仓: %s %d股" % (
                    hold_info.get("direction", "?"),
                    hold_info.get("quantity", 0),
                ))
    except Exception:
        pass

    # 检查 restricted
    try:
        restricted = plan.get("restricted", [])
        for r in restricted:
            r_sym = (r.get("symbol", "") if isinstance(r, dict) else str(r)).upper()
            if r_sym == symbol.upper() or r_sym == symbol.replace("US.", "").upper():
                reason = r.get("reason", "restricted") if isinstance(r, dict) else "restricted"
                result["risk_warnings"].append("该标的在限制名单中: %s" % reason)
    except Exception:
        pass

    result["tech_summary"] = " | ".join(tech_parts) if tech_parts else "数据不足"

    # ── 5. 默认方向推断（如果不在计划中） ──
    if not result["battle_plan_status"] or not result["battle_plan_status"].get("in_plan"):
        # 无计划数据时保持 NEUTRAL
        result["direction_suggestion"] = "NEUTRAL"
        if not result["risk_warnings"]:
            result["risk_warnings"].append("该标的不在今日作战计划中，建议参考盘前分析后再决策")

    return result
