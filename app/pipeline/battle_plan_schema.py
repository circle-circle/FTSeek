"""
BattlePlan 统一 Schema (Phase 3)

定义全天作战计划的标准字段，作为 daily_battle_plan.json 的隐式契约。
不修改现有写入逻辑（Overlay 模式），供复盘/诊断/迁移使用。

数据流映射:
  08:20 辩论    → debate_consensus.*
  08:30 Pipeline → targets (盘前), restricted, overall_bias
  08:30 辩论合并 → merge_debate_targets 注入的标的
  12:00 午间规则 → midday_*.* (MiddayPlanRefresher 注入)
  12:00 午间LLM  → midday_llm_*.* (MiddayAnalyst 注入)
  盘中竞价     → targets[].status/auction_limit_price
  全天          → plan_version 递增记录每次变更
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from datetime import datetime


# =====================================================================
#  Target (单个标的)
# =====================================================================

@dataclass
class BattlePlanTarget:
    """单个作战标的 — 全天所有阶段共享"""
    symbol: str = ""                      # HK.00700 / US.AAPL
    direction: str = "ANY"                # LONG / SHORT / ANY
    status: str = "VALID"                 # VALID / INVALID / CAUTION
    urgency: str = "Medium"               # High / Medium / Low
    key_trigger: str = ""                 # 触发条件文本
    invalidation_reason: str = ""         # 失效原因

    # 盘前辩论注入
    debate_net_confidence: float = 0.0    # 辩论净信心 (-1.0 ~ +1.0)

    # 竞价注入 (盘中)
    auction_limit_price: float = 0.0      # 竞价限价
    gap_pct: float = 0.0                  # 跳空百分比

    # 仓位
    max_position_pct: float = 0.08        # 最大仓位比例 (默认 8%)

    # 来源标记 (追踪是哪个阶段注入的)
    source: str = "premarket"             # premarket / midday_debate / midday_llm_focus / midday_llm_position / midday_volume / midday_southbound / midday_position_drift

    def to_dict(self) -> Dict:
        return {k: v for k, v in {
            "symbol": self.symbol,
            "direction": self.direction,
            "status": self.status,
            "urgency": self.urgency,
            "key_trigger": self.key_trigger,
            "invalidation_reason": self.invalidation_reason,
            "debate_net_confidence": self.debate_net_confidence,
            "auction_limit_price": self.auction_limit_price,
            "gap_pct": self.gap_pct,
            "max_position_pct": self.max_position_pct,
            "source": self.source,
        }.items() if v != "" and v != 0.0 and v != "ANY" and v != "VALID" and v != "Medium" and v != "premarket"}


# =====================================================================
#  Debate Consensus (辩论共识)
# =====================================================================

@dataclass
class DebateConsensus:
    """盘前/午间辩论共识快照"""
    direction: str = "NEUTRAL"            # BUY / SELL / NEUTRAL / CAUTIOUS_BEARISH
    conviction: float = 0.0               # 共识强度 (0.0 ~ 1.0)
    ranked_count: int = 0                 # 参与排序的标的数
    avoid_count: int = 0                  # 回避名单标的数

    def to_dict(self) -> Dict:
        return {
            "direction": self.direction,
            "conviction": round(self.conviction, 2),
            "ranked_count": self.ranked_count,
            "avoid_count": self.avoid_count,
        }


# =====================================================================
#  Plan Version History (计划版本历史)
# =====================================================================

@dataclass
class PlanVersionEntry:
    """计划的一次变更快照 — 从 plan_version + 各阶段注入字段推导"""
    version: int = 1
    phase: str = "premarket"              # premarket / midday_refresher / midday_llm / eod
    timestamp: str = ""                   # ISO 时间
    added_symbols: List[str] = field(default_factory=list)
    removed_symbols: List[str] = field(default_factory=list)
    regime_change: str = ""               # 市场 regime 变化描述

    def to_dict(self) -> Dict:
        return {
            "version": self.version,
            "phase": self.phase,
            "timestamp": self.timestamp,
            "added_symbols": self.added_symbols,
            "removed_symbols": self.removed_symbols,
            "regime_change": self.regime_change,
        }


# =====================================================================
#  BattlePlan (全天作战计划)
# =====================================================================

@dataclass
class BattlePlan:
    """全天作战计划 — 唯一事实源"""

    # ── 基础字段 ──
    date: str = ""                        # YYYY-MM-DD
    targets: List[BattlePlanTarget] = field(default_factory=list)
    restricted: List[str] = field(default_factory=list)

    # ── 市场环境 ──
    overall_bias: str = "NEUTRAL"         # BUY / SELL / NEUTRAL / CAUTIOUS_BEARISH
    market_regime: str = "LOW"            # LOW / MEDIUM / HIGH / UNKNOWN
    risk_level: str = "LOW"               # LOW / MEDIUM / HIGH
    stop_trading: bool = False

    # ── 辩论共识 ──
    debate_consensus: DebateConsensus = field(default_factory=DebateConsensus)

    # ── 版本历史 ──
    plan_version: int = 1                 # 每次变更 +1
    generated_at: float = 0.0             # 首次生成时间戳

    # ── 午间规则注入 (MiddayPlanRefresher) ──
    midday_refreshed_at: Optional[float] = None
    midday_added: List[str] = field(default_factory=list)

    # ── 午间 LLM 注入 (MiddayAnalyst) ──
    midday_llm_injected_at: Optional[str] = None
    midday_llm_added: List[str] = field(default_factory=list)

    # ── Fallback 信息 ──
    reason: str = ""                      # fallback 原因 (LLM 失败时)

    @staticmethod
    def from_json(data: Dict) -> 'BattlePlan':
        """从 JSON dict 解析 — 兼容现有所有写入格式"""
        targets = []
        for t in data.get('targets', []):
            targets.append(BattlePlanTarget(
                symbol=t.get('symbol', ''),
                direction=t.get('direction', 'ANY'),
                status=t.get('status', 'VALID'),
                urgency=t.get('urgency', 'Medium'),
                key_trigger=t.get('key_trigger', ''),
                invalidation_reason=t.get('invalidation_reason', ''),
                debate_net_confidence=float(t.get('debate_net_confidence', 0) or 0),
                auction_limit_price=float(t.get('auction_limit_price', 0) or 0),
                max_position_pct=float(t.get('max_position_pct', 0.08) or 0.08),
                source=t.get('source', 'premarket'),
            ))

        raw_debate = data.get('debate_consensus', {})
        debate = DebateConsensus(
            direction=raw_debate.get('direction', 'NEUTRAL'),
            conviction=float(raw_debate.get('conviction', 0) or 0),
            ranked_count=int(raw_debate.get('ranked_count', 0) or 0),
            avoid_count=int(raw_debate.get('avoid_count', 0) or 0),
        )

        return BattlePlan(
            date=data.get('date', ''),
            targets=targets,
            restricted=data.get('restricted', []),
            overall_bias=data.get('overall_bias', 'NEUTRAL'),
            market_regime=data.get('market_regime', 'LOW'),
            risk_level=data.get('risk_level', 'LOW'),
            stop_trading=bool(data.get('stop_trading', False)),
            debate_consensus=debate,
            plan_version=int(data.get('plan_version', 1) or 1),
            generated_at=float(data.get('generated_at', 0) or 0),
            midday_refreshed_at=data.get('midday_refreshed_at'),
            midday_added=data.get('midday_added', []),
            midday_llm_injected_at=data.get('midday_llm_injected_at'),
            midday_llm_added=data.get('midday_llm_added', []),
            reason=data.get('reason', ''),
        )

    def to_dict(self) -> Dict:
        """序列化为 JSON — 与现有 daily_battle_plan.json 格式兼容"""
        return {
            "date": self.date,
            "targets": [t.to_dict() for t in self.targets],
            "restricted": self.restricted,
            "overall_bias": self.overall_bias,
            "market_regime": self.market_regime,
            "risk_level": self.risk_level,
            "stop_trading": self.stop_trading,
            "debate_consensus": self.debate_consensus.to_dict(),
            "plan_version": self.plan_version,
            "generated_at": self.generated_at,
            "midday_refreshed_at": self.midday_refreshed_at,
            "midday_added": self.midday_added,
            "midday_llm_injected_at": self.midday_llm_injected_at,
            "midday_llm_added": self.midday_llm_added,
            "reason": self.reason,
        }

    def get_version_history(self) -> List[PlanVersionEntry]:
        """从当前计划推导版本历史 — 用于复盘"""
        history = []

        # v1: 盘前
        premarket_syms = [
            t.symbol for t in self.targets
            if t.source == "premarket" or (t.source not in (
                "midday_debate", "midday_llm_focus", "midday_llm_position",
                "midday_volume", "midday_southbound", "midday_position_drift",
            ))
        ]
        history.append(PlanVersionEntry(
            version=1, phase="premarket",
            timestamp=datetime.fromtimestamp(self.generated_at).isoformat() if self.generated_at else "",
            added_symbols=premarket_syms,
            regime_change=f"regime={self.market_regime}, bias={self.overall_bias}",
        ))

        # 午间规则注入
        if self.midday_refreshed_at and self.midday_added:
            history.append(PlanVersionEntry(
                version=2, phase="midday_refresher",
                timestamp=datetime.fromtimestamp(self.midday_refreshed_at).isoformat(),
                added_symbols=self.midday_added,
            ))

        # 午间 LLM 注入
        if self.midday_llm_injected_at and self.midday_llm_added:
            history.append(PlanVersionEntry(
                version=3, phase="midday_llm",
                timestamp=self.midday_llm_injected_at,
                added_symbols=self.midday_llm_added,
            ))

        return history

    def get_phase_summary(self) -> Dict[str, Dict]:
        """按阶段汇总标的 — 用于复盘对比"""
        phases = {
            "premarket": {"targets": [], "count": 0},
            "midday_refresher": {"targets": [], "count": 0},
            "midday_llm": {"targets": [], "count": 0},
        }

        phase_map = {
            "premarket": "premarket",
            "midday_debate": "midday_refresher",
            "midday_llm_focus": "midday_llm",
            "midday_llm_position": "midday_llm",
            "midday_volume": "midday_refresher",
            "midday_southbound": "midday_refresher",
            "midday_position_drift": "midday_refresher",
        }

        for t in self.targets:
            p = phase_map.get(t.source, "premarket")
            phases[p]["targets"].append(t.symbol)
            phases[p]["count"] += 1

        return phases
