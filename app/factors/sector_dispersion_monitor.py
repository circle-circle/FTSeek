"""
行业分散度监控模块 (Sector Dispersion Monitor)

实时监控持仓的行业集中度，防止单一板块过度集中。
支持告警和自动拦截机制。

作者: FTSeek Team
日期: 2026-04-15
"""

import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger("ftseek.factors.sector_dispersion_monitor")


# 行业映射表 (代码 → 行业)
SECTOR_MAP = {
    # 互联网
    'HK.09618': ('互联网', '电商/物流'),
    'HK.03690': ('互联网', '本地生活'),
    'HK.01024': ('互联网', '短视频/直播'),
    'HK.00700': ('互联网', '综合'),
    'HK.00941': ('红利科技', '通信'),  # 中国移动
    'US.JD': ('互联网', '电商'),
    'US.PDD': ('互联网', '电商'),
    'US.BABA': ('互联网', '电商'),
    # 半导体
    'HK.00981': ('半导体', '代工'),
    'HK.01347': ('半导体', '代工'),
    'US.MU': ('半导体', '存储'),
    'US.NVDA': ('半导体', 'GPU/AI'),
    'US.AMD': ('半导体', 'CPU/GPU'),
    'US.TSM': ('半导体', '代工'),
    # 科技
    'US.GOOG': ('科技', '搜索/云/AI'),
    'US.GOOGL': ('科技', '搜索/云/AI'),
    'US.MSFT': ('科技', '云/软件'),
    'US.AAPL': ('科技', '消费电子'),
    'US.AMZN': ('科技', '电商/云'),
    'US.META': ('科技', '社交/AI'),
    # 消费
    'HK.09992': ('消费', '潮玩'),
    'HK.01810': ('消费', '手机/IoT'),
    'HK.02020': ('消费', '运动服饰'),
    'HK.01928': ('消费', '博彩旅游'),  # 金沙中国
    # 汽车
    'HK.09868': ('汽车', '新能源'),
    'HK.02015': ('汽车', '新能源'),
    'US.TSLA': ('汽车', '新能源'),
    'US.NIO': ('汽车', '新能源'),
    'US.LI': ('汽车', '新能源'),
    'US.XPEV': ('汽车', '新能源'),
    # 医药
    'HK.01093': ('医药', '制药'),
    'HK.01801': ('医药', '生物科技'),
    'HK.06618': ('医药', '生物科技'),
    'HK.09926': ('医药', '生物科技'),
    # 金融
    'HK.00388': ('金融', '交易所'),
    # 'HK.00939': ('金融', '银行'), # Removed
    'HK.01299': ('金融', '保险'),
    'HK.02318': ('金融', '保险'),
    # 周期/能源
    'HK.01378': ('周期', '有色金属'),
    'HK.00883': ('能源', '石油'),
    'HK.00857': ('能源', '石油'),
    'HK.02899': ('资源', '金铜'),  # 紫金矿业
}


@dataclass
class SectorStats:
    """行业统计"""
    sector: str
    sub_sector: str
    stock_count: int = 0
    total_market_val: float = 0.0
    total_pl: float = 0.0
    avg_pl_pct: float = 0.0
    stocks: List[Dict] = field(default_factory=list)
    concentration_pct: float = 0.0
    status: str = "normal"  # normal / warning / critical


@dataclass
class DispersionReport:
    """分散度报告"""
    timestamp: str = ""
    total_positions: int = 0
    total_sectors: int = 0
    total_market_val: float = 0.0
    total_pl: float = 0.0
    sector_stats: Dict[str, SectorStats] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    critical_alerts: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)


