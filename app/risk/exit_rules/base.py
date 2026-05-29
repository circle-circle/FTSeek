"""
FTSeek-US 退出规则基类与统一接口 (Exit Rules Base)

Task #19 拆分自 position_exit_monitor.py 巨石模块。
本文件定义所有退出规则的统一接口与决策对象。

设计要点:
  1. ExitDecision 数据类: 统一退出决策的结构化表示 (向前兼容 dict 形式)
  2. BaseExitRule 抽象类: 所有退出规则继承此基类
  3. 每条规则持有 monitor 引用, 复用其 ATR 缓存 / cost_model / VIX 查询等
  4. should_exit() 返回 dict (向后兼容现有 orchestrator 与 ExitQualityScorer),
     新代码可使用 ExitDecision dataclass 然后调 to_dict()
"""
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any


@dataclass
class ExitDecision:
    """退出决策统一表示

    保留 dict 形式以兼容现有 orchestrator / ExitQualityScorer / _execute_exit。
    新代码建议使用本 dataclass 然后调用 to_dict()。

    Fields:
        should_exit: 是否触发退出 (False 时其他字段无效)
        rule_name : 规则代号 (FLASH_EXIT / DAYTRADE_TP1 / CHANDELIER_TRAIL ...)
        reason    : 同 rule_name (与 dict 接口对齐, 供 _REASON_TO_RULE 查询)
        reason_cn : 中文原因, 用于日志 / 钉钉推送
        exit_price: 退出参考价 (0 代表使用当前价兜底)
        exit_qty  : 部分退出时的股数 (None 代表全平)
        close_ratio: 部分退出比例 (0~1, 与 exit_qty 二选一)
        description: 详细描述, 用于日志
        metadata  : 规则特有的附加字段 (stop_price, atr_mult, peak 等)
    """
    should_exit: bool = False
    rule_name: str = ""
    reason: str = ""
    reason_cn: str = ""
    exit_price: float = 0.0
    exit_qty: Optional[int] = None
    close_ratio: Optional[float] = None
    description: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转为 orchestrator 期望的 dict 形式"""
        d: Dict[str, Any] = {
            "reason": self.reason or self.rule_name,
            "reason_cn": self.reason_cn,
            "exit_price": self.exit_price,
            "description": self.description,
        }
        if self.exit_qty is not None:
            d["close_qty"] = int(self.exit_qty)
        if self.close_ratio is not None:
            d["close_ratio"] = float(self.close_ratio)
        # metadata 字段平铺到顶层 (与原 dict 结构对齐)
        if self.metadata:
            for k, v in self.metadata.items():
                d.setdefault(k, v)
        return d


class BaseExitRule:
    """退出规则基类

    Args:
        monitor: ExitMonitor 实例引用, 用于访问共享资源 (ATR 缓存 / cost_model / VIX / DB 查询)

    Subclass contract:
        - 子类必须覆盖 rule_name (与 _REASON_TO_RULE / exit_policy.yaml 对齐)
        - 子类实现 should_exit(position, market_data, trail_state) → Optional[Dict]
        - 返回 None 表示不触发; 返回 dict 表示触发 (dict 结构与 _execute_exit 期望一致)
    """

    rule_name: str = "BASE"

    def __init__(self, monitor=None):
        self.monitor = monitor

    def should_exit(self, position, market_data: Dict[str, Any], trail_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """评估是否应该退出

        Args:
            position: Position 对象
            market_data: 市场数据上下文 (current_price, atr, effective_atr,
                         profit_atr_signed, pnl_pct, direction, now_ts,
                         policy_guardian, earnings_guard, entry_price, ...)
            trail_state: 持仓的 trail_state dict (规则可读写, orchestrator 负责持久化)

        Returns:
            None: 不触发
            dict: 触发, 结构应与 _execute_exit / ExitQualityScorer 期望一致
        """
        raise NotImplementedError(f"{self.__class__.__name__} must implement should_exit()")
