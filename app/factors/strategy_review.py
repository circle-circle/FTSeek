"""
FTSeek 策略复盘引擎 (Strategy Review Engine)

功能:
1. 交易后自动评估 LLM 新闻策略准确率
2. 对比预测方向 vs 实际价格走势
3. 生成策略版本表现报告
4. 提供迭代建议 (prompt优化、规则调整)

触发时机:
- 每次新闻策略信号执行后 30 分钟自动评估
- 每日盘后复盘 (16:30) 汇总当日所有新闻策略
- 每周深度分析 (周日) 生成策略版本迭代建议

设计原则:
- 非阻塞: 评估不影响实时交易
- 可追溯: 每条策略都有版本号和表现记录
- 自适应: 低置信度策略自动降级, 高准确率策略提权

作者: FTSeek Team
日期: 2026-04-15
"""

import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger("ftseek.factors.strategy_review")

# ============================================================
# 数据结构
# ============================================================

@dataclass
class StrategyRecord:
    """策略记录 (生成时)"""
    record_id: str
    news_id: str
    news_title: str
    event_type: str
    code: str
    name: str
    direction: str           # BUY/SELL/NEUTRAL
    confidence: str          # HIGH/MEDIUM/LOW
    position_tier: str       # FULL/HALF/LIGHT/TRACE/SKIP
    stop_loss_pct: float
    take_profit_pct: float
    entry_price: float = 0.0
    timestamp: float = field(default_factory=time.time)
    strategy_version: str = "v1.0"  # 策略版本号
    llm_model: str = ""
    reasoning: str = ""
    second_order_effects: List[str] = field(default_factory=list)


@dataclass
class StrategyOutcome:
    """策略结果 (执行后评估)"""
    record_id: str
    actual_pnl_pct: float = 0.0       # 实际盈亏%
    high_pct: float = 0.0             # 最高点%
    low_pct: float = 0.0              # 最低点%
    direction_correct: bool = False   # 方向是否正确
    stop_hit: bool = False            # 是否触发止损
    take_profit_hit: bool = False     # 是否触发止盈
    hold_duration_minutes: float = 0.0 # 持仓时长
    outcome_label: str = "PENDING"    # WIN/LOSS/BREAKEVEN/PENDING
    assessed_at: float = 0.0
    assessment_details: str = ""


@dataclass
class StrategyVersionStats:
    """策略版本统计"""
    version: str
    total_signals: int = 0
    correct_direction: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    avg_pnl_pct: float = 0.0
    avg_confidence: float = 0.0
    win_rate: float = 0.0
    direction_accuracy: float = 0.0
    by_event_type: Dict[str, dict] = field(default_factory=dict)
    by_code: Dict[str, dict] = field(default_factory=dict)


# ============================================================
# 策略复盘引擎
# ============================================================

