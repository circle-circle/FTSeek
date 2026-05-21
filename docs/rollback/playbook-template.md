# 回滚 Playbook: [场景名称]

> **适用场景**: [描述什么情况下使用此 Playbook]
> **最后更新**: YYYY-MM-DD
> **负责人**: [Agent 名称/人员]

---

## 1. 前置条件

- [ ] 确认需要回滚 (问题已定位，无法快速修复)
- [ ] 确认回滚目标版本 (commit hash)
- [ ] 通知相关人员 (如有)

---

## 2. 回滚步骤

### 2.1 停止服务

```bash
# 美股
sudo systemctl stop ftseek-us

# 港股
sudo systemctl stop ftseek-hk
```

### 2.2 回滚代码

```bash
# 方式 A: Git revert (推荐，保留历史)
git revert <commit-hash>

# 方式 B: Git reset (谨慎，会丢失历史)
git reset --hard <commit-hash>
```

### 2.3 回滚配置 (如有)

```bash
# 从备份恢复配置
cp config/unified_config.yaml.bak config/unified_config.yaml
```

### 2.4 回滚数据库 (如有 schema 变更)

```bash
# 执行回滚 SQL
sqlite3 data/ftseek_us.db < migrations/rollback_XXX.sql
```

### 2.5 启动服务

```bash
# 美股
sudo systemctl start ftseek-us

# 港股
sudo systemctl start ftseek-hk
```

---

## 3. 验证步骤

```bash
# 3.1 服务状态
sudo systemctl status ftseek-us

# 3.2 日志检查 (无 ERROR)
tail -f logs/ftseek.log | grep -E "ERROR|CRITICAL"

# 3.3 健康检查
curl http://localhost:8080/health

# 3.4 交易状态 (如有持仓)
python scripts/check_positions.py
```

---

## 4. 回滚后行动

- [ ] 记录回滚原因和时间
- [ ] 创建 INCIDENT 报告
- [ ] 分析根因，制定长期修复方案
- [ ] 更新测试用例，防止回归

---

## 5. 常见回滚场景

| 场景 | 回滚目标 | 特殊处理 |
|------|----------|----------|
| 交易路径 bug 导致误下单 | 上一个稳定 commit | 先停止服务，检查持仓 |
| 配置参数错误 | 修改前的配置 | 验证配置漂移 |
| 数据库 schema 不兼容 | 上一个 schema 版本 | 执行回滚 SQL |
| 第三方 API 变更 | 锁定旧版本 | 通知 API 提供方 |

---

## 6. 紧急联系人

| 角色 | 联系方式 | 职责 |
|------|----------|------|
| 项目所有者 | [联系方式] | 最终决策 |
| 运维负责人 | [联系方式] | 服务恢复 |
| 开发负责人 | [联系方式] | 根因分析 |
