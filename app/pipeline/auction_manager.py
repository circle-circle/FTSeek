"""
竞价单管理与计划失效机制 (Auction Manager & Plan Invalidation)

职责:
1. 根据 ADR 表现、期货走势、昨日收盘价，计算智能竞价限价 (Smart Limit Price)。
2. 检查开盘跳空幅度 (Gap Check)，如果偏离计划过大 (如 > 3%)，标记计划失效。
3. 保护机制：如果计划方向与市场跳空方向相反 (如计划做多，但低开 -5%)，自动废除计划。

特点:
- 动态定价 (非固定昨收比例)
- 风险前置 (开盘前/竞价期拦截)
- 自动失效 (Plan Invalidation)
"""

import json
import logging
import os
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger("ftseek.pipeline.auction")

# 默认配置
DEFAULT_CONFIG = {
    "max_gap_pct": 0.03,           # 最大允许跳空幅度 (3%)
    "adr_correlation": 0.8,        # ADR 传导系数 (0.8 表示 ADR 涨跌 10%，港股预期涨跌 8%)
    "limit_buffer_pct": 0.005,     # 竞价限价安全垫 (0.5%)
    "invalidation_reasons": [
        "跳空幅度过大",
        "方向与计划背离",
        "触及计划止损"
    ]
}

@dataclass
class AuctionResult:
    symbol: str
    status: str          # VALID, INVALID, CAUTION
    limit_price: float   # 建议竞价限价
    invalidation_reason: str = ""

class AuctionManager:
    """竞价单管理器"""

    def __init__(self, config: Dict = None):
        self.config = config or DEFAULT_CONFIG
        self.max_gap = self.config.get("max_gap_pct", 0.03)
        self.adr_corr = self.config.get("adr_correlation", 0.8)
        self.buffer = self.config.get("limit_buffer_pct", 0.005)

    def calculate_auction_price(self, symbol: str, yesterday_close: float, 
                                adr_change_pct: float = 0.0, 
                                plan_direction: str = "LONG") -> AuctionResult:
        """
        计算智能竞价价格
        
        Args:
            symbol: 股票代码
            yesterday_close: 昨日收盘价
            adr_change_pct: 对应 ADR 的涨跌幅 (如 +0.05 表示涨 5%)
            plan_direction: 计划方向 (LONG/SHORT)
            
        Returns:
            AuctionResult: 包含建议价格和状态
        """
        # 1. 预估开盘价 (基于 ADR 传导)
        # 预估开盘价 = 昨收 * (1 + ADR 涨跌幅 * 传导系数)
        estimated_open = yesterday_close * (1 + (adr_change_pct * self.adr_corr))
        
        # 2. 计算跳空幅度
        gap_pct = (estimated_open - yesterday_close) / yesterday_close
        
        # 3. 检查跳空风险
        if abs(gap_pct) > self.max_gap:
            return AuctionResult(
                symbol=symbol,
                status="INVALID",
                limit_price=0.0,
                invalidation_reason=f"跳空幅度过大 ({gap_pct:.1%} > {self.max_gap:.0%})"
            )
        
        # 4. 检查方向背离 (计划做多，但大幅低开)
        if plan_direction == "LONG" and gap_pct < -0.01: # 低开超过 1%
            return AuctionResult(
                symbol=symbol,
                status="CAUTION", # 警告，不直接无效，但可能不执行
                limit_price=0.0,
                invalidation_reason=f"方向背离 (计划做多，低开 {gap_pct:.1%})"
            )
            
        if plan_direction == "SHORT" and gap_pct > 0.01: # 高开超过 1%
             return AuctionResult(
                symbol=symbol,
                status="CAUTION",
                limit_price=0.0,
                invalidation_reason=f"方向背离 (计划做空，高开 {gap_pct:.1%})"
            )

        # 5. 计算竞价限价 (带安全垫)
        # 做多：在预估开盘价下方一点挂单 (防止买在高点)
        # 做空：在预估开盘价上方一点挂单
        if plan_direction == "LONG":
            limit_price = estimated_open * (1 - self.buffer)
        else:
            limit_price = estimated_open * (1 + self.buffer)
            
        # 确保价格为正
        limit_price = max(limit_price, 0.01)
        
        return AuctionResult(
            symbol=symbol,
            status="VALID",
            limit_price=round(limit_price, 3),
            invalidation_reason=""
        )

    def validate_plan(self, plan_path: str, market_data: Dict[str, Dict]) -> Tuple[bool, List[AuctionResult]]:
        """
        验证整个作战计划
        
        Args:
            plan_path: daily_battle_plan.json 路径
            market_data: 实时市场数据 {symbol: {yesterday_close, adr_change_pct, ...}}
            
        Returns:
            (PlanValid, List of Results)
        """
        if not os.path.exists(plan_path):
            logger.warning("作战计划文件不存在，无法验证")
            return False, []
            
        try:
            with open(plan_path, 'r') as f:
                plan = json.load(f)
        except Exception as e:
            logger.error(f"读取作战计划失败: {e}")
            return False, []

        results = []
        all_valid = True

        for target in plan.get("targets", []):
            sym = target["symbol"]
            direction = target.get("direction", "LONG")
            
            # 获取市场数据
            data = market_data.get(sym, {})
            y_close = data.get("yesterday_close", 0)
            adr_pct = data.get("adr_change_pct", 0.0)
            
            if y_close > 0:
                result = self.calculate_auction_price(sym, y_close, adr_pct, direction)
                results.append(result)
                
                # 如果无效，标记计划失效
                if result.status == "INVALID":
                    all_valid = False
                    target["status"] = "INVALID"
                    target["invalidation_reason"] = result.invalidation_reason
                    logger.warning(f"⛔ 计划失效: {sym} - {result.invalidation_reason}")
                elif result.status == "VALID":
                    # 更新计划中的建议价格
                    target["status"] = "VALID"
                    target["auction_limit_price"] = result.limit_price
                    logger.info(f"✅ 竞价定价: {sym} -> {result.limit_price}")
        
        # 保存更新后的计划
        if results:
            try:
                with open(plan_path, 'w') as f:
                    json.dump(plan, f, indent=2, ensure_ascii=False)
                logger.info("📝 作战计划已更新竞价状态")
            except Exception as e:
                logger.error(f"保存作战计划失败: {e}")

        return all_valid, results
