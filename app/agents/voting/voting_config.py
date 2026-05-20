# app/agents/voting/voting_config.py
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List

class ModelRole(str, Enum):
    """模型角色 - 2模型架构 2026-04-17
    去掉了 DeepSeek(与Qwen同源)、Kimi(短线价值低)、GLM-5(超时频繁)
    保留: Qwen(宏观/二阶推理/技术综合) + MiniMax(新闻情绪)
    """
    QWEN = "qwen"              # 策略推理/宏观分析/二阶传导/技术综合 (主模型 70%)
    MINIMAX = "minimax"        # 新闻情绪/异议检测 (30%，仅方向相反时触发辩论)

@dataclass
class VotingConfig:
    """投票配置 - 方案 E (平衡优化) 2026-03-30"""
    model_weights: Dict[ModelRole, float]
    model_mapping: Dict[ModelRole, str]
    model_descriptions: Dict[ModelRole, str]
    min_consensus_threshold: float = 0.55  # 共识阈值
    max_std_dev: float = 15.0  # 最大标准差 (超过需人工审核)
    
    # 一致性检测阈值 - 2模型架构 (2026-04-17 调整)
    # 2模型 std_dev 计算基准: 20分差 → std_dev≈14.1
    # 放宽阈值以适应2模型架构
    MAX_STD_DEV = 20.0           # 标准差超过此值需人工审核 (3模型=15, 2模型=20)
    MIN_CONSENSUS = 0.20         # 最低共识度 (2模型放宽: 与 scheduler_core.py MIN_CONSENSUS_LEVEL 对齐)
    
    # 中性信号检测 - 方案 E 优化：放宽中性区间 (45-55 → 40-60)
    NEUTRAL_LOW = 40             # 45 → 40，降低过滤率至~22%
    NEUTRAL_HIGH = 60            # 55 → 60，降低过滤率至~22%
    NEUTRAL_MAX_STD = 10.0
    
    # Fallback 置信度 - 方案 E 优化：提高利用率 (0.40 → 0.50)
    FALLBACK_CONFIDENCE_COMPLETE = 0.50  # 0.40 → 0.50
    FALLBACK_CONFIDENCE_CORE = 0.40      # 0.30 → 0.40
    FALLBACK_CONFIDENCE_MINIMAL = 0.30   # 0.20 → 0.30
    FALLBACK_CONFIDENCE_MAX = 0.60       # 0.50 → 0.60
    
    # 动态权重调整 - 2模型架构 VIX 三阈值
    VIX_LOW_THRESHOLD = 20.0
    VIX_MID_THRESHOLD = 25.0
    VIX_HIGH_THRESHOLD = 30.0
    VIX_WEIGHT_ADJUSTMENT = 0.10  # VIX>30 时: 降低 Qwen 权重, 提高 MiniMax 情绪权重

    # P0 修复 (2026-04-17): 模型偏差校正
    # minimax 系统性偏高 20-34 分 (实测均值 +25), 使用比例校正更平滑
    # 旧方法: 固定 -25 (过度校正)
    # 新方法: 比例缩放 minimax * 0.75 (65→49, 88→66, 78→59) 更平滑
    MODEL_BIAS_CORRECTION = {
        "minimax_scale": 0.75,  # minimax 原始分数 * 0.75 = 校正值
        "minimax_offset": 0,    # 校正值 + offset (不使用)
    }
    
    # LLM 策略引擎动态投票权重 (2026-04-17 优化)
    # 基础权重 + 动态调整 (方向一致/置信度/传导链深度)
    LLM_EXTERNAL_VOTE_WEIGHT = 0.15      # 基础权重 15%
    LLM_EXTERNAL_VOTE_MIN = 0.05         # 最小权重 5% (方向分歧时)
    LLM_EXTERNAL_VOTE_MAX = 0.30         # 最大权重 30% (高度一致+高质量推理)
    LLM_DIRECTION_AGREE_BONUS = 0.10     # 方向一致 +10%
    LLM_CONFIDENCE_BONUS = 0.05          # LLM 置信度高于内部平均 +5%
    LLM_EXTERNAL_VOTE_ENABLED = True     # 是否启用 LLM 外部投票
    
    # 一致性过高 → 自动辩论触发 (2026-04-17 新增)
    AUTO_DEBATE_CONSENSUS_THRESHOLD = 0.70   # 一致性超过此值触发反向辩论 (4模型下 std_dev<4.5)
    AUTO_DEBATE_STRENGTH_THRESHOLD = 65      # 强度超过此值 + 高一致 → 触发
    AUTO_DEBATE_ENABLED = True               # 是否启用自动辩论
    
    # 信心阈值 - 方案 E 优化：降低门槛 (0.65 → 0.55)
    CONFIDENCE_THRESHOLD = 0.55          # 0.65 → 0.55，信号通过率 +25%
    
    # 动态仓位管理 - 方案 E 新增
    POSITION_BASE = 0.10                 # 基础仓位 10%
    POSITION_HIGH_CONFIDENCE = 0.15      # 高信心 (≥0.70): 15%
    POSITION_MEDIUM_CONFIDENCE = 0.12    # 中信心 (0.60-0.70): 12%
    POSITION_LOW_CONFIDENCE = 0.08       # 低信心 (0.50-0.60): 8%
    POSITION_MINIMUM = 0.05              # 最低信心 (<0.50): 5%
    
    # 评分区间定义 (统一标准)
    STRONG_SHORT_MAX = 30      # 0-30: 强烈看空
    SHORT_MAX = 45             # 31-45: 看空
    NEUTRAL_MAX = 55           # 46-55: 中性
    LONG_MAX = 70              # 56-70: 看多
    # 71-100: 强烈看多
    
    @classmethod
    def get_default_config(cls) -> "VotingConfig":
        """获取默认配置 - 2模型架构 2026-04-29
        Qwen 70% + MiniMax 30%
        Qwen 做主判断(宏观+技术综合分析)，MiniMax 做异议检测
        MiniMax 只在与 Qwen 方向相反时才触发辩论，否则以 Qwen 为准
        """
        return cls(
            model_weights={
                ModelRole.QWEN: 0.70,       # 策略推理/宏观/技术综合 (70%, 主判断)
                ModelRole.MINIMAX: 0.30,    # 新闻情绪/异议检测 (30%, 仅分歧时介入)
            },
            # 2模型版本说明 (2026-04-17 精简架构)
            model_mapping={
                ModelRole.QWEN: "bailian/qwen-max",      # 策略推理+技术综合
                ModelRole.MINIMAX: "bailian/MiniMax-M2.5",   # 情绪分析
            },
            model_descriptions={
                ModelRole.QWEN: "策略推理/宏观/技术分析 (Qwen 3.6 Plus)",
                ModelRole.MINIMAX: "新闻情绪分析 (MiniMax M2.5)",
            }
        )
    
    def get_enabled_models(self) -> List[ModelRole]:
        """获取启用的模型列表"""
        return [
            role for role, weight in self.model_weights.items()
            if weight > 0
        ]
