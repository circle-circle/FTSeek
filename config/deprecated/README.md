# 已废弃配置文件

此目录保留用于存储被 `config/config.yaml` 替代的旧配置文件。

## 配置收敛说明 (2026-04-09)

所有配置已合并到 `config/config.yaml` (统一配置文件 v2.0)。

以下文件已删除：

| 文件 | 合并到 config.yaml 路径 | 删除日期 |
|------|------------------------|----------|
| app_config.yaml | trading, server, logging | 2026-04-09 |
| risk_config.yaml | risk_limits, stop_loss, take_profit | 2026-04-09 |
| strategy_params.json | strategy | 2026-04-09 |
| dynamic_exit_params.yaml | dynamic_exit | 2026-04-09 |
| position_strategy.yaml | trading.max_* | 2026-04-09 |
| system_config.yaml | base, monitoring | 2026-04-09 |
| monitoring_enhanced_v2.json | monitoring.metrics | 2026-04-09 |

## 恢复方法

如需恢复某个配置文件（用于参考或回滚），从 git 历史中恢复：

```bash
git show HEAD~5:config/risk_config.yaml > risk_config.yaml
```

## 当前架构

```
.env (敏感环境变量)
  ↓
config/config.yaml (唯一配置源)
  ↓
ConfigCenter (统一加载器)
```

---
更新时间：2026-04-09
