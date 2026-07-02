# 幽灵仓根因: _close_trade_record 不更新 open_positions

## 日期: 2026-07-01

## 根因链

1. `_close_trade_record` (pipeline.py:1842) 只更新 trade_records + playbook_instructions
2. **不更新 open_positions** → sync_status 仍为 'synced' (active)
3. position_sync 从 DB 加载到内存 (仓位"复活")
4. position_sync 查 SIM broker → 无此仓位 → 应标 ghost
5. 但 option_positions 查询失败 → ghost cleanup 全跳过
6. 仓位永远残留 → 每 30s 重建 → 开盘后触发异常平仓

## 修复

`_close_trade_record` 新增 `symbol` 参数, 在同一个 `conn.commit()` 内同时:
```sql
UPDATE open_positions SET sync_status='closed_by_scheduler', updated_at=?
WHERE symbol=? AND sync_status NOT IN (已关闭状态列表)
```

7个调用点全部传入 symbol: 2272, 2283, 2362, 2380, 2395, 2540, 2573

## 根因2: option_positions 表删除后进程未重启

- 7/1 删除 option_positions 表 + 代码改为返回 set()
- 但进程(09:05启动)加载旧代码 → 查询失败 → ghost cleanup 全跳过
- 重启后解决

## 影响
- MSFT 幽灵仓: EOD close 后反复重建, 开盘后 P0 方向校验失败
- BIDU: EOD_FORCE_CLOSE 后 open_positions 仍 active (但 broker 有仓, 未爆发)
