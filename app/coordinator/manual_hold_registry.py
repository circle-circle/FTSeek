"""
ManualHoldRegistry — 手动持仓标记注册表 (全局单例)

设计目标:
  - 记录所有手动交易中的标的 (持仓 + 挂单)
  - 自动交易 (signal_pipeline / orb_engine) 在扫描前查询此表，命中则跳过
  - 实时更新，无延迟 (不依赖 position_sync 30s 周期)
  - 线程安全 (使用 RLock)

使用方式:
  from app.coordinator.manual_hold_registry import manual_hold_registry

  # 手动开仓/挂单成功后:
  manual_hold_registry.register(symbol, direction, qty, reason, account="real")

  # 手动全部平仓后:
  manual_hold_registry.unregister(symbol, account="real")

  # 自动管线扫描时:
  if manual_hold_registry.is_manual_held(symbol):
      continue
"""
from __future__ import annotations

import os
import threading
import logging
from typing import Any, Dict, Set, Optional
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class ManualHoldInfo:
    symbol: str
    direction: str  # "LONG" / "SHORT"
    quantity: int
    reason: str
    account: str = "real"  # 2026-06-24 Phase 3: 账户维度 (real/sim), 默认 real (手动走实盘)
    registered_at: datetime = field(default_factory=datetime.now)


