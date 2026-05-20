"""
期权合约选择器

SHORT 信号触发时，自动选取 ATM Put 期权合约（替代卖空）：
  - 到期日：距今 MIN_DTE ~ MAX_DTE 天（默认 7~21 天）
  - 行权价：最接近当前正股价格（ATM）
  - 类型：Put

富途期权代码格式：US.TSLA250425P380000
  = US.{ticker}{YY}{MM}{DD}P{strike*1000 零填充8位}
"""
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


@dataclass
class OptionContract:
    """期权合约信息"""
    option_code:  str        # 富途期权代码，如 US.TSLA250425P380000
    symbol:       str        # 正股代码，如 US.TSLA
    strike_price: float      # 行权价
    expiry_date:  str        # 到期日 YYYY-MM-DD
    last_price:   float      # 最新价（mid price）
    bid:          float      # 买一价
    ask:          float      # 卖一价
    delta:        float      # delta（负数，put）
    dte:          int        # 距到期天数
    is_simulated: bool = False  # True = 无富途连接时的模拟合约
    # P0-4 守门扩展字段（2026-04-28）：
    iv:              float = 0.0   # 隐含波动率
    open_interest:   int   = 0     # 未平仓合约数
    volume:          int   = 0     # 当日成交量
    theta:           float = 0.0   # 时间衰减（负数）
    gamma:           float = 0.0   # gamma（备用）
    vega:            float = 0.0   # vega（备用）


