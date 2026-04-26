# FTSeek 配置速查表

> **配置文件**: `config/config.yaml` (1361行)  
> **版本**: 2.0.0 | **更新**: 2026-04-05  
> **生成时间**: 2026-04-11

---

## 📊 配置概览

| 指标 | 数值 | 说明 |
|-----|------|------|
| 顶级配置项 | 37 | 按功能分为12个章节 |
| 总配置键数 | 905 | 包含所有嵌套配置 |
| 最大嵌套深度 | 5 | 合理的嵌套层级 |
| 重复配置组 | 6 | 已全部验证一致 ✅ |

---

## 🗂️ 配置章节索引

### 1️⃣ 基础配置 (4项)
**位置**: L16-L26  
**内容**: 环境、日志级别、调试模式

```yaml
version: "2.0.0"
base:
  environment: production
  log_level: INFO
  debug_mode: false
```

**常用修改**:
- `base.log_level`: 调试时改为 `DEBUG`
- `base.debug_mode`: 生产环境保持 `false`

---

### 2️⃣ 交易连接 (17项)
**位置**: L31-L47 + server配置  
**内容**: 富途OpenD连接、API服务器

```yaml
futu:
  host: "127.0.0.1"
  port: 11111
  trade_env: 1              # 0=实盘，1=模拟盘
  force_simulate: true      # 强制模拟
  allow_real_trading: false # 安全锁
```

**⚠️ 关键配置**:
- `trade_env`: 切换实盘/模拟盘
- `allow_real_trading`: 生产环境设为 `true` 前需三重确认

---

### 3️⃣ 交易规则 (20+项)
**位置**: L52-L171  
**内容**: 股票池、仓位、订单配置

#### 股票池 (L57-L82)
- **监控股票**: 20只港股 (互联网科技、新能源车、金融等)
- **跨市场套利**: 包含ADR对应关系

#### 仓位限制 (L84-L88)
```yaml
max_single_position: 0.20   # 单标的最大20%
max_total_position: 0.60    # 总仓位最大60%
max_open_positions: 4       # 最多同时持仓4只
default_position_size: 0.10 # 默认每只10%
```

#### 订单配置 (L103-L164)
- **每手股数**: 按股票配置 (`hk_lot_sizes`)
- **默认单量**: 港股100股，美股1股
- **最大单笔**: 12万港币

**常用修改**:
- 添加新股票到 `watch_list_hk`
- 调整 `max_open_positions` 改变持仓分散度
- 修改 `default_position_size` 调整单笔仓位

---

### 4️⃣ 风控体系 (25项) ⭐⭐⭐⭐⭐
**位置**: L183-L250  
**内容**: 五层风控体系

| 层级 | 配置项 | 说明 |
|-----|-------|------|
| **信号级** | `min_confidence` | 最小置信度0.50 |
| **订单级** | `max_single_position` | 单笔最大20% |
| **仓位级** | `max_daily_trades` | 日交易≤10次 |
| **账户级** | `max_drawdown` | 最大回撤12% |
| **系统级** | `circuit_breaker` | 熔断机制 |

#### 止损配置 (L202-L215)
```yaml
stop_loss:
  fixed_stop_loss: 0.08        # 固定止损8%
  trailing_stop_loss: 0.05     # 移动止损5%
  time_stop_loss: 10           # 时间止损10天
```

#### 止盈配置 (L217-L235)
```yaml
take_profit:
  fixed_take_profit: 0.15      # 固定止盈15%
  trailing_take_profit: 0.08   # 移动止盈8%
  partial_exit_levels:         # 分批止盈
    - target: 0.10
      exit_ratio: 0.30
    - target: 0.20
      exit_ratio: 0.50
```

**⚠️ 关键配置**:
- `max_drawdown`: 触及后系统暂停交易
- `circuit_breaker.enabled`: 生产环境必须 `true`

---

### 5️⃣ 策略引擎 (45项)
**位置**: L252-L500  
**内容**: 共识引擎、策略参数、动态退出

#### 共识引擎 (L260-L320)
```yaml
strategy:
  consensus:
    window: 300                # 共识窗口5分钟
    threshold: 0.60            # 触发阈值60%
    adx_trend: 25              # ADX趋势阈值
    adx_range: 20              # ADX震荡阈值
```

#### 动态退出 (L350-L380)
```yaml
dynamic_exit:
  enabled: true
  atr_multiplier: 2.5         # ATR倍数止损
  rsi_overbought: 70          # RSI超卖
  rsi_oversold: 30            # RSI超买
```

**常用修改**:
- 调整 `consensus.threshold` 改变信号触发敏感度
- 修改 `dynamic_exit.atr_multiplier` 调整止损宽松度

---

### 6️⃣ 通知告警 (5项)
**位置**: L502-L530  
**内容**: 钉钉通知、监控告警

```yaml
notification:
  dingtalk:
    enabled: true
    webhook: "${DINGTALK_WEBHOOK}"
    
monitoring:
  health_check_interval: 60    # 健康检查60秒
  metrics_export: true
```

---

### 7️⃣ 数据持久化 (10项)
**位置**: L532-L560  
**内容**: SQLite数据库、缓存

