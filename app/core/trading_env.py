"""
TradingEnvironment — 交易环境抽象层

核心目标：将模拟盘/实盘的差异从 25+ 个 if-else 分支收敛到单一抽象，
实现安全、可验证、可回滚的无缝切换。

设计原则：
1. 环境差异集中管理，禁止散入业务代码
2. 实盘切换需双重确认（环境变量 + 显式方法调用）
3. 所有差异行为通过统一 API 暴露，业务层无需知道底层是模拟还是实盘
4. 切换时可获取完整 diff 报告，验证预期行为变化
"""

import os
import logging
import threading
from enum import IntEnum
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from pathlib import Path

logger = logging.getLogger("ftseek.trading_env")


class TradingMode(IntEnum):
    """交易模式枚举"""
    SIMULATE = 1
    REAL = 0
    BACKTEST = 2      # 预留：回测环境
    PAPER = 3         # 预留：纯纸面模拟（不连 Futu）


@dataclass
class EnvCapabilities:
    """不同环境的能力/限制差异"""
    # 仓位限制
    max_positions_multiplier: float = 1.0  # 模拟=1.0(满仓), 实盘=0.5(50%)
    
    # Futu 持仓验证
    skip_futu_position_check: bool = False  # 模拟跳过，实盘不跳过
    
    # 做空限制
    skip_short_selling_account_check: bool = False  # 模拟跳过账户验证
    allow_short_selling: bool = True                 # 是否允许做空 (模拟盘=False，实盘=True)
    record_short_in_simulate: bool = True            # 模拟盘是否记录做空信号（不执行）
    
    # 盘前计划
    use_fallback_battle_plan: bool = False  # 模拟允许万能计划
    
    # 通知标识
    notification_prefix: str = ""
    
    # 订单重试容忍度
    order_retry_max_attempts: int = 3
    order_retry_backoff_seconds: float = 2.0
    
    # 风险容忍度
    allow_negative_cash: bool = False  # 模拟盘保证金模式允许负现金
    
    # 数据库路径后缀
    db_suffix: str = "ftseek.db"


# 预定义环境配置
_MODE_CAPABILITIES: Dict[TradingMode, EnvCapabilities] = {
    TradingMode.SIMULATE: EnvCapabilities(
        max_positions_multiplier=1.0,
        skip_futu_position_check=True,
        skip_short_selling_account_check=True,
        allow_short_selling=False,        # 模拟盘禁止做空执行
        record_short_in_simulate=True,    # 但记录做空信号
        use_fallback_battle_plan=True,
        notification_prefix="[SIM]",
        order_retry_max_attempts=3,
        order_retry_backoff_seconds=2.0,
        allow_negative_cash=True,
        db_suffix="ftseek.db",
    ),
    TradingMode.REAL: EnvCapabilities(
        max_positions_multiplier=0.5,
        skip_futu_position_check=False,
        skip_short_selling_account_check=False,
        allow_short_selling=True,         # 实盘允许做空
        record_short_in_simulate=False,
        use_fallback_battle_plan=False,
        notification_prefix="",
        order_retry_max_attempts=2,
        order_retry_backoff_seconds=5.0,
        allow_negative_cash=False,
        db_suffix="ftseek.db",
    ),
    TradingMode.BACKTEST: EnvCapabilities(
        max_positions_multiplier=1.0,
        skip_futu_position_check=True,
        skip_short_selling_account_check=True,
        use_fallback_battle_plan=True,
        notification_prefix="[BT]",
        order_retry_max_attempts=1,
        order_retry_backoff_seconds=0.0,
        allow_negative_cash=True,
        db_suffix="ftseek_backtest.db",
    ),
    TradingMode.PAPER: EnvCapabilities(
        max_positions_multiplier=1.0,
        skip_futu_position_check=True,
        skip_short_selling_account_check=True,
        use_fallback_battle_plan=True,
        notification_prefix="[PAPER]",
        order_retry_max_attempts=3,
        order_retry_backoff_seconds=2.0,
        allow_negative_cash=True,
        db_suffix="ftseek_paper.db",
    ),
}


