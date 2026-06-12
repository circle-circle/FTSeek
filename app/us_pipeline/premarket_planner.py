"""
美股盘前作战计划生成器 (US Pre-Market Planner)

职责:
1. 收集盘前关键信息 (期货、VIX、ADR、新闻、行业强弱)。
2. 调用 LLM (Qwen-Max) 进行深度宏观与个股推理。
3. 生成 `daily_battle_plan_us.json`。
4. 发送盘前作战计划通知 (钉钉/飞书)。

特点:
- 独立于港股管线。
- 专为美股盘前价格发现设计。
- 输出结构化 JSON 供后续 "确认层" 和 "执行层" 使用。
- P0 修复 (2026-04-19): 接入真实数据源 (MarketDataRouter + USNewsProvider + RSSHub)。
"""

import os
import sys
import json
import logging
import asyncio
import time
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger("ftseek.us_pipeline.premarket_planner")


# ============================================================================
#  盘前分析师 Prompt
# ============================================================================

US_SYSTEM_PROMPT = """
你是 FTSeek 美股量化系统的**首席宏观策略师 (Chief US Macro Strategist)**。
当前时间：{current_time}

你的任务是分析美股市场盘前状态，并生成当日的**日内作战计划 (Daily Battle Plan)**。

## ⚠️ 核心交易约束 (必须严格遵守)

### 1. 敞口约束
- 当前总敞口：**{exposure_total_pct:.1f}%** (LONG {exposure_long_pct:.1f}% / SHORT {exposure_short_pct:.1f}%)
- 当前持仓数：**{current_positions}** / {max_positions} (上限)
- **规则**: 
  - 如果总敞口 > 120%，禁止任何新开仓，必须先减仓
  - 如果持仓数已达上限，禁止开新仓，必须先平仓
  - 每笔新仓位不得超过总资金的 3%
  - 新仓建议数量 = max_positions - current_positions = **{available_slots}** 只

### 2. 实时价格校准
- 部分标的的实时价格已在下方提供
- **入场区 (entry_zone) 必须基于当前实时价格计算**，不得使用过期数据
- 如果当前价已远高于计划入场区，放弃该标的或重新计算合理入场区
- 财报后标的 (如 NVDA) 可能出现大幅跳空，需根据最新价格调整策略

### 3. 财报退出纪律
- 所有财报前持仓必须在财报发布前一天收盘前退出
- 下方已列出需要退出的标的及截止时间
- 必须在 position_actions 中明确标注退出优先级 (P0/P1)

### 4. 数据质量感知
- 如果某些数据源 (如 VIX 盘前、Futures) 不可用，明确标注并在分析中说明
- 不要编造数据，宁可标注 "N/A" 也不虚构

你需要综合分析以下维度：
1. **宏观与情绪**: VIX 走势、主要期货 (SPY/QQQ) 表现、美元/美债收益率、CPI/非农等数据影响。
2. **行业轮动**: 哪些板块 (Tech, Energy, Financials) 强势？哪些弱势？
3. **个股催化**: 财报 (Earnings)、FDA 批准、分析师评级变更、重大新闻。
4. **盘前量价**: ADR 反馈、Gap 幅度、盘前成交量。

## 财报日特别策略 (Earnings Day Protocol)

当 Watchlist 中有股票即将发布财报时，你需要特别关注：

**财报前策略 (Pre-Earnings)**:
- 财报前 1-3 天可能出现"run-up"行情 (资金博弈预期)
- 如果技术面显示突破信号，可考虑小仓位潜伏 (Entry Zone 更保守)
- **必须在财报日前一天收盘前制定退出计划**，不可持仓赌财报

**财报后策略 (Post-Earnings)**:
- 超预期 + 指引积极 → 次日可能出现动量延续 (Post-Earnings Drift)
- 超预期但指引差 → "Beat and Drop" 陷阱，可能先跌后反弹
- 低于预期 → 可能连续下跌 2-3 天，考虑做空机会
- 财报后首日波动率通常增加 50-100%，止损宽度需要相应扩大

**财报黑名单**:
- 财报发布当天禁止开盘追单 (IV Crush + 滑点风险极高)
- 财报日将该股票加入 restricted 列表，除非有明确的技术突破信号

输出要求：
- **Market Regime**: 判断当前是 Risk-On (做多为主), Risk-Off (做空/防守), 还是 Neutral (震荡)。
- **Targets**: 筛选出 3-5 个今日重点交易标的。
  - 必须给出 **Direction** (LONG / SHORT)。
  - 必须给出 **Entry Logic** (逻辑链，如 "Gap up on earnings + VWAP hold")。
  - 必须给出 **Stop Loss / Take Profit** 的参考价位。
  - 如果标的有财报催化，在 `catalyst` 中明确标注 "Earnings Play"。
  - **entry_zone 必须基于实时价格校准数据计算**，不要使用过期的计划价格
- **Restricted**: 列出今日**禁止交易**的标的 (如财报日、SEC 调查等)。
- **Position Actions**: 明确列出需要执行的持仓操作 (退出/减仓/持有)。

请**严格输出 JSON**，不要包含任何解释性文字。

输出 JSON 结构示例：
{{
  "date": "YYYY-MM-DD",
  "market_regime": "Risk-On / Risk-Off / Neutral",
  "sentiment": "Bullish / Bearish",
  "key_drivers": ["Fed pause", "AI earnings beat"],
  "targets": [
    {{
      "symbol": "US.NVDA",
      "direction": "LONG",
      "status": "ACTIVE",
      "logic": "AI demand strong, QQQ leading, gap fill expected",
      "entry_zone": [178.00, 180.00],
      "stop_loss": 175.00,
      "take_profit": 190.00,
      "urgency": "HIGH",
      "catalyst": "Earnings beat + Sector rotation",
      "factors_used": ["news_sentiment", "regime_fit", "trend"]
    }}
  ],
  "restricted": ["US.TSLA"],
  "position_actions": [
    {{
      "symbol": "US.LLY",
      "action": "EXIT",
      "reason": "财报前必须退出",
      "deadline": "2026-04-29",
      "priority": "P0"
    }}
  ],
  "notes": "Watch CPI data at 8:30 AM ET.",
  "strategy_adjustments": ["敞口 155% 超限，建议先减仓 20% 再开新仓"],
  "risk_warnings": ["科技财报周波动放大", "数据源降级影响信号质量"]
}}

**重要提示**:
- `entry_zone` 必须是一个**数值数组 [下限，上限]** (如 `[178.0, 180.0]`)，不要使用字符串描述。这是盘中技术面自动执行的触发区间。
- `status` 初始状态必须是 `"ACTIVE"`。
- `catalyst` 是触发交易的核心原因，用于通知和记录。如果有财报相关，必须标注 "Earnings" 关键词。
- `position_actions` 必须包含所有需要退出的财报前持仓。
- `strategy_adjustments` 和 `risk_warnings` 是基于敞口和数据质量的具体建议，不要泛泛而谈。
- **`factors_used` (T9b 2026-05-16)** 必须显式列出本目标依赖的因子标签 (用于复盘归因, 不影响下单评分):
  - 可选值: `trend` / `momentum` / `volume` / `orderflow` / `news_sentiment` / `regime_fit` / `risk_reward` /
            `market_align` / `history_performance` / `earnings_catalyst` / `breakout` / `mean_reversion`
  - 至少 1 个, 最多 5 个. 例: `["news_sentiment", "earnings_catalyst", "regime_fit"]`
  - 后续 PostTrade 用此字段对比"宣称依赖的因子 vs 实际命中的因子"做归因分析.
"""

