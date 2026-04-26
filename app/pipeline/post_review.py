"""
盘后复盘模块 (Post-market Review)

职责:
1. 加载盘前作战计划 (`daily_battle_plan.json`)。
2. 获取当日实际收盘价 (Open/Close)。
3. 对比“计划方向”与“实际走势”，计算命中率 (Hit Rate)。
4. 生成复盘报告 (`daily_review_report.json`)。
5. 提取“高价值反馈”，用于优化次日 LLM Prompt。

特点:
- 闭环 (Close the loop): 验证预测准确性
- 自适应 (Adaptive): 识别失效逻辑
- 沉淀 (Accumulate): 记录高胜率/低胜率场景
"""

import json
import logging
import os
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger("ftseek.pipeline.post_review")

@dataclass
class ReviewResult:
    symbol: str
    plan_direction: str      # LONG / SHORT
    plan_logic: str          # 核心逻辑
    actual_return: float     # 实际涨跌幅 (e.g. 0.05 for 5%)
    is_hit: bool             # 是否命中
    reason: str              # 命中/未命中原因

class PostMarketReviewer:
    """盘后复盘器"""

    def __init__(self):
        pass

    def review(self, plan_path: str, market_data: Dict[str, Dict]) -> Tuple[Dict, str]:
        """
        执行复盘
        
        Args:
            plan_path: daily_battle_plan.json 路径
            market_data: 实际市场数据 {symbol: {open, close, high, low}}
            
        Returns:
            (report_dict, feedback_prompt_text)
        """
        if not os.path.exists(plan_path):
            logger.warning("作战计划文件不存在，无法复盘")
            return {}, "无作战计划可供复盘。"

        try:
            with open(plan_path, 'r') as f:
                plan = json.load(f)
        except Exception as e:
            logger.error(f"读取作战计划失败: {e}")
            return {}, "读取计划失败。"

        targets = plan.get("targets", [])
        results = []
        hits = 0
        total = 0
        
        feedback_points = []

        for target in targets:
            sym = target.get("symbol")
            direction = target.get("direction", "LONG")
            logic = target.get("logic", "")
            
            data = market_data.get(sym, {})
            
            # 1. Determine Actual Return
            actual_return = 0.0
            has_data = False
            
            if "realized_pnl" in data:
                # Use realized PnL if available (stored as percentage, e.g. 5.0 for 5%)
                actual_return = data["realized_pnl"] / 100.0 
                has_data = True
            else:
                open_price = data.get("open", 0)
                close_price = data.get("close", 0)
                if open_price > 0 and close_price > 0:
                    actual_return = (close_price - open_price) / open_price
                    has_data = True
            
            if not has_data:
                logger.debug(f"标的 {sym} 无数据，跳过")
                continue

            # 2. Check Hit/Miss
            total += 1
            is_hit = False
            reason = ""
            
            if direction == "LONG":
                if actual_return > 0.005: # 涨超 0.5% 算命中
                    is_hit = True
                    reason = f"实际上涨 {actual_return:.1%}"
                else:
                    reason = f"实际下跌/平盘 {actual_return:.1%}"
            elif direction == "SHORT":
                if actual_return < -0.005: # 跌超 0.5% 算命中
                    is_hit = True
                    reason = f"实际下跌 {actual_return:.1%}"
                else:
                    reason = f"实际上涨/平盘 {actual_return:.1%}"
            
            if is_hit:
                hits += 1
                feedback_points.append(f"✅ [Hit] {sym}: {logic} (Actual: {actual_return:.1%})")
            else:
                feedback_points.append(f"❌ [Miss] {sym}: {logic} (Actual: {actual_return:.1%}, Exp: {direction})")
            
            results.append({
                "symbol": sym,
                "plan_direction": direction,
                "actual_return": round(actual_return, 4),
                "is_hit": is_hit,
                "reason": reason,
                "logic": logic
            })

        hit_rate = (hits / total) if total > 0 else 0.0
        
        report = {
            "date": plan.get("date", ""),
            "generated_at": datetime.now().isoformat(),
            "total_targets": total,
            "hits": hits,
            "hit_rate": round(hit_rate, 2),
            "details": results
        }
        
        # 生成反馈 Prompt
        feedback_prompt = self._generate_feedback_prompt(report, feedback_points)
        
        # 保存报告
        report_dir = os.path.dirname(plan_path)
        report_path = os.path.join(report_dir, "daily_review_report.json")
        try:
            with open(report_path, 'w', encoding='utf-8') as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
            logger.info(f"📝 复盘报告已生成: {report_path}")
            logger.info(f"   今日命中率: {hit_rate:.0%} ({hits}/{total})")
        except Exception as e:
            logger.error(f"保存复盘报告失败: {e}")

        return report, feedback_prompt

    def _generate_feedback_prompt(self, report: Dict, points: List[str]) -> str:
        """生成次日 LLM 可用的反馈文本"""
        hit_rate = report.get("hit_rate", 0)
        
        intro = "【盘后复盘反馈】\n"
        
        if hit_rate >= 0.7:
            intro += "昨日预测表现优秀 (命中率 {:.0%})。请继续保持当前的推理逻辑。\n".format(hit_rate)
        elif hit_rate >= 0.4:
            intro += "昨日预测表现一般 (命中率 {:.0%})。请注意某些逻辑可能未充分定价。\n".format(hit_rate)
        else:
            intro += "昨日预测表现较差 (命中率 {:.0%})。请反思市场风格是否切换，逻辑是否失效。\n".format(hit_rate)
            
        intro += "具体案例回顾：\n"
        # 只取前 5 条反馈
        for p in points[:5]:
            intro += f"- {p}\n"
            
        return intro
