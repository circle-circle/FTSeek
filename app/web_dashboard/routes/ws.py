"""
WebSocket 实时推送路由 — 替代部分 HTTP 轮询

设计:
  - FastAPI WebSocket endpoint: ws://host:8901/ws/events
  - ConnectionManager 管理活跃连接 (上限 10)
  - 事件总线: asyncio.Queue pub/sub
  - 心跳: 30s ping/pong
  - 断线时前端自动回退到 HTTP polling (无缝降级)

事件类型:
  - quote_update: {symbol, price, change_pct}
  - order_update: {order_id, status, symbol}
  - fill: {symbol, price, qty, side}
  - signal_new: {symbol, direction, confidence, status}
  - risk_alert: {level, message}
  - position_update: {symbol, unrealized_pnl}
"""
import asyncio
import json
import logging
import time
from typing import List, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger("ftseek_us.routes.ws")
router = APIRouter()

# ═══════════════════════════════════════════════════════
#  Connection Manager
# ═══════════════════════════════════════════════════════

_MAX_CONNECTIONS = 10
_HEARTBEAT_INTERVAL = 30  # seconds


class ConnectionManager:
    """WebSocket 连接管理器"""

    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.subscriptions: dict = {}  # ws -> set of event types
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        if len(self.active_connections) >= _MAX_CONNECTIONS:
            await websocket.close(code=1013, reason="Too many connections")
            return False
        await websocket.accept()
        async with self._lock:
            self.active_connections.append(websocket)
            self.subscriptions[id(websocket)] = set()
        logger.info(f"[WS] 连接建立 (当前 {len(self.active_connections)})")
        return True

    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)
            self.subscriptions.pop(id(websocket), None)
        logger.info(f"[WS] 连接断开 (当前 {len(self.active_connections)})")

    async def subscribe(self, websocket: WebSocket, event_types: list):
        """客户端订阅特定事件类型"""
        ws_id = id(websocket)
        if ws_id in self.subscriptions:
            self.subscriptions[ws_id].update(event_types)

    async def broadcast(self, event_type: str, data: dict):
        """广播事件到所有订阅的客户端"""
        if not self.active_connections:
            return

        message = json.dumps({"type": event_type, "data": data, "ts": time.time()})
        disconnected = []

        for ws in self.active_connections:
            ws_id = id(ws)
            subs = self.subscriptions.get(ws_id, set())
            # 如果客户端没有订阅任何类型，或订阅了该类型，则发送
            if not subs or event_type in subs or "*" in subs:
                try:
                    await ws.send_text(message)
                except Exception:
                    disconnected.append(ws)

        # 清理断开的连接
        for ws in disconnected:
            await self.disconnect(ws)


# 全局单例
_manager = ConnectionManager()


def get_ws_manager() -> ConnectionManager:
    return _manager


async def publish_event(event_type: str, data: dict):
    """发布事件到 WebSocket 总线 (供其他模块调用)"""
    await _manager.broadcast(event_type, data)


def publish_event_sync(event_type: str, data: dict):
    """同步版本 — 从非 async 代码中调用 (如 ManualOrderService)"""
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_manager.broadcast(event_type, data))
        else:
            loop.run_until_complete(_manager.broadcast(event_type, data))
    except Exception:
        pass  # WebSocket 推送失败不影响主流程


# ═══════════════════════════════════════════════════════
#  WebSocket Endpoint
# ═══════════════════════════════════════════════════════

def register(app):
    @app.websocket("/ws/events")
    async def websocket_events(websocket: WebSocket):
        connected = await _manager.connect(websocket)
        if not connected:
            return

        try:
            # 发送连接确认
            await websocket.send_json({
                "type": "connected",
                "data": {"message": "WebSocket connected", "heartbeat": _HEARTBEAT_INTERVAL},
                "ts": time.time(),
            })

            # 心跳 + 消息循环
            last_ping = time.time()
            while True:
                try:
                    # 等待客户端消息 (带超时，用于心跳)
                    raw = await asyncio.wait_for(
                        websocket.receive_text(),
                        timeout=_HEARTBEAT_INTERVAL,
                    )

                    # 处理客户端消息
                    try:
                        msg = json.loads(raw)
                        if msg.get("type") == "subscribe":
                            event_types = msg.get("events", [])
                            await _manager.subscribe(websocket, event_types)
                            await websocket.send_json({
                                "type": "subscribed",
                                "data": {"events": event_types},
                            })
                        elif msg.get("type") == "ping":
                            await websocket.send_json({"type": "pong", "ts": time.time()})
                    except (json.JSONDecodeError, KeyError):
                        pass

                    last_ping = time.time()

                except asyncio.TimeoutError:
                    # 超时 = 需要心跳
                    now = time.time()
                    if now - last_ping >= _HEARTBEAT_INTERVAL:
                        await websocket.send_json({
                            "type": "heartbeat",
                            "ts": now,
                            "connections": len(_manager.active_connections),
                        })
                        last_ping = now

        except WebSocketDisconnect:
            await _manager.disconnect(websocket)
        except Exception as e:
            logger.debug(f"[WS] 连接异常: {e}")
            await _manager.disconnect(websocket)
