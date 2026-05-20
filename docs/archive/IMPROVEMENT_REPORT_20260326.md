# FTSeek-US 改进报告 - 富途 OpenAPI 集成

**实施日期**: 2026-03-26  
**实施人**: AI Assistant  
**优先级**: 1 (最高)

---

## 一、改进概述

本次改进解决了 FTSeek-US 与富途 OpenAPI **完全脱节**的核心问题，实现了：

1. ✅ **富途 API 集成模块** - 新建 `futu_integration.py`
2. ✅ **增强版监控器** - 升级 `us_stock_monitor.py`
3. ✅ **统一股票池** - 更新 `watchlist_us.json` 与 USER.md 对齐
4. ✅ **自动止损止盈** - 支持触发条件时自动下单

---

## 二、实施内容

### 2.1 新建富途集成模块

**文件**: `/opt/ftseek/FTSeek-US/app/futu_integration.py`

**功能**:
- 连接富途行情和交易 API
- 同步实际持仓到本地 `trade_records.json`
- 读取历史交易记录 (`.futu_trade_audit.jsonl`)
- 执行自动下单 (止损/止盈)
- 获取账户列表、持仓、资金、股价

**核心类**:
```python
class FutuIntegration:
    - connect()              # 连接 API
    - get_acc_list()         # 获取账户
    - get_positions()        # 获取持仓
    - get_funds()            # 获取资金
    - get_stock_price()      # 获取股价
    - sync_positions_to_local()  # 同步持仓
    - place_sell_order()     # 执行卖出
    - load_audit_records()   # 加载审计日志
```

### 2.2 升级监控器

**文件**: `/opt/ftseek/FTSeek-US/app/us_stock_monitor.py`

**新增功能**:
1. **持仓同步**: 启动时自动同步富途实际持仓
2. **自动交易**: 触发止损止盈时自动调用 `place_sell_order()`
3. **配置加载**: 从 `us_stock_config.yaml` 读取个股止损止盈参数
4. **追踪止盈**: 支持盈利后回撤自动平仓
5. **状态管理**: 完整的持仓生命周期管理

**关键改进**:
```python
# 之前：仅本地模拟持仓
self.positions = {}  # 手动创建

# 现在：同步富途实际持仓
self._sync_positions()  # 从富途 API 同步
```

### 2.3 统一股票池

**文件**: `/opt/ftseek/FTSeek-US/config/watchlist_us.json`

**变更**:
- ✅ 添加 USER.md 中的所有监控股票 (NVDA/AAPL/MSFT/GOOG/META/AMZN)
- ✅ 添加中概股 (BABA/PDD/JD)
- ✅ 添加港股持仓 (00981/03690/09992/09988)
- ✅ 为每只股票配置止损止盈参数
- ✅ 分 tier 管理 (TIER1/TIER2/ETF)

**股票池结构**:
```json
{
  "us_stocks": [10 只美股],
  "hk_stocks": [4 只港股],
  "etfs": [2 只 ETF]
}
```

### 2.4 配置文件备份

所有修改的文件已备份：
- `us_stock_monitor.py.bak`
- `watchlist_us.json.bak`
- `us_stock_config.yaml.bak`

---

## 三、测试结果

### 3.1 连接测试

```bash
cd /opt/ftseek/FTSeek-US
python3 scripts/test_futu_integration.py
```

**结果**:
- ✅ 富途行情连接成功
- ✅ 富途交易连接成功
- ✅ 港股行情获取成功 (HK.00700: $505.5)
- ⚠️ 美股行情权限不足 (需要购买行情卡)
- ✅ 审计日志读取成功 (3 条记录)

### 3.2 发现的问题

1. **账户列表获取失败**: `currency` 字段缺失
   - 已修复：添加默认值处理

2. **美股行情权限不足**: 
   - 原因：模拟账户未开通美股行情
   - 解决：需购买富途美股行情卡
   - 临时方案：使用 TwelveData/Finnhub 作为备用数据源

3. **自动下单未测试**:
   - 原因：无实际持仓触发
   - 计划：创建模拟持仓进行测试

---

## 四、使用指南

### 4.1 启动监控

```bash
# 启动服务
cd /opt/ftseek/FTSeek-US
python3 main.py --start

# 或使用 systemd
sudo systemctl start ftseek_us
```

### 4.2 查看状态

```bash
# 命令行
python3 main.py --status

# 或查看日志
tail -f /opt/ftseek/FTSeek-US/logs/ftseek_us.log
```

### 4.3 配置自动交易

编辑 `/opt/ftseek/FTSeek-US/config/us_stock_config.yaml`:

```yaml
# 启用自动交易
auto_trade_enabled: true

# 设置止损止盈
defaults:
  stop_loss: -0.08    # -8%
  take_profit: 0.15   # +15%
```

### 4.4 添加监控股票

编辑 `/opt/ftseek/FTSeek-US/config/watchlist_us.json`:

```json
{
  "us_stocks": [
    {
      "code": "US.NEWSTOCK",
      "name": "New Stock Inc",
      "enabled": true,
      "stop_loss": -0.10,
      "take_profit": 0.20
    }
  ]
}
```

---

## 五、下一步计划

### 优先级 2: 简化架构 (下周)

- [ ] 移除未使用的多 Agent 代码
- [ ] 简化 scheduler.py
- [ ] 整理 app/ 目录结构

### 优先级 3: 补充策略逻辑 (2 周后)

- [ ] 导入 FTSeek 港股策略 (RSI/MA 交叉)
- [ ] 实现信号评分系统
- [ ] 添加回测功能

### 优先级 4: 数据驱动优化 (长期)

- [ ] 建立交易日志分析
- [ ] 动态止损止盈参数
- [ ] 逐步恢复多 Agent 架构

---

## 六、核心改进对比

| 功能 | 改进前 | 改进后 |
|------|--------|--------|
| **持仓来源** | 手动创建 | 富途 API 同步 ✅ |
| **交易执行** | 无 | 自动下单 ✅ |
| **股票池** | 4 只美股 | 10 美股 +4 港股 +2 ETF ✅ |
| **配置管理** | 硬编码 | YAML 配置文件 ✅ |
| **历史数据** | 无 | 审计日志集成 ✅ |
| **通知** | 基础 | 详细平仓报告 ✅ |

---

## 七、风险提示

1. **自动交易风险**: 
   - 建议先在模拟盘测试
   - 设置合理的止损止盈参数
   - 定期检查日志

2. **行情权限**:
   - 美股行情需要购买行情卡
   - 港股行情已可用

3. **网络依赖**:
   - 富途 API 断线时会暂停交易
   - 本地有持仓缓存，重启后可恢复

---

## 八、联系与支持

- **项目路径**: `/opt/ftseek/FTSeek-US`
- **日志文件**: `/opt/ftseek/FTSeek-US/logs/ftseek_us.log`
- **配置文件**: `/opt/ftseek/FTSeek-US/config/`
- **数据文件**: `/opt/ftseek/FTSeek-US/data/`

---

**改进完成时间**: 2026-03-26 07:30  
**状态**: ✅ 已完成优先级 1 改进
