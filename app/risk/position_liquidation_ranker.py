#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
仓位清算优先级排序器 (Position Liquidation Ranker)

职责:
在多仓需要协同减仓时，按固定评分矩阵决定先减哪些仓位。
核心原则: 评分函数必须是纯函数 -- 相同输入输出一致，确保组合结构不被误砍。

固定优先级 (权重从高到低):
1. 保证金占用 (40%)  -> 占用越多，越先减
2. 浮亏幅度   (25%)  -> 亏损越大，越先减
3. 波动率     (15%)  -> 波动越高，越先减
4. 非核心逻辑 (10%)  -> EVENT/SCALP > TREND
5. 对冲效果弱 (10%)  -> 孤立仓位优先减，对冲对成对评估
"""

import time
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Set

logger = logging.getLogger("ftseek.risk.liquidation_ranker")


@dataclass
class PositionInfo:
    """仓位信息 (用于清算评分)"""
    code: str
    entry_price: float
    current_price: float
    quantity: int
    profile: str          # TREND, EVENT, SCALP, HEDGE
    market: str           # HK, US
    margin_used: float     # 该仓位占用的保证金
    atr_value: float       # ATR 值
    atr_pct: float         # ATR 占价格比例 (波动率代理)
    is_hedge_leg: bool     # 是否是对冲对的一条腿
    hedge_pair_id: str     # 对冲对 ID (如果是对冲腿)
    entry_reason: str      # 入场原因 (信号/手动/对冲)
    holding_hours: float   # 持仓时长


@dataclass
class LiquidationScore:
    """清算评分结果"""
    position: PositionInfo
    total_score: float
    margin_score: float        # 保证金占用分 (0-40)
    pnl_score: float           # 浮亏分 (0-25)
    volatility_score: float    # 波动分 (0-15)
    profile_score: float       # profile 分 (0-10)
    hedge_score: float         # 对冲效果分 (0-10)
    is_hedge_protected: bool   # 是否因对冲保护而豁免


class PositionLiquidationRanker:
    """
    仓位清算优先级排序器 -- 固定评分矩阵。
    
    用法:
        ranker = PositionLiquidationRanker()
        scores = ranker.rank_positions(positions)
        # 按 score 从高到低排序 -> 先减分数高的
    """
    
    # 固定权重 -- 确保相同输入输出一致
    WEIGHT_MARGIN = 40.0
    WEIGHT_PNL = 25.0
    WEIGHT_VOLATILITY = 15.0
    WEIGHT_PROFILE = 10.0
    WEIGHT_HEDGE = 10.0
    
    # Profile 风险分 (越高风险越大，越先减)
    PROFILE_RISK = {
        "SCALP": 10.0,    # 短线最敏感，先减
        "EVENT": 8.0,     # 事件驱动次之
        "MEAN_REVERSION": 5.0,
        "TREND": 3.0,     # 趋势最稳定，最后减
        "HEDGE": 0.0,     # 对冲保护，不减
    }
    
    def __init__(self):
        self._audit_log: List[Dict] = []
        self._max_audit = 100
        
        logger.info(
            f"仓位清算排序器已启动 | "
            f"权重: 保证金{self.WEIGHT_MARGIN} 浮亏{self.WEIGHT_PNL} "
            f"波动{self.WEIGHT_VOLATILITY} profile{self.WEIGHT_PROFILE} "
            f"对冲{self.WEIGHT_HEDGE}"
        )
    
    def rank_positions(
        self, 
        positions: List[PositionInfo],
        market_context: Optional[Dict] = None,
    ) -> List[LiquidationScore]:
        """
        对仓位列表进行清算优先级排序。
        
        Args:
            positions: 待评估的仓位列表
            market_context: 市场环境信息 (可选，用于调整评分)
            
        Returns:
            按清算优先级从高到低排序的评分列表
        """
        if not positions:
            return []
        
        # 1. 识别对冲对
        hedge_pairs = self._identify_hedge_pairs(positions)
        
        # 2. 计算每个仓位的评分
        scores = []
        for pos in positions:
            score = self._score_position(pos, hedge_pairs, market_context)
            scores.append(score)
        
        # 3. 按总分降序排序 (分数越高，越先减)
        scores.sort(key=lambda s: s.total_score, reverse=True)
        
        # 4. 审计日志
        self._log_audit(scores)
        
        return scores
    
    def _identify_hedge_pairs(self, positions: List[PositionInfo]) -> Dict[str, Set[str]]:
        """
        识别对冲对。
        
        Returns:
            {hedge_pair_id: set_of_codes}
        """
        pairs: Dict[str, Set[str]] = {}
        for pos in positions:
            if pos.is_hedge_leg and pos.hedge_pair_id:
                if pos.hedge_pair_id not in pairs:
                    pairs[pos.hedge_pair_id] = set()
                pairs[pos.hedge_pair_id].add(pos.code)
        
        return pairs
    
    def _score_position(
        self,
        pos: PositionInfo,
        hedge_pairs: Dict[str, Set[str]],
        market_context: Optional[Dict],
    ) -> LiquidationScore:
        """计算单个仓位的清算评分"""
        
        # ── P0: 保证金占用 (40%) ──────────────────────────────
        # 计算该仓位占用的保证金比例
        total_margin = sum(p.margin_used for p in [pos])  # 简化，实际应传入组合总保证金
        if pos.margin_used > 0:
            # 归一化到 0-40 分
            margin_norm = min(1.0, pos.margin_used / max(1, total_margin) * 5)
            margin_score = margin_norm * self.WEIGHT_MARGIN
        else:
            margin_score = 0
        
        # ── P1: 浮亏幅度 (25%) ──────────────────────────────
        if pos.entry_price > 0:
            pnl_pct = (pos.current_price - pos.entry_price) / pos.entry_price
            # 亏损越大分越高，盈利分低
            pnl_norm = min(1.0, max(0, -pnl_pct) / 0.10)  # -10% 归一化为 1.0
            pnl_score = pnl_norm * self.WEIGHT_PNL
        else:
            pnl_score = 0
        
        # ── P2: 波动率 (15%) ────────────────────────────────
        if pos.atr_pct > 0:
            vol_norm = min(1.0, pos.atr_pct / 0.05)  # 5% ATR 归一化为 1.0
            vol_score = vol_norm * self.WEIGHT_VOLATILITY
        else:
            vol_score = 0
        
        # ── P3: 非核心逻辑 (10%) ─────────────────────────────
        profile_risk = self.PROFILE_RISK.get(pos.profile, 5.0)
        profile_score = (profile_risk / 10.0) * self.WEIGHT_PROFILE
        
        # ── P4: 对冲效果 (10%) ───────────────────────────────
        is_hedge_protected = False
        if pos.is_hedge_leg and pos.hedge_pair_id:
            pair_codes = hedge_pairs.get(pos.hedge_pair_id, set())
            if len(pair_codes) >= 2:
                # 对冲对完整，保护
                is_hedge_protected = True
                hedge_score = 0  # 不扣分
            else:
                # 对冲腿孤立，优先减
                hedge_score = self.WEIGHT_HEDGE
        elif pos.profile == "HEDGE":
            # 标记为 HEDGE 但没有配对 -> 优先减
            hedge_score = self.WEIGHT_HEDGE
        else:
            # 普通仓位，中立
            hedge_score = self.WEIGHT_HEDGE * 0.5
        
        # ── 总分 ─────────────────────────────────────────────
        total_score = margin_score + pnl_score + vol_score + profile_score + hedge_score
        
        return LiquidationScore(
            position=pos,
            total_score=round(total_score, 2),
            margin_score=round(margin_score, 2),
            pnl_score=round(pnl_score, 2),
            volatility_score=round(vol_score, 2),
            profile_score=round(profile_score, 2),
            hedge_score=round(hedge_score, 2),
            is_hedge_protected=is_hedge_protected,
        )
    
    def _log_audit(self, scores: List[LiquidationScore]):
        """记录审计日志"""
        log_entry = {
            "timestamp": time.time(),
            "positions_ranked": len(scores),
            "scores": [
                {
                    "code": s.position.code,
                    "total_score": s.total_score,
                    "margin": s.margin_score,
                    "pnl": s.pnl_score,
                    "volatility": s.volatility_score,
                    "profile": s.profile_score,
                    "hedge": s.hedge_score,
                    "hedge_protected": s.is_hedge_protected,
                }
                for s in scores
            ],
        }
        self._audit_log.append(log_entry)
        if len(self._audit_log) > self._max_audit:
            self._audit_log = self._audit_log[-self._max_audit:]
        
        logger.info(
            f"[清算排序] {len(scores)} 个仓位已排序 | "
            f"优先级: {scores[0].position.code if scores else 'N/A'}"
        )
    
    def get_audit_log(self, limit: int = 10) -> List[Dict]:
        """获取审计日志"""
        return self._audit_log[-limit:]
    
    def get_top_liquidation_candidates(
        self, 
        positions: List[PositionInfo],
        count: int = 1,
    ) -> List[LiquidationScore]:
        """
        获取前 N 个最应该被清算的仓位。
        
        Args:
            positions: 仓位列表
            count: 返回数量
            
        Returns:
            前 N 个候选仓位 (评分最高的)
        """
        scores = self.rank_positions(positions)
        
        # 过滤掉被对冲保护的仓位
        candidates = [s for s in scores if not s.is_hedge_protected]
        
        return candidates[:count]
