"""
盘前作战计划管理器 (Battle Plan Manager)

职责:
1. 加载 `daily_battle_plan.json`
2. 盘中执行"门禁检查"：
   - 检查标的是否在计划中 (Target List)
   - 检查标的是否在黑名单中 (Restricted List)
   - 检查交易方向是否符合计划
3. 获取计划建议的仓位上限

P1 修复 (2026-04-19): 线程安全保护 (threading.Lock)
"""

import json
import os
import logging
import threading
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass

logger = logging.getLogger("ftseek.pipeline.battle_plan")


@dataclass
class BattlePlan:
    """作战计划数据对象"""
    date: str = ""
    targets: List[Dict] = None
    restricted: List[str] = None
    overall_bias: str = "Neutral"

    def __post_init__(self):
        if self.targets is None:
            self.targets = []
        if self.restricted is None:
            self.restricted = []


class BattlePlanManager:
    """作战计划管理器 (单例模式)"""

    _instance = None
    _plan: BattlePlan = None
    _is_enabled: bool = False
    _lock = threading.Lock()  # P1 修复: 线程安全保护

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(BattlePlanManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def initialize(self, plan_path: str, enabled: bool = True):
        """初始化并加载计划"""
        self._is_enabled = enabled
        if not self._initialized:
            self._load_plan(plan_path)
            self._initialized = True
            logger.info(f"✅ 作战计划管理器已初始化: Enabled={enabled}")
        else:
            # 支持运行时重载
            self._load_plan(plan_path)

    def _load_plan(self, path: str):
        """从文件加载计划 (线程安全)"""
        # P1 修复: 用锁保护写入
        with self._lock:
            if not path or not os.path.exists(path):
                logger.warning(f"⚠️ 作战计划文件不存在: {path}，门禁功能将不生效")
                self._plan = None
                # 模拟期 Fallback: 如果计划缺失，生成一个允许所有 Watchlist 的“万能计划”
                self._generate_sim_fallback(path)
                return

            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                self._plan = BattlePlan(
                    date=data.get('date', ''),
                    targets=data.get('targets', []),
                    restricted=data.get('restricted', []),
                    overall_bias=data.get('overall_bias', 'Neutral')
                )
                logger.info(f"📝 已加载作战计划 ({self._plan.date}): "
                            f"{len(self._plan.targets)} 个目标, "
                            f"{len(self._plan.restricted)} 个受限标的")
            except Exception as e:
                logger.error(f"❌ 加载作战计划失败: {e}")
                self._plan = None

    def refresh_plan_status(self, market_data: Dict[str, Dict]):
        """
        刷新计划状态 (调用 AuctionManager 验证竞价)
        
        Args:
            market_data: 市场数据字典 {symbol: {yesterday_close, adr_change_pct}}
        """
        if not self._plan:
            return

        try:
            from app.pipeline.auction_manager import AuctionManager
            # 使用默认配置，也可以从 config.yaml 读取
            am = AuctionManager() 
            
            # 这里的逻辑是：遍历 plan 中的 targets，检查市场数据，更新 status 和 limit_price
            for target in self._plan.targets:
                sym = target.get("symbol")
                if sym in market_data:
                    data = market_data[sym]
                    y_close = data.get("yesterday_close", 0)
                    adr_pct = data.get("adr_change_pct", 0.0)
                    direction = target.get("direction", "LONG")
                    
                    if y_close > 0:
                        result = am.calculate_auction_price(sym, y_close, adr_pct, direction)
                        
                        # 更新计划对象
                        target["status"] = result.status
                        target["invalidation_reason"] = result.invalidation_reason
                        target["auction_limit_price"] = result.limit_price
                        
                        if result.status != "VALID":
                            logger.warning(f"⚠️ [竞价] 计划失效: {sym} - {result.invalidation_reason}")
                        else:
                            logger.info(f"✅ [竞价] 计划有效: {sym} -> 限价 {result.limit_price}")

        except Exception as e:
            logger.error(f"❌ [竞价] 刷新计划状态失败: {e}")


    def _generate_sim_fallback(self, path: str):
        """生成模拟期兜底计划：允许 Watchlist 中所有标的"""
        try:
            from app import config
            if not hasattr(config, 'WATCH_LIST_HK'):
                return
            
            targets = [{"symbol": s, "direction": "ANY", "status": "VALID"} for s in config.WATCH_LIST_HK]
            self._plan = BattlePlan(date="Simulation", targets=targets, restricted=[])
            logger.warning(f"🤖 [模拟兜底] 已生成万能计划 (包含 {len(targets)} 只标的)")
        except Exception as e:
            logger.error(f"生成模拟兜底计划失败: {e}")

    def check_open_trade(self, symbol: str, direction: str) -> Tuple[bool, str]:
        """
        检查是否允许开新仓 (线程安全)

        Args:
            symbol: 股票代码 (e.g. HK.00700)
            direction: 交易方向 (BUY/SELL)

        Returns:
            (bool, str): (是否允许, 原因)
        """
        # P1 修复: 用锁保护读取，防止与 _load_plan 竞态
        with self._lock:
            plan = self._plan
            is_enabled = self._is_enabled

        if not is_enabled or not plan:
            return True, "作战计划未启用或为空，放行"

        # P0 修复 (2026-04-23): 计划存在但 targets 为空 → 降级放行
        # 原因：盘前LLM未生成可交易目标时不应阻止所有BUY信号
        if not plan.targets:
            logger.warning(f"⚠️ [作战计划门禁] 今日计划 targets 为空，降级放行 | bias={plan.overall_bias}")
            return True, "⚠️ 计划targets为空，降级放行"

        # 1. 检查黑名单
        if symbol in plan.restricted:
            return False, f"🚫 [黑名单拦截] 标的 {symbol} 在禁止交易名单中"

        # 2. 检查白名单 (Target List)
        target = next((t for t in plan.targets if t.get('symbol') == symbol), None)

        if not target:
            return False, f"🚫 [计划外拦截] 标的 {symbol} 不在今日作战计划中"

        # Phase 4: 检查计划状态 (VALID / INVALID)
        status = target.get('status', 'UNKNOWN')
        if status == 'INVALID':
            reason = target.get('invalidation_reason', '计划失效')
            return False, f"🚫 [计划失效] 标的 {symbol} 竞价验证失败: {reason}"
        elif status == 'CAUTION':
            logger.warning(f"⚠️ [竞价] 标的 {symbol} 处于 CAUTION 状态，风险较高")

        # 3. 检查方向
        # 计划建议 LONG，实际信号 SELL -> 拦截 (反向做空通常风险更大)
        # 允许方向匹配 或 计划标记为 "ANY"

        # 统一方向映射: BUY/OPEN -> LONG, SELL/SHORT -> SHORT
        trade_direction_normalized = "LONG" if direction == "BUY" else "SHORT"

        plan_direction = target.get('direction', '').upper()

        if plan_direction != 'ANY' and plan_direction != trade_direction_normalized:
            # 特殊处理: 计划说 BUY (做多)，实际信号说 SELL (可能是技术面看跌)
            # 这种背离应该由 L1 决策层处理，或者至少是强警告
            # 为了安全，默认拦截，除非明确允许
            return False, f"🚫 [方向背离] 计划建议 {plan_direction}，当前信号 {trade_direction}"

        return True, "✅ 符合计划"

    def get_max_position_pct(self, symbol: str) -> float:
        """获取计划建议的最大仓位比例"""
        if not self._plan:
            return 0.08  # 默认 8%

        target = next((t for t in self._plan.targets if t.get('symbol') == symbol), None)
        if target:
            return float(target.get('max_position_pct', 0.08))
        
        return 0.05  # 非计划标的（如果允许交易）给更低仓位

    def get_urgency(self, symbol: str) -> str:
        """获取标的紧急度"""
        if not self._plan:
            return "Low"
        
        target = next((t for t in self._plan.targets if t.get('symbol') == symbol), None)
        if target:
            return target.get('urgency', 'Medium')
        return "Low"
