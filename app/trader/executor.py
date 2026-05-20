
def _get_us_exit_params():
    from app.risk.dynamic_exit_manager import dynamic_exit_manager
    return dynamic_exit_manager.get_market_params('us')
"""
交易执行器
对接富途 API 下单，支持港股/美股双市场，自动识别标的市场
"""
import logging
import threading
import time
from typing import Optional, Dict
from dataclasses import dataclass
from datetime import datetime

from app import config
from app.strategy.base import Signal, SignalAction, TradeTier
from app.trader.position_manager import PositionManager
from app.risk.risk_manager import get_risk_manager
from app.risk.dynamic_exit_manager import dynamic_exit_manager
from app.logs.failure_tracker import failure_tracker
from app.risk.circuit_breaker import circuit_breaker
from app.execution.smart_router import smart_router
from app.execution.account_router import select_account_for_code, is_option_code
from app.execution.order_retry import order_retry_manager, RetryReason
from app.risk.trend_filter import get_trend_filter
from app.risk.dynamic_position_sizer import get_dynamic_position_sizer
# P1-3: 执行质量监控器 (2026-04-06 新增)
from app.execution.execution_monitor import get_execution_monitor
from app.storage.database import ConnectionPool  # P0 修复：连接池
# P0 清理 (2026-04-26): 移除废弃的外部脚本依赖
# 归因系统 (2026-04-09 新增)
from app.analytics.strategy_health import get_strategy_health_tracker, TradeRecord as HealthTradeRecord

# P0 修复 (2026-04-12): 全局连接池，避免频繁创建/销毁连接
_trade_db_pool: Optional[ConnectionPool] = None
# 全局执行器实例 (供 BattlePlan 等外部模块调用)
_executor_instance: Optional['TradeExecutor'] = None


def _get_trade_db_pool() -> ConnectionPool:
    """获取交易数据库连接池单例"""
    global _trade_db_pool
    if _trade_db_pool is None:
        DB_PATH = str(config.DATA_DIR / "ftseek.db")
        _trade_db_pool = ConnectionPool(DB_PATH, pool_size=5, timeout=30.0)
    return _trade_db_pool

def get_executor() -> Optional['TradeExecutor']:
    """获取全局交易执行器实例"""
    return _executor_instance

def set_executor_instance(executor: 'TradeExecutor'):
    """设置全局交易执行器实例"""
    global _executor_instance
    _executor_instance = executor

# 富途常量 - 提前导入避免运行时错误
try:
    from futu import TrdSide, OrderType, TrdEnv
except ImportError:
    TrdSide = None
    OrderType = None
    TrdEnv = None

# 账户选择逻辑导入（用于港股多账户支持）
# 【P0 修复 2026-04-26】原实现硬编码外部路径，现已改为环境变量/配置中心读取
# 一旦该路径被重建或部署至其他账户家目录，模块即导入失败且 bare except 吞掉 → 账户选择静默降级。
# 改为：env var FTSEEK_OPENCLAW_SKILLS_DIR > unified_config.yaml: openclaw.skills_dir > 历史 fallback。
try:
    import sys
    import os

    def _resolve_openclaw_skills_dir() -> Optional[str]:
        # P0 清理 (2026-04-26): 彻底移除硬编码 fallback，强制使用环境变量或配置中心
        env_v = os.getenv("FTSEEK_OPENCLAW_SKILLS_DIR")
        if env_v and os.path.isdir(env_v):
            return env_v
        try:
            from config.config_center import get_config_center as _gcc
            _v = _gcc().get("openclaw.skills_dir")
            if _v and os.path.isdir(str(_v)):
                return str(_v)
        except Exception:
            pass
        return None

    _openclaw_dir = _resolve_openclaw_skills_dir()
    if _openclaw_dir:
        if _openclaw_dir not in sys.path:
            sys.path.insert(0, _openclaw_dir)
        from common import select_account_for_code
        ACCOUNT_SELECTION_AVAILABLE = True
        print(f"[账户选择] 模块加载成功 (skills_dir={_openclaw_dir})")
    else:
        ACCOUNT_SELECTION_AVAILABLE = False
        print("[账户选择] skills_dir 未配置且 fallback 路径不存在，将使用默认账户")
except Exception as e:
    ACCOUNT_SELECTION_AVAILABLE = False
    print(f"[账户选择] 模块加载失败，将使用默认账户：{e}")

logger = logging.getLogger("ftseek.trader.executor")

logger.propagate = True  # 允许日志传递到根日志器，以便写入文件


@dataclass
class TradeResult:
    """交易结果"""
    success: bool
    order_id: Optional[str] = None
    position_id: Optional[int] = None
    trade_type: Optional[str] = None  # OPEN/CLOSE
    pnl: Optional[float] = None
    message: str = ""
    close_ratio: Optional[float] = None  # 平仓比例 (部分平仓时使用)
    remaining_quantity: Optional[int] = None  # 剩余数量 (部分平仓时使用)


def _detect_market(code: str) -> str:
    """根据标的代码判断市场：HK / US"""
    if code.startswith("US."):
        return "US"
    return "HK"


def _is_option(code: str) -> bool:
    """
    判断是否为期权代码 (增强版，带详细日志)
    
    期权代码特征:
    - HK.POP260429C190000 (泡泡玛特 Call 190)
    - HK.TCH260429C510000 (腾讯 Call 510)
    - 格式：市场代码 + 底层资产 + 到期日 (6 位) + C/P + 行权价
    
    正股代码特征:
    - HK.09992 (5 位数字)
    - HK.00700 (5 位数字)
    """
    import re
    
    if not code or not isinstance(code, str):
        logger.warning(f"[期权判断] 无效代码：{code}")
        return False
    
    # 提取市场代码后的部分
    parts = code.split('.')
    if len(parts) < 2:
        logger.warning(f"[期权判断] 代码格式错误：{code}")
        return False
    
    symbol = parts[-1]
    logger.debug(f"[期权判断] 分析代码：{code}, symbol={symbol}")
    
    # 规则 1: 期权特征 - 包含 6 位日期 + C/P
    if re.search(r'\d{6}[CP]', symbol, re.IGNORECASE):
        logger.info(f"[期权判断] {code} -> 期权 (匹配日期+C/P)")
        return True
    
    # 规则 2: 正股特征 - 纯数字，通常 5 位
    if re.match(r'^\d{5}$', symbol):
        logger.info(f"[期权判断] {code} -> 正股 (5 位数字)")
        return False
    
    # 规则 3: 窝轮/牛熊证特征 - 5 位数字但包含字母
    if re.match(r'^\d{5}[A-Z]$', symbol, re.IGNORECASE):
        logger.info(f"[期权判断] {code} -> 窝轮/牛熊证")
        return False
    
    # 规则 4: 备用判断 - 包含 C 或 P 且长度>10
    if ('C' in symbol.upper() or 'P' in symbol.upper()) and len(symbol) > 10:
        logger.info(f"[期权判断] {code} -> 期权 (备用规则)")
        return True
    
    # 默认：记录日志并返回 False
    logger.warning(f"[期权判断] {code} 无法识别，默认视为正股")
    return False


def _resolve_account_ids() -> Dict[str, int]:
    """
    解析账户 ID 来源，优先级：env var > unified_config.yaml > 安全 fallback。

    【P0 修复 2026-04-26】原实现在 _get_account_for_code 内硬编码账户号，
    一旦换账户或切环境（线下模拟/线上模拟）需改源码。
    今改为：
      ① 环境变量 FTSEEK_HK_CASH_ACCOUNT_ID / FTSEEK_HK_OPTION_ACCOUNT_ID / FTSEEK_US_ACCOUNT_ID（最优先）
      ② unified_config.yaml: futu.simulate_account_id / futu.margin_account_id
      ③ Fallback：保持历史值避免破坏现产环境（Batch 5 核实后可在单独 commit 中移除）
    """
    def _to_int(v, default):
        try:
            return int(str(v).strip())
        except Exception:
            return default

    hk_cash_env = os.getenv("FTSEEK_HK_CASH_ACCOUNT_ID") or os.getenv("FTSEEK_HK_ACCOUNT_ID")
    hk_opt_env = os.getenv("FTSEEK_HK_OPTION_ACCOUNT_ID")
    us_env = os.getenv("FTSEEK_US_ACCOUNT_ID")

    hk_cash_yaml = None
    hk_opt_yaml = None
    us_yaml = None
    try:
        from config.config_center import get_config_center as _gcc
        _cc = _gcc()
        hk_cash_yaml = _cc.get("futu.simulate_account_id")
        hk_opt_yaml = _cc.get("futu.margin_account_id")
        us_yaml = _cc.get("futu.us_account_id")  # 新增可选
    except Exception as _e:
        logger.debug(f"[账户解析] config_center 读取失败: {_e}")

    return {
        "hk_cash": _to_int(hk_cash_env or hk_cash_yaml, 18820895),
        "hk_option": _to_int(hk_opt_env or hk_opt_yaml, 8246818),
        "us": _to_int(us_env or us_yaml, 0),  # 0 表示未配置
    }


# 模块级缓存（避免每次下单都读一遍）
_ACCOUNT_IDS_CACHE: Optional[Dict[str, int]] = None


def _get_account_for_code(code: str, trd_env) -> Optional[int]:
    """
    根据证券代码自动选择账户

    【P0 修复 2026-04-26】账户 ID 由 _resolve_account_ids() 统一解析，
    优先级：env > unified_config.yaml > 历史 fallback。

    Args:
        code: 证券代码
        trd_env: 交易环境

    Returns:
        账户ID，实盘返回None
    """
    from futu import TrdEnv

    # 实盘不指定账户
    if trd_env != TrdEnv.SIMULATE:
        return None

    global _ACCOUNT_IDS_CACHE
    if _ACCOUNT_IDS_CACHE is None:
        _ACCOUNT_IDS_CACHE = _resolve_account_ids()

    market = _detect_market(code)

    if market == "HK":
        if _is_option(code):
            _aid = _ACCOUNT_IDS_CACHE["hk_option"]
            logger.info(f"[账户选择] {code} -> 港股期权账户 {_aid}")
            return _aid
        else:
            _aid = _ACCOUNT_IDS_CACHE["hk_cash"]
            logger.info(f"[账户选择] {code} -> 港股正股账户 {_aid}")
            return _aid
    elif market == "US":
        _aid = _ACCOUNT_IDS_CACHE.get("us", 0)
        if _aid and _aid > 0:
            logger.info(f"[账户选择] {code} -> 美股账户 {_aid}")
            return _aid
        logger.warning(f"[账户选择] {code} -> 美股账户未配置 (设置 FTSEEK_US_ACCOUNT_ID 或 config.futu.us_account_id)")
        return None

    return None


