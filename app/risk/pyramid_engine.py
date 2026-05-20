"""
Livermore 金字塔加仓引擎 (Pyramid Engine)

核心理念:
- "不要在亏损的头寸上补仓，要在盈利的头寸上补仓。"
- "赚大钱的从来不是我的思考，而是我的持仓不动。"

规则:
1. 先建试探仓 (25% 目标仓位)
2. 盈利达到阈值 (5%) 后加仓，每次加仓量递减 (25% → 20% → 15% → 10%)
3. 永不摊平亏损: 亏损头寸绝不加仓
4. 每次加仓后，止损上移到新成本价下方 (保护已实现利润)

作者: FTSeek Team
日期: 2026-04-16
"""

import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger("ftseek.pyramid_engine")


@dataclass
class PyramidLevel:
    """金字塔层级定义"""
    level: int                    # 层级编号 (0=首仓, 1=第一次加仓...)
    position_pct: float          # 该层级占总仓位的比例
    pnl_trigger_pct: float       # 触发加仓的盈利阈值 (相对上一层成本)
    description: str             # 描述


# Livermore 标准金字塔配置
LIVERMORE_PYRAMID = [
    PyramidLevel(0, 0.25, 0.00, "试探仓: 25% 目标仓位, 验证判断"),
    PyramidLevel(1, 0.20, 0.05, "第一次加仓: 盈利 5% 后加 20%, 确认趋势"),
    PyramidLevel(2, 0.15, 0.08, "第二次加仓: 盈利 8% 后加 15%, 趋势加强"),
    PyramidLevel(3, 0.10, 0.12, "第三次加仓: 盈利 12% 后加 10%, 最后冲刺"),
    PyramidLevel(4, 0.10, 0.15, "第四次加仓: 盈利 15% 后加 10%, 满仓"),
]
# 总计: 25+20+15+10+10 = 80% (留 20% 作为现金储备)


