================================================================================
FTSeek-US Project Context for Code Analysis
This is an algorithmic trading system for US stock market.
Tech stack: Python 3.11, asyncio, SQLite, Futu OpenAPI
================================================================================

# FTSeek-US Project Structure

📄 AGENT.md
📄 DESIGN.md
📁 app/ (517 Python files)
   📁 agents/ (10 files, 3105 lines)
   📁 analysis/ (10 files, 4323 lines)
   📁 analytics/ (2 files, 437 lines)
   📁 backtest/ (3 files, 477 lines)
   📁 catalyst/ (11 files, 2414 lines)
   📁 config_mgmt/ (4 files, 806 lines)
   📁 coordinator/ (8 files, 2058 lines)
   📁 core/ (32 files, 17907 lines)
   📁 data/ (30 files, 14952 lines)
   📁 decision/ (9 files, 1690 lines)
   📁 diagnostics/ (4 files, 1040 lines)
   📁 event_stream/ (12 files, 1225 lines)
   📁 evolution/ (2 files, 354 lines)
   📁 execution/ (19 files, 6432 lines)
   📁 factors/ (14 files, 2883 lines)
   📁 harness/ (2 files, 262 lines)
   📁 lifecycle/ (2 files, 325 lines)
   📁 llm/ (22 files, 5987 lines)
   📁 metrics/ (2 files, 202 lines)
   📁 monitoring/ (2 files, 347 lines)
   📁 news_v2/ (20 files, 3409 lines)
   📁 notifier/ (3 files, 1306 lines)
   📁 observability/ (10 files, 1954 lines)
   📁 pipeline/ (13 files, 13200 lines)
   📁 plan/ (16 files, 6674 lines)
   📁 playbook/ (66 files, 35267 lines)
   📁 positions/ (4 files, 1076 lines)
   📁 push/ (7 files, 1740 lines)
   📁 recommendations/ (6 files, 2546 lines)
   📁 reports/ (5 files, 730 lines)
   📁 review/ (27 files, 10999 lines)
   📁 risk/ (52 files, 17600 lines)
   📁 shared/ (2 files, 720 lines)
   📁 signals/ (2 files, 395 lines)
   📁 storage/ (11 files, 5538 lines)
   📁 strategy/ (17 files, 4809 lines)
   📁 us_pipeline/ (5 files, 2385 lines)
   📁 utils/ (17 files, 3526 lines)
   📁 web_dashboard/ (32 files, 16694 lines)
📁 code_analysis_results/ (0 Python files)
📁 config/ (3 Python files)
📁 data/ (0 Python files)
📁 docs/ (0 Python files)
📄 ftseek_us.db
📄 handoff.md
📁 logs/ (0 Python files)
📄 main.py
📁 obsidian_vault/ (0 Python files)
📁 ops/ (40 Python files)
📄 pytest.ini
📄 requirements.lock
📁 results/ (0 Python files)
📁 scripts/ (611 Python files)
📄 start.sh
📁 tests/ (542 Python files)

# Module Descriptions

## app/core/
系统核心: 配置管理(config), 引擎入口(engine), 位置同步(position_sync), 调度器(scheduler_core), 事件系统(events), DI容器(di_container)

## app/pipeline/
数据管线: 信号管线(signal_pipeline), 订单执行(order_executor), LLM管线(llm_pipeline), 持仓监控(position_monitor)

## app/risk/
风控系统: 持仓退出监控(position_exit_monitor), 风控参数, 仓位限制

## app/playbook/
交易剧本: 剧本服务(playbook_service), 退出引擎(exit_engine), 日内重入(intraday_reentry), 影子注册(shadow_registry)

## app/execution/
执行层: 券商适配器(broker_adapter), 订单路由

## app/coordinator/
协调器: 资金分配(capital_allocator), 退出调度(exit_dispatcher), 信号仲裁(signal_arbitrator), 策略协调

## app/strategy/
策略层: 多策略框架, 参数管理

## app/agents/
AI Agent: 辩论引擎(debate), 投票系统(voting), Claude集成

## app/llm/
LLM集成: Agent桥接(agent_bridge), 信号分析

## app/data/
数据层: Futu行情(futu_lv3_provider), 市场数据路由(market_data_router), 盘前数据(premarket_enrichment)

## app/review/
复盘系统: 日度复盘(daily_review), 复盘编排(review_orchestrator)

## app/web_dashboard/
Web面板: 路由, 数据服务, 实时账本

## app/storage/
存储层: 交易存储(trade_store), 数据库操作

## app/catalyst/
催化剂检测: 事件检测(detector), 分类器(event_classifier), 通知(notifier)

## app/analysis/
分析模块: 归因(trade_attribution), 信号质量(signal_quality_tracker), 系统性问题(systemic_issue_analyzer)


================================================================================
CORE CODE SAMPLES
================================================================================

# app/core/engine.py (201 lines, FULL FILE)
```python
"""
三大引擎基类

定义 Risk/Execution/Observability 引擎的统一接口
"""
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from datetime import datetime
from .models import SignalScore, Order, Position, TradeEvent, Alert, FailureRecord
from .events import EventStore


class BaseEngine(ABC):
    """
    引擎基类
    
    所有引擎继承此基类，实现统一接口
    """
    
    def __init__(self, config: Dict[str, Any], event_store: EventStore):
        self.config = config
        self.event_store = event_store
        self.enabled = True
        self.last_heartbeat = datetime.now()
    
    @abstractmethod
    def initialize(self):
        """初始化引擎"""
        pass
    
    @abstractmethod
    def shutdown(self):
        """关闭引擎"""
        pass
    
    def heartbeat(self):
        """心跳检查"""
        self.last_heartbeat = datetime.now()
    
    def is_healthy(self) -> bool:
        """检查引擎健康状态"""
        return self.enabled and (datetime.now() - self.last_heartbeat).total_seconds() < 60
    
    def emit_event(self, event: TradeEvent):
        """发出事件"""
        event.source = self.__class__.__name__
        self.event_store.append(event)
    
    def emit_alert(self, alert: Alert):
        """发出告警"""
        alert.source = self.__class__.__name__
        self.event_store.record_alert(alert)
    
    def record_failure(self, failure: FailureRecord):
        """记录失败"""
        self.event_store.record_failure(failure)


class RiskEngine(BaseEngine):
    """
    风险引擎
    
    职责:
    - 熔断检查
    - 仓位限制
    - 账户风险
    - 回撤限制
    - 关联资产敞口
    - 动态止损/止盈判定
    """
    
    def initialize(self):
        """初始化风险引擎"""
        self.circuit_breakers = {}
        self.position_limits = {}
        self.drawdown_levels = {}
    
    def shutdown(self):
        """关闭风险引擎"""
        self.enabled = False
    
    @abstractmethod
    def pre_trade_check(self, signal: SignalScore, position: Optional[Position] = None) -> bool:
        """预交易风控检查"""
        pass
    
    @abstractmethod
    def intra_trade_check(self, positions: List[Position]) -> List[Alert]:
        """持仓中风控检查"""
        pass
    
    @abstractmethod
    def post_trade_check(self, order: Order, position: Position) -> bool:
        """交易后风控检查"""
        pass
    
    @abstractmethod
    def check_circuit_breakers(self) -> Dict[str, bool]:
        """检查熔断器状态"""
        pass
    
    @abstractmethod
    def should_exit(self, position: Position, current_price: float) -> Optional[str]:
        """判断是否应该退出持仓"""
        pass


class ExecutionEngine(BaseEngine):
    """
    执行引擎
    
    职责:
    - 路由到哪个交易所/券商
    - 下单方式选择：限价/市价/IOC/FOK/TWAP/VWAP
    - 重试、撤单、补单
    - 滑点控制和成交追踪
    """
    
    def initialize(self):
        """初始化执行引擎"""
        self.order_book: Dict[str, Order] = {}
        self.pending_orders: List[Order] = []
        self.broker_adapter = None
    
    def shutdown(self):
        """关闭执行引擎"""
        self.enabled = False
        self.pending_orders.clear()
    
    @abstractmethod
    def submit_order(self, order: Order) -> Order:
        """提交订单"""
        pass
    
    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """取消订单"""
        pass
    
    @abstractmethod
    def query_order(self, order_id: str) -> Order:
        """查询订单状态"""
        pass
    
    @abstractmethod
    def select_order_type(self, signal: SignalScore) -> str:
        """选择订单类型"""
        pass
    
    @abstractmethod
    def optimize_execution(self, order: Order) -> Order:
        """优化执行策略"""
        pass


class ObservabilityEngine(BaseEngine):
    """
    观测引擎
    
    职责:
    - 实时监控
    - 告警
    - 失败追踪
    - 审计日志
    - 可回放的交易轨迹
    """
    
    def initialize(self):
        """初始化观测引擎"""
        self.metrics = {}
        self.alert_rules = {}
        self.failure_patterns = {}
    
    def shutdown(self):
        """关闭观测引擎"""
        self.enabled = False
    
    @abstractmethod
    def collect_metrics(self) -> Dict[str, Any]:
        """收集监控指标"""
        pass
    
    @abstractmethod
    def check_alerts(self, metrics: Dict[str, Any]) -> List[Alert]:
        """检查告警条件"""
        pass
    
    @abstractmethod
    def track_failure(self, failure: FailureRecord):
        """追踪失败"""
        pass
    
    @abstractmethod
    def get_health_dashboard(self) -> Dict[str, Any]:
        """获取健康度看板数据"""
        pass
    
    @abstractmethod
    def replay_trades(self, start_time: datetime, end_time: datetime) -> List[TradeEvent]:
        """回放交易"""
        pass
```

