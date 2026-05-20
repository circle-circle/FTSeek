"""
FTSeek-US LLM 二阶推理策略引擎

核心功能:
1. 新闻聚合 → 一阶情感 + 二阶传导推理
2. 结合夜间行情数据 (VIX, 盘前异动, 板块动量)
3. 生成具体下单策略 (方向/仓位/止盈止损/时效)
4. 自动通过风控过滤 → 执行层

数据流:
  新闻(RSSHub+Finhub) → LLM推理 → OrderStrategy → 风控 → 执行

作者: FTSeek Team
日期: 2026-04-16
"""

import os
import re
import json
import queue
import logging
import time
import uuid
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from pathlib import Path

import requests
from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parent.parent.parent / ".env.us"
if _env_path.exists():
    load_dotenv(_env_path)

logger = logging.getLogger("ftseek.llm_strategy")


# ============================================================
#  数据结构 & Prompt 定义
# ============================================================

from app.llm.prompts.strategy import SYSTEM_PROMPT, LLM_REASONING_PROMPT, ANALYSIS_SYSTEM_PROMPT

USER_PROMPT_TEMPLATE = """
请基于以下事件包，输出严格 JSON。

【事件包】
标题：{headline}
正文：{body}
来源：{source}
发布时间：{publish_time}
涉及标的：{symbols}
相关行业：{industries}
市场环境：{market_regime}
美股收盘情况：{us_close}
ADR 表现：{adr_effect}
近期价格行为：{price_action}
近期成交量/波动率：{volume_volatility}
历史相似事件：{historical_cases}
相关新闻摘要：{related_news}

请按 SYSTEM_PROMPT 的要求输出。
"""

@dataclass
class OrderStrategy:
    """生成的下单策略 (支持新版 Prompt 输出)"""
    strategy_id: str = field(default_factory=lambda: f"STRAT-{uuid.uuid4().hex[:10].upper()}")
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    # 策略核心
    symbol: str = ""                    # US.AAPL
    direction: str = ""                 # LONG / SHORT / OPTIONS_PUT / OPTIONS_CALL
    confidence: float = 0.0             # 0-1
    reasoning: str = ""                 # LLM 推理摘要 (对应 second_order_insight)
    
    # 新增: 业务规则校验字段 (Phase 2)
    pricing_status: str = "unknown"     # not_priced_in / partially_priced_in / fully_priced_in
    tradeability: str = "watch_only"    # tradeable / watch_only / no_trade
    event_quality: str = "medium"       # high / medium / low
    logic_chain: List[str] = field(default_factory=list)
    invalidating_factors: List[str] = field(default_factory=list)
    risk_path: str = ""
    focus_symbols: List[str] = field(default_factory=list)

    # 入场参数
    entry_price_target: float = 0.0
    entry_type: str = "market"          # market / limit
    max_position_pct: float = 0.05      # 仓位占总资产比例 (对应 position_sizing)
    
    # 退出参数
    take_profit_pct: float = 0.15       # 止盈百分比
    stop_loss_pct: float = 0.25         # 止损百分比 (对应 stop_loss)
    time_horizon_hours: int = 72        # 持有期限（小时）
    
    # 触发条件
    trigger_condition: str = "immediate"  # immediate / on_pullback / on_breakout
    expiry_time: str = ""               # ISO 时间
    
    # 关联信息
    related_event: str = ""             # 关联的新闻/事件摘要
    impact_chain: List[str] = field(default_factory=list)  # 事件传导链
    source_news_ids: List[str] = field(default_factory=list)
    
    # 状态
    status: str = "pending"             # pending / validated / executing / executed / rejected / expired
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    def to_vote_dict(self) -> Dict[str, Any]:
        """转换为投票系统兼容格式 (用于注入共识引擎)"""
        # 方向映射: LONG → +1, SHORT/OPTIONS_PUT → -1
        if self.direction in ("LONG", "OPTIONS_CALL"):
            vote_direction = 1
        elif self.direction in ("SHORT", "OPTIONS_PUT"):
            vote_direction = -1
        else:
            vote_direction = 0
        
        return {
            "model": "llm_strategy",
            "direction": vote_direction,
            "strength": int(self.confidence * 100),  # 0-100 范围
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "metadata": {
                "strategy_id": self.strategy_id,
                "related_event": self.related_event,
                "impact_chain": self.impact_chain,
                "trigger_condition": self.trigger_condition,
                "time_horizon_hours": self.time_horizon_hours,
            }
        }