```yaml
database:
  path: "data/ftseek.db"
  pool_size: 5                 # 连接池大小
  wal_mode: true               # WAL模式
  
persistence:
  signal_retention: 90         # 信号保留90天
  trade_retention: 365         # 交易记录保留1年
```

---

### 8️⃣ AI与智能 (13项)
**位置**: L562-L600  
**内容**: AI情绪分析、信号质量评分

```yaml
ai:
  provider: bailian            # 阿里云百炼
  model: qwen-plus
  max_tokens: 2000
  
quality_scorer:
  enabled: true
  min_score: 0.60              # 最低质量分0.60
```

---

### 9️⃣ 调度与定时 (4项)
**位置**: L602-L620  
**内容**: 定时任务配置

```yaml
schedule:
  pre_market_check: "09:00"    # 盘前检查
  daily_report: "16:30"        # 日报生成
  cleanup_old_data: "02:00"    # 数据清理
```

---

### 🔟 日志与调试 (17项)
**位置**: L622-L660  
**内容**: 日志配置、特性开关

```yaml
logging:
  level: INFO
  rotation: 100MB              # 日志轮转100MB
  retention: 30d               # 保留30天
  
features:
  enable_news_push: true
  enable_cross_market: true
```

---

## ⚠️ 重复配置说明

以下配置项在 `trading` 和 `risk_limits` 中都有定义，**已验证值一致**：

| 配置项 | 值 | 用途 |
|-------|-----|------|
| `max_single_position` | 0.20 | 单标的最大仓位 |
| `max_total_position` | 0.60 | 总仓位上限 |
| `max_daily_trades` | 10 | 日交易次数限制 |
| `max_daily_loss` | 0.03 | 日亏损限制 |
| `max_weekly_loss` | 0.08 | 周亏损限制 |
| `min_signal_confidence` | 0.50 | 最小信号置信度 |

**为什么重复？**
- `trading.*`: 交易规则层面 (业务逻辑)
- `risk_limits.*`: 风控层面 (风控检查)
- 两处保持一致是为了**冗余校验**，提高安全性

---

## 🎯 常用配置修改场景

### 场景1: 添加新监控股票
```yaml
# 修改 L57-L82: trading.watch_list_hk
trading:
  watch_list_hk:
    - "HK.00700"  # 腾讯
    - "HK.XXXXX"  # 新股票
```

### 场景2: 调整仓位策略
```yaml
# 修改 L84-L88
trading:
  max_open_positions: 6        # 从4改为6
  default_position_size: 0.08  # 从10%改为8%
```

### 场景3: 切换实盘交易
```yaml
# 修改 L41-L47
futu:
  trade_env: 0                 # 改为0 (实盘)
  force_simulate: false        # 关闭强制模拟
  allow_real_trading: true     # ⚠️ 三重确认后开启
```

### 场景4: 调整风控参数
```yaml
# 修改 L202-L235
stop_loss:
  fixed_stop_loss: 0.10        # 从8%改为10%
  
take_profit:
  fixed_take_profit: 0.20      # 从15%改为20%
```

### 场景5: 开启调试模式
```yaml
# 修改 L24-L26
base:
  log_level: DEBUG
  debug_mode: true
```

---

## 🔍 配置查询技巧

### 查看完整配置索引
```bash
cat config/config_index.yaml
```

### 查看详细配置文档
```bash
cat docs/CONFIG_GUIDE.md
```

### 重新生成配置文档
```bash
python3 -m app.config.auditor
```

### 查找特定配置
```bash
# 查找所有止损相关配置
grep -n "stop_loss" config/config.yaml

# 查找某只股票的配置
grep -n "00700" config/config.yaml
```

---

## 📝 配置维护建议

### ✅ 最佳实践
1. **修改前备份**: `cp config/config.yaml config/config.yaml.backup`
2. **使用环境变量**: 敏感信息用 `${ENV_VAR}` 格式
3. **保持一致性**: 修改重复配置时，确保两处同步更新
4. **版本控制**: 每次修改后更新 `updated` 字段
5. **测试验证**: 修改后运行 `python3 -m app.config.auditor` 检查

### ❌ 避免事项
1. **不要直接删除配置项**: 可能导致代码引用失败
2. **不要混合Tab和空格**: YAML格式敏感
3. **不要忘记注释**: 复杂配置添加说明
4. **不要在运行时修改**: 需重启服务生效

---

## 🛠️ 配置工具

### 配置审计工具
```bash
# 运行完整审计
python3 -m app.config.auditor

# 输出:
# - config/config_index.yaml (配置索引)
# - docs/CONFIG_GUIDE.md (配置文档)
# - config/config_optimization_report.yaml (优化建议)
```

### 配置验证
```bash
# 验证配置文件格式
python3 -c "import yaml; yaml.safe_load(open('config/config.yaml'))"
```

---

## 📚 相关文档

- [CONFIG_GUIDE.md](./CONFIG_GUIDE.md) - 完整配置文档
- [config_index.yaml](../config/config_index.yaml) - 配置索引
- [config_optimization_report.yaml](../config/config_optimization_report.yaml) - 优化建议

---

**维护者**: FTSeek团队  
**最后更新**: 2026-04-11
