# FTSeek 升级实施计划 - Phase 1 & 2

**创建日期**: 2026-03-14  
**版本**: V1.0  
**状态**: 待执行  
**保护策略**: 特性开关 + 并行模块 (现有功能 100% 保留)

---

## 📋 项目概览

### 升级范围
- **Phase 1**: 因子库扩展 (资金流 + 情绪因子)
- **Phase 2**: ML 模型集成 (XGBoost + LSTM)
- **周期**: 3-5 周
- **风险**: 低 (并行模块 + 特性开关保护)

### 核心原则
1. **现有功能零影响**: 所有新代码在独立目录
2. **TDD  mandatory**: 每个任务先写测试
3. **渐进式启用**: 通过环境变量控制
4. **完整测试**: 保留 930+ 现有测试 + 新增测试

---

## 🎯 Phase 1: 因子库扩展 (1-2 周)

### Task 1.1: 创建因子库基础架构

**目标**: 建立因子库框架，不影响现有策略

**文件**:
- `app/factors/__init__.py`
- `app/factors/base.py`
- `app/factors/config.py`

**测试**:
- `tests/factors/test_base.py`

**验收标准**:
- [ ] 测试通过
- [ ] 无现有代码修改
- [ ] 可独立导入

**预计**: 2 小时

---

### Task 1.2: 资金流因子实现

**目标**: 实现北向资金和机构资金流因子

**文件**:
- `app/factors/capital_flow.py`

**功能**:
```python
class CapitalFlowFactors:
    def northbound_flow(code: str, window: int = 5) -> float
    def institutional_flow(code: str, window: int = 10) -> float
    def money_flow_index(code: str, period: int = 14) -> float
```

**测试**:
- `tests/factors/test_capital_flow.py`
- 测试用例：10+

**验收标准**:
- [ ] 单元测试覆盖率 > 90%
- [ ] 与现有 K 线数据兼容
- [ ] 性能：单股票 < 100ms

**预计**: 4 小时

---

### Task 1.3: 情绪因子实现

**目标**: 实现新闻情绪和社交媒体情绪因子

**文件**:
- `app/factors/sentiment.py`

**功能**:
```python
class SentimentFactors:
    def news_sentiment(code: str, days: int = 3) -> float
    def social_sentiment(code: str) -> float
    def sentiment_score(text: str) -> float  # 基于简单词典
```

**数据源**:
- 默认：使用简单词典法 (无外部依赖)
- 可选：配置新闻 API

**测试**:
- `tests/factors/test_sentiment.py`
- 测试用例：8+

**验收标准**:
- [ ] 单元测试通过
- [ ] 无 API 依赖可运行
- [ ] 情绪评分范围 [-1, 1]

**预计**: 4 小时

---

### Task 1.4: ESG 因子实现

**目标**: 实现 ESG 评分因子

**文件**:
- `app/factors/esg.py`

**功能**:
```python
class ESGFactors:
    def esg_score(code: str) -> float
    def esg_trend(code: str) -> float  # ESG 改善趋势
```

**数据源**:
- 使用静态数据文件 (初始)
- 支持后续接入外部 API

**测试**:
- `tests/factors/test_esg.py`

**验收标准**:
- [ ] 单元测试通过
- [ ] 数据文件可加载

**预计**: 2 小时

---

### Task 1.5: 因子正交化和标准化

**目标**: 因子预处理，去除共线性

**文件**:
- `app/factors/preprocessing.py`

**功能**:
```python
class FactorPreprocessor:
    def standardize(factors: pd.DataFrame) -> pd.DataFrame
    def orthogonalize(factors: pd.DataFrame, method: str = 'gram_schmidt') -> pd.DataFrame
    def pca_reduce(factors: pd.DataFrame, n_components: int = 5) -> pd.DataFrame
```

**测试**:
- `tests/factors/test_preprocessing.py`

**验收标准**:
- [ ] 正交化后因子相关性 < 0.1
- [ ] 标准化后均值=0, 标准差=1

**预计**: 3 小时

---

