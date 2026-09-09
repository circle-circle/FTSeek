# 方案：人工仓位识别 — 安全默认manual

> ⚠️ 状态回写 (2026-09-06 治理审计): 本方案未记录落地/废弃状态, 悬挂;
> 新仓位默认 manual/auto 的现行行为以 app 侧 position_sync 代码与近期 DEB 为准。

## 根因
position_sync发现新Broker仓位时（DB无记录），当前逻辑：
1. 检查YAML `self_managed_symbols` 列表 → 在列表中→标manual
2. 检查 `manual_order_audit` 表当日手动订单 → 有→标manual  
3. 检查是否是PLAYBOOK_SIM仓位 → 是→标playbook_sim
4. **以上都不匹配 → 标auto → ExitMonitor可能自动平仓**

第4步是bug：人工在富途App直接买的新股票不在YAML列表里，也没经过系统下单，会被当auto仓自动管理。

## 方案：反转默认值

**当前逻辑**：新仓位默认auto，需要证明是manual才标manual
**改为**：新仓位默认manual，需要证明是auto（当日有系统下单记录）才标auto

### 改动位置
`app/core/position_sync.py` line 747+（INSERT新仓位的分支）

### 改动逻辑
在INSERT前加一个检查：
```python
# 检查今日是否有该symbol的系统下单记录
cursor.execute("""
    SELECT 1 FROM trade_records 
    WHERE symbol=? AND source_type IN ('PLAYBOOK_SIM','MAIN_PIPELINE','ORB_ENGINE')
    AND entry_time >= date('now','start of day','-1 day')
    LIMIT 1
""", (symbol,))
has_system_order = cursor.fetchone() is not None

if not has_system_order:
    # 无系统下单记录 → 人工开的 → 标manual
    owner = 'manual'
    account_type = 'real'
else:
    # 有系统下单记录 → 系统开的 → 标auto
    owner = 'auto'
    account_type = 'sim'
```

### 影响范围
- **新仓位INSERT**：默认manual，安全（不会被自动平仓）
- **已有仓位UPDATE**：不改owner（保持原值）
- **PLAYBOOK_SIM仓位**：已有专门分支（line 761-777），不受影响
- **系统下的单**：trade_records有记录 → 标auto → 正常管理

### 不改的部分
- YAML `self_managed_symbols` 仍有效（静态预声明）
- `manual_order_audit` 检查仍有效（通过系统下手动单的标的）
- ghost清理逻辑不变（DB有Broker无 → closed_by_sync）

### 风险
- 如果系统下单但trade_records写入延迟（订单提交到DB有窗口期），可能误标manual
- 但position_sync有180s新仓保护窗口（line 614-625），给DB写入留了时间
- 即使误标manual，后果是仓位不被自动管理（安全方向：不操作 > 误平仓）
