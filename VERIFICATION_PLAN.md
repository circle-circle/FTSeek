# FTSeek 修复验证计划 — 2026-05-19 周一开盘

## 已部署修复

| # | 修复 | 文件 | 预期效果 |
|---|------|------|---------|
| P0-1 | 止损 in-flight 锁 | data_subscriber.py | 止损不再无限阻塞，触发后立即执行 |
| P0-2 | reconcile 10min 冷却 | reconcile_positions.py | 近期已关闭的持仓不再被重复创建 |
| P0-3 | OOM 防护 | ftseek.service.d/memory-limit.conf | 内存上限 512M，降低 OOM 被杀风险 |
| P1-1 | weight_resolver 导入顺序 | weight_resolver.py | 消除 _cc_wr 未定义错误 |
| P1-2 | consensus_engine SSOT | consensus_engine.py | 消除 9 个硬编码常量漂移 |

## 周一验证清单

### 盘前 (09:00 HKT)
- [ ] `systemctl is-active ftseek` → active
- [ ] `journalctl -u ftseek --since "1h" | grep -c "error\|exception"` → 0
- [ ] 内存 < 384M (MemoryHigh 阈值)

### 开盘后首小时 (09:30-10:30)
- [ ] 止损日志出现 `[止损跳过] {code} 已有止损线程正在执行，跳过重复触发` → 正常
- [ ] 止损执行日志 `[止损成功]` 出现时间 < 10s 内（非 13+ 分钟）
- [ ] 无 `reconcile_sync` 策略的新持仓创建
- [ ] 如 reconcile 运行，日志出现 `判定为 Futu 结算延迟，跳过创建`

### 全天监控
- [ ] 胜率基线 vs 修复前 25%（信号交易）
- [ ] entry=exit 记录趋近于 0（旧版占 40%）
- [ ] 15:45 强制平仓数量减少（14:30 门禁拦截）
- [ ] 服务未被 SIGKILL 杀死

### 关键日志关键字
```bash
# 正常
"[止损跳过] {code} 已有止损线程正在执行"
"判定为 Futu 结算延迟/止损冷却窗口，跳过创建"

# 异常（需立即处理）
"止损后台执行异常"
"OOM" / "killed" / "memory"
"Failed with result"
```

## 已知债务（不影响 P0 功能）

| 债务 | 影响 | 计划 |
|------|------|------|
| data_subscriber.py 8 个定时常量未 SSOT | 运维层面，非交易逻辑 | 数据验证后统一迁移 |
| HKExitAggregator 双跑模式 | 仅记录不执行 | 胜率>55% 后启用 |

## 回滚方案
```bash
cd /opt/ftseek/FTSeek && git log --oneline -5  # 查看最近提交
git revert HEAD  # 回滚最近一次提交
systemctl restart ftseek
```