class ManualHoldRegistry:
    """
    手动持仓标记注册表 (全局单例)
    - 记录所有手动交易中的标的 (持仓 + 挂单)
    - 自动交易在扫描前查询此表, 命中则跳过
    - 实时更新, 无延迟
    """
    _instance = None
    _lock = threading.RLock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._holdings = {}
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self._holdings: Dict[tuple, ManualHoldInfo] = {}
            self._initialized = True

    @staticmethod
    def _normalize_account(account: Optional[str]) -> str:
        normalized = str(account or "").strip().lower()
        if normalized in {"real", "live"}:
            return "real"
        if normalized in {"sim", "simulate"}:
            return "sim"
        return normalized

    def register(self, symbol: str, direction: str, quantity: int, reason: str = "", account: str = "real"):
        """手动开仓/挂单时立即注册. account: real(默认,手动走实盘)/sim."""
        account = self._normalize_account(account)
        with self._lock:
            self._holdings[(account, symbol)] = ManualHoldInfo(
                symbol=symbol,
                direction=direction,
                quantity=quantity,
                reason=reason,
                account=account,
            )
            logger.info(
                f"[ManualHoldRegistry] REGISTERED: {symbol} {direction} "
                f"qty={quantity} account={account} reason={reason}"
            )

    def unregister(self, symbol: str, account: Optional[str] = None):
        """手动全部平仓后注销"""
        normalized_account = self._normalize_account(account) if account is not None else None
        with self._lock:
            keys = [
                key for key in self._holdings
                if key[1] == symbol and normalized_account in (None, key[0])
            ]
            for key in keys:
                info = self._holdings.pop(key)
                logger.info(
                    f"[ManualHoldRegistry] UNREGISTERED: {symbol} account={key[0]} "
                    f"(was {info.direction} qty={info.quantity})"
                )
            if not keys:
                logger.warning(
                    f"[ManualHoldRegistry] UNREGISTER called but {symbol} "
                    f"account={normalized_account} not found"
                )

    def is_manual_held(self, symbol: str, account: Optional[str] = None) -> bool:
        """自动交易调用: 该标的是否被手动占用.

        account=None (默认): 任意账户持有即 True (向后兼容, pre-check 用此).
        account='sim'/'real': 仅该账户持有才 True.
        """
        with self._lock:
            if account is None:
                return any(key[1] == symbol for key in self._holdings)
            account = self._normalize_account(account)
            return (account, symbol) in self._holdings

    # SSOT 回落缓存 (60s TTL) — 见 is_auto_blocked
    _SSOT_TTL_SEC = 60.0
    _ssot_cache: Dict[str, Any] = {"ts": 0.0, "account": "", "symbols": set()}
    # AUTO_TRADE_ACCOUNT (env) ↔ open_positions.account_type 取值
    _ACCOUNT_TYPES = {"sim": ("simulate", "sim"), "real": ("real",)}

    def _ssot_manual_symbols(self, account: str) -> Set[str]:
        """从 open_positions (SSOT) 读手动归属标的, 60s TTL.

        必要性: 本注册表是进程内单例, 面板 (web_dashboard 进程) 下手动单时 register()
        只写进那个进程的 dict, 而闸门读方在主交易进程 → 此前只有重启才 sync。
        """
        import time as _t
        _now = _t.time()
        with self._lock:
            c = ManualHoldRegistry._ssot_cache
            if c["account"] == account and (_now - c["ts"]) < ManualHoldRegistry._SSOT_TTL_SEC:
                return set(c["symbols"])
        symbols: Set[str] = set()
        try:
            import sqlite3
            from app.core import paths as _paths
            db = str(_paths.get_trade_records_db_for_arm(account))
            acc_types = ManualHoldRegistry._ACCOUNT_TYPES.get(account, (account,))
            with sqlite3.connect(db) as conn:
                rows = conn.execute(
                    "SELECT symbol FROM open_positions WHERE owner = 'manual' "
                    f"AND account_type IN ({','.join('?' * len(acc_types))})",
                    acc_types,
                ).fetchall()
            symbols = {r[0] for r in rows}
        except Exception as e:
            # fail-open (与本族 risk 门不同: 本门是协调门非亏损门, DB 瞬时锁死时
            # 全量停自动交易代价大于偶发双管一只标的) — 但盲区必须可见:
            # 此窗口内跨进程手动单不可见, 自动管线可能碰手动标的.
            logger.error(
                f"[ManualHoldRegistry] 🚨 open_positions SSOT 回落查询失败 (fail-open, "
                f"跨进程手动单在接下来 TTL {self._SSOT_TTL_SEC:.0f}s 内不可见): {e}")
            try:
                from app.core.degradation_monitor import get_degradation_monitor
                get_degradation_monitor().record("manual_hold_ssot_fallback", str(e))
            except Exception:
                pass
            return set()
        with self._lock:
            ManualHoldRegistry._ssot_cache.update(
                {"ts": _now, "account": account, "symbols": set(symbols)}
            )
        return symbols

    def is_auto_blocked(self, symbol: str, target_env: Optional[str] = None) -> bool:
        """自动交易闸门: 该标的是否在 *当前自动交易账户* 中被手动占用.

        根据 AUTO_TRADE_ACCOUNT 环境变量决定查 sim 还是 real:
        - AUTO_TRADE_ACCOUNT=sim → 仅当 sim 账户有 manual 持仓时 block
        - AUTO_TRADE_ACCOUNT=real → 仅当 real 账户有 manual 持仓时 block

        解决: 同标的在 SIM(auto) + REAL(manual) 共存时,
        REAL 的 manual 不应阻止 SIM 的 auto 交易.

        2026-08-28 (全链路复查): 内存 dict 未命中时回落 open_positions SSOT — 手动单由
        dashboard 进程注册, 主交易进程此前当日永不感知 (见 _ssot_manual_symbols)。
        """
        _auto_acc = (
            self._normalize_account(target_env)
            if target_env is not None
            else os.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower()
        )
        if self.is_manual_held(symbol, account=_auto_acc):
            return True
        return symbol in self._ssot_manual_symbols(_auto_acc)

    def get_all_manual_symbols(self) -> Set[str]:
        """获取所有手动占用的标的列表"""
        with self._lock:
            return {key[1] for key in self._holdings}

    def get_hold_info(self, symbol: str) -> Optional[ManualHoldInfo]:
        """获取手动持仓详情 (供 Dashboard 展示)"""
        with self._lock:
            _auto_acc = self._normalize_account(os.getenv("AUTO_TRADE_ACCOUNT", "sim"))
            info = self._holdings.get((_auto_acc, symbol))
            if info is not None:
                return info
            for key, candidate in self._holdings.items():
                if key[1] == symbol:
                    return candidate
            return None

    def get_all_holdings(self) -> Dict[str, ManualHoldInfo]:
        """获取所有手动持仓详情"""
        with self._lock:
            return dict(self._holdings)

    def get_manual_count(self) -> int:
        """获取手动持仓数量"""
        with self._lock:
            return len(self._holdings)

    def clear(self):
        """清空所有注册 (仅用于测试 / 重置)"""
        with self._lock:
            self._holdings.clear()
            logger.info("[ManualHoldRegistry] CLEARED all holdings")

    def sync_from_positions(self):
        """
        启动时从 PositionStore 恢复手动持仓到 Registry

        遍历当前所有持仓, 若 source 为 MANUAL (含 metadata.source='manual' /
        owner_strategy='manual_sync' / DB open_positions.owner='manual'),
        则注册到 Registry. 确保重启后 auto pipeline 不会误触手动标的.

        必须在 position_sync 启动对账完成之后、scheduler.run_forever()
        (signal_pipeline / orb_engine 启动) 之前调用.
        """
        try:
            from app.positions.source import PositionSource
            from app.core.position_sync import get_us_position_sync

            pos_sync = get_us_position_sync()
            if not pos_sync or not pos_sync.position_store:
                logger.warning(
                    "[ManualHoldRegistry] Cannot sync: position_sync.position_store not available"
                )
                return

            # P1: 确保 position_sync 已完成至少一次对账 (启动时 _sync_once 是同步的,
            # 但如果因网络/权限延迟, 这里短暂等待 + 最多一次主动触发).
            _waited = 0
            while _waited < 5:
                positions = pos_sync.position_store.all()
                if positions:
                    break
                import time as _time
                _time.sleep(1)
                _waited += 1
            else:
                # 5s 后仍为空, 主动触发一次同步
                try:
                    pos_sync._sync_once()
                except Exception as _se:
                    logger.warning(f"[ManualHoldRegistry] 主动触发 _sync_once 失败: {_se}")
                positions = pos_sync.position_store.all()

            # 兜底: 从 DB open_positions 读取 owner='manual' 的标的
            db_manual_symbols: Set[str] = set()
            try:
                import sqlite3
                from app.core.db_utils import get_main_db
                _conn = sqlite3.connect(str(get_main_db()), timeout=5)
                _cur = _conn.execute(
                    "SELECT symbol FROM open_positions WHERE owner='manual'"
                )
                db_manual_symbols = {row[0] for row in _cur.fetchall()}
                _conn.close()
            except Exception as _dbe:
                logger.debug(
                    f"[ManualHoldRegistry] DB owner=manual scan skipped: {_dbe}"
                )

            restored_count = 0
            for pos in positions:
                try:
                    symbol = getattr(pos, 'symbol', None) or str(pos)

                    # 1) 优先 metadata.source
                    source_str = ""
                    meta = getattr(pos, 'metadata', None)
                    if isinstance(meta, dict):
                        source_str = str(meta.get('source', '') or '')
                    # 2) 退化到 owner_strategy / strategy_id
                    if not source_str:
                        source_str = (
                            getattr(pos, 'owner_strategy', None)
                            or getattr(pos, 'strategy_id', None)
                            or ''
                        )

                    is_manual = False
                    # 严格匹配: PositionSource.MANUAL
                    if PositionSource.from_str(source_str).is_manual():
                        is_manual = True
                    # 宽松匹配: position_sync 写入的 manual_sync 实际语义为手动
                    elif str(source_str).lower() in (
                        'manual', 'manual_sync', 'futu_manual'
                    ):
                        is_manual = True
                    # DB 兜底: open_positions.owner='manual'
                    elif symbol in db_manual_symbols:
                        is_manual = True

                    if not is_manual:
                        continue

                    direction = getattr(pos, 'direction', 'UNKNOWN')
                    if hasattr(direction, 'value'):
                        direction = direction.value
                    direction = str(direction).upper()

                    quantity = (
                        getattr(pos, 'size', None)
                        or getattr(pos, 'quantity', 0)
                        or 0
                    )
                    try:
                        quantity = int(quantity)
                    except Exception:
                        quantity = 0

                    # ponytail: skip closed positions + pass account from metadata
                    if quantity <= 0:
                        continue
                    _acct = "sim" if isinstance(meta, dict) and meta.get("account_type") == "sim" else "real"

                    self.register(
                        symbol, direction, quantity,
                        reason="restored_on_startup",
                        account=_acct,
                    )
                    restored_count += 1
                except Exception as _pe:
                    logger.warning(
                        f"[ManualHoldRegistry] restore single position failed: {_pe}"
                    )

            logger.info(
                f"[ManualHoldRegistry] Startup sync: restored "
                f"{restored_count} manual positions (scanned {len(positions)} total)"
            )
        except Exception as e:
            logger.error(f"[ManualHoldRegistry] Startup sync failed: {e}")


# 全局单例
manual_hold_registry = ManualHoldRegistry()
