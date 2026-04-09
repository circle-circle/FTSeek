# 🚀 FTSeek 快速参考

## 📊 市场状态识别 + 回测框架 - 已上线！

---

## 🔑 关键信息

| 项目 | 值 |
|------|------|
| API Token | `5a8f072c2cba9db1...` (64 字符) |
| API 端口 | `8899` |
| 服务状态 | ✅ 运行中 |
| 新增模块 | 市场状态识别、回测框架 |

---

## 📖 快速命令

### 运行回测示例
```bash
cd /opt/ftseek/FTSeek
python3.8 backtest_example.py
```

### API 测试
```bash
TOKEN="5a8f072c2cba9db1579dd829214a38112fdcddefe089d4ba58e05742097dd272"

# 列出策略
curl "http://localhost:8899/api/backtest/strategies?token=$TOKEN"

# 运行回测
curl "http://localhost:8899/api/backtest/run?strategy=rsi&days=100&token=$TOKEN"

# 多策略对比
curl "http://localhost:8899/api/backtest/compare?days=252&token=$TOKEN"
```

### 服务管理
```bash
systemctl status ftseek
systemctl restart ftseek
journalctl -u ftseek -f
```

---

## 💻 代码示例

### 市场状态识别
```python
from app.strategy.market_regime import market_regime_detector

detector = market_regime_detector
detector.update_data("HK.00700", kline)
regime = detector.detect("HK.00700")

print(f"市场状态：{regime.regime_type.value}")
print(f"置信度：{regime.confidence:.2f}")
```

### 回测
```python
from app.backtest.backtester import Backtester
from app.strategy.rsi_strategy import RSIStrategy

strategy = RSIStrategy({"period": 14, "overbought": 70, "oversold": 30})
backtester = Backtester(strategy, data, initial_capital=100000.0)
result = backtester.run()

print(result.metrics.summary())
```

---

## 📁 文档位置

| 文档 | 路径 |
|------|------|
| 实施总结 | `docs/IMPLEMENTATION_SUMMARY.md` |
| 回测指南 | `docs/BACKTEST_GUIDE.md` |
| 升级方案 | `docs/QUANT_UPGRADE_PLAN.md` |
| 市场状态 | `docs/MARKET_REGIME_AND_BACKTEST.md` |

---

## 🎯 核心功能

### 市场状态识别
- ✅ 7 种市场状态分类
- ✅ 趋势强度判断 (ADX)
- ✅ 波动率评估 (ATR%)
- ✅ 策略推荐引擎

### 回测框架
- ✅ 单策略回测
- ✅ 多策略对比
- ✅ 15+ 性能指标
- ✅ HTTP API 接口

---

## 📞 支持

遇到问题？查看文档或联系技术支持。

**版本**: v0.2.0  
**更新日期**: 2026-02-26
