#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻反馈学习系统 (News Feedback Learner)

通过交易策略的实际反馈，自动优化新闻评分权重:
1. 收集交易策略对新闻的反馈 (点击、使用、盈利/亏损)
2. 分析反馈数据，识别评分偏差
3. 自动调整评分权重，提升评分准确性
4. 定期生成学习报告

作者: FTSeek Team
日期: 2026-04-17
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from pathlib import Path
from collections import defaultdict

logger = logging.getLogger("feedback_learner")


# ============================================================
# 配置
# ============================================================

LEARNER_CONFIG = {
    "feedback_file": "/opt/ftseek/FTSeek/data/news_feedback.json",
    "weights_file": "/opt/ftseek/FTSeek/data/news_weights.json",
    "learning_rate": 0.1,        # 学习率
    "min_samples": 10,           # 最小样本数才开始调整
    "adjustment_interval": 3600, # 权重调整间隔 (1 小时)
    "max_weight_change": 0.2,    # 单次最大权重变化
}


# ============================================================
# 反馈数据收集器
# ============================================================

class FeedbackCollector:
    """新闻反馈收集器"""
    
    def __init__(self, feedback_file: str = None):
        self.feedback_file = Path(feedback_file or LEARNER_CONFIG["feedback_file"])
        self.feedback_file.parent.mkdir(parents=True, exist_ok=True)
        
        self.feedback_data = self._load_feedback()
    
    def _load_feedback(self) -> dict:
        """加载反馈数据"""
        if self.feedback_file.exists():
            try:
                with open(self.feedback_file, 'r') as f:
                    return json.load(f)
            except:
                pass
        return {
            "news_feedback": {},  # news_id -> feedback list
            "feedback_count": 0,
            "last_updated": None,
        }
    
    def _save_feedback(self):
        """保存反馈数据"""
        try:
            with open(self.feedback_file, 'w') as f:
                json.dump(self.feedback_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"保存反馈数据失败: {e}")
    
    def add_feedback(self, news_id: str, feedback: dict):
        """添加新闻反馈"""
        if news_id not in self.feedback_data["news_feedback"]:
            self.feedback_data["news_feedback"][news_id] = []
        
        feedback["timestamp"] = datetime.now().isoformat()
        self.feedback_data["news_feedback"][news_id].append(feedback)
        self.feedback_data["feedback_count"] += 1
        self.feedback_data["last_updated"] = datetime.now().isoformat()
        
        self._save_feedback()
    
    def get_feedback_stats(self, news_id: str = None) -> dict:
        """获取反馈统计"""
        if news_id:
            feedbacks = self.feedback_data["news_feedback"].get(news_id, [])
            return {
                "count": len(feedbacks),
                "avg_outcome": sum(f.get("outcome", 0) for f in feedbacks) / max(1, len(feedbacks)),
                "positive_ratio": sum(1 for f in feedbacks if f.get("outcome", 0) > 0) / max(1, len(feedbacks)),
            }
        
        # 总体统计
        total_feedbacks = self.feedback_data["feedback_count"]
        return {
            "total_news_with_feedback": len(self.feedback_data["news_feedback"]),
            "total_feedback_count": total_feedbacks,
            "last_updated": self.feedback_data["last_updated"],
        }


# ============================================================
# 权重优化器
# ============================================================