class PyramidEngine:
    """
    Livermore 金字塔加仓引擎
    
    职责:
    1. 判断当前持仓是否可以加仓
    2. 计算加仓数量和价格
    3. 更新止损位 (保护利润)
    4. 记录加仓历史
    """
    
    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.pyramid_levels: List[PyramidLevel] = LIVERMORE_PYRAMID
        self.never_average_down = True  # 铁律: 永不摊平亏损
        
        logger.info(f"[金字塔加仓] 初始化 | "
                   f"层级数={len(self.pyramid_levels)} | "
                   f"永不摊平亏损={self.never_average_down}")
    
    def can_add_position(self, position: Any, current_price: float, 
                         max_total_size: float) -> Tuple[bool, str, Dict[str, Any]]:
        """
        判断是否可以加仓
        
        Args:
            position: 当前持仓对象 (Position)
            current_price: 当前价格
            max_total_size: 该股票最大允许总股数
        
        Returns:
            (可以加仓, 原因, 加仓参数)
        """
        # ── 铁律 1: 永不摊平亏损 ──
        if self.never_average_down:
            current_pnl_pct = (current_price - position.avg_entry_price) / position.avg_entry_price
            if current_pnl_pct < 0:
                return False, f"亏损中 (盈亏 {current_pnl_pct*100:.1f}%), 永不摊平", {}
        
        # ── 计算当前已加仓次数 ──
        add_on_count = len(getattr(position, 'add_on_records', []))
        current_level = add_on_count  # 首仓后 level=0, 第一次加仓 level=1
        
        # ── 检查是否还有可加的层级 ──
        if current_level >= len(self.pyramid_levels):
            return False, f"已达最大加仓次数 ({len(self.pyramid_levels)})", {}
        
        # ── 获取下一层级的配置 ──
        next_level = self.pyramid_levels[current_level]
        
        # ── 检查是否满足盈利阈值 ──
        if current_level > 0:
            # 非首仓: 检查相对上一层成本的盈利
            last_add = position.add_on_records[-1]
            last_cost = last_add.get('price', position.avg_entry_price)
            pnl_since_last = (current_price - last_cost) / last_cost
        else:
            # 第一次加仓: 检查相对首仓成本的盈利
            pnl_since_last = (current_price - position.avg_entry_price) / position.avg_entry_price
        
        if pnl_since_last < next_level.pnl_trigger_pct:
            remaining_pct = (next_level.pnl_trigger_pct - pnl_since_last) * 100
            return False, (
                f"盈利不足: 当前 +{pnl_since_last*100:.1f}%, "
                f"需要 +{next_level.pnl_trigger_pct*100:.1f}% (还需 {remaining_pct:.1f}%)"
            ), {}
        
        # ── 计算加仓数量 ──
        add_size = max_total_size * next_level.position_pct
        
        # ── 计算新止损位 (Livermore: 止损上移到新成本价下方) ──
        # 新成本价 = (原成本*原数量 + 新价格*新数量) / (原数量+新数量)
        new_total_size = position.size + add_size
        new_avg_price = (
            position.avg_entry_price * position.size + current_price * add_size
        ) / new_total_size
        new_sl = new_avg_price * 0.95  # 止损设在新成本价下方 5%
        
        add_params = {
            "level": next_level.level,
            "add_size": add_size,
            "add_price": current_price,
            "new_total_size": new_total_size,
            "new_avg_price": new_avg_price,
            "new_sl": new_sl,
            "description": next_level.description,
        }
        
        return True, f"满足加仓条件 (层级 {next_level.level}: {next_level.description})", add_params
    
    def execute_add_on(self, position: Any, add_params: Dict[str, Any], 
                       order_func) -> Dict[str, Any]:
        """
        执行加仓
        
        Args:
            position: 持仓对象
            add_params: can_add_position 返回的加仓参数
            order_func: 下单函数 (symbol, direction, size, price, ...)
        
        Returns:
            加仓结果
        """
        symbol = getattr(position, 'symbol', 'unknown')
        add_size = add_params['add_size']
        add_price = add_params['add_price']
        new_avg_price = add_params['new_avg_price']
        new_sl = add_params['new_sl']
        
        try:
            # 1. 下单
            order_result = order_func(
                symbol=symbol,
                direction="LONG",  # 加仓永远是同方向
                size=add_size,
                price=add_price,
                stop_loss=new_sl,
                is_add_on=True,
                add_on_level=add_params['level'],
            )
            
            if not order_result.get('success', False):
                return {
                    "success": False,
                    "reason": order_result.get('reason', '下单失败'),
                }
            
            # 2. 更新持仓状态
            old_size = position.size
            old_avg = position.avg_entry_price
            
            position.size = add_params['new_total_size']
            position.avg_entry_price = new_avg_price
            position.current_sl = new_sl
            
            # 3. 记录加仓历史
            add_on_record = {
                "level": add_params['level'],
                "time": datetime.now().isoformat(),
                "price": add_price,
                "size": add_size,
                "new_avg_price": new_avg_price,
                "new_sl": new_sl,
                "description": add_params['description'],
            }
            
            if not hasattr(position, 'add_on_records'):
                position.add_on_records = []
            position.add_on_records.append(add_on_record)
            
            # 4. 更新 trail_state (移动止损)
            if hasattr(position, 'trail_state'):
                position.trail_state['peak_price'] = add_price
                position.trail_state['active'] = True
            
            logger.info(
                f"[金字塔加仓] {symbol} 层级 {add_params['level']} 加仓成功: "
                f"加仓 {add_size:.0f} 股 @ ${add_price:.2f} | "
                f"新成本 ${new_avg_price:.2f} | "
                f"新止损 ${new_sl:.2f} | "
                f"总持仓 {position.size:.0f} 股"
            )
            
            return {
                "success": True,
                "add_on_record": add_on_record,
            }
            
        except Exception as e:
            logger.error(f"[金字塔加仓] {symbol} 加仓失败: {e}", exc_info=True)
            return {
                "success": False,
                "reason": str(e),
            }
    
    def get_position_summary(self, position: Any, current_price: float) -> Dict[str, Any]:
        """
        获取持仓金字塔状态摘要
        
        Returns:
            金字塔状态字典
        """
        add_on_count = len(getattr(position, 'add_on_records', []))
        current_level = add_on_count
        total_levels = len(self.pyramid_levels)
        
        current_pnl_pct = (current_price - position.avg_entry_price) / position.avg_entry_price
        
        # 下一级信息
        next_info = None
        if current_level < total_levels:
            next_level = self.pyramid_levels[current_level]
            if current_level > 0 and position.add_on_records:
                last_cost = position.add_on_records[-1].get('price', position.avg_entry_price)
                pnl_since_last = (current_price - last_cost) / last_cost
            else:
                pnl_since_last = current_pnl_pct
            
            next_info = {
                "level": next_level.level,
                "description": next_level.description,
                "position_pct": next_level.position_pct * 100,
                "trigger_pct": next_level.pnl_trigger_pct * 100,
                "current_pnl_since_last": pnl_since_last * 100,
                "ready": pnl_since_last >= next_level.pnl_trigger_pct,
            }
        
        return {
            "symbol": getattr(position, 'symbol', 'unknown'),
            "current_level": current_level,
            "total_levels": total_levels,
            "completion_pct": (current_level + 1) / total_levels * 100,
            "current_pnl_pct": current_pnl_pct * 100,
            "avg_entry_price": position.avg_entry_price,
            "current_price": current_price,
            "current_sl": position.current_sl,
            "next_level": next_info,
            "never_average_down": self.never_average_down,
        }


# 全局单例
_pyramid_instance: Optional[PyramidEngine] = None

def get_pyramid_engine(config: Dict[str, Any] = None) -> PyramidEngine:
    """获取全局金字塔加仓引擎单例"""
    global _pyramid_instance
    if _pyramid_instance is None:
        _pyramid_instance = PyramidEngine(config)
    return _pyramid_instance
