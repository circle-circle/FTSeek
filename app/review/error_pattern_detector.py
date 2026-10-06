"""
错误模式检测器

自动检测交易中的常见错误模式，生成改进建议
"""
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from pathlib import Path
from dataclasses import dataclass
import json

logger = logging.getLogger("ftseek_us.review.error_detector")
logger.propagate = False


@dataclass
class ErrorPattern:
    """错误模式"""
    pattern_id: str
    pattern_name: str
    pattern_type: str  # entry/exit/risk/psychology/strategy
    severity: str  # low/medium/high/critical
    count: int
    total_loss: float
    avg_loss: float
    affected_trades: List[int]
    description: str
    suggestion: str


class ErrorPatternDetector:
    """错误模式检测器"""
    
    # 错误模式定义
    #
    # 2026-10-06 判据生死普查 (现行期 333 笔 + 全历史值域实测) — 共删 3 个判据:
    #   low_signal_entry        (final_strength)   命中 0/333   ← 化石列恒 False
    #   high_disagreement_entry (consensus_level)  命中 0/333   ← 化石列恒 False, sev=high!
    #   late_stop_loss          (exit_reason!="STOP_LOSS")      ← 判别式恒 True, 语义退化
    # 化石列根因: 3 模型投票 2026-07-12 退役 (voting_predictions 停写), playbook 路径
    #   voting_data 查询恒 miss ⇒ final_strength/consensus_level 恒 0 ⇒
    #   `(0 or 50) < 50` 与 `(0.0 or 1) < 0.5` 恒 False (fallback 值本身即阈值边界)。
    # 危害不是「不触发」, 是**值班人以为有 high 级守卫在守, 实际它已静默死亡**。
    # 处置范式同 daily_review._analyze_voting_consistency (2026-09-04 M4 已裁决):
    #   上游退役 ⇒ 明确标不可用, 不保留恒 False 的尸体判据 + 注释化妆。
    # 若未来恢复投票管线, 判据须按当时的真实值域重写, 不得直接复活这三条。
    #
    # 保留但当前休眠的判据 (值域脱钩, 非死码 — 仓位放大即自然复活):
    #   large_loss (<-$1000, critical) / late_stop_loss 的同类: 现行 realized_pnl 极值
    #   [-$396, +$449] ⇒ 0 命中。这是仓位规模变化的结果, 不是判据写错, 故不删。
    ERROR_PATTERNS = {
        # 入场错误
        # signal_confidence 是活字段 (现行期 0.35-0.78, 填充率 97.9%) ⇒ 本判据有效,
        # 实测命中 60/333 = 18.0%, 累计亏损 $1,227.78。
        "low_confidence_entry": {
            "name": "低置信度入场",
            "type": "entry",
            "condition": lambda t: (t.get("signal_confidence") or 0.5) < 0.5,
            "description": "信号置信度低于 50% 时入场",
            "suggestion": "提高置信度阈值至 50% 以上",
            "severity": "medium",
        },

        # 出场错误
        # 2026-10-06 修: holding_hours 可为 NULL (现行期 1/333) ⇒ `None < 1` 抛
        #   TypeError, 被 detect_patterns 的 except 静默吞掉 (整行跳过, 无声)。
        #   改 `or 0` 与同判据内 realized_pnl 的既有写法一致。
        "early_exit": {
            "name": "过早平仓",
            "type": "exit",
            "condition": lambda t: (
                (t.get("holding_hours") or 0) < 1 and
                (t.get("realized_pnl") or 0) > 0
            ),
            "description": "持仓不足 1 小时即平仓，可能错过更大收益",
            "suggestion": "延长持仓时间，设置更合理的止盈目标",
            "severity": "low",
        },
        # 2026-10-06 删 late_stop_loss (sev=high): 其判别式 `exit_reason != "STOP_LOSS"`
        #   在生产值域 (全历史 41 个 distinct exit_reason, 实测无 "STOP_LOSS" 字面量) 下
        #   **恒为 True** ⇒ 该判据从未真正检测「止损延迟」, 一直等价于纯
        #   `realized_pnl < -500`, 与下方 large_loss (<-1000) 是同类金额判据的重复。
        #   要「修对」需维护一份止损类 exit_reason 白名单, 但该值域已漂移到 41 个且仍在长
        #   (SL_*/sl_hit/ORB_*_STOP/HARD_STOP/…大小写与新旧命名并存) ⇒ 白名单必然再次静默
        #   过期, 重蹈本次覆辙。按「删门非加守卫」: 删除, 大额亏损由 large_loss 兜底。
        #   注: 现行仓位规模下 realized_pnl 极值 [-$396, +$449], 两条金额判据当前均休眠
        #   (仓位缩小后结构上够不到 $500/$1000); large_loss 保留为 critical 级灾难兜底,
        #   仓位若放大可自然复活, 不删。

        # 风控错误
        "stop_loss_violation": {
            "name": "止损违规",
            "type": "risk",
            "condition": lambda t: (
                # trade_records.max_drawdown = 百分点口径 (5.53 ≡ 5.53%); 2026-09-17 单位勘误 0.05→5.0
                (t.get("max_drawdown") or 0) > 5.0 and
                (t.get("realized_pnl", 0) or 0) < 0
            ),
            "description": "持仓期间回撤超过 5% 导致亏损",
            "suggestion": "加强持仓监控，及时调整止损位置",
            "severity": "high",
        },
        "large_loss": {
            "name": "大额亏损",
            "type": "risk",
            "condition": lambda t: (t.get("realized_pnl", 0) or 0) < -1000,
            "description": "单笔亏损超过 $1000",
            "suggestion": "控制单笔仓位，设置最大亏损限制",
            "severity": "critical",
        },
        
        # 策略错误
        "strategy_underperform": {
            "name": "策略表现不佳",
            "type": "strategy",
            "condition": lambda t, stats: (
                stats.get("strategy", {}).get(t.get("strategy_id", ""), {}).get("win_rate", 0.5) < 0.4
            ),
            "description": "策略胜率低于 40%",
            "suggestion": "暂停该策略，重新评估策略逻辑",
            "severity": "high",
        },
    }
    
    def __init__(self, db_path: str = None):
        if db_path:
            self.db_path = Path(db_path)
        else:
            from app.core.paths import get_trade_records_db
            self.db_path = get_trade_records_db()
        
        logger.info(f"错误模式检测器已初始化：{self.db_path}")
    
    def _get_conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))
    
    def detect_patterns(
        self,
        trades: List[Dict] = None,
        days: int = 30,
    ) -> List[ErrorPattern]:
        """
        检测错误模式
        
        Args:
            trades: 交易记录列表，如果为 None 则从数据库加载
            days: 加载最近多少天的数据
        
        Returns:
            检测到的错误模式列表
        """
        if trades is None:
            trades = self._load_recent_trades(days)
        
        if not trades:
            return []
        
        # 计算策略统计
        strategy_stats = self._calculate_strategy_stats(trades)
        
        patterns = []
        
        for pattern_id, pattern_def in self.ERROR_PATTERNS.items():
            affected_trades = []
            total_loss = 0
            
            for trade in trades:
                try:
                    # 检查条件
                    if pattern_def["type"] == "strategy":
                        condition_met = pattern_def["condition"](trade, strategy_stats)
                    else:
                        condition_met = pattern_def["condition"](trade)
                    
                    if condition_met:
                        affected_trades.append(trade.get("id", 0))
                        pnl = trade.get("realized_pnl", 0) or 0
                        if pnl < 0:
                            total_loss += abs(pnl)
                except Exception as error:
                    # 2026-10-06: 原为裸 `continue` — 判据抛异常时整行静默跳过, 无日志。
                    # 实测代价: early_exit 因 holding_hours 为 NULL 抛 TypeError, 该行被无声
                    # 吞掉, 普查时才暴露 (若只读代码会以为判据在跑)。埋点禁静默吞异常。
                    # 仍不中断扫描 (单行脏数据不该毁掉整份报告), 但必须留痕可查。
                    # 注: 本 warning 只在判据真抛异常时触发; 已修 holding_hours 的 NULL
                    # TypeError ⇒ 稳态下不产生日志量, 不会撞 W456 日志卫生棘轮。
                    logger.warning(
                        f"[错误模式] 判据 {pattern_id} 在 trade id="
                        f"{trade.get('id')} 上抛异常, 该行跳过: "
                        f"{type(error).__name__}: {error}",
                        exc_info=True,
                    )
                    continue
            
            if affected_trades:
                avg_loss = total_loss / len(affected_trades) if affected_trades else 0
                
                patterns.append(ErrorPattern(
                    pattern_id=pattern_id,
                    pattern_name=pattern_def["name"],
                    pattern_type=pattern_def["type"],
                    severity=pattern_def["severity"],
                    count=len(affected_trades),
                    total_loss=total_loss,
                    avg_loss=avg_loss,
                    affected_trades=affected_trades,
                    description=pattern_def["description"],
                    suggestion=pattern_def["suggestion"],
                ))
        
        # 按严重程度和出现次数排序
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        patterns.sort(key=lambda p: (severity_order.get(p.severity, 4), -p.count))
        
        return patterns
    
    def _load_recent_trades(self, days: int) -> List[Dict]:
        """加载最近的交易记录"""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
            
            rows = conn.execute("""
                SELECT * FROM trade_records
                WHERE trade_date >= ?
                ORDER BY trade_date DESC
            """, (cutoff,)).fetchall()
            
            conn.close()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"加载交易记录失败：{e}")
            return []
    
    def _calculate_strategy_stats(self, trades: List[Dict]) -> Dict:
        """计算策略统计"""
        stats = {"strategy": {}}
        
        for trade in trades:
            strategy = trade.get("strategy_id", "unknown") or "unknown"
            if strategy not in stats["strategy"]:
                stats["strategy"][strategy] = {"trades": 0, "wins": 0, "pnl": 0}
            
            stats["strategy"][strategy]["trades"] += 1
            pnl = trade.get("realized_pnl", 0) or 0
            stats["strategy"][strategy]["pnl"] += pnl
            if pnl > 0:
                stats["strategy"][strategy]["wins"] += 1
        
        # 计算胜率
        for strategy, data in stats["strategy"].items():
            data["win_rate"] = data["wins"] / data["trades"] if data["trades"] > 0 else 0
        
        return stats
    
    def generate_report(self, days: int = 30) -> Dict:
        """
        生成错误模式报告
        
        Args:
            days: 分析最近多少天的数据
        
        Returns:
            错误模式报告
        """
        patterns = self.detect_patterns(days=days)
        
        # 按类型分组
        by_type = {}
        for pattern in patterns:
            if pattern.pattern_type not in by_type:
                by_type[pattern.pattern_type] = []
            by_type[pattern.pattern_type].append({
                "name": pattern.pattern_name,
                "severity": pattern.severity,
                "count": pattern.count,
                "total_loss": pattern.total_loss,
                "description": pattern.description,
                "suggestion": pattern.suggestion,
            })
        
        # 汇总统计
        total_errors = sum(p.count for p in patterns)
        total_loss = sum(p.total_loss for p in patterns)
        critical_count = sum(1 for p in patterns if p.severity == "critical")
        high_count = sum(1 for p in patterns if p.severity == "high")
        
        # 生成优先改进建议
        priority_suggestions = []
        for pattern in patterns[:5]:
            if pattern.severity in ["critical", "high"]:
                priority_suggestions.append({
                    "priority": "高" if pattern.severity == "critical" else "中",
                    "issue": pattern.pattern_name,
                    "action": pattern.suggestion,
                    "impact": f"影响 {pattern.count} 笔交易，累计损失 ${pattern.total_loss:.2f}",
                })
        
        return {
            "report_date": datetime.now().strftime('%Y-%m-%d'),
            "analysis_period": f"最近 {days} 天",
            "summary": {
                "total_errors": total_errors,
                "total_loss": round(total_loss, 2),
                "critical_count": critical_count,
                "high_count": high_count,
            },
            "by_type": by_type,
            "priority_suggestions": priority_suggestions,
        }


# 全局实例
_detector: Optional[ErrorPatternDetector] = None


def get_error_detector() -> ErrorPatternDetector:
    """获取错误模式检测器实例"""
    global _detector
    if _detector is None:
        _detector = ErrorPatternDetector()
    return _detector


def detect_error_patterns(days: int = 30) -> List[ErrorPattern]:
    """检测错误模式"""
    return get_error_detector().detect_patterns(days=days)


def generate_error_report(days: int = 30) -> Dict:
    """生成错误模式报告"""
    return get_error_detector().generate_report(days)