class SectorDispersionMonitor:
    """
    行业分散度监控器

    功能:
    1. 实时计算各行业持仓集中度
    2. 触发告警 (30%) 和拦截 (40%)
    3. 生成分散度报告
    4. 提供调仓建议
    """

    def __init__(self, alert_threshold: float = 30.0, block_threshold: float = 40.0,
                 max_per_sector: int = 4):
        self.alert_threshold = alert_threshold
        self.block_threshold = block_threshold
        self.max_per_sector = max_per_sector
        self._last_report: Optional[DispersionReport] = None
        self._last_report_time: float = 0
        self._cache_ttl = 60  # 60秒缓存

    def get_sector(self, code: str) -> Tuple[str, str]:
        """获取股票所属行业 (sector, sub_sector)"""
        return SECTOR_MAP.get(code, ('未知', '未知'))

    def analyze(self, positions: List[Dict], force_refresh: bool = False) -> DispersionReport:
        """
        分析持仓分散度

        Args:
            positions: 持仓列表, 每项包含 {code, name, market_val, pl, pl_pct}
            force_refresh: 强制刷新

        Returns:
            DispersionReport
        """
        now = time.time()
        if (not force_refresh and self._last_report and
                (now - self._last_report_time) < self._cache_ttl):
            return self._last_report

        report = DispersionReport(
            timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            total_positions=len(positions),
        )

        # 按行业分组
        sector_groups: Dict[str, Dict] = {}
        for pos in positions:
            code = pos.get('code', '')
            sector, sub = self.get_sector(code)
            key = sector
            if key not in sector_groups:
                sector_groups[key] = {
                    'sector': sector,
                    'sub_sectors': set(),
                    'stocks': [],
                    'total_val': 0,
                    'total_pl': 0,
                }
            sector_groups[key]['sub_sectors'].add(sub)
            sector_groups[key]['stocks'].append(pos)
            sector_groups[key]['total_val'] += pos.get('market_val', 0)
            sector_groups[key]['total_pl'] += pos.get('pl', 0)

        # 计算统计
        total_val = sum(g['total_val'] for g in sector_groups.values())
        report.total_market_val = total_val
        report.total_pl = sum(pos.get('pl', 0) for pos in positions)
        report.total_sectors = len(sector_groups)

        for sector, group in sector_groups.items():
            count = len(group['stocks'])
            concentration = (count / len(positions) * 100) if positions else 0

            # 计算平均盈亏
            pl_values = [p.get('pl_pct', 0) for p in group['stocks'] if p.get('pl_pct') is not None]
            avg_pl = sum(pl_values) / len(pl_values) if pl_values else 0

            # 判断状态
            if concentration >= self.block_threshold or count >= self.max_per_sector:
                status = "critical"
                report.critical_alerts.append(
                    f"🔴 {sector}: {count}只 ({concentration:.0f}%) "
                    f"超过上限 (max={self.max_per_sector}只/{self.block_threshold}%)"
                )
            elif concentration >= self.alert_threshold:
                status = "warning"
                report.warnings.append(
                    f"🟡 {sector}: {count}只 ({concentration:.0f}%) "
                    f"接近上限 (max={self.max_per_sector}只/{self.block_threshold}%)"
                )
            else:
                status = "normal"

            report.sector_stats[sector] = SectorStats(
                sector=sector,
                sub_sector=', '.join(group['sub_sectors']),
                stock_count=count,
                total_market_val=group['total_val'],
                total_pl=group['total_pl'],
                avg_pl_pct=avg_pl,
                stocks=group['stocks'],
                concentration_pct=concentration,
                status=status,
            )

        # 生成建议
        report.recommendations = self._generate_recommendations(report)

        self._last_report = report
        self._last_report_time = now
        return report

    def can_buy(self, code: str, positions: List[Dict]) -> Tuple[bool, str]:
        """
        检查是否可以买入某股票 (行业集中度检查)

        Returns:
            (can_buy, reason)
        """
        sector, _ = self.get_sector(code)

        # 统计该行业当前持仓数
        sector_count = 0
        for pos in positions:
            p_sector, _ = self.get_sector(pos.get('code', ''))
            if p_sector == sector:
                sector_count += 1

        # 检查数量上限
        if sector_count >= self.max_per_sector:
            return False, f"行业 {sector} 已达上限 ({sector_count}/{self.max_per_sector})"

        # 检查占比上限
        total = len(positions) + 1  # +1 for new position
        if total > 0 and ((sector_count + 1) / total * 100) >= self.block_threshold:
            return False, (
                f"行业 {sector} 集中度将达 {((sector_count+1)/total*100):.0f}% "
                f"≥ {self.block_threshold}%"
            )

        return True, f"行业 {sector} 当前 {sector_count} 只，可买入"

    def _generate_recommendations(self, report: DispersionReport) -> List[str]:
        """生成调仓建议"""
        recs = []

        # 1. 过度集中行业 → 建议减持
        for sector, stats in report.sector_stats.items():
            if stats.status == "critical":
                recs.append(
                    f"减持 {sector} 板块 (当前 {stats.stock_count}只, "
                    f"占比 {stats.concentration_pct:.0f}%)，建议减至 {self.max_per_sector} 只以下"
                )

        # 2. 缺少配置的板块 → 建议关注
        missing_sectors = set(SECTOR_MAP.values()) - set(
            (s.sector, s.sub_sector) for s in report.sector_stats.values()
        )
        if missing_sectors and report.total_positions < 15:
            top_missing = list(missing_sectors)[:3]
            recs.append(
                f"建议关注未配置板块: {', '.join(s[0] for s in top_missing)}"
            )

        # 3. 分散度评估
        if report.total_sectors >= 6 and report.total_positions >= 10:
            recs.append("✅ 行业分散度良好")
        elif report.total_sectors < 4:
            recs.append("⚠️ 行业过于集中，建议分散至 4+ 个板块")

        return recs

    def format_report(self, report: DispersionReport) -> str:
        """格式化报告为文本 (用于钉钉推送)"""
        lines = [
            f"### 📊 行业分散度报告 | {report.timestamp}",
            f"",
            f"**总持仓**: {report.total_positions} 只 | **行业**: {report.total_sectors} 个",
            f"**总市值**: {report.total_market_val:,.0f} | **总盈亏**: {report.total_pl:+,.0f}",
            f"",
            f"---",
            f"",
        ]

        # 行业详情
        for sector, stats in sorted(
                report.sector_stats.items(), key=lambda x: -x[1].stock_count):
            icon = {"normal": "✅", "warning": "🟡", "critical": "🔴"}.get(stats.status, "⚪")
            lines.append(
                f"{icon} **{stats.sector}** ({stats.stock_count}只 | {stats.concentration_pct:.0f}%) | "
                f"盈亏: {stats.total_pl:+,.0f} ({stats.avg_pl_pct:+.1f}%)"
            )
            for s in stats.stocks:
                pnl = s.get('pl_pct', 0)
                icon2 = "📈" if pnl >= 0 else "📉"
                lines.append(
                    f"   {icon2} {s.get('code', '?')} {s.get('name', '?')[:6]} | "
                    f"{pnl:+.1f}%"
                )
            lines.append("")

        # 告警
        if report.warnings:
            lines.append("### ⚠️ 警告")
            for w in report.warnings:
                lines.append(f"- {w}")
            lines.append("")

        if report.critical_alerts:
            lines.append("### 🔴 严重告警")
            for c in report.critical_alerts:
                lines.append(f"- {c}")
            lines.append("")

        # 建议
        if report.recommendations:
            lines.append("### 💡 建议")
            for r in report.recommendations:
                lines.append(f"- {r}")
            lines.append("")

        lines.append("---")
        lines.append("⚡ 行业分散度监控 · 自动分析 · 仅供参考")

        return "\n".join(lines)


# 全局单例
_monitor = None


def get_sector_monitor(
    alert_threshold: float = None,
    block_threshold: float = None,
    max_per_sector: int = None,
) -> SectorDispersionMonitor:
    """获取全局单例"""
    global _monitor
    if _monitor is None:
        from app import config
        alert_threshold = alert_threshold or getattr(config, 'SECTOR_ALERT_THRESHOLD', 30.0)
        block_threshold = block_threshold or getattr(config, 'SECTOR_BLOCK_THRESHOLD', 40.0)
        max_per_sector = max_per_sector or getattr(config, 'MAX_POSITIONS_PER_SECTOR', 4)
        _monitor = SectorDispersionMonitor(alert_threshold, block_threshold, max_per_sector)
    return _monitor


def reset_monitor():
    """重置 (测试用)"""
    global _monitor
    _monitor = None
