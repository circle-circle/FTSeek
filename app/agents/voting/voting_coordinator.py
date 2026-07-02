from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import os
import time
import hashlib
import json
import logging
from dataclasses import dataclass

from .voting_config import VotingConfig, ModelRole
from .model_agent import ModelAgent, ModelScore
from .voting_aggregator import VotingAggregator
# P2.4 (RFC-060): DebateEngine / DebateSummary import 已移除 (DebateEngine 创建后从未调用,
# 见下方注释); 模块仍保留供 catalyst/review 等其他路径使用.

# P0 清理 2026-04-25: 移除已废弃的 agent 导入
# TraderAgent, RiskAgent, FinalDecision 等已废弃
# 使用简单的占位类保持向后兼容



@dataclass
class VetoResult:
    """简化的否决结果"""
    vetoed: bool = False
    reasons: list = None
    risk_score: float = 0.0



# P1 新增: 决策记录器 — 打通投票→回测→反哺链路
from app.storage.decision_recorder import get_decision_recorder

# 本模块仅用于盘前/盘后 LLM 多模型投票。盘中由 app.pipeline.signal_pipeline._generate_technical_signal() 纯技术面驱动。
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# P1 修复: LLM 投票结果缓存
# ------------------------------------------------------------------

@dataclass
class VotingCacheEntry:
    """投票缓存条目"""
    result: Dict[str, Any]
    market_hash: str  # 行情快照哈希
    price_snapshot: Dict[str, float]  # 各标的价格快照
    timestamp: float
    hit_count: int = 0