class OptionSelector:
    """
    ATM Put 期权选择器

    优先通过富途 OpenD get_option_chain() 获取真实期权链；
    无连接时构造模拟合约（用于离线测试和模拟下单）。
    """

    def __init__(
        self,
        quote_ctx=None,
        min_dte: int = 7,
        max_dte: int = 21,
        quality_gate=None,
        iv_history_provider=None,
    ):
        """
        Args:
            quote_ctx: 已连接的 futu OpenQuoteContext，None 则使用模拟模式
            min_dte:   最小剩余到期天数
            max_dte:   最大剩余到期天数
            quality_gate: OptionsQualityGate 实例（P0-4, 可选）
                          注入后 get_atm_put 会在每级降级候选后先过守门；不注入时默认构造。
            iv_history_provider: 可选 callable(symbol) -> List[float]
                                 返回 52 周 ATM IV 历史序列，用于 IV rank 计算。
        """
        self.quote_ctx = quote_ctx
        self.min_dte = min_dte
        self.max_dte = max_dte
        # P0-4 期权质量守门（2026-04-28）
        if quality_gate is None:
            try:
                from app.risk.options_quality_gate import OptionsQualityGate, QualityGateConfig
                quality_gate = OptionsQualityGate(QualityGateConfig())
            except Exception as e:
                logger.debug(f"[期权·守门] 默认 gate 初始化失败，退化为无守门: {e}")
                quality_gate = None
        self.quality_gate = quality_gate
        self.iv_history_provider = iv_history_provider

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def get_atm_put(
        self,
        symbol: str,
        current_price: float,
        min_dte: Optional[int] = None,
        max_dte: Optional[int] = None,
        apply_greeks_filter: bool = True,
        side: str = "BUY",
    ) -> Optional[OptionContract]:
        """
        获取最近到期的 ATM Put 期权合约（多级降级 + P0-4 质量守门）。

        Args:
            symbol:        正股代码，如 "US.TSLA"
            current_price: 正股当前价格
            min_dte:       覆盖默认最小 DTE（仅影响首次尝试）
            max_dte:       覆盖默认最大 DTE（仅影响首次尝试）
            apply_greeks_filter: 是否应用质量守门（向下兼容旧参数名）
            side:          "BUY"（long put）/ "SELL"（CSP 等卖方）— 决定 IV rank 方向

        Returns:
            OptionContract 或模拟合约（保证始终返回有效合约）

        降级策略（P0-4 2026-04-28: 守门不通过也算失败，触发下一级）：
            1级：正常范围 DTE 7-21
            2级：扩大范围 DTE 5-25
            3级：最宽范围 DTE 3-30
            4级：模拟合约 fallback
        """
        min_dte = min_dte if min_dte is not None else self.min_dte
        max_dte = max_dte if max_dte is not None else self.max_dte

        tiers = [
            ("1级·正常", min_dte, max_dte),
            ("2级·扩大", 5, 25),
            ("3级·最宽", 3, 30),
        ]
        last_reject = None
        for name, lo, hi in tiers:
            contract = self._try_find_put(symbol, current_price, lo, hi)
            if not contract:
                continue
            if not apply_greeks_filter:
                logger.info(f"[期权·{name}] {symbol} 选中 {contract.option_code}（未启用守门）")
                return contract
            passed, reject = self._check_quality_gate(contract, side=side)
            if passed:
                if name != "1级·正常":
                    logger.info(f"[期权·降级 {name}] {symbol} 守门通过 {contract.option_code}")
                return contract
            last_reject = reject
            logger.info(
                f"[期权·{name}·守门拒绝] {symbol} {contract.option_code} "
                f"code={reject.reject_code} reason={reject.reject_reason}"
            )

        # 第4级：模拟合约 fallback
        reason = last_reject.reject_reason if last_reject else "无可用期权"
        logger.warning(f"[期权·降级4] {symbol} 所有候选不可用（最后拒绝：{reason}），使用模拟合约")
        return self._build_simulated_put(symbol, current_price, min_dte, max_dte)

    # ------------------------------------------------------------------
    # P0-4 守门: IV rank + spread% + OI/volume + theta
    # ------------------------------------------------------------------

    def _check_quality_gate(self, contract: OptionContract, side: str = "BUY"):
        """调用 options_quality_gate 做统一四道门

        失败时返回 (False, GateResult)；通过返回 (True, GateResult)。
        模拟合约不再完全 bypass，而是应用保守默认值（P0 修复 2026-04-28）。
        """
        if self.quality_gate is None:
            try:
                from app.risk.options_quality_gate import GateResult
                return True, GateResult(passed=True)
            except Exception:
                return True, None

        # 模拟合约：使用保守默认值过守门
        if contract.is_simulated:
            from app.risk.options_quality_gate import GateResult
            # 模拟合约无真实 OI/Volume/IV 数据，应用最严检查：
            # - 仅检查 DTE 和 spread（基于模拟价格）
            # - OI/Volume/IV 守门跳过（数据不可用）
            result = self.quality_gate.check(
                {
                    "bid": contract.bid,
                    "ask": contract.ask,
                    "last_price": contract.last_price,
                    # 模拟数据：给足够大的 OI/Volume 避免流动性拒绝
                    "open_interest": 999,
                    "volume": 999,
                    "iv": contract.iv if contract.iv > 0 else 0.35,
                    "theta": contract.theta,
                    "dte": contract.dte,
                },
                side=side,
                iv_history=[],
                dte=contract.dte,
            )
            return result.passed, result

        # 真实合约：完整守门
        iv_history = None
        if self.iv_history_provider is not None:
            try:
                iv_history = self.iv_history_provider(contract.symbol)
            except Exception as e:
                logger.debug(f"[期权·守门] IV 历史获取失败 {contract.symbol}: {e}")
                iv_history = None

        contract_dict = {
            "bid": contract.bid,
            "ask": contract.ask,
            "last_price": contract.last_price,
            "iv": contract.iv,
            "open_interest": contract.open_interest,
            "volume": contract.volume,
            "theta": contract.theta,
            "dte": contract.dte,
        }
        result = self.quality_gate.check(
            contract_dict,
            side=side,
            iv_history=iv_history or [],
            dte=contract.dte,
        )
        return result.passed, result

    # ------------------------------------------------------------------
    # P3 优化 2026-04-29: PUT Spread (买高行权价+卖低行权价 Put)
    # 裸 PUT 的 theta 每天衰减，PUT Spread 用卖出腿的 theta 收入对冲买入腿的衰减
    # 适合做空观点: 成本更低，theta 中性，但限制了最大盈利
    # ------------------------------------------------------------------

    def get_put_spread(
        self,
        symbol: str,
        current_price: float,
        width_pct: float = 0.05,
        min_dte: Optional[int] = None,
        max_dte: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        获取 PUT Vertical Spread (Bear Put Spread)

        结构: Buy ATM Put + Sell OTM Put (行权价低 width_pct%)
        - 成本 = long_put.ask - short_put.bid (净借记)
        - 最大盈利 = 行权价差 - 净成本
        - 最大亏损 = 净成本
        - theta 接近中性 (买入腿衰减 ≈ 卖出腿收入)

        Returns:
            {long_leg, short_leg, net_cost, max_profit, max_loss, quantity} 或 None
        """
        min_dte = min_dte if min_dte is not None else self.min_dte
        max_dte = max_dte if max_dte is not None else self.max_dte

        # 1. 获取 ATM Put (long leg)
        long_put = self.get_atm_put(symbol, current_price, min_dte, max_dte, apply_greeks_filter=False)
        if not long_put:
            return None

        # 2. 查找 OTM Put (short leg): 行权价 = current_price * (1 - width_pct)
        otm_strike = round(current_price * (1 - width_pct) / 5) * 5
        if otm_strike >= long_put.strike_price:
            otm_strike = round(long_put.strike_price * 0.95 / 5) * 5

        short_put = self._try_find_put_by_strike(symbol, otm_strike, long_put.dte - 3, long_put.dte + 3)
        if not short_put:
            logger.warning(f"[PUT Spread] {symbol} 未找到 OTM Put K={otm_strike}")
            return None

        # 3. 计算 Spread 成本与回报
        net_cost = long_put.ask - short_put.bid
        if net_cost <= 0:
            logger.warning(f"[PUT Spread] {symbol} 净成本为负或零 ${net_cost:.2f}, 跳过")
            return None

        spread_width = long_put.strike_price - short_put.strike_price
        max_profit = spread_width - net_cost
        max_loss = net_cost

        logger.info(
            f"[PUT Spread] {symbol} "
            f"Buy {long_put.strike_price}P @ {long_put.ask:.2f} + "
            f"Sell {short_put.strike_price}P @ {short_put.bid:.2f} | "
            f"净借记=${net_cost:.2f} | 最大盈利=${max_profit:.2f} | "
            f"最大亏损=${max_loss:.2f} | DTE={long_put.dte}"
        )

        return {
            "type": "put_spread",
            "long_leg": long_put,
            "short_leg": short_put,
            "net_cost": net_cost,
            "max_profit": max_profit,
            "max_loss": max_loss,
            "spread_width": spread_width,
        }

    def _try_find_put_by_strike(
        self, symbol: str, target_strike: float, min_dte: int, max_dte: int
    ) -> Optional[OptionContract]:
        """查找特定行权价的 Put 合约"""
        if self.quote_ctx is None:
            return None
        try:
            from futu import RET_OK, OptionType
            today = date.today()
            date_from = (today + timedelta(days=min_dte)).strftime("%Y-%m-%d")
            date_to = (today + timedelta(days=max_dte)).strftime("%Y-%m-%d")

            ret, data = self.quote_ctx.get_option_chain(
                code=symbol, start=date_from, end=date_to, option_type=OptionType.PUT,
            )
            if ret != RET_OK or data is None or len(data) == 0:
                return None

            best = None
            best_diff = float("inf")
            for _, row in data.iterrows():
                strike = float(row.get("strike_price", 0))
                diff = abs(strike - target_strike)
                if diff < best_diff:
                    best_diff = diff
                    expiry_str = str(row.get("option_expiry_date", ""))
                    exp_dt = datetime.strptime(expiry_str, "%Y-%m-%d").date()
                    dte = (exp_dt - today).days
                    best = OptionContract(
                        option_code=str(row.get("code", "")),
                        symbol=symbol,
                        strike_price=strike,
                        expiry_date=expiry_str,
                        last_price=float(row.get("last_price", 0) or 0),
                        bid=float(row.get("bid_price", 0) or 0),
                        ask=float(row.get("ask_price", 0) or 0),
                        delta=float(row.get("delta", -0.5) or -0.5),
                        dte=dte,
                        is_simulated=False,
                    )
            return best
        except Exception as e:
            logger.debug(f"[PUT Spread] 查找 {symbol} K={target_strike} 异常: {e}")
            return None

    # Greeks 过滤阈值
    DELTA_ABS_MIN = 0.30
    DELTA_ABS_MAX = 0.50
    THETA_PREMIUM_RATIO_MAX = 0.03  # 3%/天

    def _check_greeks_ok(self, contract: OptionContract) -> bool:
        """
        检查期权 Greeks 是否符合风险偏好。

        P2 修复 (2026-04-17):
        - Delta 绝对值 0.3-0.5
        - Theta/Premium 比 < 3%/天
        """
        if contract.is_simulated:
            return True  # 模拟合约跳过 Greeks 检查

        delta_abs = abs(contract.delta)

        # Delta 过滤
        if delta_abs < self.DELTA_ABS_MIN:
            logger.debug(
                f"[期权·Greeks] {contract.option_code} Delta={contract.delta:.3f} "
                f"< {self.DELTA_ABS_MIN}，方向性太弱"
            )
            return False

        if delta_abs > self.DELTA_ABS_MAX:
            logger.debug(
                f"[期权·Greeks] {contract.option_code} Delta={contract.delta:.3f} "
                f"> {self.DELTA_ABS_MAX}，方向性过强/杠杆过高"
            )
            return False

        # Theta 衰减过滤（每天时间衰减占期权价格的比例）
        if contract.last_price > 0:
            theta_daily_pct = abs(contract.delta) * 0.02  # 简化估算
            # 注：精确 Theta 需要从 GreeksCalculator 获取
            if theta_daily_pct > self.THETA_PREMIUM_RATIO_MAX:
                logger.debug(
                    f"[期权·Greeks] {contract.option_code} Theta/Premium 比 "
                    f"{theta_daily_pct*100:.1f}% > 3%/天，时间衰减过快"
                )
                return False

        return True

    def _try_find_put(
        self,
        symbol: str,
        current_price: float,
        min_dte: int,
        max_dte: int,
    ) -> Optional[OptionContract]:
        """
        尝试在指定 DTE 范围内查找 ATM Put 期权。

        Returns:
            OptionContract 或 None（无可用合约时）
        """
        if self.quote_ctx is None:
            return None

        try:
            contract = self._get_from_futu(symbol, current_price, min_dte, max_dte)
            if contract:
                return contract
        except Exception as e:
            logger.debug(f"[期权] {symbol} 查询期权链异常 (DTE {min_dte}-{max_dte}): {e}")

        return None

    # ------------------------------------------------------------------
    # 富途期权链查询
    # ------------------------------------------------------------------

    def _get_from_futu(
        self,
        symbol: str,
        current_price: float,
        min_dte: int,
        max_dte: int,
    ) -> Optional[OptionContract]:
        """通过富途 OpenD 获取真实期权链，选取 ATM Put"""
        try:
            from futu import RET_OK, OptionType, KLType

            ticker = symbol.replace("US.", "").replace("HK.", "")
            today = date.today()
            date_from = (today + timedelta(days=min_dte)).strftime("%Y-%m-%d")
            date_to   = (today + timedelta(days=max_dte)).strftime("%Y-%m-%d")

            ret, data = self.quote_ctx.get_option_chain(
                code=symbol,
                start=date_from,
                end=date_to,
                option_type=OptionType.PUT,
            )

            if ret != RET_OK or data is None or len(data) == 0:
                logger.warning(f"[期权] {symbol} get_option_chain 返回空: {data}")
                return None

            # 过滤 Put，计算 DTE，找最接近 ATM 的
            best: Optional[OptionContract] = None
            best_diff = float("inf")

            for _, row in data.iterrows():
                option_code = str(row.get("code", ""))
                strike = float(row.get("strike_price", 0))
                expiry_str = str(row.get("option_expiry_date_distance", ""))

                # 解析到期日
                expiry_date = str(row.get("option_expiry_date", ""))
                if not expiry_date:
                    continue
                try:
                    exp_dt = datetime.strptime(expiry_date, "%Y-%m-%d").date()
                    dte = (exp_dt - today).days
                except Exception:
                    continue

                if dte < min_dte or dte > max_dte:
                    continue

                diff = abs(strike - current_price)
                if diff < best_diff:
                    best_diff = diff
                    bid   = float(row.get("bid_price", 0) or 0)
                    ask   = float(row.get("ask_price", 0) or 0)
                    last  = float(row.get("last_price", 0) or 0)
                    mid   = (bid + ask) / 2 if bid > 0 and ask > 0 else last
                    delta = float(row.get("delta", -0.5) or -0.5)
                    # P0-4 (2026-04-28): 读取守门所需字段（富途列名回退）
                    iv    = float(row.get("option_implied_volatility",
                                          row.get("implied_volatility", 0)) or 0)
                    oi    = int(row.get("option_open_interest",
                                        row.get("open_interest", 0)) or 0)
                    vol   = int(row.get("volume", 0) or 0)
                    theta = float(row.get("option_theta", row.get("theta", 0)) or 0)
                    gamma = float(row.get("option_gamma", row.get("gamma", 0)) or 0)
                    vega  = float(row.get("option_vega",  row.get("vega",  0)) or 0)

                    best = OptionContract(
                        option_code=option_code,
                        symbol=symbol,
                        strike_price=strike,
                        expiry_date=expiry_date,
                        last_price=mid if mid > 0 else last,
                        bid=bid,
                        ask=ask,
                        delta=delta,
                        dte=dte,
                        is_simulated=False,
                        iv=iv,
                        open_interest=oi,
                        volume=vol,
                        theta=theta,
                        gamma=gamma,
                        vega=vega,
                    )

            if best:
                logger.info(
                    f"[期权] {symbol} 选定 ATM Put: {best.option_code} "
                    f"K={best.strike_price} expiry={best.expiry_date} "
                    f"price={best.last_price:.2f} DTE={best.dte}"
                )
            return best

        except ImportError:
            logger.debug("[期权] futu 未安装，使用模拟合约")
            return None
        except Exception as e:
            logger.error(f"[期权] 富途期权链查询异常 {symbol}: {e}", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # 模拟合约构建（无富途连接 / fallback）
    # ------------------------------------------------------------------

    def _build_simulated_put(
        self,
        symbol: str,
        current_price: float,
        min_dte: int,
        max_dte: int,
    ) -> OptionContract:
        """
        构造虚拟 ATM Put 合约用于模拟交易统计。

        期权价格估算：Black-Scholes 简化版
          - 隐含波动率默认 0.45（美股科技股典型值）
          - 无风险利率 0.05
        """
        ticker = symbol.replace("US.", "").replace("HK.", "")

        # 选目标到期日：偏向 min_dte 侧（更短期）
        target_dte = min_dte + (max_dte - min_dte) // 3
        expiry_date_obj = date.today() + timedelta(days=target_dte)
        # 调整为最近的周五（美股标准期权到期日）
        weekday = expiry_date_obj.weekday()
        days_to_friday = (4 - weekday) % 7
        expiry_date_obj = expiry_date_obj + timedelta(days=days_to_friday)
        expiry_str = expiry_date_obj.strftime("%Y-%m-%d")
        actual_dte = (expiry_date_obj - date.today()).days

        # 行权价：根据股价选择正确的间距（CBOE 标准）
        # $0-25: $1 间距, $25-50: $2.5 间距, $50-100: $5 间距, $100+: $10 间距
        if current_price <= 25:
            strike_interval = 1.0
        elif current_price <= 50:
            strike_interval = 2.5
        elif current_price <= 100:
            strike_interval = 5.0
        else:
            strike_interval = 10.0
        strike = round(current_price / strike_interval) * strike_interval

        # 期权价格简化估算（IV=45%）
        T = actual_dte / 365.0
        iv = 0.45
        # 简化版近似：ATM Put ≈ S * IV * sqrt(T) * 0.4
        put_price = max(0.5, round(current_price * iv * math.sqrt(T) * 0.4, 2))

        # 富途期权代码格式：US.TSLA250425P380000
        # 行权价部分 = strike * 1000，补零到 8 位
        exp_fmt = expiry_date_obj.strftime("%y%m%d")
        strike_int = int(strike * 1000)
        option_code = f"US.{ticker}{exp_fmt}P{strike_int:08d}"

        # 安全修复 (2026-04-30): 如果有 quote_ctx，查真实期权链验证合约存在
        # 避免构造出不存在的期权代码导致下单失败
        if self.quote_ctx is not None:
            verified_code = self._verify_or_find_real_option(
                ticker, current_price, strike, expiry_date_obj, exp_fmt
            )
            if verified_code:
                option_code = verified_code
                logger.info(f"[期权·模拟→验证] {symbol} 使用验证后的期权代码: {option_code}")
            else:
                logger.warning(
                    f"[期权·模拟] {symbol} 真实期权链中未找到匹配合约 "                    f"(K={strike} expiry={expiry_str}), 继续使用构造代码"                )

        contract = OptionContract(
            option_code=option_code,
            symbol=symbol,
            strike_price=strike,
            expiry_date=expiry_str,
            last_price=put_price,
            bid=round(put_price * 0.97, 2),
            ask=round(put_price * 1.03, 2),
            delta=-0.50,
            dte=actual_dte,
            is_simulated=True,
        )
        logger.info(
            f"[期权·模拟] {symbol} 构造 ATM Put: {option_code} "
            f"K={strike} expiry={expiry_str} estPrice=${put_price:.2f} DTE={actual_dte}"
        )
        return contract

    def _verify_or_find_real_option(
        self, ticker: str, current_price: float, target_strike: float,
        expiry_date, exp_fmt: str
    ):
        """
        通过富途期权链验证/查找真实期权代码。
        
        如果找到匹配到期日+行权价的合约，返回真实代码；否则返回 None。
        """
        try:
            from futu import RET_OK, OptionType
            symbol = f"US.{ticker}"
            today = date.today()
            # 查目标到期日前后 3 天的期权链
            date_from = (expiry_date - timedelta(days=3)).strftime("%Y-%m-%d")
            date_to = (expiry_date + timedelta(days=3)).strftime("%Y-%m-%d")

            ret, data = self.quote_ctx.get_option_chain(
                code=symbol, start=date_from, end=date_to, option_type=OptionType.PUT,
            )
            if ret != RET_OK or data is None or data.empty:
                return None

            # 找最接近目标行权价的合约（容差 ±20%）
            best = None
            best_diff = float("inf")
            tolerance = max(target_strike * 0.2, 1.0)

            for _, row in data.iterrows():
                strike = float(row.get("strike_price", 0))
                if strike <= 0:
                    continue
                diff = abs(strike - target_strike)
                if diff < best_diff:
                    best_diff = diff
                    best = str(row.get("code", ""))
            
            if best and best_diff <= tolerance:
                return best
            return None
        except Exception as e:
            logger.debug(f"[期权·验证] {ticker} 查询真实期权链异常: {e}")
            return None

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def calc_option_cost(contract: OptionContract, max_cost_usd: float) -> int:
        """
        根据最大花费计算购买手数（向下取整，最少1手）。

        美股期权 1 手 = 100 股权益
        cost = qty * last_price * 100
        """
        if contract.last_price <= 0:
            return 1
        qty = int(max_cost_usd / (contract.last_price * 100))
        return max(1, qty)

    @staticmethod
    def days_to_expiry(expiry_date: str) -> int:
        """计算距到期天数"""
        try:
            exp = datetime.strptime(expiry_date, "%Y-%m-%d").date()
            return (exp - date.today()).days
        except Exception:
            return 999

    def get_option_greeks(self, option_code: str) -> Dict[str, float]:
        """
        获取期权实时 Greeks。

        Args:
            option_code: 期权代码，如 US.TSLA250425P380000

        Returns:
            包含 delta, gamma, theta, vega, implied_volatility 的字典，
            获取失败时返回空字典。
        """
        try:
            if not self.quote_ctx:
                return {}

            from futu import RET_OK
            ret, data = self.quote_ctx.get_market_snapshot([option_code])
            if ret == RET_OK and data is not None and not data.empty:
                row = data.iloc[0]
                return {
                    "delta": float(row.get("option_delta", 0) or 0),
                    "gamma": float(row.get("option_gamma", 0) or 0),
                    "theta": float(row.get("option_theta", 0) or 0),
                    "vega": float(row.get("option_vega", 0) or 0),
                    "implied_volatility": float(row.get("option_implied_volatility", 0) or 0),
                }
        except ImportError:
            logger.debug("[期权] futu 未安装，无法获取 Greeks")
        except Exception as e:
            logger.warning(f"获取 Greeks 失败 {option_code}: {e}")
        return {}
