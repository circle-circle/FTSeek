"""
做空执行门禁 (Short Selling Execution Gate) — P2 修复 (2026-04-28)

职责: 在做空开仓前进行三层防御检查
1. 账户权限检查: 确认账户类型支持卖空
2. 标的可用性检查: 确认该股票可以卖空 (通过 Futu API 查询)
3. 做空仓位限制: 独立于多头的做空敞口上限

使用:
    gate = ShortSellingGate()
    ok, reason = gate.check(code, short_quantity, market='HK')
    if not ok:
        return TradeResult(success=False, message=reason)

═══ 与 pipeline/short_gate.py 的分工 ═══
本模块 (risk/short_selling_gate.py):
  时机: 订单执行前 (execution-time check)
  数据源: Futu API (账户权限、股票可卖空状态、仓位敞口)
  决策: 做空能否实际执行 → allow / block
  依赖: Futu TradeContext, SQLite positions DB
  环境差异: 模拟盘跳过 Layer 1+2, 实盘全量检查

pipeline/short_gate.py:
  时机: 盘前/盘中的 LLM 分析阶段 (pre-trade decision)
  数据源: LLM 评估结果 (PreMarketAssessment)
  决策: 做空是否值得考虑 → route: auto / review / observe
  依赖: 无 Futu API 调用，纯逻辑判断

两者互补：pipeline gate 回答 "该不该做空", risk gate 回答 "能不能做空"。
"""
import logging
import sqlite3
from typing import Tuple, Optional
from datetime import datetime

logger = logging.getLogger("ftseek.short_gate")

# 港股模拟盘默认账户 ID (需根据实际 Futu 账户调整)
DEFAULT_SIM_ACCOUNT_ID = 18820895


