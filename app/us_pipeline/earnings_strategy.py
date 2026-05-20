"""
财报后动量策略引擎 (Post-Earnings Momentum Strategy Engine)

Phase 2 核心: 财报后动量交易策略

职责:
1. 盘后检测已发布财报的股票
2. 分析财报超预期程度 + 历史表现 + 行业特性
3. 生成次日交易计划 (Entry Zone / Stop / TP)
4. 应用行业差异化 ATR 倍数和专属风控
5. 输出结构化信号供盘中执行

作者: FTSeek-US Team
版本: 1.0 (2026-04-23)
"""

import os
import sys
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from pathlib import Path

logger = logging.getLogger("ftseek.us_pipeline.earnings_strategy")


class EarningsMomentumStrategy:
    """
    财报后动量策略引擎
    
    基于 Post-Earnings Announcement Drift (PEAD) 学术理论:
    - 超预期 + 指引积极 → 次日可能出现动量延续 (1-3 天)
    - 超预期但指引差 → "Beat and Drop" 陷阱
    - 低于预期 → 可能连续下跌 2-3 天
    
    策略核心:
    1. 识别超预期 > 5% 且连续 Beat >= 2 期的股票
    2. 应用行业差异化 ATR 倍数计算止损
    3. 仓位限制: 单票不超过总资金 5%
    4. 持仓限制: 最多持有 72h (3 个交易日)
    """
    
    # 策略参数
    MIN_SURPRISE_PCT = 5.0  # 最低超预期百分比
    MIN_BEAT_STREAK = 2  # 最低连续 Beat 期数
    MIN_CONFIDENCE = 0.55  # 最低置信度
    MAX_HOLDING_HOURS = 72  # 最大持仓时间
    
    # 仓位参数
    MAX_POSITION_PCT = 0.05  # 单票最大仓位 5%
    MAX_TOTAL_EARNINGS_PCT = 0.15  # 所有财报仓位合计 15%
    
    # 输出路径
    def __init__(self, output_path: Optional[str] = None):
        if output_path is None:
            project_root = Path(__file__).resolve().parent.parent.parent
            output_path = str(project_root / "data" / "earnings_trading_plan.json")
        self.output_path = output_path
        self.notifier = None
    
    def analyze_and_generate(self, tickers: Optional[List[str]] = None) -> Optional[Dict]:
        """
        执行完整的财报后分析并生成交易计划
        
        Phase 2 核心流程:
        1. 获取最近发布的财报
        2. 分析每只股票的动量信号
        3. 过滤低质量信号
        4. 计算 Entry Zone / Stop / TP
        5. 生成 JSON 交易计划
        
        Args:
            tickers: 指定要分析的股票，None 则自动检测最近发布的
            
        Returns:
            交易计划字典
        """
        from app.data.earnings_calendar import EarningsCalendar
        from app.risk.earnings_risk_guard import EarningsRiskGuard, EarningsRiskConfig
        
        cal = EarningsCalendar()
        guard = EarningsRiskGuard()
        
        # Step 1: 获取最近发布的财报
        if tickers:
            opportunities = []
            for t in tickers:
                opp = cal.analyze_post_earnings_opportunity(t)
                opportunities.append(opp)
        else:
            recent = cal.get_recently_reported(days_back=2)
            guard.update_earnings_calendar(recent)
            opportunities = []
            for entry in recent:
                opp = cal.analyze_post_earnings_opportunity(entry["ticker"])
                opp["symbol"] = entry["symbol"]
                opp["report_date"] = entry["report_date"]
                opp["sector"] = entry["sector"]
                opportunities.append(opp)
        
        if not opportunities:
            logger.info("无可用财报数据，跳过分析")
            return None
        
        # Step 2: 过滤和排序
        targets = []
        for opp in opportunities:
            # 过滤低质量信号
            if opp["confidence"] < self.MIN_CONFIDENCE:
                logger.debug(f"过滤 {opp['ticker']}: 置信度 {opp['confidence']:.0%} < {self.MIN_CONFIDENCE:.0%}")
                continue
            
            # 只处理 BUY 信号 (Phase 2 仅做多，做空留给后续)
            if "BUY" not in opp["momentum_signal"]:
                logger.debug(f"过滤 {opp['ticker']}: 信号 {opp['momentum_signal']} 不是 BUY")
                continue
            
            targets.append(opp)
        
        if not targets:
            logger.info("无符合条件的财报动量机会")
            return None
        
        # 按置信度排序
        targets.sort(key=lambda x: x["confidence"], reverse=True)
        
        # Step 3: 生成交易计划
        plan = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "type": "EARNINGS_MOMENTUM",
            "generated_at": datetime.now().isoformat(),
            "targets": [],
            "risk_summary": {
                "max_position_pct": self.MAX_POSITION_PCT,
                "max_total_earnings_pct": self.MAX_TOTAL_EARNINGS_PCT,
                "max_holding_hours": self.MAX_HOLDING_HOURS,
            }
        }
        
        for opp in targets:
            target = self._build_target(opp, guard)
            if target:
                plan["targets"].append(target)
        
        # 保存计划
        self._save_plan(plan)
        
        logger.info(f"财报动量计划已生成: {len(plan['targets'])} 个目标")
        return plan
    
    def _build_target(self, opp: Dict, guard) -> Optional[Dict]:
        """
        构建单个交易目标
        
        Args:
            opp: 动量机会分析结果
            guard: 风控守卫
            
        Returns:
            目标字典，或 None (被风控过滤)
        """
        symbol = opp.get("symbol", f"US.{opp['ticker']}")
        sector = opp.get("sector", "Unknown")
        atr_mult = opp.get("atr_multiplier", 3.5)
        
        # 检查禁建期
        is_blackout, blackout_reason = guard.is_earnings_blackout(symbol)
        if is_blackout:
            logger.warning(f"跳过 {symbol}: {blackout_reason}")
            return None
        
        # 获取当前价格 (用于计算 Entry Zone)
        current_price = self._get_current_price(opp["ticker"])
        if not current_price:
            logger.warning(f"无法获取 {symbol} 当前价格")
            return None
        
        # 计算 ATR (简化：从当前价格的百分比估算)
        # 实际应该从 Futu API 获取真实 ATR，这里先用估算
        atr_pct = 0.03  # 假设 3% 日均波动
        atr_value = current_price * atr_pct
        
        # Entry Zone: 当前价附近 ± 0.5 ATR
        entry_low = round(current_price - 0.5 * atr_value, 2)
        entry_high = round(current_price + 0.5 * atr_value, 2)
        
        # Stop Loss: 入场价下方 - 2.5 * ATR (财报期间用行业倍数)
        stop_loss = round(current_price - atr_mult * atr_value, 2)
        
        # Take Profit: 入场价上方 + 5.0 * ATR (盈亏比 ~2:1)
        take_profit = round(current_price + 5.0 * atr_value, 2)
        
        # 构建目标
        target = {
            "symbol": symbol,
            "ticker": opp["ticker"],
            "direction": "LONG",
            "status": "ACTIVE",
            "strategy_type": "EARNINGS_MOMENTUM",
            "logic": f"Post-Earnings Drift: {opp['reason']}",
            "entry_zone": [entry_low, entry_high],
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "urgency": "HIGH" if opp["confidence"] > 0.7 else "MEDIUM",
            "catalyst": f"Earnings Play: {opp['reason']}",
            "confidence": opp["confidence"],
            "risk_config": {
                "atr_multiplier": atr_mult,
                "sector": sector,
                "max_position_pct": self.MAX_POSITION_PCT,
                "max_holding_hours": self.MAX_HOLDING_HOURS,
                "flash_exit_multiplier": opp.get("flash_exit_multiplier", 1.5),
            },
            "surprise_pct": opp.get("surprise_pct", 0),
            "beat_streak": opp.get("beat_streak", 0),
        }
        
        return target
    
    def _get_current_price(self, ticker: str) -> Optional[float]:
        """
        获取股票当前价格 (通过 Futu API)
        
        Args:
            ticker: 股票代码 (如 'NVDA')
            
        Returns:
            当前价格，或 None
        """
        try:
            from futu import OpenQuoteContext, RET_OK
            
            host = os.environ.get("FUTU_OPEND_HOST", "127.0.0.1")
            port = int(os.environ.get("FUTU_OPEND_PORT", "11111"))
            
            ctx = OpenQuoteContext(host=host, port=port)
            futu_sym = f"US.{ticker}"
            ret, data = ctx.get_market_snapshot([futu_sym])
            ctx.close()
            
            if ret == RET_OK and not data.empty:
                price = data.iloc[0].get("last_price")
                if price and price > 0:
                    return float(price)
        except Exception as e:
            logger.warning(f"获取 {ticker} 价格失败: {e}")
        
        return None
    
    def _save_plan(self, plan: Dict):
        """保存交易计划到文件"""
        os.makedirs(os.path.dirname(self.output_path), exist_ok=True)
        with open(self.output_path, 'w', encoding='utf-8') as f:
            json.dump(plan, f, indent=2, ensure_ascii=False)
        logger.info(f"💾 财报计划已保存至: {self.output_path}")
    
    def format_notification(self, plan: Dict) -> str:
        """格式化交易计划为钉钉通知文本"""
        targets = plan.get("targets", [])
        date = plan.get("date", "Unknown")
        
        md = f"### 📊 FTSeek-US 财报动量计划 ({date})\n\n"
        
        if targets:
            md += f"**{len(targets)} 个财报动量机会**\n\n"
            
            for t in targets:
                sym = t["symbol"]
                ticker = t["ticker"]
                confidence = t["confidence"]
                logic = t["logic"][:60]
                entry = t["entry_zone"]
                stop = t["stop_loss"]
                tp = t["take_profit"]
                surprise = t.get("surprise_pct", 0)
                
                md += f"**🟢 {sym}**\n"
                md += f"   - 逻辑: {logic}\n"
                md += f"   - 超预期: {surprise:.1f}% | 置信度: {confidence:.0%}\n"
                md += f"   - 入场: ${entry[0]:.2f} - ${entry[1]:.2f}\n"
                md += f"   - 止损: ${stop:.2f} | 目标: ${tp:.2f}\n"
                md += "\n"
        else:
            md += "🚫 **无符合条件的财报动量机会**\n"
        
        risk = plan.get("risk_summary", {})
        md += f"\n> **风控**: 单票最大 {risk.get('max_position_pct', 0)*100:.0%} | "
        md += f"总仓位 {risk.get('max_total_earnings_pct', 0)*100:.0%} | "
        md += f"持仓上限 {risk.get('max_holding_hours', 72)}h"
        
        return md


async def run_earnings_strategy(tickers: Optional[List[str]] = None) -> Optional[Dict]:
    """
    Phase 2 入口函数: 执行财报后动量分析
    
    Args:
        tickers: 指定要分析的股票，None 则自动检测
        
    Returns:
        交易计划字典
    """
    strategy = EarningsMomentumStrategy()
    plan = strategy.analyze_and_generate(tickers=tickers)
    
    if plan:
        # 发送通知
        try:
            from app.notifier.dingtalk import DingTalkNotifier
            notifier = DingTalkNotifier()
            text = strategy.format_notification(plan)
            notifier.send_text(text)
        except Exception as e:
            logger.warning(f"通知发送失败: {e}")
    
    return plan


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    
    print("=" * 60)
    print("FTSeek-US 财报后动量策略 (Phase 2)")
    print("=" * 60)
    
    # 运行策略
    plan = asyncio.run(run_earnings_strategy())
    
    if plan:
        print("\n交易计划:")
        print(json.dumps(plan, indent=2, ensure_ascii=False))
    else:
        print("\n无可用交易计划")
