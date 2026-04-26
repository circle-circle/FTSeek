"""
账户合规管理器 (Account Compliance Manager)

P0 核心模块：区分港股/美股合规规则。
- 港股 (HK): T+0 回转，无 PDT 限制，仅检查资金充足性
- 美股 (US): FINRA PDT 规则，资金 < $25k 限 3 次日内交易/5 交易日
确保反手逻辑不会违反券商合规要求。
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Any, Optional

logger = logging.getLogger("ftseek.compliance.account_manager")

class AccountStatus(Enum):
    NORMAL = "normal"
    PDT_RESTRICTED = "pdt_restricted"       # 美股 PDT 限制
    CASH_SETTLEMENT_RESTRICTED = "cash_settlement_restricted"
    HALT_INTRADAY_REVERSE = "halt_intraday_reverse"
    INSUFFICIENT_FUNDS = "insufficient_funds"

class AccountComplianceManager:
    PDT_EQUITY_THRESHOLD = 25000.0
    MAX_DAY_TRADES = 3

    def __init__(self, config_path: str = None):
        self.config_path = config_path or "data/account_state.json"
        self.state = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        
        return {
            "account_type": "margin",
            "equity": 100000.0,
            "day_trades": [],
            "last_reset_date": datetime.now().strftime("%Y-%m-%d")
        }

    def save_state(self):
        try:
            os.makedirs(os.path.dirname(self.config_path) if os.path.dirname(self.config_path) else ".", exist_ok=True)
            with open(self.config_path, 'w') as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.error(f"保存账户状态失败: {e}")

    def update_equity(self, equity: float):
        """更新账户资金"""
        self.state["equity"] = equity
        self.save_state()

    def record_day_trade(self, symbol: str):
        """记录一次日内交易 (仅美股需要)"""
        self._clean_old_day_trades()
        self.state["day_trades"].append({
            "timestamp": time.time(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "symbol": symbol
        })
        self.save_state()
        logger.info(f"[合规] 记录日内交易: {symbol}, 当前计数(5日)={self._get_day_trade_count()}")

    def _clean_old_day_trades(self):
        """清理超过 5 个日历日的交易记录 (近似交易日滑动窗口)"""
        cutoff = time.time() - (5 * 86400)
        self.state["day_trades"] = [
            t for t in self.state["day_trades"]
            if t.get("timestamp", 0) > cutoff
        ]

    def _get_day_trade_count(self) -> int:
        """获取 5 日窗口内日内交易次数"""
        self._clean_old_day_trades()
        return len(self.state["day_trades"])

    def can_trade_intraday(self, symbol: str, market: str = "HK") -> bool:
        """
        检查是否允许进行日内交易 (反手属于日内交易)

        Args:
            symbol: 股票代码
            market: 市场类型 ('HK' 或 'US')，默认 'HK'

        规则:
            港股 (HK): T+0 回转，无日内次数限制，仅检查资金
            美股 (US): PDT 规则，资金 < $25k 限 3 次/5 日
        """
        equity = self.state.get("equity", 0)
        account_type = self.state.get("account_type", "margin")

        # ── 港股: 仅检查资金充足性 (T+0 无 PDT 限制) ──
        if market == "HK":
            if equity <= 0:
                logger.warning(f"[合规-HK] {symbol} 账户资金异常 (equity={equity})")
                return False
            return True

        # ── 美股: PDT 规则检查 ──
        if market == "US":
            # 资金 >= $25k，豁免 PDT
            if equity >= self.PDT_EQUITY_THRESHOLD:
                return True

            # 保证金账户且资金不足 $25k，检查日内交易次数
            if account_type == "margin":
                count = self._get_day_trade_count()
                if count >= self.MAX_DAY_TRADES:
                    logger.warning(
                        f"[合规-US] PDT 限制触发! {symbol} 日内交易被禁止 "
                        f"(5日内 {count} 次 >= {self.MAX_DAY_TRADES}, equity=${equity:,.0f})"
                    )
                    return False

            return True

        # 未知市场类型，默认放行 (保守策略)
        logger.warning(f"[合规] 未知市场类型 '{market}' for {symbol}, 默认放行")
        return True

    def get_status(self) -> AccountStatus:
        equity = self.state.get("equity", 0)
        account_type = self.state.get("account_type", "margin")

        if equity <= 0:
            return AccountStatus.INSUFFICIENT_FUNDS

        if account_type == "margin" and equity < self.PDT_EQUITY_THRESHOLD:
            if self._get_day_trade_count() >= self.MAX_DAY_TRADES:
                return AccountStatus.PDT_RESTRICTED

        return AccountStatus.NORMAL

    def get_status_report(self) -> Dict[str, Any]:
        """返回合规状态报告"""
        status = self.get_status()
        return {
            "status": status.value,
            "equity": self.state.get("equity", 0),
            "account_type": self.state.get("account_type", "margin"),
            "day_trade_count_5d": self._get_day_trade_count(),
            "is_pdt_exempt": self.state.get("equity", 0) >= self.PDT_EQUITY_THRESHOLD,
            "pdt_threshold": self.PDT_EQUITY_THRESHOLD,
            "max_day_trades": self.MAX_DAY_TRADES,
        }