class ShortSellingGate:
    """做空前置门禁 — 三层防御"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or '/opt/ftseek/FTSeek/data/ftseek.db'
        # 做空仓位上限: 做空总敞口不超过账户净值的比例
        self.max_short_exposure_pct = 0.20  # 20%
        # 单票做空上限: 单只股票做空金额不超过净值的比例
        self.max_single_short_pct = 0.08    # 8%

    def check(
        self,
        code: str,
        quantity: int,
        price: float,
        market: str = 'HK',
        trd_ctx=None,
    ) -> Tuple[bool, str]:
        """
        执行三层做空检查

        Args:
            code: 股票代码 (如 HK.00700)
            quantity: 做空数量
            price: 入场价格
            market: 'HK' 或 'US'
            trd_ctx: Futu TradeContext (可选，用于实时查询)

        Returns:
            (bool, str): (是否允许做空, 原因)
        """
        # Layer 0: 检查当前 regime 是否允许做空 (v2.0)
        ok, reason = self._check_regime_allows_short()
        if not ok:
            return False, reason

        # Layer 1: 账户权限检查
        ok, reason = self._check_account_permission(trd_ctx)
        if not ok:
            return False, reason

        # Layer 2: 标的可卖空检查
        ok, reason = self._check_stock_shortable(code, trd_ctx)
        if not ok:
            return False, reason

        # Layer 3: 做空仓位限制
        ok, reason = self._check_short_exposure(code, quantity, price, market)
        if not ok:
            return False, reason

        return True, "做空门禁通过"

    def _check_regime_allows_short(self) -> Tuple[bool, str]:
        """
        Layer 0: 检查 DynamicExitManager 当前 regime 是否配置了做空参数。

        如果当前 regime 未启用做空 (short_enabled=False)，直接拒绝。
        """
        try:
            from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
            short_cfg = _dem.get_short_config()
            if not short_cfg.get('enabled', False):
                return False, (
                    f"当前市场 regime 未启用做空 "
                    f"(regime={_dem._current_regime})"
                )
            logger.debug(
                f"[做空门禁] Layer 0: 做空已启用 "
                f"(借券成本={short_cfg.get('borrow_cost_annual', 0):.1%}年化, "
                f"调整后止盈={_dem.get_short_take_profit_adjusted():.3%})"
            )
            return True, ""
        except Exception as e:
            logger.debug(f"[做空门禁] Layer 0: regime 检查异常 (放行): {e}")
            return True, ""

    def _check_account_permission(self, trd_ctx) -> Tuple[bool, str]:
        """
        Layer 1: 检查账户是否支持卖空

        港股: 模拟盘默认支持自动卖空 (Cash 账户)
        实盘: 需要 Margin 账户 + 开通卖空权限

        注: 此检查在模拟盘中通常通过 (Futu 模拟盘支持卖空)
        但如果 Futu API 返回卖空权限错误，会触发此门禁
        """
        # 模拟盘默认放行 (Futu 模拟盘支持卖空)
        try:
            from app.core.trading_env import get_trading_env
            if get_trading_env().is_simulate:
                logger.debug("[做空门禁] Layer 1: 模拟盘，跳过账户权限检查")
                return True, ""
        except Exception:
            pass

        # 实盘: 需要通过 Futu API 查询账户权限
        if trd_ctx is None:
            # 没有交易上下文，保守放行但记录警告
            logger.warning("[做空门禁] Layer 1: 无 trd_ctx，跳过账户权限检查")
            return True, ""

        try:
            # 查询账户信息
            ret, acc_list = trd_ctx.get_acc_list()
            if ret == 0 and acc_list is not None:
                # 检查账户类型 (需要 trd_market_auth 包含相应权限)
                for _, row in acc_list.iterrows():
                    acc_id = row.get('acc_id', 0)
                    trd_env = row.get('trd_env', 0)
                    # trd_env: 0=real, 1=simulate
                    # 模拟盘直接放行
                    if trd_env == 1:
                        return True, ""
                    # 实盘需要检查账户类型
                    # futu 账户类型: 1=Cash, 2=Margin
                    acc_type = row.get('acc_type', 1)
                    if acc_type == 1:
                        logger.warning(
                            f"[做空门禁] Layer 1: Cash 账户 (acc_id={acc_id}) "
                            f"可能不支持卖空，需确认券商自动卖空权限"
                        )
                        # Cash 账户在港股可能支持自动卖空，但不确定 → 放行但警告
                        return True, ""
                    return True, ""
        except Exception as e:
            logger.warning(f"[做空门禁] Layer 1: 账户权限检查异常 (放行): {e}")

        return True, ""

    def _check_stock_shortable(self, code: str, trd_ctx) -> Tuple[bool, str]:
        """
        Layer 2: 检查指定股票是否可以卖空

        港股: 不是所有股票都能做空，需要通过 Futu API 查询
        使用 get_stock_filter() 或 get_margin_ratio() 确认可卖空状态
        """
        # 模拟盘默认放行
        try:
            from app.core.trading_env import get_trading_env
            if get_trading_env().is_simulate:
                logger.debug(f"[做空门禁] Layer 2: 模拟盘，{code} 跳过可卖空检查")
                return True, ""
        except Exception:
            pass

        if trd_ctx is None:
            logger.warning(f"[做空门禁] Layer 2: 无 trd_ctx，{code} 跳过可卖空检查")
            return True, ""

        try:
            # 使用 Futu API 查询股票是否可以卖空
            from futu import TrdMarket
            from app.utils.futu_limiter import get_global_limiter

            limiter = get_global_limiter()

            # 获取股票代码的 market
            if code.startswith('HK.'):
                market_enum = TrdMarket.HK
            elif code.startswith('US.'):
                market_enum = TrdMarket.US
            else:
                return True, f"未知市场 {code}，跳过可卖空检查"

            # 查询股票过滤条件 (包括可卖空状态)
            if limiter.acquire(timeout=5):
                ret, data = trd_ctx.get_stock_filter(market_enum)
                limiter.record()
            else:
                return True, "频率限制，跳过可卖空检查"

            if ret == 0 and data is not None:
                # 过滤出目标股票
                short_code = code.split('.')[-1] if '.' in code else code
                mask = data['code'] == short_code
                if mask.any():
                    stock_info = data[mask].iloc[0]
                    # 检查是否允许卖空
                    short_enabled = stock_info.get('short_sell_price', None)
                    if short_enabled is not None and short_enabled > 0:
                        logger.debug(f"[做空门禁] Layer 2: {code} 可卖空，卖空价格={short_enabled}")
                        return True, ""
                    else:
                        return False, f"{code} 不支持卖空 (不在可卖空列表中)"

            # 如果查询无结果，保守放行 (可能是 API 限制)
            logger.debug(f"[做空门禁] Layer 2: {code} 查询无结果，保守放行")
            return True, ""

        except Exception as e:
            logger.warning(f"[做空门禁] Layer 2: {code} 可卖空检查异常 (放行): {e}")
            return True, ""

    def _check_short_exposure(
        self, code: str, quantity: int, price: float, market: str
    ) -> Tuple[bool, str]:
        """
        Layer 3: 做空仓位限制

        1. 单票做空金额不超过净值的 max_single_short_pct (默认 8%)
        2. 做空总敞口不超过净值的 max_short_exposure_pct (默认 20%)
        """
        try:
            # 获取当前净值
            net_value = self._get_net_value()
            if net_value is None or net_value <= 0:
                logger.warning(f"[做空门禁] Layer 3: 无法获取净值，跳过仓位检查")
                return True, ""

            short_amount = quantity * price

            # 检查 1: 单票做空上限
            max_single = net_value * self.max_single_short_pct
            if short_amount > max_single:
                return False, (
                    f"单票做空的金额 {short_amount:.0f} 超过上限 {max_single:.0f} "
                    f"(净值 {net_value:.0f} × {self.max_single_short_pct:.0%})"
                )

            # 检查 2: 做空总敞口
            total_short = self._get_total_short_exposure()
            if total_short + short_amount > net_value * self.max_short_exposure_pct:
                max_total = net_value * self.max_short_exposure_pct
                return False, (
                    f"做空总敞口 {total_short + short_amount:.0f} 超过上限 {max_total:.0f} "
                    f"(当前 {total_short:.0f} + 新仓 {short_amount:.0f}, 净值 {net_value:.0f} × {self.max_short_exposure_pct:.0%})"
                )

            logger.debug(
                f"[做空门禁] Layer 3: 仓位检查通过 | "
                f"单票 {short_amount:.0f}/{max_single:.0f}, "
                f"总敞口 {total_short + short_amount:.0f}/{net_value * self.max_short_exposure_pct:.0f}"
            )
            return True, ""

        except Exception as e:
            logger.warning(f"[做空门禁] Layer 3: 仓位检查异常 (放行): {e}")
            return True, ""

    def _get_net_value(self) -> Optional[float]:
        """获取账户净值 (从 DB 或 config)"""
        try:
            from config import config as ftseek_config
            # 优先从 config 读取初始资金作为净值参考
            return getattr(ftseek_config, 'INITIAL_CAPITAL', None) or 1000000.0
        except Exception:
            return None

    def _get_total_short_exposure(self) -> float:
        """
        计算当前做空总敞口

        从 positions 表查询所有 SELL 方向的持仓
        """
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT quantity, entry_price FROM positions "
                "WHERE status='OPEN' AND action='SELL'"
            )
            rows = cursor.fetchall()
            conn.close()
            return sum(qty * price for qty, price in rows if qty and price)
        except Exception as e:
            logger.warning(f"[做空门禁] 查询做空敞口失败: {e}")
            return 0.0
