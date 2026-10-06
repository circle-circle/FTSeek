"""
系统路由 — 服务状态 + 模式切换 + 重启
"""
import logging
import os
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.web_dashboard.data_service import get_data_service

logger = logging.getLogger("ftseek_us.routes.system")
router = APIRouter()


def register(app, templates):
    from app.web_dashboard.mode_switch_service import (
        get_current_mode, switch_mode as do_switch_mode,
    )

    @router.get("/mode-switch", response_class=HTMLResponse)
    async def mode_switch_page(request: Request):
        return __import__("fastapi").RedirectResponse(url="/system", status_code=301)

    @router.get("/system", response_class=HTMLResponse)
    async def system_page(request: Request):
        return templates.TemplateResponse("system.html", {
            "request": request,
            "title": "系统 - FTSeek-US",
        })

    @router.get("/api/system/status")
    async def get_system_status():
        import subprocess as _sp
        services = {}
        for svc_name in ["ftseek-us", "ftseek-dashboard"]:
            try:
                proc = _sp.run(["systemctl", "is-active", svc_name],
                              capture_output=True, text=True, timeout=5)
                key = "dashboard" if svc_name == "ftseek-dashboard" else "ftseek_us"
                services[key] = proc.stdout.strip()
            except Exception:
                key = "dashboard" if svc_name == "ftseek-dashboard" else "ftseek_us"
                services[key] = "unknown"

        broker_connected = False
        account_id = ""
        try:
            ds = get_data_service()
            svc = ds.svc()
            broker_connected = svc.connect()
            if broker_connected:
                from app.web_dashboard.routes.dashboard import _is_live
                is_live_env = _is_live()
                import os as _os
                account_id = _os.getenv(
                    "REAL_ACCOUNT_ID" if is_live_env else "SIMULATE_ACCOUNT_ID", ""
                )
        except Exception:
            pass

        return {
            "services": services,
            "broker_connected": broker_connected,
            "account_id": f"****{account_id[-4:]}" if len(account_id) > 4 else "****",
        }

    @router.get("/api/mode")
    async def get_mode():
        return get_current_mode()

    @router.post("/api/mode/switch")
    async def switch_mode_api(request: Request):
        try:
            body = await request.json()
        except Exception as e:
            return JSONResponse(content={"success": False, "message": "请求体无效"}, status_code=400)

        target = body.get("target_mode", "") if isinstance(body, dict) else ""
        confirm = body.get("confirm", "") if isinstance(body, dict) else ""
        logger.info(f"[模式切换] target={target}, confirm={'***' if confirm else '(空)'}")
        # H2: 在 executor 中运行阻塞的模式切换 (subprocess + sleep 轮询)
        import asyncio
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, do_switch_mode, target, confirm)
        status_code = 200 if result["success"] else result.get("http_status", 400)
        return JSONResponse(content=result, status_code=status_code)

    @router.post("/api/mode/restart")
    async def restart_service():
        # H2: systemctl restart 在 executor 中运行
        import asyncio
        loop = asyncio.get_running_loop()
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "restart", "ftseek-dashboard",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode == 0:
                return {"success": True, "message": "服务重启中，页面将在几秒后恢复"}
            return {"success": False, "message": f"systemctl 失败: {stderr.decode()[:200]}"}
        except asyncio.TimeoutError:
            return {"success": False, "message": "重启超时"}
        except Exception as e:
            return {"success": False, "message": f"重启失败: {e}"}

    # ── 自动交易开关 (2026-06-13) ──
    _YAML_PATH = "/opt/ftseek/FTSeek-US/config/unified_config.yaml"

    def _read_auto_trading() -> bool:
        """从 YAML 直接读取当前自动交易开关状态 (绕过 ConfigContainer 缓存)"""
        import re
        try:
            with open(_YAML_PATH, "r") as f:
                for line in f:
                    m = re.search(r'auto_trading_enabled:\s*(true|false)', line)
                    if m:
                        return m.group(1) == 'true'
        except Exception:
            pass
        return False

    def _write_auto_trading(enabled: bool) -> bool:
        """修改 YAML 文件中的 auto_trading_enabled 值 (原子写入, 热重载自动生效)"""
        import re
        import os
        import tempfile
        try:
            with open(_YAML_PATH, "r") as f:
                content = f.read()
            # 精确替换 auto_trading_enabled 行
            pattern = r'(  auto_trading_enabled:\s*)(true|false)'
            replacement = f'\\g<1>{"true" if enabled else "false"}'
            new_content = re.sub(pattern, replacement, content)
            if new_content == content:
                return False  # 未找到匹配
            # 原子写入: 先写临时文件再 os.replace, 防止 HotReloader 读到半截文件
            dir_name = os.path.dirname(_YAML_PATH)
            fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as tmp_f:
                    tmp_f.write(new_content)
                os.replace(tmp_path, _YAML_PATH)
            except Exception:
                os.unlink(tmp_path)
                raise
            return True
        except Exception as e:
            logger.error(f"[自动交易开关] YAML 写入失败: {e}")
            return False

    @router.get("/api/auto-trading")
    async def get_auto_trading():
        enabled = _read_auto_trading()
        return {"enabled": enabled}

    @router.post("/api/auto-trading/toggle")
    async def toggle_auto_trading(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        target = body.get("enabled", None) if isinstance(body, dict) else None

        if target is None:
            return JSONResponse(
                content={"success": False, "message": "缺少 enabled 参数 (true/false)"},
                status_code=400)

        target = bool(target)
        current = _read_auto_trading()

        if target == current:
            return {"success": True, "enabled": current, "message": f"已经是{'开启' if current else '关闭'}状态"}

        ok = _write_auto_trading(target)
        if not ok:
            return JSONResponse(
                content={"success": False, "message": "YAML 写入失败"},
                status_code=500)

        label = "🟢 已开启" if target else "🔒 已关闭"
        logger.warning(f"[自动交易开关] {label} (由 {'关闭' if target else '开启'} 切换)")

        # 同步更新 ModeSwitchService (内存开关, 控制信号扫描)
        try:
            from app.web_dashboard.mode_switch_service import get_mode_switch_service
            get_mode_switch_service().set_auto_enabled(target)
        except Exception as _sync_err:
            logger.warning(f"[自动交易开关] ModeSwitchService 同步失败: {_sync_err}")

        return {"success": True, "enabled": target, "message": f"自动交易{label}"}

    app.include_router(router)