### Task 1.6: 因子库集成测试

**目标**: 完整因子库集成验证

**文件**:
- `tests/factors/test_integration.py`

**测试场景**:
- 单因子计算
- 多因子组合
- 与现有策略兼容

**验收标准**:
- [ ] 所有因子可正常调用
- [ ] 性能：100 股票 < 5 秒
- [ ] 内存占用 < 500MB

**预计**: 3 小时

---

### Task 1.7: 特性开关配置

**目标**: 通过环境变量控制新功能启用

**文件**:
- `app/config.py` (新增配置项)
- `.env.example` (更新示例)

**配置项**:
```bash
# 因子库开关
FTSEEK_ENABLE_ALTERNATIVE_FACTORS=false
FTSEEK_FACTOR_WEIGHTS={"northbound": 0.3, "sentiment": 0.3, "esg": 0.2}
```

**测试**:
- `tests/test_config_flags.py`

**验收标准**:
- [ ] 开关关闭时不影响现有功能
- [ ] 开关开启时新因子生效

**预计**: 2 小时

---

## 🎯 Phase 2: ML 模型集成 (2-3 周)

### Task 2.1: ML 基础架构

**目标**: 建立 ML 模块框架

**文件**:
- `app/ml/__init__.py`
- `app/ml/base.py`
- `app/ml/config.py`

**依赖**:
- scikit-learn>=1.2.0
- xgboost>=1.7.0
- lightgbm>=3.3.0

**测试**:
- `tests/ml/test_base.py`

**验收标准**:
- [ ] 依赖安装成功
- [ ] 模块可导入
- [ ] 无现有代码冲突

**预计**: 3 小时

---

### Task 2.2: 特征工程模块

**目标**: 实现特征提取和选择

**文件**:
- `app/ml/feature_engineering.py`

**功能**:
```python
class FeatureEngineer:
    def create_features(df: pd.DataFrame) -> pd.DataFrame
    def select_features(X, y, method: str = 'mutual_info') -> List[str]
    def create_lag_features(df: pd.DataFrame, lags: List[int] = [1, 2, 5]) -> pd.DataFrame
```

**特征类型**:
- 技术指标 (RSI, MACD, Bollinger)
- 价量特征 (收益率、波动率)
- 滞后特征 (t-1, t-2, t-5)
- 滚动统计 (均值、标准差、偏度、峰度)

**测试**:
- `tests/ml/test_feature_engineering.py`
- 测试用例：15+

**验收标准**:
- [ ] 特征工程管道完整
- [ ] 特征数量 > 50 个
- [ ] 无数据泄露

**预计**: 6 小时

---

### Task 2.3: XGBoost 预测模型

**目标**: 实现 XGBoost 集成预测

**文件**:
- `app/ml/xgb_predictor.py`

**功能**:
```python
class XGBoostPredictor:
    def fit(X: pd.DataFrame, y: pd.Series)
    def predict(X: pd.DataFrame) -> np.ndarray
    def predict_proba(X: pd.DataFrame) -> np.ndarray
    def get_feature_importance() -> pd.DataFrame
```

**超参数**:
```python
{
    'max_depth': 6,
    'learning_rate': 0.05,
    'n_estimators': 500,
    'subsample': 0.8,
    'colsample_bytree': 0.8
}
```

**测试**:
- `tests/ml/test_xgb_predictor.py`

**验收标准**:
- [ ] 训练收敛
- [ ] 测试集准确率 > 55%
- [ ] 特征重要性可解释

**预计**: 6 小时

---

### Task 2.4: LightGBM 预测模型

**目标**: 实现 LightGBM 预测 (更快训练)

**文件**:
- `app/ml/lgb_predictor.py`

**功能**:
```python
class LightGBMPredictor:
    def fit(X: pd.DataFrame, y: pd.Series)
    def predict(X: pd.DataFrame) -> np.ndarray
```

**优势**:
- 训练速度比 XGBoost 快 3-5 倍
- 支持类别特征

**测试**:
- `tests/ml/test_lgb_predictor.py`