# app/core/config.py (19 lines, FULL FILE)
```python
"""
统一配置入口 (SSOT)

此模块作为 FTSeek-US 运行时配置的唯一推荐入口：
- from app.core.config import get_config

底层委托给 app.core.config_container.ConfigContainer，
避免 ConfigLoader / UnifiedConfig / ConfigContainer 三重解析的混乱。

历史兼容:
- config.config_loader.get_config() -> ConfigLoader (legacy, config_container底层)
以上保留但不再推荐；新代码应统一使用 app.core.config.get_config().
(config/unified_config.py 已于 2026-08-12 删除, 零消费者)
"""
from __future__ import annotations

from app.core.config_container import ConfigContainer, get_config

__all__ = ["get_config", "ConfigContainer"]
```

# app/coordinator/capital_allocator.py (648 lines, FULL FILE)
```python
"""
CapitalAllocator — 实时资金账本 (Phase A 2026-05-14)

强制每个 strategy 的 capital_pct 上限. 取代当前"软约束"。

观察模式 (observe_only=True): 记录所有 reserve/release, 但永远 return True
强制模式 (observe_only=False): 超限直接拒绝
"""
from __future__ import annotations
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class CapitalSnapshot:
    """供 dashboard / daily_review 读"""
    timestamp: float
    by_strategy: Dict[str, float]
    total_reserved: float
    max_total: float
    headroom: float  # max_total - total_reserved
    # Task #35 (2026-06-16): auto 资本隔离 — 关键诊断字段
    legacy_protected_pct: float = 0.0   # 历史持仓占总权益比例
    manual_pct: float = 0.0             # 手动持仓占总权益比例
    auto_pool_pct: float = 0.0          # auto 可用资金池占比 (= max_total - legacy - manual, 截 [0,1])
    auto_exposure_pct: float = 0.0      # auto 当前敞口占总权益比例
    auto_headroom_pct: float = 0.0      # auto 余量占 auto_pool 比例 (≥0; auto_pool=0 时为 0)


@dataclass
class AutoCapacityReport:
    """Task #35 (2026-06-16): auto 资本容量报告 — 用于 dashboard / daily_review / 单测验证.

    所有金额字段单位均为 USD; pct 字段为比例 (0~1).
    auto_pool = total_equity - legacy_protected_value - manual_value
    auto_headroom = auto_pool - current_auto_exposure  (不会为负)
    auto_headroom_pct = auto_headroom / auto_pool      (auto_pool=0 时为 0)
    """
    timestamp: float
    total_equity: float
    legacy_protected_value: float
    manual_value: float
    auto_pool: float
    current_auto_exposure: float
    auto_headroom: float
    auto_headroom_pct: float
    legacy_count: int = 0
    manual_count: int = 0
    auto_count: int = 0
    manual_orphan_count: int = 0


@dataclass
class _ReserveEvent:
    timestamp: float
    strategy: str
    delta: float           # 正数=reserve, 负数=release
    decision: str          # ALLOWED / BLOCKED_STRATEGY_CAP / BLOCKED_TOTAL_CAP
    snapshot_after: float  # strategy 当前总占用


class CapitalAllocator:
    """资金账本 + 上限强制 (线程安全)"""

    def __init__(
        self,
        caps: Dict[str, float],
        max_total_pct: float = 0.90,
        observe_only: bool = True,
        bucket_map: Optional[Dict[str, str]] = None,
        db_path: Optional[str] = None,
    ):
        """
        Args:
            caps: {bucket_name: max_pct}, 例 {"main": 0.80, "llm_plan": 0.0}
            max_total_pct: 全局上限 (所有 bucket 加总)
            observe_only: True=观察模式 (Phase A 默认), False=强制
            bucket_map: {strategy_name: bucket_name} — 多策略共享 bucket 时用
                        例 {"fast_lane": "main"} 让 fast_lane 占用计入 main 桶
                        缺省 → strategy_name = bucket_name
        """
        # P1-7: stash db_path for _init_persist_db
        self._persist_init_db_path = db_path
        self._caps = dict(caps)
        self._max_total = max_total_pct
        self._observe_only = observe_only
        self._bucket_map = dict(bucket_map or {})
        # _allocations key 是 bucket, 不是 strategy
        self._allocations: Dict[str, float] = {s: 0.0 for s in caps}
        # _by_strategy 仅供查看/报表 — 同 bucket 内多策略各自累加
        self._by_strategy: Dict[str, float] = {}
        # Phase 1 (2026-06-14): 手动仓位市值占比 — 不占 bucket cap, 但占 total cap
        self._manual_reserved: float = 0.0
        # Task #35 (2026-06-16): legacy_protected 仓位市值占比 — 与 manual 并列, 均不占 auto_pool
        # 隔离公式: auto_pool = max_total - legacy_protected - manual  (≥ 0)
        self._legacy_protected_reserved: float = 0.0
        # Task #35: 最近一次 auto 容量报告 (供 dashboard / daily_review 读)
        self._last_auto_report: Optional[AutoCapacityReport] = None
        self._lock = threading.Lock()
        self._events: List[_ReserveEvent] = []
        self._max_event_history = 500
        # P1-7 (2026-06-12): persistent ledger
        self._db_path: Optional[str] = None
        self._db_lock = threading.Lock()
        if self._persist_init_db_path is not None:
            try:
                self._init_persist_db(self._persist_init_db_path)
                self._load_persist_state()
                logger.info(
                    f"[CapitalAllocator] persist enabled db={self._db_path} "
                    f"restored allocations={self._allocations} by_strategy={self._by_strategy}"
                )
            except Exception as _e:
                logger.error(f"[CapitalAllocator] persist init failed (fallback to memory): {_e}")
                self._db_path = None

    def _bucket_for(self, strategy: str) -> str:
        return self._bucket_map.get(strategy, strategy)

    def set_observe_only(self, observe: bool):
        """运行时切换 (灰度)"""
        with self._lock:
            self._observe_only = observe
        logger.info(f"[CapitalAllocator] observe_only={observe}")

    def try_reserve(self, strategy: str, position_pct: float) -> bool:
        """尝试预留资金额度。

        ⚠️ 当前限制:
        - observe_only=True 模式下永远返回 True (仅记录不阻断)
        已接入 (2026-10-12): 主订单路径在下单点强制预留 — order_executor
        `_reserve_capital_for_order` 在 _execute_long/_execute_stock_short 下单前
        以真实 quantity×price/equity 计算 position_pct 调用本方法;
        释放经平仓路径 on_position_closed → release(position.position_pct)。
        Phase B 剩余项: observe_only 切为 False。

        资金记账走 bucket (B.3 2026-05-14): 多策略共享 bucket 时, 占用计入同一桶.
        例 strategy=fast_lane, bucket_map={"fast_lane":"main"} → 实际记到 main 桶, 受 main 的 cap 限制.

        Returns:
            True: 预留成功 (observe 模式总是 True, 但 BLOCKED 事件仍记录)
            False: 强制模式下被拒
        """
        with self._lock:
            bucket = self._bucket_for(strategy)
            current = self._allocations.get(bucket, 0.0)
            cap = self._caps.get(bucket, 0.0)
            # Task #35: total 同时包含 legacy_protected (与 manual 一样占用 max_total 总配额)
            total = (sum(self._allocations.values())
                     + self._manual_reserved
                     + self._legacy_protected_reserved)

            decision = "ALLOWED"
            if bucket not in self._caps:
                decision = "BLOCKED_UNKNOWN_BUCKET"
            elif cap <= 0:
                decision = "BLOCKED_STRATEGY_CAP"
            elif current + position_pct > cap:
                decision = "BLOCKED_STRATEGY_CAP"
            elif total + position_pct > self._max_total:
                decision = "BLOCKED_TOTAL_CAP"

            allowed = (decision == "ALLOWED")
            if allowed:
                # H7 (2026-06-03): 只有 allowed 才记账, 避免 BLOCKED 事件累加导致账本失真
                self._allocations[bucket] = current + position_pct
                self._by_strategy[strategy] = self._by_strategy.get(strategy, 0.0) + position_pct
                snapshot_after = self._allocations[bucket]
                self._record_event(strategy, position_pct, decision, snapshot_after)
                self._persist_state(bucket, strategy)  # P1-7
                return True

            # BLOCKED: 只记录事件, 不累加账本
            self._record_event(strategy, position_pct, decision, current)

            if self._observe_only:
                logger.warning(
                    f"[CapitalAllocator·OBSERVE] {strategy}->{bucket} reserve {position_pct:.3f} "
                    f"would have been {decision} (current={current:.3f}, cap={cap:.3f}, "
                    f"total={total:.3f}, max_total={self._max_total:.3f}) — 放行但不记账"
                )
                return True  # observe 模式放行, 但不累加账本

            # 强制模式 + 拒绝
            logger.warning(
                f"[CapitalAllocator·ENFORCE] {strategy}->{bucket} reserve {position_pct:.3f} REJECTED: "
                f"{decision} (current={current:.3f}, cap={cap:.3f})"
            )
            return False

    def release(self, strategy: str, position_pct: float):
        """释放 strategy 的预留资金 (平仓时调用) — 走 bucket"""
        with self._lock:
            bucket = self._bucket_for(strategy)
            current = self._allocations.get(bucket, 0.0)
            new_val = max(0.0, current - position_pct)
            self._allocations[bucket] = new_val
            self._by_strategy[strategy] = max(0.0, self._by_strategy.get(strategy, 0.0) - position_pct)
            self._record_event(strategy, -position_pct, "RELEASED", new_val)
            self._persist_state(bucket, strategy)  # P1-7

    def force_set(self, strategy: str, value: float):
        """从持仓快照同步 (启动恢复 / 定期 reconcile 用) — 走 bucket"""
        with self._lock:
            bucket = self._bucket_for(strategy)
            self._allocations[bucket] = max(0.0, value)
            self._record_event(strategy, 0.0, "FORCE_SET", value)
            self._persist_state(bucket, strategy)  # P1-7

    def snapshot(self) -> CapitalSnapshot:
        """当前账本快照. by_strategy 字段是 bucket 视图 (向后兼容现有调用方).
        Phase 1 (2026-06-14): total_reserved 包含 manual_reserved, headroom 扣除手动占用.
        Task #35 (2026-06-16): 同时暴露 legacy_protected / auto_pool / auto_headroom 字段."""
        with self._lock:
            by_s = dict(self._allocations)
            auto_total = sum(by_s.values())
            legacy = self._legacy_protected_reserved
            manual = self._manual_reserved
            total = auto_total + manual + legacy
            # Task #35: auto_pool = max_total - legacy - manual  (截 ≥0)
            auto_pool = max(0.0, self._max_total - legacy - manual)
            auto_headroom = max(0.0, auto_pool - auto_total)
            auto_headroom_pct = (auto_headroom / auto_pool) if auto_pool > 0 else 0.0
            return CapitalSnapshot(
                timestamp=time.time(),
                by_strategy=by_s,
                total_reserved=total,
                max_total=self._max_total,
                headroom=max(0.0, self._max_total - total),
                legacy_protected_pct=legacy,
                manual_pct=manual,
                auto_pool_pct=auto_pool,
                auto_exposure_pct=auto_total,
                auto_headroom_pct=auto_headroom_pct,
            )

    def by_strategy_snapshot(self) -> Dict[str, float]:
        """B.3 (2026-05-14): 按 strategy 视角看占用 (与 bucket 视图区分)"""
        with self._lock:
            return dict(self._by_strategy)

    def is_observe_only(self) -> bool:
        """B.6 (2026-05-14): 公开访问 — 替代私有 _observe_only"""
        return self._observe_only

    def compute_scale_factor(self, strategy: str, position_pct: float) -> float:
        """Task #1 (2026-05-31): 计算按比例缩放系数

        当调用方希望 "总敞口超限 → 按比例缩放" 而非硬拒时使用.
        返回 [0.0, 1.0] 区间系数: 1.0 = 无需缩放; 0.0 = 完全拒绝.
        同时考虑 bucket cap 和 总敞口 cap, 取较小者.
        调用方可: 将 position_pct 乘以 该系数 后再 try_reserve.
        """
        with self._lock:
            if position_pct <= 0:
                return 1.0
            bucket = self._bucket_for(strategy)
            current = self._allocations.get(bucket, 0.0)
            cap = self._caps.get(bucket, 0.0)
            # Task #35: total 同时包含 legacy_protected
            total = (sum(self._allocations.values())
                     + self._manual_reserved
                     + self._legacy_protected_reserved)

            # bucket 剩余
            bucket_headroom = 0.0 if bucket not in self._caps or cap <= 0 else max(0.0, cap - current)
            # 总敞口剩余 (扣除手动仓位占用)
            total_headroom = max(0.0, self._max_total - total)
            allowed = min(bucket_headroom, total_headroom, position_pct)
            scale = 0.0 if position_pct == 0 else max(0.0, min(1.0, allowed / position_pct))
            return scale

    def reset_and_reconcile(self, positions_by_strategy: Dict[str, float]):
        """
        P0-4 (2026-05-14): observe → enforce 切换前必调.

        observe 模式下账本可能透支 (BLOCKED 事件也记账了).
        切 enforce 前必须清零 + 用真实持仓重建, 否则会拒所有信号.
        """
        with self._lock:
            self._allocations = {b: 0.0 for b in self._caps}
            self._by_strategy = {}
            self._record_event("__reset__", 0.0, "ENFORCE_RESET", 0.0)
            self._persist_state_all()  # P1-7
        # 锁外调 (避免双重 lock)
        self.reconcile_with_positions(positions_by_strategy)
        logger.warning(
            f"[CapitalAllocator·RESET] 账本已清零 + reconcile, 现状: {self.snapshot().by_strategy}"
        )

    def set_manual_reserved(self, pct: float):
        """Phase 1 (2026-06-14): 设置手动仓位市值占比.

        手动仓位不占 bucket cap (不影响 auto 的 80% 上限),
        但占 total cap (auto 可用 = total_cap - manual_reserved - auto_reserved).
        由 StrategyCoordinator.reconcile() 定期更新.
        """
        with self._lock:
            old = self._manual_reserved
            self._manual_reserved = max(0.0, pct)
            if abs(old - pct) > 0.005:
                auto_total = sum(self._allocations.values())
                logger.info(
                    f"[CapitalAllocator] manual_reserved: {old:.3f} → {pct:.3f} "
                    f"(auto_pool={max(0.0, self._max_total - self._legacy_protected_reserved - pct):.3f} "
                    f"auto_exposure={auto_total:.3f})"
                )

    def set_legacy_protected_reserved(self, pct: float):
        """Task #35 (2026-06-16): 设置启动时已存在的历史 (legacy_protected) 仓位市值占比.

        与 manual 对称: 同样不占 bucket cap, 但占 total cap.
        auto_pool = max_total - legacy_protected - manual  (≥0)
        由 StrategyCoordinator.reconcile() 定期更新.
        """
        with self._lock:
            old = self._legacy_protected_reserved
            self._legacy_protected_reserved = max(0.0, pct)
            if abs(old - pct) > 0.005:
                auto_total = sum(self._allocations.values())
                logger.info(
                    f"[CapitalAllocator] legacy_protected_reserved: {old:.3f} → {pct:.3f} "
                    f"(auto_pool={max(0.0, self._max_total - pct - self._manual_reserved):.3f} "
                    f"auto_exposure={auto_total:.3f})"
                )

    def set_owner_pools(self, legacy_protected_pct: float,
                         manual_pct: float) -> None:
        """Task #35 (2026-06-16): 一次性设置 legacy + manual 占比 (原子).

        StrategyCoordinator.reconcile() 推荐入口 — 与历史 set_manual_reserved 兼容,
        但能在同一锁内更新两个池子, 避免 auto_pool 短暂为负.
        """
        with self._lock:
            old_legacy = self._legacy_protected_reserved
            old_manual = self._manual_reserved
            self._legacy_protected_reserved = max(0.0, float(legacy_protected_pct or 0.0))
            self._manual_reserved = max(0.0, float(manual_pct or 0.0))
            auto_total = sum(self._allocations.values())
            auto_pool = max(0.0, self._max_total
                            - self._legacy_protected_reserved
                            - self._manual_reserved)
            auto_headroom = max(0.0, auto_pool - auto_total)
            if (abs(old_legacy - self._legacy_protected_reserved) > 0.005
                    or abs(old_manual - self._manual_reserved) > 0.005):
                logger.info(
                    f"[CapitalAllocator·set_owner_pools] "
                    f"legacy={old_legacy:.3f}→{self._legacy_protected_reserved:.3f} "
                    f"manual={old_manual:.3f}→{self._manual_reserved:.3f} "
                    f"auto_pool={auto_pool:.3f} auto_exposure={auto_total:.3f} "
                    f"auto_headroom={auto_headroom:.3f}"
                )

    def auto_capacity_report(self,
                              total_equity: float,
                              positions: Optional[List] = None,
                              ) -> AutoCapacityReport:
        """Task #35 (2026-06-16): 生成 auto 资本容量报告 (USD 维度).

        Args:
            total_equity: 账户净资产 (broker)
            positions: 当前持仓快照 (Optional). 若提供, 则按 owner 分类直接累计市值;
                       否则用内部 pct 字段 × equity 估算.
        """
        eq = float(total_equity or 0.0)
        legacy_val = 0.0
        manual_val = 0.0
        auto_val = 0.0
        legacy_cnt = 0
        manual_cnt = 0
        auto_cnt = 0
        orphan_cnt = 0

        if positions:
            try:
                from app.positions.source import (
                    classify_owner,
                    OWNER_AUTO, OWNER_LEGACY_PROTECTED,
                    OWNER_MANUAL, OWNER_MANUAL_ORPHAN,
                )
                for p in positions:
                    try:
                        size = abs(float(getattr(p, "size", 0) or 0))
                        # 优先用当前价/市值, 次选 entry_price * size
                        cur = float(getattr(p, "current_price", 0) or 0)
                        if cur <= 0:
                            cur = float(getattr(p, "avg_entry_price", 0) or 0)
                        mv = size * cur
                        cls = classify_owner(p)
                        if cls == OWNER_AUTO:
                            auto_val += mv
                            auto_cnt += 1
                        elif cls == OWNER_LEGACY_PROTECTED:
                            legacy_val += mv
                            legacy_cnt += 1
                        elif cls == OWNER_MANUAL:
                            manual_val += mv
                            manual_cnt += 1
                        elif cls == OWNER_MANUAL_ORPHAN:
                            # manual_orphan 视同 manual 隔离
                            manual_val += mv
                            manual_cnt += 1
                            orphan_cnt += 1
                    except Exception:
                        continue
            except Exception as _e:
                logger.debug(f"[CapitalAllocator·auto_capacity_report] classify err: {_e}")
        else:
            # 从内部 pct 字段估算 (兜底)
            with self._lock:
                legacy_val = self._legacy_protected_reserved * eq
                manual_val = self._manual_reserved * eq
                auto_val = sum(self._allocations.values()) * eq

        auto_pool = max(0.0, eq - legacy_val - manual_val)
        # Task #35 约束: auto_headroom ≥ 0
        auto_headroom = max(0.0, auto_pool - auto_val)
        auto_headroom_pct = (auto_headroom / auto_pool) if auto_pool > 0 else 0.0

        report = AutoCapacityReport(
            timestamp=time.time(),
            total_equity=eq,
            legacy_protected_value=legacy_val,
            manual_value=manual_val,
            auto_pool=auto_pool,
            current_auto_exposure=auto_val,
            auto_headroom=auto_headroom,
            auto_headroom_pct=auto_headroom_pct,
            legacy_count=legacy_cnt,
            manual_count=manual_cnt,
            auto_count=auto_cnt,
            manual_orphan_count=orphan_cnt,
        )
        with self._lock:
            self._last_auto_report = report
        return report

    def get_last_auto_report(self) -> Optional[AutoCapacityReport]:
        """Task #35: 返回最近一次 auto_capacity_report 结果 (供 daily_review 读)."""
        with self._lock:
            return self._last_auto_report

    def reconcile_with_positions(self, positions_by_strategy: Dict[str, float]):
        """
        与真实持仓同步: positions_by_strategy = {strategy: total_position_pct}
        如发现账本与持仓加总不一致 → 用持仓修正 + 告警
        会先把按 strategy 的持仓汇总到 bucket, 然后修正 bucket 账本.
        """
        with self._lock:
            # 先把 strategy 维度的持仓汇总到 bucket 维度
            actual_by_bucket: Dict[str, float] = {}
            for strat, pct in positions_by_strategy.items():
                bucket = self._bucket_for(strat)
                actual_by_bucket[bucket] = actual_by_bucket.get(bucket, 0.0) + pct
            # 同步: 检查所有可能的 bucket
            all_buckets = set(self._allocations) | set(actual_by_bucket)
            for b in all_buckets:
                book = self._allocations.get(b, 0.0)
                actual = actual_by_bucket.get(b, 0.0)
                if abs(book - actual) > 0.005:  # 0.5% 容差
                    logger.warning(
                        f"[CapitalAllocator·RECONCILE] bucket={b} 账本={book:.3f} 实际={actual:.3f} → 修正"
                    )
                    self._allocations[b] = actual
                    self._record_event(b, 0.0, "RECONCILED", actual)
            # 同步 by_strategy 视图
            for strat, pct in positions_by_strategy.items():
                self._by_strategy[strat] = pct
            self._persist_state_all()  # P1-7

    def get_recent_events(self, n: int = 50) -> List[Dict]:
        """最近 N 条事件 (供 daily_review 读)"""
        with self._lock:
            return [
                {
                    "ts": e.timestamp,
                    "strategy": e.strategy,
                    "delta": e.delta,
                    "decision": e.decision,
                    "snapshot": e.snapshot_after,
                }
                for e in self._events[-n:]
            ]

    def _record_event(self, strategy: str, delta: float, decision: str, snapshot_after: float):
        e = _ReserveEvent(
            timestamp=time.time(),
            strategy=strategy,
            delta=delta,
            decision=decision,
            snapshot_after=snapshot_after,
        )
        self._events.append(e)
        if len(self._events) > self._max_event_history:
            self._events = self._events[-self._max_event_history:]
        # P1-7: persist ledger event (best-effort)
        if self._db_path:
            try:
                self._persist_event(e)
            except Exception as _pe:
                logger.debug(f"[CapitalAllocator] persist event failed: {_pe}")



    # ====================== P1-7: persistent ledger ======================
    def _init_persist_db(self, db_path: str) -> None:
        d = os.path.dirname(db_path)
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)
        with self._db_lock:
            conn = sqlite3.connect(db_path, timeout=10.0)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS capital_state (
                        kind TEXT NOT NULL,
                        key  TEXT NOT NULL,
                        value REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (kind, key)
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS capital_ledger (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts REAL NOT NULL,
                        strategy TEXT NOT NULL,
                        delta REAL NOT NULL,
                        decision TEXT NOT NULL,
                        snapshot_after REAL NOT NULL
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_capital_ledger_ts ON capital_ledger(ts)")
                conn.commit()
            finally:
                conn.close()
        self._db_path = db_path

    def _load_persist_state(self) -> None:
        if not self._db_path:
            return
        with self._db_lock:
            conn = sqlite3.connect(self._db_path, timeout=10.0)
            try:
                cur = conn.execute("SELECT kind, key, value FROM capital_state")
                for kind, key, val in cur.fetchall():
                    try:
                        v = float(val)
                    except Exception:
                        continue
                    if kind == "bucket":
                        if key in self._allocations:
                            self._allocations[key] = max(0.0, v)
                    elif kind == "strategy":
                        self._by_strategy[key] = max(0.0, v)
            finally:
                conn.close()

    def _persist_state(self, bucket: str, strategy: Optional[str] = None) -> None:
        if not self._db_path:
            return
        ts = time.time()
        rows = [("bucket", bucket, float(self._allocations.get(bucket, 0.0)), ts)]
        if strategy is not None:
            rows.append(("strategy", strategy, float(self._by_strategy.get(strategy, 0.0)), ts))
        try:
            with self._db_lock:
                conn = sqlite3.connect(self._db_path, timeout=5.0)
                try:
                    conn.executemany(
                        "INSERT INTO capital_state(kind,key,value,updated_at) VALUES (?,?,?,?) "
                        "ON CONFLICT(kind,key) DO UPDATE SET value=excluded.value, "
                        "updated_at=excluded.updated_at",
                        rows,
                    )
                    conn.commit()
                finally:
                    conn.close()
        except Exception as e:
            logger.debug(f"[CapitalAllocator] _persist_state failed: {e}")

    def _persist_state_all(self) -> None:
        if not self._db_path:
            return
        ts = time.time()
        rows = [("bucket", b, float(v), ts) for b, v in self._allocations.items()]
        rows += [("strategy", s, float(v), ts) for s, v in self._by_strategy.items()]
        try:
            with self._db_lock:
                conn = sqlite3.connect(self._db_path, timeout=5.0)
                try:
                    conn.executemany(
                        "INSERT INTO capital_state(kind,key,value,updated_at) VALUES (?,?,?,?) "
                        "ON CONFLICT(kind,key) DO UPDATE SET value=excluded.value, "
                        "updated_at=excluded.updated_at",
                        rows,
                    )
                    conn.commit()
                finally:
                    conn.close()
        except Exception as e:
            logger.debug(f"[CapitalAllocator] _persist_state_all failed: {e}")

    def _persist_event(self, e) -> None:
        if not self._db_path:
            return
        with self._db_lock:
            conn = sqlite3.connect(self._db_path, timeout=5.0)
            try:
                conn.execute(
                    "INSERT INTO capital_ledger(ts,strategy,delta,decision,snapshot_after) "
                    "VALUES (?,?,?,?,?)",
                    (e.timestamp, e.strategy, e.delta, e.decision, e.snapshot_after),
                )
                conn.execute(
                    "DELETE FROM capital_ledger WHERE id <= "
                    "(SELECT MAX(id) FROM capital_ledger) - 5000"
                )
                conn.commit()
            finally:
                conn.close()

_singleton: Optional[CapitalAllocator] = None


def get_capital_allocator(caps: Optional[Dict[str, float]] = None,
                          max_total_pct: float = 0.90,
                          observe_only: bool = True,
                          db_path: Optional[str] = None) -> CapitalAllocator:
    """单例. 第一次调用必须传 caps. P1-7: db_path enables persistence."""
    global _singleton
    if _singleton is None:
        if caps is None:
            raise ValueError("CapitalAllocator 第一次构造必须传 caps")
        _singleton = CapitalAllocator(
            caps=caps, max_total_pct=max_total_pct,
            observe_only=observe_only, db_path=db_path,
        )
    return _singleton
```

