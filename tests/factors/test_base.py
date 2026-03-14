"""
因子库基础测试
"""
import pytest
import sys
from pathlib import Path
from typing import Dict

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.factors.base import BaseFactor, FactorResult
from app.factors.config import FactorConfig


class ConcreteFactor(BaseFactor):
    """用于测试的具体因子实现"""
    name = "concrete_factor"
    
    def calculate(self, code: str, data: Dict):
        """实现抽象方法"""
        return None


class TestFactorResult:
    """测试 FactorResult"""
    
    def test_create_factor_result(self):
        """创建因子结果"""
        result = FactorResult(
            code='HK.00700',
            factor_name='northbound',
            value=0.75,
            timestamp='2026-03-14'
        )
        assert result.code == 'HK.00700'
        assert result.factor_name == 'northbound'
        assert result.value == 0.75
    
    def test_factor_result_with_metadata(self):
        """带元数据的因子结果"""
        result = FactorResult(
            code='US.AAPL',
            factor_name='sentiment',
            value=-0.3,
            timestamp='2026-03-14',
            metadata={'source': 'news', 'count': 10}
        )
        assert result.metadata['source'] == 'news'
    
    def test_factor_result_warning_range(self, caplog):
        """因子值超出范围警告"""
        import logging
        logging.getLogger("ftseek.factors").setLevel(logging.WARNING)
        
        result = FactorResult(
            code='HK.00700',
            factor_name='test',
            value=15.0,  # 超出范围
            timestamp='2026-03-14'
        )
        assert "因子值超出范围" in caplog.text


class TestBaseFactor:
    """测试 BaseFactor 基类"""
    
    def test_validate_data_insufficient(self):
        """数据不足验证失败"""
        factor = ConcreteFactor()
        data = {'klines': [{'close': 100}]}  # 只有 1 条数据
        assert factor.validate_data(data, min_periods=50) == False
    
    def test_validate_data_sufficient(self):
        """数据充足验证通过"""
        factor = ConcreteFactor()
        data = {'klines': [{'close': i} for i in range(100)]}
        assert factor.validate_data(data, min_periods=50) == True
    
    def test_validate_data_empty(self):
        """空数据验证失败"""
        factor = ConcreteFactor()
        assert factor.validate_data({}) == False


class TestFactorConfig:
    """测试 FactorConfig"""
    
    def test_default_config(self, monkeypatch):
        """默认配置"""
        monkeypatch.setenv('FTSEEK_ENABLE_ALTERNATIVE_FACTORS', 'false')
        config = FactorConfig.from_env()
        assert config.enabled == False
        assert config.weight == 1.0
    
    def test_enabled_config(self, monkeypatch):
        """启用配置"""
        monkeypatch.setenv('FTSEEK_ENABLE_ALTERNATIVE_FACTORS', 'true')
        monkeypatch.setenv('FTSEEK_FACTOR_WEIGHT', '0.8')
        config = FactorConfig.from_env()
        assert config.enabled == True
        assert config.weight == 0.8