class WeightOptimizer:
    """评分权重优化器"""
    
    def __init__(self, weights_file: str = None):
        self.weights_file = Path(weights_file or LEARNER_CONFIG["weights_file"])
        self.weights_file.parent.mkdir(parents=True, exist_ok=True)
        
        # 默认权重 (与 news_value_decay.py 中的评分维度对应)
        self.weights = {
            "timeliness": 0.25,          # 时效性
            "market_impact": 0.25,       # 市场影响度
            "relevance": 0.25,           # 相关度
            "source_credibility": 0.15,  # 来源可信度
            "uniqueness": 0.10,          # 独特性
            # LLM 评分维度
            "llm_market_impact": 0.20,
            "llm_second_order": 0.20,
            "llm_trade_actionability": 0.20,
            "llm_time_sensitivity": 0.15,
            "llm_summary": 0.05,
        }
        
        self._load_weights()
        
        # 学习统计
        self.learning_stats = {
            "adjustments": [],
            "last_adjustment": None,
            "total_adjustments": 0,
        }
    
    def _load_weights(self):
        """加载权重"""
        if self.weights_file.exists():
            try:
                with open(self.weights_file, 'r') as f:
                    data = json.load(f)
                    if "weights" in data:
                        self.weights.update(data["weights"])
                    if "learning_stats" in data:
                        self.learning_stats.update(data["learning_stats"])
            except:
                pass
    
    def _save_weights(self):
        """保存权重"""
        try:
            with open(self.weights_file, 'w') as f:
                json.dump({
                    "weights": self.weights,
                    "learning_stats": self.learning_stats,
                    "updated_at": datetime.now().isoformat(),
                }, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"保存权重失败: {e}")
    
    def analyze_feedback(self, feedback_data: dict) -> dict:
        """分析反馈数据，识别评分偏差"""
        analysis = {
            "news_scores": {},  # news_id -> {predicted_score, actual_outcome, error}
            "dimension_errors": defaultdict(list),
            "overall_error": 0,
            "sample_count": 0,
        }
        
        for news_id, feedbacks in feedback_data.get("news_feedback", {}).items():
            if not feedbacks:
                continue
            
            # 计算平均实际结果
            outcomes = [f.get("outcome", 0) for f in feedbacks]
            avg_outcome = sum(outcomes) / len(outcomes)
            
            # 获取预测分数
            predicted_score = feedbacks[0].get("predicted_score", 50)
            
            # 计算误差
            error = predicted_score - (avg_outcome * 100)  # 标准化到 0-100
            
            analysis["news_scores"][news_id] = {
                "predicted_score": predicted_score,
                "actual_outcome": avg_outcome,
                "error": error,
                "feedback_count": len(feedbacks),
            }
            
            analysis["sample_count"] += len(feedbacks)
            analysis["overall_error"] += error * len(feedbacks)
        
        if analysis["sample_count"] > 0:
            analysis["overall_error"] /= analysis["sample_count"]
        
        return analysis
    
    def adjust_weights(self, analysis: dict, learning_rate: float = None) -> dict:
        """根据分析结果调整权重"""
        lr = learning_rate or LEARNER_CONFIG["learning_rate"]
        
        if analysis["sample_count"] < LEARNER_CONFIG["min_samples"]:
            logger.info(f"样本数不足 ({analysis['sample_count']}/{LEARNER_CONFIG['min_samples']}), 跳过权重调整")
            return {"adjusted": False, "reason": "样本不足"}
        
        old_weights = self.weights.copy()
        adjustments = {}
        
        # 根据误差方向调整权重
        # 如果预测分数普遍高于实际结果，说明某些维度权重过高
        overall_error = analysis["overall_error"]
        
        for dimension in self.weights:
            # 计算该维度的平均误差贡献
            dimension_errors = []
            for news_id, scores in analysis["news_scores"].items():
                # 这里简化处理，实际应该分析每个维度对误差的贡献
                dimension_errors.append(scores["error"])
            
            if not dimension_errors:
                continue
            
            avg_error = sum(dimension_errors) / len(dimension_errors)
            
            # 如果平均误差为正 (预测过高)，降低该维度权重
            # 如果平均误差为负 (预测过低)，提高该维度权重
            adjustment = -avg_error * lr * self.weights[dimension]
            
            # 限制调整幅度
            max_change = LEARNER_CONFIG["max_weight_change"] * self.weights[dimension]
            adjustment = max(-max_change, min(max_change, adjustment))
            
            new_weight = self.weights[dimension] + adjustment
            
            # 确保权重在合理范围内 [0.05, 0.40]
            new_weight = max(0.05, min(0.40, new_weight))
            
            adjustments[dimension] = {
                "old": self.weights[dimension],
                "new": new_weight,
                "change": adjustment,
            }
            
            self.weights[dimension] = new_weight
        
        # 归一化权重 (确保总和为 1)
        total_weight = sum(self.weights.values())
        if total_weight > 0:
            for dimension in self.weights:
                self.weights[dimension] /= total_weight
        
        # 保存权重
        self._save_weights()
        
        # 记录调整
        self.learning_stats["adjustments"].append({
            "timestamp": datetime.now().isoformat(),
            "sample_count": analysis["sample_count"],
            "overall_error": overall_error,
            "adjustments": adjustments,
        })
        self.learning_stats["last_adjustment"] = datetime.now().isoformat()
        self.learning_stats["total_adjustments"] += 1
        
        return {
            "adjusted": True,
            "adjustments": adjustments,
            "overall_error": overall_error,
            "sample_count": analysis["sample_count"],
        }
    
    def get_weights(self) -> dict:
        """获取当前权重"""
        return self.weights.copy()
    
    def get_learning_stats(self) -> dict:
        """获取学习统计"""
        return {
            "total_adjustments": self.learning_stats["total_adjustments"],
            "last_adjustment": self.learning_stats["last_adjustment"],
            "recent_adjustments": self.learning_stats["adjustments"][-5:],
            "current_weights": self.get_weights(),
        }