# /opt/ftseek/FTSeek-US/app/pipeline/order_executor.py (total 5771 lines)

## File Header (lines 1-80)
```python
"""
订单执行管线 — FTSeek-US 下单执行与仓位管理

从 scheduler_tasks.py 拆分而来。本模块包含完整的订单构建、风控门禁、
仓位计算、平仓逻辑和通知。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from app.utils.timezone import ET, _et_trade_date, now_et
from app.storage.trade_store import assign_cohort

from app.core.models import (
    SignalScore, Direction, MarketRegime,
    Order, OrderType, OrderState, Position,
    TradeEvent, ExitReason, AlertLevel,
)

from app.data.adaptive_polling import StockTier
from app.storage.rejection_logger import get_rejection_logger
from app.execution.twap_executor import (
    TWAPExecutor, TWAPResult, should_use_twap, cancel_active_twap,
)
from app.execution.symbol_lock_manager import get_symbol_lock_manager
from app.risk.pdt_guard import PDTGuard

logger = logging.getLogger(__name__)

# ── 全局常量 ──
DEFAULT_ORDER_QTY = 50  # 默认下单股数 (watchlist_us.json 可覆盖)

_pdt_guard: Optional[PDTGuard] = None

def _get_pdt_guard() -> PDTGuard:
    """懒加载并复用 PDTGuard 实例, 避免 import 时访问 DB."""
    global _pdt_guard
    if _pdt_guard is None:
        _pdt_guard = PDTGuard()
    return _pdt_guard


def _with_symbol_lock_release(func):
    """装饰器: 保证 _close_position 任何 early return/异常都释放 SymbolLock."""
    from functools import wraps

    @wraps(func)
    def wrapper(scheduler, position, *args, **kwargs):
        try:
            return func(scheduler, position, *args, **kwargs)
        finally:
            try:
                _dir_str = position.direction.value if hasattr(position.direction, "value") else str(position.direction)
                _owner = getattr(position, "owner_strategy", None) or "main"
                get_symbol_lock_manager().release(position.symbol, _dir_str, _owner)
            except Exception as _sre:
                logger.debug(f"[SymbolLock·symdir·close] finally 释放异常: {_sre}")

    return wrapper


# ══════════════════════════════════════════════════════════════════
#  Task #34 (P0, 2026-06-16): 平仓重入与持仓不足级联失败修复
# ──────────────────────────────────────────────────────────────────
#  问题根因:
#    - 21:30:20 下单 success+订单ID, 21:30:29 立即"持仓不足"
#    - 23:07:01 "不支持交叉交易, 有待成交订单" — 旧订单未清算
#    - 重试无 backoff, 每 tick 重触发, 6 次失败 → is_anomalous → 8h 不可平仓
#  修复策略:
#    1. 平仓前强制刷新 broker.get_position(symbol)
#    2. 检测待成交订单 (broker.get_open_orders), 等待结算最多 60s
```

