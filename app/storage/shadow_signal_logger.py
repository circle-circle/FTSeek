"""
Shadow Signal Logger - 记录所有生成的信号（包括未执行的）

用途：
- Phase 1 Shadow 验证：追踪信号质量
- 信号回测：分析被拒绝/未执行的信号表现
- 策略优化：评估不同策略的信号分布

设计：
- 独立于 signal_outcomes 表（只在平仓时写入）
- 使用 JSONL 文件存储，轻量且易于分析
- 包含完整信号上下文：symbol, direction, strength, confidence, strategy, timestamp
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Any

logger = logging.getLogger(__name__)


class ShadowSignalLogger:
    """记录所有生成的信号（包括未执行的）"""
    
    def __init__(self, log_path: str = "logs/shadow_signals.jsonl"):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        
    def record(self, candidate: Any, context: Dict[str, Any]) -> None:
        """
        记录一个 SignalCandidate
        
        Args:
            candidate: SignalCandidate 对象
            context: 额外上下文信息（price, volume, regime 等）
        """
        try:
            record = {
                "timestamp": datetime.now().isoformat(),
                "symbol": candidate.symbol,
                "direction": "LONG" if candidate.is_long else "SHORT",
                "strength": candidate.strength,
                "confidence": candidate.confidence,
                "strategy": candidate.strategy_id,
                "source": candidate.source,
                "rationale": candidate.rationale,
                "trace_id": getattr(candidate, "trace_id", ""),
                "context": {
                    "price": context.get("price"),
                    "volume": context.get("volume"),
                    "vix": context.get("vix"),
                    "regime": context.get("regime"),
                    "market_sentiment": context.get("market_sentiment"),
                },
                "metadata": {
                    "quality_factors": candidate.metadata.get("quality_factors") if hasattr(candidate, "metadata") and candidate.metadata else None,
                    "fused": candidate.metadata.get("fused") if hasattr(candidate, "metadata") and candidate.metadata else None,
                    "fused_count": candidate.metadata.get("fused_count") if hasattr(candidate, "metadata") and candidate.metadata else None,
                },
            }
            
            # 追加写入 JSONL
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            
            logger.info(
                f"[ShadowSignal] 记录: {candidate.symbol} {record['direction']} "
                f"strength={candidate.strength:.1f} conf={candidate.confidence:.2f} "
                f"strategy={candidate.strategy_id}"
            )
            
        except Exception as e:
            logger.warning(f"[ShadowSignal] 记录失败（不影响主流程）: {e}")
    
    def get_today_signals(self) -> list:
        """获取今日所有 shadow 信号"""
        try:
            if not self.log_path.exists():
                return []
            
            today = datetime.now().date()
            signals = []
            
            with open(self.log_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        record = json.loads(line.strip())
                        signal_date = datetime.fromisoformat(record["timestamp"]).date()
                        if signal_date == today:
                            signals.append(record)
                    except (json.JSONDecodeError, KeyError):
                        continue
            
            return signals
            
        except Exception as e:
            logger.warning(f"[ShadowSignal] 读取失败: {e}")
            return []


# 全局单例
_shadow_logger: Optional[ShadowSignalLogger] = None


def get_shadow_signal_logger() -> ShadowSignalLogger:
    """获取全局 Shadow 信号记录器实例"""
    global _shadow_logger
    if _shadow_logger is None:
        _shadow_logger = ShadowSignalLogger()
    return _shadow_logger
