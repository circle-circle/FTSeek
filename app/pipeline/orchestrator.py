"""Pipeline 端到端编排模块

串联所有 stage:
  NewsCollector → NewsCleaner → EventClusterer → RelevanceScorer
  → EventPackBuilder → PreMarketAnalyst → StrategyRouter → SignalStore/DingTalk
"""

import asyncio
import logging
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Any, Optional

logger = logging.getLogger("ftseek.pipeline.orchestrator")


@dataclass
class PipelineResult:
    """Pipeline 执行结果"""

    trace_id: str
    started_at: float
    completed_at: float = 0.0  # 默认 0.0，在 Pipeline 结束时设置
    raw_news_count: int = 0
    cleaned_news_count: int = 0
    cluster_count: int = 0
    relevant_cluster_count: int = 0
    pack_count: int = 0
    assessment_count: int = 0
    auto_signals: int = 0
    review_cards: int = 0
    observe_only: int = 0
    assessments: List[Any] = field(default_factory=list)
    market_context: str = ""           # 市场环境数据 (指数/汇率/期货)
    price_context: str = ""            # 价格技术数据 (K线/指标/量价)
    auction_context: str = ""          # 竞价验证数据 (09:00-09:20 HKT)
    event_packs: List[Any] = field(default_factory=list)  # 事件包列表
    errors: List[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return self.completed_at - self.started_at

    def to_dict(self) -> Dict:
        return {
            "trace_id": self.trace_id,
            "started_at": datetime.fromtimestamp(self.started_at).isoformat(),
            "completed_at": datetime.fromtimestamp(self.completed_at).isoformat(),
            "duration_seconds": round(self.duration_seconds, 1),
            "raw_news_count": self.raw_news_count,
            "cleaned_news_count": self.cleaned_news_count,
            "cluster_count": self.cluster_count,
            "relevant_cluster_count": self.relevant_cluster_count,
            "pack_count": self.pack_count,
            "assessment_count": self.assessment_count,
            "auto_signals": self.auto_signals,
            "review_cards": self.review_cards,
            "observe_only": self.observe_only,
            "errors": self.errors,
        }


@dataclass
class PreMarketPipeline:
    """盘前 Pipeline 端到端编排器

    负责串联所有 stage，处理异常和降级。
    """

    config: Dict = field(default_factory=dict)

    def __post_init__(self):
        self.config = self.config or {}

        # 延迟初始化各阶段组件
        self._cleaner = None
        self._clusterer = None
        self._scorer = None
        self._pack_builder = None
        self._analyst = None
        self._router = None
        self._signal_converter = None
        self._last_battle_plan = None  # Phase 1 新增: 存储最新生成的作战计划
        self._initialized = False

    def _init_components(self):
        """延迟初始化所有组件"""
        if self._initialized:
            return

        try:
            # Stage 1: 新闻清洗
            from app.pipeline.news_cleaner import NewsCleaner
            cleaner_cfg = self.config.get("cleaner", {})
            self._cleaner = NewsCleaner(config=cleaner_cfg)

            # Stage 2: 事件聚类
            from app.pipeline.event_clusterer import EventClusterer
            clusterer_cfg = self.config.get("clusterer", {})
            self._clusterer = EventClusterer(config=clusterer_cfg)

            # Stage 3: 相关性评分
            from app.pipeline.relevance_scorer import RelevanceScorer
            relevance_cfg = self.config.get("relevance", {})
            self._scorer = RelevanceScorer(config=relevance_cfg)

            # Stage 4: 事件包构造
            from app.pipeline.event_pack_builder import EventPackBuilder
            builder_cfg = self.config.get("pack_builder", {})
            self._pack_builder = EventPackBuilder(config=builder_cfg)

            # Stage 5: LLM 分析
            from app.pipeline.premarket_analyst import PreMarketAnalyst
            analyst_cfg = self.config.get("analyst", {})
            self._analyst = PreMarketAnalyst(config=analyst_cfg)

            # Stage 6: 策略路由
            from app.pipeline.strategy_router import StrategyRouter, PreMarketSignalConverter
            router_cfg = self.config.get("router", {})
            self._router = StrategyRouter(config=router_cfg)
            self._signal_converter = PreMarketSignalConverter()

            # Stage 7: 突发新闻拦截器
            from app.pipeline.news_interceptor import EmergencyNewsInterceptor
            self._interceptor = EmergencyNewsInterceptor()
            self._interceptor.initialize(self._analyst) # 注入 LLM 配置

            self._initialized = True
            logger.info("[PreMarketPipeline] 所有组件初始化完成")

        except Exception as e:
            logger.error(f"[PreMarketPipeline] 组件初始化失败: {e}")
            raise

    async def run(self) -> PipelineResult:
        """执行端到端 Pipeline

        Returns:
            PipelineResult: 执行结果统计
        """
        trace_id = f"PIPE-{datetime.now().strftime('%Y%m%d%H%M')}-{uuid.uuid4().hex[:6].upper()}"
        result = PipelineResult(
            trace_id=trace_id,
            started_at=time.time(),
        )

        logger.info(f"[PreMarketPipeline] 开始执行 trace_id={trace_id}")

        try:
            self._init_components()

            # Stage 1: 采集 + 清洗
            result.raw_news_count, result.cleaned_news_count = await self._stage_clean(result)

            # Stage 1.5: 突发新闻拦截 (检查黑天鹅事件)
            await self._stage_emergency_intercept()

            # Stage 1.6: TTL 过滤 (新增)
            # 在执行聚类前过滤掉过期新闻
            from app.pipeline.news_ttl_filter import NewsTTLFilter
            ttl_filter = NewsTTLFilter()
            cleaned = self._cached_cleaned_news
            
            # 应用 TTL 过滤
            cleaned = ttl_filter.filter_and_decay(cleaned)
            result.cleaned_news_count = len(cleaned)
            self._cached_cleaned_news = cleaned  # 更新缓存

            # Stage 2: 聚类 (使用已缓存的 TTL 过滤后新闻)
            clusters = self._clusterer.cluster(self._cached_cleaned_news)
            result.cluster_count = len(clusters)

            # Stage 3: 相关性评分
            relevant_clusters, scores = self._stage_score(clusters)

            # Stage 4: 事件包构造
            packs = self._stage_build_packs(relevant_clusters, scores)
            result.pack_count = len(packs)
            result.event_packs = packs  # P0 fix: 注入 event_packs 到 result

            # Stage 5: LLM 分析
            assessments = await self._stage_analyze(packs)
            result.assessment_count = len(assessments)
            result.assessments = assessments

            # Stage 6: 策略路由
            result.auto_signals, result.review_cards, result.observe_only = (
                await self._stage_route(assessments)
            )

            result.completed_at = time.time()
            logger.info(
                f"[PreMarketPipeline] 执行完成 trace_id={trace_id} "
                f"耗时={result.duration_seconds:.1f}s "
                f"新闻={result.raw_news_count}->{result.cleaned_news_count} "
                f"聚类={result.cluster_count}->{result.relevant_cluster_count} "
                f"分析={result.assessment_count} "
                f"路由: auto={result.auto_signals} review={result.review_cards} observe={result.observe_only}"
            )

        except Exception as e:
            logger.error(f"[PreMarketPipeline] 执行异常: {e}", exc_info=True)
            result.errors.append(str(e))
            result.completed_at = time.time()

        return result

    # ── 各 Stage 方法 ────────────────────────────────────────────

    async def _stage_clean(self, result: PipelineResult) -> tuple:
        """Stage 1: 采集 + 清洗"""
        logger.info("[Pipeline] Stage 1: 采集 + 清洗")

        # 采集新闻
        from app.news_push.collector import NewsCollector
        collector = NewsCollector()
        try:
            raw_news = collector.fetch_all()
            collector.close()
        except Exception as e:
            logger.error(f"[Pipeline] 新闻采集失败: {e}")
            raw_news = []
            result.errors.append(f"采集失败: {e}")

        result.raw_news_count = len(raw_news)

        # 清洗
        cleaned_news = self._cleaner.clean(raw_news)
        self._cached_cleaned_news = cleaned_news  # 缓存供后续阶段使用
        result.cleaned_news_count = len(cleaned_news)

        if not cleaned_news:
            logger.warning("[Pipeline] Stage 1: 无有效新闻，终止 Pipeline")

        return len(raw_news), len(cleaned_news)

    async def _stage_emergency_intercept(self):
        """Stage 1.5: 突发新闻拦截"""
        if not hasattr(self, '_interceptor') or not self._interceptor:
            return

        try:
            # 1. 获取当前持仓
            from app.trader.position_manager import PositionManager
            positions = PositionManager.get_instance().get_all_positions()
            
            if not positions:
                return

            # 2. 执行拦截检查
            # 使用清洗后的新闻
            cleaned_news = getattr(self, '_cached_cleaned_news', [])
            if not cleaned_news:
                return

            logger.info(f"[Pipeline] Stage 1.5: 突发新闻拦截 (扫描 {len(cleaned_news)} 条新闻, {len(positions)} 个持仓)")
            results = await self._interceptor.check_and_intercept(cleaned_news, positions)
            
            if results:
                logger.warning(f"[Pipeline] 拦截器触发: {len(results)} 条新闻涉及风险")

        except Exception as e:
            logger.error(f"[Pipeline] Stage 1.5 拦截失败: {e}")

    def _stage_score(self, clusters: List[Any]) -> tuple:
        """Stage 3: 相关性评分"""
        logger.info(f"[Pipeline] Stage 3: 相关性评分 ({len(clusters)} 个聚类)")

        relevant = self._scorer.filter_relevant(clusters)
        scores = [getattr(c, "_relevance_scores", {}) for c in relevant]

        return relevant, scores

    def _stage_build_packs(
        self, clusters: List[Any], scores: List[Dict[str, float]]
    ) -> List[Any]:
        """Stage 4: 事件包构造"""
        logger.info(f"[Pipeline] Stage 4: 事件包构造 ({len(clusters)} 个)")
        return self._pack_builder.build_batch(clusters, scores)

    async def _stage_analyze(self, packs: List[Any]) -> List[Any]:
        """Stage 5: LLM 分析 + 盘前作战计划生成"""
        if not packs:
            logger.info("[Pipeline] Stage 5: 无事件包，跳过分析")
            return []

        # 防御: 港股 Pipeline 过滤美股标的 (防止跨市场污染)
        packs = self._filter_hk_packs(packs)

        # P0 fix (2026-05-09): 收集市场上下文和价格数据注入 PipelineResult
        self._collect_market_context()
        self._collect_price_context()

        # Phase 2 (2026-05-09): 收集竞价验证数据 (09:00-09:20 HKT)
        self._collect_auction_context()

        logger.info(f"[Pipeline] Stage 5: LLM 分析 ({len(packs)} 个包)")
        assessments = await self._analyst.analyze_batch(packs)
        
        # LLM 分析质量摘要 (2026-05-22 新增)
        if assessments:
            confidence_values = [a.confidence for a in assessments]
            action_counts = {}
            for a in assessments:
                action = getattr(a, 'action', 'NO_TRADE')
                action_counts[action] = action_counts.get(action, 0) + 1
            
            avg_conf = sum(confidence_values) / len(confidence_values)
            min_conf = min(confidence_values)
            max_conf = max(confidence_values)
            
            logger.info(
                f"[Pipeline] LLM 分析摘要: "
                f"总计={len(assessments)} 平均置信度={avg_conf:.2f} "
                f"min={min_conf:.2f} max={max_conf:.2f} "
                f"动作分布={action_counts}"
            )
        
        # Phase 1 新增: 生成日内作战计划 (Daily Battle Plan)
        try:
            # P0 修复 (2026-05-11): 防止 Pipeline 补执行覆盖午间刷新结果
            # 检查文件是否存在且在最近 60 分钟内更新过
            plan_path = self._analyst.output_path
            should_generate = True
            if os.path.exists(plan_path):
                mtime = os.path.getmtime(plan_path)
                # 如果文件在 1 小时内更新过，说明已有最新计划，跳过生成
                if time.time() - mtime < 3600:
                    logger.info(f"[Pipeline] 跳过作战计划生成: 文件在 {mtime} 已更新 (防覆盖午间刷新)")
                    should_generate = False

            if should_generate:
                plan = self._analyst.generate_battle_plan_json(assessments)
                logger.info(f"[Pipeline] 作战计划已生成: {len(plan['targets'])} 个目标, 基调={plan['overall_bias']}")
                self._last_battle_plan = plan
            else:
                # P0 修复 (2026-05-26): 跳过生成时，从文件加载现有计划
                # 根因: 原代码用 `or` 短路逻辑，should_generate=False 时仍会调用 generate 覆盖好计划
                try:
                    with open(plan_path, 'r', encoding='utf-8') as f:
                        plan = json.load(f)
                    self._last_battle_plan = plan
                    logger.info(f"[Pipeline] 复用现有作战计划: {len(plan.get('targets', []))} 个目标")
                except Exception as e:
                    logger.warning(f"[Pipeline] 加载现有计划失败: {e}, 回退到空计划")
                    plan = {"targets": [], "restricted": [], "overall_bias": "NEUTRAL"}
                    self._last_battle_plan = plan
            
            # 2026-05-11 新增: 自动注入战斗计划目标到动态池 (必须在 if 之外)
            # 2026-05-17 修复: 传递 signal_impact/urgency 到动态池，保留 LLM 信号质量
            # P0 修复 (2026-05-26): 移除危险的 `or` 短路，直接使用 self._last_battle_plan
            from app.utils.daily_active_pool import get_daily_active_pool
            pool = get_daily_active_pool(market_prefix="HK.")
            injected = 0
            for t in plan.get('targets', []):
                sym = t.get('symbol', '')
                if sym.startswith('HK.'):
                    try:
                        pool.add_plan_target(
                            code=sym,
                            direction=t.get('direction', 'ANY'),
                            logic=t.get('key_trigger', t.get('logic', '')),
                            max_position_pct=t.get('max_position_pct', 0.05),
                            signal_impact=t.get('signal_impact', 'neutral'),
                            urgency=t.get('urgency', 'Medium'),
                        )
                        injected += 1
                    except Exception as e:
                        logger.debug(f"[Pipeline] 注入 {sym} 到动态池失败: {e}")
            if injected:
                logger.info(f"[Pipeline] 战斗计划已注入 {injected} 只标的到动态池")
            
            # 领域事件 — 盘前计划生成
            from app.core.domain_events import get_event_bus, DomainEvent, EventType, PlanPremarketPayload
            target_symbols = [t.get('symbol', '') for t in plan.get('targets', [])]
            get_event_bus().emit(DomainEvent(
                type=EventType.PLAN_PREMARKET,
                data=PlanPremarketPayload(
                    date=plan.get('date', datetime.now().strftime("%Y-%m-%d")),
                    market='HK',
                    targets=target_symbols,
                    restricted=[t.get('symbol', '') for t in plan.get('restricted', [])],
                    overall_bias=plan.get('overall_bias', '中性'),
                    regime=plan.get('market_regime', 'unknown'),
                    generated_at=plan.get('generated_at', ''),
                )
            ))
            logger.info(f"[Pipeline] 盘前计划领域事件已发射")
        except Exception as e:
            logger.warning(f"[Pipeline] 生成作战计划失败: {e}")
            
        return assessments

    async def _stage_route(self, assessments: List[Any]) -> tuple:
        """Stage 6: 策略路由"""
        if not assessments:
            return 0, 0, 0

        logger.info(f"[Pipeline] Stage 6: 策略路由 ({len(assessments)} 个评估)")
        routed = self._router.route_batch(assessments)

        auto_count = 0
        review_count = 0
        observe_count = 0

        # 自动执行：生成 Signal → 直接执行 + 通知
        for a in routed.get("auto", []):
            signal = self._signal_converter.convert(a)
            if signal:
                # P0 修复 (2026-05-21): auto 信号必须执行，不能只入库
                # 根因：只调 _send_to_signal_store 不执行不发通知 → 全部过期
                self._execute_auto_signal(a, signal)
                auto_count += 1

        # 人工确认：发送 DingTalk ActionCard
        for a in routed.get("review", []):
            self._send_review_card(a)
            review_count += 1

        # 仅观察：计入统计 + 记录 NO_TRADE 详情
        observe_list = routed.get("observe", [])
        observe_count = len(observe_list)
        
        # 【P1 2026-05-22 优化】NO_TRADE 统计日志
        if observe_count > 0:
            no_trade_details = []
            conf_values = []
            for a in observe_list:
                conf = getattr(a, 'confidence', 0)
                conf_values.append(conf)
                event_pack_id = getattr(a, 'event_pack_id', 'N/A')
                no_trade_details.append(f"  - EP-{event_pack_id}: conf={conf:.2f}, tradeability={getattr(a, 'tradeability', 'N/A')}")
            
            # 置信度分布统计
            conf_avg = sum(conf_values) / len(conf_values) if conf_values else 0
            conf_max = max(conf_values) if conf_values else 0
            conf_min = min(conf_values) if conf_values else 0
            
            logger.info(
                f"[Pipeline] NO_TRADE 统计：{observe_count} 个事件进入观察队列\n"
                f"  置信度分布：avg={conf_avg:.2f}, max={conf_max:.2f}, min={conf_min:.2f}\n"
                f"  阈值检查：auto>={self._router.auto_execute_threshold:.2f}, review>=0.55, observe<0.55\n"
                f"  详情 (前 5 个):\n" + "\n".join(no_trade_details[:5]) +
                (f"\n  ... 还有 {len(no_trade_details)-5} 个" if len(no_trade_details) > 5 else "")
            )

        return auto_count, review_count, observe_count

    def _send_to_signal_store(self, signal: Any):
        """将 Signal 发送到 SignalStore"""
        try:
            from app.signal.core import signal_store
            success = signal_store.add(signal)
            if success:
                logger.info(
                    f"[Pipeline] 信号已入库: {signal.code} {signal.action.value} "
                    f"(strategy=premarket_analyst)"
                )
            else:
                logger.warning(f"[Pipeline] 信号被冷却拦截: {signal.code}")
        except Exception as e:
            logger.error(f"[Pipeline] 信号入库失败: {e}")

    def _execute_auto_signal(self, assessment: Any, signal: Any):
        """P0 修复 (2026-05-21): auto 信号直接执行 + 通知
        
        根因: auto 信号只调 _send_to_signal_store → 无人确认 → 全部过期
        修复: auto 信号已通过 strategy_router 高置信度过滤，应直接执行
        """
        from app.signal.core import signal_store
        
        # 1. 先入库 (用于追踪和冷却)
        success = signal_store.add(signal)
        if not success:
            logger.warning(f"[Pipeline] auto 信号被冷却拦截: {signal.code}")
            return
        
        # 2. 直接执行
        try:
            from app.trader.executor import get_executor
            executor = get_executor()
            
            if executor and signal.action.value in ("BUY", "SELL"):
                result = executor.execute(signal)
                signal_store.mark_executed(signal.id)
                
                # TradeResult 是 dataclass，用属性访问
                ok = result.success if hasattr(result, 'success') else False
                msg = result.message if hasattr(result, 'message') else str(result)
                
                logger.info(
                    f"[Pipeline] ✅ auto 信号已执行: {signal.code} {signal.action.value} "
                    f"success={ok} msg={msg[:100]}"
                )
                
                # 3. 通知执行结果
                try:
                    from app.main import get_notifier
                    notifier = get_notifier()
                    if notifier and hasattr(notifier, 'send_execution_result'):
                        notifier.send_execution_result(signal, ok, msg)
                except Exception as e:
                    logger.debug(f"[Pipeline] 执行结果通知失败: {e}")
            else:
                logger.warning(
                    f"[Pipeline] auto 信号无 executor 或非交易信号: "
                    f"{signal.code} {signal.action.value}"
                )
                signal_store.mark_executed(signal.id)
        except Exception as e:
            logger.error(f"[Pipeline] auto 信号执行失败: {signal.code}: {e}")

    def _send_review_card(self, assessment: Any):
        """发送 DingTalk 人工确认 ActionCard"""
        try:
            from app.notifier.dingtalk import DingTalkNotifier

            # 尝试获取全局 notifier
            notifier = None
            try:
                from app.main import get_notifier
                notifier = get_notifier()
            except Exception as _e:
                logger.debug(f'SignalStore 发送失败: {_e}')


            if notifier and hasattr(notifier, "send_review_action_card"):
                card_data = self._signal_converter.to_dingtalk_review_card(assessment)
                notifier.send_review_action_card(card_data)
            else:
                logger.info(
                    f"[Pipeline] 待确认评估: {assessment.event_pack_id} "
                    f"({assessment.direction}, conf={assessment.confidence:.2f})"
                )
        except Exception as e:
            logger.error(f"[Pipeline] ActionCard 发送失败: {e}")

    # ------------------------------------------------------------------
    #  跨市场防御
    # ------------------------------------------------------------------

    def _collect_market_context(self):
        """Phase 2 (2026-05-09): 收集市场环境数据注入 PipelineResult
        增强版: 港股指数 + A股收盘 + 美股隔夜 + VIX + 汇率 + 南向资金
        """
        try:
            from futu import OpenQuoteContext, RET_OK
            from app import config

            ctx = OpenQuoteContext(host=config.OPEND_HOST, port=config.OPEND_PORT)
            try:
                parts = []

                # 1. 港股指数
                hk_indices = {"HK.800000": "恒生指数", "HK.800700": "国企指数", "HK.HSI": "恒生科技"}
                ret, data = ctx.get_market_snapshot(list(hk_indices.keys()))
                if ret == RET_OK and data is not None and not data.empty:
                    parts.append("【港股大盘】")
                    for _, row in data.iterrows():
                        code = row.get("code", "")
                        name = hk_indices.get(code, code)
                        last = float(row.get("last_price", 0) or 0)
                        change = float(row.get("change_rate", 0) or 0) / 100.0
                        parts.append(f"  {name} ({code}): {last:.2f} ({change:+.2%})")

                # 2. A股指数 (前日收盘)
                ashare_indices = {"SH.000001": "上证指数", "SH.000300": "沪深300", "SZ.399006": "创业板指"}
                ret, data = ctx.get_market_snapshot(list(ashare_indices.keys()))
                if ret == RET_OK and data is not None and not data.empty:
                    parts.append("【A股收盘】")
                    for _, row in data.iterrows():
                        code = row.get("code", "")
                        name = ashare_indices.get(code, code)
                        last = float(row.get("last_price", 0) or 0)
                        change = float(row.get("change_rate", 0) or 0) / 100.0
                        parts.append(f"  {name} ({code}): {last:.2f} ({change:+.2%})")

                # 3. 美股隔夜表现 (Phase 2 新增)
                us_indices = {"US.DJI": "道琼斯", "US.SPX": "标普500", "US.IXIC": "纳斯达克"}
                ret, data = ctx.get_market_snapshot(list(us_indices.keys()))
                if ret == RET_OK and data is not None and not data.empty:
                    parts.append("【美股隔夜】")
                    for _, row in data.iterrows():
                        code = row.get("code", "")
                        name = us_indices.get(code, code)
                        last = float(row.get("last_price", 0) or 0)
                        change = float(row.get("change_rate", 0) or 0) / 100.0
                        parts.append(f"  {name} ({code}): {last:.2f} ({change:+.2%})")

                # 4. 中概股 ETF (Phase 2 新增)
                adr_etf = {"US.KWEB": "中概互联ETF", "US.FXI": "中国大盘ETF"}
                ret, data = ctx.get_market_snapshot(list(adr_etf.keys()))
                if ret == RET_OK and data is not None and not data.empty:
                    parts.append("【中概股ETF】")
                    for _, row in data.iterrows():
                        code = row.get("code", "")
                        name = adr_etf.get(code, code)
                        last = float(row.get("last_price", 0) or 0)
                        change = float(row.get("change_rate", 0) or 0) / 100.0
                        parts.append(f"  {name} ({code}): {last:.2f} ({change:+.2%})")

                # 5. 汇率
                ret, data = ctx.get_market_snapshot(["HK.USDHKD"])
                if ret == RET_OK and data is not None and not data.empty:
                    rate = float(data.iloc[0].get("last_price", 0) or 0)
                    parts.append(f"【汇率】USD/HKD: {rate:.4f}")

                # 6. VIX 恐慌指数 (Phase 2 新增)
                ret, data = ctx.get_market_snapshot(["US.VIX"])
                if ret == RET_OK and data is not None and not data.empty:
                    vix = float(data.iloc[0].get("last_price", 0) or 0)
                    vix_status = "高波动" if vix > 25 else "正常" if vix < 20 else "警戒"
                    parts.append(f"【VIX恐慌指数】{vix:.2f} ({vix_status})")

                self.market_context = "\n".join(parts) if parts else "无实时数据"
                logger.info(f"[Pipeline] 市场上下文已收集: {len(parts)} 项")
            finally:
                ctx.close()
        except Exception as e:
            logger.warning(f"[Pipeline] 市场上下文收集失败: {e}")
            self.market_context = "数据不可用"

    def _collect_price_context(self):
        """P0 fix (2026-05-09): 收集价格技术数据注入 PipelineResult"""
        try:
            from app import config
            watchlist = getattr(config, 'WATCH_LIST_HK', [])[:15]
            if not watchlist:
                self.price_context = "watchlist为空"
                return

            from futu import OpenQuoteContext, RET_OK
            ctx = OpenQuoteContext(host=config.OPEND_HOST, port=config.OPEND_PORT)
            try:
                ret, data = ctx.get_market_snapshot(watchlist)
                parts = []
                if ret == RET_OK and data is not None and not data.empty:
                    for _, row in data.iterrows():
                        code = row.get("code", "")
                        last = float(row.get("last_price", 0) or 0)
                        change = float(row.get("change_rate", 0) or 0) / 100.0
                        vol_ratio = float(row.get("volume_ratio", 0) or 0)
                        turnover = float(row.get("turnover", 0) or 0)
                        amplitude = float(row.get("amplitude", 0) or 0) / 100.0 if row.get("amplitude") else 0
                        parts.append(
                            f"  {code}: {last:.2f} ({change:+.2%}) "
                            f"量比={vol_ratio:.1f}x 换手={turnover:.2%} 振幅={amplitude:.2%}"
                        )
                self.price_context = "\n".join(parts) if parts else "无实时数据"
                logger.info(f"[Pipeline] 价格上下文已收集: {len(parts)} 只标的")
            finally:
                ctx.close()
        except Exception as e:
            logger.warning(f"[Pipeline] 价格上下文收集失败: {e}")
            self.price_context = "数据不可用"

    def _collect_auction_context(self):
        """Phase 2 (2026-05-09): 收集竞价验证数据 (09:00-09:20 HKT)
        用于开盘前最后过滤：跳空幅度/方向背离/ADR传导
        """
        try:
            from futu import OpenQuoteContext, RET_OK
            from app import config

            ctx = OpenQuoteContext(host=config.OPEND_HOST, port=config.OPEND_PORT)
            try:
                watchlist = getattr(config, 'WATCH_LIST_HK', [])
                if not watchlist:
                    self.auction_context = "watchlist为空"
                    return

                # 获取竞价盘口数据
                ret, data = ctx.get_market_snapshot(watchlist)
                parts = []
                if ret == RET_OK and data is not None and not data.empty:
                    parts.append("【竞价盘口】")
                    for _, row in data.iterrows():
                        code = row.get("code", "")
                        last = float(row.get("last_price", 0) or 0)
                        y_close = float(row.get("last_close", 0) or 0)
                        change = float(row.get("change_rate", 0) or 0) / 100.0
                        bid = float(row.get("bid_price", 0) or 0)
                        ask = float(row.get("ask_price", 0) or 0)

                        # 计算跳空幅度
                        gap_pct = (last - y_close) / y_close if y_close > 0 else 0
                        gap_status = "⚠️大跳空" if abs(gap_pct) > 0.03 else "正常"

                        parts.append(
                            f"  {code}: 竞价{last:.2f} ({change:+.2%}) "
                            f"买一={bid:.2f} 卖一={ask:.2f} "
                            f"跳空={gap_pct:+.2%} {gap_status}"
                        )

                self.auction_context = "\n".join(parts) if parts else "无竞价数据"
                logger.info(f"[Pipeline] 竞价上下文已收集: {len(parts)} 只标的")
            finally:
                ctx.close()
        except Exception as e:
            logger.warning(f"[Pipeline] 竞价上下文收集失败: {e}")
            self.auction_context = "数据不可用"

    def _filter_hk_packs(self, packs: List[Any]) -> List[Any]:
        """港股 Pipeline 防御: 过滤纯美股标的的事件包"""
        filtered = []
        skipped = 0
        for pack in packs:
            focus = getattr(pack, "focus_symbols", []) or []
            # 仅保留含港股标的的事件包
            hk_symbols = [s for s in focus if s.startswith("HK.")]
            if hk_symbols:
                # 更新 focus_symbols 仅保留港股
                pack.focus_symbols = hk_symbols
                filtered.append(pack)
            else:
                skipped += 1
                # 记录被过滤的美股事件包
                theme = getattr(pack, "theme", "")
                us_only = [s for s in focus if s.startswith("US.")]
                if us_only:
                    logger.info(
                        f"[Pipeline] 过滤纯美股事件包: {theme} -> {us_only[:3]}"
                    )

        if skipped:
            logger.info(f"[Pipeline] 跨市场过滤: 跳过 {skipped} 个纯美股事件包")

        return filtered