## Lines 781-813 (matched: def _submit)
```python


def _submit_order_optimized(
    scheduler,
    order: Order,
    signal_price: float,
    signal_time: float,
    direction: Direction,
    signal_id: str = "",
    trace_id: str = "",
) -> Tuple[Order, Dict[str, Any]]:
    """统一下单入口 — 金额 > 阈值 走 TWAP, 否则走市价; 返回 (order, exec_metadata)

    Args:
        order: 已准备好的订单 (quantity/price/direction 均已设置)
        signal_price: 信号生成时参考价
        signal_time: 信号生成 epoch (s)
        direction: 方向
        signal_id/trace_id: 归因

    Returns:
        (submitted_order, metadata) — metadata 含 slippage_pct/execution_latency_ms/twap 详情
    """
    cfg = _load_execution_optimization_config()
    notional = float(order.quantity) * float(order.price or signal_price or 0.0)
    use_twap = (
        cfg["enable_twap"]
        and should_use_twap(notional, cfg["twap_threshold_usd"])
        and scheduler.execution_engine is not None
    )

    if not use_twap:
        # 普通路径
```


# /opt/ftseek/FTSeek-US/app/core/position_sync.py (total 2888 lines)

## File Header (lines 1-80)
```python
#!/usr/bin/env python3
"""
US Position Sync Module (FTSeek-US)
- Runtime position reconciliation between Futu SSOT and local DB
- Runs as background thread, 30s interval
- Handles both LONG and SHORT positions
"""

import json
import logging
import threading
import time
import sqlite3
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Optional

from app.core.db_utils import safe_sqlite_connect

logger = logging.getLogger("ftseek_us.position_sync")

# 2026-08-05: 自管持仓跳过日志的进程内去重 (首条 INFO, 后续 debug)
_SELF_MANAGED_SKIP_LOGGED: set = set()

# external_close 查重判据 (2026-08-21 建立, 2026-08-28 收紧): 只有"已了结的记账行"才算重复。
# 旧判据 realized_pnl IS NOT NULL 会把同一仓位的**开仓行**也当作已记账平仓 (未平仓行历史上带
# realized_pnl=0.0 占位) → 真实券商平仓被跳过 INSERT 与通知, 而 open_positions 的 DELETE 照常
# 执行 → 该笔平仓在系统里彻底消失。改按全仓统一的已平仓口径 exit_price > 0 判定。
# 抽为模块常量: 守卫本体深在 ~500 行同步方法内不可单测, 由 tests 直接对该 SQL 钉不变量。
EXTERNAL_CLOSE_DUP_SQL = (
    "SELECT id, entry_time, quantity, close_futu_order_id FROM trade_records WHERE symbol=? AND direction=? "
    "AND exit_price > 0"
)

# ── [RFC-080 2026-09-09] 系统委托 remark 白名单 — AUTO 认领的唯一凭证 ──────────
# 病灶: _query_today_futu_orders 返回当日 env 内**所有**已成交委托, AUTO/MANUAL 分类
# 此前只看 "symbol in _today_orders" ⇒ 切实盘后 (AUTO_TRADE_ACCOUNT=real), 用户在富途
# APP 直接下的手动单 (无 remark) 会被认领为 AUTO → 纳入 PositionStore/DB trader_type=
# AUTO → 被自动退出监控平掉真钱手动仓。2026-07-02 "安全默认 MANUAL" 只修了 else 分支,
# 没修 _today_orders 命中分支。
# 活体探针实证 (2026-09-09, history_order_list_query 9/1-9/9):
#   SIMULATE env 159 笔 remark 全部 'pb_*' (playbook client_order_id=pb_{iid}_{env});
#   REAL env 45 笔 remark 全部 '' (APP/看板手动单 place_order 不带 remark)。
#   remark 经 broker 往返不丢失 ⇒ 可作归属凭证。
# 白名单 = 全部 auto 下单生产者的 remark 前缀 (grep 'remark=' 全仓枚举):
#   pb_*              playbook 入场 (pb_{iid}_{env}) + 平仓 (pb_close_/pb_be_close_/pb_stop_)
#                     — dual_broker.py:228 remark=order.client_order_id
#   orb_*             ORB 引擎 (engine.py:2327, 现禁用, 预留)
#   OVER_LIMIT_TRIM   position_sync 自trim (:2427)
# fail-safe 方向: 未知/空 remark → 非系统单 → MANUAL (宁可漏认领也不误平手动仓)。
# 严格大小写匹配 (生产者全小写 pb_/orb_, OVER_LIMIT_TRIM 全大写; 不做 case-fold,
# 意外大小写变体归 MANUAL 更安全)。
# 注意: 外部平仓检测 (~:1548 _today_orders.get) **不得**用本白名单过滤 —
# 手动/APP 平仓正是它要检测的对象。本白名单只用于 AUTO 认领两处分类点。
_SYSTEM_ORDER_REMARK_PREFIXES = ("pb_", "orb_", "ext_entry", "ext_exit_", "OVER_LIMIT_TRIM")


def is_system_order_remark(remark) -> bool:
    """委托 remark 是否出自系统下单路径 (RFC-080 AUTO 认领白名单).

    fail-safe: None/空/未知前缀 → False (归 MANUAL, 与 2026-07-02 安全默认同向)。
    """
    if not remark:
        return False
    r = str(remark)
    return any(r.startswith(p) for p in _SYSTEM_ORDER_REMARK_PREFIXES)


def _entry_time_epoch(entry_time) -> Optional[float]:
    try:
        parsed = datetime.fromisoformat(str(entry_time).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except (TypeError, ValueError):
        return None


```