**验收标准**:
- [ ] 训练时间 < XGBoost 的 50%
- [ ] 准确率与 XGBoost 相当 (±2%)

**预计**: 4 小时

---

### Task 2.5: LSTM 时序预测模型

**目标**: 实现深度学习时序预测

**文件**:
- `app/ml/lstm_predictor.py`

**依赖**:
- torch>=2.0.0

**架构**:
```python
LSTM(
    input_dim=50,
    hidden_dim=64,
    num_layers=2,
    dropout=0.2
) -> Dense(3) -> Softmax  # 涨/平/跌
```

**测试**:
- `tests/ml/test_lstm_predictor.py`

**验收标准**:
- [ ] 模型可训练
- [ ] 验证集准确率 > 50%
- [ ] 无过拟合 (训练/验证差距 < 5%)

**预计**: 8 小时

---

### Task 2.6: 集成预测器

**目标**: 集成多个 ML 模型

**文件**:
- `app/ml/ensemble_predictor.py`

**功能**:
```python
class EnsemblePredictor:
    def __init__(self):
        self.models = {
            'xgb': XGBoostPredictor(),
            'lgb': LightGBMPredictor(),
            'lstm': LSTMPredictor()
        }
    
    def fit(all_models: bool = True)
    def predict(X: pd.DataFrame, method: str = 'weighted_avg') -> np.ndarray
    def predict_proba(X: pd.DataFrame) -> Dict[str, float]
```

**集成方法**:
- 简单平均
- 加权平均 (根据历史表现)
- Stacking (元学习器)

**测试**:
- `tests/ml/test_ensemble_predictor.py`

**验收标准**:
- [ ] 集成准确率 > 单模型
- [ ] 可配置集成权重

**预计**: 6 小时

---

### Task 2.7: ML 与 FusionV2 融合

**目标**: 将 ML 预测集成到现有 FusionV2 策略

**文件**:
- `app/strategy/fusion_v3.py` (新文件，不影响 fusion_v2.py)

**融合逻辑**:
```python
# 技术面 40% + 情绪面 30% + ML 预测 30%
final_score = (
    technical_score * 0.4 +
    sentiment_score * 0.3 +
    ml_score * 0.3
)
```

**特性开关**:
```bash
FTSEEK_ENABLE_ML_FUSION=false  # 默认关闭
FTSEEK_ML_WEIGHT=0.3
```

**测试**:
- `tests/strategy/test_fusion_v3.py`

**验收标准**:
- [ ] FusionV2 完全不受影响
- [ ] FusionV3 可选启用
- [ ] 融合后信号质量提升 > 10%

**预计**: 6 小时

---

### Task 2.8: ML 模型持久化

**目标**: 模型保存和加载

**文件**:
- `app/ml/model_store.py`

**功能**:
```python
class ModelStore:
    def save(model, path: str)
    def load(path: str) -> model
    def list_models() -> List[str]
```

**格式**:
- XGBoost/LightGBM: `.pkl` 或原生格式
- LSTM: `.pth` (PyTorch)

**测试**:
- `tests/ml/test_model_store.py`

**验收标准**:
- [ ] 保存后可正确加载
- [ ] 加载后预测结果一致

**预计**: 3 小时

---

### Task 2.9: ML 训练管道

**目标**: 自动化训练流程

**文件**:
- `scripts/train_ml_models.py`

**功能**:
```bash
# 训练所有模型
python3 scripts/train_ml_models.py --all

# 训练单个模型
python3 scripts/train_ml_models.py --model xgb

# 验证模型
python3 scripts/train_ml_models.py --validate
```

**输出**:
- 训练日志
- 模型文件
- 性能报告

**测试**:
- 手动验证

**验收标准**:
- [ ] 一键训练所有模型
- [ ] 训练过程可监控

**预计**: 4 小时

---

### Task 2.10: ML 集成测试

**目标**: 完整 ML 流程验证

**文件**:
- `tests/ml/test_integration.py`

**测试场景**:
- 特征工程 → 模型训练 → 预测 → 融合

