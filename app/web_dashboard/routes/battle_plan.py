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
from fastapi.responses import JSONResponse

from app.web_dashboard.data_service import get_data_service

logger = logging.getLogger("ftseek_us.routes.battle_plan")
router = APIRouter()

_PLAN_PATH = Path(__file__).parent.parent.parent.parent / "data" / "daily_battle_plan_us.json"

# 并发写锁: 防止多个 Dashboard 操作同时 read-modify-write 导致数据丢失 (2026-06-12 修复)
_write_lock = asyncio.Lock()


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


def register(app, templates):
    @router.get("/plan")
    async def battle_plan_page(request: Request):
        """作战计划独立页面"""
        import os
        is_live = os.getenv("TRADE_ENV", "1").strip() == "0"
        return templates.TemplateResponse("battle_plan.html", {
            "request": request,
            "is_live": is_live,
        })

    @router.get("/api/battle-plan")
    async def get_battle_plan():
        """读取战前计划 + 附加实时价格"""
        if not _PLAN_PATH.exists():
            return JSONResponse(content={"error": "今日无战前计划"}, status_code=404)

        try:
            with open(_PLAN_PATH) as f:
                plan = json.load(f)
        except Exception as e:
            return JSONResponse(content={"error": f"读取失败: {e}"}, status_code=500)

        # ── 判断是否为交易日 ──
        from datetime import datetime as _dt
        _now = _dt.now()
        _is_weekend = _now.weekday() >= 5
        _plan_date = plan.get("date", "")
        _is_stale = _plan_date != _now.strftime("%Y-%m-%d") if _plan_date else False

        if _is_weekend or _is_stale:
            # 非交易日或过期计划: 跳过所有外部 API 调用, 直接返回原始数据
            plan["_is_trading_day"] = False
            plan["_weekend"] = _is_weekend
            plan["_stale"] = _is_stale
            plan["_confirmed"] = plan.get("_confirmed", False)
            plan["_paused"] = plan.get("_paused", False)
            plan["_is_live"] = __import__("os").getenv("TRADE_ENV", "1").strip() == "0"
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

        # ── 新增 (2026-06-12): 附加技术指标 + 板块 + 经济事件 ──
        _SCAN_CACHE = Path(__file__).parent.parent.parent.parent / "data" / "premarket_scan_cache.json"
        if _SCAN_CACHE.exists():
            try:
                with open(_SCAN_CACHE) as sf:
                    scan_data = json.load(sf)
                technicals = scan_data.get("technicals", {})
                sectors = scan_data.get("sectors", {})
                sector_map = scan_data.get("sector_symbol_map", {})
                for t in targets:
                    sym = t.get("symbol", "")
                    if sym in technicals:
                        t["_technical"] = technicals[sym]
                    if sym in sector_map:
                        t["_sector_info"] = sector_map[sym]
                plan["_sectors"] = sectors
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

        # Mark live mode for frontend modeTag sync
        import os as _os
        plan["_is_live"] = _os.getenv("TRADE_ENV", "1").strip() == "0"
        plan["_is_trading_day"] = True

        return plan

    @router.get("/api/pipeline/factor-scores")
    async def get_factor_scores():
        """从factor_screen_latest.json读取所有股票的因子评分，供watchlist等页面使用"""
        _FACTOR_PATH = Path(__file__).parent.parent.parent.parent / "data" / "factor_screen_latest.json"
        if not _FACTOR_PATH.exists():
            return JSONResponse(content={})
        try:
            with open(_FACTOR_PATH) as f:
                data = json.load(f)
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
            return JSONResponse(content=scores)
        except Exception as e:
            logger.warning(f"[factor-scores] read error: {e}")
            return JSONResponse(content={})

    @router.put("/api/battle-plan/targets/{symbol:path}")
    async def update_target(symbol: str, request: Request):
        """修改单个 target 的参数"""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(content={"error": "请求无效"}, status_code=400)

        if not _PLAN_PATH.exists():
            return JSONResponse(content={"error": "计划文件不存在"}, status_code=404)

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
                return JSONResponse(content={"error": f"未找到标的 {symbol}"}, status_code=404)

            await _atomic_save_plan(plan)

            logger.info(f"[BattlePlan] 更新 {symbol}: {body}")
            return {"success": True, "symbol": symbol}
        except Exception as e:
            logger.error(f"[BattlePlan] 更新失败: {e}")
            return JSONResponse(content={"error": str(e)}, status_code=500)

    @router.post("/api/battle-plan/confirm")
    async def confirm_plan():
        """确认战前计划"""
        if not _PLAN_PATH.exists():
            return JSONResponse(content={"error": "计划文件不存在"}, status_code=404)

        try:
            with open(_PLAN_PATH) as f:
                plan = json.load(f)
            plan["_confirmed"] = True
            plan["_confirmed_at"] = datetime.now().isoformat()
            await _atomic_save_plan(plan)
            logger.info("[BattlePlan] 计划已确认")
            return {"success": True}
        except Exception as e:
            return JSONResponse(content={"error": str(e)}, status_code=500)

    @router.post("/api/battle-plan/pause")
    async def pause_plan():
        """暂停今日自动交易"""
        if not _PLAN_PATH.exists():
            return JSONResponse(content={"error": "计划文件不存在"}, status_code=404)

        try:
            with open(_PLAN_PATH) as f:
                plan = json.load(f)
            plan["_paused"] = True
            plan["_paused_at"] = datetime.now().isoformat()
            await _atomic_save_plan(plan)
            logger.info("[BattlePlan] 计划已暂停")
            return {"success": True}
        except Exception as e:
            return JSONResponse(content={"error": str(e)}, status_code=500)

    @router.post("/api/battle-plan/resume")
    async def resume_plan():
        """取消暂停，恢复自动交易 (2026-06-12 新增)"""
        if not _PLAN_PATH.exists():
            return JSONResponse(content={"error": "计划文件不存在"}, status_code=404)

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
            return JSONResponse(content={"error": str(e)}, status_code=500)

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
                # Use cached price from ds if available
                cur = 0
                try:
                    cached = ds._price_cache.get(sym, {})
                    cur = cached.get("price", 0) if isinstance(cached, dict) else 0
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

        # --- Layer 1: 必须执行 (保护触发 + 紧急退出) ---
        for p in positions:
            sym = p.get("symbol", "")
            bare = sym.replace("US.", "")
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

        # Position actions (EXIT priority)
        for pa in position_actions:
            if pa.get("action", "").upper() in ("EXIT", "CLOSE", "REDUCE"):
                sym = pa.get("symbol", "")
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
