#!/usr/bin/env python3
"""
跨市场套利调度器 (Cross Market Scheduler)

功能:
1. 09:00 盘前分析 → 生成评分信号
2. 09:10 钉钉推送计划 → 用户审核
3. 09:25 倒计时5分钟 → 最后确认窗口
4. 09:30 开盘执行 → 自动执行未否决信号
5. 10:00 退出 → 清理 + 趋势策略接手

设计理念:
  观察期采用"信任但验证"模式:
  - 系统默认执行 (保证信号不被遗漏)
  - 人工可否决 (保留风控权)
  - 15分钟审核 + 5分钟倒计时 (给用户充足决策时间)
"""

import logging
import threading
import time as time_module
from datetime import datetime, time, timedelta
from typing import List, Dict, Optional

from app.strategy.cross_market import (
    CrossMarketArbitrage,
    CrossMarketSessionWindow,
    OvernightChange,
)
from app.notifier.dingtalk import DingTalkNotifier
from app.strategy.transition_manager import get_transition_manager
from app.strategy.signal_coordinator import SignalCoordinator

logger = logging.getLogger("ftseek.cross_market_scheduler")


class CrossMarketSignalRecord:
    """
    跨市场信号记录 (带审核状态追踪)
    
    用于:
    - 钉钉推送卡片
    - 倒计时提醒
    - 执行日志
    - 复盘分析
    """
    
    def __init__(
        self,
        signal_id: str,
        code: str,
        action: str,  # 'BUY' / 'SELL'
        metadata: dict,
    ):
        self.signal_id = signal_id
        self.code = code
        self.action = action
        self.metadata = metadata
        
        # 时间线
        now = datetime.now()
        self.created_at = now
        self.pushed_at = now + timedelta(minutes=10)
        self.countdown_at = now + timedelta(minutes=25)
        self.execution_time = now.replace(hour=9, minute=30)
        
        # 审核状态
        self.review_status = 'pending'  # pending/approved/rejected/auto_executed/expired
        self.reviewed_by: Optional[str] = None
        self.review_comment: str = ""
        
        # 执行结果
        self.executed = False
        self.execution_price: Optional[float] = None
        self.execution_timestamp: Optional[float] = None  # 实际执行时间戳
        self.expected_price: Optional[float] = None       # 预期价 (signal.price)
        self.slippage_pct: Optional[float] = None         # 滑点百分比
        self.pnl: Optional[float] = None
        self.exit_reason: Optional[str] = None
    
    def is_auto_executable(self) -> bool:
        """是否可自动执行"""
        return self.review_status in ('pending',)
    
    def should_execute(self) -> bool:
        """是否应该执行 (包括自动和手动确认)"""
        return self.review_status in ('approved', 'auto_executed')
    
    def is_rejected(self) -> bool:
        """是否被否决"""
        return self.review_status == 'rejected'


