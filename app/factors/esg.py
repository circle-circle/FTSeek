"""
ESG 因子模块

使用静态 ESG 评分数据 (后续可扩展 API)
"""
from .base import BaseFactor, FactorResult
from typing import Optional, Dict

ESG_DATA = {
    'HK.00700': {'esg_score': 75, 'trend': 'improving'},
    'HK.9988': {'esg_score': 68, 'trend': 'stable'},
    'US.AAPL': {'esg_score': 82, 'trend': 'improving'},
}


class ESGFactors(BaseFactor):
    """ESG 因子"""
    
    name = "esg"
    
    def calculate(self, code: str, data: Dict) -> Optional[FactorResult]:
        """计算 ESG 因子"""
        esg_info = ESG_DATA.get(code, {'esg_score': 50, 'trend': 'unknown'})
        score = (esg_info['esg_score'] - 50) / 50
        
        return FactorResult(
            code=code,
            factor_name='esg',
            value=score,
            timestamp=data.get('timestamp', ''),
            metadata=esg_info
        )