## Lines 385-417 (matched: def _reconcile)
```python
            return False

    def _reconcile_playbook_execution_states(self, ctx, trd_env, broker_env: str,
                                             futu_positions: Dict, limit: int = 25) -> None:
        """B1-lite 未决订单对账；只有 broker 明确零成交终态才解锁。"""
        db_path = _get_db_path()
        if not os.path.exists(db_path):
            return
        conn = safe_sqlite_connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, symbol, direction, stop_loss, take_profit, execution_price, "
                "execution_time, execution_state, execution_attempt, broker_order_id, broker_env "
                "FROM playbook_instructions WHERE execution_state IN "
                "('UNKNOWN','SUBMITTED','FILLED_UNBOOKED') ORDER BY updated_at, id LIMIT ?",
                (int(limit),),
            ).fetchall()
            from app.playbook.db import PlaybookDB
            pdb = PlaybookDB(conn=conn)
            for row in rows:
                env = str(row["broker_env"] or broker_env).lower()
                if env != broker_env:
                    continue
                instruction_id = int(row["id"])
                state = str(row["execution_state"] or "")
                order_id = str(row["broker_order_id"] or "")
                attempt = int(row["execution_attempt"] or 0)
                dealt_qty = 0.0
                dealt_avg_price = 0.0

                if state in ("UNKNOWN", "SUBMITTED"):
                    try:
```


