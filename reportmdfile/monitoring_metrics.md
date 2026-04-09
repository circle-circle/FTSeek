# FTSeek Fusion 策略监控指标

## 监控指标概览

### 1. 融合策略胜率
- **指标名称**: `fusion_strategy_win_rate`
- **目标值**: 60-65%
- **计算方式**: 
  - 失败信号: confidence > 0.60 && pnl < 0
  - 成功信号: confidence > 0.60 && pnl > 0
  - 胜率 = 成功 / (成功 + 失败)
- **告警阈值**:
  - ⚠️ 跌破 60% (提醒优化)
  - ❌ 低于 55% (强制暂停)

### 2. 止损触发率
- **指标名称**: `stop_loss_trigger_rate`
- **ATR 乘数**: 2.5× (修复方案)
- **目标值**: < 20%
- **计算方式**:
  - 止损触发次数 / 总交易次数
- **告警阈值**:
  - ⚠️ 超过 25% (检查止损逻辑)
  - ❌ 超过 30% (暂停策略)

### 3. 冲突解决成功率
- **指标名称**: `conflict_resolution_success_rate`
- **计算方式**:
  - 无冲突信号次数 / 总信号次数
  - 冲突定义: 同一窗口内同一策略方向反转
- **目标值**: > 85%
- **告警阈值**:
  - ⚠️ 低于 80% (检查信号质量)
  - ❌ 低于 70% (暂停策略)

### 4. 置信度分布
- **指标名称**: `confidence_distribution`
- **分类**:
  - 低置信度 (< 0.31): 不触发
  - 中置信度 (0.31-0.50): 观察
  - 高置信度 (0.50-0.70): 推荐
  - 极高置信度 (> 0.70): 强烈推荐

### 5. 融合策略权重
- **技术权重**: 0.5 (修复方案)
- **情绪权重**: 0.5 (修复方案)
- **更新周期**: 每日收盘后

## 监控告警规则

### 告警等级定义

| 等级 | 条件 | 响应actions |
|------|------|-------------|
| **INFO** | 正常波动 | 日志记录 |
| **WARNING** | 指标连续3天偏离目标 | 邮件提醒 |
| **CRITICAL** | 指标跌破关键阈值 | 自动暂停策略 + 钉钉告警 |

### 告警规则配置

```yaml
# FTSeek 监控告警规则
rules:
  - name: fusion_win_rate
    description: "融合策略胜率监控"
    metrics:
      - fusion_strategy_win_rate
    threshold:
      warning: 0.60
      critical: 0.55
    duration: 3d
    actions:
      - log
      - email
      - dingtalk

  - name: stop_loss_rate
    description: "止损触发率监控"
    metrics:
      - stop_loss_trigger_rate
    threshold:
      warning: 0.25
      critical: 0.30
    duration: 1d
    actions:
      - log
      - email
      - dingtalk

  - name: confidence_distribution
    description: "置信度分布监控"
    metrics:
      - confidence_distribution
    threshold:
      low_confidence_ratio: < 0.31 = 0.30
    duration: 1d
    actions:
      - log

  - name: conflict_resolution
    description: "冲突解决成功率监控"
    metrics:
      - conflict_resolution_success_rate
    threshold:
      warning: 0.80
      critical: 0.70
    duration: 3d
    actions:
      - log
      - email
```

## 仪表盘指标（可选）

如需在 Grafana/Dashboards 中可视化，可使用以下指标：

```
ftseek_fusion_win_rate{strategy="fusion_v2"}
ftseek_stop_loss_rate{strategy="fusion_v2"}
ftseek_conflict_success_rate{strategy="fusion_v2"}
ftseek_confidence_avg{strategy="fusion_v2"}
```

## 日志格式

### 修复方案验证日志

```
[共识] {code} BUY 置信度计算 | 技术项={technical_part:.2f} | 情绪项={sentiment_part:.2f} | 资金流={capital_sentiment:.2f} | 最终={confidence:.2f}
```

### 监控指标日志

```
[监控] {datetime} | fusion_v2 | win_rate={:.2%} | stop_loss_rate={:.2%} | conflict_rate={:.2%} | confidence_avg={:.2f}
```

## 策略暂停/恢复规则

### 自动暂停条件

1. **胜率连续3天 < 60%**
2. **止损率连续2天 > 25%**
3. **置信度均值 < 0.35**
4. **冲突率 > 30%**

### 手动恢复条件

1. 胜率 > 60% 持续1天
2. 止损率 < 20% 持续1天
3. 配置文件手动启用 `enabled=True`

---

*生成时间: 2026-03-31*
*版本: v2.0 (修复方案)*
