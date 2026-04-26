"""
仓位管理模块 (Position Sizing Manager)

基于"不破产"原则的仓位管理框架。

核心规则:
1. 单股最大占比 ≤ 10% (黑天鹅 -50% 时仅损 5% 总资金)
2. 单行业最大占比 ≤ 25% (行业 -40% 时仅损 10% 总资金)
3. 最低现金缓冲 ≥ 10% (防极端行情)
4. 总亏损 -15% → 清仓熔断
5. 每日亏损 -3% → 当日停交易
6. 个股止损 8%, 止盈 15%

作者: FTSeek Team
日期: 2026-04-15
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger("ftseek.factors.position_sizing")


# 行业映射 (与 sector_dispersion_monitor 保持一致)
SECTOR_MAP = {
    'HK.09618': '互联网', 'HK.03690': '互联网', 'HK.01024': '互联网',
    'HK.00700': '互联网', 'US.JD': '互联网', 'US.PDD': '互联网', 'US.BABA': '互联网',
    'HK.00981': '半导体', 'HK.01347': '半导体', 'US.MU': '半导体',
    'US.NVDA': '半导体', 'US.AMD': '半导体', 'US.TSM': '半导体',
    'US.GOOG': '科技', 'US.GOOGL': '科技', 'US.MSFT': '科技',
    'US.AAPL': '科技', 'US.AMZN': '科技', 'US.META': '科技',
    'HK.09992': '消费', 'HK.01810': '消费', 'HK.02020': '消费',
    'HK.09868': '汽车', 'HK.02015': '汽车', 'US.TSLA': '汽车',
    'HK.01093': '医药', 'HK.01801': '医药', 'HK.06618': '医药', 'HK.09926': '医药',
    'HK.00388': '金融', 'HK.00939': '金融', 'HK.01299': '金融', 'HK.02318': '金融',
    'HK.01378': '周期', 'HK.00883': '能源', 'HK.00857': '能源',
}


@dataclass
class PositionSizeResult:
    """仓位计算结果"""
    code: str
    suggested_quantity: int = 0
    suggested_amount: float = 0.0
    position_pct: float = 0.0
    risk_pct: float = 0.0          # 止损时损失占总资金 %
    black_swan_risk: float = 0.0   # 黑天鹅 -50% 损失占总资金 %
    approved: bool = False
    reason: str = ""
    warnings: List[str] = field(default_factory=list)


@dataclass
class PortfolioRiskReport:
    """组合风险报告"""
    timestamp: str = ""
    total_capital: float = 0.0
    total_market_value: float = 0.0
    cash: float = 0.0
    cash_pct: float = 0.0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    drawdown_pct: float = 0.0
    daily_loss_pct: float = 0.0
    monthly_loss_pct: float = 0.0      # Phase A 新增: 月度亏损 %

    # 合规检查
    position_violations: List[Dict] = field(default_factory=list)
    sector_violations: List[Dict] = field(default_factory=list)
    cash_violation: bool = False
    drawdown_violation: bool = False
    daily_loss_violation: bool = False
    monthly_loss_violation: bool = False  # Phase A 新增

    # 极端情景
    max_stop_loss_loss: float = 0.0    # 全部止损总损失
    max_black_swan_loss: float = 0.0   # 黑天鹅总损失
    max_sector_crash_loss: float = 0.0 # 最大行业崩盘损失

    # 状态
    can_trade: bool = True
    trade_block_reason: str = ""
    recommendations: List[str] = field(default_factory=list)


class PositionSizingManager:
    """
    仓位管理器

    实现基于资金安全的仓位计算和风控检查。
    """

    def __init__(
        self,
        max_position_pct: float = 8.0,      # 2026-04-18: 10%->8%, Phase A 安全底线优先
        max_sector_pct: float = 20.0,        # 2026-04-18: 25%->20%, 更严格的事件链控制
        min_cash_reserve_pct: float = 15.0,  # 2026-04-18: 10%->15%, 安全缓冲
        max_drawdown_pct: float = 15.0,
        max_daily_loss_pct: float = 3.0,
        max_monthly_loss_pct: float = 15.0,  # 2026-04-18 Phase A 新增
        stop_loss_pct: float = 8.0,
    ):
        self.max_position_pct = max_position_pct
        self.max_sector_pct = max_sector_pct
        self.min_cash_reserve_pct = min_cash_reserve_pct
        self.max_drawdown_pct = max_drawdown_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_monthly_loss_pct = max_monthly_loss_pct
        self.stop_loss_pct = stop_loss_pct

        self._daily_loss_tracker: Dict[str, float] = {}  # date -> loss
        self._monthly_loss_tracker: Dict[str, float] = {}  # YYYY-MM -> loss (Phase A 新增)

    def get_sector(self, code: str) -> str:
        """获取行业"""
        return SECTOR_MAP.get(code, '未知')

    def calculate_position_size(
        self,
        code: str,
        price: float,
        total_capital: float,
        current_positions: List[Dict],
        market: str = 'HK',
    ) -> PositionSizeResult:
        """
        计算安全仓位大小

        Args:
            code: 股票代码
            price: 当前价格
            total_capital: 总资金
            current_positions: 当前持仓列表 [{code, market_val, pl_pct, ...}]
            market: 市场 (HK/US)

        Returns:
            PositionSizeResult
        """
        result = PositionSizeResult(code=code)
        warnings = []

        # 获取行业
        sector = self.get_sector(code)

        # 1. 计算当前各行业/个股占比
        current_mv = sum(p.get('market_val', 0) for p in current_positions)
        available_cash = total_capital * (1 - self.min_cash_reserve_pct / 100)

        # 当前行业市值
        sector_mv = sum(
            p.get('market_val', 0) for p in current_positions
            if self.get_sector(p.get('code', '')) == sector
        )
        sector_pct = (sector_mv / total_capital * 100) if total_capital > 0 else 0

        # 检查是否已持有
        existing_pos = None
        for p in current_positions:
            if p.get('code', '') == code:
                existing_pos = p
                break

        if existing_pos:
            current_pct = existing_pos.get('market_val', 0) / total_capital * 100
            result.position_pct = current_pct

            # 已持有 → 检查是否可以补仓
            if current_pct >= self.max_position_pct:
                result.approved = False
                result.reason = f"单股占比 {current_pct:.1f}% 已达上限 {self.max_position_pct}%"
                return result

            # 补仓上限: max_position_pct - current_pct
            max_add_pct = self.max_position_pct - current_pct
            max_add_pct = min(max_add_pct, self.max_sector_pct - sector_pct)
            max_add_pct = min(max_add_pct, self.min_cash_reserve_pct)
            max_add_amount = total_capital * max_add_pct / 100
            result.suggested_quantity = int(max_add_amount / price)
            result.suggested_amount = result.suggested_quantity * price
            result.position_pct = (current_mv + result.suggested_amount) / total_capital * 100
            result.risk_pct = result.suggested_amount / total_capital * self.stop_loss_pct / 100 * 100
            result.black_swan_risk = result.suggested_amount / total_capital * 50 / 100 * 100
            result.approved = result.suggested_quantity > 0
            result.reason = (f"补仓上限 {max_add_pct:.1f}% = "
                            f"{result.suggested_quantity} 股 @ {price}")
            return result

        # 新仓计算
        # 可用资金上限 (考虑现金缓冲)
        investable = total_capital - current_mv
        investable = min(investable, total_capital * (1 - self.min_cash_reserve_pct / 100))

        # 行业上限约束
        sector_budget = total_capital * self.max_sector_pct / 100 - sector_mv
        investable = min(investable, sector_budget)

        # 单股上限约束
        max_single_amount = total_capital * self.max_position_pct / 100
        investable = min(investable, max_single_amount)

        # 计算数量
        if price <= 0:
            result.approved = False
            result.reason = "价格为0或负数"
            return result

        raw_qty = int(investable / price)

        # 对齐整手
        if market == 'HK':
            lot = 100  # 简化处理, 实际应查每手股数
            raw_qty = (raw_qty // lot) * lot
        elif market == 'US':
            lot = 1
            raw_qty = int(raw_qty)

        result.suggested_quantity = raw_qty
        result.suggested_amount = raw_qty * price
        result.position_pct = result.suggested_amount / total_capital * 100 if total_capital > 0 else 0
        result.risk_pct = result.position_pct * self.stop_loss_pct / 100
        result.black_swan_risk = result.position_pct * 50 / 100

        # 合规检查
        if result.suggested_quantity <= 0:
            result.approved = False
            result.reason = "可用资金不足"
            return result

        # 检查行业集中度
        new_sector_pct = (sector_mv + result.suggested_amount) / total_capital * 100
        if new_sector_pct > self.max_sector_pct:
            result.approved = False
            result.reason = f"行业 {sector} 占比将达 {new_sector_pct:.1f}% > {self.max_sector_pct}%"
            return result

        # 检查现金缓冲
        new_cash = total_capital - current_mv - result.suggested_amount
        new_cash_pct = new_cash / total_capital * 100
        if new_cash_pct < self.min_cash_reserve_pct:
            warnings.append(
                f"现金缓冲将降至 {new_cash_pct:.1f}% < {self.min_cash_reserve_pct}%"
            )

        result.approved = True
        result.reason = (
            f"建议买入 {raw_qty} 股 @ {price} = {result.suggested_amount:,.0f} "
            f"(占比 {result.position_pct:.1f}%, 止损风险 {result.risk_pct:.1f}%)"
        )
        result.warnings = warnings
        return result

    def assess_portfolio_risk(
        self,
        total_capital: float,
        current_positions: List[Dict],
        daily_pnl: float = 0.0,
        initial_capital: float = None,
    ) -> PortfolioRiskReport:
        """
        评估组合风险

        Args:
            total_capital: 当前总资金 (市值+现金)
            current_positions: 当前持仓
            daily_pnl: 当日盈亏
            initial_capital: 初始资金 (用于计算回撤)

        Returns:
            PortfolioRiskReport
        """
        report = PortfolioRiskReport(
            timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            total_capital=total_capital,
        )

        # 基本统计
        total_mv = sum(p.get('market_val', 0) for p in current_positions)
        total_pl = sum(p.get('pl_val', 0) for p in current_positions)
        cash = total_capital - total_mv

        report.total_market_value = total_mv
        report.cash = cash
        report.cash_pct = cash / total_capital * 100 if total_capital > 0 else 0
        report.total_pnl = total_pl
        report.total_pnl_pct = total_pl / (total_capital - total_pl) * 100 if (total_capital - total_pl) != 0 else 0
        report.daily_loss_pct = daily_pnl / total_capital * 100 if total_capital > 0 else 0

        # Phase A 新增: 月度亏损追踪
        current_month = datetime.now().strftime('%Y-%m')
        self._daily_loss_tracker[datetime.now().strftime('%Y-%m-%d')] = daily_pnl
        # 累计当月亏损
        month_prefix = datetime.now().strftime('%Y-%m')
        month_total = sum(
            v for k, v in self._daily_loss_tracker.items() 
            if k.startswith(month_prefix) and v < 0
        )
        report.monthly_loss_pct = month_total / total_capital * 100 if total_capital > 0 else 0
        self._monthly_loss_tracker[current_month] = month_total

        # 回撤
        if initial_capital and initial_capital > 0:
            report.drawdown_pct = max(0, (initial_capital - total_capital) / initial_capital * 100)

        # 检查单股违规
        for p in current_positions:
            code = p.get('code', '')
            mv = p.get('market_val', 0)
            pct = mv / total_capital * 100 if total_capital > 0 else 0
            if pct > self.max_position_pct:
                report.position_violations.append({
                    'code': code,
                    'pct': pct,
                    'limit': self.max_position_pct,
                    'excess': pct - self.max_position_pct,
                })

        # 检查行业违规
        sector_mv = {}
        for p in current_positions:
            s = self.get_sector(p.get('code', ''))
            sector_mv[s] = sector_mv.get(s, 0) + p.get('market_val', 0)

        for s, mv in sector_mv.items():
            pct = mv / total_capital * 100 if total_capital > 0 else 0
            if pct > self.max_sector_pct:
                report.sector_violations.append({
                    'sector': s,
                    'pct': pct,
                    'limit': self.max_sector_pct,
                    'excess': pct - self.max_sector_pct,
                    'count': sum(1 for p in current_positions if self.get_sector(p.get('code', '')) == s),
                })

        # 现金缓冲检查
        report.cash_violation = report.cash_pct < self.min_cash_reserve_pct

        # 回撤熔断
        report.drawdown_violation = report.drawdown_pct >= self.max_drawdown_pct

        # 每日亏损检查
        report.daily_loss_violation = abs(report.daily_loss_pct) >= self.max_daily_loss_pct

        # Phase A 新增: 月度亏损检查
        report.monthly_loss_violation = abs(report.monthly_loss_pct) >= self.max_monthly_loss_pct

        # 极端情景分析
        # 全部止损
        report.max_stop_loss_loss = total_mv * self.stop_loss_pct / 100

        # 黑天鹅 (每只 -50%)
        report.max_black_swan_loss = total_mv * 0.50

        # 最大行业崩盘 (-40%)
        max_sector_crash = 0
        for s, mv in sector_mv.items():
            crash = mv * 0.40
            if crash > max_sector_crash:
                max_sector_crash = crash
        report.max_sector_crash_loss = max_sector_crash

        # 能否交易
        if report.drawdown_violation:
            report.can_trade = False
            report.trade_block_reason = f"回撤 {report.drawdown_pct:.1f}% ≥ {self.max_drawdown_pct:.0f}%，触发清仓熔断"
        elif report.monthly_loss_violation:
            # Phase A 新增: 月度亏损拦截
            report.can_trade = False
            report.trade_block_reason = f"本月累计亏损 {abs(report.monthly_loss_pct):.1f}% ≥ {self.max_monthly_loss_pct:.0f}%，暂停交易至次月"
        elif report.daily_loss_violation:
            report.can_trade = False
            report.trade_block_reason = f"今日亏损 {abs(report.daily_loss_pct):.1f}% ≥ {self.max_daily_loss_pct:.0f}%，当日暂停"
        elif report.cash_violation:
            report.can_trade = True  # 仍可交易但不能开新仓
            report.trade_block_reason = f"现金缓冲 {report.cash_pct:.1f}% < {self.min_cash_reserve_pct}%，禁止新开仓"
        else:
            report.can_trade = True

        # 生成建议
        report.recommendations = self._generate_risk_recommendations(report)

        return report

    def _generate_risk_recommendations(self, report: PortfolioRiskReport) -> List[str]:
        """生成风险建议"""
        recs = []

        # 紧急
        if report.drawdown_violation:
            recs.append(
                f"🔴 紧急: 回撤 {report.drawdown_pct:.1f}% 已达熔断线，"
                f"建议清仓所有持仓，剩余资金 {report.total_capital * (1 - self.max_drawdown_pct/100):,.0f}"
            )

        if report.daily_loss_violation:
            recs.append(
                f"🔴 今日已亏损 {abs(report.daily_loss_pct):.1f}%，暂停当日交易"
            )

        # Phase A 新增: 月度亏损建议
        if report.monthly_loss_violation:
            recs.append(
                f"🔴 本月累计亏损 {abs(report.monthly_loss_pct):.1f}% ≥ {self.max_monthly_loss_pct:.0f}%，"
                f"暂停交易至次月，剩余资金 {report.total_capital:,.0f}"
            )

        # 仓位违规
        for v in report.position_violations:
            recs.append(
                f"减仓 {v['code']}: 占比 {v['pct']:.1f}% > {v['limit']:.0f}% "
                f"(超出 {v['excess']:.1f}%)"
            )

        # 行业违规
        for v in report.sector_violations:
            recs.append(
                f"减仓 {v['sector']} 板块: 占比 {v['pct']:.1f}% > {v['limit']:.0f}% "
                f"({v['count']}只, 超出 {v['excess']:.1f}%)"
            )

        # 现金
        if report.cash_violation:
            recs.append(
                f"⚠️ 现金缓冲不足 {report.cash_pct:.1f}%，禁止新开仓"
            )

        # 安全
        if not report.position_violations and not report.sector_violations and not report.cash_violation:
            if report.cash_pct > 30:
                recs.append(f"现金充足 {report.cash_pct:.1f}%，可考虑加仓")
            elif report.cash_pct > self.min_cash_reserve_pct:
                recs.append(f"现金 {report.cash_pct:.1f}% 在安全范围内")

        return recs

    def format_risk_report(self, report: PortfolioRiskReport) -> str:
        """格式化风险报告为文本"""
        status_icon = "🔴" if not report.can_trade else "🟡" if report.position_violations or report.sector_violations else "✅"
        status_text = "禁止交易" if not report.can_trade else "可交易(有警告)" if report.position_violations else "正常"

        lines = [
            f"### {status_icon} 资金安全水位报告 | {report.timestamp}",
            f"",
            f"**状态**: {status_text}",
            f"**总资金**: {report.total_capital:,.0f}",
            f"**持仓市值**: {report.total_market_value:,.0f}",
            f"**现金**: {report.cash:,.0f} ({report.cash_pct:.1f}%)",
            f"**总盈亏**: {report.total_pnl:+,.0f} ({report.total_pnl_pct:+.1f}%)",
            f"**回撤**: {report.drawdown_pct:.1f}% / 熔断线 {self.max_drawdown_pct:.0f}%",
            f"**今日盈亏**: {report.daily_loss_pct:+.1f}% / 限制 {self.max_daily_loss_pct:.0f}%",
            f"**本月盈亏**: {report.monthly_loss_pct:+.1f}% / 限制 {self.max_monthly_loss_pct:.0f}%",
            f"",
            f"---",
            f"",
        ]

        # 极端情景
        lines.append("### ⚠️ 极端情景")
        lines.append(f"| 情景 | 预计损失 | 剩余 |")
        lines.append(f"|------|---------|------|")
        if report.total_capital > 0:
            sl_remain = report.total_capital - report.max_stop_loss_loss
            lines.append(f"| 全部止损 ({self.stop_loss_pct}%) | {report.max_stop_loss_loss:,.0f} ({report.max_stop_loss_loss/report.total_capital*100:.1f}%) | {sl_remain:,.0f} |")
            bs_remain = report.total_capital - report.max_black_swan_loss
            lines.append(f"| 黑天鹅 (-50%) | {report.max_black_swan_loss:,.0f} ({report.max_black_swan_loss/report.total_capital*100:.1f}%) | {bs_remain:,.0f} |")
            sc_remain = report.total_capital - report.max_sector_crash_loss
            lines.append(f"| 最大行业崩盘 (-40%) | {report.max_sector_crash_loss:,.0f} ({report.max_sector_crash_loss/report.total_capital*100:.1f}%) | {sc_remain:,.0f} |")

        lines.append("")

        # 违规
        if report.position_violations:
            lines.append("### 🔴 单股违规")
            for v in report.position_violations:
                lines.append(f"- {v['code']}: {v['pct']:.1f}% (上限 {v['limit']:.0f}%)")
            lines.append("")

        if report.sector_violations:
            lines.append("### 🔴 行业违规")
            for v in report.sector_violations:
                lines.append(f"- {v['sector']}: {v['pct']:.1f}% ({v['count']}只, 上限 {v['limit']:.0f}%)")
            lines.append("")

        # 建议
        if report.recommendations:
            lines.append("### 💡 建议")
            for r in report.recommendations:
                lines.append(f"- {r}")
            lines.append("")

        lines.append("---")
        lines.append("⚡ 资金安全水位监控 · 基准: 不破产")

        return "\n".join(lines)


# 全局单例
_manager = None


def get_position_sizing_manager(
    max_position_pct: float = None,
    max_sector_pct: float = None,
    min_cash_reserve_pct: float = None,
    max_drawdown_pct: float = None,
    max_daily_loss_pct: float = None,
    max_monthly_loss_pct: float = None,  # Phase A 新增
    stop_loss_pct: float = None,
) -> 'PositionSizingManager':
    """获取全局单例"""
    global _manager
    if _manager is None:
        from app import config
        _manager = PositionSizingManager(
            max_position_pct=max_position_pct or getattr(config, 'MAX_POSITION_PCT', 8.0),
            max_sector_pct=max_sector_pct or getattr(config, 'MAX_SECTOR_PCT', 20.0),
            min_cash_reserve_pct=min_cash_reserve_pct or getattr(config, 'MIN_CASH_RESERVE_PCT', 15.0),
            max_drawdown_pct=max_drawdown_pct or getattr(config, 'MAX_PORTFOLIO_DRAWDOWN', 15.0),
            max_daily_loss_pct=max_daily_loss_pct or getattr(config, 'MAX_DAILY_LOSS_PCT', 3.0),
            max_monthly_loss_pct=max_monthly_loss_pct or getattr(config, 'MAX_MONTHLY_LOSS_PCT', 15.0),
            stop_loss_pct=stop_loss_pct or getattr(config, 'STOP_LOSS_PCT', 8.0),
        )
    return _manager


def reset_manager():
    """重置 (测试用)"""
    global _manager
    _manager = None
