"""Pipeline 端到端编排模块

串联所有 stage:
  NewsCollector → NewsCleaner → EventClusterer → RelevanceScorer
  → EventPackBuilder → PreMarketAnalyst → StrategyRouter → SignalStore/DingTalk
"""

import asyncio
import logging
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

    def _stage_cluster(self, result: PipelineResult) -> List[Any]:
        """Stage 2: 事件聚类"""
        logger.info("[Pipeline] Stage 2: 事件聚类")

        # 获取清洗后的新闻 (重新采集并清洗，或从缓存获取)
        from app.news_push.collector import NewsCollector
        collector = NewsCollector()
        try:
            raw_news = collector.fetch_all()
            collector.close()
        except Exception:
            raw_news = []

        cleaned_news = self._cleaner.clean(raw_news)
        clusters = self._clusterer.cluster(cleaned_news)

        # 缓存新闻供后续使用
        self._cached_cleaned_news = cleaned_news

        return clusters

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

        logger.info(f"[Pipeline] Stage 5: LLM 分析 ({len(packs)} 个包)")
        assessments = await self._analyst.analyze_batch(packs)
        
        # Phase 1 新增: 生成日内作战计划 (Daily Battle Plan)
        try:
            plan = self._analyst.generate_battle_plan_json(assessments)
            logger.info(f"[Pipeline] 作战计划已生成: {len(plan['targets'])} 个目标, 基调={plan['overall_bias']}")
            self._last_battle_plan = plan
            
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

        # 自动执行: 生成 Signal → SignalStore
        for a in routed.get("auto", []):
            signal = self._signal_converter.convert(a)
            if signal:
                self._send_to_signal_store(signal)
                auto_count += 1

        # 人工确认: 发送 DingTalk ActionCard
        for a in routed.get("review", []):
            self._send_review_card(a)
            review_count += 1

        # 仅观察: 计入统计
        observe_count = len(routed.get("observe", []))

        return auto_count, review_count, observe_count

    def _send_to_signal_store(self, signal: Any):
        """将 Signal 发送到 SignalStore"""
        try:
            from app.signal_store import signal_store
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
