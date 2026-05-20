"""
突发新闻拦截器 (Emergency News Interceptor)

职责:
1. 监控实时新闻，匹配“黑天鹅”关键词 (如退市、造假、破产、制裁等)。
2. 如果新闻涉及**当前持仓**标的，立即触发 LLM 快速研判 (qwen-flash)。
3. 若研判结果为“EXIT (清仓)”，则绕过常规管线，直接调用执行器市价卖出。

特点:
- 快速 (Flash 模型)
- 精准 (仅针对持仓)
- 果断 (直接执行，不经过信号共识)
"""

import json
import logging
import re
import os
import time
import asyncio
# FTSeek 适配: 集成市场规则注入 (2026-05-17)
from app.utils.market_prompt_injector import inject_market_context
from typing import List, Dict, Optional, Any
from dataclasses import dataclass

logger = logging.getLogger("ftseek.pipeline.interceptor")

# 核心拦截关键词 (中英文)
BLACK_SWAN_KEYWORDS = [
    # 财务/合规
    "退市", "Delist", "破产", "Bankruptcy", "清算", "违约", "Default", 
    "造假", "Fraud", "被调查", "Investigation", "立案", "财务造假", "证监会立案",
    # 宏观/地缘
    "制裁", "Sanction", "禁令", "Ban", "断供", "实体清单", "Entity List",
    # 市场极端
    "闪崩", "Crash", "熔断", "Halt", "停牌", "暴跌", "Plunge", "腰斩",
    # 产品/业务
    "召回", "Recall", "停产", "Shutdown", "核心资产被没收", "吊销牌照", "Revoked"
]

# 快速研判 Prompt
INTERCEPTOR_PROMPT = """
你是一个极其谨慎的首席风控官。
当前突发新闻：
标题：{headline}
内容：{content}

持仓信息：
标的：{symbol}
持仓数量：{qty}
成本价：{cost}

请判断：该新闻是否对该持仓构成“实质性、不可逆的毁灭性打击”？
如果是，必须立即清仓。
如果只是短期波动、情绪宣泄或噪音，则保留。

请严格输出 JSON:
{{
  "action": "EXIT" | "HOLD",
  "confidence": 0.0-1.0,
  "reason": "简短说明 (20 字内)"
}}
"""

@dataclass
class InterceptorResult:
    action: str  # EXIT / HOLD
    confidence: float
    reason: str
    news_headline: str

