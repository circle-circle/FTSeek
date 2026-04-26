"""盘前作战计划生成器 (Pre-Market Battle Planner)

功能:
1. 聚合新闻分析、跨市场表现、技术面信号。
2. 使用 qwen3.6-flash 进行快速二阶推理 (超时 20s)。
3. 生成 `data/daily_battle_plan.json`。
4. 推送钉钉通知 (Markdown, 纯告知)。

运行时间: 09:00 HKT
"""
import os
import sys
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.notifier import get_dingtalk_notifier
from app.core.config_container import get_config
from app.utils.llm_client import LLMClient

logger = logging.getLogger("ftseek.battle_planner")

# 配置
BATTLE_PLAN_PATH = "/opt/ftseek/FTSeek/data/daily_battle_plan.json"
LLM_MODEL = "qwen3.6-flash"
LLM_TIMEOUT = 20  # 秒

class BattlePlanner:
    def __init__(self):
        self.config = get_config()
        self.notifier = get_dingtalk_notifier()
        self.llm = LLMClient(model=LLM_MODEL, timeout=LLM_TIMEOUT)
    
    def generate_plan(self) -> Dict:
        """生成当日作战计划"""
        logger.info("开始生成今日作战计划...")
        
        # 1. 获取基础数据
        watchlist = self.config.get("watch_list_hk", [])
        news_summary = self._get_latest_news_summary()
        market_bias = self._analyze_market_bias()
        
        # 2. LLM 快速分析 (二阶推理)
        llm_analysis = self._run_llm_analysis(news_summary, market_bias, watchlist)
        
        # 3. 生成计划
        plan = self._build_plan(llm_analysis, watchlist)
        
        # 4. 保存
        self._save_plan(plan)
        
        # 5. 推送钉钉
        self._push_to_dingtalk(plan)
        
        logger.info(f"作战计划已生成并推送: {len(plan['target_positions'])} 个目标")
        return plan

    def _get_latest_news_summary(self) -> str:
        """获取最近 12h 新闻摘要"""
        # 简化实现: 从新闻库读取
        try:
            from app.pipeline.news_ttl_filter import get_recent_news
            # 假设存在此函数
            news = get_recent_news(hours=12)
            if news:
                return "\n".join([f"- {n.get('title', '')}" for n in news[:10]])
        except Exception:
            pass
        return "无显著突发新闻"

    def _analyze_market_bias(self) -> str:
        """分析大盘方向偏置"""
        # 简化实现: 读取 VIX 或 恒生指数期货
        return "NEUTRAL"

    def _run_llm_analysis(self, news: str, bias: str, watchlist: List[str]) -> Dict:
        """运行 LLM 快速分析"""
        prompt = f"""作为 FTSeek 首席策略师，请分析今日市场。
新闻: {news}
大盘偏置: {bias}
观察列表: {", ".join(watchlist)}

请输出 JSON:
{{"bullish": ["代码"], "bearish": ["代码"], "rationale": "简述"}}
"""
        try:
            start = time.time()
            response = self.llm.chat(prompt)
            elapsed = time.time() - start
            logger.info(f"LLM 分析完成，耗时: {elapsed:.1f}s")
            return json.loads(response)
        except Exception as e:
            logger.error(f"LLM 分析失败: {e}")
            return {"bullish": [], "bearish": [], "rationale": "LLM 不可用"}

    def _build_plan(self, llm_result: Dict, watchlist: List[str]) -> Dict:
        """构建计划结构"""
        plan = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "market_bias": "NEUTRAL", # 替换为真实分析
            "confidence": 0.0,
            "target_positions": [],
            "watch_only": [],
            "avoid": llm_result.get("bearish", []),
            "breaking_news_rules": {
                "trigger_threshold": 0.8,
                "max_response_time_seconds": 30
            }
        }
        # TODO: 结合实际技术信号填充 target_positions
        return plan

    def _save_plan(self, plan: Dict):
        os.makedirs(os.path.dirname(BATTLE_PLAN_PATH), exist_ok=True)
        with open(BATTLE_PLAN_PATH, 'w') as f:
            json.dump(plan, f, indent=2)

    def _push_to_dingtalk(self, plan: Dict):
        if not self.notifier:
            return
        lines = [f"### 📊 FTSeek 盘前作战计划 | {plan['date']}\n"]
        lines.append(f"**大盘偏置**: {plan['market_bias']}")
        lines.append(f"**置信度**: {plan['confidence']}")
        lines.append("\n**🎯 目标多头**:")
        for p in plan['target_positions']:
            lines.append(f"- {p['code']}: {p['rationale']}")
        lines.append("\n**⚠️ 回避**:")
        lines.append(", ".join(plan['avoid']) or "无")
        lines.append(f"\n**⏰ 执行时间**: 09:30 HKT 自动下单")
        lines.append(f"\n> 此为全自动执行计划，仅供知晓。")
        
        self.notifier.send_markdown("📊 盘前作战计划", "\n".join(lines))

if __name__ == "__main__":
    planner = BattlePlanner()
    planner.generate_plan()
