# FTSeek v3.0 会话管理指南

---

## 📌 当前会话状态

**会话 ID**: `agent:main:main`  
**创建时间**: 2026-03-01 10:34  
**最后活动**: 2026-03-01 12:34  
**持续时间**: ~2 小时

---

## 💾 会话保存方式

### 方式 1: 使用 OpenClaw 会话管理 (推荐)

OpenClaw 会自动保存所有会话，您可以通过以下方式管理：

#### 查看会话列表
```bash
openclaw sessions list
```

#### 查看会话详情
```bash
openclaw sessions show <session-id>
```

#### 导出会话历史
```bash
openclaw sessions export <session-id> > session_backup.md
```

---

### 方式 2: 手动备份关键信息

#### 1. 导出当前配置
```bash
cd /opt/ftseek/FTSeek

# 备份策略配置
cp app/strategies/config.yaml docs/backups/config_$(date +%Y%m%d_%H%M%S).yaml

# 备份风控配置
cp config/risk_config.yaml docs/backups/risk_$(date +%Y%m%d_%H%M%S).yaml

# 备份环境变量
cp .env docs/backups/env_$(date +%Y%m%d_%H%M%S).env
```

#### 2. 导出会话摘要
创建一个会话摘要文件，包含：
- 会话 ID
- 关键决策
- 配置变更
- 待办事项

---

### 方式 3: 使用子代理会话

如果您使用了子代理 (sessions_spawn)，它们会自动保存：

```bash
# 查看子代理会话
openclaw subagents list

# 查看子代理历史
openclaw sessions history <subagent-session-key>
```

---

## 🔄 新会话开启方式

### 场景 1: 继续 FTSeek 项目

**新会话提示词**:
```
继续 FTSeek v3.0 模拟盘观察项目

当前状态:
- 策略配置：方案 B + 双向交易 + 保守风控
- 配置文件：app/strategies/config.yaml
- 回调服务器：http://39.97.58.4:8899
- 钉钉推送：已配置并测试通过
- 定时任务：已配置 (crontab)

下一步:
- 明日 (2026-03-03) 开市观察
- 接收实时买卖信号
- 人工确认执行
- 追踪绩效

参考文档:
- docs/TRADING_DAY_PLAN.md
- docs/SIGNAL_CRITERIA_AND_FEES.md
- docs/SESSION_MANAGEMENT.md
```

---

### 场景 2: 优化策略逻辑

**新会话提示词**:
```
优化 FTSeek v3.0 策略逻辑

当前配置:
- 融合 v2 权重 50%, 阈值 0.60
- 均线交叉权重 30%
- RSI 反弹权重 20%
- 双向交易：已开启
- 风控：保守 (止损 5%, 回撤 15%)

需要优化:
- 根据实际交易数据调整阈值
- 优化做空信号判断
- 调整仓位配置

参考数据:
- 查看 docs/ 目录下交易记录
- 分析胜率/盈亏比
- 评估风控参数
```

---

### 场景 3: 问题排查

**新会话提示词**:
```
FTSeek v3.0 问题排查

问题描述:
[详细描述遇到的问题]

已尝试:
- [已尝试的解决方案]

系统状态:
- 回调服务器：运行中/停止
- 钉钉推送：正常/异常
- 数据库：正常/异常
- 富途连接：正常/异常

日志位置:
- logs/ftseek.log
- logs/callback_server.log
- logs/daily_stats.log
```

---

## 📋 会话交接清单

### 开市前会话 (每日 09:00)

**包含内容**:
- [ ] 系统自检结果
- [ ] 隔夜新闻分析
- [ ] 预判信号
- [ ] 风控状态
- [ ] 持仓情况

**提示词模板**:
```
FTSeek v3.0 开市前检查

日期：2026-03-03
状态：准备就绪

检查项:
- 系统自检：✅
- 新闻分析：✅
- 预判信号：[列表]
- 风控状态：正常
- 持仓：[列表]

今日目标:
- 收益：1-2%
- 交易：≤8 次
- 风控：零违规
```

---

### 收盘后会话 (每日 16:30)

**包含内容**:
- [ ] 收盘统计
- [ ] 交易记录
- [ ] 盈亏分析
- [ ] 问题记录
- [ ] 明日计划

**提示词模板**:
```
FTSeek v3.0 收盘总结

日期：2026-03-03

今日统计:
- 信号：X 次
- 执行：X 次
- 收益：X%
- 胜率：X%

交易记录:
[详细列表]

问题记录:
[遇到的问题]

明日计划:
[调整优化方向]
```

---

### 周度会话 (每周六 09:00)

**包含内容**:
- [ ] 周报生成
- [ ] 策略评估
- [ ] 参数调整
- [ ] 下周计划

**提示词模板**:
```
FTSeek v3.0 周度总结

周期：2026-W10 (03-03 ~ 03-09)

本周统计:
- 交易：X 次
- 胜率：X%
- 收益：X%
- 最大回撤：X%

策略表现:
- 融合 v2: [数据]
- 均线交叉：[数据]
- RSI 反弹：[数据]

参数调整:
[调整建议]

下周计划:
[重点方向]
```

---

## 🗄️ 数据持久化

### 数据库备份

```bash
# 每日备份
cd /opt/ftseek/FTSeek
cp data/ftseek.db data/ftseek.db.backup.$(date +%Y%m%d)

# 每周导出
sqlite3 data/ftseek.db ".dump" > data/backup/weekly_$(date +%Y%m%d).sql
```

### 日志归档

```bash
# 日志轮转 (每周)
cd /opt/ftseek/FTSeek/logs
tar -czf logs_$(date +%Y%m%d).tar.gz *.log
mv logs_*.tar.gz archive/
```

### 配置版本控制

```bash
# 使用 git 管理配置
cd /opt/ftseek/FTSeek
git add app/strategies/config.yaml
git commit -m "更新策略配置：方案 B + 双向交易"
git push origin master
```

---

## 📞 紧急联系

### 系统故障

1. **回调服务器宕机**
   ```bash
   # 重启服务
   pkill -f callback_server
   cd /opt/ftseek/FTSeek
   python3 app/callback_server.py &
   ```

2. **钉钉推送失败**
   ```bash
   # 测试推送
   python3 scripts/test_dingtalk.py
   # 检查 webhook 配置
   cat .env | grep DINGTALK
   ```

3. **数据库异常**
   ```bash
   # 恢复备份
   cp data/ftseek.db.backup.YYYYMMDD data/ftseek.db
   ```

---

## 📝 最佳实践

### 1. 会话命名规范

```
FTSeek-YYYYMMDD-[目的]
例：FTSeek-20260301-策略配置
    FTSeek-20260303-开市观察
    FTSeek-20260308-周度总结
```

### 2. 关键决策记录

每次重要决策后，更新 `docs/DECISIONS.md`:
```markdown
## 2026-03-01 策略配置

**决策**: 采用方案 B + 双向交易 + 保守风控

**原因**:
- 追求 1-2% 日收益
- 希望对冲风险 (做空)
- 保守风控保证安全

**配置**:
- 融合 v2 阈值：0.60
- 止损：5%
- 回撤：15%

**预期**:
- 日收益：1-2%
- 月收益：35-60%
- 最大回撤：<15%
```

### 3. 定期回顾

- **每日**: 收盘后回顾交易
- **每周**: 周六生成周报
- **每月**: 1 日月度总结
- **每季**: 策略大调整

---

**文档生成**: 2026-03-01  
**版本**: FTSeek v3.0  
**状态**: 已生效
