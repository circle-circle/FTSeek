# FTSeek 自愈引擎防重机制 v1.0

## 解决的问题

多会话并发执行健康检查时，可能对同一个问题进行重复分析和修复：
- **同一分钟**：health_guardian 每分钟运行，同一问题被重复处理
- **不同会话**：多个 cron 任务或手动触发同时运行
- **相似问题**：不同股票、不同价格的同一类问题被当作不同问题

## 防重策略

### 1. 问题实例 Hash 去重

使用 `IssueHasher` 为问题生成唯一 Hash，**归一化**后相同的问题视为同一实例：

```python
# 这些不同的问题会被归一化为相同的 Hash
"价格检查: 价格 8.62 接近日内高点 8.63 (距离 0.12% < 2.0%)"
"价格检查: 价格 59.70 接近日内高点 60.25 (距离 0.91% < 2.0%)"
"价格检查: 价格 111.20 接近日内高点 113.00 (距离 1.59% < 2.0%)"
# → 归一化: "价格检查: 价格 X 接近日内高点 X (距离 X% < X%)"
# → Hash: 51b247e19701 (相同)
```

**归一化规则**：
- 时间戳 → 空
- PID → 空
- 内存地址 → 空
- 数值 (价格/距离/百分比) → X
- 股票代码 → HK.X / US.X
- 耗时 → Xms

### 2. 三层防重机制

| 层级 | 规则 | 抑制时间 | 说明 |
|------|------|---------|------|
| **实例级** | 相同 Hash 的问题 | 30min | 同一问题 30min 内只处理一次 |
| **分类级** | 同分类的问题 | 5min | 同类问题 5min 冷却 |
| **成功修复** | 修复成功的问题 | 24h | 成功修复的问题 24h 内不再触发 |

### 3. 抑制时间动态调整

| 修复结果 | 抑制时间 | 原因 |
|---------|---------|------|
| ✅ 修复成功 + 验证通过 | 24h | 问题已解决，不需要重复检查 |
| ❌ 修复失败 (未升级) | 2h | 给系统时间重试或等待人工 |
| 🚨 升级人工处理 | 6h | 已通知人工，等待处理 |

## 架构

```
health_guardian 检测到异常
        ↓
  _process_with_self_healing()
        ↓
  engine.handle_issue() [被防重包装]
        ↓
  ┌─────────────────────────┐
  │  1. 生成问题 Hash       │
  │  2. 检查实例级去重      │ ←─ SQLite: issue_instances
  │  3. 检查分类级冷却      │ ←─ SQLite: category_cooldowns
  │  4. 调用原始处理逻辑    │
  │  5. 记录修复结果        │
  │  6. 设置抑制时间        │
  └─────────────────────────┘
        ↓
  返回 report 或 None (被跳过)
```

## SQLite 表结构

### issue_instances (问题实例表)
```sql
CREATE TABLE issue_instances (
    issue_hash TEXT PRIMARY KEY,      -- 问题 Hash
    issue_name TEXT NOT NULL,         -- 问题名称
    issue_detail TEXT,                -- 问题详情
    category TEXT,                    -- 分类
    first_seen TEXT NOT NULL,         -- 首次出现时间
    last_seen TEXT NOT NULL,          -- 最后出现时间
    last_processed TEXT,              -- 最后处理时间
    process_count INTEGER DEFAULT 0,  -- 处理次数
    last_fix_success INTEGER DEFAULT 0, -- 上次修复是否成功
    last_escalated INTEGER DEFAULT 0,   -- 上次是否升级
    suppressed_until TEXT             -- 下次可处理时间
);
```

### category_cooldowns (分类冷却表)
```sql
CREATE TABLE category_cooldowns (
    category TEXT PRIMARY KEY,        -- 分类名称
    cooldown_until TEXT NOT NULL,     -- 冷却结束时间
    reason TEXT                       -- 冷却原因
);
```

## 使用示例

### 创建带防重的引擎
```python
from app.self_healing.dedup import create_engine_with_dedup

engine = create_engine_with_dedup("/opt/ftseek/FTSeek")
```

### 处理问题 (自动防重)
```python
# 第一次调用：处理
report = engine.handle_issue(
    issue_text="价格检查: 价格 8.62 接近日内高点 8.63 (距离 0.12% < 2.0%)",
    check_name="追高买入",
    severity="warning"
)
# → 返回 SelfHealingReport

# 1 分钟后再次调用：跳过 (同一问题实例)
report = engine.handle_issue(
    issue_text="价格检查: 价格 59.70 接近日内高点 60.25 (距离 0.91% < 2.0%)",
    check_name="追高买入",
    severity="warning"
)
# → 返回 None (被防重)
```

### 查看防重统计
```python
stats = engine.dedup_store.get_stats()
print(stats)
# {
#     "total_tracked_issues": 15,
#     "currently_suppressed": 8,
#     "active_cooldowns": 2,
#     "top_issues": [
#         {"name": "追高买入", "count": 5},
#         {"name": "碎股下单", "count": 3}
#     ]
# }
```

## 集成到 health_guardian

```python
# health_guardian.py
from app.self_healing.dedup import create_engine_with_dedup

_SELF_HEALING_ENGINE = create_engine_with_dedup(str(BASE_DIR))

def _process_with_self_healing(state, all_issues, critical_issues, warning_issues):
    processed_count = 0
    skipped_count = 0
    
    for issue in all_issues:
        report = _SELF_HEALING_ENGINE.handle_issue(
            issue_text=issue,
            check_name=issue.split(":")[0].strip(),
            severity="critical" if issue in critical_issues else "warning"
        )
        
        if report:
            processed_count += 1
            # 推送报告...
        else:
            skipped_count += 1  # 被防重跳过
    
    if skipped_count > 0:
        logger.info(f"[防重] 跳过 {skipped_count} 个重复问题")
```

## 日志输出示例

```
[自愈/诊断] 追高买入: 追高阈值 2.0% 可能过严 (置信度=medium)
[自愈/修复] 追高买入: ❌ 无修复动作
[防重] 跳过 2 个重复问题
[自愈] 处理了 1 个问题
```

## 注意事项

1. **跨会话持久化**：防重状态存储在 SQLite，重启后仍然有效
2. **Hash 冲突**：使用 MD5 前 12 位，冲突概率极低 (< 10^-9)
3. **抑制时间可调**：修改 `record_fix_result()` 中的 timedelta 即可调整
4. **手动清除抑制**：可直接操作 SQLite 删除 `suppressed_until` 字段

## 文件清单

```
app/self_healing/
├── dedup.py           # 防重引擎核心 (17KB)
├── engine.py          # 自愈引擎 (50KB)
├── optimizer.py       # 自愈优化器 (12KB)
└── __init__.py        # 模块导出

data/
├── self_healing_dedup.db         # 防重状态 (SQLite)
├── self_healing_history.json     # 自愈历史
├── self_healing_patterns.json    # 日志模式库
└── self_healing_optimization.json # 优化记录
```
