"""
Playbook 路由 — Dashboard 交易指令视图 + 执行 + 反馈

Endpoints:
  GET  /playbook                     — Playbook 页面 (HTML)
  GET  /api/playbook/today           — 今日指令列表
  GET  /api/playbook/summary         — 汇总统计
  GET  /api/playbook/history         — 历史指令
  GET  /api/playbook/instruction/{id} — 指令详情
  GET  /api/playbook/factor-stats    — 因子统计
  GET  /api/playbook/patterns        — Patterns 列表
  POST /api/playbook/execute         — 执行指令
  POST /api/playbook/skip            — 跳过指令
  POST /api/playbook/modify          — 修改指令参数
"""
import logging
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger("ftseek_us.routes.playbook")
router = APIRouter()


def register(app, templates):
    """注册 Playbook 路由"""

    # ========== 页面 ==========

    @router.get("/playbook", response_class=HTMLResponse)
    async def playbook_page(request: Request):
        """Playbook 主页面"""
        return templates.TemplateResponse("playbook.html", {
            "request": request,
            "title": "📋 Playbook - FTSeek-US",
        })

    # ========== 查询 API ==========

    @router.get("/api/playbook/today")
    async def get_today_instructions(date: str = None):
        """获取今日指令"""
        from app.playbook.playbook_service import get_playbook_service
        svc = get_playbook_service()
        instructions = svc.get_today_instructions(trade_date=date)
        return {
            "success": True,
            "data": instructions,
            "count": len(instructions),
        }

    @router.get("/api/playbook/summary")
    async def get_summary(days: int = 30):
        """获取汇总统计"""
        from app.playbook.playbook_service import get_playbook_service
        svc = get_playbook_service()
        summary = svc.get_playbook_summary(days=days)
        return {"success": True, "data": summary}

    @router.get("/api/playbook/history")
    async def get_history(days: int = 30):
        """获取历史指令"""
        from app.playbook.playbook_service import get_playbook_service
        svc = get_playbook_service()
        history = svc.get_history(days=days)
        return {"success": True, "data": history, "count": len(history)}

    @router.get("/api/playbook/instruction/{instruction_id}")
    async def get_instruction(instruction_id: int):
        """获取指令详情"""
        from app.playbook.playbook_service import get_playbook_service
        svc = get_playbook_service()
        inst = svc.get_instruction_detail(instruction_id)
        if not inst:
            return JSONResponse(
                content={"success": False, "error": "指令不存在"},
                status_code=404,
            )
        return {"success": True, "data": inst}

    @router.get("/api/playbook/factor-stats")
    async def get_factor_stats():
        """获取因子统计"""
        from app.playbook.playbook_service import get_playbook_service
        svc = get_playbook_service()
        stats = svc.get_factor_stats()
        return {"success": True, "data": stats}

    @router.get("/api/playbook/patterns")
    async def get_patterns(status: str = None):
        """获取 Patterns"""
        from app.playbook.playbook_service import get_playbook_service
        svc = get_playbook_service()
        patterns = svc.get_patterns(status=status)
        return {"success": True, "data": patterns}

    # ========== 执行 API ==========

    @router.post("/api/playbook/execute")
    async def execute_instruction(request: Request):
        """执行 Playbook 指令"""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                content={"success": False, "error": "请求体无效"},
                status_code=400,
            )

        instruction_id = body.get("instruction_id")
        if not instruction_id:
            return JSONResponse(
                content={"success": False, "error": "缺少 instruction_id"},
                status_code=400,
            )

        from app.playbook.playbook_service import get_playbook_service
        svc = get_playbook_service()
        result = svc.execute_instruction(
            instruction_id=instruction_id,
            acknowledge_risk=body.get("acknowledge_risk", False),
            custom_params=body.get("custom_params"),
        )
        return result

    @router.post("/api/playbook/skip")
    async def skip_instruction(request: Request):
        """跳过 Playbook 指令"""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                content={"success": False, "error": "请求体无效"},
                status_code=400,
            )

        instruction_id = body.get("instruction_id")
        if not instruction_id:
            return JSONResponse(
                content={"success": False, "error": "缺少 instruction_id"},
                status_code=400,
            )

        from app.playbook.playbook_service import get_playbook_service
        svc = get_playbook_service()
        result = svc.skip_instruction(
            instruction_id=instruction_id,
            reason=body.get("reason", ""),
            custom_reason=body.get("custom_reason", ""),
        )
        return result

    @router.post("/api/playbook/modify")
    async def modify_instruction(request: Request):
        """修改指令参数"""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                content={"success": False, "error": "请求体无效"},
                status_code=400,
            )

        instruction_id = body.get("instruction_id")
        modified_field = body.get("modified_field")
        modified_value = body.get("modified_value")

        if not all([instruction_id, modified_field, modified_value is not None]):
            return JSONResponse(
                content={"success": False, "error": "参数不完整"},
                status_code=400,
            )

        from app.playbook.playbook_service import get_playbook_service
        svc = get_playbook_service()
        result = svc.modify_instruction(
            instruction_id=instruction_id,
            modified_field=modified_field,
            modified_value=str(modified_value),
        )
        return result

    # ── 注册路由 ──
    app.include_router(router)
