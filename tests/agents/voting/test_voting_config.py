import pytest
from app.agents.voting.voting_config import VotingConfig, ModelRole

def test_model_role_definition():
    """测试模型角色定义 - 2 核心模型架构 (Qwen + MiniMax)"""
    assert ModelRole.QWEN.value == "qwen"
    assert ModelRole.MINIMAX.value == "minimax"
    # DEEPSEEK/GLM5/QWEN35 removed in P0 optimization (2026-04-17)
    assert not hasattr(ModelRole, 'DEEPSEEK')
    assert not hasattr(ModelRole, 'GLM5')

def test_voting_config_default_weights():
    """测试投票配置默认权重 - 2 模型精简版"""
    config = VotingConfig.get_default_config()

    # 2 模型架构: Qwen (70%) + MiniMax (30%) — optimized weights per 2026-05-02 review
    assert config.model_weights[ModelRole.QWEN] == 0.70
    assert config.model_weights[ModelRole.MINIMAX] == 0.30
    assert len(config.model_weights) == 2
    # Total = 1.0
    assert sum(config.model_weights.values()) == pytest.approx(1.0)

def test_voting_config_model_mapping():
    """测试模型映射 - 2 模型版本"""
    config = VotingConfig.get_default_config()

    assert config.model_mapping[ModelRole.QWEN] == "bailian/qwen-max"
    assert config.model_mapping[ModelRole.MINIMAX] == "bailian/MiniMax-M2.5"
    assert len(config.model_mapping) == 2
