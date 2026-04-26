"""
LLM 宏观影响分析器 (LLM Impact Analyzer) - 2026-04-13

功能:
1. 接收宏观/地缘新闻内容。
2. 调用 LLM (Qwen) 分析该新闻对 Watchlist 中股票的影响。
3. 输出结构化的 JSON 数据（包含股票代码、影响分数、理由）。

集成位置:
- 作为旁路模块，在新闻采集到宏观关键词时异步触发。
- 分析结果用于增强推送卡片 和 辅助共识引擎决策。
"""

import json
import logging
import asyncio
from typing import Dict, List, Optional, Any
from pathlib import Path

# 尝试导入项目内的配置和客户端
try:
    from app.ai.providers.qwen import QwenProvider
    from app.ai.base import AIResponse
except ImportError:
    try:
        from app.ai.providers.bailian import BailianProvider as QwenProvider
    except ImportError:
        QwenProvider = None

logger = logging.getLogger("ftseek.factors.llm_impact")

# 预设 Prompt 模板
IMPACT_ANALYSIS_PROMPT = """
# Role
你是 FTSeek 首席量化宏观分析师。擅长通过突发新闻快速推导其对二级市场的直接和间接影响。

# Task
分析以下突发新闻，评估其对"目标股票池"中股票的影响。

# Input Data
新闻标题: {title}
新闻内容: {content}
目标股票池: 
{watchlist_str}

# Analysis Logic
1. **事件定性**: 判断是宏观政策、地缘政治、行业周期还是突发事件。
2. **传导路径**: 寻找"事件 -> 行业/宏观指标 -> 具体个股"的逻辑链条。
3. **二阶效应**: 不仅看直接受益/受损，还要看替代效应（如制裁 A 利好 B）或流动性效应（如加息利空高估值）。

# Scoring Criteria (-1.0 to 1.0)
- 1.0: 极大利好 (如：直接重组、重大利好政策落地、业绩超预期爆发)
- 0.5 ~ 0.8: 明显利好 (如：行业复苏信号、直接受益订单)
- 0.1 ~ 0.4: 轻微利好 / 情绪提振
- 0.0: 中性 / 无影响
- -0.1 ~ -0.4: 轻微利空 / 情绪压制
- -0.5 ~ -0.8: 明显利空 (如：监管收紧、关税壁垒、核心部件断供)
- -1.0: 极大利空 (如：退市风险、重大财务造假、毁灭性制裁)

# Output Format
必须严格输出合法的 JSON 对象，不要包含 Markdown 代码块标记或其他解释性文字。格式如下：
{
    "summary": "一句话总结核心影响逻辑",
    "impacts": [
        {
            "code": "HK.00700",
            "name": "腾讯控股",
            "score": -0.6,
            "reason": "美联储加息导致外资流出港股，压制互联网板块估值"
        }
    ]
}
"""

class LLMImpactAnalyzer:
    """LLM 宏观影响分析器"""

    _instance = None

    @classmethod
    def get_instance(cls, config: Dict = None):
        """单例模式获取实例"""
        if cls._instance is None:
            cls._instance = cls(config)
        return cls._instance

    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.provider = None
        self._init_provider()

    def _init_provider(self):
        """初始化 LLM Provider"""
        if QwenProvider is None:
            logger.warning("LLM Provider 未安装，ImpactAnalyzer 将不可用")
            return

        try:
            # QwenProvider 会自动从环境变量读取配置
            self.provider = QwenProvider()
            if not self.provider.api_key:
                logger.warning("API Key 未配置，ImpactAnalyzer 不可用")
                self.provider = None
            else:
                logger.info("LLM Impact Analyzer 初始化成功")
        except Exception as e:
            logger.error(f"LLM Impact Analyzer 初始化失败: {e}")
            self.provider = None

    async def analyze(self, title: str, content: str, watchlist: List[Dict]) -> Dict[str, Any]:
        """
        分析新闻对股票池的影响 (异步)

        Args:
            title: 新闻标题
            content: 新闻内容
            watchlist: 股票列表 [{"code": "HK.00700", "name": "腾讯", "sector": "Tech"}]

        Returns:
            {
                "summary": str,
                "impacts": [{"code": str, "name": str, "score": float, "reason": str}]
            }
        """
        if not self.provider:
            return {"summary": "LLM 服务不可用", "impacts": []}

        # 构建 Watchlist 字符串上下文
        wl_str = "\n".join([
            f"- {s['code']} ({s['name']})" 
            for s in watchlist
        ])

        prompt = IMPACT_ANALYSIS_PROMPT.format(
            title=title,
            content=content[:1500], # 限制内容长度
            watchlist_str=wl_str
        )

        try:
            # 异步调用 LLM
            loop = asyncio.get_running_loop()
            response_text = await loop.run_in_executor(None, self._call_llm_sync, prompt)
            
            return self._parse_response(response_text)

        except Exception as e:
            logger.error(f"LLM 分析过程异常: {e}")
            return {"summary": f"分析失败：{str(e)}", "impacts": []}

    def _call_llm_sync(self, prompt: str) -> str:
        """同步调用 LLM API"""
        try:
            response = self.provider.chat(
                prompt=prompt,
                temperature=0.1,
                max_tokens=1000
            )
            
            if response.success:
                return response.data.get('text', '')
            else:
                logger.error(f"LLM 调用失败: {response.error}")
                return ""
        except Exception as e:
            logger.error(f"LLM 调用异常: {e}")
            return ""

    def _parse_response(self, text: str) -> Dict[str, Any]:
        """解析 LLM 返回的 JSON"""
        try:
            if not text:
                return {"summary": "无响应", "impacts": []}

            # 清理可能存在的 Markdown 标记
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            
            data = json.loads(text)
            
            # 兼容字段名差异
            impacts = data.get("impacts", data.get("impact_stocks", []))
            
            # 数据校验与清洗
            valid_impacts = []
            for item in impacts:
                if isinstance(item, dict) and "code" in item and "score" in item:
                    score = float(item.get("score", 0))
                    item["score"] = max(-1.0, min(1.0, score))
                    valid_impacts.append(item)
            
            return {
                "summary": data.get("summary", "无摘要"),
                "impacts": valid_impacts
            }

        except Exception as e:
            logger.error(f"解析 LLM JSON 失败: {e}\nRaw text: {text[:200]}...")
            return {"summary": "解析失败", "impacts": []}