class CrossMarketScheduler:
    """
    跨市场套利调度器 (默认自动执行模式)
    
    工作流程:
    1. 09:00 盘前分析 → 生成信号
    2. 09:10 钉钉推送计划 → 用户审核
    3. 09:25 倒计时5分钟 + LLM预分析 → 最后确认窗口
    4. 09:30 开盘执行 → 自动执行未否决信号
    5. 10:00 退出 → 清理 + 趋势策略接手
    """
    
    def __init__(
        self,
        strategy: CrossMarketArbitrage,
        notifier: DingTalkNotifier,
        trade_executor=None,  # 可选,用于实际执行
        llm_analyst=None,     # 可选,LLMMarketAnalyst 实例
    ):
        self.strategy = strategy
        self.notifier = notifier
        self.trade_executor = trade_executor
        self._llm_analyst = llm_analyst
        
        # 信号协调器
        self.coordinator = SignalCoordinator()
        
        # 信号记录缓存
        self._signal_records: Dict[str, CrossMarketSignalRecord] = {}
        
        # 过渡管理器
        self.transition_mgr = get_transition_manager()
        
        # 线程
        self._thread: Optional[threading.Thread] = None
        self._running = False
        
        # 每日任务标记 (2026-04-14: 添加线程安全保护)
        self._daily_flags = {}
        self._flags_lock = threading.Lock()
        
        logger.info("跨市场套利调度器已初始化 (默认自动执行模式)")
    
    def start(self):
        """启动调度器"""
        if self._running:
            logger.warning("调度器已在运行")
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("跨市场套利调度器已启动")
    
    def stop(self):
        """停止调度器"""
        self._running = False
        
        if self._thread:
            self._thread.join(timeout=5)
        
        logger.info("跨市场套利调度器已停止")
    
    def _run(self):
        """调度主循环"""
        logger.info("[调度器] 主循环启动")
        
        while self._running:
            try:
                now = datetime.now()
                
                # 仅在港股交易日运行
                if not CrossMarketSessionWindow.is_trading_day(now):
                    time_module.sleep(300)  # 5分钟检查一次
                    continue
                
                current_t = now.time()
                date_key = now.strftime("%Y-%m-%d")
                
                # 09:00 盘前分析
                if current_t >= time(9, 0) and current_t < time(9, 1):
                    if not self._daily_flags.get(f"{date_key}_analysis"):
                        self._job_pre_analysis()
                        self._daily_flags[f"{date_key}_analysis"] = True
                
                # 09:10 推送计划
                elif current_t >= time(9, 10) and current_t < time(9, 11):
                    if not self._daily_flags.get(f"{date_key}_push"):
                        self._job_push_plan()
                        self._daily_flags[f"{date_key}_push"] = True
                
                # 09:25 倒计时
                elif current_t >= time(9, 25) and current_t < time(9, 26):
                    if not self._daily_flags.get(f"{date_key}_countdown"):
                        self._job_start_countdown()
                        self._daily_flags[f"{date_key}_countdown"] = True
                
                # 09:30 执行
                elif current_t >= time(9, 30) and current_t < time(9, 31):
                    if not self._daily_flags.get(f"{date_key}_execution"):
                        self._job_execute()
                        self._daily_flags[f"{date_key}_execution"] = True
                
                # 10:00 清理
                elif current_t >= time(10, 0) and current_t < time(10, 1):
                    if not self._daily_flags.get(f"{date_key}_cleanup"):
                        self._job_cleanup()
                        self._daily_flags[f"{date_key}_cleanup"] = True
                        # 重置每日标记 (保留今日记录)
                        keys_to_remove = [k for k in self._daily_flags if k.startswith(date_key)]
                        for key in keys_to_remove:
                            del self._daily_flags[key]
                
                time_module.sleep(30)  # 30秒检查一次
            
            except Exception as e:
                logger.error(f"[调度器] 主循环异常: {e}", exc_info=True)
                time_module.sleep(60)  # 异常后等待1分钟
    
    def _job_pre_analysis(self):
        """09:00 盘前分析 (扩展版 - 含LLM综合研判)"""
        logger.info("[09:00] ========== 开始盘前分析 ==========")
        
        try:
            # 获取美股隔夜数据
            us_data = self._fetch_us_overnight_data()
            
            if not us_data:
                logger.warning("[09:00] 无美股隔夜数据,跳过分析")
                return
            
            # 分析并评分
            signals = self.strategy.analyze_us_to_hk(us_data)
            
            if not signals:
                logger.info("[09:00] 无符合条件的跨市场机会")
                return
            
            # 创建信号记录
            for signal in signals:
                record = CrossMarketSignalRecord(
                    signal_id=signal.id,
                    code=signal.code,
                    action=signal.action.value,
                    metadata=signal.extra,
                )
                self._signal_records[signal.id] = record
                
                # 记录到过渡管理器
                self.transition_mgr.record_arb_signal()
            
            logger.info(f"[09:00] 盘前分析完成: {len(signals)} 个信号")
            for sig_id, record in self._signal_records.items():
                logger.info(
                    f"  {record.code} {record.action} | "
                    f"评分={record.metadata.get('arb_total_score', 0):.2f}"
                )
            
            # 【新增】LLM综合研判与交易计划生成 (2026-04-12)
            # 修复: 在后台线程中使用新事件循环，避免 "no running event loop" 错误
            if self._llm_analyst:
                import asyncio
                try:
                    # 创建新的事件循环 (适配后台线程)
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        pre_market_plan = loop.run_until_complete(
                            self._generate_pre_market_plan(us_data, signals)
                        )
                        if pre_market_plan:
                            # 保存到文件
                            self._save_pre_market_plan(pre_market_plan)
                            # 推送钉钉
                            self._push_pre_market_plan(pre_market_plan)
                    finally:
                        loop.close()
                except Exception as e:
                    logger.error(f"[09:00] LLM盘前计划生成失败: {e}", exc_info=True)
                    # 降级：继续原有流程，不阻断
            else:
                logger.debug("[09:00] 未配置LLM分析师，跳过盘前计划生成")
        
        except Exception as e:
            logger.error(f"[09:00] 盘前分析异常: {e}", exc_info=True)
    
    def _job_push_plan(self):
        """09:10 钉钉推送计划"""
        logger.info("[09:10] ========== 推送跨市场套利计划 ==========")
        
        pending_records = [
            r for r in self._signal_records.values()
            if r.review_status == 'pending'
        ]
        
        if not pending_records:
            logger.info("[09:10] 无待推送信号")
            return
        
        # 推送每个信号
        for record in pending_records:
            success = self.notifier.send_cross_market_plan(
                metadata_dict=record.metadata,
                signal_id=record.signal_id,
            )
            
            if success:
                logger.info(f"[09:10] 已推送: {record.code} {record.action}")
            else:
                logger.error(f"[09:10] 推送失败: {record.code}")
        
        logger.info(f"[09:10] 推送完成: {len(pending_records)} 个信号")
    
    def _job_start_countdown(self):
        """09:25 开始5分钟倒计时 + LLM预分析"""
        logger.info("[09:25] ========== 开始5分钟倒计时 ==========")
        
        # 触发 LLM 预分析（后台线程，不阻塞倒计时流程）
        if self._llm_analyst and getattr(self._llm_analyst, 'enabled', False):
            llm_thread = threading.Thread(
                target=self._run_pre_market_llm_analysis,
                daemon=True,
                name="llm-pre-analysis",
            )
            llm_thread.start()
            logger.info("[09:25] LLM预分析已在后台线程启动")
        
        pending_records = [
            r for r in self._signal_records.values()
            if r.review_status == 'pending'
        ]
        
        if not pending_records:
            logger.info("[09:25] 无待倒计时信号")
            return
        
        # 推送倒计时卡片
        for record in pending_records:
            success = self.notifier.send_cross_market_countdown(
                metadata_dict=record.metadata,
                signal_id=record.signal_id,
            )
            
            if success:
                logger.info(f"[09:25] 倒计时开始: {record.code} {record.action}")
        
        logger.info(f"[09:25] 倒计时启动: {len(pending_records)} 个信号")
    
    def _get_arb_codes(self) -> List[str]:
        """获取跨市场套利标的港股代码列表"""
        try:
            pairs = self.strategy.stock_pool.get_enabled_correlation_pairs()
            codes = [p.hk_code for p in pairs if p.hk_code]
            logger.debug(f"[LLM预分析] 获取到 {len(codes)} 只套利标的")
            return codes
        except Exception as e:
            logger.warning(f"[LLM预分析] 获取标的列表失败，降级到 watch_list: {e}")
            try:
                from app import config as app_config
                return list(getattr(app_config, 'WATCH_LIST', []))
            except Exception:
                return []
    
    def _build_cross_market_context(self):
        """构建跨市场上下文，复用美股隔夜数据"""
        try:
            from app.ai.llm_market_analyst import CrossMarketContext
            from app.data.us_overnight_fetcher import get_overnight_fetcher
            
            fetcher = get_overnight_fetcher()
            us_data = fetcher.fetch_overnight_data() if fetcher._enabled else []
            
            # 提取 SPY/QQQ 变化幅度
            spy_change = 0.0
            qqq_change = 0.0
            arb_pairs = []
            
            for item in us_data:
                us_code = getattr(item, 'code', '') or getattr(item, 'symbol', '')
                change_pct = getattr(item, 'change_pct', 0.0) or 0.0
                if us_code == 'SPY':
                    spy_change = float(change_pct)
                elif us_code == 'QQQ':
                    qqq_change = float(change_pct)
                else:
                    lag_code = getattr(item, 'lag_code', '') or getattr(item, 'hk_code', '')
                    arb_pairs.append({
                        'us_code': us_code,
                        'hk_code': lag_code,
                        'change_pct': float(change_pct),
                    })
            
            return CrossMarketContext(
                spy_change_pct=spy_change,
                qqq_change_pct=qqq_change,
                vix=0.0,
                vix_status="unknown",
                us_bond_10y=0.0,
                arb_pairs=arb_pairs[:10],
            )
        except Exception as e:
            logger.warning(f"[LLM预分析] 构建跨市场上下文失败: {e}")
            try:
                from app.ai.llm_market_analyst import CrossMarketContext
                return CrossMarketContext(
                    spy_change_pct=0.0,
                    qqq_change_pct=0.0,
                    vix=0.0,
                    vix_status="unknown",
                    us_bond_10y=0.0,
                )
            except Exception:
                return None
    
    def _build_news_context(self, code: str):
        """构建新闻上下文"""
        try:
            from app.ai.llm_market_analyst import NewsContext
            from app.factors.news_integration import get_news_analyzer
            
            analyzer = get_news_analyzer()
            news_data = analyzer.analyze_stock(code)
            
            if not news_data:
                return None
            
            headlines = news_data.get('headlines', [])
            sentiment = float(news_data.get('sentiment_score', 0.0))
            news_count = int(news_data.get('news_count', len(headlines)))
            summary = news_data.get('summary', '') or news_data.get('top_news_summary', '')
            avg_age = float(news_data.get('avg_age_minutes', 60.0))
            has_breaking = bool(news_data.get('has_breaking', False))
            
            return NewsContext(
                headlines=headlines[:5],
                sentiment_score=sentiment,
                news_count=news_count,
                top_news_summary=summary[:200] if summary else '',
                avg_age_minutes=avg_age,
                has_breaking=has_breaking,
            )
        except Exception as e:
            logger.debug(f"[LLM预分析] {code} 新闻上下文构建失败（非致命）: {e}")
            return None
    
    def _run_pre_market_llm_analysis(self):
        """开盘前 LLM 预分析：对跨市场套利标的运行 LLM 研判
        
        2026-04-12 优化: 使用并行调用替代串行, 30只 -> ~3分钟 (原来15分钟)
        """
        if not self._llm_analyst or not getattr(self._llm_analyst, 'enabled', False):
            return
        
        import asyncio
        import time as time_mod
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        logger.info("[LLM预分析] 开始对跨市场标的运行 LLM 研判...")
        start_time = time_mod.time()
        
        # 获取标的列表
        codes = self._get_arb_codes()
        if not codes:
            logger.warning("[LLM预分析] 无标的列表，跳过")
            return
        
        # 限制最多 20 只
        codes = codes[:20]
        
        # 构建跨市场上下文（公共上下文，所有标的共享）
        cross_market_ctx = self._build_cross_market_context()
        
        results = {}
        max_workers = 5  # 并行度5, 避免API限流 (百炼60 RPM)
        
        logger.info(f"[LLM预分析] 使用并行模式: max_workers={max_workers}, stocks={len(codes)}")
        
        def _analyze_single(code):
            """单只股票LLM分析 (在独立线程中运行)"""
            import asyncio as _asyncio
            try:
                news_ctx = self._build_news_context(code)
                
                # 在线程中创建新事件循环
                loop = _asyncio.new_event_loop()
                _asyncio.set_event_loop(loop)
                try:
                    assessment = loop.run_until_complete(
                        self._llm_analyst.analyze(
                            code=code,
                            cross_market_context=cross_market_ctx,
                            news_context=news_ctx,
                            trigger_type="SCHEDULED",
                            urgent=True,
                        )
                    )
                    return code, assessment, None
                finally:
                    loop.close()
            except Exception as e:
                return code, None, str(e)
        
        # 并行执行 (ThreadPoolExecutor)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_analyze_single, code): code for code in codes}
            for future in as_completed(futures, timeout=300):
                code, assessment, error = future.result()
                if error:
                    logger.warning(f"[LLM预分析] {code} 分析失败: {error}")
                    continue
                if assessment and assessment.direction != "NEUTRAL":
                    results[code] = assessment
                    logger.info(
                        f"[LLM预分析] {code}: {assessment.direction} "
                        f"({assessment.confidence_level})"
                    )
        
        elapsed = time_mod.time() - start_time
        logger.info(
            f"[LLM预分析] 完成，{len(results)}/{len(codes)} 个标的有方向性研判，"
            f"耗时 {elapsed:.1f}s"
        )
        
        # 推送汇总到钉钉
        self._push_llm_summary(results, codes, elapsed)
    
    def _push_llm_summary(self, results: dict, all_codes: list, elapsed: float):
        """推送 LLM 预分析汇总到钉钉（增强版 2026-04-12）"""
        if not self.notifier:
            return
        
        try:
            bullish = []
            bearish = []
            
            direction_map = {
                "STRONG_BUY": "🔴强烈看多",
                "BUY": "🔴看多",
                "SELL": "🟢看空",
                "STRONG_SELL": "🟢强烈看空",
            }
            
            for code, assessment in results.items():
                direction = assessment.direction
                confidence = assessment.confidence_level
                entry = f"{code}({confidence})"
                if direction in ("STRONG_BUY", "BUY"):
                    bullish.append(entry)
                elif direction in ("STRONG_SELL", "SELL"):
                    bearish.append(entry)
            
            lines = [
                "### 🤖 LLM 盘前研判汇总",
                "",
                f"**分析标的**: {len(all_codes)} 只 | "
                f"**有效研判**: {len(results)} 只 | "
                f"**耗时**: {elapsed:.0f}s",
                "",
            ]
            
            if bullish:
                lines.append(f"**看多({len(bullish)})**: {', '.join(bullish)}")
            if bearish:
                lines.append(f"**看空({len(bearish)})**: {', '.join(bearish)}")
            if not bullish and not bearish:
                lines.append("**结论**: 无明显方向性研判")
            
            lines.append("")
            lines.append("---")
            
            # 每个标的详细分析（增强版）
            for code, assessment in results.items():
                d = direction_map.get(assessment.direction, assessment.direction)
                
                # 获取详细数据
                direction_score = assessment.direction_score if hasattr(assessment, 'direction_score') else 0
                confidence_value = assessment.confidence_value if hasattr(assessment, 'confidence_value') else 0
                
                lines.append(f"### {d} {code}")
                lines.append("")
                lines.append(f"- **信号强度**: {direction_score:+.2f}")
                lines.append(f"- **置信度**: {assessment.confidence_level}")
                lines.append(f"- **仓位建议**: {assessment.position_tier}")
                lines.append(f"- **执行时机**: {assessment.recommended_timing}")
                lines.append("")
                
                # 关键因素（最多2条）
                if hasattr(assessment, 'key_factors') and assessment.key_factors:
                    lines.append(f"**关键因素**:")
                    for i, factor in enumerate(assessment.key_factors[:2], 1):
                        lines.append(f"{i}. {factor[:80]}")
                    lines.append("")
                
                # 推理摘要（80字）
                if hasattr(assessment, 'reasoning') and assessment.reasoning:
                    lines.append(f"**逻辑**: {assessment.reasoning[:80]}")
                    lines.append("")
                
                lines.append("---")
            
            lines.append("")
            lines.append("*qwen3.5-plus | 开盘前定时研判 | 置信度调节因子*")
            
            text = "\n".join(lines)
            
            # 汇总推送（限 4096 字符）
            self.notifier._do_post({
                "msgtype": "markdown",
                "markdown": {
                    "title": f"LLM盘前研判({len(bullish)}多/{len(bearish)}空)",
                    "text": text[:4096],
                },
            })
            logger.info(
                f"[LLM预分析] 钉钉汇总已推送: {len(bullish)}看多/{len(bearish)}看空"
            )
            
            # 逐个标的详细推送（有方向性结果才推）
            for code, assessment in results.items():
                try:
                    assessment_data = assessment.to_dict()
                    self.notifier.send_llm_assessment(
                        code=code,
                        assessment_data=assessment_data,
                    )
                    time_module.sleep(0.3)  # 避免限流
                except Exception as e:
                    logger.debug(f"[LLM预分析] {code} 详细推送失败: {e}")
        
        except Exception as e:
            logger.warning(f"[LLM预分析] 钉钉汇总推送失败: {e}")
    
    def _job_execute(self):
        """09:30 开盘执行"""
        logger.info("[09:30] ========== 开盘执行 ==========")
        
        executed_count = 0
        skipped_count = 0
        
        # 如果有多个信号，先进行冲突检测 (修复6: SignalCoordinator 集成)
        auto_executable_records = [
            r for r in self._signal_records.values()
            if r.is_auto_executable()
        ]
        if len(auto_executable_records) > 1:
            from app.strategy.base import Signal, SignalAction
            # 构造信号列表用于冲突检测
            temp_signals = []
            for r in auto_executable_records:
                action = SignalAction.BUY if r.action == 'BUY' else SignalAction.SELL
                sig = Signal(
                    id=r.signal_id,
                    code=r.code,
                    action=action,
                    strategy='cross_market_arbitrage',
                    priority=4,  # CRITICAL
                    extra=r.metadata,
                )
                temp_signals.append(sig)
            # 获取实际持仓用于冲突检测 (P1: 避免传空dict忽略持仓冲突)
            actual_positions = {}
            try:
                from app.trader.position_manager import PositionManager
                pm = PositionManager.get_instance()
                for pos in pm.get_all_positions():
                    if getattr(pos, 'status', '') != 'CLOSED':
                        actual_positions[pos.code] = {
                            'action': getattr(pos, 'action', 'BUY'),
                            'pnl_pct': getattr(pos, 'unrealized_pnl_pct', 0),
                        }
            except Exception as e:
                logger.warning(f"[09:30] 获取持仓失败，冲突检测降级: {e}")
            
            resolved = self.coordinator.resolve_conflicts(
                signals=temp_signals,
                current_positions=actual_positions,
                is_arb_window=True,
            )
            resolved_ids = {s.id for s in resolved}
            # 将冲突中被撤销的信号标记为 expired
            for r in auto_executable_records:
                if r.signal_id not in resolved_ids:
                    r.review_status = 'expired'
                    logger.info(
                        f"[09:30] 冲突检测: {r.code} {r.action} 被去除 (正常又高优先级信号存在)"
                    )
                    skipped_count += 1
        
        for sig_id, record in list(self._signal_records.items()):
            # 跳过已否决的信号
            if record.is_rejected():
                logger.info(
                    f"[09:30] 跳过 (人工否决): {record.code} "
                    f"理由: {record.review_comment}"
                )
                skipped_count += 1
                continue
            
            # 跳过冲突检测已撤销的信号
            if record.review_status == 'expired':
                continue
            
            # 自动执行未否决的信号
            if record.is_auto_executable():
                record.review_status = 'auto_executed'
                
                # 调用真实执行逻辑 (修复3)
                success = self._execute_signal(record)
                
                if success:
                    record.executed = True
                    executed_count += 1
                    logger.info(f"[09:30] ✅ 自动执行: {record.code} {record.action}")
                    
                    # 推送执行结果
                    self.notifier.send_cross_market_execution_result(
                        metadata_dict=record.metadata,
                        success=True,
                        detail="市价单执行成功",
                    )
                    
                    # 记录到过渡管理器
                    self.transition_mgr.record_arb_signal(executed=True)
                else:
                    logger.error(f"[09:30] ❌ 执行失败: {record.code}")
                    # 推送执行失败通知 (修复3: 添加失败通知)
                    self.notifier.send_cross_market_execution_result(
                        metadata_dict=record.metadata,
                        success=False,
                        detail="执行失败，请检查交易执行器配置",
                    )
        
        logger.info(
            f"[09:30] 执行完成: 执行={executed_count}, "
            f"跳过={skipped_count}"
        )
    
    def _job_cleanup(self):
        """10:00 清理 + 趋势策略接手"""
        logger.info("[10:00] ========== 清理 + 趋势策略接手 ==========")
            
        # 清理未执行信号
        unexecuted = [
            r for r in self._signal_records.values()
            if not r.executed and not r.is_rejected()
        ]
            
        for record in unexecuted:
            record.review_status = 'expired'
            logger.info(f"[10:00] 信号过期: {record.code} {record.action}")
            
        # 输出今日统计
        stats = self.transition_mgr.get_stats()
        logger.info(
            f"[10:00] 今日统计: "
            f"跨市场信号={stats['arb_signals_count']}, "
            f"执行={stats['arb_executed_count']}, "
            f"否决={stats['arb_rejected_count']}"
        )
            
        # 清空信号记录 (修复4: 跨日不清理问题)
        self._signal_records.clear()
        logger.info("[10:00] 信号记录已清空")
            
        # 重置过渡管理器每日统计 (修复4)
        self.transition_mgr.reset_daily_stats()
            
        # 清理旧日期的 _daily_flags key (修复4)
        today_str = datetime.now().strftime("%Y-%m-%d")
        with self._flags_lock:
            old_keys = [k for k in list(self._daily_flags.keys()) if not k.startswith(today_str)]
            for key in old_keys:
                del self._daily_flags[key]
        if old_keys:
            logger.info(f"[10:00] 清理旧日期标记: {len(old_keys)} 个")
            
        logger.info("[10:00] 跨市场策略退出，趋势策略接手")
    
    def handle_reject(self, signal_id: str, user_id: str = None, comment: str = ""):
        """
        处理人工否决
        
        Args:
            signal_id: 信号ID
            user_id: 操作人
            comment: 否决理由
        
        Returns:
            是否处理成功
        """
        record = self._signal_records.get(signal_id)
        if not record:
            logger.warning(f"否决失败: 信号不存在 {signal_id}")
            return False
        
        if not record.is_auto_executable():
            logger.warning(
                f"否决失败: 信号状态不允许否决 "
                f"({record.review_status})"
            )
            return False
        
        # 更新状态
        record.review_status = 'rejected'
        record.reviewed_by = user_id
        record.review_comment = comment
        
        logger.info(
            f"[人工否决] {record.code} {record.action} | "
            f"操作人: {user_id} | 理由: {comment}"
        )
        
        # 推送确认
        self.notifier.send_cross_market_reject_confirm(
            metadata_dict=record.metadata,
            user_id=user_id,
            comment=comment,
        )
        
        # 记录到过渡管理器
        self.transition_mgr.record_arb_signal(rejected=True)
        
        return True
    
    def get_today_signals(self) -> List[CrossMarketSignalRecord]:
        """获取今日信号记录 (仵返回今日的信号)"""
        today = datetime.now().date()
        return [
            r for r in self._signal_records.values()
            if r.created_at.date() == today
        ]
    
    def _fetch_us_overnight_data(self):
        """
        获取美股隔夜数据
        
        使用 USOvernightDataFetcher (复用 FTSeek-US 的 TwelveData + Finnhub)
        """
        try:
            from app.data.us_overnight_fetcher import get_overnight_fetcher
            
            fetcher = get_overnight_fetcher()
            
            if not fetcher._enabled:
                logger.warning("[数据获取] 美股数据源未启用")
                return []
            
            # 获取美股隔夜数据
            us_data = fetcher.fetch_overnight_data()
            
            logger.info(f"[数据获取] 获取到 {len(us_data)} 只美股隔夜数据")
            
            return us_data
        
        except Exception as e:
            logger.error(f"[数据获取] 美股隔夜数据获取失败: {e}", exc_info=True)
            return []
    
    def _pre_execute_risk_check(self, record: CrossMarketSignalRecord) -> tuple:
        """
        执行前风控关键检查 (跨市场信号专用)
        
        只检查系统级安全项，不做策略级过滤 (ADX/多周期等由套利评分覆盖):
        1. 熔断器: 系统已暂停时禁止下单
        2. 持仓上限: 不超过 max_open_positions
        3. 日交易次数: 不超过 max_daily_trades
        
        Returns:
            (passed: bool, reason: str)
        """
        try:
            # --- 1. 熔断器检查 ---
            try:
                from app.risk.circuit_breaker import get_circuit_breaker
                cb = get_circuit_breaker()
            except Exception:
                cb = None
            if cb and cb.is_paused():
                return False, "熔断器已触发，跨市场信号被拦截"
            
            # --- 2. 持仓上限检查 ---
            try:
                from app.trader.position_manager import PositionManager
                pm = PositionManager.get_instance()
                open_positions = pm.get_all_positions()
                open_count = len([p for p in open_positions if getattr(p, 'status', '') != 'CLOSED'])
            except Exception:
                open_count = 0
            
            from config.config_center import get_config_center
            cc = get_config_center()
            max_open = int(cc.get('trading.max_open_positions', 20))  # 2026-04-18 调整 (12->20)
            if open_count >= max_open:
                return False, f"持仓已达上限 ({open_count}/{max_open})"
            
            # --- 3. 日交易次数检查 ---
            max_daily = int(cc.get('risk_limits.max_daily_trades', 5))  # 2026-04-16 对齐 config.yaml (5)
            try:
                from app.analytics.trade_evaluation import get_evaluator
                evaluator = get_evaluator()
                today_trades = getattr(evaluator, 'today_trade_count', 0)
            except Exception:
                today_trades = 0
            if today_trades >= max_daily:
                return False, f"日交易次数已达上限 ({today_trades}/{max_daily})"
            
            return True, ""
        except Exception as e:
            logger.warning(f"[风控预检] 检查异常，放行 (安全降级): {e}")
            return True, ""  # 检查失败时安全降级为放行
    
    def _execute_signal(self, record: CrossMarketSignalRecord) -> bool:
        """
        执行信号 (调用 trade_executor)
        
        从 record 中提取信号信息，构造 Signal 对象并调用 trade_executor.execute()
        执行前会进行风控关键检查 (熔断器/持仓上限/日交易次数)
        """
        if not self.trade_executor:
            logger.warning("[执行] trade_executor 未配置，跳过执行")
            return False
        
        # ── 风控预检 (P0: 跨市场信号不可绕过系统级风控) ──
        risk_ok, risk_reason = self._pre_execute_risk_check(record)
        if not risk_ok:
            logger.warning(
                f"[风控拦截] {record.code} {record.action} 被系统风控拒绝: {risk_reason}"
            )
            # 推送拦截通知
            try:
                self.notifier.send_cross_market_execution_result(
                    metadata_dict=record.metadata,
                    success=False,
                    detail=f"风控拦截: {risk_reason}",
                )
            except Exception:
                pass
            return False
        
        try:
            from app.strategy.base import Signal, SignalAction
            
            # 从 record 中提取信号信息
            action = SignalAction.BUY if record.action == 'BUY' else SignalAction.SELL
            metadata = record.metadata
            
            # 补充跨市场元数据到 metadata，供历史统计模块识别配对
            # lag_code 即港股代码，lead_code 即美股代码
            if 'hk_code' not in metadata:
                metadata['hk_code'] = metadata.get('lag_code', record.code)
            if 'us_code' not in metadata:
                metadata['us_code'] = metadata.get('lead_code', '')
            # 确保 arb_type 存在（用于 _is_cross_market_record 识别）
            if 'arb_type' not in metadata:
                metadata['arb_type'] = 'us_to_hk'
            
            # 构造 Signal 对象
            signal = Signal(
                id=record.signal_id,
                code=record.code,
                action=action,
                price=0.0,  # 开盘市价单
                quantity=0,  # 由 trade_executor 动态计算
                reason=(
                    f"跨市场套利 | 评分={metadata.get('arb_total_score', 0):.2f} "
                    f"({metadata.get('arb_decision', 'execute')})"
                ),
                strategy='cross_market_arbitrage',
                priority=4,  # CRITICAL
                extra=metadata,
            )
            # 记录预期价 (市价单时 price=0, 用 lead_change_pct 估算的参考价)
            # 若 metadata 中有 entry_price 则优先使用
            record.expected_price = (
                float(metadata.get('entry_price')) if metadata.get('entry_price')
                else None
            )
            
            # 调用交易执行器执行
            result = self.trade_executor.execute(signal, trade_type='OPEN')
            
            if result and getattr(result, 'success', False):
                # 记录实际执行时间戳
                record.execution_timestamp = time_module.time()
                
                # 如果 trade_executor 返回了成交价，记录到 record
                exec_price = getattr(result, 'filled_price', None) or getattr(result, 'avg_price', None)
                if exec_price:
                    record.execution_price = float(exec_price)
                    record.metadata['execution_price'] = float(exec_price)
                    
                    # 计算滑点 (signal.price 为 0.0 表示市价单，用 metadata 中的 lead_change_pct 估算)
                    expected = record.expected_price
                    if expected and expected > 0:
                        slippage = abs(exec_price - expected) / expected
                        record.slippage_pct = slippage
                        record.metadata['slippage_pct'] = slippage
                        if slippage > 0.005:  # 高于 0.5% 告警
                            logger.warning(
                                f"[滑点告警] {record.code} 滑点={slippage:.3%}, "
                                f"预期价={expected:.4f}, 成交价={exec_price:.4f}"
                            )
                        else:
                            logger.info(
                                f"[滑点] {record.code} 滑点={slippage:.3%}, "
                                f"预期价={expected:.4f}, 成交价={exec_price:.4f}"
                            )
                    else:
                        logger.debug(f"[滑点] {record.code} 无预期价信息，跳过滑点计算")
                
                logger.info(
                    f"[执行] {record.code} {record.action} 成功 | "
                    f"订单ID={getattr(result, 'order_id', 'N/A')} | "
                    f"成交价={record.execution_price or 'N/A'} | "
                    f"执行时间戳={record.execution_timestamp:.3f}"
                )
                return True
            else:
                msg = getattr(result, 'message', '未知错误') if result else 'trade_executor 返回空结果'
                logger.error(
                    f"[执行] {record.code} {record.action} 失败: {msg}"
                )
                return False
        
        except Exception as e:
            logger.error(f"[执行] {record.code} 执行异常: {e}", exc_info=True)
            return False
    
    # ==================== 盘前计划生成方法 (2026-04-12 新增) ====================
    
    async def _generate_pre_market_plan(self, us_data, signals) -> Optional[Dict]:
        """
        生成盘前交易计划 (LLM综合研判)
        
        Args:
            us_data: 美股隔夜数据
            signals: 跨市场套利信号列表
        
        Returns:
            结构化交易计划JSON
        """
        try:
            logger.info(f"[盘前计划] 开始生成LLM综合研判 ({len(signals)} 个信号)")
            
            # 1. 构建LLM输入上下文
            news_context = self._build_news_context()
            cross_market_context = self._build_cross_market_context(us_data)
            
            # 2. 对每个信号进行LLM研判 (最多10个)
            llm_results = {}
            codes_to_analyze = [s.code for s in signals[:10]]
            
            for code in codes_to_analyze:
                try:
                    # 修复: 盘前计划生成不用urgent，使用正常超时120秒
                    # urgent=True会设置30秒超时，可能导致LLM调用失败
                    assessment = await self._llm_analyst.analyze(
                        code=code,
                        news_context=news_context,
                        cross_market_context=cross_market_context,
                        trigger_type="SCHEDULED",
                        urgent=False  # 修复: 盘前分析有充足时间
                    )
                    
                    if assessment and assessment.direction.value != "NEUTRAL":
                        llm_results[code] = {
                            "direction": assessment.direction.value,
                            "confidence": assessment.confidence.value if assessment.confidence else "MEDIUM",
                            "key_factors": assessment.key_factors[:3] if hasattr(assessment, 'key_factors') else [],
                            "position_suggestion": assessment.position.value if hasattr(assessment, 'position') else "HALF",
                        }
                        logger.info(f"[盘前计划] {code}: {assessment.direction.value} ({assessment.confidence.value if assessment.confidence else 'MEDIUM'})")
                    
                    # 避免API限流，增加间隔到2秒
                    import asyncio
                    await asyncio.sleep(2)
                    
                except Exception as e:
                    logger.warning(f"[盘前计划] {code} LLM研判失败: {e}")
                    continue
            
            if not llm_results:
                logger.warning("[盘前计划] 无有效LLM研判结果，跳过计划生成")
                return None
            
            # 3. 组装交易计划
            plan = self._assemble_plan(signals, llm_results)
            
            logger.info(f"[盘前计划] 生成完成: {len(llm_results)} 个LLM研判")
            return plan
            
        except Exception as e:
            logger.error(f"[盘前计划] 生成失败: {e}", exc_info=True)
            return None
    
    def _build_news_context(self) -> str:
        """构建新闻上下文 (简化版)"""
        try:
            # TODO: 从新闻系统获取最新新闻
            return "暂无实时新闻数据"
        except Exception as e:
            logger.warning(f"[盘前计划] 获取新闻上下文失败: {e}")
            return "新闻数据获取失败"
    
    def _build_cross_market_context(self, us_data) -> str:
        """构建跨市场上下文"""
        try:
            context_lines = ["跨市场数据:"]
            for code, data in list(us_data.items())[:5]:
                change = data.get('change_pct', 0)
                context_lines.append(f"  {code}: {change:+.2f}%")
            return "\n".join(context_lines)
        except Exception as e:
            logger.warning(f"[盘前计划] 构建跨市场上下文失败: {e}")
            return "跨市场数据获取失败"
    
    def _assemble_plan(self, signals, llm_results: Dict) -> Dict:
        """
        组装交易计划JSON
        
        Returns:
            {
                "session": "pre_market",
                "date": "2026-04-13",
                "primary_scene": "TREND_START",
                "arb_candidates": ["HK.00700", "HK.09988"],
                "focus_symbols": ["HK.00700", "HK.03690"],
                "position_bias": "moderate_long",
                "risk_events": ["earnings", "policy"],
                "trade_plan": [
                    "观察ADR与美股夜盘",
                    "若开盘高开不破则回踩买入",
                    "若新闻失效则不追高"
                ],
                "llm_assessment": {
                    "direction": "BUY",
                    "confidence": "HIGH",
                    "key_factors": ["美股科技走强", "腾讯财报超预期"]
                },
                "generated_at": "2026-04-13T08:30:00"
            }
        """
        from datetime import datetime
        
        # 提取关键信息
        arb_candidates = [s.code for s in signals[:5]]
        focus_symbols = list(llm_results.keys())[:5]
        
        # 综合判断整体方向
        directions = [r['direction'] for r in llm_results.values()]
        buy_count = sum(1 for d in directions if 'BUY' in d)
        sell_count = sum(1 for d in directions if 'SELL' in d)
        
        if buy_count > sell_count * 1.5:
            position_bias = "moderate_long"
            overall_direction = "BUY"
        elif sell_count > buy_count * 1.5:
            position_bias = "moderate_short"
            overall_direction = "SELL"
        else:
            position_bias = "neutral"
            overall_direction = "NEUTRAL"
        
        # 提取关键因素
        key_factors = []
        for r in llm_results.values():
            key_factors.extend(r.get('key_factors', []))
        key_factors = list(set(key_factors))[:5]  # 去重，最多5个
        
        # 生成交易计划
        trade_plan = [
            "观察ADR与美股夜盘走势",
            "若开盘符合条件则按信号执行",
            "严格控制仓位，注意风险"
        ]
        
        plan = {
            "session": "pre_market",
            "date": datetime.now().strftime("%Y-%m-%d"),
            "primary_scene": "TREND_START",
            "arb_candidates": arb_candidates,
            "focus_symbols": focus_symbols,
            "position_bias": position_bias,
            "risk_events": [],  # TODO: 从新闻中提取风险事件
            "trade_plan": trade_plan,
            "llm_assessment": {
                "direction": overall_direction,
                "confidence": "HIGH" if len(llm_results) > 5 else "MEDIUM",
                "key_factors": key_factors
            },
            "llm_details": llm_results,  # 保留每个标的的详细研判
            "signal_count": len(signals),
            "llm_coverage": len(llm_results),
            "generated_at": datetime.now().isoformat()
        }
        
        return plan
    
    def _save_pre_market_plan(self, plan: Dict):
        """保存盘前计划到文件"""
        import json
        import os
        
        try:
            date = datetime.now().strftime("%Y-%m-%d")
            filepath = f"data/pre_market_plans/{date}.json"
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(plan, f, ensure_ascii=False, indent=2)
            
            logger.info(f"[盘前计划] 已保存至 {filepath}")
            
        except Exception as e:
            logger.error(f"[盘前计划] 保存失败: {e}", exc_info=True)
    
    def _push_pre_market_plan(self, plan: Dict):
        """推送盘前计划到钉钉"""
        try:
            markdown_text = self._format_plan_to_markdown(plan)
            self.notifier.send_text(markdown_text)
            logger.info("[盘前计划] 已推送钉钉")
        except Exception as e:
            logger.error(f"[盘前计划] 推送失败: {e}", exc_info=True)
    
    def _format_plan_to_markdown(self, plan: Dict) -> str:
        """将计划转换为Markdown格式"""
        lines = [
            "## 📊 盘前交易计划\n",
            f"**日期**: {plan['date']}",
            f"**场景**: {plan['primary_scene']}",
            f"**仓位建议**: {plan['position_bias']}\n",
            "### 🎯 套利候选",
        ]
        
        for symbol in plan.get('arb_candidates', []):
            lines.append(f"- {symbol}")
        
        lines.append("\n### 🤖 LLM综合研判")
        llm = plan.get('llm_assessment', {})
        lines.append(f"- **方向**: {llm.get('direction', 'NEUTRAL')}")
        lines.append(f"- **置信度**: {llm.get('confidence', 'MEDIUM')}")
        
        factors = llm.get('key_factors', [])
        if factors:
            lines.append("- **关键因素**:")
            for factor in factors:
                lines.append(f"  - {factor}")
        
        lines.append("\n### 📝 交易计划")
        for i, step in enumerate(plan.get('trade_plan', []), 1):
            lines.append(f"{i}. {step}")
        
        lines.append(f"\n_生成时间: {plan['generated_at']}_")
        
        return "\n".join(lines)


# 全局单例
_scheduler: Optional[CrossMarketScheduler] = None


def get_scheduler() -> Optional[CrossMarketScheduler]:
    """获取调度器单例"""
    return _scheduler


def init_scheduler(
    strategy: CrossMarketArbitrage,
    notifier: DingTalkNotifier,
    trade_executor=None,
    llm_analyst=None,
) -> CrossMarketScheduler:
    """初始化调度器"""
    global _scheduler
    _scheduler = CrossMarketScheduler(strategy, notifier, trade_executor, llm_analyst)
    return _scheduler