class EmergencyNewsInterceptor:
    """突发新闻拦截器"""

    def __init__(self, llm_analyst=None):
        self._llm_analyst = llm_analyst
        self._keywords = BLACK_SWAN_KEYWORDS
        self._is_active = True
        # 初始化 LLM 客户端 (复用 PreMarketAnalyst 的配置)
        self._client = None
        self._api_key = os.environ.get("DASHSCOPE_API_KEY", "")

    def initialize(self, llm_analyst):
        """初始化拦截器"""
        self._llm_analyst = llm_analyst
        # 尝试从 Analyst 获取 API Key 或 Model
        if hasattr(llm_analyst, 'fast_model'):
            self.model = llm_analyst.fast_model
        else:
            self.model = "qwen3.6-flash" # 默认

        logger.info("🛡️ 突发新闻拦截器已启动")

    async def check_and_intercept(self, news_items: List[Any], positions: List[Any]) -> List[InterceptorResult]:
        """
        检查新闻并执行拦截
        
        Args:
            news_items: 待检查的新闻列表
            positions: 当前持仓列表 (List[Position] 或 List[Dict])
            
        Returns:
            执行过的拦截结果列表
        """
        if not self._is_active:
            return []

        results = []
        
        # 1. 建立持仓索引 (Symbol -> Info)
        pos_map = {}
        for p in positions:
            code = getattr(p, 'code', None)
            if code:
                # P0 Fix 2026-05-03: Handle both dict and object types safely
                qty = getattr(p, 'quantity', 0) or (p.get('quantity', 0) if isinstance(p, dict) else 0)
                avg_price = getattr(p, 'avg_price', 0) or (p.get('avg_price', 0) if isinstance(p, dict) else 0)
                if not avg_price: avg_price = getattr(p, 'cost_price', 0) or (p.get('cost_price', 0) if isinstance(p, dict) else 0)
                
                pos_map[code] = {
                    'code': code,
                    'qty': qty,
                    'avg_price': avg_price
                }

        for item in news_items:
            # 获取新闻文本
            headline = getattr(item, "headline", "") or getattr(item, "title", "")
            content = getattr(item, "content", "") or getattr(item, "summary", "")
            text = f"{headline} {content}".lower()

            # 2. 关键词预检
            matched_kw = None
            for kw in self._keywords:
                if kw.lower() in text:
                    matched_kw = kw
                    break
            
            if not matched_kw:
                continue

            # 3. 检查是否涉及持仓
            symbols = getattr(item, "focus_symbols", [])
            if not symbols:
                # 尝试从标题/内容中匹配代码
                for sym in pos_map.keys():
                    # 处理代码格式匹配 (e.g. "HK.00700" -> "00700")
                    code_part = sym.replace("HK.", "").replace("US.", "")
                    if code_part in text or sym in text:
                        symbols.append(sym)
            
            relevant_symbols = [s for s in symbols if s in pos_map]
            if not relevant_symbols:
                continue

            # 4. LLM 研判 (针对每一个相关持仓)
            for sym in relevant_symbols:
                pos_info = pos_map[sym]
                
                logger.warning(f"🚨 [拦截器] 捕获持仓 {sym} 敏感新闻 (关键词:{matched_kw}): {headline}")
                
                # 调用 LLM
                result = await self._assess_risk(sym, headline, content[:500], pos_info)
                if result:
                    results.append(result)
                    # 如果判定 EXIT 且置信度高，立即执行
                    if result.action == "EXIT" and result.confidence > 0.75:
                        logger.critical(f"🔥 [拦截器] 判定 EXIT: {sym} - {result.reason}")
                        await self._execute_emergency_exit(sym, result.reason)

        return results

    async def _assess_risk(self, symbol: str, headline: str, content: str, pos_info: Dict) -> Optional[InterceptorResult]:
        """调用 LLM 研判风险 — P2 2026-05-06 使用 agent_bridge (替代直接 LLM 调用)"""
        try:
            from app.llm.agent_bridge import get_ftseek_agent_bridge
            bridge = get_ftseek_agent_bridge("hk")
            result = bridge.analyze_news_risk(symbol, headline, content, pos_info)

            if result and result.get("status") != "fallback":
                return InterceptorResult(
                    action=result.get("action", "HOLD"),
                    confidence=result.get("confidence", 0.0),
                    reason=result.get("reason", ""),
                    news_headline=headline
                )

            # 降级: 保守持有
            logger.warning(f"[Interceptor] 降级为 HOLD: {symbol}")
            return InterceptorResult(
                action="HOLD", confidence=0.0, reason="LLM 不可用，保守持有",
                news_headline=headline
            )

        except Exception as e:
            logger.error(f"[Interceptor] 研判失败 {symbol}: {e}")
            return InterceptorResult(
                action="HOLD", confidence=0.0, reason=f"异常: {e}",
                news_headline=headline
            )

    async def _execute_emergency_exit(self, symbol: str, reason: str):
        """执行紧急清仓"""
        try:
            from app.trader.executor import get_executor
            from app.strategy.base import Signal, SignalAction
            
            executor = get_executor()
            if executor:
                # 构造紧急卖出信号
                emergency_signal = Signal(
                    code=symbol,
                    action=SignalAction.SELL,
                    price=0.0, # 0 表示获取市价或当前价
                    quantity=0, # 0 表示全部
                    strategy="EMERGENCY_INTERCEPTOR",
                    trace_id="interceptor"
                )
                
                # 执行清仓 (调用 execute_partial_close 或直接 execute)
                # 假设 executor 有 execute 方法
                res = executor.execute(emergency_signal) 
                
                if res.success:
                    logger.info(f"✅ [拦截器] 成功清仓 {symbol}: {reason}")
                    # P0 修复: 发送 DingTalk 告警
                    try:
                        from app.notifier.dingtalk import DingTalkNotifier
                        notifier = DingTalkNotifier()
                        alert_msg = f"""🚨 **紧急风险拦截**

📉 标的：{symbol}
🔪 操作：强制清仓
⚠️ 原因：{reason}
⏰ 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

系统已自动执行清仓以控制风险。"""
                        notifier.send_text(alert_msg)
                        logger.info(f"[拦截器] 钉钉告警已发送: {symbol}")
                    except Exception as _alert_e:
                        logger.error(f"[拦截器] 钉钉告警发送失败: {_alert_e}")
                else:
                    logger.error(f"❌ [拦截器] 清仓失败 {symbol}: {res.message}")
            else:
                logger.error("[Interceptor] 无法获取执行器实例")
        except Exception as e:
            logger.error(f"[Interceptor] 执行清仓异常 {symbol}: {e}")