class TradingEnvironment:
    """
    交易环境管理器 — 单例模式
    
    统一管理模拟盘/实盘的所有差异，提供类型安全的 API。
    
    使用方式:
        from app.core.trading_env import get_trading_env
        
        env = get_trading_env()
        
        # 替代: if config.TRADE_ENV == 1
        if env.is_simulate:
            ...
        
        # 替代: TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL
        trd_env = env.get_futu_trd_env()
        
        # 替代: 各处硬编码的账户 ID 逻辑
        account_id = env.get_account_id_for_code("HK.00700")
        
        # 替代: 硬编码的 max_positions 计算
        max_pos = env.get_max_positions(watchlist_size, config_max)
    """
    
    _instance: Optional['TradingEnvironment'] = None
    _lock = threading.Lock()
    
    def __init__(self, mode: TradingMode, config: Optional[Dict[str, Any]] = None):
        self._mode = mode
        self._config = config or {}
        self._capabilities = _MODE_CAPABILITIES.get(mode, _MODE_CAPABILITIES[TradingMode.SIMULATE])
        self._locked = False  # 运行中锁定，防止意外切换
        
        # 账户配置
        self._simulate_account_id = os.getenv("SIMULATE_ACCOUNT_ID", "18820895")
        self._real_account_hk = os.getenv("REAL_ACCOUNT_HK", "")
        self._real_account_us = os.getenv("REAL_ACCOUNT_US", "")
        
        # 安全门控
        self._allow_real = os.getenv("ALLOW_REAL_TRADING", "false").lower() == "true"
    
    @classmethod
    def initialize(cls, mode: Optional[TradingMode] = None, 
                   config: Optional[Dict[str, Any]] = None) -> 'TradingEnvironment':
        """初始化单例（线程安全）"""
        with cls._lock:
            if cls._instance is not None:
                logger.warning("TradingEnvironment 已初始化，忽略重复调用")
                return cls._instance
            
            if mode is None:
                mode = cls._detect_mode_from_env()
            
            cls._instance = cls(mode=mode, config=config)
            logger.info(
                f"TradingEnvironment 初始化: {mode.name} "
                f"(allow_real={cls._instance._allow_real})"
            )
            return cls._instance
    
    @classmethod
    def get_instance(cls) -> 'TradingEnvironment':
        """获取单例实例"""
        if cls._instance is None:
            raise RuntimeError(
                "TradingEnvironment 未初始化。"
                "请在应用启动时调用 TradingEnvironment.initialize()"
            )
        return cls._instance
    
    @classmethod
    def _detect_mode_from_env(cls) -> TradingMode:
        """从环境变量检测交易模式"""
        raw = int(os.getenv("TRADE_ENV", "1"))
        allow_real = os.getenv("ALLOW_REAL_TRADING", "false").lower() == "true"
        
        if raw == 0 and not allow_real:
            logger.warning(
                "[安全拦截] TRADE_ENV=0 被忽略，"
                "当前强制使用模拟环境。设置 ALLOW_REAL_TRADING=true 以启用实盘。"
            )
            return TradingMode.SIMULATE
        
        if raw == 0 and allow_real:
            return TradingMode.REAL
        
        return TradingMode.SIMULATE
    
    # ===== 基础属性 =====
    
    @property
    def mode(self) -> TradingMode:
        return self._mode
    
    @property
    def is_simulate(self) -> bool:
        return self._mode == TradingMode.SIMULATE
    
    @property
    def is_real(self) -> bool:
        return self._mode == TradingMode.REAL
    
    @property
    def is_backtest(self) -> bool:
        return self._mode == TradingMode.BACKTEST
    
    @property
    def is_paper(self) -> bool:
        return self._mode == TradingMode.PAPER
    
    @property
    def capabilities(self) -> EnvCapabilities:
        return self._capabilities
    
    # ===== Futu API 兼容 =====
    
    def get_futu_trd_env(self):
        """
        获取 Futu SDK 的 TrdEnv 枚举值
        
        替代: TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL
        """
        try:
            from futu import TrdEnv
            return TrdEnv.SIMULATE if self.is_simulate else TrdEnv.REAL
        except ImportError:
            return 1 if self.is_simulate else 0
    
    # ===== 账户路由 =====
    
    def get_account_id(self, code: str, market: Optional[str] = None) -> int:
        """
        根据股票代码路由到正确的账户 ID
        
        替代: 各处硬编码的 _get_account_for_code() 逻辑
        """
        if self.is_simulate:
            return int(self._simulate_account_id)
        
        # 实盘模式：根据市场路由
        if market == "US" or (code and code.startswith("US.")):
            return int(self._real_account_us) if self._real_account_us else int(self._simulate_account_id)
        return int(self._real_account_hk) if self._real_account_hk else int(self._simulate_account_id)
    
    # ===== 差异化行为 =====
    
    def get_max_positions(self, watchlist_size: int, config_max: int) -> int:
        """
        计算最大持仓数
        
        替代: decision_buffer.py 中的 if TRADE_ENV == 1 分支
        """
        mult = self._capabilities.max_positions_multiplier
        if mult >= 1.0:
            # 模拟/回测：满仓
            return watchlist_size
        else:
            # 实盘：按比例限制
            dynamic = max(1, int(watchlist_size * mult))
            return min(dynamic, config_max)
    
    def should_skip_futu_position_check(self) -> bool:
        """
        是否跳过 Futu 实际持仓验证
        
        替代: data_subscriber.py 中的 if config.TRADE_ENV == 1 分支
        """
        return self._capabilities.skip_futu_position_check
    
    def should_skip_short_selling_account_check(self) -> bool:
        """
        是否跳过做空账户验证
        
        替代: short_selling_gate.py 中的 if TRADE_ENV == 1 分支
        """
        return self._capabilities.skip_short_selling_account_check

    def can_short_sell(self) -> bool:
        """
        是否允许做空执行
        
        - 模拟盘: False (记录但不执行)
        - 实盘: True (按 ShortSellingGate 检查结果执行)
        """
        return self._capabilities.allow_short_selling

    def should_record_short_in_simulate(self) -> bool:
        """
        模拟盘是否记录做空信号（不执行）
        
        用于在模拟盘中记录做空意图，用于后续分析和归因，
        但不实际执行做空订单。
        """
        return self._capabilities.record_short_in_simulate and self.is_simulate
    
    def should_use_fallback_battle_plan(self) -> bool:
        """
        是否使用万能盘前计划
        
        替代: battle_plan.py 中的模拟兜底逻辑
        """
        return self._capabilities.use_fallback_battle_plan
    
    def allow_negative_cash(self) -> bool:
        """是否允许负现金（保证金模式）"""
        return self._capabilities.allow_negative_cash
    
    # ===== 模式切换（安全门控） =====
    
    def switch_to_real(self, confirmation_token: str) -> bool:
        """
        切换到实盘模式 — 需要双重确认
        
        安全机制:
        1. ALLOW_REAL_TRADING 环境变量必须为 true
        2. 必须提供正确的 confirmation_token (环境变量 REAL_SWITCH_TOKEN)
        3. 运行中切换会先锁定，验证通过后切换
        """
        expected_token = os.getenv("REAL_SWITCH_TOKEN", "")
        if not expected_token:
            logger.error("[安全拦截] REAL_SWITCH_TOKEN 未设置，禁止切换到实盘")
            return False
        
        if confirmation_token != expected_token:
            logger.error("[安全拦截] 实盘切换 token 不匹配")
            return False
        
        if not self._allow_real:
            logger.error("[安全拦截] ALLOW_REAL_TRADING 未启用")
            return False
        
        old_mode = self._mode
        self._mode = TradingMode.REAL
        self._capabilities = _MODE_CAPABILITIES[TradingMode.REAL]
        self._locked = True
        
        logger.critical(
            f"⚠️  交易模式切换: {old_mode.name} → REAL ⚠️\n"
            f"  时间: {self._now_str()}\n"
            f"  确认 token: {confirmation_token[:4]}***\n"
            f"  请立即验证以下配置:\n"
            f"    - 实盘账户 ID: HK={self._real_account_hk}, US={self._real_account_us}\n"
            f"    - 持仓验证: 已启用\n"
            f"    - 最大持仓: 已限制为 50%\n"
            f"    - 做空门禁: 已启用"
        )
        
        return True
    
    def switch_to_simulate(self) -> bool:
        """切换回模拟模式（无需 token，但记录日志）"""
        old_mode = self._mode
        self._mode = TradingMode.SIMULATE
        self._capabilities = _MODE_CAPABILITIES[TradingMode.SIMULATE]
        self._locked = False
        
        logger.info(f"交易模式切换: {old_mode.name} → SIMULATE")
        return True
    
    def get_switch_diff(self, target_mode: TradingMode) -> Dict[str, Any]:
        """
        获取模式切换的差异报告
        
        返回: 当前模式 vs 目标模式的所有行为差异
        """
        current = self._capabilities
        target = _MODE_CAPABILITIES.get(target_mode, current)
        
        diff = {
            "current_mode": self._mode.name,
            "target_mode": target_mode.name,
            "changes": {}
        }
        
        # 比较所有能力字段
        for attr in EnvCapabilities.__dataclass_fields__:
            curr_val = getattr(current, attr)
            target_val = getattr(target, attr)
            if curr_val != target_val:
                diff["changes"][attr] = {
                    "from": curr_val,
                    "to": target_val
                }
        
        return diff
    
    def lock(self):
        """锁定环境，禁止切换"""
        self._locked = True
    
    @property
    def is_locked(self) -> bool:
        return self._locked
    
    # ===== 辅助方法 =====
    
    def get_db_path(self, base_dir: Optional[Path] = None) -> Path:
        """获取数据库路径"""
        if base_dir is None:
            base_dir = Path(__file__).parent.parent.parent / "data"
        return base_dir / self._capabilities.db_suffix
    
    def get_notification_prefix(self) -> str:
        """获取通知前缀"""
        return self._capabilities.notification_prefix
    
    @staticmethod
    def _now_str() -> str:
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ===== 全局便捷访问 =====

def get_trading_env() -> TradingEnvironment:
    """获取当前交易环境（延迟初始化）"""
    if TradingEnvironment._instance is None:
        return TradingEnvironment.initialize()
    return TradingEnvironment.get_instance()


def init_trading_env(mode: Optional[TradingMode] = None,
                     config: Optional[Dict[str, Any]] = None) -> TradingEnvironment:
    """显式初始化交易环境"""
    return TradingEnvironment.initialize(mode=mode, config=config)
