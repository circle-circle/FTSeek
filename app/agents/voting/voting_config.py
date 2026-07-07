# app/agents/voting/voting_config.py
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List

class ModelRole(str, Enum):
    """模型角色 - 3模型架构 2026-06-28
    qwen3.7-max: 综合策略师 (宏观+技术综合)
    glm-5.2: 纯技术分析师 (K线/指标/量价)
    deepseek-v4-pro: 风险/反向审查官 (魔鬼代言人)
    """
    QWEN = "qwen3.7_max"              # 综合策略师 (主模型 50%)
    GLM = "glm_5_2"                   # 纯技术分析师 (30%)
    DEEPSEEK = "deepseek_v4_pro"      # 风险/反向审查官 (20%)

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

    # 偏差校正 (2026-06-28): 3模型架构下, 仅对 deepseek 做轻微 scale 校正
    # 原因: deepseek 作为风险审查官, 输出可能系统性偏谨慎(分数偏低)
    MODEL_BIAS_CORRECTION = {
        "deepseek_scale": 1.05,  # 轻微放大, 避免过度压低
        "deepseek_offset": 0,
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
        """获取默认配置 - 3模型架构 2026-07-02 调整
        Qwen3.7-max 35% + GLM-5.2 30% + DeepSeek-v4-pro 35%
        
        2026-07-02 权重调整依据 (178条3模型并行样本):
        - DeepSeek 分歧时正确率 52.6% (40/76) >> Ensemble 21.1% (16/76)
        - DeepSeek 整体准确率 46.6% >> Qwen 37.6%, GLM 41.6%
        - 原 DS 20% 权重导致 76 次分歧中 96% 被 qwen+glm 否决 → 次优决策
        - 现提至 35%, 让 DS 在分歧时有约等概率影响结果
        """
        return cls(
            model_weights={
                ModelRole.QWEN: 0.35,        # was 0.50, 综合策略/宏观/技术综合
                ModelRole.GLM: 0.30,         # 不变, 纯技术面分析
                ModelRole.DEEPSEEK: 0.35,    # was 0.20, 风险反向审查 (数据证明分歧时更准)
            },
            # 3模型版本说明 (2026-06-28)
            model_mapping={
                ModelRole.QWEN: "bailian/qwen3.7-max",        # 综合策略
                ModelRole.GLM: "bailian/glm-5.2",               # 技术分析
                ModelRole.DEEPSEEK: "bailian/kimi-k2.6",  # 风险反向 (2026-07-06: deepseek→kimi-k2, DashScope兼容性)
            },
            model_descriptions={
                ModelRole.QWEN: "综合策略师 (Qwen 3.7 Max)",
                ModelRole.GLM: "技术分析师 (GLM 5.2)",
                ModelRole.DEEPSEEK: "风险审查官 (DeepSeek v4 Pro)",
            }
        )
    
    def get_enabled_models(self) -> List[ModelRole]:
        """获取启用的模型列表"""
        return [
            role for role, weight in self.model_weights.items()
            if weight > 0
        ]
