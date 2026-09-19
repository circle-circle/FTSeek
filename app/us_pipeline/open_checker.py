"""
美股开盘确认器 (US Open Checker)

职责:
1. 在美东 09:20 (开盘前) 检查盘前行情。
2. 验证 "作战计划" 中的标的是否符合执行条件 (Gap 幅度、量能)。
3. 更新 `daily_battle_plan_us.json` 中的目标状态 (VALID / CANCELLED)。
4. 发送盘前确认通知。
"""

import os
import sys
import json
import time
import logging
import asyncio
from typing import Dict, List, Any
from datetime import datetime

# Add project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.data.market_data_router import MarketDataRouter
from app.us_pipeline.battle_plan_manager import USBattlePlanManager
from app.push import HubTextAdapter
from app.push.message import Category as _CtOpen

logger = logging.getLogger("ftseek.us_pipeline.open_checker")

class USOpenChecker:
    """美股开盘确认器"""

    def __init__(self):
        # 2026-09-18 统一推送渠道: 直连钉钉 → PushHub 单一出口 (适配器保留调用形状)
        self.notifier = HubTextAdapter(category=_CtOpen.PLAN_OPEN_CHECK)
        self.plan_manager = USBattlePlanManager()
        # P0 修复 (2026-04-23): 读取路径对齐到项目根目录/data/
        # premarket_planner 写入为 FTSeek-US/data/daily_battle_plan_us.json，
        # 原代码读的是 app/data/，造成盘前确认永远读不到计划
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.plan_path = os.path.join(project_root, 'data', 'daily_battle_plan_us.json')
        
        # Initialize Market Data Router
        # Reusing existing logic to get keys
        self.router = MarketDataRouter(
            twelvedata_api_key=os.environ.get("TWELVEDATA_API_KEY"),
            finnhub_api_key=os.environ.get("FINNHUB_API_KEY"),
            alphavantage_api_key=os.environ.get("ALPHAVANTAGE_API_KEY"),
            futu_opend_host=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"),
            futu_opend_port=int(os.environ.get("FUTU_OPEND_PORT", "11111")),
            enable_futu_lv3=True
        )

    async def run_check(self):
        """执行盘前检查流程"""
        logger.info("🔍 开始美股盘前开盘确认 (Open Checker)...")

        # 1. 加载计划
        if not self.plan_manager.load(self.plan_path):
            logger.warning("⚠️ 未找到作战计划，跳过确认。")
            return

        plan = self.plan_manager._plan
        targets = plan.get("targets", [])
        
        if not targets:
            logger.info("ℹ️ 计划中无交易目标，跳过。")
            return

        # 2. 检查每个目标
        results = []
        for target in targets:
            symbol = target.get("symbol")
            direction = target.get("direction", "LONG")
            entry_zone = target.get("entry_zone", "")
            
            result = await self._check_symbol(symbol, direction, entry_zone)
            results.append(result)
            
            # 更新目标状态
            target["open_check_status"] = result["status"]
            target["gap_pct"] = result["gap_pct"]
            if result["status"] == "CANCELLED":
                target["cancellation_reason"] = result["reason"]
            else:
                target["confirmed_entry_price"] = result.get("current_price")
                # P0 修复 (2026-06-19): OpenChecker VALID → 设置 status=ACTIVE
                # 根因: signal_pipeline.py:1320 和 signal_stage.py:276 检查
                # target.get("status") == "ACTIVE" 来决定是否走快车道
                # 但之前 OpenChecker 只设置 open_check_status，不设置 status
                # → 所有 VALID 标的 status=None → 快车道从未激活
                # 修复: open_check_status=VALID 的 target 自动获得 status=ACTIVE
                if result["status"] == "VALID":
                    target["status"] = "ACTIVE"
                    logger.info(f"✅ [{symbol}] status → ACTIVE (open_check VALID)")

        # 3. 保存更新后的计划
        self._save_plan(plan)

        # 4. 发送通知
        self._send_summary(results)

        logger.info("✅ 盘前开盘确认完成。")

    async def _check_symbol(self, symbol: str, direction: str, entry_zone: str) -> Dict:
        """检查单个标"""
        # 获取实时报价 (盘前)
        quote = self.router.get_quote(symbol)  # 同步方法，不需要 await

        # O6 (2026-05-16): MarketDataRouter.get_quote 返回 {"price", "prev_close"} (Futu/TwelveData)
        # 或 {"price", "previous_close"} (Finnhub) — 原代码读 last_price/prev_close_price 永远 0
        # → 5/15 5 个 plan_target 全 NO_DATA, 0% 执行率
        current_price = 0.0
        prev_close = 0.0
        if quote:
            current_price = float(
                quote.get("price")
                or quote.get("last_price")
                or 0
            )
            prev_close = float(
                quote.get("prev_close")
                or quote.get("previous_close")
                or quote.get("prev_close_price")
                or current_price
            )

        if not quote or current_price <= 0:
            # 如果没拿到价格，可能还没开盘或数据源问题
            # 默认保持 PRE_MARKET 状态，不 Cancel
            return {
                "symbol": symbol,
                "status": "NO_DATA",
                "gap_pct": 0.0,
                "current_price": 0.0,
                "reason": "No pre-market data available"
            }
        
        # 计算 Gap
        if prev_close > 0:
            gap_pct = (current_price - prev_close) / prev_close
        else:
            gap_pct = 0.0
            
        logger.info(f"📊 [{symbol}] Price: {current_price}, PrevClose: {prev_close}, Gap: {gap_pct:.2%}")

        # 规则判定
        
        # 规则 1: Gap 过大 (3%)
        max_gap = 0.03
        if abs(gap_pct) > max_gap:
            return {
                "symbol": symbol,
                "status": "CANCELLED",
                "gap_pct": gap_pct,
                "reason": f"Gap too large ({gap_pct:.2%} > {max_gap:.0%})"
            }
        
        # 规则 2: 方向背离 (Gap 方向与计划相反且幅度明显 > 1%)
        # 计划做多，但低开 -1.5%
        if direction == "LONG" and gap_pct < -0.015:
            return {
                "symbol": symbol,
                "status": "CANCELLED",
                "gap_pct": gap_pct,
                "reason": f"Direction Divergence (Plan LONG but Gap {gap_pct:.2%})"
            }
        # 计划做空，但高开 +1.5%
        if direction == "SHORT" and gap_pct > 0.015:
            return {
                "symbol": symbol,
                "status": "CANCELLED",
                "gap_pct": gap_pct,
                "reason": f"Direction Divergence (Plan SHORT but Gap {gap_pct:.2%})"
            }

        # 规则 3: 价格已进入止盈/止损区 (虽然少见)
        # 暂时跳过复杂逻辑

        # ── 新增规则 4 (2026-06-12): 经济事件 30 分钟内 → CANCEL ──
        try:
            plan = self.plan_manager._plan or {}
            no_trade_windows = plan.get("no_trade_windows", [])
            if no_trade_windows:
                from datetime import datetime as _oc_dt
                import pytz as _oc_pz
                now_et = _oc_dt.now(_oc_pz.timezone('US/Eastern'))
                for window in no_trade_windows:
                    if window.get("impact") != "HIGH":
                        continue
                    w_start = window.get("start", "")
                    if not w_start:
                        continue
                    parts = w_start.split(":")
                    if len(parts) == 2:
                        event_dt = now_et.replace(
                            hour=int(parts[0]), minute=int(parts[1]),
                            second=0, microsecond=0
                        )
                        delta_min = abs((now_et - event_dt).total_seconds()) / 60
                        if delta_min < 30:
                            reason_text = window.get("reason", "Economic event")
                            logger.warning(
                                f"⚠️ [{symbol}] 经济事件 {delta_min:.0f}min 内: {reason_text}"
                            )
                            return {
                                "symbol": symbol,
                                "status": "CANCELLED",
                                "gap_pct": gap_pct,
                                "reason": f"Economic event within {delta_min:.0f}min: {reason_text}",
                                "current_price": current_price,
                            }
        except Exception as econ_err:
            logger.debug(f"[OpenChecker] 经济事件检查异常 (不阻断): {econ_err}")

        # ── 新增规则 5 (2026-06-12): 技术 RSI 极端 → CANCEL ──
        try:
            # 从 plan target 中读取 technical_summary (由 PlanValidator 注入)
            plan = self.plan_manager._plan or {}
            for t in plan.get("targets", []):
                if t.get("symbol") == symbol:
                    tech_summary = t.get("technical_summary", "")
                    if tech_summary and "RSI=" in tech_summary:
                        import re
                        rsi_match = re.search(r'RSI=(\d+\.?\d*)', tech_summary)
                        if rsi_match:
                            rsi_val = float(rsi_match.group(1))
                            if direction == "LONG" and rsi_val > 75:
                                logger.warning(
                                    f"⚠️ [{symbol}] RSI={rsi_val:.1f} 超买, LONG cancelled"
                                )
                                return {
                                    "symbol": symbol,
                                    "status": "CANCELLED",
                                    "gap_pct": gap_pct,
                                    "reason": f"RSI={rsi_val:.1f} overbought, LONG cancelled",
                                    "current_price": current_price,
                                }
                            if direction == "SHORT" and rsi_val < 25:
                                logger.warning(
                                    f"⚠️ [{symbol}] RSI={rsi_val:.1f} 超卖, SHORT cancelled"
                                )
                                return {
                                    "symbol": symbol,
                                    "status": "CANCELLED",
                                    "gap_pct": gap_pct,
                                    "reason": f"RSI={rsi_val:.1f} oversold, SHORT cancelled",
                                    "current_price": current_price,
                                }
                    break
        except Exception as tech_err:
            logger.debug(f"[OpenChecker] 技术检查异常 (不阻断): {tech_err}")

        # 通过检查
        return {
            "symbol": symbol,
            "status": "VALID",
            "gap_pct": gap_pct,
            "reason": "Gap and direction acceptable",
            "current_price": current_price
        }

    def _save_plan(self, plan):
        """保存计划"""
        try:
            with open(self.plan_path, 'w', encoding='utf-8') as f:
                json.dump(plan, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存计划失败: {e}")

    def _send_summary(self, results):
        """发送确认摘要"""
        valid_count = sum(1 for r in results if r['status'] == 'VALID')
        cancelled_count = sum(1 for r in results if r['status'] == 'CANCELLED')
        
        md = f"### 🇺🇸 美股开盘确认报告\n\n"
        md += f"**有效目标**: {valid_count}\n"
        md += f"**取消目标**: {cancelled_count}\n\n"
        
        for r in results:
            icon = "✅" if r['status'] == 'VALID' else "❌"
            md += f"- {icon} **{r['symbol']}**: {r['status']} (Gap {r['gap_pct']:.1%})"
            if r.get('reason'):
                md += f" _({r['reason']})_"
            md += "\n"
            
        self.notifier.send_text(md)

if __name__ == "__main__":
    # 测试
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    checker = USOpenChecker()
    asyncio.run(checker.run_check())
