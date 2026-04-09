# FTSeek 模拟盘数据同步 - 故障排查报告

**排查时间**: 2026-03-31 18:25  
**问题**: 无法从富途 OpenD 同步真实交易数据

---

## 🔴 问题现状

### 症状
- OpenD 服务正常运行 (端口 11111 监听)
- 配置文件读取正常 (账号 7078740)
- 连接 OpenD 失败，错误信息：
  - `无法连接富途 OpenD (交易)`
  - `解锁交易账户失败：交易密码输入错误`
  - `解锁交易账户失败：此数据暂时还未准备好`

### 已尝试的解决方案

| 尝试 | 结果 | 说明 |
|------|------|------|
| 配置环境变量 | ❌ 无效 | FUTU_LOGIN_PWD 等 |
| 从 XML 读取配置 | ❌ 无效 | login_pwd_md5, trade_pwd_md5 |
| 重启 OpenD 服务 | ❌ 无效 | 等待 30 秒后重试 |
| 使用 MD5 密码 | ❌ 无效 | 32 位 MD5 格式 |
| 使用明文密码 | ❌ 无效 | login_pwd 字段为空 |

---

## 🔍 根本原因分析

### 可能原因

1. **密码格式不匹配**
   - OpenD.xml 中的密码是 MD5 加密
   - unlock_trade() 可能需要明文密码
   - 或者需要特定格式的密码

2. **OpenD 连接限制**
   - 可能有连接次数限制
   - 可能需要先登录行情账户

3. **权限问题**
   - 模拟账户可能不支持 API 访问
   - 需要特定的账户权限

4. **API 版本不兼容**
   - Futu OpenD 版本：10.1.6108
   - futu-api 版本：需要检查

---

## ✅ 已完成的工作

### 1. 代码修复

| 文件 | 修改内容 | 状态 |
|------|---------|------|
| `app/simulation/sync_service.py` | 添加重试机制、价格更新 | ✅ |
| `app/simulation/opend_config.py` | 从 XML 安全读取配置 | ✅ |
| `app/simulation/api.py` | 创建 4 个 API 端点 | ✅ |
| `app/server.py` | 注册 simulation 路由 | ✅ |

### 2. 基础设施

| 项目 | 状态 | 说明 |
|------|------|------|
| Systemd Timer | ✅ 已配置 | 每日 16:30 自动同步 |
| 数据库清理 | ✅ 已完成 | 删除重复账户 |
| 备份机制 | ✅ 已配置 | 同步前自动备份 |
| 日志记录 | ✅ 已配置 | data/simulation_logs/ |

### 3. API 验证

| 端点 | 状态 | 返回数据 |
|------|------|---------|
| `/api/simulation/metrics` | ✅ 正常 | 胜率、盈亏等 |
| `/api/simulation/positions` | ✅ 正常 | 持仓列表 |
| `/api/simulation/trades` | ✅ 正常 | 交易记录 |
| `/api/simulation/capital_curve` | ✅ 正常 | 资金曲线 |

---

## 📊 当前数据状态

**数据库**: `/opt/ftseek/FTSeek/data/simulation.db`

| 表名 | 记录数 | 数据来源 |
|------|--------|---------|
| `simulation_account` | 1 | 测试数据 |
| `simulation_positions` | 2 | 测试数据 (腾讯 + 阿里) |
| `simulation_trades` | 2 | 测试数据 |

**当前持仓**：
- 腾讯控股 (00700): 200 股 @ 385.0
- 阿里巴巴 (09988): 500 股 @ 78.5

---

## 🔧 替代方案

### 方案 A: 手动导入交易数据

**步骤**:
```bash
# 1. 从富途导出交易记录 (CSV 格式)
# 2. 使用导入脚本
cd /opt/ftseek/FTSeek
python3 scripts/import_trades_from_csv.py --file trades.csv

# 3. 验证导入
sqlite3 data/simulation.db "SELECT COUNT(*) FROM simulation_trades;"
```

### 方案 B: 使用富途 API 直接查询

**步骤**:
```python
# 创建独立查询脚本
from futu import OpenSecTradeContext, TrdEnv, TrdMarket

trade_ctx = OpenSecTradeContext(host='127.0.0.1', port=11111)
ret, data = trade_ctx.get_position_list(trd_env=TrdEnv.SIMULATE, market=TrdMarket.HK)
print(data)
```

### 方案 C: 继续使用测试数据

**说明**: 如果无法同步真实数据，可以：
1. 保留现有测试数据
2. 添加更多测试场景
3. 用于策略回测和验证

---

## 📝 下一步建议

### 立即执行

1. **检查 OpenD 日志**
   ```bash
   tail -100 /opt/ftseek/Futu_OpenD_*/FutuOpenD.log
   ```

2. **验证 API 版本**
   ```bash
   python3 -c "import futu; print(futu.__version__)"
   ```

3. **尝试手动连接**
   ```bash
   cd /opt/ftseek/FTSeek
   python3 -c "
   from futu import OpenSecTradeContext, TrdEnv
   ctx = OpenSecTradeContext()
   ret = ctx.unlock_trade(password='your_password')
   print(ret)
   "
   ```

### 长期方案

1. **联系富途技术支持**
   - 确认 API 访问权限
   - 获取正确的密码格式
   - 确认账户权限

2. **使用实盘账户测试**
   - 实盘账户可能有不同的 API 权限
   - 可以先用小额资金测试

3. **考虑替代数据源**
   - 使用富途 CSV 导出
   - 使用第三方数据服务

---

## 📖 相关文档

- **配置指南**: `/opt/ftseek/FTSeek/docs/SIMULATION_SYNC_SETUP.md`
- **API 文档**: `/opt/ftseek/FTSeek/docs/WEB_ACCESS_GUIDE.md`
- **执行报告**: `/opt/ftseek/FTSeek/data/simulation_logs/execution_report_20260331.md`

---

**最后更新**: 2026-03-31 18:25  
**状态**: ⚠️ 同步功能待修复，API 端点正常运作