class USPreMarketPlanner:
    """美股盘前计划生成器

    P0 修复 (2026-04-19): 接入真实数据源，替代 Mock 数据。
    复用 scheduler 已有的 MarketDataRouter, USNewsProvider, RSSHubUSNewsFetcher。
    """

    def __init__(self, scheduler=None):
        self.scheduler = scheduler
        # P0 修复 (2026-04-22): 修正输出路径 - 使用项目根目录/data而非app/data
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # P1 修复: 路径对齐到项目根目录 data/ (与审计脚本一致)
        # project_root 是 app/ 目录，所以父目录才是项目根目录 FTSeek-US/
        self.output_path = os.path.join(os.path.dirname(project_root), 'data', 'daily_battle_plan_us.json')
        self.notifier = None
        if scheduler and hasattr(scheduler, 'notifier'):
            self.notifier = scheduler.notifier
        if not self.notifier:
            try:
                from app.notifier.dingtalk import DingTalkNotifier
                self.notifier = DingTalkNotifier()
            except Exception:
                pass

    async def run(self):
        """执行盘前分析流程"""
        logger.info("🚀 开始美股盘前分析 (US Pre-Market Planner)...")

        try:
            # P1 优化：从 scheduler 获取注入的优化数据
            exposure_context = getattr(self.scheduler, '_exposure_context', {}) if self.scheduler else {}
            price_calibration = getattr(self.scheduler, '_price_calibration', {}) if self.scheduler else {}
            earnings_exit_plan = getattr(self.scheduler, '_earnings_exit_plan', []) if self.scheduler else {}

            # O6 (2026-05-16): _price_calibration 此前从未被任何模块写入 → LLM 无实时价格锚点
            # → entry_zone 从训练数据幻觉 (5/15 NVDA entry=[132.5,135.0], 实际价 $200+)
            # → 5/15 5 个 plan_target 全部 missed (0% 执行)
            # 修复: 这里主动 fetch 当前价 (sandbox universe 或 watchlist 前 10 只)
            if not price_calibration:
                try:
                    _router = getattr(self.scheduler, 'data_router', None) if self.scheduler else None
                    if _router is None:
                        from app.data.market_data_router import MarketDataRouter
                        _router = MarketDataRouter(
                            twelvedata_api_key=os.environ.get("TWELVEDATA_API_KEY"),
                            finnhub_api_key=os.environ.get("FINNHUB_API_KEY"),
                            alphavantage_api_key=os.environ.get("ALPHAVANTAGE_API_KEY"),
                            futu_opend_host=os.environ.get("OPEND_HOST", "127.0.0.1"),
                            futu_opend_port=int(os.environ.get("OPEND_PORT", "11111")),
                            enable_futu_lv3=True,
                        )
                    # 优先 sandbox universe, 否则 watchlist
                    _cal_symbols = []
                    try:
                        from app.core.config_container import get_config as _gc
                        _cfg = _gc()
                        if _cfg.get_dotted("mode.sandbox.enabled", False):
                            _univ = _cfg.get_dotted("mode.sandbox.universe", []) or []
                            _cal_symbols = [s if s.startswith("US.") else f"US.{s}" for s in _univ][:15]
                    except Exception:
                        pass
                    if not _cal_symbols and self.scheduler:
                        _wl = getattr(self.scheduler, 'watchlist', []) or []
                        _cal_symbols = [s if isinstance(s, str) else s.get('code', '') for s in _wl][:15]
                    for _sym in _cal_symbols:
                        if not _sym:
                            continue
                        try:
                            _q = _router.get_quote(_sym)
                            if _q and float(_q.get("price", 0) or 0) > 0:
                                price_calibration[_sym] = {
                                    "current_price": float(_q.get("price", 0)),
                                    "bid": float(_q.get("bid", 0) or _q.get("price", 0)),
                                    "ask": float(_q.get("ask", 0) or _q.get("price", 0)),
                                }
                        except Exception:
                            continue
                    if price_calibration:
                        logger.info(
                            f"[盘前计划·O6] 自动校准价格: {len(price_calibration)} 只标的 "
                            f"(避免 LLM 从训练数据幻觉 entry_zone)"
                        )
                except Exception as _pce:
                    logger.warning(f"[盘前计划·O6] 价格校准 fetch 失败 (LLM 仍可能幻觉): {_pce}")
            
            # 1. 获取盘前上下文 (真实数据)
            context = self._fetch_premarket_context()
            
            # 2. 获取相关新闻 (RSSHub + USNewsProvider)
            news_summary = await self._fetch_news_summary()
            
            # P1 优化：新闻不足时尝试 Finnhub 降级
            if not news_summary or len(news_summary) < 5:
                try:
                    from app.pipeline.llm_pipeline import _fallback_to_finnhub_news
                    fallback_news = _fallback_to_finnhub_news(limit=10)
                    if fallback_news:
                        news_summary = news_summary + "\n[Finnhub 降级新闻]\n" + "\n".join(
                            [f"- {n.get('title', '')[:80]}" for n in fallback_news[:5]]
                        )
                        logger.info(f"[盘前计划] Finnhub 降级补充 {len(fallback_news)} 条新闻")
                except Exception as e:
                    logger.debug(f"[盘前计划] Finnhub 降级失败：{e}")
            
            # 3. 获取 watchlist 持仓信息
            watchlist = self._get_watchlist_info()
            
            # P0 新增 (2026-04-23): 获取财报日历数据
            earnings_info = self._get_earnings_info(watchlist)

            # P1 优化：构建价格校准文本
            price_cal_text = ""
            if price_calibration:
                price_cal_text = "\n【实时价格校准】(基于此数据计算 entry_zone)\n"
                for sym, data in price_calibration.items():
                    price_cal_text += f"- {sym}: 当前价=${data['current_price']:.2f}, 买=${data.get('bid',0):.2f}, 卖=${data.get('ask',0):.2f}\n"
            
            # P1 优化：构建财报退出计划文本
            earnings_exit_text = ""
            if earnings_exit_plan:
                earnings_exit_text = "\n【财报退出计划】(必须在 position_actions 中列出)\n"
                for item in earnings_exit_plan:
                    earnings_exit_text += f"- {item['symbol']}: {item['action']} 截止 {item['deadline']} (优先级:{item['priority']}) - {item['reason']}\n"

            # P1 优化：构建敞口状态文本
            exposure_total = exposure_context.get('total_exposure_pct', 0)
            exposure_long = exposure_context.get('long_pct', 0)
            exposure_short = exposure_context.get('short_pct', 0)
            current_positions = exposure_context.get('current_positions_count', 0)
            max_positions = exposure_context.get('max_positions', 5)
            available_slots = max(0, max_positions - current_positions)

            # P0-fix 2026-05-02: 教训注入 — 从 lessons DB 加载活跃教训注入到 LLM prompt
            lesson_context = ""
            try:
                from app.review.lesson_injector import LessonInjector
                li = getattr(self.scheduler, 'lesson_injector', None)
                if li is None:
                    li = LessonInjector()
                lesson_context = li.generate_battle_plan_injection()
                if lesson_context and "无活跃教训" not in lesson_context:
                    logger.info(f"[盘前计划] 教训注入: {lesson_context[:100]}...")
            except Exception as le:
                logger.debug(f"[盘前计划] 教训注入失败 (不影响主流程): {le}")
                lesson_context = "【历史教训】教训加载失败，正常执行"

            # ── 3.5 新增 (2026-06-12): 盘前扫描仪 — 技术指标 + 板块轮动 ──
            scan_result = {"technicals": {}, "sectors": {}, "sector_symbol_map": {}}
            scanner = None
            try:
                from app.us_pipeline.premarket_scanner import PreMarketScanner
                watchlist_symbols = [w["symbol"] for w in watchlist] if watchlist else []
                scanner = PreMarketScanner(scheduler=self.scheduler)
                scan_result = await scanner.run_full_scan(watchlist_symbols)
                logger.info(
                    f"[盘前计划] 扫描完成: {len(scan_result.get('technicals', {}))} 只技术指标, "
                    f"{len(scan_result.get('sectors', {}).get('ranked', []))} 个板块"
                )
            except Exception as scan_err:
                logger.warning(f"[盘前计划] 盘前扫描失败 (不影响主流程): {scan_err}")

            # ── 3.6 新增 (2026-06-12): 经济日历 — 宏观事件 + 禁止交易窗口 ──
            econ_calendar_text = "📅 今日无重大经济数据发布。"
            no_trade_windows = []
            try:
                from app.data.economic_calendar import EconomicCalendar
                econ_cal = EconomicCalendar()
                todays_events = econ_cal.get_todays_events()
                no_trade_windows = econ_cal.get_no_trade_windows()
                if todays_events:
                    econ_calendar_text = econ_cal.format_for_llm()
                    logger.info(f"[盘前计划] 今日经济事件: {len(todays_events)} 个")
                else:
                    econ_calendar_text = econ_cal.format_for_llm()  # 仍生成未来 7 天预览
            except Exception as econ_err:
                logger.warning(f"[盘前计划] 经济日历失败 (不影响主流程): {econ_err}")
                todays_events = []

            # 4. 构建 Prompt (注入敞口约束参数)
            prompt = US_SYSTEM_PROMPT.format(
                current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                exposure_total_pct=exposure_total,
                exposure_long_pct=exposure_long,
                exposure_short_pct=exposure_short,
                current_positions=current_positions,
                max_positions=max_positions,
                available_slots=available_slots,
            )

            # ── 新增 (2026-06-12): 格式化扫描数据 ──
            tech_text = ""
            sector_text = ""
            constraints_text = ""
            if scanner:
                try:
                    tech_text = scanner.format_technicals_for_llm(
                        scan_result.get("technicals", {})
                    )
                    sector_text = scanner.format_sectors_for_llm(
                        scan_result.get("sectors", {})
                    )
                    constraints_text = scanner.format_hard_constraints(
                        scan_result.get("technicals", {}),
                        no_trade_windows,
                    )
                except Exception as fmt_err:
                    logger.warning(f"[盘前计划] 扫描数据格式化失败: {fmt_err}")

            user_input = f"""
【市场数据】
SPY Futures: {context.get('spy_futures', 'N/A')}
QQQ Futures: {context.get('qqq_futures', 'N/A')}
VIX: {context.get('vix', 'N/A')}
10Y Yield: {context.get('yield_10y', 'N/A')}
DXY: {context.get('dxy', 'N/A')}
SPY vs MA50: {context.get('spy_vs_ma50', 'N/A')}
大盘趋势：{context.get('market_trend', 'N/A')}

{tech_text}

{sector_text}

{econ_calendar_text}

{constraints_text}

【持仓盘前异动】
{json.dumps(context.get('pre_market_changes', {}), ensure_ascii=False, indent=2)}

【观察池信息】
{json.dumps(watchlist, ensure_ascii=False, indent=2)}

【盘前新闻摘要 (最近 4 小时)】
{news_summary}
{price_cal_text}
{earnings_exit_text}

【本周财报日历】
{earnings_info}

【当前敞口状态】
LONG: {exposure_long:.1f}% | SHORT: {exposure_short:.1f}% | 总敞口：{exposure_total:.1f}%
当前持仓：{current_positions} / {max_positions} | 可开新仓：{available_slots} 只

{lesson_context}

请根据上述信息生成 JSON 作战计划。务必遵守【硬约束规则】中的止损和风险收益比要求。
"""

            # 5. 调用 LLM - v3.0升级为增强版辩论
            logger.info("🧠 调用 LLM 进行盘前推理...")
            
            # P0 升级 (2026-05-03): v3.0增强版辩论模式 (迭代+记忆+分歧注册)
            # B2 修复 (2026-05-13): 修正 import 路径
            
            # 检查是否启用辩论模式
            debate_enabled = os.environ.get("FTSEEK_DEBATE_MODE", "false").lower() == "true"
            
            # 注意: enhanced_debate 已移至 .trash/ (llm.md B7)
            # 如需启用辩论模式，请先从 .trash/ 恢复并修复 import 路径
            if debate_enabled:
                logger.warning("[辩论模式v3.0] 增强版辩论已禁用 (enhanced_debate 为死代码)")
                debate_enabled = False
                
            if debate_enabled:
                logger.info("[辩论模式v3.0] 启动增强版辩论 (迭代+记忆+分歧注册)")
                
                try:
                    # 构建lessons数据
                    lessons = {"lessons": []}
                    if lesson_context and "无活跃教训" not in lesson_context:
                        lessons["lessons"] = [{"text": lesson_context}]
                    
                    # 初始化增强版辩论执行器
                    debate = get_enhanced_executor(
                        agent_bridge=self._get_agent_bridge(),
                        config={
                            "timeout": int(os.environ.get("FTSEEK_DEBATE_TIMEOUT", "600")),
                            "fallback_model": os.environ.get("FTSEEK_DEBATE_FALLBACK_MODEL", "qwen-max"),
                            "max_rounds": int(os.environ.get("FTSEEK_DEBATE_MAX_ROUNDS", "3")),
                            "convergence_threshold": float(os.environ.get("FTSEEK_DEBATE_CONVERGENCE", "0.4"))
                        }
                    )
                    
                    # 运行增强版辩论
                    debate_result = await debate.run_enhanced_debate(
                        prompt=user_input,
                        system_prompt=US_SYSTEM_PROMPT,
                        lessons=lessons,
                        topic="premarket_planning"
                    )
                    
                    # 使用收敛结果
                    response = self._format_debate_result(debate_result)
                    logger.info(
                        f"[辩论模式v3.0] 收敛度: {debate_result.get('debate_stats', {}).get('convergence_score', 0):.2%} | "
                        f"轮次: {debate_result.get('iteration_summary', {}).get('total_rounds', 1)} | "
                        f"分歧: {len(debate_result.get('divergences', []))}"
                    )
                    
                except Exception as debate_err:
                    logger.warning(f"[辩论模式v3.0] 失败，降级为单模型: {debate_err}")
                    response = await self._call_llm(prompt, user_input)
            else:
                # 原有单模型模式
                response = await self._call_llm(prompt, user_input)

            if not response:
                raise Exception("LLM 调用失败")

            # 6. 解析并校验
            plan = self._parse_response(response)

            # ── 6.5 新增 (2026-06-12): 硬校验 — PlanValidator ──
            try:
                from app.us_pipeline.plan_validator import PlanValidator
                validator = PlanValidator(
                    technicals=scan_result.get("technicals", {}),
                    sectors=scan_result.get("sectors", {}),
                    sector_symbol_map=scan_result.get("sector_symbol_map", {}),
                    economic_events=todays_events,
                    price_calibration=price_calibration,
                )
                plan = validator.validate_and_annotate(plan)
                logger.info(f"[盘前计划] 校验完成: {validator.__class__.__name__}")
            except Exception as val_err:
                logger.warning(f"[盘前计划] 计划校验失败 (不影响保存): {val_err}")

            # 注入禁止交易窗口到 plan
            if no_trade_windows:
                plan["no_trade_windows"] = no_trade_windows

            self._save_plan(plan)

            # P2 新增: 根据盘前分析结果更新龙头池市场阶段
            try:
                from app.utils.dragonhead_pool import get_dragonhead_pool, MarketRegime
                dragon_pool = get_dragonhead_pool()
                regime_map = {
                    "Risk-On": MarketRegime.RISK_ON,
                    "Risk-Off": MarketRegime.RISK_OFF,
                    "Neutral": MarketRegime.ROTATION,
                }
                regime_str = plan.get("market_regime", "Risk-On")
                regime = regime_map.get(regime_str, MarketRegime.RISK_ON)
                dragon_pool.set_regime(regime, force=True)

                # 注入强度评分 (从计划 targets 中提取 urgency 作为代理)
                scores = {}
                urgency_score = {"HIGH": 90.0, "MEDIUM": 70.0, "NORMAL": 50.0}
                for target in plan.get("targets", []):
                    code = target.get("symbol", "")
                    urgency = target.get("urgency", "NORMAL")
                    if code in [s.code for s in dragon_pool.get_active_pool("US")]:
                        scores[code] = urgency_score.get(urgency, 50.0)
                if scores:
                    dragon_pool.update_strength_scores(scores)

                logger.info(
                    f"[盘前计划] 龙头池已更新: 阶段={regime.value}, "
                    f"强度评分={len(scores)}只"
                )
            except Exception as e:
                logger.warning(f"[盘前计划] 龙头池更新失败: {e}")

            # 7. 发送通知
            self._send_notification(plan)

            logger.info(f"✅ 盘前计划已生成并推送: {len(plan.get('targets', []))} 个目标")
            return plan

        except Exception as e:
            logger.error(f"❌ 盘前计划生成失败: {e}", exc_info=True)
            # Fallback: 发送告警
            if self.notifier:
                self.notifier.send_text(f"⚠️ FTSeek-US 盘前计划生成失败: {e}")
            return None

    # ── 真实数据获取 ──────────────────────────────────────────

    def _fetch_premarket_context(self) -> Dict:
        """获取盘前市场数据 (真实数据源)"""
        context = {
            "spy_futures": "N/A",
            "qqq_futures": "N/A",
            "vix": "N/A",
            "yield_10y": "N/A",
            "dxy": "N/A",
            "spy_vs_ma50": "N/A",
            "market_trend": "N/A",
            "pre_market_changes": {},
        }

        # 优先使用 scheduler 已有的数据源 (避免重复初始化)
        if self.scheduler and hasattr(self.scheduler, 'yahoo'):
            try:
                vix = self.scheduler.market_data.get_vix()
                context["vix"] = f"{vix:.1f}"
            except Exception as e:
                logger.warning(f"获取 VIX 失败: {e}")

            try:
                spy_ctx = self.scheduler.market_data.get_spy_context()
                spx_change = spy_ctx.get("spx_change_percent", 0)
                context["spy_futures"] = f"{spx_change:+.2f}%"
                context["spy_vs_ma50"] = spy_ctx.get("spy_vs_ma50", "N/A")
                context["market_trend"] = "up" if spx_change > 0.01 else ("down" if spx_change < -0.01 else "flat")
            except Exception as e:
                logger.warning(f"获取 SPY 上下文失败: {e}")

            # 2026-04-24 关闭: 盘前获取 watchlist 异动已禁用
            # 原因: Futu LV3 盘前无美股数据，逐个 get_quote 产生大量 "所有数据源均失败" 错误
            # watchlist = self.scheduler.watchlist if hasattr(self.scheduler, 'watchlist') else []
            # for sym in watchlist[:10]:
            #     try:
            #         quote = self.scheduler.market_data.get_quote(sym)
            #         if quote and 'change_percent' in quote:
            #             cp = quote['change_percent']
            #             context["pre_market_changes"][sym] = round(cp, 2)
            #     except Exception:
            #         pass
            context["pre_market_note"] = "盘前行情不可用 (LV3 无盘前数据)"
        else:
            # 2026-04-24 关闭: 独立运行时 MarketDataRouter 轮询已禁用
            # 原因: 盘前无实时行情，创建 Router 实例并轮询 API 无意义且浪费配额
            # try:
            #     from app.data.market_data_router import MarketDataRouter
            #     import os
            #     router = MarketDataRouter(...)
            #     vix = router.get_vix()
            #     spy_quote = router.get_quote("US.SPY")
            # except Exception as e:
            #     logger.warning(f"MarketDataRouter 初始化失败: {e}")
            context["vix"] = "N/A (盘前不可用)"
            context["spy_futures"] = "N/A"
            context["market_trend"] = "unknown"

        return context

    async def _fetch_news_summary(self) -> str:
        """获取新闻摘要 (RSSHub + USNewsProvider)"""
        news_items = []

        # 1. 尝试 RSSHub (主要来源)
        try:
            from app.data.rsshub_us_news import RSSHubUSNewsFetcher
            rsshub = RSSHubUSNewsFetcher()
            rsshub.fetch_all()
            rss_news = rsshub.get_recent_news(hours=4, limit=15)
            for item in rss_news:
                news_items.append(
                    f"- [{item.get('source_name', 'RSSHub')}] {item.get('title', '')}: "
                    f"{item.get('summary', '')[:120]}"
                )
            logger.info(f"[盘前计划] RSSHub 获取 {len(rss_news)} 条新闻")
        except Exception as e:
            logger.warning(f"[盘前计划] RSSHub 新闻获取失败: {e}")

        # 2. 尝试 USNewsProvider (备用来源)
        if len(news_items) < 5:
            try:
                from app.data.news_provider import USNewsProvider
                us_news = USNewsProvider()
                # 获取市场级别新闻
                for category in ["market", "tech"]:
                    headlines = us_news.fetch_recent_news("US.SPY", hours=4)
                    for h in headlines[:5]:
                        news_items.append(f"- [USNews] {h}")
                logger.info(f"[盘前计划] USNewsProvider 获取 {len(headlines)} 条新闻")
            except Exception as e:
                logger.warning(f"[盘前计划] USNewsProvider 失败: {e}")

        # 3. 尝试 Finnhub 新闻 (最终降级)
        if len(news_items) < 3:
            try:
                from app.pipeline.llm_pipeline import _fallback_to_finnhub_news
                finnhub_news = _fallback_to_finnhub_news(limit=10)
                for item in finnhub_news:
                    news_items.append(
                        f"- [Finnhub] {item.get('headline', '')}: "
                        f"{item.get('summary', '')[:120]}"
                    )
            except Exception as e:
                logger.warning(f"[盘前计划] Finnhub 降级失败: {e}")

        if not news_items:
            return "⚠️ 所有新闻源均不可用，请基于市场数据进行分析。"

        return "\n".join(news_items[:15])

    def _get_watchlist_info(self) -> List[Dict]:
        """获取观察池基本信息"""
        if not self.scheduler or not hasattr(self.scheduler, 'watchlist'):
            return []

        info = []
        for sym in self.scheduler.watchlist:
            entry = {"symbol": sym}
            # 检查是否有持仓
            if self.scheduler.position_store.has(sym):
                pos = self.scheduler.position_store.get(sym)
                entry["has_position"] = True
                entry["direction"] = pos.direction.value if hasattr(pos.direction, 'value') else str(pos.direction)
                entry["size"] = pos.size
                entry["pnl_pct"] = getattr(pos, 'pnl_pct', 0)
            else:
                entry["has_position"] = False
            info.append(entry)
        return info

    # ── 财报日历 (P0 新增 2026-04-23) ─────────────────────────────

    def _get_earnings_info(self, watchlist: List[Dict]) -> str:
        """
        获取财报日历信息并格式化为 LLM 可读文本
        
        P0 新增 (2026-04-23): 财报日历注入到 LLM prompt，
        使盘前计划能够识别财报日的特殊交易机会和风险。
        
        Returns:
            格式化的财报日历文本
        """
        try:
            from app.data.earnings_calendar import EarningsCalendar
            cal = EarningsCalendar()
            
            # 提取 watchlist 中的 symbol 列表
            watchlist_symbols = [w["symbol"] for w in watchlist] if watchlist else None
            
            # 获取未来 7 天的财报日历
            return cal.format_for_llm(watchlist_symbols=watchlist_symbols, days_ahead=7)
        except Exception as e:
            logger.warning(f"[财报日历] 获取失败: {e}")
            return "⚠️ 财报日历数据暂不可用。"

    async def _call_llm(self, system_prompt: str, user_input: str) -> Optional[str]:
        """调用 LLM (异步) — 2026-05-17 迁移到 model_call 统一入口.

        变化:
          - 旧: AsyncOpenAI + qwen-max 硬编码 + 失败 None
          - 新: model_call.call_llm("premarket_planner") + schema 强校验 + 缓存兜底
          - model 由 yaml `llm.features.premarket_planner` 路由 (default: qwen-plus → deepseek-pro fallback)

        Returns:
            JSON str (供下游 _parse_response 兼容旧解析)
            None: primary + fallback + cache 全失败
        """
        try:
            import asyncio
            from app.llm.model_call import call_llm

            # Citation 强化 — 反幻觉 (要求 LLM 引用具体数值)
            citation_hint = (
                "\n\n🚨 反幻觉: 每个 target 的 logic 字段必须引用至少 1 个具体数值. "
                "❌ 错误示例: 'AI demand strong'  "
                "✅ 正确示例: 'NVDA RSI=68, MA20=$145.5, 量比 1.8x'"
            )
            enhanced_system = system_prompt + citation_hint

            # 走线程池, 避免阻塞 event loop
            res = await asyncio.to_thread(
                call_llm,
                "premarket_planner",
                user_input,
                system=enhanced_system,
                temperature=0.1,
                max_tokens=5000,
                # 不在此处加 schema_required, 因为 plan 字段较多, 用旧 _parse_response 容错抽取
                # 关键字段校验交给 USBattlePlanManager.revalidate_on_restart (已存在)
            )

            if not res or not res.success:
                err = res.error if res else "unknown"
                logger.error(f"[盘前计划·LLM] 失败: {err} (primary+fallback 都试过)")
                # cache_on_fail 已由 model_call 内部处理: 自动加载 24h 内昨日 plan
                if res and res.from_cache:
                    logger.warning(f"[盘前计划·LLM] 使用 24h 内缓存 plan")
                    # 推送告警 (复用 PushHub)
                    try:
                        from app.push import get_push_hub, critical_alert as _crit
                        get_push_hub().push(_crit(
                            "📋 盘前计划降级",
                            f"所有 LLM 失败, 使用昨日缓存 plan. 错误: {err}",
                            dedup_key=f"plan_cache_{int(time.time()/3600)}",
                        ))
                    except Exception:
                        pass
                    return res.response if isinstance(res.response, str) else None
                return None

            # 记录新接口元数据 (供 audit)
            logger.info(
                f"[盘前计划·LLM] ✓ model={res.model_used} latency={int(res.latency_ms)}ms "
                f"cost=${res.cost_usd:.4f} fallback_used={res.fallback_used}"
            )
            return res.response if isinstance(res.response, str) else None
        except Exception as e:
            logger.error(f"[盘前计划·LLM] 异常: {e}", exc_info=True)
            return None

    def _parse_response(self, text: str) -> Dict:
        """解析 LLM 返回的 JSON"""
        try:
            # 尝试提取 JSON (处理可能的 Markdown 代码块)
            if text.startswith("```json"):
                text = text.split("```json")[1].split("```")[0]
            elif text.startswith("```"):
                text = text.split("```")[1].split("```")[0]
            
            plan = json.loads(text)
            
            # 增加元数据
            plan["generated_at"] = datetime.now().isoformat()
            plan["status"] = "PRE_MARKET" # 初始状态
            
            return plan
        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析失败: {e}\nRaw: {text}")
            raise

    def _save_plan(self, plan: Dict):
        """保存计划到文件"""
        os.makedirs(os.path.dirname(self.output_path), exist_ok=True)
        with open(self.output_path, 'w', encoding='utf-8') as f:
            json.dump(plan, f, indent=2, ensure_ascii=False)
        logger.info(f"💾 计划已保存至: {self.output_path}")

    def _send_notification(self, plan: Dict):
        """发送飞书通知 (美股)"""
        try:
            from app.notifier.feishu import FeishuNotifier
            notifier = FeishuNotifier()
        except Exception:
            try:
                from app.notifier.dingtalk import DingTalkNotifier
                notifier = DingTalkNotifier()
            except Exception:
                logger.warning("无法初始化通知器，跳过推送")
                return

        date = plan.get("date", "Unknown")
        regime = plan.get("market_regime", "Unknown")
        targets = plan.get("targets", [])
        restricted = plan.get("restricted", [])
        
        lines = [f"🇺🇸 美股盘前作战计划 ({date})", f"━━━━━━━━━━━━━━━━━━━━", f"🌍 市场定调: {regime}"]
        
        if targets:
            lines.append("")
            lines.append("🎯 核心目标:")
            for t in targets:
                sym = t.get('symbol', '?')
                direction = t.get('direction', '?')
                logic = t.get('logic', '')[:50] + ('...' if len(t.get('logic', '')) > 50 else '')
                urgency = t.get('urgency', 'NORMAL')
                catalyst = t.get('catalyst', '')
                icon = "📈" if direction == "LONG" else "📉"
                earnings_badge = "📊 财报 " if "arning" in catalyst else ""
                lines.append(f"• {icon} {sym} ({direction}) | {earnings_badge}{urgency}")
                lines.append(f"  - {logic}")
        
        if restricted:
            lines.append("")
            lines.append(f"🚫 禁止交易: {', '.join(restricted)}")
            
        lines.append(f"━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        
        content = "\n".join(lines)
        
        try:
            if hasattr(notifier, "send_text"):
                notifier.send_text(content)
            logger.info(f"✅ 盘前计划已推送")
        except Exception as e:
            logger.error(f"盘前计划推送失败: {e}")

    def _get_system_prompt(self) -> str:
        return US_SYSTEM_PROMPT
    
    def _get_agent_bridge(self):
        """获取agent_bridge实例（延迟初始化）"""
        if not hasattr(self, '_agent_bridge'):
            try:
                from app.llm.agent_bridge import FTSeekUSAgentBridge
                self._agent_bridge = FTSeekUSAgentBridge()
            except Exception as e:
                logger.warning(f"AgentBridge初始化失败: {e}")
                self._agent_bridge = None
        return self._agent_bridge
    
    def _format_debate_result(self, debate_result: dict) -> Optional[str]:
        """将辩论结果格式化为LLM响应格式"""
        try:
            converged = debate_result.get("converged_result", {})
            if not converged:
                # 如果converged_result为空，尝试使用任一成功结果
                hermes = debate_result.get("hermes_result")
                claude = debate_result.get("claude_result")
                converged = hermes or claude
            
            if isinstance(converged, dict):
                return json.dumps(converged, ensure_ascii=False)
            elif isinstance(converged, str):
                return converged
            else:
                return str(converged)
        except Exception as e:
            logger.error(f"辩论结果格式化失败: {e}")
            return None

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    planner = USPreMarketPlanner()
    asyncio.run(planner.run())
