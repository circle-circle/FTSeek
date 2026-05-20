"""
LLM 输出质量评估器 (LLM Quality Evaluator)

P2-1: 对比 LLM 预测 vs 实际交易结果，量化 LLM 输出质量。

核心功能:
1. 从 llm_metrics.jsonl 读取 LLM 预测 (prompt/response)
2. 从 trade_attribution 读取实际交易结果
3. 对比评估，生成质量评分
4. 写入 llm_quality_scores.jsonl 供后续优化使用

设计原则:
- 只在盘后审计时运行 (非实时)
- 评估维度: 方向准确性、信号质量、风险评估
- 支持 A/B 测试: 对比不同 prompt 版本的质量

作者: FTSeek Team
日期: 2026-05-21
"""

import json
import logging
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger("ftseek.llm.quality_evaluator")

# 数据路径
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
METRICS_FILE = DATA_DIR / "llm_metrics.jsonl"
QUALITY_FILE = DATA_DIR / "llm_quality_scores.jsonl"


class LLMQualityEvaluator:
    """LLM 输出质量评估器"""

    # 评估维度权重
    DIMENSION_WEIGHTS = {
        "direction_accuracy": 0.35,    # 方向判断准确率
        "signal_quality": 0.25,         # 信号质量评分
        "risk_assessment": 0.20,        # 风险评估准确性
        "timing_accuracy": 0.10,        # 时机判断准确率
        "format_compliance": 0.10,      # 输出格式合规率
    }

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or str(DATA_DIR / "ftseek.db")
        self.scores_cache: Dict[str, Any] = {}

    def evaluate_batch(self, days: int = 7) -> Dict[str, Any]:
        """
        批量评估最近 N 天的 LLM 输出质量

        Returns:
            {
                "period": "2026-05-14 ~ 2026-05-21",
                "total_predictions": 42,
                "evaluated": 35,
                "quality_score": 0.72,
                "dimensions": {
                    "direction_accuracy": 0.68,
                    "signal_quality": 0.75,
                    ...
                },
                "by_task_type": {
                    "premarket_analysis": {...},
                    "attribution": {...},
                    ...
                }
            }
        """
        cutoff = int(time.time()) - (days * 86400)
        metrics = self._load_metrics(cutoff)
        attributions = self._load_attributions(days)

        if not metrics:
            return {"total_predictions": 0, "quality_score": 0.0}

        # 按 task_type 分组评估
        by_task = defaultdict(list)
        for m in metrics:
            task = m.get("task_type", "unknown")
            by_task[task].append(m)

        # 评估每个 task_type
        task_scores = {}
        all_direction_scores = []
        all_signal_scores = []
        all_risk_scores = []
        all_format_scores = []

        for task_type, task_metrics in by_task.items():
            task_result = self._evaluate_task_type(task_type, task_metrics, attributions)
            task_scores[task_type] = task_result

            if task_result.get("direction_accuracy") is not None:
                all_direction_scores.append(task_result["direction_accuracy"])
            if task_result.get("signal_quality") is not None:
                all_signal_scores.append(task_result["signal_quality"])
            if task_result.get("risk_assessment") is not None:
                all_risk_scores.append(task_result["risk_assessment"])
            if task_result.get("format_compliance") is not None:
                all_format_scores.append(task_result["format_compliance"])

        # 计算总体评分
        overall = self._compute_overall_score(
            direction_scores=all_direction_scores,
            signal_scores=all_signal_scores,
            risk_scores=all_risk_scores,
            format_scores=all_format_scores,
        )

        period_start = datetime.fromtimestamp(cutoff).strftime("%Y-%m-%d")
        period_end = datetime.now().strftime("%Y-%m-%d")

        result = {
            "period": f"{period_start} ~ {period_end}",
            "total_predictions": len(metrics),
            "evaluated": sum(t.get("evaluated", 0) for t in task_scores.values()),
            "quality_score": overall["total"],
            "dimensions": overall["dimensions"],
            "by_task_type": task_scores,
            "timestamp": datetime.now().isoformat(),
        }

        # 写入质量评分文件
        self._persist_quality_score(result)

        logger.info(
            f"[LLMQuality] 评估完成: {period_start}~{period_end} | "
            f"预测={len(metrics)} 评估={result['evaluated']} 质量={result['quality_score']:.2f}"
        )

        return result

    def _evaluate_task_type(
        self, task_type: str, metrics: List[Dict], attributions: List[Dict]
    ) -> Dict[str, Any]:
        """评估单个 task_type 的质量"""
        evaluated = 0
        direction_correct = 0
        signal_good = 0
        risk_correct = 0
        format_ok = 0
        total = len(metrics)

        for m in metrics:
            if m.get("status") != "success":
                continue

            response = m.get("response_preview", "")
            if not response:
                continue

            evaluated += 1

            # 1. 格式合规: 响应是否包含有效 JSON
            if self._check_format_compliance(response):
                format_ok += 1

            # 2. 方向准确性: 如果有对应的 trade_attribution
            trace_id = self._extract_trace_id(m)
            matching_attr = None
            if trace_id:
                matching_attr = next(
                    (a for a in attributions if a.get("trace_id") == trace_id), None
                )

            if matching_attr:
                # 方向判断是否正确
                predicted_dir = self._extract_predicted_direction(response)
                actual_result = matching_attr.get("pnl_category", "")

                if predicted_dir and actual_result:
                    if (predicted_dir == "BUY" and actual_result == "win") or \
                       (predicted_dir == "SELL" and actual_result in ("win",)) or \
                       (predicted_dir == "NO_TRADE"):
                        direction_correct += 1

                # 信号质量
                if matching_attr.get("signal_quality"):
                    signal_good += 1

                # 风险评估
                if matching_attr.get("risk_quality") in ("good", "acceptable"):
                    risk_correct += 1

        direction_acc = direction_correct / evaluated if evaluated > 0 else None
        signal_acc = signal_good / evaluated if evaluated > 0 else None
        risk_acc = risk_correct / evaluated if evaluated > 0 else None
        format_acc = format_ok / evaluated if evaluated > 0 else None

        # 如果没有 attribution 数据，使用启发式评估
        if direction_acc is None and evaluated > 0:
            direction_acc = self._heuristic_direction_score(metrics)

        return {
            "task_type": task_type,
            "total": total,
            "evaluated": evaluated,
            "direction_accuracy": round(direction_acc, 3) if direction_acc is not None else None,
            "signal_quality": round(signal_acc, 3) if signal_acc is not None else None,
            "risk_assessment": round(risk_acc, 3) if risk_acc is not None else None,
            "format_compliance": round(format_acc, 3) if format_acc is not None else None,
        }

    def _check_format_compliance(self, response: str) -> bool:
        """检查响应是否包含有效 JSON"""
        if not response:
            return False
        text = response.strip()
        # 尝试提取 JSON
        try:
            import re
            text = re.sub(r'^```(?:json)?\s*\n?', '', text, flags=re.MULTILINE)
            text = re.sub(r'\n?\s*```\s*$', '', text, flags=re.MULTILINE)
            text = text.strip()
            json.loads(text)
            return True
        except (json.JSONDecodeError, ValueError):
            pass
        # 尝试提取 {...}
        try:
            start = text.index("{")
            end = text.rindex("}") + 1
            json.loads(text[start:end])
            return True
        except (json.JSONDecodeError, ValueError):
            pass
        return False

    def _extract_trace_id(self, metric: Dict) -> Optional[str]:
        """从 metric 中提取 trace_id"""
        prompt = metric.get("prompt_preview", "")
        if not prompt:
            return None
        import re
        match = re.search(r'(TRC-[A-F0-9]{12}|US-\d{8}-[A-F0-9]{6})', prompt)
        return match.group(1) if match else None

    def _extract_predicted_direction(self, response: str) -> Optional[str]:
        """从 LLM 响应中提取预测方向"""
        if not response:
            return None
        import re
        # 尝试提取 direction 字段
        match = re.search(r'"direction"\s*:\s*"(BUY|SELL|NO_TRADE)"', response)
        if match:
            return match.group(1)
        # 尝试提取 signal_impact 字段
        match = re.search(r'"signal_impact"\s*:\s*"(boost|neutral|weaken|block)"', response)
        if match:
            impact = match.group(1)
            if impact == "boost":
                return "BUY"
            elif impact == "block":
                return "NO_TRADE"
        return None

    def _heuristic_direction_score(self, metrics: List[Dict]) -> float:
        """启发式方向评分 (无 attribution 数据时使用)"""
        score = 0.5  # baseline
        for m in metrics:
            response = m.get("response_preview", "")
            if not response:
                continue
            # 检查是否有明确的 direction/signal_impact 输出
            if '"direction"' in response or '"signal_impact"' in response:
                score += 0.02
            # 检查是否有 confidence 输出
            if '"confidence"' in response:
                score += 0.01
        return min(score, 1.0)

    def _compute_overall_score(
        self,
        direction_scores: List[float],
        signal_scores: List[float],
        risk_scores: List[float],
        format_scores: List[float],
    ) -> Dict[str, Any]:
        """计算总体质量评分"""
        def safe_avg(lst):
            return round(sum(lst) / len(lst), 3) if lst else 0.0

        dims = {
            "direction_accuracy": safe_avg(direction_scores),
            "signal_quality": safe_avg(signal_scores),
            "risk_assessment": safe_avg(risk_scores),
            "format_compliance": safe_avg(format_scores),
            "timing_accuracy": 0.0,  # 暂无数据
        }

        total = sum(dims[k] * self.DIMENSION_WEIGHTS.get(k, 0) for k in dims)

        return {"total": round(total, 3), "dimensions": dims}

    def _load_metrics(self, cutoff_ts: int) -> List[Dict]:
        """加载 llm_metrics.jsonl"""
        if not METRICS_FILE.exists():
            return []

        records = []
        try:
            with open(METRICS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        r = json.loads(line)
                        if r.get("ts", 0) >= cutoff_ts:
                            records.append(r)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.warning(f"[LLMQuality] 加载 metrics 失败: {e}")

        return records

    def _load_attributions(self, days: int) -> List[Dict]:
        """加载 trade_attribution 数据"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """SELECT trace_id, code, pnl_category, overall_score,
                   direction_correct, signal_quality, risk_quality, pnl
                   FROM trade_attribution
                   WHERE created_at >= datetime('now', ?)""",
                (f"-{days} days",),
            )
            rows = [dict(r) for r in cursor.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            logger.warning(f"[LLMQuality] 加载 attribution 失败: {e}")
            return []

    def _persist_quality_score(self, result: Dict):
        """写入质量评分到 llm_quality_scores.jsonl"""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(QUALITY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(result, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"[LLMQuality] 写入质量评分失败: {e}")

    def get_quality_trend(self, days: int = 30) -> List[Dict]:
        """获取质量评分趋势 (用于图表)"""
        if not QUALITY_FILE.exists():
            return []

        records = []
        cutoff = int(time.time()) - (days * 86400)

        try:
            with open(QUALITY_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        r = json.loads(line)
                        ts = r.get("timestamp", "")
                        if ts:
                            records.append({
                                "date": ts[:10],
                                "quality_score": r.get("quality_score", 0),
                                "dimensions": r.get("dimensions", {}),
                            })
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass

        return records

    def get_model_comparison(self) -> Dict[str, Dict]:
        """对比不同模型的质量评分"""
        if not METRICS_FILE.exists():
            return {}

        model_stats = defaultdict(lambda: {"calls": 0, "success": 0, "errors": 0, "avg_latency": 0})
        latencies = defaultdict(list)

        try:
            with open(METRICS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        r = json.loads(line)
                        model = r.get("model", "unknown")
                        model_stats[model]["calls"] += 1
                        if r.get("status") == "success":
                            model_stats[model]["success"] += 1
                        else:
                            model_stats[model]["errors"] += 1
                        latencies[model].append(r.get("latency_ms", 0))
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass

        result = {}
        for model, stats in model_stats.items():
            result[model] = {
                "calls": stats["calls"],
                "success_rate": round(stats["success"] / stats["calls"], 3) if stats["calls"] > 0 else 0,
                "avg_latency_ms": round(sum(latencies[model]) / len(latencies[model])) if latencies[model] else 0,
                "error_rate": round(stats["errors"] / stats["calls"], 3) if stats["calls"] > 0 else 0,
            }

        return result


# ── 全局单例 ─────────────────────────────────────────────────

_evaluator: Optional[LLMQualityEvaluator] = None


def get_quality_evaluator(db_path: Optional[str] = None) -> LLMQualityEvaluator:
    """获取全局质量评估器"""
    global _evaluator
    if _evaluator is None:
        _evaluator = LLMQualityEvaluator(db_path=db_path)
    return _evaluator


# ── CLI 入口 ─────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    evaluator = get_quality_evaluator()
    result = evaluator.evaluate_batch(days=7)
    print(json.dumps(result, indent=2, ensure_ascii=False))