@dataclass
class EventImpact:
    """事件影响评估 (LLM 推理输出)"""
    event_category: str = ""            # 个股 / 行业 / 宏观 / 地缘 / 政策
    direct_impact: str = ""             # 直接影响描述
    second_order_impact: str = ""       # 二阶传导描述
    affected_symbols: Dict[str, float] = field(default_factory=dict)  # {symbol: impact_score}
    impact_chain: List[str] = field(default_factory=list)
    strategy_suggestions: List[Dict] = field(default_factory=list)
    confidence: float = 0.0


# ============================================================
#  LLM 推理提示词
# ============================================================


# ============================================================
#  人类可读深度分析 System Prompt (用于盘前推送)
# ============================================================
# 与 SYSTEM_PROMPT 的区别: 不强制 JSON 输出，用于生成人类可读的深度分析报告



# ============================================================
#  LLM 策略引擎
# ============================================================

class LLMStrategyEngine:
    """LLM 二阶推理策略引擎"""
    
    def __init__(self, config_path: str = None):
        # P1 修复 (2026-04-17): 优先从 unified_config.yaml 读取，环境变量作为降级备份
        try:
            from app.core.config_container import get_config
            cfg = get_config(config_path).get('llm.strategy_engine', {})
        except Exception:
            cfg = {}
            
        self.enabled = cfg.get('enabled', False)
        # 默认使用 qwen3.6-flash (二阶推理专用)
        self.model = cfg.get('model', os.getenv("BAILIAN_MODEL_STRATEGY", "qwen3.6-flash"))
        self.bailian_base_url = cfg.get('base_url', os.getenv("BAILIAN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"))
        
        # 读取 API Key: 优先 config -> 其次环境变量
        api_key_from_cfg = cfg.get('api_key', '')
        api_key_env_name = cfg.get('api_key_env', 'BAILIAN_API_KEY')
        self.bailian_api_key = api_key_from_cfg or os.getenv(api_key_env_name, "")
        
        self.enabled = self.enabled and bool(self.bailian_api_key)
        
        # 策略历史 (用于冷却/去重)
        self._recent_strategies: List[OrderStrategy] = []
        self._strategy_cooldown: Dict[str, datetime] = {}  # {symbol_direction: last_time}
        
        # 2026-04-23 新增: 即时触发队列 (Event-Triggered Scan)
        self._urgent_queue: queue.Queue = queue.Queue(maxsize=50)
        self._scan_event: threading.Event = threading.Event()

        logger.info(f"[LLM策略引擎] 初始化 | 模型={self.model} | 启用={self.enabled} | 来源=unified_config")
    
    def analyze_and_generate(
        self,
        news_items: List[Dict],
        market_state: Dict[str, Any],
        watchlist: List[str] = None,
        positions: List[Dict] = None,
    ) -> List[OrderStrategy]:
        """
        核心方法: 新闻 + 行情 → LLM 二阶推理 → 策略生成
        
        Args:
            news_items: 新闻列表 (from RSSHub + Finnhub)
            market_state: {vix, spy_trend, pre_market_changes, sector_momentum, us_close, adr_effect}
            watchlist: 观察名单
            positions: 当前持仓
        """
        if not self.enabled:
            logger.warning("[LLM策略引擎] 未配置 API Key, 跳过推理")
            return []
        
        if not news_items:
            logger.debug("[LLM策略引擎] 无新闻输入, 跳过推理")
            return []
        
        # 构建用户 Prompt (使用新模板适配二阶推理)
        # 我们取最相关的前几条新闻进行综合分析，或针对单条新闻生成策略
        # 这里为了保持系统稳定，我们将新闻聚合为 Context 传入
        context_text = self._build_user_prompt_context(news_items, market_state, watchlist, positions)
        
        # 调用 LLM
        try:
            response = self._call_llm(context_text)
            if not response:
                return []
            
            # 解析输出
            raw_strategies = self._parse_response(response, news_items)
            
            # P2 新增: 校验输出 (JSON Schema + 业务规则)
            validated_strategies = [self._validate_strategy_output(s) for s in raw_strategies]
            
            # 过滤掉被强制为 NO_TRADE 的策略
            strategies = [s for s in validated_strategies if s.direction != "NO_TRADE"]
            
            # 应用冷却/去重
            strategies = self._apply_cooldown(strategies)
            
            logger.info(f"[LLM策略引擎] 最终生成 {len(strategies)} 条可执行策略 (过滤前 {len(raw_strategies)})")
            return strategies
            
        except Exception as e:
            logger.error(f"[LLM策略引擎] 推理失败: {e}")
            return []

    def _build_user_prompt_context(
        self, 
        news_items: List[Dict], 
        market_state: Dict[str, Any], 
        watchlist: List[str], 
        positions: List[Dict]
    ) -> str:
        """构建符合 USER_PROMPT_TEMPLATE 格式的上下文"""
        # 聚合新闻或选取最重要的
        # 这里我们构造一个包含多条新闻的上下文，适配模板
        news_section = ""
        for i, n in enumerate(news_items[:5]): # 最多分析 5 条
            headline = n.get('title', n.get('headline', ''))
            body = n.get('summary', n.get('content', ''))
            source = n.get('source', 'Unknown')
            pub_time = n.get('published_at', 'Unknown')
            symbols = n.get('symbols', '')
            
            # 尝试从市场状态获取行业信息
            industries = n.get('industry', 'N/A') 
            
            news_section += f"""
--- 新闻 {i+1} ---
标题：{headline}
正文：{body}
来源：{source}
发布时间：{pub_time}
涉及标的：{symbols}
相关行业：{industries}
"""
        if not news_section:
            news_section = "无详细新闻数据。"

        # 提取市场状态字段
        us_close = market_state.get('us_close_summary', market_state.get('spy_trend', 'N/A'))
        adr_effect = market_state.get('adr_effect', 'N/A')
        price_action = market_state.get('price_action', 'N/A')
        volume_vol = market_state.get('volume_volatility', 'N/A')
        
        return USER_PROMPT_TEMPLATE.format(
            headline="多条新闻聚合分析" if len(news_items) > 1 else (news_items[0].get('title', '') if news_items else ""),
            body=news_section,
            source="Multi-Source",
            publish_time=datetime.now().isoformat(),
            symbols=", ".join([n.get('symbols', '') for n in news_items[:3]]),
            industries="Multi-Sector",
            market_regime=market_state.get('livermore_regime', {}).get('regime_label', 'Unknown'),
            us_close=us_close,
            adr_effect=adr_effect,
            price_action=price_action,
            volume_volatility=volume_vol,
            historical_cases="N/A",
            related_news="See Context Above"
        )

    def _validate_strategy_output(self, strategy: OrderStrategy) -> OrderStrategy:
        """
        P2 新增: 校验 LLM 输出
        业务规则:
        1. confidence < 0.6 -> 强制 NO_TRADE
        2. tradeability == 'no_trade' -> 强制 NO_TRADE
        3. pricing_status == 'fully_priced_in' -> 强制 NO_TRADE (保守策略)
        4. focus_symbols 为空 -> 丢弃
        """
        # 这里我们将校验逻辑嵌入到解析阶段，或者在这里修正
        # 为了安全，直接修改 strategy 对象
        
        if not strategy:
            return None

        # 1. 检查置信度
        if strategy.confidence < 0.60:
            logger.warning(f"📉 [校验] 策略 {strategy.symbol} 置信度过低 ({strategy.confidence:.2f}) -> 拒绝")
            return None # 返回 None 以便后续过滤

        # 2. 检查交易可行性
        if hasattr(strategy, 'tradeability') and strategy.tradeability == 'no_trade':
            logger.warning(f"🛑 [校验] 策略 {strategy.symbol} 标记为不可交易 -> 拒绝")
            return None
            
        # 3. 检查定价状态
        if hasattr(strategy, 'pricing_status') and strategy.pricing_status == 'fully_priced_in':
            logger.warning(f"⚠️ [校验] 策略 {strategy.symbol} 已充分定价 -> 拒绝")
            return None

        # 4. 检查标的
        if not strategy.symbol:
            logger.warning(f"❌ [校验] 策略无明确标的 -> 丢弃")
            return None

        return strategy
    
    def _format_news(self, news_items: List[Dict]) -> str:
        """格式化新闻列表为文本 (P1 优化: 注入情感分数)"""
        lines = []
        for i, n in enumerate(news_items[:15]):  # 最多15条
            title = n.get('title', n.get('headline', ''))
            summary = n.get('summary', n.get('content', ''))[:200]
            source = n.get('source_name', n.get('source', ''))
            time_str = n.get('published_at', n.get('fetched_at', ''))
            symbols = n.get('symbols', '')
            
            # P1 优化 (2026-04-17): 注入量化情感分数
            sentiment_score = n.get('qwen_sentiment', n.get('sentiment_score', None))
            sentiment_str = ""
            if sentiment_score is not None:
                if sentiment_score > 0.3:
                    sentiment_label = "正向"
                elif sentiment_score < -0.3:
                    sentiment_label = "负向"
                else:
                    sentiment_label = "中性"
                sentiment_str = f" | 情感: {sentiment_label} ({sentiment_score:+.2f})"
            
            lines.append(f"{i+1}. [{source}]{sentiment_str} {title}")
            if summary:
                lines.append(f"   摘要: {summary}")
            if symbols:
                lines.append(f"   关联: {symbols}")
            lines.append(f"   时间: {time_str}")
            lines.append("")
        
        return "\n".join(lines) if lines else "无新闻数据"
    
    def _call_llm(self, prompt: str) -> Optional[str]:
        """调用 Qwen/Bailian API (带重试) — 默认 JSON 模式"""
        return self._call_llm_api(prompt, json_mode=True)

    def _call_llm_analysis(self, prompt: str, system_prompt: str = None, timeout: int = 120) -> Optional[str]:
        """
        调用 LLM 进行深度分析 (人类可读文本模式)
        
        与 _call_llm 的区别:
        - 不使用 response_format json_object
        - 可使用自定义 system prompt
        - 更高超时 (默认 120s，适合深度推理)
        - max_tokens 更大 (4000)
        
        Args:
            prompt: 用户 prompt
            system_prompt: 自定义系统提示词，None 则使用 ANALYSIS_SYSTEM_PROMPT
            timeout: 超时秒数
        
        Returns:
            LLM 返回的文本内容
        """
        return self._call_llm_api(prompt, json_mode=False, system_prompt=system_prompt, timeout=timeout)

    def _call_llm_api(
        self, 
        prompt: str, 
        json_mode: bool = True, 
        system_prompt: str = None,
        timeout: int = 90
    ) -> Optional[str]:
        """
        底层 LLM API 调用
        
        Args:
            prompt: 用户 prompt
            json_mode: True=强制 JSON 输出, False=自由文本输出
            system_prompt: 自定义系统提示词 (仅 json_mode=False 时生效)
            timeout: 超时秒数
        """
        try:
            base = self.bailian_base_url.rstrip('/')
            url = f"{base}/chat/completions"
            headers = {
                "Authorization": f"Bearer {self.bailian_api_key}",
                "Content-Type": "application/json",
            }
            
            # 根据模式选择不同的 system prompt
            if json_mode:
                sys_prompt = SYSTEM_PROMPT
            else:
                sys_prompt = system_prompt or ANALYSIS_SYSTEM_PROMPT
            
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3 if json_mode else 0.7,
                "max_tokens": 4000,
            }
            
            # 仅 JSON 模式强制 response_format
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            
            for attempt in range(2):
                try:
                    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
                    resp.raise_for_status()
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    
                    # JSON 模式: 清理 markdown 代码块
                    if json_mode:
                        content = re.sub(r'^```json\s*', '', content, flags=re.MULTILINE)
                        content = re.sub(r'\s*```$', '', content, flags=re.MULTILINE)
                    
                    return content.strip()
                except requests.exceptions.Timeout:
                    logger.warning(f"[LLM API] 超时 (尝试 {attempt+1}/2), 等待重试...")
                    time.sleep(5)
                except requests.exceptions.RequestException as e:
                    logger.error(f"[LLM API] 请求异常: {e}")
                    return None
                    
            logger.error(f"[LLM API] 重试后仍然超时 (mode={'json' if json_mode else 'text'})")
            return None
            
        except Exception as e:
            logger.error(f"[LLM API] 调用失败: {e}")
            return None

    def _parse_response(self, response_text: str, news_items: List[Dict]) -> List[OrderStrategy]:
        """解析 LLM 响应为策略列表 (适配新版 Prompt 结构)"""
        strategies = []
        
        try:
            # 尝试解析 JSON (可能包含在一个数组中或者单个对象)
            result = json.loads(response_text)
            
            # 兼容两种格式：单个对象 或 包含 strategies 键的对象
            if isinstance(result, dict):
                if "strategies" in result:
                    analysis_list = result.get("strategies", [])
                elif "direction" in result:
                    # 单个分析结果
                    analysis_list = [result]
                else:
                    # 可能是其他格式，尝试找 list
                    for v in result.values():
                        if isinstance(v, list):
                            analysis_list = v
                            break
                    else:
                        analysis_list = []
            else:
                analysis_list = result if isinstance(result, list) else []

            for analysis in analysis_list:
                direction_val = analysis.get("direction", "NO_TRADE")
                tradeability = analysis.get("tradeability", "no_trade")
                confidence = float(analysis.get("confidence", 0.0))
                
                # 过滤 NO_TRADE 或 tradeability 为 no_trade 的
                if direction_val == "NO_TRADE" or tradeability == "no_trade":
                    continue
                
                # 获取标的列表
                focus_symbols = analysis.get("focus_symbols", [])
                if not focus_symbols and analysis.get("symbol"):
                    focus_symbols = [analysis.get("symbol")]
                
                # 增强鲁棒性: 如果方向明确但未给出 focus_symbols，尝试从 reasoning 中提取
                if not focus_symbols and direction_val != "NO_TRADE":
                    logger.warning(f"[解析] 缺失 focus_symbols，尝试从上下文推断")
                    # 这里暂时跳过，依赖 LLM 下一次输出或手动修复
                    continue
                    
                if not focus_symbols:
                    logger.warning(f"[解析] 无明确标的 (focus_symbols 为空), 跳过")
                    continue
                
                # 解析仓位大小 (position_sizing)
                pos_sizing = analysis.get("position_sizing", "small")
                pos_pct = 0.02 # default small
                if "medium" in str(pos_sizing) or "中" in str(pos_sizing): pos_pct = 0.05
                elif "large" in str(pos_sizing) or "大" in str(pos_sizing): pos_pct = 0.10
                # 尝试提取数字百分比
                import re as _re
                match = _re.search(r'(\d+)%', str(pos_sizing))
                if match: pos_pct = int(match.group(1)) / 100.0
                
                # 解析止损 (stop_loss)
                stop_loss_pct = 0.10 # default
                sl_text = str(analysis.get("stop_loss", ""))
                match_sl = _re.search(r'(\d+)%', sl_text)
                if match_sl: stop_loss_pct = int(match_sl.group(1)) / 100.0

                # 创建策略对象
                for sym in focus_symbols:
                    strategy = OrderStrategy()
                    strategy.symbol = sym
                    strategy.direction = "LONG" if direction_val == "BUY" else "SHORT"
                    strategy.confidence = confidence
                    strategy.reasoning = analysis.get("second_order_insight", analysis.get("notes", ""))
                    strategy.max_position_pct = pos_pct
                    strategy.stop_loss_pct = stop_loss_pct
                    strategy.related_event = "; ".join(analysis.get("key_events", []))
                    strategy.impact_chain = analysis.get("logic_chain", [])
                    
                    # 新字段映射
                    strategy.pricing_status = analysis.get("pricing_status", "unknown")
                    strategy.tradeability = tradeability
                    strategy.event_quality = analysis.get("event_quality", "medium")
                    strategy.logic_chain = analysis.get("logic_chain", [])
                    strategy.invalidating_factors = analysis.get("invalidating_factors", [])
                    strategy.risk_path = analysis.get("risk_path", "")
                    strategy.focus_symbols = focus_symbols
                    
                    strategies.append(strategy)
                    
            logger.info(f"[解析] 成功生成 {len(strategies)} 个策略")
            return strategies

        except json.JSONDecodeError as e:
            logger.error(f"[解析] JSON 解码失败: {e}")
            return []
        except Exception as e:
            logger.error(f"[解析] 解析异常: {e}")
            return []
            event_category = result.get("event_category", "")
            direct_impact = result.get("direct_impact", "")
            second_order = result.get("second_order_impact", "")
            
            event_summary = f"[{event_category}] {direct_impact}"
            if second_order:
                event_summary += f" → {second_order}"
            
            for s in strategy_list:
                strategy = OrderStrategy(
                    symbol=s.get("symbol", ""),
                    direction=s.get("direction", ""),
                    confidence=min(1.0, max(0.0, float(s.get("confidence", 0)))),
                    reasoning=s.get("reasoning", ""),
                    take_profit_pct=float(s.get("take_profit_pct", 0.15)),
                    stop_loss_pct=float(s.get("stop_loss_pct", 0.25)),
                    time_horizon_hours=int(s.get("time_horizon_hours", 72)),
                    trigger_condition=s.get("trigger_condition", "immediate"),
                    max_position_pct=min(0.25, float(s.get("max_position_pct", 0.05))),
                    related_event=event_summary,
                    impact_chain=impact_chain,
                    source_news_ids=[n.get('id', '') for n in news_items[:5]],
                    expiry_time=(datetime.now() + timedelta(hours=4)).isoformat(),
                )
                strategies.append(strategy)
                
        except json.JSONDecodeError as e:
            logger.error(f"[LLM策略引擎] JSON 解析失败: {e}")
            logger.debug(f"[LLM策略引擎] 原始响应: {response_text[:500]}")
        
        return strategies
    
    def _apply_cooldown(self, strategies: List[OrderStrategy]) -> List[OrderStrategy]:
        """应用冷却过滤: 同股票同方向 2 小时内不重复"""
        filtered = []
        now = datetime.now()
        
        for s in strategies:
            cooldown_key = f"{s.symbol}_{s.direction}"
            last_time = self._strategy_cooldown.get(cooldown_key)
            
            if last_time and (now - last_time).total_seconds() < 7200:  # 2 小时冷却
                logger.info(f"[LLM策略引擎] 冷却过滤: {cooldown_key} (上次: {last_time})")
                s.status = "rejected"
                s.reasoning += " [冷却过滤]"
                continue
            
            filtered.append(s)
            self._strategy_cooldown[cooldown_key] = now
            self._recent_strategies.append(s)
        
        # 保留最近 50 条策略
        self._recent_strategies = self._recent_strategies[-50:]
        
        return filtered
    
    def get_recent_strategies(self, limit: int = 10) -> List[Dict]:
        """获取最近生成的策略"""
        return [s.to_dict() for s in self._recent_strategies[-limit:]]
    
    def get_latest_for_symbol(self, symbol: str) -> Optional[OrderStrategy]:
        """获取指定股票的最新策略 (用于注入投票管线)"""
        for s in reversed(self._recent_strategies):
            if s.symbol == symbol and s.status in ("pending", "validated"):
                return s
        return None
    
    def audit_position_async(self, symbol: str, context: Dict, rsshub=None, timeout: int = 15):
        """
        盘中异步审计：交易已执行，LLM 负责检查是否有“暴雷”风险。
        运行在独立线程中，绝不阻塞主进程 (Fire-and-Forget)。
        """
        import threading
        import re
        import logging
        
        def _run_audit():
            logger = logging.getLogger("ftseek_us.llm_audit")
            try:
                # 1. 获取相关新闻 (仅最近 30 分钟)
                news_snippet = "None"
                if rsshub:
                    try:
                        items = rsshub.get_recent_news(hours=0.5, limit=5)
                        ticker = symbol.replace("US.", "")
                        relevant = [i for i in items if ticker.upper() in i.get('title', '').upper()]
                        if relevant:
                            news_snippet = relevant[0].get('title', 'No title')
                    except Exception:
                        pass
                
                # 2. 构造 Prompt (专注于找茬/风险识别)
                prompt = f"""
Role: Risk Auditor. We just executed a {context.get('direction', 'LONG')} trade on {symbol}.
Current Status:
- Price: ${context.get('price', 0):.2f}
- RSI: {context.get('rsi', 0):.1f}
- MACD: {context.get('macd', 0):.2f}
- Trend: {context.get('trend', 'NEUTRAL')}

Latest News: {news_snippet}

Task: Identify any hidden risk (e.g., earnings surprise, sudden bad news, pump & dump pattern).
Output: 
- SAFE: No specific risks found.
- RISK: [Specific Risk Reason] (e.g., RISK: Earnings report in 1 hour).
"""
                
                # 3. 执行推理 (带超时保护)
                try:
                    response = self._call_llm_analysis(prompt, timeout=timeout)
                    
                    # 4. 处理结果
                    if response and "RISK" in response.upper():
                        risk_match = re.search(r"RISK:?\s*(.*)", response, re.IGNORECASE)
                        reason = risk_match.group(1).strip() if risk_match else "Unknown Risk"
                        logger.warning(f"⚠️ [LLM 审计] {symbol} 发现风险: {reason}")
                        # 🚨 动作: 收紧止损 (例如移至成本价)
                        self._tighten_stop_loss_risk(symbol, reason)
                    else:
                        logger.info(f"✅ [LLM 审计] {symbol} 安全确认 (SAFE)")
                        
                except Exception as e:
                    logger.debug(f"[LLM 审计] {symbol} 调用异常 (已忽略): {e}")
            except Exception as e:
                logger.debug(f"[LLM 审计] {symbol} 线程异常 (已忽略): {e}")

        # 🚀 启动后台线程 (Fire-and-Forget)
        threading.Thread(target=_run_audit, daemon=True, name=f"LLM-Audit-{symbol}").start()

    def _tighten_stop_loss_risk(self, symbol: str, reason: str):
        """
        基于风险审计收紧止损
        """
        # 这里需要访问 scheduler 的 position_store 或 trade_store 来更新止损
        # 由于 engine 是单例，可以通过全局引用或者回调注入
        # 暂时只记录日志，后续可接入 TradeExecutor 的修改止损接口
        import logging
        logger = logging.getLogger("ftseek_us.llm_audit")
        logger.warning(f"🚨 [风控动作] {symbol}: 建议收紧止损 | 原因: {reason}")
        # TODO: 调用 scheduler.execution_engine.modify_stop_loss(symbol, new_sl=cost_price)
    
    def get_vote_for_symbol(self, symbol: str) -> Optional[Dict[str, Any]]:
        """获取指定股票的投票格式策略 (用于注入共识引擎)"""
        strategy = self.get_latest_for_symbol(symbol)
        if strategy:
            return strategy.to_vote_dict()
        return None
