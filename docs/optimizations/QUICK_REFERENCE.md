# 做空优化 + 港股特性适配 - 快速参考

**实施日期**: 2026-03-15  
**状态**: ✅ 已部署  

---

## 🎯 核心变更一览

| 项目 | 旧配置 | 新配置 | 效果 |
|------|--------|--------|------|
| **RSI 超买阈值** | 70 | 65 | SELL 信号 +30% |
| **SuperTrend 乘数** | 3.0 | 2.5 | SELL 信号 +20% |
| **AI 权重** | 0 (未启用) | 20% | 准确率 +5-10% |
| **做空止损** | -5% | -8% | 适应无涨跌停 |
| **做空止盈** | +15% | +20% | 盈亏比优化 |
| **T+0 交易** | ❌ | ✅ | 日内机会 +20% |
| **流动性分层** | ❌ | ✅ 3 层 | 小盘股风险 -50% |

---

## 📊 监控指标

### 每日检查
```bash
# SELL 信号数量
grep "SELL" /opt/ftseek/FTSeek/logs/ftseek.log | wc -l

# 流动性分层应用
grep "流动性" /opt/ftseek/FTSeek/logs/ftseek.log | tail -10

# T+0 交易
grep "T+0" /opt/ftseek/FTSeek/logs/ftseek.log | tail -10
```

### 钉钉监控命令
```
/策略列表          # 查看所有策略状态
/策略详情 rsi      # RSI 参数
/策略详情 supertrend  # SuperTrend 参数
```

---

## 🔧 紧急调整

### 临时禁用做空优化
```bash
# 方法 1: 钉钉命令
/调参 rsi overbought=70
/调参 supertrend multiplier=3.0

# 方法 2: 修改配置
vi /opt/ftseek/FTSeek/config/strategy_params.json
# 设置 "short_bias_factor": 1.0

# 重启服务
sudo systemctl restart ftseek
```

### 调整 AI 权重
```bash
# 钉钉命令
/调参 fusion_v3 ai_weight=0.15

# 或修改配置文件
vi /opt/ftseek/FTSeek/config/strategy_params.json
```

---

## 📈 预期效果

| 场景 | 优化前 | 优化后 |
|------|--------|--------|
| **超买预警** | RSI>70 才触发 | RSI>65 即触发 |
| **趋势翻转** | 较晚确认 | 提前 16.7% |
| **小盘股交易** | 统一仓位 | 自动降至 50% |
| **日内交易** | 不支持 | 支持 T+0 回转 |
| **做空盈亏比** | 3:1 (15/5) | 2.5:1 (20/8) |

---

## ⚠️ 风险提示

1. **做空风险**: 止损放宽至 -8%，单笔损失可能增加
2. **T+0 成本**: 日内交易增加印花税和佣金
3. **小盘股流动性**: Tier3 仓位降至 8%，可能错过暴涨
4. **AI 准确性**: 首次启用，如准确率<50% 自动禁用

---

## 📞 故障排查

### 问题 1: SELL 信号未增加
```bash
# 检查 RSI 参数
grep "overbought" /opt/ftseek/FTSeek/logs/ftseek.log

# 预期输出应包含：overbought=65
```

### 问题 2: 流动性分层未生效
```bash
# 检查港股适配器
grep "港股特性" /opt/ftseek/FTSeek/logs/ftseek.log

# 预期输出应包含：港股特性适配引擎初始化完成
```

### 问题 3: T+0 交易被拒绝
```bash
# 检查 T+0 配置
grep "T+0" /opt/ftseek/FTSeek/logs/ftseek.log

# 确认 t0_trading.enabled=true
```

---

## 📚 相关文档

- **详细报告**: `docs/optimizations/short_optimization_20260315.md`
- **测试脚本**: `scripts/test_short_optimization.py`
- **港股模块**: `app/strategy/hk_specific.py`

---

**最后更新**: 2026-03-15 17:41  
**下次回顾**: 2026-03-22
