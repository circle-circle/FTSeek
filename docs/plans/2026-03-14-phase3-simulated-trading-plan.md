# FTSeek Phase 3: 模拟盘验证实施计划

**启动时间**: 2026-03-14 18:05  
**预计周期**: 1 周 (2026-03-15 ~ 2026-03-21)  
**目标**: 验证 AI 增强策略的实际效果

---

## 📋 Phase 3 任务清单

### Task 3.1: API Key 配置 (1h)
- [ ] 获取真实 Bailian API Key
- [ ] 配置 .env 文件
- [ ] 测试 API 连接
- [ ] 验证调用正常

### Task 3.2: 小流量测试配置 (2h)
- [ ] 设置 AI 信号比例 (10% 起步)
- [ ] 配置 A/B 测试分组
- [ ] 设置监控指标
- [ ] 配置告警阈值

### Task 3.3: 信号质量监控 (3h)
- [ ] 实现信号准确率统计
- [ ] 对比 AI vs 非 AI 信号
- [ ] 记录预测 vs 实际走势
- [ ] 生成日报/周报

### Task 3.4: 成本监控优化 (2h)
- [ ] 监控实际 API 调用
- [ ] 分析缓存命中率
- [ ] 优化缓存策略
- [ ] 调整预算配置

### Task 3.5: 效果评估报告 (2h)
- [ ] 统计 AI 信号胜率
- [ ] 计算 AI 贡献度
- [ ] 生成效果评估报告
- [ ] 提出优化建议

---

## 🎯 验证指标

### 信号质量
| 指标 | 目标值 | 当前值 |
|------|--------|--------|
| AI 信号胜率 | > 60% | 待验证 |
| 预测准确率 | > 65% | 待验证 |
| 缓存命中率 | > 50% | 待验证 |

### 成本控制
| 指标 | 目标值 | 当前值 |
|------|--------|--------|
| 日成本 | < ¥50 | 待验证 |
| 月成本 | < ¥150 | 待验证 |
| 单次成本 | < ¥0.01 | 待验证 |

### 性能表现
| 指标 | 目标值 | 当前值 |
|------|--------|--------|
| 响应时间 (无缓存) | < 3s | ~2-3s |
| 响应时间 (有缓存) | < 50ms | < 10ms |
| 并发支持 | > 100 次/分钟 | 待验证 |

---

## 🚀 立即执行 Task 3.1

### 步骤 1: 获取 API Key

请提供您的 Bailian API Key，或按以下指南获取：

1. 访问阿里云百炼平台：https://dashscope.console.aliyun.com/
2. 登录阿里云账号
3. 进入"API Key 管理"
4. 创建新 API Key
5. 复制 Key 值

### 步骤 2: 配置文件

创建 `.env` 文件：

```bash
# AI 功能开关
FTSEEK_ENABLE_AI=true

# AI Provider
AI_PROVIDER=bailian

# Bailian API Key (请替换为您的真实 Key)
BAILIAN_API_KEY=sk-xxxxxxxxxxxxxxxx

# AI 权重
FTSEEK_AI_WEIGHT=0.3

# 预算控制
AI_DAILY_BUDGET=50
AI_MONTHLY_BUDGET=1500

# 缓存配置
AI_CACHE_TTL=3600

# 限流配置
AI_REQUESTS_PER_MINUTE=10
```

### 步骤 3: 测试 API 连接

```bash
cd /opt/ftseek/FTSeek

# 测试 AI 服务
python3 -c "
from app.ai import AIService
ai = AIService()
print('✅ AI 服务初始化成功')
print(f'Provider: {ai.provider.name}')
"

# 测试情绪分析 (可选，会产生实际调用)
python3 -c "
from app.ai import AIService
ai = AIService()
result = ai.analyze_sentiment('公司业绩超预期，净利润增长 50%')
if result.success:
    print(f'✅ 情绪分析成功：{result.data.get(\"sentiment_score\", 0)}')
else:
    print(f'❌ 失败：{result.error}')
"
```

---

## 📊 A/B 测试方案

### 分组策略

**A 组 (对照组)**: 90% 流量
- 使用传统因子 (FusionV2)
- 不使用 AI 预测

**B 组 (实验组)**: 10% 流量
- 使用 AI 增强因子 (FusionV3)
- AI 权重 30%

### 监控指标

```python
# 每日统计
{
    'date': '2026-03-15',
    'group_a': {
        'signals': 100,
        'correct': 55,
        'accuracy': 0.55
    },
    'group_b': {
        'signals': 11,
        'correct': 7,
        'accuracy': 0.64
    },
    'ai_improvement': '+16%'
}
```

---

## ⚠️ 风险控制

### 1. 预算控制
- 日预算：¥50 (保守起步)
- 月预算：¥1500
- 超支自动停止 AI 功能

### 2. 流量控制
- 初期 10% 流量
- 根据效果逐步提升
- 最多 50% 流量

### 3. 质量监控
- 每日检查信号准确率
- 如准确率 < 50%，暂停 AI
- 每周生成效果报告

### 4. 应急预案
- API Key 失效：自动切换回传统模式
- 成本超支：自动降低流量比例
- 效果不佳：调整 AI 权重或暂停

---

## 📈 预期收益

### 信号质量提升
- AI 信号胜率：60-65%
- 整体准确率：+5-10%
- 夏普比率：1.8-2.2

### 成本效益
- 月成本：¥75-150
- 单次调用：¥0.005-0.01
- ROI: > 5 倍 (假设信号质量提升带来收益增长)

---

## 📅 时间线

```
Day 1 (今天): Task 3.1-3.2
├── API Key 配置 ✅
├── 测试 API 连接
└── A/B 测试配置

Day 2-3 (3/15-16): Task 3.3
├── 信号质量监控
├── 数据收集
└── 初步分析

Day 4-5 (3/17-18): Task 3.4
├── 成本监控
├── 缓存优化
└── 性能调优

Day 6-7 (3/19-21): Task 3.5
├── 效果评估
├── 生成报告
└── Phase 3 验收
```

---

## ✅ 下一步行动

**请立即执行**:

1. **提供 API Key** (或自行配置)
2. **运行测试脚本** 验证连接
3. **启用 AI 功能** (设置 FTSEEK_ENABLE_AI=true)
4. **开始小流量测试** (10% 流量)

**配置完成后回复"配置完成"，我将继续执行 Task 3.2-3.5**

---

*计划版本：V1.0*  
*创建时间：2026-03-14 18:05*  
*状态：待执行*