class StrategyReviewEngine:
    """
    策略复盘引擎
    
    核心流程:
    1. 信号生成时 → 创建 StrategyRecord (跟踪快照)
    2. 信号执行后 → 开始跟踪价格走势
    3. 评估时机到 → 计算实际结果, 生成 StrategyOutcome
    4. 盘后/周度 → 汇总统计, 生成迭代建议
    """
    
    def __init__(self, data_dir: str = None):
        self.data_dir = Path(data_dir or "/opt/ftseek/FTSeek/data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        # 持久化存储
        self._records_file = self.data_dir / "strategy_records.json"
        self._outcomes_file = self.data_dir / "strategy_outcomes.json"
        self._stats_file = self.data_dir / "strategy_version_stats.json"
        
        # 内存缓存
        self._records: Dict[str, StrategyRecord] = {}
        self._outcomes: Dict[str, StrategyOutcome] = {}
        self._pending_assessments: Dict[str, float] = {}  # record_id -> assess_at timestamp
        
        self._load()
        
        logger.info(
            "StrategyReviewEngine 初始化: "
            f"records={len(self._records)}, "
            f"outcomes={len(self._outcomes)}, "
            f"pending={len(self._pending_assessments)}"
        )
    
    # ----------------------------------------------------------
    # 记录管理
    # ----------------------------------------------------------
    
    def record_signal(self, signal, news_strategy_signal) -> str:
        """
        记录一条策略信号 (信号生成时调用)
        
        Returns:
            record_id
        """
        record_id = f"SR_{int(time.time())}_{hash(signal.id) % 10000:04d}"
        
        extra = getattr(signal, 'extra', {})
        
        record = StrategyRecord(
            record_id=record_id,
            news_id=news_strategy_signal.news_id if news_strategy_signal else signal.id,
            news_title=news_strategy_signal.news_title if news_strategy_signal else "",
            event_type=extra.get('event_type', ''),
            code=signal.code,
            name=signal.name,
            direction=extra.get('llm_direction', 'NEUTRAL'),
            confidence=extra.get('llm_confidence', 'LOW'),
            position_tier=extra.get('position_tier', 'SKIP'),
            stop_loss_pct=extra.get('stop_loss_pct', -3.0),
            take_profit_pct=extra.get('take_profit_pct', 8.0),
            entry_price=signal.price,
            timestamp=time.time(),
            strategy_version="v1.0",  # 后续可通过迭代逻辑更新
            llm_model=os.getenv("BAILIAN_MODEL", "qwen-plus"),
            reasoning=extra.get('reasoning', ''),
            second_order_effects=extra.get('second_order_effects', []),
        )
        
        self._records[record_id] = record
        
        # 设置评估时间 (30分钟后)
        assess_at = time.time() + 1800  # 30分钟
        self._pending_assessments[record_id] = assess_at
        
        self._save()
        
        logger.info(
            f"📝 策略记录创建: {record_id} | {signal.code} | "
            f"dir={record.direction} | conf={record.confidence} | "
            f"评估时间: {datetime.fromtimestamp(assess_at).strftime('%H:%M')}"
        )
        
        return record_id
    
    def assess_outcome(self, record_id: str, current_price: float, 
                       high_price: float = 0.0, low_price: float = 0.0) -> Optional[StrategyOutcome]:
        """
        评估一条策略的实际结果
        
        Args:
            record_id: 策略记录ID
            current_price: 当前价格
            high_price: 期间最高价
            low_price: 期间最低价
        """
        record = self._records.get(record_id)
        if not record:
            logger.warning(f"策略记录不存在: {record_id}")
            return None
        
        entry = record.entry_price if record.entry_price > 0 else current_price
        if entry <= 0:
            logger.warning(f"无入场价格, 跳过评估: {record_id}")
            return None
        
        # 计算实际盈亏
        if record.direction == "BUY":
            pnl_pct = ((current_price - entry) / entry) * 100
            high_pct = ((high_price - entry) / entry) * 100 if high_price > 0 else pnl_pct
            low_pct = ((low_price - entry) / entry) * 100 if low_price > 0 else pnl_pct
        elif record.direction == "SELL":
            pnl_pct = ((entry - current_price) / entry) * 100
            high_pct = ((entry - low_price) / entry) * 100 if low_price > 0 else pnl_pct
            low_pct = ((entry - high_price) / entry) * 100 if high_price > 0 else pnl_pct
        else:
            pnl_pct = 0.0
            high_pct = 0.0
            low_pct = 0.0
        
        # 判断方向是否正确
        direction_correct = pnl_pct > 0
        
        # 判断是否触发止损/止盈
        stop_hit = low_pct <= record.stop_loss_pct if record.direction == "BUY" else high_pct >= -record.stop_loss_pct
        take_profit_hit = high_pct >= record.take_profit_pct if record.direction == "BUY" else low_pct <= -record.take_profit_pct
        
        # 结果标签
        if stop_hit and not take_profit_hit:
            outcome_label = "LOSS"
        elif take_profit_hit:
            outcome_label = "WIN"
        elif abs(pnl_pct) < 0.5:
            outcome_label = "BREAKEVEN"
        elif pnl_pct > 0:
            outcome_label = "WIN"
        else:
            outcome_label = "LOSS"
        
        hold_duration = (time.time() - record.timestamp) / 60  # 分钟
        
        # 评估详情
        details = (
            f"入场=${entry:.2f}, 当前=${current_price:.2f}, "
            f"最高={high_pct:+.1f}%, 最低={low_pct:+.1f}%, "
            f"PnL={pnl_pct:+.1f}%, 持仓={hold_duration:.0f}分钟"
        )
        
        outcome = StrategyOutcome(
            record_id=record_id,
            actual_pnl_pct=round(pnl_pct, 2),
            high_pct=round(high_pct, 2),
            low_pct=round(low_pct, 2),
            direction_correct=direction_correct,
            stop_hit=stop_hit,
            take_profit_hit=take_profit_hit,
            hold_duration_minutes=round(hold_duration, 1),
            outcome_label=outcome_label,
            assessed_at=time.time(),
            assessment_details=details,
        )
        
        self._outcomes[record_id] = outcome
        self._pending_assessments.pop(record_id, None)
        
        # 从 pending 移除
        if record_id in self._pending_assessments:
            del self._pending_assessments[record_id]
        
        self._save()
        
        logger.info(
            f"📊 策略评估完成: {record_id} | {record.code} | "
            f"{outcome_label} | PnL={pnl_pct:+.1f}% | "
            f"方向{'✓' if direction_correct else '✗'}"
        )
        
        return outcome
    
    def check_pending_assessments(self, get_price_fn=None) -> List[StrategyOutcome]:
        """
        检查所有待评估的策略, 自动评估到期的
        
        Args:
            get_price_fn: 获取价格的函数 fn(code) -> (current, high, low)
        """
        now = time.time()
        due = [rid for rid, at in self._pending_assessments.items() if now >= at]
        
        outcomes = []
        for record_id in due:
            record = self._records.get(record_id)
            if not record:
                continue
            
            if get_price_fn:
                try:
                    current, high, low = get_price_fn(record.code)
                    outcome = self.assess_outcome(record_id, current, high, low)
                    if outcome:
                        outcomes.append(outcome)
                except Exception as e:
                    logger.warning(f"获取价格失败 {record.code}: {e}")
            else:
                logger.debug(f"跳过评估 (无价格函数): {record_id}")
        
        return outcomes
    
    # ----------------------------------------------------------
    # 统计分析
    # ----------------------------------------------------------
    
    def compute_version_stats(self) -> StrategyVersionStats:
        """计算策略版本统计"""
        stats = StrategyVersionStats(version="v1.0")
        
        for record_id, outcome in self._outcomes.items():
            record = self._records.get(record_id)
            if not record:
                continue
            
            stats.total_signals += 1
            if outcome.direction_correct:
                stats.correct_direction += 1
            if outcome.outcome_label == "WIN":
                stats.wins += 1
            elif outcome.outcome_label == "LOSS":
                stats.losses += 1
            elif outcome.outcome_label == "BREAKEVEN":
                stats.breakeven += 1
            
            stats.avg_pnl_pct += outcome.actual_pnl_pct
            
            # 按事件类型统计
            etype = record.event_type or "unknown"
            if etype not in stats.by_event_type:
                stats.by_event_type[etype] = {"total": 0, "wins": 0, "losses": 0, "avg_pnl": 0.0}
            stats.by_event_type[etype]["total"] += 1
            if outcome.outcome_label == "WIN":
                stats.by_event_type[etype]["wins"] += 1
            elif outcome.outcome_label == "LOSS":
                stats.by_event_type[etype]["losses"] += 1
            stats.by_event_type[etype]["avg_pnl"] += outcome.actual_pnl_pct
        
        if stats.total_signals > 0:
            stats.avg_pnl_pct /= stats.total_signals
            stats.win_rate = stats.wins / stats.total_signals
            stats.direction_accuracy = stats.correct_direction / stats.total_signals
        
        # 平均每个事件类型的 PnL
        for etype, data in stats.by_event_type.items():
            if data["total"] > 0:
                data["avg_pnl"] /= data["total"]
        
        return stats
    
    def generate_iteration_suggestions(self) -> List[str]:
        """生成策略迭代建议"""
        suggestions = []
        stats = self.compute_version_stats()
        
        if stats.total_signals < 5:
            suggestions.append("📊 样本量不足 (<5), 建议继续积累数据后再迭代")
            return suggestions
        
        # 整体胜率
        if stats.win_rate < 0.4:
            suggestions.append(
                f"⚠️ 整体胜率仅 {stats.win_rate:.0%}, 低于40%阈值. "
                f"建议: 提高LLM置信度门槛 (当前HIGH/MEDIUM→仅HIGH)"
            )
        elif stats.win_rate > 0.65:
            suggestions.append(
                f"🎉 整体胜率 {stats.win_rate:.0%}, 高于65%. "
                f"建议: 可适当提高仓位 (LIGHT→HALF)"
            )
        
        # 方向准确率
        if stats.direction_accuracy < 0.5:
            suggestions.append(
                f"⚠️ 方向准确率仅 {stats.direction_accuracy:.0%}, "
                f"建议: 检查Prompt中的分析逻辑, 增加技术面验证权重"
            )
        
        # 按事件类型分析
        for etype, data in sorted(stats.by_event_type.items(), key=lambda x: x[1]["total"], reverse=True):
            if data["total"] >= 3:
                event_win_rate = data["wins"] / data["total"]
                if event_win_rate < 0.3:
                    suggestions.append(
                        f"❌ 事件类型 '{etype}' 胜率 {event_win_rate:.0%} ({data['wins']}/{data['total']}), "
                        f"建议: 对该类型启用更严格的止盈止损"
                    )
                elif event_win_rate > 0.7:
                    suggestions.append(
                        f"✅ 事件类型 '{etype}' 胜率 {event_win_rate:.0%} ({data['wins']}/{data['total']}), "
                        f"建议: 提高该类信号的默认仓位"
                    )
        
        # 平均盈亏
        if stats.avg_pnl_pct < -1.0:
            suggestions.append(f"⚠️ 平均盈亏 {stats.avg_pnl_pct:+.1f}%, 建议收紧止损参数")
        elif stats.avg_pnl_pct > 2.0:
            suggestions.append(f"🎉 平均盈亏 {stats.avg_pnl_pct:+.1f}%, 策略表现优秀")
        
        return suggestions
    
    # ----------------------------------------------------------
    # 持久化
    # ----------------------------------------------------------
    
    def _save(self):
        """保存数据到磁盘"""
        try:
            records_data = {k: _asdict(v) for k, v in self._records.items()}
            with open(self._records_file, 'w', encoding='utf-8') as f:
                json.dump(records_data, f, ensure_ascii=False, indent=2)
            
            outcomes_data = {k: _asdict(v) for k, v in self._outcomes.items()}
            with open(self._outcomes_file, 'w', encoding='utf-8') as f:
                json.dump(outcomes_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存策略数据失败: {e}")
    
    def _load(self):
        """从磁盘加载数据"""
        try:
            if self._records_file.exists():
                with open(self._records_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                for k, v in data.items():
                    self._records[k] = StrategyRecord(**v)
            
            if self._outcomes_file.exists():
                with open(self._outcomes_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                for k, v in data.items():
                    self._outcomes[k] = StrategyOutcome(**v)
            
            # 重建 pending
            for rid, record in self._records.items():
                if rid not in self._outcomes:
                    self._pending_assessments[rid] = record.timestamp + 1800
        except Exception as e:
            logger.error(f"加载策略数据失败: {e}")
    
    # ----------------------------------------------------------
    # 报告生成
    # ----------------------------------------------------------
    
    def generate_daily_report(self) -> str:
        """生成每日复盘报告"""
        today_start = datetime.now().replace(hour=0, minute=0, second=0).timestamp()
        
        today_records = {
            rid: r for rid, r in self._records.items()
            if r.timestamp >= today_start
        }
        today_outcomes = {
            rid: o for rid, o in self._outcomes.items()
            if rid in today_records
        }
        
        lines = [
            f"# FTSeek 新闻策略日报 | {datetime.now().strftime('%Y-%m-%d')}",
            f"",
            f"## 概览",
            f"- 信号总数: {len(today_records)}",
            f"- 已评估: {len(today_outcomes)}",
            f"- 待评估: {len(today_records) - len(today_outcomes)}",
            f"",
        ]
        
        if today_outcomes:
            wins = sum(1 for o in today_outcomes.values() if o.outcome_label == "WIN")
            losses = sum(1 for o in today_outcomes.values() if o.outcome_label == "LOSS")
            avg_pnl = sum(o.actual_pnl_pct for o in today_outcomes.values()) / len(today_outcomes)
            
            lines.extend([
                f"## 表现",
                f"- 盈利: {wins}",
                f"- 亏损: {losses}",
                f"- 平均盈亏: {avg_pnl:+.1f}%",
                f"- 胜率: {wins/len(today_outcomes):.0%}" if today_outcomes else "- 胜率: N/A",
                f"",
                f"## 明细",
            ])
            
            for rid, outcome in today_outcomes.items():
                record = today_records.get(rid)
                if record:
                    emoji = "✅" if outcome.outcome_label == "WIN" else ("❌" if outcome.outcome_label == "LOSS" else "➡️")
                    lines.append(
                        f"- {emoji} {record.code} | {record.direction} | "
                        f"PnL={outcome.actual_pnl_pct:+.1f}% | {outcome.outcome_label} | "
                        f"{record.event_type}"
                    )
        
        lines.append(f"\n## 迭代建议")
        suggestions = self.generate_iteration_suggestions()
        for s in suggestions:
            lines.append(f"- {s}")
        
        return "\n".join(lines)
    
    def get_stats_summary(self) -> dict:
        """获取统计摘要"""
        stats = self.compute_version_stats()
        suggestions = self.generate_iteration_suggestions()
        
        return {
            "total_records": len(self._records),
            "total_outcomes": len(self._outcomes),
            "pending_assessments": len(self._pending_assessments),
            "version": stats.version,
            "total_signals": stats.total_signals,
            "wins": stats.wins,
            "losses": stats.losses,
            "breakeven": stats.breakeven,
            "win_rate": round(stats.win_rate, 3),
            "direction_accuracy": round(stats.direction_accuracy, 3),
            "avg_pnl_pct": round(stats.avg_pnl_pct, 2),
            "by_event_type": stats.by_event_type,
            "suggestions": suggestions,
        }


# Helper: dataclass to dict
def _asdict(obj):
    if hasattr(obj, '__dataclass_fields__'):
        return {k: v for k, v in vars(obj).items() if not k.startswith('_')}
    return obj


# ============================================================
# 全局单例
# ============================================================

_global_review: Optional[StrategyReviewEngine] = None

def get_review_engine(data_dir: str = None) -> StrategyReviewEngine:
    global _global_review
    if _global_review is None:
        _global_review = StrategyReviewEngine(data_dir)
    return _global_review

def reset_review_engine():
    global _global_review
    _global_review = None