**验收标准**:
- [ ] 端到端流程通畅
- [ ] 性能：100 股票预测 < 10 秒
- [ ] 内存占用 < 1GB

**预计**: 4 小时

---

## 📊 测试计划

### 现有测试保护
- **保留**: 所有 930+ 现有测试
- **验证**: 每次提交后运行现有测试
- **零破坏**: 不允许现有测试失败

### 新增测试
| Phase | 新增测试文件 | 新增测试用例 |
|-------|-------------|-------------|
| Phase 1 | 7 个 | 50+ |
| Phase 2 | 10 个 | 80+ |
| **总计** | **17 个** | **130+** |

### 测试覆盖率目标
- **新增代码**: > 90%
- **整体项目**: > 75%

---

## 🚀 执行顺序

### Week 1 (Phase 1)
```
Day 1-2: Task 1.1-1.3 (基础架构 + 资金流 + 情绪)
Day 3:   Task 1.4-1.5 (ESG + 预处理)
Day 4:   Task 1.6-1.7 (集成测试 + 特性开关)
Day 5:   模拟盘验证 + Bug 修复
```

### Week 2-4 (Phase 2)
```
Week 2 Day 1-2: Task 2.1-2.3 (ML 基础 + 特征工程 + XGBoost)
Week 2 Day 3-5: Task 2.4-2.6 (LightGBM + LSTM + 集成)

Week 3 Day 1-2: Task 2.7-2.8 (FusionV3 融合 + 模型持久化)
Week 3 Day 3-4: Task 2.9-2.10 (训练管道 + 集成测试)
Week 3 Day 5:   全面测试 + Bug 修复

Week 4:         模拟盘验证 2 周 + 性能优化
```

---

## ✅ 验收标准汇总

### Phase 1 验收
- [ ] 所有因子单元测试通过
- [ ] 因子库性能达标 (100 股票 < 5 秒)
- [ ] 特性开关正常工作
- [ ] 现有 930+ 测试零失败
- [ ] 模拟盘运行 1 周无异常

### Phase 2 验收
- [ ] 所有 ML 模型可训练和预测
- [ ] 集成预测准确率 > 单模型
- [ ] FusionV3 与 FusionV2 完全隔离
- [ ] 新增 130+ 测试全部通过
- [ ] 现有 930+ 测试零失败
- [ ] 模拟盘运行 2 周，信号质量提升 > 15%

---

## 🔧 技术栈

### 新增依赖
```txt
# requirements.ml
scikit-learn>=1.2.0
xgboost>=1.7.0
lightgbm>=3.3.0
torch>=2.0.0  # LSTM
pandas>=2.0.0
numpy>=1.24.0
```

### 安装命令
```bash
pip3 install -r requirements.ml
```

---

## 📝 提交规范

### Git Commit 格式
```
feat(factors): 添加资金流因子

- 实现北向资金流因子
- 实现机构资金流因子
- 添加单元测试 (覆盖率 95%)

Closes #1
```

### Branch 命名
- `feature/factors-phase1`
- `feature/ml-phase2`

---

## 🎯 风险控制

### 风险 1: 现有功能受影响
**缓解**: 并行模块 + 特性开关 + 完整回归测试

### 风险 2: ML 模型效果不佳
**缓解**: A/B 测试 + 模拟盘验证 + 可回滚

### 风险 3: 性能下降
**缓解**: 性能基准测试 + 优化 + 缓存

### 风险 4: 数据依赖问题
**缓解**: 降级方案 (无 API 也可运行)

---

## 📈 成功指标

### 技术指标
- [ ] 信号质量提升 > 15%
- [ ] 预测准确率 > 60%
- [ ] 组合波动率降低 > 20%
- [ ] 现有测试 100% 通过

### 业务指标
- [ ] 模拟盘夏普比率 > 1.8
- [ ] 最大回撤 < -10%
- [ ] 胜率 > 65%

---

*实施计划版本: V1.0*  
*创建时间：2026-03-14*  
*状态：待执行*  
*下一步：Phase 3 - Subagent-Driven Development*
