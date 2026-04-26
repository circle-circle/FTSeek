"""
FTSeek 与 Hermes Agent 轻量桥接模块

核心设计:
1. 通过文件系统与本地 Hermes Agent 通信
2. 港股/美股记忆完全分离，避免经验混用
3. 聚焦"稳定赚钱"核心目标
4. 渐进式改进，不破坏现有系统
5. **独立降级**: Hermes 不可用时自动 fallback，不影响交易

架构:
  盘前 (08:00) → 写入请求 → Hermes 处理 → 读取计划
  盘后 (16:30) → 写入交易记录 → Hermes 处理 → 读取报告 + 更新记忆
  ↓
  记忆分离: hk.json (港股) / us.json (美股)
"""

import json
import logging
import os
import time
from typing import Dict, List, Optional, Any
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("ftseek.llm.agent_bridge")

# 文件系统协议目录
HERMES_HOME = os.path.expanduser("~/.hermes")
FTSEEK_DIR = os.path.join(HERMES_HOME, "ftseek")
REQUESTS_DIR = os.path.join(FTSEEK_DIR, "requests")
RESPONSES_DIR = os.path.join(FTSEEK_DIR, "responses")
MEMORY_DIR = os.path.join(FTSEEK_DIR, "memory")

# 确保目录存在
for d in [REQUESTS_DIR, RESPONSES_DIR, MEMORY_DIR]:
    os.makedirs(d, exist_ok=True)


