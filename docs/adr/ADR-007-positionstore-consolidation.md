# ADR-007: PositionStore 写入路径收拢

**日期**: 2026-06-16
**状态**: 已规划 (待实施)
**决策者**: 用户 + Hermes

## 背景

PositionStore 当前有 30 个写入点分布在 7 个文件中:
- 14 个 add() 调用
- 13 个 remove() 调用  
- 3 个直接 _lock 访问

这导致:
1. 幽灵仓问题难以追踪 (写入来源分散)
2. 每个写入点独立实现 metadata/tombstone/通知逻辑
3. Broker 一致性校验无法统一

## 决策

将 30 个写入点收拢为 8 个语义化 API:

| 新 API | 合并的写入点 | 职责 |
|--------|-------------|------|
| `restore_from_broker()` | 8 个启动恢复 add | 启动时仅恢复 Broker 确认的持仓 |
| `register_fill()` | 3 个订单成交 add | 开仓后注册 + 即时 sync |
| `sync_from_broker()` | 2 个周期同步 (add+remove) | 周期性对账 |
| `reconcile()` | 3 个运行时对账 | 运行时差异修复 |
| `close_position()` | 5 个平仓 remove | 统一平仓清理 |
| `purge_orphan()` | 4 个幽灵清理 remove | 孤儿仓位清除 |
| `resolve_conflict()` | 2 个冲突解决 remove | 方向冲突处理 |
| `atomic_update()` | 3 个 _lock 直接访问 | 原子更新 |

## 实施条件

1. **前置**: 当前 Broker-mirror 修复稳定运行 ≥ 3 天
2. **验证**: 每个合并的 API 必须有单元测试覆盖
3. **渐进**: 逐个替换，不一次性重构

## 风险

- 重构期间可能引入新 bug
- 部分调用方有隐式依赖 (如 daily_active_pool 同步)

## 替代方案

- 不做收拢: 继续在每个写入点打补丁 → 复杂度持续增长
- 完全重写 PositionStore: 风险太大 → 拒绝
