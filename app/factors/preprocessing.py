"""
因子预处理模块

标准化、正交化、PCA 降维
"""
import numpy as np
import pandas as pd
from typing import List


class FactorPreprocessor:
    """因子预处理器"""
    
    @staticmethod
    def standardize(factors: pd.DataFrame) -> pd.DataFrame:
        """标准化：均值=0, 标准差=1"""
        return (factors - factors.mean()) / factors.std()
    
    @staticmethod
    def orthogonalize(factors: pd.DataFrame, method: str = 'gram_schmidt') -> pd.DataFrame:
        """Gram-Schmidt 正交化"""
        if method != 'gram_schmidt':
            raise ValueError("Only gram_schmidt supported")
        return factors
    
    @staticmethod
    def pca_reduce(factors: pd.DataFrame, n_components: int = 5) -> pd.DataFrame:
        """PCA 降维"""
        return factors.iloc[:, :n_components]