class FTSeekAgentBridge:
    """
    FTSeek 与 Hermes Agent 的轻量桥接
    
    设计原则:
    - 稳定赚钱 > 激进收益
    - 数据驱动 > 主观判断
    - 持续改进 > 一次性优化
    - 记忆分离 > 混合使用 (港股/美股独立)
    - 降级优先 > 阻塞等待 (Hermes 不可用时不影响交易)
    """
    
    def __init__(self, market: str = "hk"):
        """
        初始化 Agent 桥接
        
        Args:
            market: 市场标识 "hk" (港股) 或 "us" (美股)
        """
        self.market = market.lower()
        self._memory_file = os.path.join(MEMORY_DIR, f"{self.market}.json")
        self._memory = self._load_memory()
        self._hermes_available = True  # Hermes 可用性标志
        
    def _load_memory(self) -> Dict:
        """加载指定市场的交易记忆"""
        if os.path.exists(self._memory_file):
            try:
                with open(self._memory_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"[{self.market}] 加载记忆失败: {e}")
        return self._default_memory()
    
    def _default_memory(self) -> Dict:
        """默认记忆 - 按市场区分"""
        market_name = "港股" if self.market == "hk" else "美股"
        return {
            "market": market_name,
            "core_principle": "稳定赚钱 > 激进收益",
            "market_specific_rules": self._get_market_rules(),
            "historical_lessons": [],  # 该市场的教训
            "strategy_adjustments": [],  # 策略调整记录
            "symbol_lessons": {},  # 该市场标的的特殊规律
            "performance_tracking": {
                "total_trades": 0,
                "win_rate": 0.0,
                "avg_pnl": 0.0,
                "max_drawdown": 0.0,
                "last_30_days_pnl": 0.0
            },
            "last_updated": datetime.now().isoformat()
        }
    
    def _get_market_rules(self) -> List[str]:
        """按市场返回不同的交易规则"""
        if self.market == "hk":
            return [
                "1. 永远先保本，再考虑盈利",
                "2. 单日亏损超 3% 立即停手",
                "3. 连续 3 笔亏损，强制休息",
                "4. 计划外的交易需要 2 倍确认",
                "5. 盈利时要拿得住，亏损时要跑得快",
                "6. 不要试图抓住每一波行情",
                "7. 港股受 A 股和美股双重影响，注意联动",
                "8. 关注南向资金流向和港股通标的",
                "9. 资金安全第一，收益率第二"
            ]
        else:  # us
            return [
                "1. 永远先保本，再考虑盈利",
                "2. 单日亏损超 3% 立即停手",
                "3. 连续 3 笔亏损，强制休息",
                "4. 计划外的交易需要 2 倍确认",
                "5. 盈利时要拿得住，亏损时要跑得快",
                "6. 不要试图抓住每一波行情",
                "7. 美股受宏观数据 (CPI/非农/FOMC) 影响大",
                "8. 注意盘前/盘后交易量和价差",
                "9. PDT 规则限制 (账户<$25k 时 5 天 3 次)",
                "10. 资金安全第一，收益率第二"
            ]
    
    def _save_memory(self):
        """保存记忆到本地文件"""
        self._memory["last_updated"] = datetime.now().isoformat()
        try:
            with open(self._memory_file, 'w', encoding='utf-8') as f:
                json.dump(self._memory, f, ensure_ascii=False, indent=2)
            logger.info(f"✅ [{self.market}] 交易记忆已保存")
        except Exception as e:
            logger.error(f"[{self.market}] 保存记忆失败: {e}")
    
    def generate_battle_plan(self, yesterday_review: str = "", overnight_news: str = "") -> Dict:
        """
        盘前生成作战计划
        
        利用历史记忆 + 昨日教训，生成今日计划
        """
        logger.info(f"[{self.market}] 🌅 开始生成盘前作战计划...")
        
        memory_context = self._format_memory_for_prompt()
        request_id = f"premarket_{self.market}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        request = {
            "id": request_id,
            "type": "generate_battle_plan",
            "market": self.market,
            "timestamp": datetime.now().isoformat(),
            "context": {
                "memory": memory_context,
                "yesterday_review": yesterday_review,
                "overnight_news": overnight_news
            },
            "prompt": f"""
你是 FTSeek 交易系统的盘前策略师。当前市场：**{self.market.upper()}**。核心目标：**稳定赚钱**。

【历史记忆与教训】(仅 {self.market} 市场)
{memory_context}

【昨日复盘结论】
{yesterday_review or "无昨日复盘数据"}

【隔夜重要新闻】
{overnight_news or "无隔夜新闻"}

【任务要求】
1. 基于 {self.market} 历史教训，调整今日策略倾向
2. 识别今日重点标的 (最多 5 只)
3. 明确交易方向、入场条件、止损位
4. 如果市场不确定性高，建议减少交易或观望

【输出格式】
严格 JSON:
{{
  "date": "YYYY-MM-DD",
  "market_regime": "RISK_ON/RISK_OFF/NEUTRAL",
  "targets": [
    {{
      "symbol": "HK.00700",
      "direction": "LONG/SHORT",
      "entry_condition": "具体入场条件",
      "stop_loss": "止损价格或百分比",
      "take_profit": "止盈目标",
      "confidence": 0.6-0.9,
      "rationale": "推荐理由"
    }}
  ],
  "risk_warnings": ["风险警告"],
  "max_positions": 3,
  "notes": "其他注意事项"
}}
"""
        }
        
        # 写入请求文件
        request_file = os.path.join(REQUESTS_DIR, f"{request_id}.json")
        with open(request_file, 'w', encoding='utf-8') as f:
            json.dump(request, f, ensure_ascii=False, indent=2)
        
        logger.info(f"📤 请求已写入: {request_file}")
        
        # 等待响应 (最多 60 秒)
        return self._wait_for_response(request_id, timeout=60)
    
    def generate_eod_review(self, trades: List[Dict], battle_plan: Dict) -> Dict:
        """
        盘后生成归因报告
        
        分析交易记录，提取教训，更新记忆
        """
        logger.info(f"[{self.market}] 🌆 开始生成盘后归因报告...")
        
        request_id = f"eod_{self.market}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        trades_json = json.dumps(trades, ensure_ascii=False, indent=2)[:3000]
        plan_json = json.dumps(battle_plan, ensure_ascii=False, indent=2)
        memory_json = json.dumps(self._memory, ensure_ascii=False, indent=2)[:2000]
        
        request = {
            "id": request_id,
            "type": "generate_eod_review",
            "market": self.market,
            "timestamp": datetime.now().isoformat(),
            "context": {
                "trades": trades,
                "battle_plan": battle_plan,
                "memory_snapshot": memory_json
            },
            "prompt": f"""
你是 FTSeek 交易系统的盘后复盘师。当前市场：**{self.market.upper()}**。核心目标：**稳定赚钱**。

【今日交易记录】(仅 {self.market} 市场)
{trades_json}

【今日作战计划】
{plan_json}

【当前记忆】(仅 {self.market} 市场)
{memory_json}

【任务要求】
1. 对比计划与实际执行的偏差
2. 分析每笔交易的盈亏归因
3. 识别可重复的盈利模式
4. 识别需要避免的亏损模式
5. 提出明日改进建议
6. **提取 1-2 条新的交易教训**，更新 {self.market} 记忆

【输出格式】
严格 JSON:
{{
  "date": "YYYY-MM-DD",
  "summary": {{
    "total_trades": 0,
    "win_rate": 0.0,
    "total_pnl": 0.0,
    "plan_execution_rate": 0.0
  }},
  "trade_analysis": [
    {{
      "symbol": "HK.00700",
      "action": "BUY/SELL",
      "result": "WIN/LOSS",
      "pnl": 0.0,
      "lesson": "从这笔交易中学到了什么"
    }}
  ],
  "new_lessons": ["新的交易教训"],
  "strategy_adjustments": ["明日策略调整"],
  "memory_updates": {{
    "historical_lessons": ["要添加到记忆的新教训"],
    "symbol_lessons": {{
      "HK.00700": "这只标的的特殊规律"
    }}
  }}
}}
"""
        }
        
        # 写入请求文件
        request_file = os.path.join(REQUESTS_DIR, f"{request_id}.json")
        with open(request_file, 'w', encoding='utf-8') as f:
            json.dump(request, f, ensure_ascii=False, indent=2)
        
        logger.info(f"📤 请求已写入: {request_file}")
        
        # 等待响应
        response = self._wait_for_response(request_id, timeout=60)
        
        # 更新记忆
        if response and response.get("memory_updates"):
            self._update_memory(response["memory_updates"])
        
        return response
    
    def _wait_for_response(self, request_id: str, timeout: int = 60) -> Dict:
        """
        等待 Hermes 响应 (带降级机制)
        
        降级策略:
        1. 首次超时 → 标记 Hermes 不可用
        2. 后续请求 → 直接返回 fallback，不等待
        3. 连续 5 次失败 → 禁用该市场 Agent 调用
        """
        # 如果 Hermes 已标记不可用，直接返回 fallback
        if not self._hermes_available:
            logger.warning(f"[{self.market}] Hermes 不可用，使用降级响应")
            return self._fallback_response(request_id)
        
        response_file = os.path.join(RESPONSES_DIR, f"{request_id}.json")
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            if os.path.exists(response_file):
                try:
                    with open(response_file, 'r', encoding='utf-8') as f:
                        response = json.load(f)
                    logger.info(f"✅ [{self.market}] 收到响应: {request_id}")
                    # 重置可用性
                    self._hermes_available = True
                    # 清理请求文件
                    request_file = os.path.join(REQUESTS_DIR, f"{request_id}.json")
                    if os.path.exists(request_file):
                        os.remove(request_file)
                    return response.get("result", {})
                except Exception as e:
                    logger.warning(f"读取响应失败: {e}")
            time.sleep(2)
        
        # 超时 → 标记不可用，返回 fallback
        logger.warning(f"⏱️ [{self.market}] 响应超时: {request_id}")
        self._hermes_available = False
        return self._fallback_response(request_id)
    
    def _fallback_response(self, request_id: str) -> Dict:
        """
        降级响应 (Hermes 不可用时使用)
        
        确保交易流程不因 LLM 问题而阻塞
        """
        if "premarket" in request_id:
            return {
                "status": "fallback",
                "message": "Hermes 不可用，使用降级模式",
                "plan": {
                    "date": datetime.now().strftime('%Y-%m-%d'),
                    "market_regime": "NEUTRAL",
                    "targets": [],
                    "risk_warnings": ["Agent 不可用，建议保守交易"],
                    "max_positions": 2,  # 降级时减少仓位
                    "notes": "使用硬编码风控，等待 Agent 恢复"
                }
            }
        else:
            return {
                "status": "fallback",
                "message": "Hermes 不可用，跳过 AI 归因",
                "summary": {
                    "total_trades": 0,
                    "win_rate": 0.0,
                    "total_pnl": 0.0
                },
                "memory_updates": {}
            }
    
    def _update_memory(self, updates: Dict):
        """更新交易记忆"""
        if "historical_lessons" in updates:
            for lesson in updates["historical_lessons"]:
                if lesson not in self._memory["historical_lessons"]:
                    self._memory["historical_lessons"].append(lesson)
                    logger.info(f"[{self.market}] 📝 新增教训: {lesson}")
        
        if "symbol_lessons" in updates:
            for symbol, lesson in updates["symbol_lessons"].items():
                self._memory["symbol_lessons"][symbol] = lesson
                logger.info(f"[{self.market}] 📝 更新标的教训: {symbol}")
        
        if "strategy_adjustments" in updates:
            for adj in updates["strategy_adjustments"]:
                self._memory["strategy_adjustments"].append({
                    "date": datetime.now().isoformat(),
                    "adjustment": adj
                })
        
        # 更新性能追踪
        if "performance" in updates:
            self._memory["performance_tracking"].update(updates["performance"])
        
        self._save_memory()
    
    def _format_memory_for_prompt(self) -> str:
        """将记忆格式化为 prompt 上下文"""
        market_name = "港股" if self.market == "hk" else "美股"
        lines = [
            f"市场: {market_name}",
            f"核心原则: {self._memory['core_principle']}",
            "",
            f"{market_name}交易法则:",
        ]
        for rule in self._memory["market_specific_rules"]:
            lines.append(f"  {rule}")
        
        if self._memory["historical_lessons"]:
            lines.append("")
            lines.append(f"{market_name}历史教训:")
            for lesson in self._memory["historical_lessons"][-5:]:  # 最近 5 条
                lines.append(f"  - {lesson}")
        
        if self._memory["symbol_lessons"]:
            lines.append("")
            lines.append(f"{market_name}标的特殊规律:")
            for symbol, lesson in list(self._memory["symbol_lessons"].items())[:3]:
                lines.append(f"  - {symbol}: {lesson}")
        
        perf = self._memory["performance_tracking"]
        lines.append("")
        lines.append(f"{market_name}性能: 交易{perf['total_trades']}笔, 胜率{perf['win_rate']:.0%}")
        
        return "\n".join(lines)
    
    def reset_memory(self):
        """重置该市场记忆 (谨慎使用)"""
        self._memory = self._default_memory()
        self._save_memory()
        logger.warning(f"⚠️ [{self.market}] 交易记忆已重置")
    
    def get_memory_summary(self) -> Dict:
        """获取记忆摘要"""
        market_name = "港股" if self.market == "hk" else "美股"
        return {
            "market": market_name,
            "lessons_count": len(self._memory["historical_lessons"]),
            "symbols_tracked": len(self._memory["symbol_lessons"]),
            "performance": self._memory["performance_tracking"],
            "last_updated": self._memory["last_updated"],
            "hermes_available": self._hermes_available
        }
    
    def force_reset_hermes_status(self):
        """强制重置 Hermes 可用性 (用于手动恢复)"""
        self._hermes_available = True
        logger.info(f"[{self.market}] Hermes 可用性已重置")


# 全局单例 (按市场分离)
_bridges: Dict[str, FTSeekAgentBridge] = {}

def get_ftseek_agent_bridge(market: str = "hk") -> FTSeekAgentBridge:
    """
    获取 Agent 桥接单例 (按市场分离)
    
    Args:
        market: "hk" (港股) 或 "us" (美股)
    """
    market = market.lower()
    if market not in _bridges:
        _bridges[market] = FTSeekAgentBridge(market=market)
    return _bridges[market]
