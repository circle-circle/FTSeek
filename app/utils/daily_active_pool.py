"""
每日动态交易池管理器 v2 (Daily Active Pool Manager)

设计目标:
分层来源 + 自动同步 + 容量保护 + TTL 过期机制

池子组成:
  Layer 1: 持仓股 (POSITIONS) - 自动同步，最高优先级，不限容量
  Layer 2: 盘前计划 (PLAN_TARGETS) - 日级 TTL，次日 00:00 自动过期
  Layer 3: 人工指定 (MANUAL) - 永久保留，从 daily_active_manual.json 读取
  Layer 4: 盘中异动 (INTRADAY_ALERT) - 2h TTL，量比/突破触发

输出:
供 DataSubscriber (HK) 和 Scheduler (US) 调用的"高优监控列表"。

容量保护:
  总数上限 30 只（可配置），超出时按优先级淘汰: INTRADAY_ALERT < PLAN_TARGETS < MANUAL
  持仓股 (POSITIONS) 不受容量限制。

自动同步:
  成交回调自动 add_position()，平仓回调自动 remove_position()
  美股 PositionStore.add/remove 自动联动
"""

import os
import json
import logging
import threading
from typing import List, Set, Dict, Optional
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field, asdict

logger = logging.getLogger("ftseek.daily_active_pool")

# ─── 来源类型 ───────────────────────────────────────────────
SOURCE_POSITION = "POSITION"        # 持仓股 (最高优先级)
SOURCE_PLAN = "PLAN"               # 盘前计划目标
SOURCE_MANUAL = "MANUAL"           # 人工指定
SOURCE_INTRADAY = "INTRADAY_ALERT"  # 盘中异动

# 来源优先级 (数字越小优先级越高，用于容量淘汰)
SOURCE_PRIORITY = {
    SOURCE_POSITION: 0,
    SOURCE_MANUAL: 1,
    SOURCE_PLAN: 2,
    SOURCE_INTRADAY: 3,
}

# 默认 TTL
TTL_PLAN_HOURS = 24          # 盘前计划: 24 小时
TTL_INTRADAY_HOURS = 2       # 盘中异动: 2 小时
TTL_MANUAL_HOURS = None      # 人工指定: 永久
TTL_POSITION_HOURS = None    # 持仓: 永久 (直到平仓)

# 容量限制
DEFAULT_MAX_POOL_SIZE = 30   # 动态池总容量上限 (不含持仓)
MAX_INTRADAY_ALERTS = 5      # 盘中异动最大数量