# /opt/ftseek/FTSeek-US/app/pipeline/signal_pipeline.py (total 1433 lines)

## File Header (lines 1-80)
```python
"""
信号管线 — FTSeek-US 核心决策链路

从 scheduler_tasks.py 拆分而来。本模块包含完整的信号生成到决策管线，
回测适配层和实盘调度器共用此代码。

管线流程:
    scan_loop()                  # 主循环: 市场门禁 + 熔断 + 巡检
      └─ _run_signal_scan_impl() # 自适应轮询批次
           └─ _process_symbol_impl()  # 单标的完整决策链路
                ├─ 行情获取 + 动态成本注入
                ├─ 财报感知 + VIX Regime 注入
                ├─ 作战计划快车道 (跳过投票)
                ├─ 信号生成 (盘中技术面 / 盘后LLM投票)
                ├─ 大盘联动过滤 + 金字塔加仓检测
                ├─ 共识检查 + SignalScore 构建
                └─ 风控检查 → 下单委托
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from app.utils.timezone import now_et


def _get_vwap_deviation(scheduler, symbol: str, cached_context: dict = None) -> Optional[float]:
    """M9 (2026-06-03): 从已获取的 context 读取 VWAP 偏离度, 避免重复 get_full_market_context。
    返回 None 表示数据不可用 (pre_trade_check 将软放行)。
    """
    try:
        # 优先使用已缓存的 context (H1 去重)
        if cached_context and "ticker_vwap_deviation" in cached_context:
            return float(cached_context["ticker_vwap_deviation"])
        if scheduler and hasattr(scheduler, 'market_data') and scheduler.market_data:
            ctx = scheduler.market_data.get_full_market_context(symbol)
            if ctx and "ticker_vwap_deviation" in ctx:
                return float(ctx["ticker_vwap_deviation"])
    except Exception:
        pass
    return None

from app.core.models import (
    SignalScore, Direction, MarketRegime,
)
from app.storage.rejection_logger import get_rejection_logger
from app.coordinator.manual_hold_registry import manual_hold_registry

logger = logging.getLogger(__name__)

# ── Rapid Regime Change 历史缓冲区 (P0 2026-06-17) ──
# 保留最近 5 分钟的 VIX/SPY 采样, 供 check_rapid_regime_change 检测急变
_VIX_HISTORY: list = []   # [(timestamp, vix_value), ...]
_SPY_HISTORY: list = []   # [(timestamp, spy_value), ...]
_HISTORY_MAX_SECONDS: float = 360.0  # 保留最近 6 分钟 (略大于 5 分钟窗口)
_HISTORY_MAX_ENTRIES: int = 30       # 最多 30 个采样点


# ============================================================
# 决策引擎辅助 (Task #19)
# ============================================================
def _check_execution_rules(rules: dict, symbol: str, context: dict) -> bool:
    """检查 execution_rules 是否允许执行。

    返回 True=允许, False=阻止。规则缺失或无法判定时一律放行。
    """
    if not rules or not isinstance(rules, dict):
        return True

    try:
        prev_close = float(context.get("prev_close", 0) or 0)
        open_price = float(context.get("open_price", 0) or 0)
        current_price = float(context.get("current_price", context.get("price", 0)) or 0)
    except (TypeError, ValueError):
        return True

    if prev_close > 0 and open_price > 0:
```


# /opt/ftseek/FTSeek-US/app/risk/position_exit_monitor.py (total 2556 lines)

## File Header (lines 1-80)
```python
"""
FTSeek-US 持仓退出监控 (Position Exit Monitor)

从 stoploss_daemon 提取的核心止损/止盈逻辑，合并到主进程内执行。
消除独立 Daemon 的双写入者竞争问题，确保：
  1. 单一写入者 (无 DB 竞争)
  2. 复用主进程 Futu 连接 (不消耗额外槽位)
  3. trail_state 实时更新 (不再全 0.0)
  4. 内存 SSOT (PositionStore → DB 单向同步)

规则 (由 exit_policy.yaml 统一配置):
  0. 防振荡: 同一持仓 60 秒内不重复触发
  1. Flash Exit: 60s 内波动 > flash_multiplier × ATR (优化: 1.0x, 60s扫描)
  1.5. Day Trade Mode: 紧止损(1.5xATR/3%) + 主动止盈(+4%/+8%/+15%) + 时间衰减 (R/R≥1.33)
  2. ATR 动态止损: entry - atr × multiplier (非线性状态机, initial=2.0x)
  2.5. Scale Out 分批止盈: 5%/10%/18% 三层
  3. Chandelier 追踪: 峰值 - atr × trail_mult (盈利≥1.2ATR 激活, regime差异化)
  4. 保本触发: 盈利≥1.0ATR 移至成本价+buffer
  3.5. Pre-Close Exit: 分层退出 + 时间加权 (亏损全平/0~0.5ATR减半/0.5~1ATR减1/4/≥1ATR过夜)
  5. Time Stop: > 72h 持仓

数学期望修正 (P0-2, 2026-04-28):
  - profit_atr 改为有向（signed）：盈利>0，亏损<0
  - 亏损时禁止激活 Chandelier / Breakeven（原 abs() 实现会误触发追踪线上移）
  - 止损定价逻辑下沉到 atr_stop_engine（纯函数，可单测/回测）
  - 接入 cost_buffer：止损线额外让出 k × total_cost_bps 价格空间

优化记录 (2026-05-03):
  - P0: Flash Exit 阈值 0.8→1.0x ATR, 扫描间隔 30→60s (历史2次Flash全部亏损)
  - P1: PRE_CLOSE_EXIT 改为分层退出 + 时间加权, 根因修复 54% 被动强平问题
  - P1: Day Trade Mode 参数全面放宽, 匹配 session-aware 实际波动
  - P1: Chandelier 激活门槛从 2.0ATR 降至 1.2ATR, regime乘数放宽 (给利润更多空间)
  - P2: 分层止盈门槛从 8%/15%/25% 降至 5%/10%/18%
  - P2: Chandelier regime乘数: normal 2.0→2.5, trending 3.0→3.5, volatile 1.5→2.0
"""
import logging
import math
import time
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from pathlib import Path

from app.core.db_utils import safe_sqlite_connect

# ATR 止损定价引擎（纯函数）
from .atr_stop_engine import compute_stop_lines, compute_profit_atr_signed, resolve_trail_multiplier

# Task #19 (2026-05-27): 退出规则模块化 — 各规则已拆分到 exit_rules/
# ExitMonitor 自身只负责编排 (规则调度 + 质量评分 + 执行平仓)
from .exit_rules import (
    ScaleOutRule,
    TrailingStopRule,
    PreCloseExitRule,
    check_scale_out as _impl_scale_out,
    check_trailing_stop as _impl_trailing_stop,
    check_pre_close_exit as _impl_pre_close_exit,
    # Task #48: 优先级注册表 (渐进式引入, 现仅作为辅助入口)
    ExitRuleRegistry,
    ExitRuleContext,
    ExitReason,
)

# 共享内核 — session-aware ATR (P2 2026-05-01)
from app.core.shared_kernel import get_shared_kernel

# C4 修复 (2026-05-13): 全局 EOD 配置 — 主线 + ORB 共享
from app.risk.eod_config import load_eod_config

# 退出质量追踪 (P2 2026-05-01)
try:
    from app.storage.exit_quality_tracker import get_exit_quality_tracker
    _QUALITY_TRACKER_AVAILABLE = True
except Exception:
    get_exit_quality_tracker = None  # type: ignore
    _QUALITY_TRACKER_AVAILABLE = False

# 退出规则质量评分 (P2 2026-05-27): 多规则触发时选最优
try:
```


