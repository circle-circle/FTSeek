---
type: "review"
title: "sim放量性能预审与修复 (2026-08-09)"
created: "2026-08-09"
tags: [performance, sim-volume, get-positions, shadow-portfolio, config]
---

# sim放量性能预审与修复 — get_positions循环内调用 / shadow幂等 / 配置入档

> 背景: Qoder 8/9 提交3个"sim放量"改动 (conf门70→55 / 影子组合 / micro缓存)。
> 放量前性能预审发现1个P0热点 + 2个P1/P2问题, 已修复。本笔记为修复记录。

## 一、结论速览

| 风险 | 严重度 | 问题 | 修复 |
|---|---|---|---|
| `get_positions` 循环内调用 | 🔴 P0 | 每条未执行指令调一次OpenD交易连接, 放量后 40+指令×78轮/日=数千次 | 提升到循环外, 每轮一次 |
| `_SP_EMITTED` 进程内set | 🟡 P1 | 重启即清空 → 同 (inst_id,date) 可能重记一行 | 落盘前查文件兜底 |
| 配置键未入yaml | 🟡 P2 | 只有代码默认值, "可覆盖"是空头承诺 | 三键写入 unified_config.yaml |

## 二、P0 详情: get_positions 循环内调用

**位置**: `pipeline.py run_playbook_auto_execute` 的 `for inst in instructions` 循环内:

```python
# 修复前 (每条未执行指令都执行):
_broker_positions = _dual.get_positions(target_env="sim")   # OpenD交易连接, ~300ms/次

# 修复后 (循环外一次, 循环内复用):
_broker_positions = _dual.get_positions(target_env="sim") or []  # 每轮一次
```

**放量后影响**: 40+指令 × 78轮/日 (300s间隔×6.5h) = **数千次OpenD交易连接调用/日**。
broker持仓是**轮级全局状态**, 同轮内不会变 → 同轮重复查询结果一致, 纯浪费。

**修复语义**: 失败降级为空列表 (与原每条指令独立失败返回 [] 一致, 防误拦)。

## 三、P1 详情: shadow emit 幂等

`_SP_EMITTED` 是进程内 set, **服务重启即清空** → 重启后同 (inst_id,date) 可能重记一行。
结算端 (settle_shadow_portfolio.py) 虽按 inst_id 去重, 但文件冗余行污染"行数≈样本量"的
直观统计。

**修复**: 落盘前查文件兜底 (仅重启后首行触发, 每行一次 grep 成本可忽略),
文件已有同键 → 只入 set 不重写。文件不可读时退化为仅进程内 set 去重。

## 四、P2 详情: 配置入档

新增 `config/unified_config.yaml` playbook 段 (含注释+回滚说明):

```yaml
supply_conf_gate_buy: 55        # conf门兜底 (Qoder 50809e7b), 回滚=70
supply_conf_gate_short: 55
shadow_portfolio_enabled: true  # 影子组合开关 (Qoder 67612201), 回滚=false
```

## 五、验证结果

| 检查项 | 结果 |
|---|---|
| YAML解析 | ✅ 3个新键全部读取正常 |
| config_container 读取 | ✅ 55/55/True |
| 全量测试 | ✅ 238 passed (含7个shadow单测) |
| 语法检查 | ✅ |
| 服务重启 | ✅ active, 无 ERROR/Traceback |

## 六、周一开盘验证点

1. auto_execute 日志确认 "轮级 broker 持仓查询" 只打一次 (不再每条指令打)
2. 影子 entries 正常落盘无重复行
3. micro 拉取返回真实 spread_bps (US.US. 前缀修复生效)

## 关联

- Qoder commits: 50809e7b (conf门) / 67612201 (影子组合) / b2a5245e (micro缓存)
- 本修复 (未commit): pipeline.py +42 / unified_config.yaml +9
- 2026-08-09_adf_acf_pacf_review.md (同日统计方法复核)