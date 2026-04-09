# 已废弃配置文件

此目录包含已被 `config.yaml` 替代的旧配置文件。

## 迁移说明

所有配置已合并到 `config/config.yaml` (统一配置文件 v2.0)。

## 已废弃的文件

| 文件 | 合并到 config.yaml 的路径 | 废弃日期 |
|------|--------------------------|----------|
| app_config.yaml | trading, server, logging | 2026-04-05 |
| risk_config.yaml | risk_limits, stop_loss, take_profit | 2026-04-05 |
| strategy_params.json | strategy | 2026-04-05 |
| dynamic_exit_params.yaml | dynamic_exit | 2026-04-05 |
| position_strategy.yaml | trading.max_* | 2026-04-05 |
| system_config.yaml | base, monitoring | 2026-04-05 |
| market_regime_params.yaml | (待整合) | 2026-04-05 |
| alert_thresholds.yaml | notification.alerts | 2026-04-05 |

## 保留的文件

以下文件仍在使用，未被废弃：

| 文件 | 用途 |
|------|------|
| alert_rules.yaml | 告警规则（独立配置） |
| trading_rules_20260402.yaml | 交易规则（独立配置） |
| backtest_presets.json | 回测预设（引用配置） |
| watchlist.json | 监控股票列表（引用配置） |
| accounts.yaml | 账户配置（独立配置） |
| us_stock_config.yaml | 美股配置（独立配置） |
| version.yaml | 版本信息（独立配置） |

## 恢复方法

如需恢复某个配置文件，从 deprecated 目录复制回 config 目录：

```bash
cp deprecated/risk_config.yaml ../
```

## 删除计划

这些文件将在 2026-04-30 后永久删除，除非有恢复需求。

---
更新时间：2026-04-05