# /opt/ftseek/FTSeek-US/app/playbook/exit_engine.py (total 1129 lines)

## File Header (lines 1-80)
```python
"""Playbook 退出引擎 — 自管退出规则链 (P2a 纯搬家, 2026-09-06).

本模块全部代码逐字迁自 app/playbook/pipeline.py (9453 行单体), 零行为变更:
  - _check_playbook_exit_rules  (711 行, 规则[0]冷却/[0.5]早期紧SL/[1]SL/[1.5]TP/
    [2]OVERNIGHT_GAP_STOP/[3]PRE_CLOSE/[3.4]MFE_STALL/[4]TIME_EXIT/[5]MAX_HOLD)
  - 其私有 helper: _is_overnight_position / _get_factor_exit_config (+_factor_exit_warned)
    / _compute_dynamic_effective_sl / _load_horizon_config
  - 进程级日志去重集 _EARLY_TIGHT_SL_LOGGED / _LOSER_DS_EXEMPT_LOGGED
  - P7 埋点缝 _EARLY_DEAD_SHADOW_PATH (tests/conftest.py 登记已同步迁至本模块)

隔夜豁免判定不在此复制: EOD 单点走 app.playbook.overnight_decision (P1),
模块顶部以同名别名引入, 使下方迁入函数体保持逐字不变。
pipeline.py 侧保留同名转发器 (外部调用点/既有测试 import 路径不变)。

回滚: git revert 本提交即可 (pipeline 转发器与本体并存, 无双写)。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict

from app.playbook.overnight_decision import (
    horizon_max_hold_hit as _horizon_max_hold_hit,
    overnight_eod_exempt as _overnight_eod_exempt,
    w_week_loser_exempt as _loser_overnight_exempt,
)

logger = logging.getLogger(__name__)

# P7 埋点缝 (值与解析语义逐字不变; 隔离登记在 tests/conftest.py _PRODUCTION_DATA_SEAMS)
_EARLY_DEAD_SHADOW_PATH = Path("data/early_dead_signal_shadow.jsonl")

# 惰性 ET 时区访问 (与 pipeline._now_et 同模式; SSOT = app.core.scheduler_triggers.ET)
_ET = None


def _now_et():
    global _ET
    if _ET is None:
        from app.core.scheduler_triggers import ET as _et
        _ET = _et
    return _ET


def _is_overnight_position(entry_time_str: str) -> bool:
    """RFC-061 #6: 仓位是否隔夜 (entry 在之前的交易日, 非今日入场).

    用于隔夜深亏快平 (OVERNIGHT_GAP_STOP): 持仓过夜 且 亏损 ≥ 阈值 即快平。
    2026-09-24 勘误: 名称里的 "GAP" 是历史命名 — 判据里没有 gap 校验, 开盘跳空只是其中一种触发。
    用 ET 日期判断. 异常返回 False (不触发, 保守).
    """
    if not entry_time_str:
        return False
    try:
        _et = _now_et()
        _entry = datetime.fromisoformat(entry_time_str)
        if hasattr(_entry, "tzinfo") and _entry.tzinfo is not None:
            _entry_date = _entry.astimezone(_et).date()
        else:
            _entry_date = _entry.date()
        return _entry_date < datetime.now(_et).date()
    except Exception:
        return False



def note_exit_gap(meta: dict, quote, exit_reason: str, current_price: float) -> None:
    """出场日 gap 证据采集 (record-only, RFC-106 §5.2 未决项落地).

    OVERNIGHT_GAP_STOP 判据无 gap 校验 (见 _is_overnight_position 勘误) — 本函数
    **不改任何判据/阈值/平仓动作**, 只在出场决策成立时把决策时刻报价里的
    prev_close / today_open 落盘, 供复盘区分「真开盘跳空」与「开盘后下杀」。
    与入场侧 entry_gap 对称 (playbook_service 复用 _arr_ctx 同款字段)。
    fail-open: quote 缺 prev_close/open → 不写键 (历史缺省语义不变), 绝不阻断平仓。
    回滚: 删本函数 + 两个调用点。
    """
```


# /opt/ftseek/FTSeek-US/app/execution/broker_adapter.py (total 1347 lines)

## File Header (lines 1-80)
```python
# app/execution/broker_adapter.py
"""
富途券商适配器

实现富途 OpenD 真实连接：
- 模拟账户美股下单（TrdEnv.SIMULATE）
- 持仓查询
- 实时报价
"""
import logging
import os
import time
import threading
from typing import Optional, Dict, Any, List

from app.core.models import Order, OrderState, Direction, OrderType
from app.execution.futu_accounting import normalize_futu_account

logger = logging.getLogger(__name__)


class BrokerUnavailableError(RuntimeError):
    """Raised when broker state or positions cannot be determined."""


class FutuBrokerAdapter:
    """富途券商适配器（支持模拟和真实环境）"""

    # 心跳与重连配置
    HEARTBEAT_INTERVAL_SEC = 30
    MAX_RECONNECT_ATTEMPTS = 3
    RECONNECT_BACKOFF_SEC = [5, 15, 30]  # 指数退避间隔

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11111,
        trade_env: int = 1,    # 0=真实，1=模拟
        account_id: str = "",
        on_disconnect_callback=None,
        allow_offline_simulated_fill: bool = False,
    ):
        if trade_env not in (0, 1):
            raise ValueError(f"Invalid trade_env: {trade_env!r}; expected 0 (real) or 1 (simulate)")
        self.host = host
        self.port = port
        self.trade_env = trade_env
        self.allow_offline_simulated_fill = allow_offline_simulated_fill
        self.account_id = account_id
        self.connected = False
        self.quote_ctx = None
        self.trade_ctx = None
        # 实盘解锁密码: 优先 MD5 (更安全，文件里不存明文)
        # 获取方式: echo -n "你的密码" | md5sum | awk '{print $1}'
        self._unlock_pwd_md5 = os.getenv("FUTU_UNLOCK_PWD_MD5", "")   # 推荐: MD5 哈希
        self._unlock_pwd_plain = os.getenv("FUTU_UNLOCK_PWD", "")      # 备选: 明文密码
        self._unlock_pwd = self._unlock_pwd_md5 or self._unlock_pwd_plain
        self._use_md5 = bool(self._unlock_pwd_md5)  # 有 MD5 则用 MD5，否则用明文

        # 心跳与重连状态
        self._last_heartbeat_time = 0.0
        self._heartbeat_failures = 0
        self._reconnect_attempts = 0
        self._reconnect_lock = threading.Lock()
        self._on_disconnect_callback = on_disconnect_callback
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop_event = threading.Event()

    # ------------------------------------------------------------------
    # 兜底安全门禁 (2026-06-06): broker 前最后一道墙
    # ------------------------------------------------------------------

    _NOTIONAL_HARD_CAP_PCT = 0.30  # 30% equity — 粗略硬兜底，比上游 25% 宽松

    def _notional_sanity_check(self, symbol: str, qty, price: float, is_close: bool = False) -> bool:
        """
        兜底检查: 单笔名义 ≤ equity × 30%.
        不依赖 position_store, 只查 equity. 如果 equity 获取失败则 fail-close (拒绝).
        这是所有路径的最后一道墙，不是主要防线。

```