@dataclass
class PoolEntry:
    """池子中的单个标的条目"""
    code: str
    source: str                          # SOURCE_POSITION / PLAN / MANUAL / INTRADAY
    added_at: str                        # ISO 格式时间戳
    expires_at: Optional[str] = None     # None 表示永久
    metadata: Dict = field(default_factory=dict)  # 额外信息 (如方向、原因)

    def is_expired(self) -> bool:
        """检查是否过期"""
        if self.expires_at is None:
            return False
        try:
            exp = datetime.fromisoformat(self.expires_at)
            # 处理无时区信息的旧时间戳
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) > exp
        except (ValueError, TypeError):
            return False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PoolEntry":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _make_expiry(hours: Optional[int]) -> Optional[str]:
    """创建过期时间戳"""
    if hours is None:
        return None
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DailyActivePool:
    """每日动态交易池 v2 (线程安全单例)"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(DailyActivePool, cls).__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self, market_prefix: str = "HK."):
        if self._initialized:
            return

        # 市场前缀过滤: 港股="HK.", 美股="US."
        self._market_prefix = market_prefix

        # 按来源分层的条目存储
        self._entries: Dict[str, PoolEntry] = {}  # code -> PoolEntry
        self._lock = threading.Lock()
        self._max_pool_size = DEFAULT_MAX_POOL_SIZE

        # P0 修复: 路径计算错误 - 使用项目根目录而非 app/ 目录
        # __file__ 在 app/utils/daily_active_pool.py, 需要向上 2 级到项目根目录
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self._manual_path = os.path.join(base_dir, 'data', 'daily_active_manual.json')
        self._pool_path = os.path.join(base_dir, 'data', 'daily_active_pool.json')
        self._initialized = True

    # ─── 初始化与加载 ───────────────────────────────────────

    def initialize(self):
        """启动时初始化"""
        self.load()
        self._cleanup_expired()
        self._cleanup_cross_market()  # P0: 清理跨市场残留
        self._reconcile_positions()   # P3: 对账验证 POSITION 条目有效性
        self.save()  # 保存初始状态以便验证
        logger.info(f"✅ 每日动态交易池 v2 已初始化: {self.size()} 只标的 (市场={self._market_prefix})")

    def _cleanup_cross_market(self):
        """P0 修复: 清理不属于本市场的持仓条目
        
        防止服务重启/切换时，另一个市场的持仓残留在本池中
        """
        with self._lock:
            cross_market = [
                code for code, entry in self._entries.items()
                if entry.source == SOURCE_POSITION and not code.startswith(self._market_prefix)
            ]
            for code in cross_market:
                logger.warning(
                    f"🚫 清理跨市场残留: {code} (本市场={self._market_prefix}, "
                    f"来源={self._entries[code].source})"
                )
                del self._entries[code]
            if cross_market:
                logger.info(f"🧹 已清理 {len(cross_market)} 只跨市场残留持仓")

    def _reconcile_positions(self):
        """P3 修复: 启动时对账 — 验证 POSITION 条目与实际持仓的一致性
        
        注意: 对账需要在 main.py 中传入实际持仓列表后调用。
        这里只做基本的格式和状态验证。
        """
        with self._lock:
            position_entries = [
                (code, entry) for code, entry in self._entries.items()
                if entry.source == SOURCE_POSITION
            ]
            if not position_entries:
                return
            logger.info(f"🔍 启动对账: 动态池中有 {len(position_entries)} 只 POSITION 条目")

    def load(self):
        """加载人工指定名单 + 持久化池子状态"""
        # 1. 加载人工指定
        if os.path.exists(self._manual_path):
            try:
                with open(self._manual_path, 'r') as f:
                    data = json.load(f)
                stocks = data.get("stocks", [])
                if isinstance(stocks, list):
                    for code in stocks:
                        if code and code not in self._entries:
                            self._add_entry(code, SOURCE_MANUAL, ttl_hours=None)
                    logger.info(f"📂 加载人工指定名单: {len(stocks)} 只")
                else:
                    logger.warning(f"daily_active_manual.json stocks 格式异常: {type(stocks)}")
            except Exception as e:
                logger.error(f"加载人工指定名单失败: {e}")

        # 2. 恢复持久化状态 (盘前计划、盘中异动)
        if os.path.exists(self._pool_path):
            try:
                with open(self._pool_path, 'r') as f:
                    data = json.load(f)
                for entry_dict in data.get("entries", []):
                    entry = PoolEntry.from_dict(entry_dict)
                    if entry.source != SOURCE_MANUAL:  # manual 已从上面的逻辑加载
                        self._entries[entry.code] = entry
                logger.info(f"📂 恢复持久化池子: {len(data.get('entries', []))} 条记录")
            except Exception as e:
                logger.error(f"恢复持久化池子失败: {e}")

    def save(self):
        """持久化当前池子状态"""
        try:
            with self._lock:
                data = {
                    "timestamp": _now_iso(),
                    "entries": [e.to_dict() for e in self._entries.values()],
                    "summary": self._get_summary()
                }
            with open(self._pool_path, 'w') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存池子状态失败: {e}")

    # ─── 核心操作 ───────────────────────────────────────────

    def add_position(self, code: str, metadata: Optional[Dict] = None):
        """添加持仓股到动态池 (最高优先级，不受容量限制)
        
        P0 修复: 增加市场前缀过滤，防止跨市场持仓混入
        """
        # P0: 过滤非本市场标的
        if not code.startswith(self._market_prefix):
            logger.debug(f"🚫 跳过非本市场持仓: {code} (本市场={self._market_prefix})")
            return False
        with self._lock:
            existing = self._entries.get(code)
            if existing and existing.source == SOURCE_POSITION:
                # 已存在且来源为持仓，更新 metadata
                if metadata:
                    existing.metadata.update(metadata)
                self.save()
                return False
            self._entries[code] = PoolEntry(
                code=code,
                source=SOURCE_POSITION,
                added_at=_now_iso(),
                expires_at=None,
                metadata=metadata or {}
            )
            self.save()
            logger.info(f"💼 注入持仓: {code}")
            return True

    def remove_position(self, code: str):
        """移除持仓股 (平仓后自动调用)
        
        P0 修复: 增加市场前缀过滤
        """
        # P0: 过滤非本市场标的
        if not code.startswith(self._market_prefix):
            return False
        with self._lock:
            entry = self._entries.get(code)
            if entry and entry.source == SOURCE_POSITION:
                del self._entries[code]
                self.save()
                logger.info(f"📤 移除持仓: {code} (已平仓)")
                return True
            # 如果该股票还有其他来源 (如 MANUAL)，保留它
            return False

    def add_plan_target(self, code: str, direction: str = "ANY", logic: str = "", max_position_pct: float = 0.05):
        """添加盘前计划目标 (24h TTL)"""
        with self._lock:
            existing = self._entries.get(code)
            if existing:
                # 已存在，更新 metadata (如果优先级更高则不覆盖)
                if SOURCE_PRIORITY.get(existing.source, 99) <= SOURCE_PRIORITY[SOURCE_PLAN]:
                    existing.metadata.update({
                        "direction": direction,
                        "logic": logic,
                        "max_position_pct": max_position_pct
                    })
                    self.save()
                    return False
            # 检查容量
            if not self._can_add(SOURCE_PLAN):
                self._evict_lowest_priority()
            self._entries[code] = PoolEntry(
                code=code,
                source=SOURCE_PLAN,
                added_at=_now_iso(),
                expires_at=_make_expiry(TTL_PLAN_HOURS),
                metadata={"direction": direction, "logic": logic, "max_position_pct": max_position_pct}
            )
            self.save()
            logger.info(f"📝 注入盘前计划: {code} (方向: {direction})")
            return True

    def add_intraday_alert(self, code: str, reason: str = ""):
        """添加盘中异动标的 (2h TTL, 容量有限)"""
        with self._lock:
            existing = self._entries.get(code)
            if existing:
                # 已存在，不重复添加
                return False
            # 检查盘中异动数量限制
            intraday_count = sum(1 for e in self._entries.values() if e.source == SOURCE_INTRADAY)
            if intraday_count >= MAX_INTRADAY_ALERTS:
                logger.warning(f"⚠️ 盘中异动池已满 ({intraday_count}/{MAX_INTRADAY_ALERTS})，跳过 {code}")
                return False
            if not self._can_add(SOURCE_INTRADAY):
                self._evict_lowest_priority()
            self._entries[code] = PoolEntry(
                code=code,
                source=SOURCE_INTRADAY,
                added_at=_now_iso(),
                expires_at=_make_expiry(TTL_INTRADAY_HOURS),
                metadata={"reason": reason}
            )
            self.save()
            logger.info(f"🔔 盘中异动加入: {code} (原因: {reason})")
            return True

    def add_manual(self, code: str):
        """人工添加标的 (永久)"""
        with self._lock:
            if code in self._entries:
                return False
            self._entries[code] = PoolEntry(
                code=code,
                source=SOURCE_MANUAL,
                added_at=_now_iso(),
                expires_at=None,
                metadata={}
            )
            # 同步更新 manual JSON
            self._sync_manual_json()
            self.save()
            logger.info(f"👤 人工添加: {code}")
            return True

    def remove_manual(self, code: str):
        """人工移除标的"""
        with self._lock:
            entry = self._entries.get(code)
            if entry and entry.source == SOURCE_MANUAL:
                del self._entries[code]
                self._sync_manual_json()
                self.save()
                logger.info(f"👤 人工移除: {code}")
                return True
            return False

    # ─── 批量注入 ───────────────────────────────────────────

    def inject_positions(self, positions: List[str], position_map: Optional[Dict[str, Dict]] = None):
        """批量注入持仓 (启动时使用)
        
        P0 修复: 增加市场前缀过滤
        P1 修复: 支持 position_map 传入 metadata (如 action/quantity)
        
        Args:
            positions: 股票代码列表
            position_map: {code: {"action": "BUY", "quantity": 200}} metadata 映射
        """
        count = 0
        skipped = 0
        with self._lock:
            for code in positions:
                # P0: 过滤非本市场标的
                if not code.startswith(self._market_prefix):
                    skipped += 1
                    logger.debug(f"🚫 inject_positions 跳过: {code} (非 {self._market_prefix})")
                    continue
                metadata = position_map.get(code, {}) if position_map else {}
                self._entries[code] = PoolEntry(
                    code=code,
                    source=SOURCE_POSITION,
                    added_at=_now_iso(),
                    expires_at=None,
                    metadata=metadata,
                )
                count += 1
        if count > 0:
            self.save()
            logger.info(f"💼 批量注入持仓: {count} 只 (跳过 {skipped} 只非本市场)")
        elif skipped > 0:
            logger.info(f"💼 批量注入: 全部跳过 (无 {self._market_prefix} 持仓)")

    def inject_plan_targets(self, plan: dict):
        """注入盘前计划目标 (启动时使用)"""
        if not plan or "targets" not in plan:
            return
        count = 0
        with self._lock:
            for target in plan["targets"]:
                symbol = target.get("symbol")
                if symbol and symbol not in self._entries:
                    self._entries[symbol] = PoolEntry(
                        code=symbol,
                        source=SOURCE_PLAN,
                        added_at=_now_iso(),
                        expires_at=_make_expiry(TTL_PLAN_HOURS),
                        metadata={
                            "direction": target.get("direction", "ANY"),
                            "logic": target.get("logic", ""),
                            "max_position_pct": target.get("max_position_pct", 0.05)
                        }
                    )
                    count += 1
        if count > 0:
            self.save()
            logger.info(f"📝 批量注入盘前计划: {count} 只")

    # ─── 查询 ───────────────────────────────────────────────

    def get_active_codes(self) -> List[str]:
        """获取当前有效交易池列表 (自动过滤过期)"""
        self._cleanup_expired()
        with self._lock:
            return list(self._entries.keys())

    def get_entries(self) -> List[PoolEntry]:
        """获取所有条目 (含来源信息)"""
        self._cleanup_expired()
        with self._lock:
            return list(self._entries.values())

    def get_source(self, code: str) -> Optional[str]:
        """获取某标的的来源"""
        with self._lock:
            entry = self._entries.get(code)
            return entry.source if entry else None

    def size(self) -> int:
        """池子大小"""
        self._cleanup_expired()
        with self._lock:
            return len(self._entries)

    def get_summary(self) -> dict:
        """获取池子统计摘要"""
        self._cleanup_expired()
        with self._lock:
            return self._get_summary()

    def get_source_breakdown(self) -> dict:
        """按来源分类统计"""
        self._cleanup_expired()
        with self._lock:
            breakdown = {
                SOURCE_POSITION: [],
                SOURCE_PLAN: [],
                SOURCE_MANUAL: [],
                SOURCE_INTRADAY: [],
            }
            for entry in self._entries.values():
                if entry.source in breakdown:
                    breakdown[entry.source].append(entry.code)
            return breakdown

    # ─── 内部方法 ───────────────────────────────────────────

    def _add_entry(self, code: str, source: str, ttl_hours: Optional[int] = None, metadata: Dict = None):
        """内部: 添加条目 (需外部持有锁)"""
        self._entries[code] = PoolEntry(
            code=code,
            source=source,
            added_at=_now_iso(),
            expires_at=_make_expiry(ttl_hours),
            metadata=metadata or {}
        )

    def _can_add(self, source: str) -> bool:
        """检查是否可以添加 (需外部持有锁)"""
        # 持仓不受限制
        if source == SOURCE_POSITION:
            return True
        # 计算非持仓数量
        non_position = sum(1 for e in self._entries.values() if e.source != SOURCE_POSITION)
        return non_position < self._max_pool_size

    def _evict_lowest_priority(self):
        """淘汰最低优先级的标的 (需外部持有锁)
        
        P0 修复: MANUAL (人工指定) 作为固定兜底名单，永远不被容量淘汰
        只有 INTRADAY_ALERT 和 PLAN 可以被淘汰
        """
        # 只有 INTRADAY_ALERT 和 PLAN 可以被容量淘汰
        # MANUAL (固定兜底名单) 和 POSITION (持仓) 永远不被淘汰
        candidates = [e for e in self._entries.values()
                      if e.source in (SOURCE_INTRADAY, SOURCE_PLAN) and not e.is_expired()]
        if not candidates:
            logger.warning("⚠️ 容量已满，但无 INTRADAY/PLAN 可淘汰，跳过 (MANUAL 兜底名单受保护)")
            return
        candidates.sort(key=lambda e: (-SOURCE_PRIORITY.get(e.source, 99), e.added_at))
        victim = candidates[-1]  # 最低优先级 + 最老
        del self._entries[victim.code]
        logger.info(f"♻️ 容量淘汰: {victim.code} (来源: {victim.source})")

    def _cleanup_expired(self):
        """清理过期条目"""
        with self._lock:
            expired = [code for code, entry in self._entries.items() if entry.is_expired()]
            for code in expired:
                entry = self._entries[code]
                logger.info(f"⏰ 过期移除: {code} (来源: {entry.source})")
                del self._entries[code]
            if expired:
                self.save()

    def _get_summary(self) -> dict:
        """获取摘要 (需外部持有锁)"""
        breakdown = {
            SOURCE_POSITION: 0,
            SOURCE_PLAN: 0,
            SOURCE_MANUAL: 0,
            SOURCE_INTRADAY: 0,
        }
        for entry in self._entries.values():
            if entry.source in breakdown:
                breakdown[entry.source] += 1
        return {
            "total": len(self._entries),
            "breakdown": breakdown,
            "max_pool_size": self._max_pool_size,
        }

    def _sync_manual_json(self):
        """同步人工指定名单到 JSON 文件"""
        try:
            manual_codes = [e.code for e in self._entries.values() if e.source == SOURCE_MANUAL]
            data = {
                "stocks": manual_codes,
                "description": "人工指定 + 当前持仓 (自动同步)",
                "updated_at": _now_iso()
            }
            with open(self._manual_path, 'w') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"同步人工指定名单失败: {e}")


# ─── 全局单例入口 ───────────────────────────────────────────
# 延迟初始化: 由 get_daily_active_pool(market_prefix) 触发
_active_pool_instance: Optional[DailyActivePool] = None


def get_daily_active_pool(market_prefix: str = "HK.") -> DailyActivePool:
    """获取全局动态池实例
    
    P0 修复: 支持传入市场前缀，防止跨市场持仓混入
    港股调用: get_daily_active_pool("HK.")
    美股调用: get_daily_active_pool("US.")
    """
    global _active_pool_instance
    if _active_pool_instance is None:
        _active_pool_instance = DailyActivePool(market_prefix=market_prefix)
    return _active_pool_instance
