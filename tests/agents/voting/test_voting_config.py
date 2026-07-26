import pytest
from app.agents.voting.voting_config import VotingConfig, ModelRole

def test_model_role_definition():
    """测试模型角色定义 - 3 核心模型架构 (2026-06-28: Qwen3.7-max + GLM-5.2 + DeepSeek-v4-pro)"""
    assert ModelRole.QWEN.value == "qwen3.7_max"
    assert ModelRole.GLM.value == "glm_5_2"
    assert ModelRole.DEEPSEEK.value == "deepseek_v4_pro"
    # MINIMAX removed in 3-model architecture (2026-06-28)
    assert not hasattr(ModelRole, 'MINIMAX')
    assert not hasattr(ModelRole, 'GLM5')

def test_voting_config_default_weights():
    """测试投票配置默认权重 - 3 模型架构 (2026-07-02 权重调整)"""
    config = VotingConfig.get_default_config()

    # 3 模型架构: Qwen (35%) + GLM (30%) + DeepSeek (35%)
    # 2026-07-02 调整依据: DeepSeek 分歧时正确率 52.6% >> Ensemble 21.1%
    assert config.model_weights[ModelRole.QWEN] == 0.35
    assert config.model_weights[ModelRole.GLM] == 0.30
    assert config.model_weights[ModelRole.DEEPSEEK] == 0.35
    assert len(config.model_weights) == 3
    # Total = 1.0
    assert sum(config.model_weights.values()) == pytest.approx(1.0)

def test_voting_config_model_mapping():
    """测试模型映射 - 3 模型版本 (2026-07-06: deepseek→kimi-k2.6, DashScope 兼容性)"""
    config = VotingConfig.get_default_config()

    assert config.model_mapping[ModelRole.QWEN] == "bailian/qwen3.7-max"
    assert config.model_mapping[ModelRole.GLM] == "bailian/glm-5.2"
    assert config.model_mapping[ModelRole.DEEPSEEK] == "bailian/kimi-k2.6"
    assert len(config.model_mapping) == 3
