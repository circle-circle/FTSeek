"""
AI 否决层观察器 (AI Veto Observer)

核心原则:
- 纯观察模式，不阻断任何交易
- 记录"如果否决了会怎样"的对照数据
- 利用 LLM 擅长的事: 环境判断、信号质量评估

数据流:
  信号生成 → Veto Observer 并行运行 → 记录判断 → 交易照常执行 → 回填结果
"""

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("ftseek.risk.ai_veto_observer")

VETO_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "data"
VETO_LOG_FILE = VETO_LOG_DIR / "veto_log.jsonl"


class AIVetoObserver:
    """AI 否决层观察器 — 模拟盘阶段收集否决效果数据"""
    
    def __init__(self, agent_bridge=None):
        self.agent_bridge = agent_bridge
        VETO_LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    def evaluate(self, symbol: str, signal: Dict, market_context: Dict) -> Dict:
        """
        评估一个信号是否应该被否决/降仓/放行。
        
        Returns: {decision, reason, confidence}
        """
        prompt = self._build_prompt(symbol, signal, market_context)
        messages = [
            {"role": "system", "content": "你是 FTSeek 否决层。只输出 JSON。"},
            {"role": "user", "content": prompt},
        ]
        
        try:
            from app.llm.unified_service import get_unified_llm, UnifiedLLMService
            svc = get_unified_llm(default_model="qwen3.6-plus", default_timeout=60, max_retries=1)
            
            result_text = UnifiedLLMService.run_sync(
                svc.call(messages, model="qwen3.6-plus", timeout=60, task_type="veto"),
                timeout=90,
            )
            if result_text:
                parsed = svc.extract_json(result_text)
                if parsed:
                    return self._parse(parsed)
        except Exception as e:
            logger.debug(f"[VetoObserver] LLM 评估失败: {e}")
        
        return {
            "decision": "PASS",
            "reason": "LLM 评估失败，默认放行",
            "confidence": 0.0,
            "error": str(e) if 'e' in dir() else None,
        }
    
    def log(self, symbol: str, signal: Dict, veto_result: Dict, trade_result: Dict = None):
        """记录否决判断到 JSONL"""
        record = {
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            "signal": {
                "direction": signal.get("direction", "?"),
                "strength": signal.get("strength", signal.get("score", 0)),
                "source": signal.get("source", signal.get("strategy", "?")),
            },
            "veto": {
                "decision": veto_result.get("decision", "PASS"),
                "reason": veto_result.get("reason", ""),
                "confidence": veto_result.get("confidence", 0.0),
            },
        }
        
        if trade_result:
            record["result"] = {
                "pnl": trade_result.get("pnl", 0),
                "hold_time_min": trade_result.get("hold_time_min", 0),
                "exit_reason": trade_result.get("exit_reason", "?"),
            }
            record["veto_correct"] = self._calc_correct(
                veto_result.get("decision", "PASS"),
                trade_result.get("pnl", 0)
            )
        
        with open(VETO_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
        
        logger.info(
            f"[VetoObserver] {symbol} {signal.get('direction','?')} "
            f"→ {veto_result.get('decision','?')} "
            f"({veto_result.get('reason','')[:50]})"
        )
    
    def stats(self, days: int = 7) -> Dict:
        """获取否决层统计数据"""
        if not VETO_LOG_FILE.exists():
            return {"total": 0}
        
        records = []
        try:
            with open(VETO_LOG_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip():
                        records.append(json.loads(line))
        except Exception:
            return {"total": 0}
        
        vetoed = [r for r in records if r.get("veto", {}).get("decision") == "VETO"]
        passed = [r for r in records if r.get("veto", {}).get("decision") == "PASS"]
        
        veto_with_results = [r for r in vetoed if "result" in r]
        veto_correct = sum(1 for r in veto_with_results if r.get("veto_correct") == True)
        
        return {
            "total_signals": len(records),
            "veto_count": len(vetoed),
            "pass_count": len(passed),
            "veto_accuracy": veto_correct / max(len(veto_with_results), 1),
            "avg_pnl_vetoed": sum(r["result"].get("pnl", 0) for r in veto_with_results) / max(len(veto_with_results), 1),
            "avg_pnl_passed": sum(r["result"].get("pnl", 0) for r in passed if "result" in r) / max(len([r for r in passed if "result" in r]), 1),
        }
    
    def _build_prompt(self, symbol, signal, market):
        return f"""评估信号是否应该否决:
标的: {symbol}
方向: {signal.get('direction', '?')} | 强度: {signal.get('strength', 0)} | 来源: {signal.get('source', '?')}
市场: VIX={market.get('vix', '?')} | 趋势={market.get('trend', '?')}

判断:
1. 市场环境: 适合/不适合/不确定
2. 信号质量: 一致/矛盾/不确定
3. 历史模式: 好/差/不确定

输出 JSON:
{{"decision": "PASS/REDUCE/VETO", "reason": "一句话", "confidence": 0.0-1.0, "dimensions": {{"market": "...", "signal": "...", "history": "..."}}}}"""
    
    def _parse(self, result):
        decision = result.get("decision", "PASS").upper()
        if decision not in ("PASS", "REDUCE", "VETO"):
            decision = "PASS"
        return {"decision": decision, "reason": result.get("reason", ""), "confidence": float(result.get("confidence", 0.0))}
    
    def _calc_correct(self, decision, pnl):
        if decision in ("VETO", "REDUCE"):
            return pnl < 0  # 否决/降仓了亏损 → 正确
        elif decision == "PASS":
            return pnl > 0  # 放行了盈利 → 正确
        return None