class TradeExecutor:
    """富途交易执行器 (港股 + 美股双市场)"""

    def __init__(self, exit_manager=None):
        self._trd_ctx_hk = None
        self._trd_ctx_us = None
        self.position_manager = PositionManager.get_instance()
        self.risk_manager = get_risk_manager()
        self.exit_manager = exit_manager # Phase 1: 注入离场管理器
        # 幂等锁：防止网络抖动/重复回调导致重复下单
        self._order_lock = threading.Lock()
        self._pending_signals: Dict[str, float] = {}  # signal_hash → timestamp, TTL 30s
        # 滑点记录：(signal_price, dealt_avg_price, code)，最多保留 200 条，供健康度看板读取
        self._slippage_records: List[tuple] = []
        
        # 每手股数动态缓存 (code → lot_size), 避免重复 Futu API 调用
        self._lot_size_cache: Dict[str, int] = {}
        
        # S3: 水位管理单例注入 (2026-04-06)
        # 资金来源: app.main.py 中初始化，initial_capital=1000000 (100万)
        try:
            from app.factors.water_level_manager import get_water_level_manager, WaterLevel
            self._water_level_manager = get_water_level_manager()
            logger.info("[水位管理] 单例注入成功，当前水位等级可通过 get_current_level() 查询")
        except Exception as e:
            logger.warning(f"[水位管理] 单例注入失败，水位限制功能禁用: {e}")
            self._water_level_manager = None
        
        # 断点2修复: 止盈止损管理器单例注入 (2026-04-06)
        try:
            from app.factors.profit_loss_manager import get_profit_loss_manager
            self._profit_loss_manager = get_profit_loss_manager()
            logger.info("[止盈止损管理] 单例注入成功，三层止盈架构已激活")
        except Exception as e:
            logger.warning(f"[止盈止损管理] 单例注入失败，止盈止损增强功能禁用: {e}")
            self._profit_loss_manager = None
        
        # FutuMarginMonitor: 实盘保证金风控守卫 (2026-05-08)
        # 复用现有 futu_margin_monitor.py (v2.1)，包含滞回防抖/降级防护/压力测试
        try:
            from app.risk.futu_margin_monitor import FutuMarginMonitor
            from app.config import USE_MARGIN_GUARD
            
            if USE_MARGIN_GUARD:
                self.futu_margin_monitor = FutuMarginMonitor()
                # 注意：FutuMarginMonitor 需要在 main.py 中 initialize(trd_ctx, acc_id)
                # 这里仅保存引用，初始化延迟到 main.py
                logger.info("[FutuMarginMonitor] 守卫已注册 (等待 main.py 初始化)")
            else:
                self.futu_margin_monitor = None
                logger.info("[FutuMarginMonitor] 未启用 (USE_MARGIN_GUARD=False)")
        except Exception as e:
            logger.warning(f"[FutuMarginMonitor] 注册失败: {e}")
            self.futu_margin_monitor = None
        
        # 断点5修复: 持仓周期管理器单例注入 (2026-04-06)
        try:
            from app.factors.holding_period_manager import get_holding_period_manager
            self._holding_period_manager = get_holding_period_manager()
            logger.info("[持仓周期管理] 单例注入成功，质量分级持仓期限已激活")
        except Exception as e:
            logger.warning(f"[持仓周期管理] 单例注入失败，持仓周期管理功能禁用: {e}")
            self._holding_period_manager = None

        # P1 门禁链 (2026-05-19): 渐进式集成，保留 inline 逻辑作为 fallback
        # 配置开关：USE_GATE_CHAIN=true 时使用门禁链，false 时使用 inline 逻辑
        try:
            from app.trader.gates import GateChain, TimeGate, FeeGate, MaxPositionGate, LLMSignalImpactGate, MarketRegimeGate
            self._gate_chain = GateChain()
            # 按优先级添加门禁
            self._gate_chain.add_gate(MaxPositionGate(), priority=0)  # 最高优先级：持仓数
            self._gate_chain.add_gate(TimeGate(), priority=1)          # 时段检查
            self._gate_chain.add_gate(FeeGate(), priority=2)           # 费用检查
            self._gate_chain.add_gate(LLMSignalImpactGate(), priority=3)  # LLM 信号
            self._gate_chain.add_gate(MarketRegimeGate(), priority=4)  # 市场趋势
            logger.info(f"[门禁链] 已初始化，{len(self._gate_chain.get_gates())} 个门禁")
        except Exception as e:
            logger.warning(f"[门禁链] 初始化失败，降级使用 inline 逻辑：{e}")
            self._gate_chain = None

    def _get_trd_ctx(self, market: str):
        """获取交易上下文 (按市场懒加载)"""
        from futu import OpenSecTradeContext, TrdEnv, TrdMarket

        if market == "US":
            if self._trd_ctx_us is None:
                self._trd_ctx_us = OpenSecTradeContext(
                    host=config.OPEND_HOST,
                    port=config.OPEND_PORT,
                    filter_trdmarket=TrdMarket.US,
                    security_firm=None,
                )
                logger.info(f"美股交易上下文已创建，环境：{'模拟' if config.TRADE_ENV == 1 else '真实'}")
            return self._trd_ctx_us
        else:
            if self._trd_ctx_hk is None:
                self._trd_ctx_hk = OpenSecTradeContext(
                    host=config.OPEND_HOST,
                    port=config.OPEND_PORT,
                    filter_trdmarket=TrdMarket.HK,
                    security_firm=None,
                )
                logger.info(f"港股交易上下文已创建，环境：{'模拟' if config.TRADE_ENV == 1 else '真实'}")
            return self._trd_ctx_hk

    def _get_lot_size(self, code: str) -> int:
        """
        获取港股每手股数 (配置优先 + Futu API 兜底)
        
        2026-04-24: 解决硬编码 DEFAULT_ORDER_QTY_BY_CODE 不覆盖所有股票导致碎股下单失败的问题
        2026-05-08 P0 修复: 配置 lot_size 优先于 Futu API — API 可能返回错误值（如 01211 返回 100 实际 500）
        """
        if code in self._lot_size_cache:
            return self._lot_size_cache[code]
        
        # P0 修复: 配置优先 — unified_config.yaml / config/__init__.py 的 HK_LOT_SIZES 是 SSOT
        from app.config import HK_LOT_SIZES
        config_lot = HK_LOT_SIZES.get(code, 0)
        if config_lot > 0:
            logger.info(f"[每手股数] {code} → {config_lot} (配置优先: HK_LOT_SIZES)")
            self._lot_size_cache[code] = config_lot
            return config_lot
        
        # 配置未覆盖的股票，才走 Futu API 查询
        try:
            from futu import OpenQuoteContext, RET_OK
            
            quote_ctx = OpenQuoteContext(host=config.OPEND_HOST, port=config.OPEND_PORT)
            ret, data = quote_ctx.get_market_snapshot([code])
            quote_ctx.close()
            
            if ret == RET_OK and len(data) > 0:
                lot_size = int(data.iloc[0].get('lot_size', 0))
                if lot_size > 0:
                    logger.info(f"[每手股数] {code} → {lot_size} (Futu API 查询)")
                    self._lot_size_cache[code] = lot_size
                    return lot_size
            
            # API 返回无效值
            logger.warning(f"[每手股数] {code} Futu API 返回无效 lot_size，使用全局默认值")
            
        except Exception as e:
            logger.warning(f"[每手股数] {code} Futu API 查询失败: {e}，使用全局默认值")
        
        # 最终降级: 全局默认值
        lot_size = config.DEFAULT_ORDER_QTY_HK
        logger.info(f"[每手股数] {code} → {lot_size} (降级: 全局默认)")
        self._lot_size_cache[code] = lot_size
        return lot_size

    def _round_hk_price(self, price: float, action: str = 'BUY') -> float:
        """港股价格 tick 对齐（复用富途报价规则，避免下单 API 精度拒绝）"""
        if price < 0.25:
            tick = 0.001
        elif price < 0.50:
            tick = 0.005
        elif price < 10.00:
            tick = 0.01
        elif price < 20.00:
            tick = 0.02
        elif price < 100.00:
            tick = 0.05
        elif price < 200.00:
            tick = 0.10
        elif price < 500.00:
            tick = 0.20
        elif price < 1000.00:
            tick = 0.50
        elif price < 2000.00:
            tick = 1.00
        elif price < 5000.00:
            tick = 2.00
        else:
            tick = 5.00

        # 卖出：向下取整（更保守，避免挂单被拒）
        rounded = (price // tick) * tick

        if tick >= 1.0:
            return round(rounded, 0)
        elif tick >= 0.1:
            return round(rounded, 1)
        elif tick >= 0.01:
            return round(rounded, 2)
        else:
            return round(rounded, 3)

    def _get_qty(self, signal: Signal) -> int:
        """根据市场、股票和动态仓位计算下单数量 (避免碎股)"""
        market = _detect_market(signal.code)

        # ----------------------------------------------------------------
        # 动态仓位计算（基于置信度、市场状态、AI分数、波动率）
        # ----------------------------------------------------------------
        try:
            sizer = get_dynamic_position_sizer()
            extra = signal.extra or {}
            confidence = extra.get('confidence', 0.6)
            raw_regime = extra.get('market_regime', 'sideways')

            # 将共识引擎 market_regime 映射到 DynamicPositionSizer 期望的格式
            regime_map = {
                'trending': 'uptrend',
                'ranging': 'sideways',
                'neutral': 'sideways',
                'strong_trend_up': 'uptrend',
                'weak_trend_up': 'uptrend',
                'range_bound': 'sideways',
                'weak_trend_down': 'downtrend',
                'strong_trend_down': 'downtrend',
                'high_volatility': 'sideways',
                'low_volatility': 'sideways',
            }
            market_regime = regime_map.get(str(raw_regime), 'sideways')

            ai_score = extra.get('ai_score', 0.0)

            # 计算动态仓位比例（0.05~0.30）
            position_ratio = sizer.calculate_position(
                signal_confidence=confidence,
                market_regime=market_regime,
                ai_score=ai_score,
                volatility=0.0,
            )
        except Exception as e:
            logger.warning(f"[动态仓位] 计算失败，使用默认配置: {e}")
            position_ratio = None

        # ----------------------------------------------------------------
        # S3: 水位管理限制 (2026-04-06 新增)
        # 水位低时自动降低仓位或禁止交易
        # ----------------------------------------------------------------
        water_level_max_ratio = 1.0  # 默认不限制
        water_level_action = "NORMAL"
        
        if self._water_level_manager is not None:
            try:
                # 获取当前水位状态
                state = self._water_level_manager.get_state()
                current_level = state.current_level
                water_level_action = state.suggested_action
                
                # 记录水位等级变化日志
                if state.previous_level and state.previous_level != current_level:
                    logger.warning(
                        f"[水位变化] {state.previous_level.value} → {current_level.value} | "
                        f"建议操作: {water_level_action} | 当前水位: {state.current_ratio:.1%}"
                    )
                
                # 检查是否允许交易
                can_trade, reason = self._water_level_manager.can_trade()
                if not can_trade:
                    logger.warning(f"[水位限制] {signal.code} 交易被禁止: {reason}")
                    return 0  # 返回 0 表示禁止交易
                
                # 获取最大仓位比例
                water_level_max_ratio = self._water_level_manager.get_max_position()
                
                # 如果水位限制比动态仓位更严格，应用水位限制
                if position_ratio is not None and position_ratio > water_level_max_ratio:
                    original_ratio = position_ratio
                    position_ratio = water_level_max_ratio
                    logger.info(
                        f"[水位限制] {signal.code} 动态仓位 {original_ratio:.1%} → "
                        f"水位限制 {water_level_max_ratio:.1%} (水位等级: {current_level.value})"
                    )
                
            except Exception as e:
                logger.warning(f"[水位管理] 获取水位限制失败，跳过: {e}")
        
        # P5 fix (2026-04-26): Kelly 公式仓位计算 — 基于策略历史胜率/盈亏比
        try:
            from app.strategy.consensus_engine import _adaptive_weight_manager
            if hasattr(_adaptive_weight_manager, 'get_kelly_stats'):
                agree_strategies = list(extra.get('strategies', [])) if extra else []
                if not agree_strategies and hasattr(signal, 'strategy'):
                    s = signal.strategy if isinstance(signal.strategy, str) else str(signal.strategy)
                    agree_strategies = [s]
                mr = extra.get('market_regime', '') if extra else ''
                kelly_stats = _adaptive_weight_manager.get_kelly_stats(agree_strategies, mr)
                if kelly_stats and kelly_stats['half_kelly'] > 0:
                    kelly_ratio = kelly_stats['half_kelly']
                    if position_ratio is not None:
                        # Blend: 60% Kelly + 40% heuristic
                        position_ratio = kelly_ratio * 0.6 + position_ratio * 0.4
                    else:
                        position_ratio = kelly_ratio
                    logger.info(
                        f"[Kelly仓位] {signal.code} 策略={agree_strategies} "
                        f"胜率={kelly_stats['win_rate']:.0%} "
                        f"盈亏比={kelly_stats['reward_risk']:.1f} "
                        f"Kelly={kelly_stats['kelly_fraction']:.1%} "
                        f"半Kelly={kelly_ratio:.1%} "
                        f"(样本={kelly_stats['total_trades']}笔)"
                    )
        except Exception as _kelly_e:
            logger.debug(f"[Kelly仓位] 计算跳过: {_kelly_e}")

        # 港股：根据股票配置每手股数
        if market == "HK":
            # 基础每手股数 (动态获取)
            base_qty = self._get_lot_size(signal.code)

            # 如果动态仓位计算成功，按比例调整倍数（以 0.10 基准比例对应 1 手）
            if position_ratio is not None:
                base_ratio = 0.10  # 基础仓位对应 1 手
                multiplier = max(1, round(position_ratio / base_ratio))
                qty = base_qty * multiplier
                
                # 断点4修复: 质量仓位系数 (2026-04-06)
                # Class A: 1.3x, Class B: 1.0x, Class C: 0.7x
                quality_boost = extra.get('quality_boost', 1.0)
                if quality_boost != 1.0:
                    qty = max(base_qty, int(qty * quality_boost))
                    qty = (qty // base_qty) * base_qty or base_qty
                    logger.info(
                        f"[质量仓位] {signal.code} 质量加成={quality_boost:.1f}x → 调整后={qty}"
                    )
                
                # 应用新闻情绪倍率（由 data_subscriber 注入 extra['news_boost']）
                news_boost = extra.get('news_boost', 1.0)
                if news_boost != 1.0:
                    raw_qty = qty
                    qty = max(base_qty, int(qty * news_boost))
                    # 对齐整手
                    qty = (qty // base_qty) * base_qty or base_qty
                    logger.info(
                        f"[动态仓位] {signal.code} 置信度={confidence:.2f} "
                        f"市场={market_regime} 仓位={position_ratio:.1%} "
                        f"基础={base_qty} 倍数={multiplier}x 新闻={news_boost:.1f}x "
                        f"原={raw_qty} 最终={qty}"
                    )
                else:
                    logger.info(
                        f"[动态仓位] {signal.code} 置信度={confidence:.2f} "
                        f"市场={market_regime} 仓位={position_ratio:.1%} "
                        f"基础={base_qty} 倍数={multiplier}x 最终={qty}"
                    )

                # 2026-05-11: 应用智能熔断仓位倍率 (CircuitBreaker -> SignalContext -> executor)
                position_multiplier = extra.get('position_multiplier', 1.0)
                if position_multiplier != 1.0:
                    raw_qty = qty
                    qty = max(base_qty, int(qty * position_multiplier))
                    qty = (qty // base_qty) * base_qty or base_qty
                    logger.info(
                        f"[风控仓位] {signal.code} 熔断倍率={position_multiplier:.1f}x "
                        f"原={raw_qty} 调整后={qty}"
                    )

                return qty

            # 优先使用信号中的数量 (2026-04-15 修复: 增加整手校验)
            if signal.quantity and signal.quantity > 0:
                # 检查是否为整手倍数
                if signal.quantity % base_qty == 0:
                    return signal.quantity
                else:
                    # 不是整手倍数，向下对齐
                    aligned_qty = (signal.quantity // base_qty) * base_qty
                    if aligned_qty >= base_qty:
                        logger.warning(
                            f"[碎股防护] {signal.code} signal.quantity={signal.quantity} 非整手倍数，"
                            f"向下对齐到 {aligned_qty} (每手 {base_qty} 股)"
                        )
                        return aligned_qty
                    else:
                        logger.warning(
                            f"[碎股防护] {signal.code} signal.quantity={signal.quantity} 不足 1 手，"
                            f"使用基础数量 {base_qty}"
                        )
                        return base_qty

            return base_qty

        # ============================================================
        # 美股 (US) - 最小交易单位 1 股，无碎股问题
        # ============================================================
        if market == "US":
            # 美股最小交易单位: 1 股
            US_MIN_QTY = 1
            
            if position_ratio is not None:
                base_qty = config.DEFAULT_ORDER_QTY_US  # 默认 1 股
                base_ratio = 0.10  # 基础仓位对应 1 股
                multiplier = max(1, round(position_ratio / base_ratio))
                qty = max(US_MIN_QTY, base_qty * multiplier)

                # 2026-05-11: 应用智能熔断仓位倍率
                position_multiplier = extra.get('position_multiplier', 1.0)
                if position_multiplier != 1.0:
                    qty = max(US_MIN_QTY, int(qty * position_multiplier))
                    logger.info(
                        f"[风控仓位] {signal.code}(US) 熔断倍率={position_multiplier:.1f}x → 调整后={qty}"
                    )

                logger.info(
                    f"[动态仓位] {signal.code}(US) 置信度={confidence:.2f} "
                    f"市场={market_regime} 仓位={position_ratio:.1%} "
                    f"基础={base_qty} 倍数={multiplier}x 最终={qty} (美股最小单位: 1股)"
                )
                return qty

            # 优先使用信号中的数量 (2026-04-15 修复: 确保 >= 1)
            if signal.quantity and signal.quantity > 0:
                qty = max(US_MIN_QTY, int(signal.quantity))
                if qty != signal.quantity:
                    logger.warning(
                        f"[美股数量修正] {signal.code} signal.quantity={signal.quantity} → {qty} (最小 1 股)"
                    )
                return qty

            return config.DEFAULT_ORDER_QTY_US  # 默认 1 股
    
    def _execute_order_with_retry(self, trd_ctx, code: str, price: float, qty: int,
                                  trd_side, order_type, trd_env, signal) -> tuple:
        """
        执行订单并支持重试 (2026-03-17 新增)
        
        Args:
            trd_ctx: 交易上下文
            code: 股票代码
            price: 价格
            qty: 数量
            trd_side: 买卖方向
            order_type: 订单类型
            trd_env: 交易环境
            signal: 交易信号
        
        Returns:
            (ret, data) 元组
        """
        # 生成临时订单 ID 用于重试追踪
        import uuid
        temp_order_id = str(uuid.uuid4())[:8]
        
        # 根据证券类型自动选择账户 (2026-03-31: 期权/正股双账户支持)
        acc_id = _get_account_for_code(code, trd_env)
        ret, data = trd_ctx.place_order(
            price=price,
            qty=qty,
            code=code,
            trd_side=trd_side,
            order_type=order_type,
            trd_env=trd_env,
            acc_id=acc_id,
        )
        
        # 如果失败且可以重试
        if ret != 0 and order_retry_manager.should_retry(temp_order_id):
            retry_count = order_retry_manager.get_retry_count(temp_order_id)
            logger.warning(f"订单失败，准备重试 (第{retry_count + 1}次): {data}")
            
            # 计算新价格 (防御修复: 最大追价幅度不超过 1%)
            action = 'BUY' if trd_side == TrdSide.BUY else 'SELL'
            new_price = order_retry_manager.get_next_price(
                temp_order_id, code, action, price,
                'MARKET' if order_type == OrderType.MARKET else 'LIMIT'
            )
            # 2026-05-11 防御修复: 订单重试价格保护，最大追价幅度不超过 1%
            if action == 'BUY':
                max_allowed_price = price * 1.01
                if new_price > max_allowed_price:
                    logger.warning(
                        f"[订单重试价格保护] {code} 追价 {new_price:.2f} > 上限 {max_allowed_price:.2f} "
                        f"(原价格 {price:.2f} 的 1.01x)，强制限制"
                    )
                    new_price = max_allowed_price
            else:  # SELL
                min_allowed_price = price * 0.99
                if new_price < min_allowed_price:
                    logger.warning(
                        f"[订单重试价格保护] {code} 压价 {new_price:.2f} < 下限 {min_allowed_price:.2f} "
                        f"(原价格 {price:.2f} 的 0.99x)，强制限制"
                    )
                    new_price = min_allowed_price
            
            # 记录重试
            order_retry_manager.record_retry(
                temp_order_id, code, action,
                RetryReason.MARKET_FAILED if order_type == OrderType.MARKET else RetryReason.LIMIT_NOT_FILLED,
                price, new_price, False, str(data)
            )
            
            # 重试下单
            logger.info(f"重试下单：价格={new_price:.2f}")
            ret, data = trd_ctx.place_order(
                price=new_price,
                qty=qty,
                code=code,
                trd_side=trd_side,
                order_type=OrderType.NORMAL,  # 重试时使用限价单
                trd_env=trd_env,
                acc_id=acc_id,
            )
            
            # 记录重试结果
            order_retry_manager.record_retry(
                temp_order_id, code, action,
                RetryReason.MARKET_FAILED,
                price, new_price, ret == 0, str(data) if ret != 0 else "成功"
            )
        
        return ret, data

    def _signal_hash(self, code: str, action, price: float) -> str:
        """生成信号哈希，用于幂等去重。价格对齐到最近tick(0.02)避免微小浮动造成误判"""
        action_str = action.value if hasattr(action, 'value') else str(action)
        price_band = round(price / 0.02) * 0.02
        return f"{code}:{action_str}:{price_band:.3f}"

    def _record_gate_reject(self, signal: Signal, gate_name: str, reason: str,
                            final_score: float = None, shadow_mode: bool = False):
        """记录门禁拦截数据到 blocked_decisions 表 (P2 2026-05-17 新增)

        与 failure_tracker.record_failure 互补: failure_tracker 用于统计，
        本方法用于结构化数据分析 (final_score 分布、gate hit rate 等)。
        """
        try:
            from app import config
            db_path = str(config.DATA_DIR / "ftseek.db")
            import sqlite3
            conn = sqlite3.connect(db_path)
            extra = getattr(signal, 'extra', {}) or {}
            conn.execute(
                "INSERT INTO blocked_decisions "
                "(code, reject_check, reject_code, reject_reason, final_score, "
                " gate_name, signal_impact, shadow_mode, raw_score) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    signal.code,
                    "executor_gate",
                    gate_name,
                    reason[:200],
                    final_score,
                    gate_name,
                    extra.get('signal_impact'),
                    1 if shadow_mode else 0,
                    extra.get('raw_score', signal.score if hasattr(signal, 'score') else None),
                )
            )
            conn.commit()
            conn.close()
        except Exception as _ge:
            logger.debug(f"[Gate数据记录] 写入失败: {_ge}")

    def execute(self, signal: Signal, trade_type: str = 'AUTO') -> TradeResult:
        """
        执行交易信号 (集成熔断器 + 失败追踪 + 自动识别开仓/平仓)
        :param signal: 交易信号
        :param trade_type: AUTO(自动识别) / OPEN(开仓) / CLOSE(平仓)
        :return: TradeResult {success, order_id, position_id, trade_type, pnl}
        """
        if signal.action == SignalAction.ALERT:
            return TradeResult(success=False, message="ALERT 类型信号不执行交易")

        # P0 修复 (2026-05-19): _is_sim 必须在方法开头定义，避免后续门禁检查时 NameError
        # 根因: L841/L880 使用 _is_sim，但原定义在 L1015，导致熔断器/性能衰减触发时崩溃
        _is_sim = getattr(config, 'TRADE_ENV', 1) == 1

        # Phase 3: 交易分层 - Tier2/Tier3 影子盘拦截
        if signal.tier != TradeTier.TIER_TREND:
            try:
                from app.executor.shadow_executor import ShadowExecutor, ShadowTradeRecord
                executor = ShadowExecutor.get_instance()
                
                # 默认短线参数
                stop_loss = signal.tier_stop_loss if signal.tier_stop_loss > 0 else 0.015
                take_profit = signal.tier_take_profit if signal.tier_take_profit > 0 else 0.03
                max_hold = signal.tier_max_hold_minutes if signal.tier_max_hold_minutes > 0 else 120
                
                record = ShadowTradeRecord(
                    trace_id=signal.trace_id,
                    code=signal.code,
                    price=signal.price,
                    qty=signal.quantity,
                    tier_stop_loss=stop_loss,
                    tier_take_profit=take_profit,
                    max_hold_min=max_hold
                )
                executor.add_trade(record)
                
                logger.info(f"[影子盘] Tier2 信号已拦截并模拟入场: {signal.code} @ {signal.price:.2f}")
                return TradeResult(success=True, message="shadow_tier2_executed", trade_type='SHADOW')
            except Exception as e:
                logger.error(f"[影子盘] 模拟执行失败: {e}")
                return TradeResult(success=False, message=f"shadow_executor_error: {e}")

        # 熔断器检查 (2026-03-17 新增)
        if not circuit_breaker.can_trade():
            failure_tracker.record_failure(signal.code, 'circuit_breaker', '熔断暂停中')
            self._record_gate_reject(signal, 'circuit_breaker', '熔断暂停中', shadow_mode=_is_sim)
            return TradeResult(success=False, message="熔断器触发，暂停交易")

        # P1 门禁链检查 (2026-05-19): 渐进式集成，与 inline 逻辑双跑
        # 配置开关：USE_GATE_CHAIN=true 时启用门禁链，false 时使用 inline 逻辑
        if self._gate_chain is not None and getattr(config, 'USE_GATE_CHAIN', False):
            try:
                # 构建门禁链上下文
                gate_context = {
                    'config': config,
                    'config_center': None,  # 将在门禁内部懒加载
                    'position_manager': self.position_manager,
                    'logger': logger,
                    'failure_tracker': failure_tracker,
                    'is_sim': _is_sim,
                    'get_lot_size_fn': self._get_lot_size,
                }
                # 尝试获取 config_center
                try:
                    from config.config_center import get_config_center
                    gate_context['config_center'] = get_config_center()
                except Exception:
                    pass

                # 执行门禁链检查
                passed, reason = self._gate_chain.check_all(signal, gate_context)
                if not passed:
                    logger.info(f"[门禁链] {signal.code} 拦截：{reason}")
                    self._record_gate_reject(signal, 'gate_chain', reason, shadow_mode=_is_sim)
                    failure_tracker.record_failure(signal.code, 'gate_chain', reason)
                    return TradeResult(success=False, message=reason)
                else:
                    logger.debug(f"[门禁链] {signal.code} 全部通过")
            except Exception as _gc_e:
                # 异常隔离：门禁链失败时降级使用 inline 逻辑
                logger.warning(f"[门禁链] 检查异常，降级使用 inline 逻辑：{_gc_e}")

        # 新闻增强检查 (2026-04-08 新增)
        # 仅对开仓信号执行新闻增强（平仓/风控信号直接执行）
        if trade_type != 'CLOSE':
            # 构造信号字典
            signal_dict = {
                'stock_code': signal.code,
                'action': 'buy' if signal.action == SignalAction.BUY else 'sell',
                'confidence': signal.confidence if hasattr(signal, 'confidence') else 0.5,
                'strategy': signal.strategy if hasattr(signal, 'strategy') else 'unknown'
            }
            
            # P0 清理 (2026-04-26): 移除废弃的新闻增强逻辑
            # 该模块依赖的外部脚本已不可用，且已有降级处理，此处直接移除死代码
            # 原逻辑: check_news_block -> 阻止交易 / enhance_signal -> 调整置信度
            
            # 原 L711: Phase 3 新增：性能衰减检查 (Performance Decay)
        # 根据近期策略胜率动态调整信号强度，防止“连败”期间继续重仓
        try:
            from app.utils.performance_decay import get_decay_factor
            import os
            # 获取 DB 路径 (executor.py 在 app/trader/，需上移 2 级到项目根目录)
            db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'ftseek.db')
            
            strategy = signal.strategy if hasattr(signal, 'strategy') else 'unknown'
            decay = get_decay_factor(db_path, strategy, lookback_days=5)
            
            if decay < 1.0:
                # 应用衰减到信号置信度/数量
                original_conf = signal.confidence if hasattr(signal, 'confidence') else 0.5
                signal.confidence = max(0.1, original_conf * decay)
                logger.warning(f"📉 [性能衰减] 策略 {strategy} 近期表现不佳 -> 信号降权 (x{decay})")
                
                # 如果衰减后置信度过低，直接拦截
                if signal.confidence < 0.3:
                    logger.warning(f"🚫 [性能衰减] {signal.code} 信号降权后过低 ({signal.confidence:.2f})，自动拦截")
                    failure_tracker.record_failure(signal.code, 'performance_decay', '信号降权后置信度过低')
                    self._record_gate_reject(signal, 'performance_decay', '信号降权后置信度过低', shadow_mode=_is_sim)
                    return TradeResult(success=False, message="性能衰减拦截")
        except Exception as e:
            logger.debug(f"[性能衰减] 检查失败 (不影响主流程): {e}")

        # 幂等检查：防止同一信号（code+方向+价格档位）在30s内重复下单
        # 2026-03-27 修复: 平仓/止损/止盈信号无条件跳过幂等检查，风控信号必须立即执行
        # 优先检查 trade_type 参数：CLOSE 类型直接跳过（最根本的修复，防止11000+次duplicate_signal）
        if trade_type == 'CLOSE':
            logger.info(f"[平仓信号] {signal.code} {signal.strategy} 跳过幂等检查，立即执行")
        else:
            # 仅对开仓信号(OPEN/AUTO)执行幂等去重
            is_risk_signal = (
                signal.strategy in ("止损", "止盈", "移动止损", "保本止损", "时间止损", "硬止损") or
                signal.extra.get("stop_loss") or
                signal.extra.get("take_profit") or
                signal.extra.get("trailing_stop")
            )
            if not is_risk_signal:
                sig_hash = self._signal_hash(signal.code, signal.action, signal.price)
                with self._order_lock:
                    now = time.time()
                    if sig_hash in self._pending_signals:
                        if now - self._pending_signals[sig_hash] < 30:
                            logger.warning(f"[幂等拦截] {sig_hash} 30s内重复信号，跳过")
                            return TradeResult(success=False, message="duplicate_signal")
                    self._pending_signals[sig_hash] = now
                    # 清理超过60s的过期记录
                    self._pending_signals = {k: v for k, v in self._pending_signals.items() if now - v < 60}
            else:
                logger.info(f"[风控信号] {signal.code} {signal.strategy} 跳过幂等检查，立即执行")

        # 自动识别开仓/平仓 (2026-03-20 新增)
        # 2026-04-15 优化: 增强补仓规则 (金字塔 + 行业集中度 + LLM信号)
        if trade_type == 'AUTO':
            position = self.position_manager.get_position(signal.code)
            if position and position.status == 'OPEN':
                if position.action == 'BUY' and signal.action == SignalAction.SELL:
                    trade_type = 'CLOSE'
                    logger.info(f"[自动识别] {signal.code} 多头持仓存在，SELL 信号转为平仓")
                elif position.action == 'SELL' and signal.action == SignalAction.BUY:
                    trade_type = 'CLOSE'
                    logger.info(f"[自动识别] {signal.code} 空头持仓存在，BUY 信号转为平仓")
                elif position.action == 'BUY' and signal.action == SignalAction.BUY:
                    # 2026-04-15 优化: 增强补仓规则
                    current_price = signal.price
                    entry_price = position.entry_price
                    pnl_pct = (current_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
                    
                    # 读取补仓配置
                    profit_threshold = getattr(config, 'ADD_POSITION_PROFIT_THRESHOLD', 5.0)
                    loss_threshold = getattr(config, 'ADD_POSITION_LOSS_THRESHOLD', -2.0)
                    max_add = getattr(config, 'MAX_ADD_PER_POSITION', 2)
                    
                    # 检查补仓次数
                    try:
                        add_count = self.position_manager.get_today_add_count(signal.code)
                    except Exception:
                        add_count = 0
                    
                    # 规则 1: 亏损超过阈值 → 拒绝补仓 (止损优先)
                    if pnl_pct <= loss_threshold:
                        logger.info(
                            f"[补仓拒绝] {signal.code} 亏损 {pnl_pct:+.1f}% < {loss_threshold}%，"
                            f"由止损引擎处理"
                        )
                        return TradeResult(
                            success=False,
                            message=f"loss_too_deep ({pnl_pct:+.1f}%)"
                        )
                    
                    # 规则 2: 盈利超过阈值 → 谨慎补仓 (需强信号)
                    if pnl_pct >= profit_threshold:
                        # 检查是否为 LLM 强信号 (STRONG_BUY + HIGH confidence)
                        is_strong = (
                            signal.extra.get('llm_direction') == 'STRONG_BUY' and
                            signal.extra.get('llm_confidence') == 'HIGH'
                        ) if signal.extra else False
                        
                        if is_strong:
                            logger.info(
                                f"[补仓谨慎] {signal.code} 盈利 {pnl_pct:+.1f}% >= {profit_threshold}%，"
                                f"但 LLM 强信号确认，允许金字塔补仓"
                            )
                        else:
                            logger.info(
                                f"[补仓拒绝] {signal.code} 盈利 {pnl_pct:+.1f}% >= {profit_threshold}%，"
                                f"非 LLM 强信号，优先止盈"
                            )
                            return TradeResult(
                                success=False,
                                message=f"profit_too_high_no_strong_signal ({pnl_pct:+.1f}%)"
                            )
                    
                    # 规则 3: 补仓次数检查
                    if add_count >= max_add:
                        logger.warning(
                            f"[补仓拒绝] {signal.code} 今日已补仓 {add_count}/{max_add} 次"
                        )
                        return TradeResult(
                            success=False,
                            message=f"add_position_limit_reached ({add_count}/{max_add})"
                        )
                    
                    # 规则 4: 行业集中度检查 (新增)
                    try:
                        sector_ok, sector_reason = self._check_sector_concentration(signal.code)
                        if not sector_ok:
                            logger.warning(f"[行业集中度] {signal.code} {sector_reason}")
                            return TradeResult(success=False, message=sector_reason)
                    except Exception as e:
                        logger.debug(f"行业集中度检查失败 (非阻塞): {e}")
                    
                    # 通过所有检查 → 允许补仓 (金字塔量)
                    trade_type = 'ADD'
                    add_multipliers = getattr(config, 'MAX_ADD_MULTIPLIER', [1.0, 0.5, 0.25])
                    multiplier = add_multipliers[min(add_count, len(add_multipliers)-1)]
                    logger.info(
                        f"[补仓批准] {signal.code} 盈亏 {pnl_pct:+.1f}% | "
                        f"补仓次数 {add_count}/{max_add} | "
                        f"金字塔系数 {multiplier}"
                    )
                else:
                    trade_type = 'OPEN'
            else:
                # 无持仓：FTSeek 为纯多头策略，忽略 SELL 信号
                if signal.action == SignalAction.SELL:
                    logger.info(f"[纯多头策略] {signal.code} 无持仓时忽略 SELL 信号 (策略设计为纯多头)")
                    return TradeResult(success=False, message="pure_long_strategy_no_position")
                trade_type = 'OPEN'

        # 风控检查 (仅开仓需要)
        if trade_type == 'OPEN':
            # ── 模拟盘 Shadow 模式检测 (2026-05-17) ──
            # 模拟盘目标: 最大化数据收集，门禁改为告警+标签而非硬拦截
            _is_sim = getattr(config, 'TRADE_ENV', 1) == 1
            _shadow_mode = _is_sim  # 模拟盘默认 shadow 模式

            # ── P0 LLM signal_impact 门禁 (2026-05-17) ──
            # LLM 在盘前分析中对标的给出 signal_impact (boost/neutral/weaken/block)
            # 优先从动态池 metadata 读取，其次从 signal.extra 读取 (battle_plan 路径)
            try:
                from app.utils.daily_active_pool import get_daily_active_pool
                _impact = None
                _urgency = "Medium"
                
                # 路径 1: 动态池 metadata
                _pool = get_daily_active_pool(market_prefix="HK.")
                
                # P1 修复 (2026-05-17): 前缀归一化查找 (兼容 "00700" vs "HK.00700")
                _entry = _pool._entries.get(signal.code)
                if not _entry and not signal.code.startswith("HK."):
                    _entry = _pool._entries.get(f"HK.{signal.code}")
                
                if _entry and not _entry.is_expired():
                    _impact = _entry.metadata.get("signal_impact", "neutral")
                    _urgency = _entry.metadata.get("urgency", "Medium")
                
                # 路径 2: signal.extra (battle_plan 等绕过动态池的路径)
                if _impact is None or _impact == "neutral":
                    _extra_impact = (signal.extra or {}).get("signal_impact")
                    if _extra_impact:
                        _impact = _extra_impact
                        _urgency = (signal.extra or {}).get("urgency", "Medium")
                
                if _impact and _impact != "neutral":
                    signal.extra = signal.extra or {}
                    signal.extra["llm_signal_impact"] = _impact
                    signal.extra["llm_urgency"] = _urgency

                    if _impact == "block":
                        if _is_sim:
                            logger.warning(
                                f"[LLM门禁·影子] {signal.code} LLM建议回避(block)，"
                                f"模拟盘放行+打标签 (urgency={_urgency})"
                            )
                            signal.extra['llm_block_override'] = True
                        else:
                            _reason = f"LLM 信号 block: {_entry.metadata.get('logic', '政策/风险因素') if _entry else 'LLM判断风险'}"[:100]
                            logger.warning(f"[LLM门禁·硬拦截] {signal.code} {_reason}")
                            failure_tracker.record_failure(signal.code, 'llm_block', _reason)
                            self._record_gate_reject(signal, 'llm_block', _reason, shadow_mode=_is_sim)
                            return TradeResult(success=False, message=f"LLM信号block: {_reason}")

                    elif _impact == "weaken":
                        logger.info(f"[LLM门禁·降权] {signal.code} signal_impact=weaken, 要求更高阈值")
                        signal.extra['llm_weaken_applied'] = True
                    elif _impact == "boost":
                        logger.info(f"[LLM门禁·增强] {signal.code} signal_impact=boost (urgency={_urgency})")
                        signal.extra['llm_boost_applied'] = True
            except Exception as _llm_gate_e:
                logger.debug(f"[LLM门禁] 检查跳过: {_llm_gate_e}")

            # ── P1 仓位费用感知门禁 (2026-05-17) ──
            # 根因: 港股费用 ~140bps，小仓位交易被费用严重侵蚀
            # HK$3K 交易: 费用占比 1.4% → 实际 R:R 从 1.43:1 降至 0.4:1
            # HK$8K+ 交易: 费用占比 <0.6% → R:R 可接受
            # 注意: 此门禁放在 execute() 的顶层 OPEN 块中，确保 _is_sim 在作用域内
            try:
                from config.config_center import get_config_center as _gcc_mtv
                _min_tv = _gcc_mtv().get('base.simulation_mode.min_trade_value', 8000)
                _est_value = signal.price * self._get_lot_size(signal.code) if signal.price > 0 else 0
                if 0 < _est_value < _min_tv:
                    if _is_sim:  # 模拟盘: 告警但放行
                        logger.info(f"[模拟门禁·费用] {signal.code} 预估金额 HK${_est_value:,.0f} < HK${_min_tv:,}，"
                                   f"费用侵蚀严重但模拟盘放行 (数据收集)")
                        signal.extra = signal.extra or {}
                        signal.extra['fee_warning'] = f'trade_value_below_min ({_est_value:.0f}<{_min_tv})'
                    else:  # 实盘: 硬拦截
                        msg = f"交易金额过小: HK${_est_value:,.0f} < HK${_min_tv:,}，费用侵蚀严重"
                        logger.info(f"[费用门禁] {signal.code} {msg}")
                        failure_tracker.record_failure(signal.code, 'min_trade_value', msg)
                        self._record_gate_reject(signal, 'min_trade_value', msg, shadow_mode=_is_sim)
                        return TradeResult(success=False, message=msg)
            except Exception as _mtv_e:
                logger.debug(f"[费用门禁] 检查跳过: {_mtv_e}")

            # ── P0 时段门禁 (2026-05-16): 禁止 14:30 后开新仓 ──
            # 根因: 历史数据 40% 持平交易因 14:30+ 开仓 → 15:45 盘末退出强平
            # 优化 (2026-05-17): 模拟盘 shadow 模式 — 14:30-15:30 告警放行，15:30+ 硬拦截
            try:
                from datetime import datetime
                import pytz
                tg_cfg = getattr(config, 'TIME_GATE', {'enabled': True, 'hk_latest_open': '14:30'})
                if tg_cfg.get('enabled', True):
                    latest_str = tg_cfg.get('hk_latest_open', '14:30')
                    latest_h, latest_m = map(int, latest_str.split(':'))
                    latest_min = latest_h * 60 + latest_m
                    hk_now = datetime.now(pytz.timezone('Asia/Hong_Kong'))
                    hk_minutes = hk_now.hour * 60 + hk_now.minute

                    # ── 半日市下午拦截 (2026-05-17 新增) ──
                    # 半日市仅上午交易 (09:30-12:00)，下午完全休市
                    try:
                        from app.utils.holiday_calendar import get_holiday_calendar
                        from datetime import date as _date
                        cal = get_holiday_calendar()
                        hk_today = _date(hk_now.year, hk_now.month, hk_now.day)
                        if cal.is_hk_half_day(hk_today) and hk_minutes >= 12 * 60:
                            msg = f"半日市下午休市: {hk_now.strftime('%Y-%m-%d')} 12:00 后禁止开仓"
                            logger.info(f"[半日市门禁] {signal.code} {msg}")
                            failure_tracker.record_failure(signal.code, 'half_day_closed', msg)
                            self._record_gate_reject(signal, 'half_day', msg, shadow_mode=_is_sim)
                            return TradeResult(success=False, message=msg)
                    except Exception as _hd_err:
                        logger.debug(f"[半日市门禁] 检查跳过: {_hd_err}")

                    if hk_minutes >= latest_min:
                        if _shadow_mode and hk_minutes < 15 * 60 + 30:
                            # 模拟盘 14:30-15:30: 告警但放行，收集数据
                            msg = f"时段门禁告警(影子): {hk_now.strftime('%H:%M')} ≥ {latest_str}，模拟盘放行 (数据收集)"
                            logger.info(f"[模拟门禁·影子] {signal.code} {msg}")
                            signal.extra = signal.extra or {}
                            signal.extra['gate_warning'] = 'time_near_close'
                        elif hk_minutes >= 15 * 60 + 30:
                            # 15:30+ 硬拦截: 距离盘末退出仅 15min，不值得
                            msg = f"时段门禁: 港股 {hk_now.strftime('%H:%M')} ≥ 15:30，禁止开新仓 (盘末退出前不足15min)"
                            logger.info(f"[时段门禁] {signal.code} {msg}")
                            failure_tracker.record_failure(signal.code, 'time_gate', msg)
                            self._record_gate_reject(signal, 'time_gate', msg, shadow_mode=_is_sim)
                            return TradeResult(success=False, message=msg)
                        else:
                            # 实盘: 硬拦截
                            msg = f"时段门禁: 港股 {hk_now.strftime('%H:%M')} ≥ {latest_str}，禁止开新仓 (防盘末退出强平)"
                            logger.info(f"[时段门禁] {signal.code} {msg}")
                            failure_tracker.record_failure(signal.code, 'time_gate', msg)
                            self._record_gate_reject(signal, 'time_gate', msg, shadow_mode=_is_sim)
                            return TradeResult(success=False, message=msg)
            except Exception as _tg_e:
                logger.debug(f"[时段门禁] 检查跳过: {_tg_e}")

            # ── P0 市场趋势门禁 (2026-05-16): 恒指下跌趋势中禁止开新仓 ──
            # 根因: 恒指 MA5 < MA20 时，个股做多信号多为假突破，逆市做多胜率低
            # 修复 (2026-05-17): ① HSI数据缓存(5min TTL)避免重复连接 ② 模拟盘shadow模式 ③ gap_pct阈值防横盘误杀
            try:
                import time as _time
                _mr_cfg = getattr(config, 'MARKET_REGIME_GATE', {
                    'enabled': True,
                    'hsi_gap_min_pct': 0.3,  # MA5偏离MA20的最小幅度，低于此视为横盘
                })
                if _mr_cfg.get('enabled', True):
                    from app.utils.futu_limiter import get_global_limiter

                    # ── HSI 数据缓存 (5分钟 TTL) ──
                    # 避免每次信号检查都新建 OpenQuoteContext 消耗槽位
                    if not hasattr(_check_trade_gates, '_hsi_cache'):
                        _check_trade_gates._hsi_cache = {'data': None, 'ma5': None, 'ma20': None, 'ts': 0}
                    _cache = _check_trade_gates._hsi_cache
                    _cache_ttl = 300  # 5分钟
                    _now_ts = _time.time()

                    if _cache['data'] is None or (_now_ts - _cache['ts']) > _cache_ttl:
                        from futu import OpenQuoteContext, KLType, RET_OK
                        _hq = OpenQuoteContext(host=config.OPEND_HOST, port=config.OPEND_PORT)
                        _limiter = get_global_limiter()
                        _ret_k, _hsi_data = None, None
                        if _limiter.acquire(timeout=5):
                            _ret_k, _hsi_data = _hq.get_cur_kline("HK.800000", 30, KLType.K_DAY)
                            _limiter.record()
                        _hq.close()

                        if _ret_k == RET_OK and _hsi_data is not None and len(_hsi_data) >= 20:
                            _closes = _hsi_data['close'].values.tolist()
                            _cache['ma5'] = sum(_closes[-5:]) / 5
                            _cache['ma20'] = sum(_closes[-20:]) / 20
                            _cache['data'] = _closes
                            _cache['ts'] = _now_ts
                            _cache['current'] = _closes[-1]
                        else:
                            logger.debug(f"[市场趋势门禁] HSI 数据获取失败 (ret={_ret_k})，降级放行")
                            _cache = None

                    if _cache and _cache['ma5'] is not None:
                        _ma5 = _cache['ma5']
                        _ma20 = _cache['ma20']
                        _current_hsi = _cache['current']
                        _gap_pct = (_ma20 - _ma5) / _ma20 * 100 if _ma20 > 0 else 0
                        _gap_min = _mr_cfg.get('hsi_gap_min_pct', 0.3)

                        if _ma5 < _ma20 and abs(_gap_pct) >= _gap_min:
                            # 恒指处于下跌趋势 (MA5 < MA20 且偏离幅度≥阈值)
                            _msg = f"市场趋势门禁: HSI MA5({_ma5:.0f}) < MA20({_ma20:.0f}) 差距{_gap_pct:.1f}%≥{_gap_min}%"
                            # 模拟盘 shadow 模式: 告警+打标签，不硬拦截
                            if _shadow_mode:
                                logger.warning(f"[模拟门禁·影子] {signal.code} {_msg}，告警但放行 (数据收集)")
                                signal.extra = signal.extra or {}
                                signal.extra['hsi_regime_warning'] = f'downtrend_gap_{_gap_pct:.1f}pct'
                            else:
                                _msg += "，下跌趋势中禁止开新仓 (逆市做多风险高)"
                                logger.info(f"[市场趋势门禁] {signal.code} {_msg}")
                                failure_tracker.record_failure(signal.code, 'market_regime_gate', _msg)
                                return TradeResult(success=False, message=_msg)
                        elif _ma5 < _ma20:
                            # MA5 < MA20 但 gap 太小，视为横盘震荡而非下跌趋势
                            logger.debug(f"[市场趋势门禁] HSI MA5({_ma5:.0f}) < MA20({_ma20:.0f}) 但差距{_gap_pct:.1f}% < {_gap_min}%，视为横盘，放行")
                        else:
                            logger.debug(f"[市场趋势门禁] HSI MA5({_ma5:.0f}) >= MA20({_ma20:.0f})，趋势允许开仓")
            except Exception as _mr_e:
                logger.debug(f"[市场趋势门禁] 检查跳过: {_mr_e}")

            # 2026-03-25 新增: 最大持仓数检查
            # 2026-04-15 修复: 以富途服务端实时数据为准，不再依赖本地 DB
            max_open_positions = getattr(config, 'MAX_OPEN_POSITIONS', 12)  # 2026-04-15 调整
            
            # 2026-05-08 修复: 统一走本地 DB 路径，避免 live_query Futu API 永久阻塞
            current_open_positions = self.position_manager.get_open_positions_count()
            data_source = "local_db (SSOT)"
            
            if current_open_positions >= max_open_positions:
                msg = f"已达最大持仓数 ({current_open_positions}/{max_open_positions})，拒绝开仓 [{data_source}]"
                logger.warning(f"[持仓限制] {signal.code} {msg}")
                failure_tracker.record_failure(signal.code, 'position_limit', msg)
                self._record_gate_reject(signal, 'max_position', msg, shadow_mode=_is_sim)
                return TradeResult(success=False, message=msg)

            # P0 修复 (2026-05-08): 开仓前 Futu 可用资金检查
            # 防止实盘中因资金不足被拒单或在保证金模式下过度杠杆
            try:
                _acc = self.get_account_info("HK")
                if _acc:
                    _power = float(_acc.get('power', 0) or 0)
                    _total = float(_acc.get('total_assets', 0) or 0)
                    # 预估本次开仓所需资金 (信号价格 × 基础数量)
                    _est_cost = signal.price * self._get_lot_size(signal.code) if signal.price > 0 else 0
                    if _est_cost > 0 and _power > 0 and _est_cost > _power:
                        msg = f"购买力不足: 预估需要 {_est_cost:,.0f}, 可用 {_power:,.0f}, 总资产 {_total:,.0f}"
                        logger.warning(f"[资金门禁] {signal.code} {msg}")
                        failure_tracker.record_failure(signal.code, 'insufficient_funds', msg)
                        return TradeResult(success=False, message=msg)
                    logger.debug(
                        f"[资金门禁] {signal.code} 购买力={_power:,.0f} 总资产={_total:,.0f} 预估成本={_est_cost:,.0f} ✅"
                    )
            except Exception as _af_e:
                logger.debug(f"[资金门禁] 查询跳过: {_af_e}")

            # 趋势过滤检查 V2 (2026-03-21 增强：多周期确认) (2026-04-20: 接入全局限流器)
            # 2026-04-24 修复: 仅 OPEN 需要趋势过滤；ADD/CLOSE 是已有持仓管理，趋势检查应在开仓时已完成
            # 避免已有持仓因短期波动被 ADD 信号拒绝，导致无法加仓
            if trade_type == 'OPEN':
                try:
                    from futu import OpenQuoteContext, KLType, SubType, RET_OK
                    from app.utils.futu_limiter import get_global_limiter
                    limiter = get_global_limiter()

                    quote_ctx = OpenQuoteContext(host=config.OPEND_HOST, port=config.OPEND_PORT)
                    
                    # 订阅日K和60分钟K
                    sub_types = [SubType.K_DAY, SubType.K_60M]
                    ret_sub, msg_sub = quote_ctx.subscribe(signal.code, sub_types)
                    if ret_sub != RET_OK:
                        logger.warning(f"[趋势过滤] 订阅K线失败: {msg_sub}，尝试直接获取")
                    
                    # 获取日K数据
                    if not limiter.acquire(timeout=10):
                        ret_daily = -1
                        data_daily = None
                    else:
                        ret_daily, data_daily = quote_ctx.get_cur_kline(signal.code, 30, KLType.K_DAY)
                        limiter.record()

                    # 获取60分钟K线数据
                    if not limiter.acquire(timeout=10):
                        ret_60min = -1
                        data_60min = None
                    else:
                        ret_60min, data_60min = quote_ctx.get_cur_kline(signal.code, 50, KLType.K_60M)
                        limiter.record()
                    
                    if ret_daily == 0:
                        closes = data_daily['close'].values.tolist()
                        highs = data_daily['high'].values.tolist() if 'high' in data_daily.columns else None
                        lows = data_daily['low'].values.tolist() if 'low' in data_daily.columns else None
                        intraday_closes = None
                        
                        if ret_60min == 0:
                            intraday_closes = data_60min['close'].values.tolist()
                            logger.debug(f"[趋势过滤] 获取日K {len(closes)}条, 60分钟K {len(intraday_closes)}条")
                        else:
                            logger.warning(f"[趋势过滤] 60分钟K获取失败，仅使用日K")
                        
                        # 重置趋势过滤器以加载新配置
                        from app.risk.trend_filter import reset_trend_filter, get_trend_filter
                        reset_trend_filter()
                        trend_filter = get_trend_filter()
                        
                        # P0 (2026-05-16): 从 K 线计算 ADX，防止震荡市假信号
                        # 根因: 信号 pipeline 不传 ADX → 趋势过滤失去范围市场判断能力
                        adx_value = None
                        if hasattr(signal, 'extra') and signal.extra:
                            adx_value = signal.extra.get('adx')
                        if adx_value is None and highs and lows and len(highs) >= 14:
                            try:
                                adx_value = trend_filter._compute_adx(highs, lows, closes)
                                if adx_value is not None:
                                    logger.debug(f"[ADX计算] {signal.code} ADX={adx_value:.1f}")
                            except Exception as _adx_e:
                                logger.debug(f"[ADX计算] 失败: {_adx_e}")
                        
                        # 多周期趋势分析 (增强版：支持ADX + 多周期确认)
                        trend_info = trend_filter.analyze_trend(
                            closes,
                            highs=highs,
                            lows=lows,
                            intraday_closes=intraday_closes,
                            adx=adx_value,
                            code=signal.code
                        )
                        
                        action_str = signal.action.value if hasattr(signal.action, 'value') else str(signal.action)
                        should_trade, reason = trend_filter.should_trade(action_str, trend_info)
                        
                        if not should_trade:
                            quote_ctx.close()
                            failure_tracker.record_failure(signal.code, 'trend_filter', reason)
                            logger.warning(f"[趋势过滤] {signal.code} 交易被阻止: {reason}")
                            return TradeResult(success=False, message=reason)
                        
                        logger.info(f"[趋势过滤] {signal.code} {trend_info.trend.value} | {reason}")
                        
                        # 2026-03-25 V3 新增: 多周期趋势确认
                        if not trend_info.multi_timeframe_confirmed:
                            logger.warning(f"[多周期趋势] {signal.code} 日线与60分钟线趋势冲突，建议观望")
                            # 不强制禁止，但记录警告
                        
                        # 2026-03-25 新增: 入场价格检查
                        try:
                            from app.risk.entry_price_checker import get_entry_price_checker
                            price_checker = get_entry_price_checker()
                            
                            # 获取日内数据
                            from futu import KLType
                            if limiter.acquire(timeout=10):
                                ret_snap, snap_data = quote_ctx.get_market_snapshot([signal.code])
                                limiter.record()
                            else:
                                ret_snap, snap_data = -1, None
                            if ret_snap == 0 and len(snap_data) > 0:
                                day_high = float(snap_data.iloc[0].get('high_price', 0))
                                day_low = float(snap_data.iloc[0].get('low_price', 0))
                                day_open = float(snap_data.iloc[0].get('open_price', 0))
                                
                                if day_high > 0 and day_low > 0:
                                    price_result = price_checker.check_entry_price(
                                        code=signal.code,
                                        action=signal.action.value if hasattr(signal.action, 'value') else str(signal.action),
                                        current_price=signal.price,
                                        day_high=day_high,
                                        day_low=day_low,
                                        day_open=day_open
                                    )
                                    
                                    if not price_result.allowed:
                                        quote_ctx.close()
                                        failure_tracker.record_failure(signal.code, 'price_check', price_result.reason)
                                        logger.warning(f"[价格检查] {signal.code} {price_result.reason}")
                                        return TradeResult(success=False, message=price_result.reason)
                                    
                                    if price_result.warning:
                                        logger.warning(f"[价格检查] {signal.code} {price_result.warning}")
                                    else:
                                        logger.info(f"[价格检查] {signal.code} {price_result.reason}")
                        except Exception as e:
                            logger.warning(f"[价格检查] 检查失败，跳过: {e}")
                        
                    else:
                        # 2026-03-27 修复: 日K获取失败时应拒绝交易，而非跳过检查
                        logger.warning(f"[趋势过滤] {signal.code} 日K获取失败，拒绝交易以确保安全")
                        quote_ctx.close()
                        failure_tracker.record_failure(signal.code, 'trend_filter', '日K数据获取失败')
                        return TradeResult(success=False, message="趋势过滤数据获取失败，拒绝交易")
                    quote_ctx.close()
                except Exception as e:
                    # 2026-03-27 修复: 异常时拒绝交易，而非跳过
                    logger.warning(f"[趋势过滤] {signal.code} 检查失败: {e}，拒绝交易")
                    failure_tracker.record_failure(signal.code, 'trend_filter', str(e))
                    return TradeResult(success=False, message=f"趋势过滤检查失败: {e}")
            
            risk_result = self.risk_manager.check_before_trade(signal)
            
            if not risk_result.passed:
                failed_checks = [k for k, v in risk_result.details.items() if not v]
                logger.warning(f"风控检查失败：{', '.join(failed_checks)}")
                # 记录失败 (2026-03-17 新增)
                failure_tracker.record_failure(signal.code, 'risk', f"风控检查失败：{', '.join(failed_checks)}")
                circuit_breaker.record_reject(f"风控检查失败：{', '.join(failed_checks)}")
                return TradeResult(
                    success=False,
                    message=f"风控检查失败：{', '.join(failed_checks)}"
                )
            
            logger.debug(f"风控检查通过：{risk_result.details}")

        # 2026-04-15 新增: 仓位资金安全水位检查 (仅开仓/补仓)
        if trade_type in ('OPEN', 'ADD'):
            try:
                sizing_ok, sizing_reason = self._check_position_sizing(signal, trade_type)
                if not sizing_ok:
                    logger.warning(f"[资金安全] {signal.code} {sizing_reason}")
                    failure_tracker.record_failure(signal.code, 'position_sizing', sizing_reason)
                    return TradeResult(success=False, message=sizing_reason)
            except Exception as e:
                logger.debug(f"仓位资金检查失败 (非阻塞): {e}")

        if trade_type == 'OPEN':
            return self._execute_open(signal)
        elif trade_type == 'ADD':
            return self._execute_add_position(signal)
        else:
            return self._execute_close(signal)

    def _execute_open(self, signal: Signal) -> TradeResult:
        """
        执行开仓交易
        Phase 2: 增加作战计划门禁检查
        1. 检查是否符合今日作战计划
        2. 富途下单
        3. 下单成功后创建持仓记录
        4. 返回 position_id
        """
        # 🔴 Phase 2: 离场冷却期检查 (防震荡反复打脸)
        try:
            from app.trade_exit_manager import TradeExitManager
            exit_mgr = TradeExitManager()
            if exit_mgr.is_in_cooldown(signal.code):
                logger.warning(f"⛔ [冷却期] 拒绝开仓 {signal.code}: 处于离场冷却期，禁止追入")
                return TradeResult(success=False, message="处于离场冷却期")
        except Exception as e:
            logger.warning(f"⚠️ 冷却期检查异常 (放行): {e}")

        # 🔴 Phase HK-1: 全局风险状态机门禁检查 (最高优先级)
        try:
            from app.pipeline.risk_state_machine import RiskStateMachine, RiskAction
            sm = RiskStateMachine()
            if not sm.is_trade_allowed(RiskAction.OPEN):
                current_state = sm.get_state().value
                logger.warning(f"🚫 [全局风控拦截] 拒绝开仓 {signal.code}: 系统处于 {current_state} 状态")
                return TradeResult(success=False, message=f"全局风控拦截: {current_state}")
        except Exception as e:
            logger.error(f"⚠️ [全局风控] 检查异常 (放行): {e}")

        # Phase 2: 作战计划门禁检查
        try:
            from app.pipeline.battle_plan import BattlePlanManager
            plan_mgr = BattlePlanManager()
            direction = "BUY" if signal.action == SignalAction.BUY else "SELL"
            allowed, reason = plan_mgr.check_open_trade(signal.code, direction)
            if not allowed:
                # 高置信度计划外通道: 尝试 override
                extra = getattr(signal, 'extra', {}) or {}
                cs = float(extra.get('consensus_score', extra.get('score', 0)))
                ac = int(extra.get('agree_count', 0))
                mtf_a = float(extra.get('mtf_alignment', 0))
                vol_r = float(extra.get('volume_ratio', 0))
                override_ok, override_reason = plan_mgr.check_high_conviction_override(
                    signal.code, direction, consensus_score=cs, agree_count=ac,
                    mtf_alignment=mtf_a, volume_ratio=vol_r,
                )
                if override_ok:
                    logger.warning(
                        f"🟡 [高置信度例外] {signal.code} plan_gate_reason={reason} "
                        f"override_reason={override_reason} cs={cs:.1f} ac={ac}"
                    )
                else:
                    logger.warning(
                        f"🚫 [作战计划门禁] 拒绝开仓 {signal.code}: {reason} "
                        f"(override: {override_reason})"
                    )
                    return TradeResult(success=False, message=f"计划拦截: {reason}")
            else:
                logger.debug(f"✅ [作战计划门禁] 允许开仓 {signal.code}: {reason}")
        except Exception as e:
            logger.error(f"⚠️ [作战计划门禁] 检查异常 (放行): {e}")

        market = _detect_market(signal.code)
        currency = "US$" if market == "US" else "HK$"

        # P1-3: 记录执行质量监控开始
        exec_monitor = get_execution_monitor()
        order_type_str = "MARKET" if config.DEFAULT_ORDER_TYPE == "MARKET" else "LIMIT"
        record_id = exec_monitor.record_start(
            code=signal.code,
            signal_price=signal.price,
            quantity=signal.quantity,
            order_type=order_type_str,
        )

        try:
            # TrdSide, OrderType, TrdEnv 已在文件顶部导入

            trd_ctx = self._get_trd_ctx(market)
            trd_env = TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL

            trd_side = TrdSide.BUY if signal.action == SignalAction.BUY else TrdSide.SELL

            qty = self._get_qty(signal)

            # 防御修复 (2026-05-11): 单标的仓位不超过总资金的 5%
            # 无论 quality_boost / news_boost / position_multiplier 如何叠加
            if signal.price and signal.price > 0 and qty > 0:
                max_single_amount = config.MAX_ORDER_AMOUNT * 0.05
                max_qty_by_limit = int(max_single_amount / signal.price)
                _mkt = _detect_market(signal.code)
                if _mkt == "HK":
                    _base = self._get_lot_size(signal.code)
                    if max_qty_by_limit >= _base:
                        max_qty_by_limit = (max_qty_by_limit // _base) * _base
                    else:
                        max_qty_by_limit = _base
                else:
                    max_qty_by_limit = max(1, max_qty_by_limit)
                if qty > max_qty_by_limit and max_qty_by_limit > 0:
                    logger.warning(
                        f"[单标的上限] {signal.code} 计算qty={qty} 超过资金5%限制"
                        f"({max_single_amount:.0f}/{signal.price:.2f}={max_qty_by_limit})，"
                        f"强制限制为 {max_qty_by_limit}"
                    )
                    qty = max_qty_by_limit

            # FutuMarginMonitor 检查 (2026-05-08): 复用现有保证金监控
            # 包含滞回防抖/降级防护/压力测试 (开仓后预估保证金率)
            try:
                from app.config import USE_MARGIN_GUARD
                if USE_MARGIN_GUARD and hasattr(self, 'futu_margin_monitor') and self.futu_margin_monitor:
                    _est_cost = signal.price * qty  # 预估成本
                    if not self.futu_margin_monitor.is_safe_to_open(_est_cost):
                        msg = "FutuMarginMonitor 拦截：保证金率不安全"
                        logger.warning(f"[保证金门禁] {signal.code} {msg}")
                        return TradeResult(success=False, message=msg)
            except Exception as _mg_e:
                logger.debug(f"[保证金门禁] 检查跳过: {_mg_e}")

            # 2026-03-17 集成智能订单路由
            # 修复: 兼容 action 可能是 SignalAction 枚举或字符串的情况
            action_str = signal.action.value if hasattr(signal.action, 'value') else str(signal.action)
            order = smart_router.route_order(
                code=signal.code,
                action=action_str,
                price=signal.price,
                quantity=qty,
                market_data={'volatility': 0.0}  # 可传入实时波动率数据
            )
            
            # 根据智能路由结果设置订单类型和价格
            # 2026-03-20 修复: 做空开仓不能使用市价单(价格=0)，需获取实时价格
            if order.order_type.value == 'market':
                if signal.action == SignalAction.SELL:
                    # 做空开仓：获取当前买一价作为限价
                    from futu import OpenQuoteContext
                    from app.utils.futu_limiter import get_global_limiter
                    limiter = get_global_limiter()

                    quote_ctx = OpenQuoteContext(host=config.OPEND_HOST, port=config.OPEND_PORT)
                    try:
                        if limiter.acquire(timeout=10):
                            ret, snapshot = quote_ctx.get_market_snapshot([signal.code])
                            limiter.record()
                        else:
                            ret, snapshot = -1, None
                        if ret == 0 and len(snapshot) > 0:
                            price = snapshot.iloc[0]['bid_price']  # 买一价
                            order_type = OrderType.NORMAL  # 改用限价单
                            logger.info(f"[做空开仓] 使用买一价 {price:.2f} 作为限价")
                        else:
                            price = signal.price  # 回退到信号价格
                            order_type = OrderType.NORMAL
                    finally:
                        quote_ctx.close()
                else:
                    order_type = OrderType.MARKET
                    price = 0
            else:
                order_type = OrderType.NORMAL
                price = order.price
            
            logger.info(
                f"智能路由：[{market}] {signal.code} 类型={order.order_type.value} "
                f"价格={price:.2f} 偏移={order.offset_pct:.1%} 原因={order.reason}"
            )

            # 安全检查：金额限制
            estimated_amount = signal.price * qty
            if estimated_amount > config.MAX_ORDER_AMOUNT:
                msg = f"预估金额 {currency}{estimated_amount:.2f} 超过限额 {currency}{config.MAX_ORDER_AMOUNT:.2f}, 拒绝下单"
                logger.warning(msg)
                # 记录失败 (2026-03-17 新增)
                failure_tracker.record_failure(signal.code, 'limit', msg)
                circuit_breaker.record_reject('订单金额超限')
                return TradeResult(success=False, message=msg)

            # P3 优化 (2026-04-26): 港股盘口价差预检 + 智能定价
            # 辩证设计: 阈值可配置，定价改为主动吃单 (Bid+1Tick/Ask-1Tick) 而非死板的中位数
            if order_type == OrderType.MARKET and market == "HK":
                cfg = getattr(config, 'PHASE1_HK_OPT', {}).get('spread_check', {})
                if cfg.get('enabled', True):
                    max_spread = cfg.get('max_spread_pct', 0.012) # 默认 1.2%
                    try:
                        from futu import OpenQuoteContext
                        _spread_ctx = OpenQuoteContext(host=config.OPEND_HOST, port=config.OPEND_PORT)
                        try:
                            _sr, _ob = _spread_ctx.get_order_book(signal.code, num=1)
                            if _sr == 0 and _ob is not None and len(_ob.get('Bid', [])) > 0 and len(_ob.get('Ask', [])) > 0:
                                _bid = _ob['Bid'][0][0]
                                _ask = _ob['Ask'][0][0]
                                if _bid > 0 and _ask > 0:
                                    _spread_pct = (_ask - _bid) / ((_bid + _ask) / 2)
                                    if _spread_pct > max_spread:
                                        # 智能限价逻辑
                                        _mid = (_bid + _ask) / 2
                                        # 港交所报价单位阶梯
                                        _tick = 0.01 if _mid < 0.5 else (0.05 if _mid < 2.0 else 0.1)
                                        
                                        if signal.action == SignalAction.BUY:
                                            smart_price = _bid + _tick # 主动买一价
                                        else:
                                            smart_price = _ask - _tick # 主动卖一价
                                            
                                        logger.warning(
                                            f"[价差过大] {signal.code} 价差={_spread_pct:.2%} > {max_spread:.1%}, "
                                            f"转智能限价: {smart_price:.3f}"
                                        )
                                        order_type = OrderType.NORMAL
                                        price = smart_price
                        finally:
                            _spread_ctx.close()
                    except Exception as _sp_e:
                        logger.debug(f"[价差检查] {signal.code} 异常(降级放行): {_sp_e}")

            logger.info(
                f"准备下单：[{market}] {signal.code} {trd_side} "
                f"qty={qty} type={order_type} price={price}"
            )

            # 2026-03-17 集成订单重试机制
            ret, data = self._execute_order_with_retry(
                trd_ctx=trd_ctx,
                code=signal.code,
                price=price,
                qty=qty,
                trd_side=trd_side,
                order_type=order_type,
                trd_env=trd_env,
                signal=signal,
            )

            if ret == 0:
                order_id = str(data.iloc[0]["order_id"]) if len(data) > 0 else "unknown"
                
                # ----------------------------------------------------------------
                # P0 修复 (2026-04-26): 订单状态确认
                # 原逻辑假设 ret==0 即成交，实际可能因资金不足/风控被拒。
                # 若未真正成交，不应创建持仓，防止产生幽灵仓。
                # ----------------------------------------------------------------
                
                # 获取真实成交价：市价单下单后查询实际成交均价
                # 避免使用 signal.price（多个K线的均值）作为 entry_price，
                # 否则止损止盈价基准不准确，导致开仓后立刻亏损平仓
                # ----------------------------------------------------------------
                actual_entry_price = signal.price  # 默认值
                order_status = "UNKNOWN"
                is_filled = False
                
                try:
                    import time as _time
                    _time.sleep(0.3)  # 等待成交数据落盘

                    # P0 修复 (2026-05-06): 模拟盘增加订单等待时间
                    # 模拟盘市价单可能需要5-10秒成交，3秒太短
                    _wait_seconds = 10.0 if config.TRADE_ENV == 1 else 3.0
                    is_filled = False
                    order_status = "UNKNOWN"
                    dealt_avg = 0.0
                    dealt_qty = 0.0
                    _poll_deadline = _time.time() + _wait_seconds

                    while _time.time() < _poll_deadline:
                        ret_order, order_data = trd_ctx.order_list_query(order_id=order_id, trd_env=trd_env)
                        if ret_order == 0 and len(order_data) > 0:
                            order_status = order_data.iloc[0].get('order_status', '')
                            dealt_avg = float(order_data.iloc[0].get('dealt_avg_price', 0))
                            dealt_qty = float(order_data.iloc[0].get('qty', 0))

                            # 坚决拦截失败/取消的订单
                            if order_status in ['FAILED', 'CANCELLED', 'DELETED', 'CANCELLING']:
                                logger.error(
                                    f"🚨 [订单失败] {signal.code} Status={order_status}, 拒绝创建持仓"
                                )
                                return TradeResult(success=False, message=f"Order {order_status}")

                            # 已成交或部分成交 → 可以创建持仓
                            # P0 修复 (2026-05-06): Futu SDK 返回 FILLED_ALL/FILLED_PART，不是 FULLY_DEALT/PART_DEALT
                            if order_status in ('FILLED_ALL', 'FILLED_PART', 'PART_DEALT', 'FULLY_DEALT', 'PART_CANCELLED') and dealt_avg > 0:
                                is_filled = True
                                logger.info(
                                    f"[成交确认] {signal.code} Status={order_status} "
                                    f"dealt_avg={dealt_avg:.4f} qty={dealt_qty}"
                                )
                                break

                        _time.sleep(0.5)  # 未成交则等待后重试

                    if not is_filled:
                        logger.error(
                            f"🚨 [未成交] {signal.code} 订单未在3s内成交 "
                            f"(Status={order_status}, dealt_avg={dealt_avg}), 拒绝创建持仓"
                        )
                        return TradeResult(
                            success=False,
                            message=f"Order not filled: {order_status}",
                        )

                    # 订单已成交: 使用真实成交均价
                    actual_entry_price = dealt_avg
                    logger.info(f"[真实成交价] {signal.code} dealt_avg_price={dealt_avg:.4f}")
                    if signal.price > 0:
                        self._slippage_records.append((signal.price, dealt_avg, signal.code))
                        if len(self._slippage_records) > 200:
                            self._slippage_records = self._slippage_records[-200:]
                except Exception as ep:
                    logger.warning(f"[成交价查询失败] {signal.code}: {ep}，使用信号价格 {signal.price:.4f}")
                
                # --- Phase 2 Attribution Hook (Execution Result) ---
                try:
                    from app.attribution.chain import update_execution_result
                    signal_price = signal.price if signal.price > 0 else actual_entry_price
                    slippage = actual_entry_price - signal_price
                    trace_id = getattr(signal, 'trace_id', '')
                    if trace_id:
                        update_execution_result(trace_id, actual_entry_price, slippage)
                except Exception as e:
                    logger.debug(f"归因记录失败 (Execution): {e}")

                # 获取动态止盈止损参数 (2026-03-17 新增)
                # SSOT: Market-specific exit params
                is_us = signal.code.startswith('US.')
                if is_us:
                    us_params = _get_us_exit_params()
                    stop_loss = abs(us_params.get('stop_loss', 0.03))
                    take_profit = abs(us_params.get('take_profit', 0.05))
                else:
                    stop_loss = dynamic_exit_manager.get_stop_loss()
                    take_profit = dynamic_exit_manager.get_take_profit()
                
                # 以真实成交价为基础计算止盈止损价 (考虑交易费用)
                # 2026-03-24: 优化止盈止损计算，加入交易费用考量
                from app.risk.trading_fees import DEFAULT_FEES
                
                # 使用考虑费用的止盈止损计算
                stop_price = dynamic_exit_manager.get_stop_loss_price_with_fees(
                    actual_entry_price, qty, DEFAULT_FEES
                )
                take_profit_price = dynamic_exit_manager.get_take_profit_price_with_fees(
                    actual_entry_price, qty, DEFAULT_FEES
                )
                
                # 2026-04-24 修复: 防御性校验 — 止损价必须低于入场价至少 0.5%
                # 防止因竞态/DB异常导致 stop_loss=entry_price 引发秒级止损
                if stop_price >= actual_entry_price * 0.995:
                    logger.critical(
                        f"🚨 [止损价异常] {signal.code} stop_price={stop_price:.4f} >= "
                        f"entry_price*0.995={actual_entry_price*0.995:.4f}，强制使用 -3% 止损"
                    )
                    stop_price = round(actual_entry_price * (1 - 0.03), 4)
                    take_profit_price = round(actual_entry_price * (1 + 0.10), 4)
                
                # 如果是做空，反转止盈止损方向
                if signal.action == SignalAction.SELL:
                    # 做空: 止损在上方，止盈在下方
                    stop_price = actual_entry_price * (1 + abs(stop_loss) + DEFAULT_FEES.total_with_slippage)
                    take_profit_price = actual_entry_price * (1 - take_profit - DEFAULT_FEES.total_with_slippage)
                
                logger.info(
                    f"[止盈止损] {signal.code} 入场价={actual_entry_price:.4f} "
                    f"止损={stop_price:.4f} 止盈={take_profit_price:.4f} "
                    f"(含交易费用 {DEFAULT_FEES.total_with_slippage:.2%})"
                )
                
                # 创建持仓记录 (带止盈止损 + 信号关联)
                # 注意：加仓信号（is_add_position=True）不创建新持仓，
                # 由 data_subscriber._try_add_position 统一调用 add_to_position 更新均价
                is_add = signal.extra.get('is_add_position', False) if hasattr(signal, 'extra') and signal.extra else False
                if not is_add:
                    entry_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    action_str = "BUY" if signal.action == SignalAction.BUY else "SELL"

                    # ── P2 策略对比标签 (2026-05-17) ──
                    # 门禁组合标签: 记录当前信号通过了哪些门禁，用于后续归因分析
                    _gate_tags = []
                    if hasattr(signal, 'extra') and signal.extra:
                        if signal.extra.get('gate_warning'):
                            _gate_tags.append(f"gw:{signal.extra['gate_warning']}")
                        if signal.extra.get('fee_warning'):
                            _gate_tags.append(f"fw:{signal.extra['fee_warning']}")
                    # 构建 strategy_tags 字符串
                    _strategy_tags = "|".join(_gate_tags) if _gate_tags else "clean"

                    position_id = self.position_manager.create_position(
                        code=signal.code,
                        action=action_str,
                        quantity=qty,
                        entry_price=actual_entry_price,  # 用真实成交价，不用信号均价
                        entry_time=entry_time,
                        stop_loss=stop_price,
                        take_profit=take_profit_price,
                        signal_id=getattr(signal, 'id', None),
                        strategy=getattr(signal, 'strategy', None),
                        trace_id=getattr(signal, 'trace_id', ''),  # Phase 1: 全链路归因 ID
                        trader_type='AI',  # P0 修复 2026-05-08: 标记为AI自动执行
                        strategy_tags=_strategy_tags  # P2: 门禁组合标签用于对比分析
                    )

                    # 🔴 【P0 修复 2026-04-26 / 2026-05-06 二次修复】开仓后强制校验保护单
                    # Bug 根因: get_position() 接受 code (str), 不是 position_id (int)。
                    # 之前传 position_id=165 → WHERE code='165' 查无此记录 → _pos=None → stop_loss_db=0 → 误触发 HALT。
                    # 修复: 改用 signal.code 回读，并增加 by_id 直读兜底。
                    _protection_ok = False
                    _stop_loss_db = 0.0
                    try:
                        # 方法1: 通过 code 回读 (get_position 按 code 查询)
                        _pos = self.position_manager.get_position(signal.code)
                        _stop_loss_db = float(getattr(_pos, 'stop_loss', 0) or 0) if _pos else 0.0
                        if signal.action == SignalAction.BUY:
                            _protection_ok = 0 < _stop_loss_db < actual_entry_price
                        else:  # SELL
                            _protection_ok = _stop_loss_db > actual_entry_price

                        # 方法2: 如果方法1失败，尝试通过 position_id 直读 (按 ID 精确查询)
                        if not _protection_ok and position_id:
                            _pos_by_id = self.position_manager.get_position_by_id(position_id)
                            _sl_by_id = float(getattr(_pos_by_id, 'stop_loss', 0) or 0) if _pos_by_id else 0.0
                            if _sl_by_id > 0:
                                _stop_loss_db = _sl_by_id
                                if signal.action == SignalAction.BUY:
                                    _protection_ok = 0 < _sl_by_id < actual_entry_price
                                else:
                                    _protection_ok = _sl_by_id > actual_entry_price
                    except Exception as _e_check:
                        logger.warning(f"[保护校验] {signal.code} 回读 DB 失败: {_e_check}")
                        _protection_ok = False

                    if not _protection_ok:
                        # ① 重试 3 次（0.5s / 1s / 2s 指数退避），每次重写 stop_loss
                        import time as _time
                        for _attempt in range(3):
                            _backoff = 0.5 * (2 ** _attempt)
                            logger.warning(
                                f"⚠️ [保护校验] {signal.code} 第{_attempt+1}/3 次重试 (等待{_backoff}s, stop_loss_db={_stop_loss_db:.4f})"
                            )
                            _time.sleep(_backoff)
                            try:
                                # 重写止损字段
                                self.position_manager.update_stop_loss_take_profit(
                                    signal.code,
                                    stop_loss=stop_price,
                                    take_profit=take_profit_price,
                                )
                                # 按 code 回读
                                _pos2 = self.position_manager.get_position(signal.code)
                                _sl2 = float(getattr(_pos2, 'stop_loss', 0) or 0) if _pos2 else 0.0
                                # 按 ID 直读兜底
                                if _sl2 == 0 and position_id:
                                    _pos2_by_id = self.position_manager.get_position_by_id(position_id)
                                    _sl2 = float(getattr(_pos2_by_id, 'stop_loss', 0) or 0) if _pos2_by_id else 0.0
                                if signal.action == SignalAction.BUY:
                                    _ok = 0 < _sl2 < actual_entry_price
                                else:
                                    _ok = _sl2 > actual_entry_price
                                if _ok:
                                    _protection_ok = True
                                    _stop_loss_db = _sl2
                                    logger.info(
                                        f"✅ [保护校验] {signal.code} 重试成功于第{_attempt+1}次, stop_loss={_sl2:.4f}"
                                    )
                                    break
                            except Exception as _e_retry:
                                logger.error(f"[保护校验] 重试异常: {_e_retry}")

                    if not _protection_ok:
                        logger.critical(
                            f"🚨 [P3] 保护校验持续失败! {signal.code} qty={qty} "
                            f"entry={actual_entry_price:.4f} stop_loss_db={_stop_loss_db:.4f} — 启动全局 HALT + 人工止损"
                        )
                        # ② 切 HALT（阻止后续新开仓再次裸仓）
                        try:
                            from app.pipeline.risk_state_machine import RiskStateMachine, RiskState
                            RiskStateMachine().set_state(
                                RiskState.HALT,
                                reason=f"protection_check_failed code={signal.code}",
                                source="TradeExecutor.protection_check"
                            )
                        except Exception as _e_rsm:
                            logger.error(f"[保护校验] HALT 切换失败: {_e_rsm}")

                        # ③ 同步挂远价限价卖单作人工止损 (entry -3%, 不立即成交，留人工处置窗口)
                        try:
                            from futu import OrderType as _OT, TrdSide as _TS
                            _emerg_raw = actual_entry_price * (1 - 0.03)
                            # P2 修复 (2026-05-06): 使用 tick 对齐的价格精度，避免 Futu API 拒绝
                            if signal.code.startswith("HK."):
                                _emerg_price = self._round_hk_price(_emerg_raw, 'SELL')
                            else:
                                _emerg_price = round(_emerg_raw, 2)
                            _side = _TS.SELL if signal.action == SignalAction.BUY else _TS.BUY
                            _ret_e, _data_e = trd_ctx.place_order(
                                price=_emerg_price,
                                qty=qty,
                                code=signal.code,
                                trd_side=_side,
                                order_type=_OT.NORMAL,
                                trd_env=trd_env,
                                remark="emergency_stop_due_to_protection_failure",
                            )
                            if _ret_e == 0:
                                logger.warning(
                                    f"🔴 [人工止损] {signal.code} 已挂远价限价卖单 @ {_emerg_price:.4f}—等待运维接手"
                                )
                            else:
                                logger.error(f"❌ [人工止损] 挂单失败: {_data_e}")
                        except Exception as _e_emerg:
                            logger.error(f"❌ [人工止损] 挂单异常: {_e_emerg}")

                        # ④ 红色钉钉告警
                        try:
                            from app.notifier import get_dingtalk_notifier
                            get_dingtalk_notifier().send_text(
                                f"🚨🚨🚨 [FTSeek P0] 保护校验持续失败\n"
                                f"标的: {signal.code}\n数量: {qty}\n开仓价: {actual_entry_price:.4f}\n"
                                f"stop_loss_db: {_stop_loss_db:.4f}\n"
                                f"已做: 切 HALT + 挂远价人工止损 (价格 -3%)\n"
                                f"请运维立即登录富途确认持仓与订单状态"
                            )
                        except Exception as _e_dt:
                            logger.error(f"[人工止损] 钉钉告警失败: {_e_dt}")

                    # --- Phase 2 Attribution Hook (Slippage) ---
                    try:
                        from app.attribution import record_entry
                        record_entry(
                            trace_id=getattr(signal, 'trace_id', ''),
                            code=signal.code,
                            signal_price=signal.price,
                            actual_price=actual_entry_price,
                            context={"strategy": getattr(signal, 'strategy', ''), "order_type": order_type.value}
                        )
                    except Exception as e:
                        logger.debug(f"归因记录失败 (Entry): {e}")
                        
                    trade_type_label = 'OPEN'
                else:
                    # 加仓：position_id 指向已有持仓（get_position 取第一条 OPEN）
                    existing = self.position_manager.get_position(signal.code)
                    position_id = existing.id if existing else None
                    trade_type_label = 'ADD'
                    logger.info(f"[加仓下单] {signal.code} +{qty}股 @ {actual_entry_price:.4f}，跳过新建持仓记录")
                
                msg = f"[{market}] {trade_type_label}下单成功，order_id={order_id}, position_id={position_id}, 成交价={actual_entry_price:.4f}, 止损={stop_price:.4f}, 止盈={take_profit_price:.4f}"
                logger.info(msg)
                
                # P1-3: 记录执行质量监控 - 成交
                exec_monitor.record_fill(
                    record_id=record_id,
                    fill_price=actual_entry_price,
                    retry_count=0,
                )

                # P0 修复：记录成交到数据库 (同时更新 trades 和 positions 表)
                try:
                    self.record_trade_to_db(order_id, signal, actual_entry_price, pnl=0, status='OPEN')
                    logger.info(f"✅ [数据库] 开仓成交已记录：trade_id=trade_{order_id}")

                    # ── LLM 信号质量追踪 (2026-05-17) ──
                    # 在不确定性中找到确定性：记录 LLM signal_impact 的实际交易表现
                    try:
                        _llm_impact = signal.extra.get("llm_signal_impact", "neutral") if signal.extra else "neutral"
                        _llm_factor_map = {'boost': 1.15, 'weaken': 0.80, 'block': 0.30, 'neutral': 1.00}
                        _llm_factor = _llm_factor_map.get(_llm_impact, 1.0)
                        from app.consensus.llm_quality_tracker import get_llm_quality_tracker
                        _tracker = get_llm_quality_tracker()
                        _tracker.record_signal(
                            trade_id=f"trade_{order_id}",
                            code=signal.code,
                            signal_impact=_llm_impact,
                            original_score=signal.score if hasattr(signal, 'score') else 0,
                            final_score=signal.score * _llm_factor if hasattr(signal, 'score') else 0,
                            llm_factor_applied=_llm_factor,
                            entry_price=actual_entry_price,
                            quantity=qty,
                        )
                        logger.debug(f"[LLM质量追踪] 信号入场: {signal.code} impact={_llm_impact}")
                    except Exception as _llm_q_e:
                        logger.debug(f"[LLM质量追踪] 入场记录失败: {_llm_q_e}")
                except Exception as e:
                    logger.error(f"❌ [数据库] 记录开仓成交失败：{e}", exc_info=True)

                # P0 修复 (2026-05-08): 开仓成功后同步水位管理器资金
                try:
                    self._sync_water_level_capital()
                except Exception as _wl_e:
                    logger.warning(f"[水位同步] 开仓后更新失败: {_wl_e}")

                # P0 修复 (2026-05-06): 开仓成功后发送钉钉通知
                if not is_add:
                    try:
                        from app.notifier import get_dingtalk_notifier
                        strategy_name = getattr(signal, 'strategy', 'consensus') or 'consensus'
                        get_dingtalk_notifier().send_text(
                            f"🟢 [{market}] 开仓成交通知\n"
                            f"标的: {signal.code}\n"
                            f"方向: BUY\n"
                            f"数量: {qty} 股\n"
                            f"成交价: {actual_entry_price:.4f}\n"
                            f"订单号: {order_id}\n"
                            f"止损: {stop_price:.4f}\n"
                            f"止盈: {take_profit_price:.4f}\n"
                            f"策略: {strategy_name}\n"
                            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                        )
                        logger.info(f"[钉钉通知] {signal.code} 开仓通知已发送")
                    except Exception as dt_err:
                        logger.debug(f"[钉钉通知] {signal.code} 开仓通知发送失败: {dt_err}")

                # 记录成功交易 (重置熔断器计数)
                circuit_breaker.record_success()
                
                # ----------------------------------------------------------------
                # 断点2+5修复: 止盈止损管理器 + 持仓周期管理器集成 (2026-04-06)
                # 开仓成功后将持仓信息同步到两个管理器
                # ----------------------------------------------------------------
                # 获取质量等级 (从signal.extra中获取，或默认为C级)
                quality_class_str = "CLASS_C"  # 默认值
                if hasattr(signal, 'extra') and signal.extra:
                    quality_class_str = signal.extra.get('quality_class', 'CLASS_C')
                
                # 导入质量等级枚举
                try:
                    from app.factors.quality_scorer import QualityClass
                    quality_class = getattr(QualityClass, quality_class_str.upper(), QualityClass.CLASS_C)
                except Exception:
                    quality_class = None
                
                # 断点2: 止盈止损管理器 - 添加持仓记录
                # 参数: code, entry_price, quantity, quality_class, entry_time
                if self._profit_loss_manager is not None and not is_add and quality_class is not None:
                    try:
                        self._profit_loss_manager.add_position(
                            code=signal.code,
                            entry_price=actual_entry_price,
                            quantity=qty,
                            quality_class=quality_class,
                            entry_time=datetime.now(),
                        )
                        logger.info(f"[止盈止损管理] {signal.code} 持仓已添加，三层止盈架构生效")
                    except Exception as plm_e:
                        logger.warning(f"[止盈止损管理] {signal.code} 添加持仓失败: {plm_e}")
                
                # 断点5: 持仓周期管理器 - 添加持仓记录
                # 参数: code, quality_class, entry_price, entry_time
                if self._holding_period_manager is not None and not is_add and quality_class is not None:
                    try:
                        self._holding_period_manager.add_position(
                            code=signal.code,
                            quality_class=quality_class,
                            entry_price=actual_entry_price,
                            entry_time=datetime.now(),
                        )
                        logger.info(f"[持仓周期管理] {signal.code} 持仓已添加，质量等级={quality_class_str}")
                    except Exception as hpm_e:
                        logger.warning(f"[持仓周期管理] {signal.code} 添加持仓失败: {hpm_e}")
                
                return TradeResult(
                    success=True,
                    order_id=order_id,
                    position_id=position_id,
                    trade_type=trade_type_label,
                    message=msg
                )
            else:
                msg = f"[{market}] 开仓下单失败：{data}"
                logger.error(msg)

                # P1-3: 记录执行质量监控 - 拒绝
                exec_monitor.record_reject(
                    record_id=record_id,
                    reason=str(data),
                )

                # 记录失败 (2026-03-17 新增)
                failure_tracker.record_failure(signal.code, 'other', f"下单失败：{data}")
                circuit_breaker.record_reject('下单失败')
                return TradeResult(success=False, message=msg)

        except Exception as e:
            msg = f"[{market}] 开仓交易执行异常：{e}"
            logger.error(msg, exc_info=True)
            # 记录失败 (2026-03-17 新增)
            failure_tracker.record_failure(signal.code, 'other', f"执行异常：{e}")
            circuit_breaker.record_reject('执行异常')
            return TradeResult(success=False, message=msg)

    def _execute_add_position(self, signal: Signal) -> TradeResult:
        """
        执行补仓交易 (2026-04-15 新增)
        1. 获取现有持仓
        2. 计算金字塔补仓数量
        3. 富途下单
        4. 更新持仓均价 (add_to_position)
        """
        market = _detect_market(signal.code)
        currency = "US$" if market == "US" else "HK$"

        # P3 修复 (2026-04-15): 记录执行质量监控开始
        exec_monitor = get_execution_monitor()
        order_type_str = "LIMIT"
        record_id = exec_monitor.record_start(
            code=signal.code,
            signal_price=signal.price,
            quantity=signal.quantity,
            order_type=order_type_str,
        )

        try:
            # 1. 获取现有持仓
            position = self.position_manager.get_position(signal.code)
            if position is None:
                return TradeResult(success=False, message=f"补仓失败: 未找到 {signal.code} 持仓")

            # 2. 计算金字塔补仓数量
            add_count = self.position_manager.get_today_add_count(signal.code)
            add_multipliers = getattr(config, 'MAX_ADD_MULTIPLIER', [1.0, 0.5, 0.25])
            multiplier = add_multipliers[min(add_count, len(add_multipliers)-1)]
            base_qty = position.quantity
            add_qty = max(int(base_qty * multiplier), 100)  # 最小100股

            # 对齐到整手
            lot_size = config.HK_LOT_SIZES.get(signal.code, 100) if market == "HK" else 1
            add_qty = (add_qty // lot_size) * lot_size
            if add_qty < lot_size:
                add_qty = lot_size

            logger.info(
                f"[补仓下单] {signal.code} | 现有 {base_qty} 股 | "
                f"补仓 {add_qty} 股 (系数 {multiplier}) | "
                f"价格 {signal.price}"
            )

            # 3. 富途下单
            trd_ctx = self._get_trd_ctx(market)
            trd_env = TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL

            ret, data = trd_ctx.place_order(
                price=signal.price,
                qty=add_qty,
                code=signal.code,
                trd_side=TrdSide.BUY,
                order_type=OrderType.NORMAL,
                trd_env=trd_env,
            )

            if ret != 0:
                msg = f"补仓下单失败: {data}"
                logger.error(f"[补仓失败] {signal.code} {msg}")
                return TradeResult(success=False, message=msg)

            order_id = str(data.get('order_id', '')) if isinstance(data, dict) else ''
            if not order_id and hasattr(data, 'order_id'):
                order_id = str(data.order_id)

            # 4. 更新持仓均价
            from datetime import datetime
            add_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            position_id = self.position_manager.add_to_position(
                code=signal.code,
                add_qty=add_qty,
                add_price=signal.price,
                add_time=add_time,
                signal_id=signal.id,
            )

            logger.info(
                f"✅ 补仓成功: {signal.code} +{add_qty}股 @ {signal.price} | "
                f"订单 {order_id} | 持仓ID {position_id}"
            )

            # P3 修复 (2026-04-15): 记录执行质量监控 - 成交
            exec_monitor.record_fill(
                record_id=record_id,
                fill_price=signal.price,
                retry_count=0,
            )

            return TradeResult(
                success=True,
                order_id=order_id,
                position_id=position_id,
                trade_type='ADD',
                message=f"补仓成功 +{add_qty}股",
            )

        except Exception as e:
            msg = f"[{market}] 补仓交易执行异常: {e}"
            logger.error(msg, exc_info=True)
            # P3 修复: 记录拒绝
            try:
                exec_monitor.record_reject(record_id=record_id, reason=str(e))
            except Exception:
                pass
            return TradeResult(success=False, message=msg)

    def _check_sector_concentration(self, code: str) -> tuple:
        """
        检查行业集中度 (2026-04-15 新增)

        Returns:
            (ok: bool, reason: str)
        """
        from app.factors.live_position_query import get_live_position_query
        live_query = get_live_position_query(force_refresh=True)
        active = live_query.get_active_positions(market='HK')

        if not active:
            return True, "无持仓"

        # 行业映射 (基于持仓分析)
        sector_map = {
            'HK.09618': '互联网', 'HK.03690': '互联网', 'HK.01024': '互联网',
            'HK.00981': '半导体', 'HK.01347': '半导体',
            'HK.01378': '周期', 'HK.09868': '汽车', 'HK.09992': '消费',
            'HK.01093': '医药', 'US.GOOG': '科技', 'US.JD': '互联网',
            'US.MU': '半导体',
        }

        # 新股票的行业
        new_sector = sector_map.get(code, '未知')

        # 统计各行业持仓数
        sector_count = {}
        for p in active:
            s = sector_map.get(p.code, '未知')
            sector_count[s] = sector_count.get(s, 0) + 1

        # 检查新股票所属行业
        current_count = sector_count.get(new_sector, 0)
        max_per_sector = getattr(config, 'MAX_POSITIONS_PER_SECTOR', 4)
        sector_block = getattr(config, 'SECTOR_BLOCK_THRESHOLD', 40.0)

        if current_count >= max_per_sector:
            return False, f"行业 {new_sector} 已达上限 ({current_count}/{max_per_sector})"

        # 检查行业占比
        total = len(active)
        if total > 0 and (current_count / total * 100) >= sector_block:
            return False, f"行业 {new_sector} 集中度 {(current_count/total*100):.0f}% >= {sector_block}%"

        return True, f"行业 {new_sector} 当前 {current_count} 只"

    def _check_position_sizing(self, signal: Signal, trade_type: str) -> tuple:
        """
        检查仓位资金安全水位 (2026-04-15 新增)

        Returns:
            (ok: bool, reason: str)
        """
        from app.factors.position_sizing import get_position_sizing_manager
        from app.factors.live_position_query import get_live_position_query

        manager = get_position_sizing_manager()
        live_query = get_live_position_query()

        # 获取总资金和持仓
        max_drawdown = getattr(config, 'MAX_PORTFOLIO_DRAWDOWN', 15.0)
        daily_max = getattr(config, 'MAX_DAILY_LOSS_PCT', 3.0)
        min_cash = getattr(config, 'MIN_CASH_RESERVE_PCT', 10.0)

        # 估算总资金 (持仓市值 + 假设的现金)
        active = live_query.get_active_positions()
        total_mv = sum(p.market_val for p in active)
        # 假设当前已投入 total_mv，总资金估算为 total_mv / (1 - min_cash/100)
        total_capital = total_mv / (1 - min_cash / 100) if total_mv > 0 else 300000

        # 构建持仓列表
        positions_data = []
        for p in active:
            positions_data.append({
                'code': p.code,
                'market_val': p.market_val,
                'pl_val': p.pl_val,
                'pl_pct': p.pl_ratio,
            })

        # 风险评估
        report = manager.assess_portfolio_risk(
            total_capital=total_capital,
            current_positions=positions_data,
        )

        # 1. 熔断检查
        if report.drawdown_violation:
            return False, (
                f"资金回撤 {report.drawdown_pct:.1f}% ≥ {max_drawdown}%，"
                f"触发清仓熔断，禁止所有交易"
            )

        # 2. 每日亏损检查
        if report.daily_loss_violation:
            return False, (
                f"今日亏损 {abs(report.daily_loss_pct):.1f}% ≥ {daily_max}%，"
                f"暂停当日交易"
            )

        # 3. 现金缓冲 (禁止新开仓) - 2026-04-15 修复: 边界值不应拦截
        if report.cash_violation and report.cash_pct < min_cash - 0.5:  # 留0.5%容差
            return False, (
                f"现金缓冲 {report.cash_pct:.1f}% < {min_cash}%，"
                f"禁止新开仓"
            )

        # 4. 单股违规
        for v in report.position_violations:
            if v['code'] == signal.code:
                return False, (
                    f"{signal.code} 占比 {v['pct']:.1f}% 已达上限 "
                    f"{v['limit']:.0f}%，禁止继续买入"
                )

        # 5. 行业违规
        sector = manager.get_sector(signal.code)
        for v in report.sector_violations:
            if v['sector'] == sector:
                return False, (
                    f"行业 {sector} 占比 {v['pct']:.1f}% 已达上限 "
                    f"{v['limit']:.0f}%，禁止该行业新开仓"
                )

        return True, "资金安全水位正常"

    def _execute_close(self, signal: Signal) -> TradeResult:
        """
        执行平仓交易
        1. 获取持仓
        2. 富途下单平仓
        3. 更新持仓状态
        4. 计算已实现盈亏
        5. 返回 pnl
        
        2026-03-25 修复: 处理碎股问题，确保平仓数量是整手倍数
        """
        market = _detect_market(signal.code)
        currency = "US$" if market == "US" else "HK$"

        # P3 修复 (2026-04-15): 记录执行质量监控开始
        exec_monitor = get_execution_monitor()
        order_type_str = config.DEFAULT_ORDER_TYPE
        record_id = exec_monitor.record_start(
            code=signal.code,
            signal_price=signal.price,
            quantity=0,  # 平仓数量从持仓获取
            order_type=order_type_str,
        )

        try:
            # 1. 获取持仓
            position = self.position_manager.get_position(signal.code)
            if position is None:
                msg = f"[{market}] 平仓失败：未找到 {signal.code} 的持仓记录"
                logger.warning(msg)
                return TradeResult(success=False, message=msg)

            # TrdSide, OrderType, TrdEnv 已在文件顶部导入

            trd_ctx = self._get_trd_ctx(market)
            trd_env = TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL

            # 平仓方向与开仓相反
            if position.action == 'BUY':
                trd_side = TrdSide.SELL
            else:
                trd_side = TrdSide.BUY

            # 使用持仓的数量
            qty = position.quantity
            
            # 2026-03-25 修复碎股问题: 确保平仓数量是整手倍数
            if market == "HK":
                # P1 2026-05-08: 用 _get_lot_size() 动态查询替代硬编码 fallback
                # 解决 HK.01211 等 lot_size=500 但 DEFAULT_ORDER_QTY_BY_CODE 返回 100 的问题
                lot_size = self._get_lot_size(signal.code)
                original_qty = qty
                # 向下取整到最近的整手倍数
                qty = (qty // lot_size) * lot_size
                if qty <= 0:
                    msg = f"[{market}] 平仓失败：持仓 {original_qty} 股不足 1 手 ({lot_size} 股/手)，无法平仓"
                    logger.error(msg)
                    return TradeResult(success=False, message=msg)
                if qty < original_qty:
                    logger.warning(f"[碎股处理] {signal.code} 持仓 {original_qty} 股，调整为整手 {qty} 股 (每手 {lot_size} 股)，剩余 {original_qty - qty} 股碎股暂无法平仓")
            elif market == "US":
                # 美股最小交易单位 1 股，向下取整到整数
                original_qty = qty
                qty = int(qty)
                if qty < 1:
                    msg = f"[{market}] 平仓失败：持仓 {original_qty} 股不足 1 股，无法平仓"
                    logger.error(msg)
                    return TradeResult(success=False, message=msg)
                if qty != original_qty:
                    logger.debug(f"[美股平仓] {signal.code} 持仓 {original_qty} 股，取整为 {qty} 股")

            if config.DEFAULT_ORDER_TYPE == "MARKET":
                order_type = OrderType.MARKET
                price = 0
            else:
                order_type = OrderType.NORMAL
                price = signal.price

            # 平仓不做金额限制 - 平仓是减少风险的操作，不应被金额限制
            # 2026-03-24 修复: 移除平仓金额检查，解决止盈被拦截的问题
            # estimated_amount = signal.price * qty
            # if estimated_amount > config.MAX_ORDER_AMOUNT:
            #     msg = f"预估金额 {currency}{estimated_amount:.2f} 超过限额 {currency}{config.MAX_ORDER_AMOUNT:.2f}, 拒绝平仓"
            #     logger.warning(msg)
            #     return TradeResult(success=False, message=msg)

            logger.info(
                f"准备平仓：[{market}] {signal.code} {trd_side} "
                f"qty={qty} type={order_type} price={price} position_id={position.id}"
            )

            # 根据证券类型自动选择账户 (2026-03-31: 期权/正股双账户支持)
            acc_id = _get_account_for_code(signal.code, trd_env)

            # 诊断日志：记录平仓下单关键参数
            logger.info(f"[平仓下单] {signal.code} trd_side={trd_side} acc_id={acc_id} qty={qty} price={price}")

            ret, data = trd_ctx.place_order(
                price=price,
                qty=qty,
                code=signal.code,
                trd_side=trd_side,
                order_type=order_type,
                trd_env=trd_env,
                acc_id=acc_id,
            )

            # 平仓重试与退避机制：区分可重试/不可重试错误
            RETRYABLE_ERRORS = ["频率太高", "rate limit", "网络", "timeout", "系统繁忙"]
            NON_RETRYABLE_ERRORS = ["余额不足", "权限不足"]
            MAX_CLOSE_RETRIES = 2
            RETRY_DELAYS = [2, 5]  # 第1次重试等2秒，第2次等5秒

            for retry_i in range(MAX_CLOSE_RETRIES):
                if ret == 0:
                    break  # 成功，退出重试

                error_msg = str(data) if data is not None else ""

                # 证券类型错误特殊处理：尝试切换账户
                if any(kw in error_msg for kw in ["证券类型", "security type", "不支持此证券"]):
                    logger.error(f"[证券类型错误] {signal.code} 账户 {acc_id} 不支持此证券类型")
                    # 尝试切换到另一个账户
                    alternative_acc_ids = [18820895, 8246818]
                    switched = False
                    for alt_acc_id in alternative_acc_ids:
                        if alt_acc_id == acc_id:
                            continue
                        logger.info(f"[平仓] {signal.code} 尝试切换账户 {alt_acc_id}")
                        ret, data = trd_ctx.place_order(
                            price=price,
                            qty=qty,
                            code=signal.code,
                            trd_side=trd_side,
                            order_type=order_type,
                            trd_env=trd_env,
                            acc_id=alt_acc_id,
                        )
                        if ret == 0:
                            logger.info(f"[平仓] {signal.code} 账户 {alt_acc_id} 平仓成功")
                            acc_id = alt_acc_id
                            switched = True
                            break
                    if switched:
                        break  # 切换成功，退出重试
                    break  # 证券类型错误不再重试

                # 卖空错误特殊处理：尝试切换CASH账户
                if any(kw in error_msg for kw in ["卖空", "short selling", "short"]):
                    # P0 修复：使用备选账户列表
                    alternative_acc_ids = [18820895, 8246818]
                    try:
                        alt_acc = int(config.SIMULATE_ACCOUNT_ID)
                        if alt_acc not in alternative_acc_ids:
                            alternative_acc_ids.insert(0, alt_acc)
                    except Exception as e:
                        logger.debug(f"[备选账户] 解析 SIMULATE_ACCOUNT_ID 失败: {e}")
                    
                    switched = False
                    for alt_acc_id in alternative_acc_ids:
                        if alt_acc_id == acc_id:
                            continue
                        logger.info(f"[平仓] {signal.code} 尝试账户 {alt_acc_id}")
                        ret, data = trd_ctx.place_order(
                            price=price,
                            qty=qty,
                            code=signal.code,
                            trd_side=trd_side,
                            order_type=order_type,
                            trd_env=trd_env,
                            acc_id=alt_acc_id,
                        )
                        if ret == 0:
                            logger.info(f"[平仓] {signal.code} 账户 {alt_acc_id} 平仓成功")
                            acc_id = alt_acc_id
                            switched = True
                            break
                    
                    if switched:
                        break  # 切换成功，退出重试

                # 不可重试错误直接退出
                if any(kw in error_msg for kw in NON_RETRYABLE_ERRORS):
                    logger.warning(f"[平仓] {signal.code} 不可重试错误: {error_msg}")
                    break

                # 可重试错误进行退避重试
                if any(kw in error_msg for kw in RETRYABLE_ERRORS):
                    delay = RETRY_DELAYS[retry_i]
                    logger.info(
                        f"[平仓重试] {signal.code} 第{retry_i+1}次重试，"
                        f"等待{delay}秒: {error_msg}"
                    )
                    time.sleep(delay)
                    ret, data = trd_ctx.place_order(
                        price=price,
                        qty=qty,
                        code=signal.code,
                        trd_side=trd_side,
                        order_type=order_type,
                        trd_env=trd_env,
                        acc_id=acc_id,
                    )
                else:
                    break  # 未知错误不重试

            if ret == 0:
                order_id = str(data.iloc[0]["order_id"]) if len(data) > 0 else "unknown"
                
                # 查询真实平仓成交价（同开仓逻辑）
                actual_exit_price = signal.price  # 默认值（当前价）
                try:
                    import time as _time
                    _time.sleep(0.3)
                    ret_order, order_data = trd_ctx.order_list_query(order_id=order_id, trd_env=trd_env)
                    if ret_order == 0 and len(order_data) > 0:
                        dealt_avg = float(order_data.iloc[0].get('dealt_avg_price', 0))
                        if dealt_avg > 0:
                            actual_exit_price = dealt_avg
                            logger.info(f"[真实平仓价] {signal.code} dealt_avg_price={dealt_avg:.4f}")
                except Exception as ep:
                    logger.debug(f"[平仓价查询失败] {signal.code}: {ep}")
                
                # 更新持仓状态并计算盈亏
                exit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                # --- Phase 2 Attribution Hook (Exit) ---
                # 获取持仓中的 trace_id
                pos_trace_id = getattr(position, 'trace_id', '') or 'N/A'
                
                pnl = self.position_manager.close_position(
                    code=signal.code,
                    exit_price=actual_exit_price,
                    exit_time=exit_time
                )
                
                # 记录平仓归因
                try:
                    from app.attribution import record_exit
                    record_exit(
                        trace_id=pos_trace_id,
                        code=signal.code,
                        exit_price=actual_exit_price,
                        pnl=pnl,
                        context={"exit_time": exit_time}
                    )
                except Exception as e:
                    logger.debug(f"归因记录失败 (Exit): {e}")
                
                # P0 修复：同步更新 trades 表 (确保与 positions 表一致)
                try:
                    from app.storage.database import get_database
                    db = get_database()
                    open_trades = db.get_open_trades()
                    updated = False
                    for trade in open_trades:
                        if trade['code'] == signal.code:
                            denominator = trade['entry_price'] * trade['quantity'] if trade.get('quantity', 0) else 0
                            pnl_percent = pnl / denominator if denominator > 0 else 0
                            db.update_trade_exit(
                                trade_id=trade['id'],
                                exit_price=actual_exit_price,
                                pnl=pnl,
                                pnl_percent=pnl_percent,
                                exit_reason='normal_close'
                            )
                            logger.info(f"✅ [数据库] 交易记录已更新：trade_id={trade['id']}, pnl={pnl:.2f}, pnl%={pnl_percent:.2%}")
                            updated = True
                            break
                    
                    if not updated:
                        logger.warning(f"⚠️ [数据库] 未找到 {signal.code} 的 OPEN 交易记录，positions 表已平仓但 trades 表未同步")
                except Exception as e:
                    logger.error(f"❌ [数据库] 更新交易记录失败：{e}", exc_info=True)
                
                msg = f"[{market}] 平仓下单成功，order_id={order_id}, pnl={pnl:.2f}"
                logger.info(msg)

                # P3 修复 (2026-04-15): 记录执行质量监控 - 成交
                exec_monitor.record_fill(
                    record_id=record_id,
                    fill_price=actual_exit_price,
                    retry_count=0,
                )

                # P0+P4 修复 (2026-04-15): 记录平仓成交到数据库 (status=CLOSED)
                try:
                    self.record_trade_to_db(order_id, signal, actual_exit_price, pnl, status='CLOSED')
                    logger.info(f"✅ 平仓成交已记录到数据库 (status=CLOSED)")

                    # ── Phase 1.5: 假设驱动进化评估 (对标 RD-Agent Feedback) ──
                    try:
                        from app.review.hypothesis_engine import get_hypothesis_engine
                        trade_info = {
                            'code': signal.code,
                            'pnl': pnl,
                            'exit_reason': signal.extra.get('exit_reason', signal.reason) if hasattr(signal, 'extra') and signal.extra else signal.reason,
                            'strategy': getattr(signal, 'strategy', 'unknown'),
                        }
                        result = get_hypothesis_engine().evaluate_trade(trade_info)
                        if result.get('matched', 0) > 0:
                            logger.info(f"[HypothesisEngine] 交易评估: {result['matched']} 个假设被触发, "
                                       f"更新: {len(result.get('updates', []))} 条")
                    except Exception as _hyp_e:
                        logger.debug(f"[HypothesisEngine] 平仓交易评估失败: {_hyp_e}")
                    
                    # ── LLM 信号质量追踪 · 结果记录 (2026-05-17) ──
                    try:
                        from app.consensus.llm_quality_tracker import get_llm_quality_tracker
                        _tracker = get_llm_quality_tracker()
                        _open_sig = _tracker.find_open_signal_by_code(signal.code)
                        if _open_sig:
                            _holding_min = 0
                            if hasattr(signal, 'timestamp'):
                                _holding_min = max(1, int((datetime.now() - signal.timestamp).total_seconds() / 60))
                            _entry_price = _open_sig.get('entry_price', 0)
                            _qty = _open_sig.get('quantity', 1)
                            _denom = _entry_price * _qty
                            _actual_return = pnl / _denom if _denom > 0 else 0
                            _max_dd = abs(min(pnl, 0) / _denom) if _denom > 0 else 0
                            _max_profit = max(pnl, 0) / _denom if _denom > 0 else 0
                            _exit_reason = signal.extra.get('exit_reason', signal.reason) if hasattr(signal, 'extra') and signal.extra else signal.reason
                            if _exit_reason is None:
                                _exit_reason = 'normal_close'
                            _tracker.record_outcome(
                                trade_id=_open_sig['trade_id'],
                                code=signal.code,
                                signal_impact=_open_sig['signal_impact'],
                                actual_return=_actual_return,
                                max_drawdown=_max_dd,
                                max_profit=_max_profit,
                                exit_reason=str(_exit_reason),
                                holding_minutes=_holding_min,
                            )
                            logger.debug(f"[LLM质量追踪] 结果记录: {signal.code} impact={_open_sig['signal_impact']} return={_actual_return:+.2%}")
                    except Exception as _llm_o_e:
                        logger.debug(f"[LLM质量追踪] 结果记录失败: {_llm_o_e}")
                    
                    # ── Phase 2: 更新归因记录 (Attribution Update) ──
                    try:
                        from app.attribution.chain import update_trade_attribution_result
                        exit_reason = signal.extra.get('exit_reason', signal.reason) if hasattr(signal, 'extra') and signal.extra else signal.reason
                        holding_hours = (datetime.now() - signal.timestamp).total_seconds() / 3600 if hasattr(signal, 'timestamp') else 0
                        
                        update_trade_attribution_result(
                            trace_id=signal.trace_id,
                            code=signal.code,
                            pnl=pnl,
                            exit_reason=exit_reason,
                            holding_hours=holding_hours,
                            signal_strength=signal.score if hasattr(signal, 'score') else 0,
                            signal_confidence=signal.confidence if hasattr(signal, 'confidence') else 0,
                        )
                        logger.debug(f"✅ 归因记录已更新: {signal.trace_id}")
                        
                        # ── Phase 3: 记录退出事件 (WAL) ──
                        from app.core.event_log import log_event
                        log_event(
                            trace_id=signal.trace_id,
                            event_type="EXIT",
                            symbol=signal.code,
                            data={
                                "exit_price": actual_exit_price,
                                "pnl": pnl,
                                "reason": exit_reason,
                                "holding_hours": holding_hours,
                            }
                        )
                    except Exception as e:
                        logger.debug(f"归因/日志更新异常: {e}")
                        
                except Exception as e:
                    logger.error(f"❌ 记录平仓成交失败：{e}")

                # ================================================================
                # 归因系统集成 (2026-04-09 新增): 记录交易结果到健康度追踪器
                # ================================================================
                try:
                    tracker = get_strategy_health_tracker()

                    # 从信号获取策略名称和市场状态
                    strategy = getattr(signal, 'strategy', 'unknown')
                    market_regime = 'neutral'
                    if hasattr(signal, 'extra') and signal.extra:
                        market_regime = signal.extra.get('market_regime', 'neutral')

                    # 获取持仓信息
                    position_obj = self.position_manager.get_position(signal.code)
                    entry_time = position_obj.entry_time if position_obj else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    entry_price = position_obj.entry_price if position_obj else actual_exit_price

                    # 创建健康度交易记录
                    health_trade = HealthTradeRecord(
                        trade_id=order_id,
                        strategy=strategy,
                        code=signal.code,
                        action=position_obj.action if position_obj else signal.action.value,
                        entry_price=entry_price,
                        exit_price=actual_exit_price,
                        pnl=pnl,
                        pnl_percent=pnl / (entry_price * position_obj.quantity) if position_obj and entry_price > 0 else 0,
                        entry_time=entry_time,
                        exit_time=datetime.now().isoformat(),
                        market_regime=market_regime,
                    )

                    # 记录到健康度追踪器
                    tracker.record_trade(health_trade)
                    logger.info(
                        f"[归因系统] {strategy} 交易已记录：{signal.code} "
                        f"盈亏={pnl:.2f} ({pnl/entry_price*100:.2f}%) 市场状态={market_regime}"
                    )
                except Exception as health_e:
                    logger.warning(f"[归因系统] 记录交易健康度失败：{health_e}")
                # ================================================================

                # ═══════════════════════════════════════════════════════════
                # P0 修复 (2026-05-08): 写入 signal_outcomes 表
                # 打通策略学习闭环 — 让因子质量评分基于真实交易结果
                # ═══════════════════════════════════════════════════════════
                try:
                    from app.storage.database import get_database
                    db = get_database()
                    
                    # 计算盈亏百分比
                    pnl_pct = pnl / (entry_price * position.quantity) if position and entry_price > 0 and position.quantity > 0 else 0
                    
                    # 从 signal.extra 提取策略上下文
                    extra = getattr(signal, 'extra', {}) or {}
                    
                    # 计算持仓周期数 (K线数)
                    try:
                        from datetime import datetime as _dt
                        entry_dt = _dt.strptime(position.entry_time, "%Y-%m-%d %H:%M:%S") if hasattr(position, 'entry_time') and position.entry_time else _dt.now()
                        exit_dt = _dt.now()
                        holding_hours = (exit_dt - entry_dt).total_seconds() / 3600
                        # 假设日K线，1天=1个周期；如果是分钟K线需要调整
                        holding_periods = max(1, int(holding_hours * 6.5))  # 港股每天6.5小时交易
                    except Exception:
                        holding_periods = 1
                    
                    outcome_data = {
                        'code': signal.code,
                        'action': position.action if position else signal.action.value if hasattr(signal, 'action') else 'BUY',
                        'strategy': getattr(signal, 'strategy', 'confluence_v2') or 'confluence_v2',
                        'entry_price': entry_price,
                        'exit_price': actual_exit_price,
                        'pnl_pct': pnl_pct * 100,  # 数据库存储百分比 (如 5.0 表示 5%)
                        'holding_periods': holding_periods,
                        'market_regime': extra.get('market_regime', 'neutral'),
                        'scenario': extra.get('scenario', None),
                        'adx_value': extra.get('adx', 0.0) or 0.0,
                        'mtf_alignment': extra.get('mtf_alignment', 0.0) or 0.0,
                        'sentiment_score': extra.get('sentiment_score', 0.0) or 0.0,
                        'consensus_score': extra.get('consensus_score', extra.get('conviction', 0.5)) or 0.5,
                        'quality_score': extra.get('quality_score', 1.0) or 1.0,
                        'max_drawdown': extra.get('max_drawdown', 0.0) or 0.0,
                        'max_profit': extra.get('max_profit', abs(pnl_pct)) or abs(pnl_pct),
                        'confidence': extra.get('conviction', 0.5) or 0.5,
                    }
                    
                    db.save_signal_outcome(outcome_data)
                    logger.info(
                        f"✅ [信号结果] {signal.code} 已写入 signal_outcomes | "
                        f"strategy={outcome_data['strategy']} | "
                        f"pnl={pnl_pct*100:+.2f}% | "
                        f"regime={outcome_data['market_regime']} | "
                        f"conviction={outcome_data['confidence']:.2f}"
                    )
                except Exception as outcome_e:
                    logger.warning(f"[信号结果] 写入 signal_outcomes 失败 (不影响主流程): {outcome_e}")
                # ═══════════════════════════════════════════════════════════

                # P0 修复 (2026-05-06): 平仓成功后发送钉钉通知
                try:
                    from app.notifier import get_dingtalk_notifier
                    strategy_name = getattr(signal, 'strategy', 'consensus') or 'consensus'
                    pnl_pct_str = f"{pnl/entry_price*100:+.2f}%" if entry_price > 0 else "N/A"
                    get_dingtalk_notifier().send_text(
                        f"🔴 [{market}] 平仓成交通知\n"
                        f"标的: {signal.code}\n"
                        f"方向: SELL\n"
                        f"数量: {getattr(position, 'quantity', 0)} 股\n"
                        f"入场价: {entry_price:.4f}\n"
                        f"出场价: {actual_exit_price:.4f}\n"
                        f"盈亏: {pnl:+.2f} ({pnl_pct_str})\n"
                        f"订单号: {order_id}\n"
                        f"策略: {strategy_name}\n"
                        f"原因: {getattr(signal, 'reason', 'N/A')}\n"
                        f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                    logger.info(f"[钉钉通知] {signal.code} 平仓通知已发送")
                except Exception as dt_err:
                    logger.debug(f"[钉钉通知] {signal.code} 平仓通知发送失败: {dt_err}")

                return TradeResult(
                    success=True,
                    order_id=order_id,
                    position_id=position.id,
                    trade_type='CLOSE',
                    pnl=pnl,
                    message=msg
                )
            else:
                msg = f"[{market}] 平仓下单失败：{data}"
                logger.error(msg)
                # P3 修复: 记录拒绝
                exec_monitor.record_reject(record_id=record_id, reason=str(data))
                return TradeResult(success=False, message=msg)

        except Exception as e:
            msg = f"[{market}] 平仓交易执行异常：{e}"
            logger.error(msg, exc_info=True)
            # P3 修复: 记录拒绝
            try:
                exec_monitor.record_reject(record_id=record_id, reason=str(e))
            except Exception:
                pass
            return TradeResult(success=False, message=msg)

    def execute_partial_close(self, signal: Signal, close_ratio: float = 0.5) -> TradeResult:
        """
        执行部分平仓
        :param signal: 交易信号
        :param close_ratio: 平仓比例 (0.5 = 50%)
        :return: TradeResult {success, order_id, pnl, close_ratio, remaining_quantity}
        """
        market = _detect_market(signal.code)
        currency = "US$" if market == "US" else "HK$"

        # 验证平仓比例
        if not 0 < close_ratio <= 1:
            msg = f"无效的平仓比例：{close_ratio}，必须在 (0, 1] 范围内"
            logger.error(msg)
            return TradeResult(success=False, message=msg)

        try:
            # 1. 获取持仓
            position = self.position_manager.get_position(signal.code)
            if position is None:
                msg = f"[{market}] 部分平仓失败：未找到 {signal.code} 的持仓记录"
                logger.warning(msg)
                return TradeResult(success=False, message=msg)

            # TrdSide, OrderType, TrdEnv 已在文件顶部导入

            trd_ctx = self._get_trd_ctx(market)
            trd_env = TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL

            # 平仓方向与开仓相反
            if position.action == 'BUY':
                trd_side = TrdSide.SELL
            else:
                trd_side = TrdSide.BUY

            # 2. 计算平仓数量 — 优先使用 signal.quantity (已由调用方对齐整手)
            # P0 修复 (2026-05-10): 港股必须对齐整手倍数，否则 Futu 会拒绝订单
            if signal.quantity and signal.quantity > 0:
                close_qty = signal.quantity
            else:
                close_qty = int(position.quantity * close_ratio)
                # 港股整手对齐
                if market == "HK":
                    lot_size = self._get_lot_size(signal.code)
                    if close_qty % lot_size != 0:
                        close_qty = (close_qty // lot_size) * lot_size
                        # P1 修复 (2026-05-11): 取整后不足一手时，至少 1 手但不能超卖
                        if close_qty < lot_size:
                            close_qty = lot_size
                            if close_qty > position.quantity:
                                close_qty = position.quantity  # 持仓不足 1 手，全平
            
            if close_qty < 1:
                close_qty = 1  # 至少平仓 1 股
            
            # P1 修复 (2026-05-11): 防止超卖 — 平仓数量不能超过实际持仓
            if close_qty > position.quantity:
                close_qty = position.quantity
            
            # 剩余数量
            remaining_qty = position.quantity - close_qty

            if config.DEFAULT_ORDER_TYPE == "MARKET":
                order_type = OrderType.MARKET
                price = 0
            else:
                order_type = OrderType.NORMAL
                price = signal.price

            # 安全检查：金额限制
            estimated_amount = signal.price * close_qty
            if estimated_amount > config.MAX_ORDER_AMOUNT:
                msg = f"预估金额 {currency}{estimated_amount:.2f} 超过限额 {currency}{config.MAX_ORDER_AMOUNT:.2f}, 拒绝部分平仓"
                logger.warning(msg)
                return TradeResult(success=False, message=msg)

            logger.info(
                f"准备部分平仓：[{market}] {signal.code} {trd_side} "
                f"close_qty={close_qty} (ratio={close_ratio}) remaining_qty={remaining_qty} "
                f"type={order_type} price={price} position_id={position.id}"
            )

            # 根据证券类型自动选择账户 (2026-03-31: 期权/正股双账户支持)
            acc_id = _get_account_for_code(signal.code, trd_env)
            ret, data = trd_ctx.place_order(
                price=price,
                qty=close_qty,
                code=signal.code,
                trd_side=trd_side,
                order_type=order_type,
                trd_env=trd_env,
                acc_id=acc_id,
            )

            if ret == 0:
                order_id = str(data.iloc[0]["order_id"]) if len(data) > 0 else "unknown"
                
                # 4. 更新持仓 (减少数量，不改变状态)
                exit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                pnl = self.position_manager.partial_close_position(
                    code=signal.code,
                    close_qty=close_qty,
                    exit_price=signal.price,
                    exit_time=exit_time
                )
                
                msg = f"[{market}] 部分平仓下单成功，order_id={order_id}, close_qty={close_qty}, remaining_qty={remaining_qty}, pnl={pnl:.2f}"
                logger.info(msg)
                
                # 5. 返回结果 (包含剩余数量)
                return TradeResult(
                    success=True,
                    order_id=order_id,
                    position_id=position.id,
                    trade_type='PARTIAL_CLOSE',
                    pnl=pnl,
                    message=msg,
                    close_ratio=close_ratio,
                    remaining_quantity=remaining_qty
                )
            else:
                msg = f"[{market}] 部分平仓下单失败：{data}"
                logger.error(msg)
                return TradeResult(success=False, message=msg)

        except Exception as e:
            msg = f"[{market}] 部分平仓交易执行异常：{e}"
            logger.error(msg, exc_info=True)
            return TradeResult(success=False, message=msg)

    def get_account_info(self, market: str = "HK") -> Optional[dict]:
        """获取账户信息"""
        try:
            from futu import TrdEnv
            trd_ctx = self._get_trd_ctx(market)
            trd_env = TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL
            ret, data = trd_ctx.accinfo_query(trd_env=trd_env)
            if ret == 0:
                return data.to_dict("records")[0] if len(data) > 0 else {}
            return None
        except Exception as e:
            logger.error(f"获取{market}账户信息失败：{e}")
            return None

    def _sync_water_level_capital(self, market: str = "HK") -> bool:
        """
        P0 修复 (2026-05-08): 从 Futu 获取真实资金并同步到水位管理器

        解决水位管理器 current_capital 永远等于 initial_capital 的缺陷。
        每次成交后/定时调用，确保水位等级随真实资金变化。

        Returns:
            True 如果同步成功，False 如果失败或水位管理器未启用
        """
        if self._water_level_manager is None:
            return False

        try:
            acc_info = self.get_account_info(market)
            if not acc_info:
                logger.warning("[水位同步] Futu 账户信息不可用")
                return False

            # Futu accinfo_query 返回字段:
            # power (购买力), cash (现金), market_val (持仓市值), total_assets (总资产)
            # 使用 total_assets 作为 current_capital
            total_assets = float(acc_info.get('total_assets', 0) or 0)
            cash = float(acc_info.get('cash', 0) or 0)
            market_val = float(acc_info.get('market_val', 0) or 0)
            power = float(acc_info.get('power', 0) or 0)

            if total_assets <= 0:
                logger.warning(f"[水位同步] Futu 返回总资产={total_assets}，跳过更新")
                return False

            old_ratio = self._water_level_manager.current_capital / self._water_level_manager.initial_capital
            old_level = self._water_level_manager.get_current_level()

            self._water_level_manager.update_capital(
                current_capital=total_assets,
            )

            new_level = self._water_level_manager.get_current_level()
            new_ratio = self._water_level_manager.current_capital / self._water_level_manager.initial_capital

            level_changed = new_level != old_level
            level_icon = "🟢" if new_level.value == "green" else "🟡" if new_level.value == "yellow" else "🟠" if new_level.value == "orange" else "🔴"

            log_msg = (
                f"[水位同步] 总资产={total_assets:,.0f} | 现金={cash:,.0f} | 持仓={market_val:,.0f} | 购买力={power:,.0f} | "
                f"水位={new_ratio:.1%} ({level_icon} {new_level.value})"
                + (f" | ⚠️ 等级变化: {old_level.value}→{new_level.value}" if level_changed else "")
            )
            if level_changed:
                logger.warning(log_msg)
            else:
                logger.debug(log_msg)

            return True
        except Exception as e:
            logger.error(f"[水位同步] 异常: {e}")
            return False

    def close(self):
        if self._trd_ctx_hk:
            self._trd_ctx_hk.close()
            self._trd_ctx_hk = None
        if self._trd_ctx_us:
            self._trd_ctx_us.close()
            self._trd_ctx_us = None


# ============================================================================
# P0 修复：添加成交记录方法
# ============================================================================

# ============================================================================
# P0 修复：添加 record_trade_to_db 方法 (2026-03-30 13:19)
# ============================================================================

    def record_trade_to_db(self, order_id: str, signal, price: float = None, pnl: float = 0, status: str = 'OPEN'):
        """
        记录成交到数据库 (使用 HK 本地 TradeRecorder，无跨项目依赖)
        """
        from app.storage import trade_recorder as hk_trade_recorder
        from app import config
        from app.storage.database import ConnectionPool

        try:
            pool = _get_trade_db_pool()
            conn = pool.get_connection(timeout=30.0)
            cursor = conn.cursor()
            
            trade_id = f"trade_{order_id}"
            signal_id = f"signal_{trade_id}"
            
            code = signal.code
            action = 'BUY' if signal.action == SignalAction.BUY else 'SELL'
            # P1 修复 (2026-05-12): 平仓时 signal.quantity=0，实际数量从持仓获取
            if status == 'CLOSED':
                position = self.position_manager.get_position(code)
                quantity = position.quantity if position else 0
                if quantity <= 0:
                    logger.warning(f"[record_trade] {code} 平仓 quantity={quantity}，从 OPEN 记录回读")
                    cursor.execute("SELECT quantity FROM trades WHERE id = ? AND status = 'OPEN'", (trade_id,))
                    row = cursor.fetchone()
                    quantity = row[0] if row and row[0] and row[0] > 0 else 0
            else:
                quantity = getattr(signal, 'quantity', 0)
            entry_price = price if price else getattr(signal, 'price', 0)
            strategy = getattr(signal, 'strategy', 'manual')
            
            # 检查是否已存在
            cursor.execute("SELECT id, status FROM trades WHERE id = ?", (trade_id,))
            existing = cursor.fetchone()
            
            if existing:
                existing_status = existing[1] if len(existing) > 1 else 'OPEN'
                if status == 'CLOSED' and existing_status == 'OPEN':
                    cursor.execute("SELECT entry_price, quantity FROM trades WHERE id = ?", (trade_id,))
                    row = cursor.fetchone()
                    _ep = row[0] if row and row[0] and row[0] > 0 else 0
                    _qty = row[1] if row and row[1] else 1

                    # 使用 HK 本地 TradeRecorder 记录平仓 (JSON)
                    try:
                        recorder = hk_trade_recorder.get_trade_recorder()
                        recorder.record_close(
                            trade_id=trade_id,
                            exit_price=price,
                            exit_reason='normal_close',
                        )
                    except Exception as e:
                        logger.debug(f"[HK TradeRecorder] record_close 跳过: {e}")

                    # P2-1 2026-05-08: 同时更新 SQLite trades 表
                    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    cursor.execute("""
                        UPDATE trades
                        SET exit_price = ?, exit_time = ?, pnl = ?,
                            status = 'CLOSED', exit_reason = 'normal_close'
                        WHERE id = ?
                    """, (price, now, pnl, trade_id))
                    conn.commit()
                    logger.info(f"✅ [SQLite trades] 平仓已更新：{trade_id} exit_price={price} pnl={pnl}")

                    pool.return_connection(conn)
                    return
                else:
                    logger.info(f"⏭️  成交已存在：{trade_id}")
                    pool.return_connection(conn)
                    return

            # 新开记录 — 使用 HK 本地 TradeRecorder (JSON)
            # P0 2026-05-10 去重：检查同 order_id 是否已有记录
            rec_trade_id = f"trade_{order_id}"
            try:
                existing_rec = hk_trade_recorder.get_trade_recorder().get_record(rec_trade_id)
                if existing_rec and status == 'OPEN':
                    logger.info(f"⏭️ [HK TradeRecorder] 开仓记录已存在，跳过重复写入：{rec_trade_id}")
                    return  # 已有记录，跳过
            except Exception as _dup_e:
                logger.debug(f"[HK TradeRecorder] 去重检查失败: {_dup_e}")

            side = 'buy' if action == 'BUY' else 'sell'
            trade_rec = hk_trade_recorder.record_trade(
                stock_code=code,
                side=side,
                quantity=quantity,
                price=entry_price,
                strategy_type=strategy,
                strategy=strategy
            )

            # P0 2026-05-10: 修正 trade_id 为与 SQLite 一致的格式
            if trade_rec and hasattr(trade_rec, 'trade_id'):
                trade_rec.trade_id = rec_trade_id
                hk_trade_recorder.get_trade_recorder()._save_records()

            if trade_rec and hasattr(trade_rec, 'id'):
                logger.info(f"✅ [HK TradeRecorder] 交易记录已写入：{trade_rec.id}")

            # P2-1 2026-05-08: 同时写入 SQLite trades 表 (供 EOD 报告/归因链使用)
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            # SELL 方向直接记 CLOSED, BUY 方向记 OPEN
            db_status = 'CLOSED' if status == 'CLOSED' else status
            cursor.execute("""
                INSERT INTO trades (
                    id, signal_id, code, name, action,
                    entry_price, exit_price, quantity,
                    pnl, pnl_percent, fees,
                    strategy, entry_time, exit_time,
                    exit_reason, order_id, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_id, signal_id, code, '', action,
                entry_price, None if db_status == 'OPEN' else price, quantity,
                pnl, 0, 0,
                strategy, now, None if db_status == 'OPEN' else now,
                'normal_close' if db_status == 'CLOSED' else '', order_id, db_status
            ))
            conn.commit()
            logger.info(f"✅ [SQLite trades] 成交已写入：{trade_id} {code} {action} {quantity}股 @ {entry_price} status={db_status}")
            
            pool.return_connection(conn)
            
        except Exception as e:
            logger.error(f"record_trade_to_db 失败: {e}")