# ============================================================
# 反馈学习引擎
# ============================================================

class FeedbackLearner:
    """反馈学习引擎"""
    
    def __init__(self, collector: FeedbackCollector = None, optimizer: WeightOptimizer = None):
        self.collector = collector or FeedbackCollector()
        self.optimizer = optimizer or WeightOptimizer()
        
        self.last_adjustment_time = 0
    
    def collect_feedback(self, news_id: str, outcome: float, predicted_score: float, 
                        strategy_name: str = "default", extra: dict = None):
        """收集交易策略反馈"""
        feedback = {
            "outcome": outcome,  # -1 到 1 (亏损到盈利)
            "predicted_score": predicted_score,
            "strategy_name": strategy_name,
            "timestamp": datetime.now().isoformat(),
        }
        
        if extra:
            feedback.update(extra)
        
        self.collector.add_feedback(news_id, feedback)
        logger.info(f"收集反馈: news_id={news_id}, outcome={outcome:.2f}, predicted={predicted_score:.1f}")
    
    def run_learning_cycle(self) -> dict:
        """运行学习周期"""
        now = time.time()
        
        # 检查是否需要调整
        if now - self.last_adjustment_time < LEARNER_CONFIG["adjustment_interval"]:
            return {"skipped": True, "reason": "未到调整间隔"}
        
        # 分析反馈
        feedback_data = self.collector.feedback_data
        analysis = self.optimizer.analyze_feedback(feedback_data)
        
        # 调整权重
        result = self.optimizer.adjust_weights(analysis)
        
        self.last_adjustment_time = now
        
        return {
            "skipped": False,
            "analysis": analysis,
            "adjustment": result,
            "learning_stats": self.optimizer.get_learning_stats(),
        }
    
    def get_feedback_stats(self) -> dict:
        """获取反馈统计"""
        return self.collector.get_feedback_stats()
    
    def get_learning_report(self) -> dict:
        """生成学习报告"""
        return {
            "feedback_stats": self.get_feedback_stats(),
            "learning_stats": self.optimizer.get_learning_stats(),
            "current_weights": self.optimizer.get_weights(),
            "generated_at": datetime.now().isoformat(),
        }


# ============================================================
# 使用示例
# ============================================================

if __name__ == "__main__":
    # 测试示例
    learner = FeedbackLearner()
    
    # 模拟收集反馈
    print("=== 模拟收集反馈 ===")
    for i in range(15):
        news_id = f"test_news_{i}"
        outcome = 0.5 if i < 10 else -0.3  # 前 10 个盈利，后 5 个亏损
        predicted = 75.0 if i < 10 else 60.0
        
        learner.collect_feedback(news_id, outcome, predicted, strategy_name="momentum")
    
    # 查看反馈统计
    stats = learner.get_feedback_stats()
    print(f"反馈统计: {stats}")
    
    # 运行学习周期
    print("\n=== 运行学习周期 ===")
    result = learner.run_learning_cycle()
    print(f"学习结果: {result}")
    
    # 查看学习报告
    report = learner.get_learning_report()
    print(f"\n=== 学习报告 ===")
    print(f"当前权重: {report['current_weights']}")
    print(f"学习统计: {report['learning_stats']}")