class VotingCache:
    """
    LLM 投票结果缓存

    缓存策略：
    - 按 symbol 缓存
    - 行情变动 < price_change_threshold 且技术指标无信号切换时复用
    - 最长 TTL cache_ttl_seconds，超时强制刷新

    2026-05-31 优化: TTL 30min→5min, 阈值 0.3%→0.8%, 参数化可从 unified_config.yaml 读取
    """

    # 默认值 (2026-05-31 优化: 30min→5min 提升决策新鲜度)
    DEFAULT_CACHE_TTL = 300            # 5 分钟 (原 1800)
    DEFAULT_PRICE_CHANGE_THRESHOLD = 0.008  # 0.8% (原 0.003)
    DEFAULT_MAX_CACHE_ENTRIES = 50

    def __init__(
        self,
        cache_ttl: Optional[int] = None,
        price_change_threshold: Optional[float] = None,
        max_cache_entries: Optional[int] = None,
    ):
        # 优先使用显式参数，其次从 unified_config.yaml 读取，最后使用默认值
        cfg_ttl = None
        cfg_thr = None
        cfg_max = None
        try:
            from app.core.config_container import get_config
            cfg = get_config()
            cfg_ttl = cfg.get_dotted("llm_voting.cache_ttl_seconds", None)
            cfg_thr = cfg.get_dotted("llm_voting.price_change_threshold", None)
            cfg_max = cfg.get_dotted("llm_voting.max_cache_entries", None)
        except Exception as e:
            logger.debug(f"[投票缓存] 读取 llm_voting 配置失败, 使用默认值: {e}")

        self.cache_ttl = int(
            cache_ttl if cache_ttl is not None
            else (cfg_ttl if cfg_ttl is not None else self.DEFAULT_CACHE_TTL)
        )
        self.price_change_threshold = float(
            price_change_threshold if price_change_threshold is not None
            else (cfg_thr if cfg_thr is not None else self.DEFAULT_PRICE_CHANGE_THRESHOLD)
        )
        self.max_cache_entries = int(
            max_cache_entries if max_cache_entries is not None
            else (cfg_max if cfg_max is not None else self.DEFAULT_MAX_CACHE_ENTRIES)
        )

        self.entries: Dict[str, VotingCacheEntry] = {}
        logger.info(
            f"[VotingCache] 初始化 ttl={self.cache_ttl}s "
            f"price_threshold={self.price_change_threshold:.3%} "
            f"max_entries={self.max_cache_entries}"
        )

    # 向后兼容: 保留旧类常量引用 (只读, 不再生效, 仅供旧代码访问不报错)
    @property
    def CACHE_TTL(self) -> int:  # noqa: N802 (历史命名)
        return self.cache_ttl

    @property
    def PRICE_CHANGE_THRESHOLD(self) -> float:  # noqa: N802
        return self.price_change_threshold

    def _compute_market_hash(self, market_data: Dict[str, Any]) -> str:
        """计算行情快照哈希"""
        key_fields = {
            "symbol": market_data.get("symbol", ""),
            "rsi": market_data.get("rsi"),
            "macd_signal": market_data.get("macd_signal"),
            "regime": market_data.get("regime"),
        }
        return hashlib.md5(json.dumps(key_fields, sort_keys=True).encode()).hexdigest()[:8]

    def get(self, symbol: str, market_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        尝试从缓存获取投票结果

        Returns:
            缓存结果 or None（缓存未命中/过期/行情变动大）
        """
        entry = self.entries.get(symbol)
        if not entry:
            logger.info(f"[VotingCache] {symbol} 缓存未命中 reason=no_entry")
            return None

        # TTL 检查
        age = time.time() - entry.timestamp
        if age > self.cache_ttl:
            logger.info(
                f"[VotingCache] {symbol} 缓存未命中 reason=ttl_expired age={age:.0f}s ttl={self.cache_ttl}s"
            )
            del self.entries[symbol]
            return None

        # 行情变动检查
        current_price = market_data.get("price", 0)
        cached_price = entry.price_snapshot.get(symbol, 0)
        change_pct = 0.0

        if current_price > 0 and cached_price > 0:
            change_pct = abs(current_price - cached_price) / cached_price
            if change_pct > self.price_change_threshold:
                logger.info(
                    f"[VotingCache] {symbol} 缓存未命中 reason=price_change "
                    f"pct={change_pct:.2%} threshold={self.price_change_threshold:.2%}"
                )
                del self.entries[symbol]
                return None

        # 技术指标信号检查
        current_hash = self._compute_market_hash(market_data)
        if current_hash != entry.market_hash:
            logger.info(
                f"[VotingCache] {symbol} 缓存未命中 reason=signal_change "
                f"old={entry.market_hash} new={current_hash}"
            )
            del self.entries[symbol]
            return None

        # 缓存命中
        entry.hit_count += 1
        logger.debug(
            f"[VotingCache] {symbol} 缓存命中 age={age:.0f}s "
            f"price_change={change_pct:.2%} hits={entry.hit_count}"
        )
        return entry.result

    def put(self, symbol: str, result: Dict[str, Any], market_data: Dict[str, Any]):
        """写入缓存 (超过最大条目数时按 LRU 淘汰最早写入的)"""
        # 简单容量控制: 满则按 timestamp 淘汰最早一条
        if symbol not in self.entries and len(self.entries) >= self.max_cache_entries:
            oldest_symbol = min(
                self.entries.items(), key=lambda kv: kv[1].timestamp
            )[0]
            logger.debug(
                f"[VotingCache] 容量已满({self.max_cache_entries}), 淘汰最早条目 {oldest_symbol}"
            )
            del self.entries[oldest_symbol]

        price = market_data.get("price", 0)
        self.entries[symbol] = VotingCacheEntry(
            result=result,
            market_hash=self._compute_market_hash(market_data),
            price_snapshot={symbol: price} if price > 0 else {},
            timestamp=time.time(),
        )

    def clear(self, symbol: Optional[str] = None):
        """清除缓存"""
        if symbol:
            self.entries.pop(symbol, None)
        else:
            self.entries.clear()

    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        total_hits = sum(e.hit_count for e in self.entries.values())
        return {
            "size": len(self.entries),
            "total_hits": total_hits,
            "ttl": self.cache_ttl,
            "price_change_threshold": self.price_change_threshold,
            "max_entries": self.max_cache_entries,
        }

class VotingCoordinator:
    """投票协调器 - 3模型架构 2026-06-28

    3 模型分工:
    - qwen3.7-max (50%): 综合策略师, 宏观+技术综合判断
    - glm-5.2 (30%): 纯技术分析师, K线/指标/量价
    - deepseek-v4-pro (20%): 风险/反向审查官, 魔鬼代言人
    """

    # 3模型差异化提示词 (2026-06-28)
    # qwen3.7_max: 综合策略师
    # glm_5_2: 纯技术分析师
    # deepseek_v4_pro: 风险/反向审查官
    _ROLE_PROMPTS: Dict[str, str] = {

        "qwen3.7_max": """【综合策略师】你负责宏观事件传导链、二阶推理、跨资产共振和技术面综合判断。

【核心能力 - 美股特异性强化】:
- **宏观共振优先**: 必须结合 SPY/QQQ 大盘趋势判断。若个股技术看涨但大盘走跌，需大幅降低置信度 (Confidence) 和强度 (Strength)。
- **机构/技术主导**: 重点分析技术指标与量价配合 (RSI/MACD/Volume/MA趋势)。
- **Livermore 大盘状态**: 严格遵循 "顺势而为" 原则，VIX 过高时拒绝做多。

✅ 你关注的:
• 大盘基准 (SPY/QQQ) 趋势及关键均线 (MA50/MA200) 支撑/阻力
• 宏观政策变化 (利率/关税/财政刺激) 及对市场流动性的影响
• 板块轮动逻辑 (资金从 A 板块流向 B 板块)
• 技术指标共振 (趋势 + 动量 + 成交量)

❌ 你不看的:
• 纯情绪宣泄
• 个股微观基本面 (短线不依赖)

【重要共振原则】:
- 若 SPY 趋势向下，即使个股技术形态良好，最大 Strength 不得超过 60 (看多)。
- 若 VIX > 25，降低所有做多信号的置信度 (Confidence * 0.8)。
- 返回纯 JSON，不要使用 ```json 代码块，不要有任何额外文本。
返回格式：{"strength": 65, "confidence": 0.72, "direction": "LONG", "reasoning": "降息预期+MACD金叉，但SPY承压，降低评分"}""",

        "glm_5_2": """【纯技术面分析师】你 ONLY 关注价格走势和技术指标，禁止参考任何基本面、宏观或情绪信息。

✅ 你关注的:
• K 线形态（突破/旗形/三角整理/双底双顶）
• 趋势指标（MA5/MA20/MA50 排列、EMA 穿越）
• 动量指标（RSI 超买超卖、MACD 金叉死叉、随机指标）
• 量价关系（放量突破/缩量回调/量价背离）
• 支撑阻力位（前高前低、整数关口、Fibonacci）

❌ 你不看的:
• 新闻/情绪/事件
• 宏观政策/板块轮动
• 任何非图表数据

评分标准:
- 0-30: 强烈看空（明确下跌趋势 + 动量向下 + 跌破支撑）
- 31-45: 看空（趋势偏空或动量走弱）
- 46-55: 中性（横盘震荡/信号矛盾）
- 56-70: 看多（趋势偏多或动量走强）
- 71-100: 强烈看多（明确上涨趋势 + 动量向上 + 突破阻力）

【重要】只基于技术面给出判断。返回纯 JSON，不要使用 ```json 代码块。
返回格式：{"strength": 65, "confidence": 0.72, "direction": "LONG", "reasoning": "MA20上穿MA50+RSI=62动量上行+量价配合"}""",

        "deepseek_v4_pro": """【风险审查官】你的职责是从风险角度独立评估这笔交易，找出最可能出错的地方。

你关注:
- 当前交易假设最薄弱的环节
- 大盘反转时这笔交易会亏多少
- 止损是否合理, 仓位是否过大
- 是否存在共识陷阱(所有人都看多时最危险)
- 技术面是否有背离或失效信号

评分标准:
- strength: 0-100, 表示你对当前方向的信心(50=中性, >50偏多, <50偏空)
- 如果认为做多风险大, strength应低于50(偏SHORT)
- 如果认为做空风险大, strength应高于50(偏LONG)
- VIX>25时, 极端方向信心不超过60
- confidence: 0.0-1.0, 你的确信度
- direction: "LONG"或"SHORT" (你必须给出方向判断)

返回纯JSON, 不要使用代码块。
返回格式: {"strength": 40, "confidence": 0.80, "direction": "SHORT", "reasoning": "价格空头排列+MACD负值, 做多风险大"}""",

    }

    
    def __init__(
        self,
        config: Optional[VotingConfig] = None,
        timeout_seconds: int = 90,
        enable_debate: bool = True,
        enable_cache: bool = True,
    ):
        self.config = config or VotingConfig.get_default_config()
        self.timeout = timeout_seconds
        self.aggregator = VotingAggregator(self.config)
        self.model_agents = self._create_model_agents()

        # P1 修复: LLM 投票结果缓存
        self.enable_cache = enable_cache
        self.vote_cache = VotingCache() if enable_cache else None

        # P1 (2026-05-27): 投票上下文丰富化辅助单例
        # - news_provider: USNewsProvider 实例 (按需初始化)
        # - market_data_router: MarketDataRouter 实例 (按需初始化, 用于取 bid/ask spread)
        # 任何一项初始化失败即置 None, 不影响主流程
        self._news_provider = None
        self._md_router = None
        
        # P2.4 (RFC-060): 辩论引擎已移除 — DebateEngine 此前创建后从未被调用
        # (run_debate 零引用; 见 debate_engine.py:479 自述 "辩论降级/结论持久化/中性降级全部不可达").
        # 且 bear_model=DeepSeek 与 bull=Qwen 同源 (中国大模型生态), 即使接通也只是假分歧.
        # 待能接入真正差异化模型 (Claude/GPT) 作 Bear 时, 再用 hermes 重新引入.
        # 注: auto_debate_triggered 标志保留 (voting_aggregator 仍计算分歧度 +
        #     decision_recorder.py:214 持久化供诊断). enable_debate 参数保留以兼容调用方.
        self.enable_debate = enable_debate
    
    def _create_model_agents(self) -> Dict[ModelRole, ModelAgent]:
        """创建模型 Agent（各角色使用专属差异化提示词）"""
        agents = {}
        
        for role, model_name in self.config.model_mapping.items():
            # 优先使用角色专属提示词，fallback 到通用提示词
            prompt = self._ROLE_PROMPTS.get(role.value, self._create_generic_prompt(role))
            agents[role] = ModelAgent(role.value, model_name, prompt)
        
        return agents
    
    def _create_generic_prompt(self, role: ModelRole) -> str:
        """通用提示词（兜底）"""
        description = self.config.model_descriptions.get(role, "综合分析")
        return f"""你是一个专业的股票分析师，专注于{description}。
评分 0-100：0-30 强烈看空 | 31-45 看空 | 46-55 中性 | 56-70 看多 | 71-100 强烈看多
【重要】返回纯 JSON，不要使用 ```json 代码块，不要有任何额外文本。
返回格式：{{"strength": <整数>, "confidence": <0.0-1.0>, "direction": "<LONG 或 SHORT>", "reasoning": "<30 字内>"}}"""
    
    # ------------------------------------------------------------------
    # Task #9 (2026-05-31): 三级降级入口
    #   Level 0: 多模型投票 (_run_full_voting)
    #   Level 1: 单模型降级 (_run_single_model_vote, primary only, 30s)
    #   Level 2: 纯技术信号   (_build_technical_only_result, conf ≤0.60)
    # ------------------------------------------------------------------
    def vote(self, symbol: str, ctx) -> Dict[str, Any]:
        """Convenience wrapper: accept (symbol, SymbolContext) from signal_stage.

        Converts SymbolContext dataclass → market_data dict, delegates to run_voting().
        Fixes: signal_stage.py calls voting_coordinator.vote(symbol, ctx) but only
        run_voting(market_data) existed.
        """
        import dataclasses
        if hasattr(ctx, '__dataclass_fields__'):
            market_data = dataclasses.asdict(ctx)
        elif isinstance(ctx, dict):
            market_data = ctx
        else:
            market_data = {}
        market_data.setdefault("symbol", symbol)
        market_data.setdefault("ticker", symbol)
        return self.run_voting(market_data)

    def run_voting(
        self,
        market_data: Dict[str, Any],
        external_votes: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """盘前/盘后 LLM 多模型投票 (含三级降级).

        降级链路:
        - Level 0: 正常多模型投票
        - Level 1: 多模型超时/异常 → primary 单模型 (30s)
        - Level 2: LLM 完全不可用 → 纯技术信号 (置信度封顶 0.60)
        """
        symbol = market_data.get("symbol") or market_data.get("ticker") or "UNKNOWN"

        # Level 0: 正常多模型投票
        try:
            result = self._run_full_voting(market_data, external_votes)
            if result is not None:
                result.setdefault("degradation_level", 0)
                return result
            logger.warning(
                f"[VotingCoordinator] {symbol} 多模型投票返回 None，进入降级"
            )
        except Exception as e:
            logger.warning(
                f"[VotingCoordinator] {symbol} 多模型投票失败：{e}，尝试单模型降级"
            )

        # Level 1: 单模型降级
        try:
            logger.info(f"[VotingCoordinator] {symbol} 降级到单模型投票")
            result = self._run_single_model_vote(market_data)
            if result is not None:
                result["degraded"] = True
                result["degradation_level"] = 1
                result["degradation_reason"] = "多模型投票超时或异常，降级为单模型"
                # 单模型置信度不超过 0.70
                if "final_confidence" in result:
                    result["final_confidence"] = min(
                        float(result["final_confidence"] or 0.5), 0.70
                    )
                return result
        except Exception as e:
            logger.warning(f"[VotingCoordinator] {symbol} 单模型投票也失败：{e}")

        # Level 2: 纯技术信号
        logger.warning(
            f"[VotingCoordinator] {symbol} LLM 完全不可用，降级为纯技术信号"
        )
        return self._build_technical_only_result(market_data)

    def _run_full_voting(
        self,
        market_data: Dict[str, Any],
        external_votes: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Level 0: 多模型投票 (原 run_voting 主体逻辑)。

        盘前/盘后 LLM 多模型投票。盘中请使用 app.pipeline.signal_pipeline._generate_technical_signal()。
        """
        symbol = market_data.get("symbol") or market_data.get("ticker") or "UNKNOWN"

        # P1 缓存检查
        if self.enable_cache and self.vote_cache:
            cached = self.vote_cache.get(symbol, market_data)
            if cached:
                logger.info(f"[投票] {symbol} 使用缓存结果 (节省 3 次 LLM API 调用)")
                # 更新时间戳但复用其他字段
                cached["timestamp"] = time.time()
                cached["from_cache"] = True
                return cached

        # P1 (2026-05-27): 丰富投票上下文 - 从 7 指标扩展到 15+ 维度
        # 新增维度: 个股新闻情绪、当前持仓、近 7 天胜率、bid-ask spread、最近 5 笔交易、盘前计划上下文
        # 任何一项失败都不影响投票主流程, _collect_scores_parallel 会自动消费 enriched 字段
        enriched_data = self._enrich_voting_context(market_data)

        scores = self._collect_scores_parallel(enriched_data)

        # P0 优化 (2026-04-17): 注入外部投票 (LLM 策略引擎)
        # P1 修复 (2026-04-17): ModelScore 使用 model_id= 而非 model=, direction 转字符串
        if external_votes:
            for ev in external_votes:
                model_name = ev.get("model", "external")
                direction_raw = ev.get("direction", 0)
                # direction: int 1 → "LONG", int -1 → "SHORT", int 0 → "NEUTRAL"
                if isinstance(direction_raw, int):
                    direction_str = "LONG" if direction_raw > 0 else ("SHORT" if direction_raw < 0 else "NEUTRAL")
                else:
                    direction_str = str(direction_raw).upper()

                score = ModelScore(
                    model_id=model_name,
                    strength=ev.get("strength", 50),
                    confidence=ev.get("confidence", 0.5),
                    direction=direction_str,
                    reasoning=ev.get("reasoning", ""),
                )
                scores[model_name] = score
                logger.info(
                    f"[投票] 注入外部投票 {model_name}: "
                    f"strength={score.strength}, confidence={score.confidence:.2f}, "
                    f"direction={score.direction}"
                )

        result = self.aggregator.aggregate(scores, enriched_data)

        # 添加元数据
        result["symbol"] = enriched_data.get("symbol") or enriched_data.get("ticker") or "UNKNOWN"
        result["timestamp"] = time.time()

        # 方案 E 优化：添加动态仓位建议
        result["position_suggestion"] = self.calculate_position_suggestion(result)

        # 方案 E 优化：添加市场状态评估
        result["market_regime"] = self.assess_market_regime(enriched_data)

        # P1 新增: 决策记录 — 打通断点1 (投票→数据库)
        self._record_decision_to_db(result, enriched_data, external_votes)

        # P1 修复: 写入缓存 (缓存 key 仍基于原始 market_data 的核心字段, 保持兼容)
        if self.enable_cache and self.vote_cache:
            self.vote_cache.put(symbol, result, market_data)

        return result

    # ------------------------------------------------------------------
    # P1 (2026-05-27): 投票上下文丰富化
    # ------------------------------------------------------------------

    def _enrich_voting_context(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        丰富投票上下文：从 7 指标扩展到 15+ 维度

        新增维度:
        - news_sentiment_score: 个股新闻情绪分 (-1.0 ~ +1.0)
        - position_info: 当前持仓状态 + 未实现 P&L
        - win_rate_7d: 最近 7 天同标的同方向胜率
        - spread_bps: bid-ask spread (流动性指标, 单位 bps)
        - recent_trades: 最近 5 笔同标的交易结果
        - battle_plan_context: 当日盘前计划中该标的的信息

        所有数据获取均 try/except 包裹，任何一项失败不影响投票继续。
        """
        enriched = market_data.copy()
        symbol = market_data.get("symbol") or market_data.get("ticker") or ""
        if not symbol:
            return enriched

        # 1. 个股新闻情绪分
        try:
            sentiment = None
            try:
                # 优先尝试模块级函数 (若未来提供)
                from app.data.news_provider import get_news_sentiment_score  # type: ignore
                sentiment = get_news_sentiment_score(symbol)
            except ImportError:
                # Fallback: 复用 USNewsProvider 单例 (含 LLM + 关键词规则 + 缓存)
                if self._news_provider is None:
                    from app.data.news_provider import USNewsProvider
                    self._news_provider = USNewsProvider()
                sentiment = self._news_provider.get_sentiment_score(symbol)
            if sentiment is not None:
                enriched["news_sentiment_score"] = sentiment
                # 同步写入 model_agent 实际读取的键名 (若上游未填充)
                if "news_sentiment" not in market_data:
                    enriched["news_sentiment"] = sentiment
        except Exception as e:
            logger.debug(f"[投票上下文] 新闻情绪获取失败 {symbol}: {e}")

        # 2. 当前持仓状态 + 未实现 P&L
        try:
            store = None
            try:
                from app.core.position_sync import get_position_store  # type: ignore
                store = get_position_store()
            except ImportError:
                # Fallback: 通过 USPositionSync 单例访问其内部 position_store
                from app.core.position_sync import get_us_position_sync
                sync = get_us_position_sync()
                store = getattr(sync, "position_store", None)

            if store is not None and hasattr(store, "get"):
                pos = store.get(symbol)
                if pos:
                    direction = getattr(pos, "direction", "")
                    direction_str = (
                        direction.value if hasattr(direction, "value") else str(direction)
                    )
                    holding_minutes = getattr(pos, "holding_minutes", None)
                    if holding_minutes is None:
                        # 从 entry_time 估算
                        try:
                            from datetime import datetime as _dt
                            entry_time = getattr(pos, "entry_time", None)
                            if entry_time:
                                holding_minutes = int(
                                    (_dt.now() - entry_time).total_seconds() / 60
                                )
                            else:
                                holding_minutes = 0
                        except Exception:
                            holding_minutes = 0
                    # 2026-06-10 红队修复 B2: 移除 unrealized_pnl_pct/entry_price/holding_minutes
                    # 防止 LLM 因浮亏/浮盈产生"继续持有"偏差 (行为经济学: 损失厌恶污染信号)
                    enriched["position_info"] = {
                        "held": True,
                        "direction": direction_str,
                    }
                else:
                    enriched["position_info"] = {"held": False}
        except Exception as e:
            logger.debug(f"[投票上下文] 持仓状态获取失败 {symbol}: {e}")

        # 3. 最近 7 天同标的同方向胜率 (使用 realized_pnl 列)
        try:
            import sqlite3
            from app.core.paths import get_trade_records_db
            direction = market_data.get("direction", "LONG")
            ticker = symbol.split(".")[-1] if "." in symbol else symbol
            conn = sqlite3.connect(str(get_trade_records_db()))
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS total,
                           SUM(CASE WHEN COALESCE(realized_pnl, 0) > 0 THEN 1 ELSE 0 END) AS wins
                    FROM trade_records
                    WHERE symbol LIKE ? AND direction = ?
                      AND exit_price > 0
                      AND trade_date >= date('now', '-7 days')
                    """,
                    (f"%{ticker}%", direction),
                ).fetchone()
            finally:
                conn.close()
            if row and row["total"] and row["total"] > 0:
                wins = row["wins"] or 0
                enriched["win_rate_7d"] = {
                    "rate": round(wins / row["total"], 3),
                    "trades": row["total"],
                }
        except Exception as e:
            logger.debug(f"[投票上下文] 7d 胜率获取失败 {symbol}: {e}")

        # 4. Bid-ask spread (流动性指标, 单位 bps)
        try:
            if self._md_router is None:
                from app.data.market_data_router import MarketDataRouter
                import os as _os
                self._md_router = MarketDataRouter(
                    twelvedata_api_key=_os.getenv("TWELVEDATA_API_KEY"),
                    finnhub_api_key=_os.getenv("FINNHUB_API_KEY"),
                    alphavantage_api_key=_os.getenv("ALPHAVANTAGE_API_KEY"),
                )
            quote = self._md_router.get_quote(symbol) if self._md_router else None
            if quote:
                bid = quote.get("bid", 0) or 0
                ask = quote.get("ask", 0) or 0
                mid = (bid + ask) / 2 if (bid and ask) else 0
                if mid > 0 and ask >= bid:
                    enriched["spread_bps"] = round((ask - bid) / mid * 10000, 1)
        except Exception as e:
            logger.debug(f"[投票上下文] spread 获取失败 {symbol}: {e}")

        # 5. 最近 5 笔同标的交易结果
        try:
            import sqlite3
            from app.core.paths import get_trade_records_db
            ticker = symbol.split(".")[-1] if "." in symbol else symbol
            conn = sqlite3.connect(str(get_trade_records_db()))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    SELECT direction, realized_pnl, entry_price, exit_price, trade_date
                    FROM trade_records
                    WHERE symbol LIKE ?
                    ORDER BY created_at DESC LIMIT 5
                    """,
                    (f"%{ticker}%",),
                ).fetchall()
            finally:
                conn.close()
            if rows:
                enriched["recent_trades"] = [
                    {
                        "direction": r["direction"],
                        "pnl": r["realized_pnl"] or 0,
                        "date": r["trade_date"],
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.debug(f"[投票上下文] 近 5 笔交易获取失败 {symbol}: {e}")

        # 6. 当日盘前计划上下文
        try:
            from app.us_pipeline.battle_plan_manager import USBattlePlanManager
            mgr = USBattlePlanManager()
            plan = mgr.get_plan() if hasattr(mgr, "get_plan") else None
            if plan and isinstance(plan, dict):
                ticker = symbol.split(".")[-1] if "." in symbol else symbol
                for target in plan.get("targets", []):
                    target_sym = target.get("symbol", "") or ""
                    if ticker and ticker in target_sym:
                        logic_str = target.get("logic", "") or ""
                        enriched["battle_plan_context"] = {
                            "direction": target.get("direction"),
                            "urgency": target.get("urgency"),
                            "entry_zone": target.get("entry_zone"),
                            "logic": logic_str[:100],
                        }
                        break
        except Exception as e:
            logger.debug(f"[投票上下文] 盘前计划获取失败 {symbol}: {e}")

        # 7. 历史交易上下文 (Task #33 - 长上下文利用)
        # 注入最近 N 笔同标的交易, 让 LLM 学习时间序列模式
        try:
            inject_history = True
            history_max_trades = 20
            try:
                from app.core.config_container import get_config
                cfg = get_config()
                inject_history = cfg.get_dotted("voting.inject_history", True)
                history_max_trades = int(
                    cfg.get_dotted("voting.history_max_trades", 20)
                )
            except Exception:
                pass

            if inject_history:
                import sqlite3
                from app.core.paths import get_trade_records_db
                ticker = symbol.split(".")[-1] if "." in symbol else symbol
                conn = sqlite3.connect(str(get_trade_records_db()))
                conn.row_factory = sqlite3.Row
                try:
                    rows = conn.execute(
                        """
                        SELECT direction, realized_pnl, entry_price, exit_price,
                               trade_date, entry_time, exit_time, signal_confidence
                        FROM trade_records
                        WHERE symbol LIKE ? AND exit_price > 0
                        ORDER BY created_at DESC LIMIT ?
                        """,
                        (f"%{ticker}%", history_max_trades),
                    ).fetchall()
                finally:
                    conn.close()

                if rows:
                    lines = []
                    for r in reversed(rows):  # 按时间正序
                        pnl = r["realized_pnl"] or 0
                        result_icon = "+" if pnl > 0 else "-"
                        pnl_pct = ""
                        entry_p = r["entry_price"]
                        exit_p = r["exit_price"]
                        if entry_p and entry_p > 0 and exit_p:
                            pct = (exit_p - entry_p) / entry_p * 100
                            if r["direction"] == "SHORT":
                                pct = -pct
                            pnl_pct = f"{pct:+.1f}%"

                        # 计算持有时长
                        holding = ""
                        if r["entry_time"] and r["exit_time"]:
                            try:
                                from datetime import datetime as _dt2
                                et = r["entry_time"]
                                xt = r["exit_time"]
                                entry_t = (
                                    _dt2.fromisoformat(et.replace("Z", "+00:00"))
                                    if isinstance(et, str) else et
                                )
                                exit_t = (
                                    _dt2.fromisoformat(xt.replace("Z", "+00:00"))
                                    if isinstance(xt, str) else xt
                                )
                                hours = (exit_t - entry_t).total_seconds() / 3600
                                holding = (
                                    f"{hours:.0f}h" if hours >= 1
                                    else f"{hours*60:.0f}m"
                                )
                            except Exception:
                                pass

                        sc = r["signal_confidence"]
                        conf = f"c={sc:.1f}" if sc else ""
                        line = (
                            f"{r['trade_date']} {r['direction']} "
                            f"{result_icon}${abs(pnl):.0f}({pnl_pct}) {holding} {conf}"
                        )
                        lines.append(line.strip())

                    # 控制总长度 (约 500 tokens ≈ 2000 字符)
                    historical_text = "\n".join(lines)
                    if len(historical_text) > 2000:
                        historical_text = historical_text[:2000] + "..."

                    enriched["historical_trades_text"] = historical_text
                    enriched["historical_trade_count"] = len(rows)

                    # 汇总统计
                    wins = sum(1 for r in rows if (r["realized_pnl"] or 0) > 0)
                    enriched["historical_win_rate"] = (
                        round(wins / len(rows), 3) if rows else 0
                    )
        except Exception as e:
            logger.debug(f"[投票上下文] 历史交易上下文获取失败 {symbol}: {e}")

        return enriched

    def _record_decision_to_db(
        self,
        result: Dict[str, Any],
        market_data: Dict[str, Any],
        external_votes: Optional[List[Dict[str, Any]]] = None,
    ):
        """将投票决策写入数据库 (断点1)"""
        try:
            recorder = get_decision_recorder()
            symbol = result.get("symbol") or market_data.get("symbol") or market_data.get("ticker") or "UNKNOWN"

            # P2.6 修复 (RFC-060): LLM投票信号此前漏挂 strategy_id
            # 根因: voting_aggregator.aggregate() 不产出 strategy_id, decision_recorder:285
            # 读 voting_result.get("strategy_id") 永远为 None → 338笔盘后LLM信号无标签,
            # 把整体 IC 拖成负的假象 (实测: 无标签SHORT 40.8%WR -0.42%, 其余57%WR +0.5%).
            # 补挂 voting_llm 标签 (与 signal_pipeline:2021 一致), 使 IC监控/策略归因/strategy_health
            # 能识别并针对该劣质信号源动作 (P1.5门禁 + ic_hard_blocking 现可生效).
            if not result.get("strategy_id"):
                result["strategy_id"] = "voting_llm"

            # 准备 LLM 投票信息
            voting_llm = None
            if external_votes:
                for ev in external_votes:
                    if ev.get("model") == "llm_strategy":
                        voting_llm = {
                            "strength": ev.get("strength", 50),
                            "weight": self.config.LLM_EXTERNAL_VOTE_WEIGHT,
                        }
                        break

            # 转换 model_scores 为 list of dict
            model_scores = result.get("model_scores", [])
            # Ensure direction is string (handle int from LLM)
            for ms in model_scores:
                d = ms.get("direction", "NEUTRAL")
                if isinstance(d, int):
                    ms["direction"] = "LONG" if d > 0 else "SHORT" if d < 0 else "NEUTRAL"

            decision_id = recorder.record_decision(
                symbol=symbol,
                voting_result={**result, "model_scores": model_scores},
                market_data={
                    "regime": (
                        result.get("market_regime")
                        if isinstance(result.get("market_regime"), str)
                        else (result.get("market_regime") or {}).get("regime")
                    ),
                    "vix": market_data.get("vix"),
                },
                voting_llm=voting_llm,
            )
            # 将 decision_id 写回 result 供下游使用
            result["decision_id"] = decision_id
            logger.info(f"[DecisionRecorder] 决策已记录: {decision_id}")
        except Exception as e:
            logger.warning(f"[DecisionRecorder] 记录决策失败 (不影响主流程): {e}")

        # Task #26 (2026-05-27): 记录投票预测到准确率追踪器
        try:
            from app.analysis.voting_accuracy_tracker import get_voting_accuracy_tracker
            tracker = get_voting_accuracy_tracker()

            # 构建模型投票明细
            model_votes_detail: Dict[str, Any] = {}
            for ms in result.get("model_scores", []) or []:
                if not isinstance(ms, dict):
                    continue
                mid = ms.get("model_id") or ms.get("model") or "unknown"
                d = ms.get("direction", "NEUTRAL")
                if isinstance(d, int):
                    d = "LONG" if d > 0 else "SHORT" if d < 0 else "NEUTRAL"
                model_votes_detail[mid] = {
                    "direction": d,
                    "strength": ms.get("strength", 50),
                    "confidence": ms.get("confidence", 0.5),
                }

            tracker.record_prediction(
                decision_id=result.get("decision_id") or f"DEC-{int(time.time())}",
                symbol=result.get("symbol") or symbol or "",
                predicted_direction=result.get("direction", "NEUTRAL"),
                predicted_strength=int(result.get("final_strength", 50) or 50),
                predicted_confidence=float(
                    result.get("final_confidence")
                    or result.get("consensus_level")
                    or 0.5
                ),
                model_votes=model_votes_detail,
            )
        except Exception as _vae:
            logger.debug(f"[投票准确率] 记录预测失败 (不影响主流程): {_vae}")
    
    def calculate_position_suggestion(self, voting_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        根据投票结果动态计算仓位建议 - 方案 E 新增
        
        Returns:
            {
                "suggested_position": float,  # 建议仓位 (0.0-1.0)
                "confidence_level": str,      # 信心等级
                "reasoning": str,             # 仓位建议理由
            }
        """
        confidence = voting_result.get("final_confidence", 0.5)
        strength = voting_result.get("final_strength", 50)
        consensus = voting_result.get("consensus_level", 0.5)
        
        # 基础仓位
        base_position = self.config.POSITION_BASE
        
        # 根据信心等级调整仓位
        if confidence >= 0.70:
            position = self.config.POSITION_HIGH_CONFIDENCE  # 15%
            confidence_level = "HIGH"
        elif confidence >= 0.60:
            position = self.config.POSITION_MEDIUM_CONFIDENCE  # 12%
            confidence_level = "MEDIUM"
        elif confidence >= self.config.CONFIDENCE_THRESHOLD:  # 0.55
            position = self.config.POSITION_LOW_CONFIDENCE  # 8%
            confidence_level = "LOW"
        else:
            position = self.config.POSITION_MINIMUM  # 5%
            confidence_level = "MINIMAL"
        
        # 根据一致性调整
        if consensus < 0.55:
            position *= 0.8  # 一致性低，降低仓位
        elif consensus > 0.80:
            position *= 1.1  # 一致性高，小幅提升仓位
        
        # 根据信号强度调整 (极端信号降低仓位，可能是反转信号)
        if strength < 25 or strength > 75:
            position *= 0.9  # 极端信号，谨慎
        
        # 限制仓位范围
        position = max(0.0, min(0.20, position))  # 最大 20%
        
        return {
            "suggested_position": round(position, 3),
            "confidence_level": confidence_level,
            "reasoning": f"信心{confidence:.2f}, 一致性{consensus:.2f}, 强度{strength}",
        }
    
    def assess_market_regime(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        评估市场状态 - 方案 E 新增
        
        Returns:
            {
                "regime": str,           # 市场状态
                "vix_level": str,        # VIX 水平
                "suggested_strategy": str,  # 建议策略
            }
        """
        vix = market_data.get("vix", 20.0)
        spy_ma200_ratio = market_data.get("spy_ma200_ratio", 1.0)
        
        # VIX 水平评估
        if vix < self.config.VIX_LOW_THRESHOLD:  # <20
            vix_level = "LOW"
            regime = "LOW_VOLATILITY"
            suggested_strategy = "正常策略，可适度提升仓位"
        elif vix < self.config.VIX_MID_THRESHOLD:  # 20-25
            vix_level = "NORMAL"
            regime = "NORMAL"
            suggested_strategy = "标准策略"
        elif vix < self.config.VIX_HIGH_THRESHOLD:  # 25-30
            vix_level = "HIGH"
            regime = "HIGH_VOLATILITY"
            suggested_strategy = "降低仓位，提高风控标准"
        else:  # >30
            vix_level = "EXTREME"
            regime = "EXTREME_VOLATILITY"
            suggested_strategy = "最小仓位，仅高确定性机会"
        
        # 大盘趋势评估
        if spy_ma200_ratio > 1.05:
            trend = "BULL"
        elif spy_ma200_ratio < 0.95:
            trend = "BEAR"
        else:
            trend = "SIDEWAYS"
        
        return {
            "regime": regime,
            "vix_level": vix_level,
            "vix": vix,
            "trend": trend,
            "spy_ma200_ratio": spy_ma200_ratio,
            "suggested_strategy": suggested_strategy,
        }

    # ------------------------------------------------------------------
    # Task #9 (2026-05-31): 单模型降级 + 纯技术信号 fallback
    # ------------------------------------------------------------------
    def _get_primary_model_role(self) -> ModelRole:
        """返回 primary 模型角色 (默认 Qwen, 不存在则取首个)。"""
        if ModelRole.QWEN in self.model_agents:
            return ModelRole.QWEN
        return next(iter(self.model_agents.keys()))

    def _run_single_model_vote(
        self, market_data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Level 1: 单模型降级投票。

        仅使用 primary 模型 (默认 Qwen)，超时 30s (较多模型 90s 更短)。
        任何异常/超时返回 None，走 Level 2。
        """
        primary_role = self._get_primary_model_role()
        primary_agent = self.model_agents.get(primary_role)
        if primary_agent is None:
            return None

        # 单模型降级也需要丰富上下文 (复用同一函数，失败不影响主流程)
        try:
            enriched = self._enrich_voting_context(market_data)
        except Exception as e:
            logger.debug(f"[VotingCoordinator] 单模型降级丰富上下文失败: {e}")
            enriched = market_data

        SINGLE_TIMEOUT = 30.0
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(primary_agent.generate_score, enriched)
            try:
                score = future.result(timeout=SINGLE_TIMEOUT)
            except FuturesTimeoutError:
                logger.warning(
                    f"[VotingCoordinator] 单模型 {primary_role.value} 也超时(30s)"
                )
                return None
            except Exception as e:
                logger.warning(
                    f"[VotingCoordinator] 单模型 {primary_role.value} 异常: {e}"
                )
                return None

        symbol = enriched.get("symbol") or enriched.get("ticker") or "UNKNOWN"
        is_neutral = 45 <= int(score.strength) <= 55
        result = {
            "symbol": symbol,
            "direction": score.direction,
            "final_strength": int(score.strength),
            "final_confidence": float(score.confidence),
            "consensus_level": float(score.confidence),
            "std_dev": 0.0,
            "model_scores": [
                {
                    "model_id": score.model_id,
                    "strength": int(score.strength),
                    "confidence": float(score.confidence),
                    "direction": score.direction,
                    "reasoning": score.reasoning,
                }
            ],
            "is_neutral": is_neutral,
            "timestamp": time.time(),
        }
        try:
            result["position_suggestion"] = self.calculate_position_suggestion(result)
        except Exception:
            result["position_suggestion"] = {}
        try:
            result["market_regime"] = self.assess_market_regime(enriched)
        except Exception:
            result["market_regime"] = {"regime": "NORMAL"}
        return result

    def _build_technical_only_result(
        self, market_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Level 2: 纯技术信号。置信度严格不超过 0.60。"""
        symbol = market_data.get("symbol") or market_data.get("ticker") or "UNKNOWN"
        score = self._generate_technical_fallback(market_data)
        capped_conf = min(float(score.confidence), 0.60)
        result = {
            "symbol": symbol,
            "direction": score.direction,
            "final_strength": int(score.strength),
            "final_confidence": capped_conf,
            "consensus_level": capped_conf,
            "std_dev": 0.0,
            "model_scores": [
                {
                    "model_id": "technical_fallback",
                    "strength": int(score.strength),
                    "confidence": capped_conf,
                    "direction": score.direction,
                    "reasoning": score.reasoning,
                }
            ],
            "is_neutral": (score.direction == "NEUTRAL"),
            "degraded": True,
            "degradation_level": 2,
            "degradation_reason": "LLM 完全不可用，使用纯技术信号",
            "reasoning": "技术面信号（无 LLM 增强）",
            "timestamp": time.time(),
        }
        try:
            result["position_suggestion"] = self.calculate_position_suggestion(result)
        except Exception:
            result["position_suggestion"] = {}
        try:
            result["market_regime"] = self.assess_market_regime(market_data)
        except Exception:
            result["market_regime"] = {"regime": "NORMAL"}
        return result

    def _collect_scores_parallel(
        self,
        market_data: Dict[str, Any],
    ) -> List[ModelScore]:
        """并行收集所有模型评分

        P0 修复 (2026-04-17):
        - 增加 LLM 调用失败降级策略
        - 当超过半数模型失败时，触发降级模式
        - 降级模式：使用纯技术指标评分 + 发送钉钉告警

        P1 修复 (2026-05-27):
        - 每个失败模型生成 degraded_vote=True 的技术 fallback 评分
        - 记录失败原因 (timeout/api_error/rate_limit)
        - 降级监控指标记录到 EventStore
        """
        scores = []
        failed_models = []  # (model_name, failure_reason)
        total_models = len(self.model_agents)

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(agent.generate_score, market_data): role
                for role, agent in self.model_agents.items()
            }

            for future in futures:
                role = futures[future]
                try:
                    score = future.result(timeout=self.timeout)
                    scores.append(score)
                except FuturesTimeoutError:
                    failed_models.append((role.value, "timeout"))
                    logger.error(f"[投票] {role.value} 模型评分超时")
                except Exception as e:
                    # 区分 rate_limit 和其他 API 错误
                    err_str = str(e).lower()
                    if "rate" in err_str or "429" in err_str or "limit" in err_str:
                        reason = "rate_limit"
                    else:
                        reason = "api_error"
                    failed_models.append((role.value, reason))
                    logger.error(f"[投票] {role.value} 模型评分失败：{e}")

        # 为每个失败模型生成降级投票 (degraded_vote=True)
        for model_name, failure_reason in failed_models:
            degraded_score = self._generate_technical_fallback(market_data)
            degraded_score.model_id = model_name
            degraded_score.degraded_vote = True
            degraded_score.failure_reason = failure_reason
            degraded_score.reasoning = (
                f"[降级] {failure_reason} | {degraded_score.reasoning}"
            )
            scores.append(degraded_score)

        # 降级监控: 记录到 EventStore
        if failed_models:
            self._record_degradation_event(
                failed_models, total_models, market_data
            )

        # P0 降级告警: 超过半数模型失败 → 发送钉钉告警
        if len(failed_models) > total_models / 2:
            logger.critical(
                f"[投票·降级] {len(failed_models)}/{total_models} 模型失败 "
                f"({', '.join(m for m, _ in failed_models)})，切换纯技术模式"
            )
            self._send_llm_failure_alert(
                [m for m, _ in failed_models],
                market_data.get("symbol") or market_data.get("ticker") or "UNKNOWN",
            )

        return scores

    def _generate_technical_fallback(
        self, market_data: Dict[str, Any]
    ) -> ModelScore:
        """纯技术指标多维评分（盘中 TRADING phase 专用）

        修复 2026-04-25: 原硬编码 confidence=0.3 与门禁 0.5 矛盾，
        导致所有盘中信号 100% 被拦截。改为多维评分 + 动态置信度。
        """
        symbol = market_data.get("symbol") or market_data.get("ticker") or "UNKNOWN"
        rsi = market_data.get("rsi", 50)
        macd_signal = market_data.get("macd_signal", 0)
        price = market_data.get("price", 0)
        ma20 = market_data.get("ma20", 0)
        ma50 = market_data.get("ma50", 0)
        volume_ratio = market_data.get("volume_ratio", 1.0)

        # ── 多维技术评分 ──
        bull_signals = 0
        bear_signals = 0
        total_checks = 0

        # 1. RSI 评分
        if rsi < 30:
            bull_signals += 2; total_checks += 2
        elif rsi < 40:
            bull_signals += 1; total_checks += 1
        elif rsi > 70:
            bear_signals += 2; total_checks += 2
        elif rsi > 60:
            bear_signals += 1; total_checks += 1
        else:
            total_checks += 1

        # 2. MACD 信号线
        if macd_signal > 0:
            bull_signals += 1; total_checks += 1
        elif macd_signal < 0:
            bear_signals += 1; total_checks += 1
        else:
            total_checks += 1

        # 3. 均线趋势
        if ma20 > 0 and ma50 > 0:
            if price > ma20 > ma50:
                bull_signals += 1; total_checks += 1
            elif price < ma20 < ma50:
                bear_signals += 1; total_checks += 1
            else:
                total_checks += 1
        else:
            total_checks += 1

        # 4. 量比（放量增强信号可信度）
        if volume_ratio > 1.5:
            total_checks += 1  # 仅加分项，不改变方向

        # 计算强度
        if total_checks > 0:
            bull_ratio = bull_signals / total_checks
            bear_ratio = bear_signals / total_checks
        else:
            bull_ratio = bear_ratio = 0

        if bull_ratio > bear_ratio:
            strength = 50 + int(bull_ratio * 40)  # 51-90
            direction = "LONG"
        elif bear_ratio > bull_ratio:
            strength = 50 - int(bear_ratio * 40)  # 10-49
            direction = "SHORT"
        else:
            strength = 50
            direction = "NEUTRAL"

        # ── 动态置信度 (0.35 ~ 0.85) ──
        signal_agreement = abs(bull_ratio - bear_ratio)  # 0=无共识, 1=完全一致
        confidence = 0.35 + signal_agreement * 0.45  # 基础范围 0.35-0.80

        # 量比增强
        if volume_ratio > 2.0:
            confidence = min(confidence * 1.15, 0.85)

        return ModelScore(
            model_id="technical_fallback",
            strength=max(10, min(90, strength)),
            confidence=round(confidence, 2),
            direction=direction,
            reasoning=f"技术面评分: bull={bull_ratio:.2f} bear={bear_ratio:.2f} "
                      f"RSI={rsi:.0f} MACD={macd_signal:.2f} VR={volume_ratio:.1f}",
        )

    def _send_llm_failure_alert(self, failed_models: List[str], symbol: str):
        """发送 LLM 失败告警"""
        try:
            from app.notifier.dingtalk import DingTalkNotifier
            msg = (
                f"📌 标的: {symbol}\n"
                f"❌ 失败模型: {', '.join(failed_models)}\n"
                f"🔧 已切换: 纯技术指标降级模式\n"
                f"⚠️ 降级投票权重已降至 30%，建议人工审查"
            )
            notifier = DingTalkNotifier()
            notifier.notify_system_alert("LLM 降级告警", msg, "CRITICAL")
        except Exception as e:
            logger.error(f"[投票] 发送 LLM 失败告警异常: {e}")

    def _record_degradation_event(
        self,
        failed_models: List[tuple],
        total_models: int,
        market_data: Dict[str, Any],
    ):
        """记录降级事件到 EventStore 供后续统计月度降级触发率"""
        try:
            from app.core.events import EventStore
            from app.core.models import TradeEvent, AlertLevel
            from app.core.di_container import get_di_container
            import uuid as _uuid
            from datetime import datetime as _dt

            symbol = market_data.get("symbol") or market_data.get("ticker") or "UNKNOWN"
            degraded_ratio = len(failed_models) / total_models if total_models > 0 else 0

            event_data = {
                "failed_models": [
                    {"model": m, "reason": r} for m, r in failed_models
                ],
                "total_models": total_models,
                "degraded_count": len(failed_models),
                "degraded_ratio": round(degraded_ratio, 2),
                "symbol": symbol,
            }

            event = TradeEvent(
                event_id=str(_uuid.uuid4()),
                event_type="llm_degradation",
                timestamp=_dt.now(),
                data=event_data,
                source="voting_coordinator",
                severity=AlertLevel.WARNING if degraded_ratio <= 0.5 else AlertLevel.CRITICAL,
            )

            container = get_di_container()
            store = container.event_store()
            store.append(event)

            logger.info(
                f"[降级监控] 已记录降级事件: {len(failed_models)}/{total_models} "
                f"models failed for {symbol} | "
                f"reasons: {', '.join(r for _, r in failed_models)}"
            )
        except Exception as e:
            logger.warning(f"[降级监控] 记录降级事件失败 (不影响主流程): {e}")