## Lines 472-504 (matched: def submit_order)
```python
    # ------------------------------------------------------------------

    def submit_order(self, order: Order) -> Dict[str, Any]:
        """
        提交订单到富途模拟账户

        Args:
            order: 订单对象

        Returns:
            {"order_id": str, "status": str, "futu_order_id": str}
        """
        # 2. 提交订单
        if not self.is_connected():
            if self.trade_env == 0 or not self.allow_offline_simulated_fill:
                raise RuntimeError(
                    f"[实盘安全] Broker 未连接, 拒绝提交 {order.symbol} 订单 "
                    f"(trade_env={self.trade_env}, offline_simulated_fill={self.allow_offline_simulated_fill})"
                )
            return self._simulate_fill(order)

        try:
            from futu import TrdEnv, TrdSide, OrderType as FutuOrderType, RET_OK, TimeInForce, Session

            # 转换方向 (确保 Order 对象有 direction 或从 action 推断)
            trd_side = order.trd_side if hasattr(order, 'trd_side') else (TrdSide.BUY if order.direction == Direction.LONG else TrdSide.SELL)
            # 转换订单类型
            futu_order_type = FutuOrderType.NORMAL
            if order.order_type == OrderType.MARKET:
                futu_order_type = FutuOrderType.MARKET
            # 转换交易环境
            trd_env = TrdEnv.SIMULATE if self.trade_env == 1 else TrdEnv.REAL

```

## Lines 791-823 (matched: def get_position)
```python
    # ------------------------------------------------------------------

    def get_positions(self) -> List[Dict[str, Any]]:
        """
        查询美股持仓列表。综合账户可能返回跨市场持仓，此处必须按代码前缀隔离。

        Returns:
            [{"symbol", "qty", "cost_price", "current_price", "unrealized_pnl"}, ...]
        """
        if not self.is_connected():
            raise BrokerUnavailableError("Broker is not connected; positions are unknown")

        try:
            from futu import TrdEnv, RET_OK
            try:
                from futu import Currency as _FutuCurrency
            except ImportError:
                _FutuCurrency = None  # 老 SDK 无 Currency 枚举 → 退回 SDK 默认 (USD)

            trd_env = TrdEnv.SIMULATE if self.trade_env == 1 else TrdEnv.REAL
            # 2026-09-24 pl_val 币种防御 (切实盘前置项): 显式钉 currency=USD,
            # 不依赖 SDK 默认值; 返回行透传 currency 供下游 (pre_trade_gate 回撤)
            # 校验。若行币种非 USD, pl_val 量纲错 ~7.8x (HKD) 会静默污染回撤熔断。
            _q_kwargs = dict(
                trd_env=trd_env,
                acc_id=int(self.account_id) if self.account_id else 0,
            )
            if _FutuCurrency is not None:
                _q_kwargs["currency"] = _FutuCurrency.USD
            ret, data = self.trade_ctx.position_list_query(**_q_kwargs)

            if ret == RET_OK and len(data) > 0:
                positions = []
```

## Lines 606-638 (matched: def cancel_order)
```python
            raise

    def cancel_order(self, order_id: str) -> bool:
        """取消订单

        DEB-20260903 #97: 断连/缺依赖时返回 False 而非模拟成功,
        防止上游误判订单已取消导致重复下单或状态不一致。
        """
        if not self.is_connected():
            logger.warning(f"[富途] 未连接，无法确认取消订单 {order_id}")
            return False

        try:
            from futu import TrdEnv, ModifyOrderOp, RET_OK

            trd_env = TrdEnv.SIMULATE if self.trade_env == 1 else TrdEnv.REAL

            ret, data = self.trade_ctx.modify_order(
                modify_order_op=ModifyOrderOp.CANCEL,
                order_id=order_id,
                qty=0,
                price=0,
                trd_env=trd_env,
                acc_id=int(self.account_id) if self.account_id else 0,
            )
            if ret == 0:  # RET_OK
                # DEB-20260904 #185: RET_OK 只代表撤单"请求"被接受, 不代表已到终态.
                # 补一次状态查询: 已 FILLED_ALL = 撤单输了竞速 → False;
                # CANCELLING_*/CANCELLED_* → True; 其余/查询异常 → True+warning
                # (终态确认交给 position_sync 30s 兜底, 不阻塞主链路).
                try:
                    ret_q, data_q = self.trade_ctx.order_list_query(
                        order_id=order_id, trd_env=trd_env,
```


# /opt/ftseek/FTSeek-US/app/core/scheduler_core.py (total 3136 lines)

## File Header (lines 1-80)
```python
"""
FTSeek-US 核心调度器 - 核心调度逻辑

包含:
- PositionStore 类
- TradingScheduler 主类框架
- 初始化和配置
"""
import logging
import os
import threading
import time
from datetime import datetime, timedelta, date
from typing import Dict, Any, List, Optional, Set
from pathlib import Path
import sys

from app.utils.timezone import ET

from app.core.config_defaults import CFG

# 路径
BASE_DIR = Path(__file__).resolve().parent.parent
# P0 Fix 2026-04-23: PROJECT_ROOT is one level above BASE_DIR
PROJECT_ROOT = BASE_DIR.parent
sys.path.insert(0, str(BASE_DIR))

from app.data.news_provider import USNewsProvider
from app.data.market_data_router import MarketDataRouter
# 2026-07-10: VotingCoordinator/VotingConfig 顶层 import 已移除.
# scheduler 不再实例化 VotingCoordinator (改用 NullVotingAdapter/BridgeVotingAdapter).
# DI 容器的 voting_coordinator() 工厂保留但仅作为 playbook fallback, 那里用延迟 import.
from app.core.models import (
    SignalScore, Direction, MarketRegime,
    Order, OrderType, OrderState, Position,
    TradeEvent, FailureRecord, FailureType, ExitReason, AlertLevel,
)
from app.core.events import EventStore
from app.storage.trade_store import TradeStore
# ADR-003 (2026-06-04): 期权已禁用 (options.enabled=false), 移除 OptionSelector 初始化
# 保留导入以兼容 position_monitor 的期权平仓代码 (处理历史残留仓位)
# from app.execution.options_selector import OptionSelector, OptionContract

logger = logging.getLogger(__name__)


class PositionStore:
    """内存持仓管理（轻量级，无富途依赖）"""

    def __init__(self):
        self._positions: Dict[str, Position] = {}  # symbol -> Position
        # P0-4a 修复 (2026-05-03): 改用 RLock 允许 sync 线程重入
        # sync 线程获取锁后调用 add/remove (内部也获取锁) 不会死锁
        self._lock = threading.RLock()
        # P0 修复 (2026-06-20): pending 集合防止重复开仓
        # broker_sync 5s debounce 窗口内, 多个 signal_pipeline 线程可能同时
        # 检查 has() 返回 False, 导致重复开仓. pending 即时锁定 symbol,
        # 订单提交/失败后释放. 纯内存操作, 零 IO 开销.
        self._pending: Set[str] = set()

    def add(self, symbol: str, position: Position):
        # Critical #3 修复 (2026-06-02):
        # 内存写入 + daily_active_pool 同步必须在同一锁内, 否则
        # scan_loop / monitor_loop 并发调用时可能出现:
        #   1) 内存与动态池/JSON 文件不一致
        #   2) daily_active_pool.save() 与 remove() 交叉导致条目丢失
        # 锁顺序: PositionStore._lock → DailyActivePool._lock
        # (DailyActivePool 不会回调 PositionStore, 因此不存在循环依赖)
        with self._lock:
            self._positions[symbol] = position
            self._pending.discard(symbol)  # 开仓成功，清除pending标记

            # 自动同步动态池 (P0 修复: 传入市场前缀)
            # Task #49 (2026-05-28): 消除 silent exception, 失败记录但不中断主流程 (非关键路径)
            try:
                from app.utils.daily_active_pool import get_daily_active_pool
                pool = get_daily_active_pool(market_prefix="US.")
                pool.add_position(symbol, metadata={
                    "direction": position.direction.value if hasattr(position.direction, 'value') else str(position.direction),
                    "quantity": position.size,
```
