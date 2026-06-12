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

        # 附加实时价格到每个 target
        targets = plan.get("targets", [])
        symbols = [t["symbol"] for t in targets if t.get("symbol")]
        if symbols:
            ds = get_data_service()
            try:
                quotes = await ds.get_batch_quotes(symbols)
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

        return plan

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

    app.include_router(router)
