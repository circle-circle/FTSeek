"""
盘前作战计划管理器 (Battle Plan Manager)

职责:
1. 加载 `daily_battle_plan.json`
2. 盘中执行"门禁检查"：
   - 检查标的是否在计划中 (Target List)
   - 检查标的是否在黑名单中 (Restricted List)
   - 检查交易方向是否符合计划
3. 获取计划建议的仓位上限
4. 午间LLM推荐缓存注入与查询

风控层级位置 (见 risk_pipeline.py):
  SmartStopGuard (账户) > CircuitBreaker (系统) > RiskPipeline (信号) > BattlePlan (计划) > DEM (退出)

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
    # 2026-05-11: 午间新增推荐缓存，用于下午交易决策
    _midday_recommendations: Dict[str, str] = {}
    # 2026-05-11 防御修复: 缓存项TTL (2小时)，过期自动失效
    _midday_timestamps: Dict[str, float] = {}

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
            # 2026-05-11: 每日开盘前清空午间缓存，防止残留前日数据
            self.clear_midday_recommendations()
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
            # 使用默认配置，也可以从 unified_config.yaml 读取
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

        # P0 修复 (2026-05-11): 计划存在但 targets 为空 → 严格限制，而非完全放行
        # 原因：盘前LLM未生成可交易目标时，不应无条件放行所有信号。
        # 策略：仅允许 watchlist 前N只 + 提高置信度要求 + 降低仓位上限
        # 2026-05-12 更新: N 从 5→8 (从 SSOT 配置读取)
        if not plan.targets:
            # 从 SSOT 配置读取 watchlist gate 参数
            gate_count = self._get_config("battle_plan.empty_plan.watchlist_gate_count", 8)
            try:
                from app import config
                watch_list = getattr(config, 'WATCH_LIST_HK', [])[:gate_count]
            except Exception:
                watch_list = []

            # P0-3 新增 (2026-05-12): 动态池豁免 — 已在活跃池中的标的允许交易
            exempt_ok, exempt_reason = self._check_active_pool_exemption(symbol)
            if exempt_ok:
                logger.warning(
                    f"⚠️ [作战计划门禁] {symbol} 动态池豁免放行: {exempt_reason}"
                )
                return True, f"⚠️ 动态池豁免: {exempt_reason}"

            if symbol not in watch_list:
                return False, f"🚫 [严格降级] 计划 targets 为空，{symbol} 不在 watchlist 前{gate_count}只内"
            # 要求更高置信度 (通过 RiskPipeline 的 WeakSignalCheck 执行)
            # 降低仓位上限
            logger.warning(
                f"⚠️ [作战计划门禁] 今日计划 targets 为空，严格降级 | "
                f"仅放行 watchlist 前{gate_count}只: {watch_list}, bias={plan.overall_bias}"
            )
            return True, f"⚠️ 计划targets为空，严格降级放行 (仅watchlist前{gate_count}只)"

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
        # WATCH = 仅观察, 实际等同于 ANY (不限制多空), 仅做白名单过滤
        if plan_direction in ('WATCH', 'ANY'):
            pass
        elif plan_direction != trade_direction_normalized:
            return False, f"🚫 [方向背离] 计划建议 {plan_direction}，当前信号 {trade_direction}"

        return True, "✅ 符合计划"

    def check_high_conviction_override(
        self, symbol: str, direction: str,
        consensus_score: float = 0, agree_count: int = 0,
        mtf_alignment: float = 0, volume_ratio: float = 0,
    ) -> Tuple[bool, str]:
        """高置信度计划外通道 — 门槛从 SSOT 配置读取"""
        # 从配置读取阈值 (2026-05-18: 模拟盘宽松调试，降低门槛以验证策略)
        min_score = self._get_config("battle_plan.empty_plan.override_consensus_threshold", 1.0)
        min_agree = self._get_config("battle_plan.empty_plan.override_agree_count", 2)
        min_mtf = self._get_config("battle_plan.empty_plan.override_mtf_alignment", 0.0)
        min_vol = self._get_config("battle_plan.empty_plan.override_volume_ratio", 1.1)

        if consensus_score < min_score:
            return False, f"共识分数{consensus_score:.1f}<{min_score}"
        if agree_count < min_agree:
            return False, f"策略同意数{agree_count}<{min_agree}"
        if mtf_alignment < min_mtf:
            return False, f"MTF对齐度{mtf_alignment:.0%}<{min_mtf:.0%}"
        if volume_ratio < min_vol:
            return False, f"量比{volume_ratio:.1f}x<{min_vol}x"
        return True, "高置信度例外放行"

    def get_override_max_position_pct(self) -> float:
        return 0.03

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

    # ── 午间推荐缓存管理 ─────────────────────────────────────

    def update_midday_recommendations(self, recommendations: Dict[str, str]):
        """更新午间LLM推荐缓存 (带 2小时 TTL)

        Args:
            recommendations: {symbol: action} 映射,
                             action 取值 HOLD|ADD|REDUCE|CLOSE
        """
        import time as _time
        with self._lock:
            self._midday_recommendations.update(recommendations)
            now = _time.time()
            for sym in recommendations:
                self._midday_timestamps[sym] = now
            logger.info(
                f"[午间推荐] 已更新 {len(recommendations)} 只标的缓存: "
                f"{list(recommendations.items())[:5]}"
            )

    def get_midday_recommendation(self, symbol: str) -> Optional[str]:
        """获取标的午间推荐动作 (带 2小时 TTL 自动失效)

        Returns:
            str: HOLD|ADD|REDUCE|CLOSE 或 None (无推荐/已过期)
        """
        import time as _time
        with self._lock:
            action = self._midday_recommendations.get(symbol)
            if action is None:
                return None
            ts = self._midday_timestamps.get(symbol)
            if ts is None:
                return None
            if _time.time() - ts > 7200:  # 2小时 TTL
                logger.warning(
                    f"[午间推荐] {symbol} 缓存已过期 (>{7200}s)，自动失效"
                )
                self._midday_recommendations.pop(symbol, None)
                self._midday_timestamps.pop(symbol, None)
                return None
            return action

    def clear_midday_recommendations(self):
        """清空午间推荐缓存 (每日开盘前调用)"""
        with self._lock:
            self._midday_recommendations.clear()
            self._midday_timestamps.clear()
            logger.info("[午间推荐] 缓存已清空")

    # ── SSOT 配置读取 (2026-05-12 新增) ──────────────────────────

    def _get_config(self, dotted_key: str, default):
        """从 unified_config.yaml SSOT 读取配置点"""
        try:
            from config.config_center import get_config_center
            cc = get_config_center()
            return cc.get(dotted_key, default)
        except Exception as e:
            logger.debug(f"[BattlePlan] SSOT 配置读取失败 {dotted_key}: {e}，使用默认值 {default}")
            return default

    def _check_active_pool_exemption(self, symbol: str) -> Tuple[bool, str]:
        """检查标的是否在动态池中，如果在则允许交易 (2026-05-12 新增)

        当作战计划 targets 为空时，已在动态池 (active pool) 中的标的
        即使不在 plan targets 中，也允许交易 — 避免辩论结论为空时
        所有非 watchlist 前 N 只标的都被拦截。
        """
        enabled = self._get_config("battle_plan.active_pool_exempt.enabled", False)
        if not enabled:
            return False, "动态池豁免未启用"

        try:
            from app.utils.daily_active_pool import get_daily_active_pool
            pool = get_daily_active_pool(market_prefix="HK.")
            active_codes = pool.get_active_codes()
            if symbol in active_codes:
                return True, f"标的 {symbol} 已在动态池中 ({len(active_codes)} 只标的)"
            return False, f"标的 {symbol} 不在动态池中"
        except Exception as e:
            logger.debug(f"[BattlePlan] 动态池豁免检查失败: {e}")
            return False, f"动态池检查异常: {e}"
