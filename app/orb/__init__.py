"""
ORB 辅线模块 — RFC-056

Opening Range Breakout 独立交易管线:
  - ETF 专属标的池 (与主线零重叠)
  - 5 秒轮询 daemon 线程
  - 3 层防御入场 + 独立退出栈
  - observe → shadow → active 渐进上线

用法:
    from app.orb import get_orb_engine
    engine = get_orb_engine(config=orb_cfg, ...)
    engine.start()
"""

from app.orb.config import ORBConfig
from app.orb.exits import (
    ORBExitDecision,
    ORBFloorStop,
    ORBEODExit,
    ORBExitStack,
)
from app.orb.engine import ORBEngine, ORBPosition, get_orb_engine

__all__ = [
    "ORBConfig",
    "ORBExitDecision",
    "ORBFloorStop",
    "ORBEODExit",
    "ORBExitStack",
    "ORBEngine",
    "ORBPosition",
    "get_orb_engine",
]
