# Coordinator Phase A — 开盘前验证 Checklist

> **生效版本**: 2026-05-14 (含 A.8-A.13 增强)
> **灰度模式**: `enabled=true, observe_only=true` (不阻断, 仅记录)
> **目标**: 美东开盘 (北京时间 21:30) 后, 确认 owner_strategy 数据落库 + 资金账本一致 + signal_arbitration 落表

## A.8-A.13 新增功能
- 信号路径接通 / 5min reconcile / 启动 banner / 状态 CLI / 启动回灌 / position_pct / F3 强校验 / daily_review 集成

## B.1-B.8 第一性原理审查后修复 (今晚必验)
- **B.1 trade_records SQL**: save_trade 增加 owner_strategy 字段 (之前列加了但 INSERT 没传)
- **B.2 trade_attribution INSERT**: 入场归因写 owner_strategy + direction + entry_time + entry_price
- **B.3 fast_lane 资金语义**: capital_bucket="main" — fast_lane 占用计入 main 桶, 不再重复加总
- **B.4 scalping 路径**: position.owner_strategy="scalping" + coordinator notify (开仓 + 平仓)
- **B.5 ExitDispatcher 注册**: 4 个最小 ExitPolicy 注册到 dispatcher, 不再死代码
- **B.6 公开 is_observe_only**: 删除私有 _observe_only 访问, capital/lock/coordinator 三层都暴露
- **B.7 持久化端到端测试**: 6 条新单测覆盖 schema → INSERT → 读回 + legacy DB 升级
- **B.8 多源信号采集**: 所有 adapter.generate_signal 都喂入 arbitrate, Phase B 切真实生成时 0 修改路径

---

## 启动前 (~21:00 北京时间)

```bash
# 1) 验证 yaml 灰度开关已开
grep -A 6 "^  coordinator:" config/unified_config.yaml
# 期望:
#   coordinator:
#     enabled: true
#     observe_only: true
#     max_total_pct: 0.90
#     audit_arbitration: true

# 2) 验证 schema 升级已生效
sqlite3 data/ftseek_us.db "PRAGMA table_info(trade_records);" | grep owner_strategy
sqlite3 data/ftseek_us.db "PRAGMA table_info(open_positions);" | grep owner_strategy
sqlite3 data/ftseek_us.db "PRAGMA table_info(trade_attribution);" | grep owner_strategy
# 期望: 三个表都有 owner_strategy 列

# 3) 跑 Phase A 单测确认绿
PYTHONPATH=$PWD/venv/lib/python3.8/site-packages /Library/Developer/CommandLineTools/usr/bin/python3 \
  -m pytest tests/coordinator/ -v
# 期望: 16 passed
```

---

## 启动 (~21:25 北京时间)

```bash
systemctl restart ftseek-us
sleep 3
# 期望日志出现:
#   [调度器·Coordinator] enabled=True observe_only=True max_total=0.9 caps={...}
#   [调度器·Coordinator] 已注册 4 个 adapter: ['main(p=5)', 'scalping(p=10)', 'fast_lane(p=8)', 'llm_plan(p=3)']
journalctl -u ftseek-us -n 50 --no-pager | grep -iE "Coordinator|adapter"
```

---

## 开盘 + 30 分钟 (~22:00 北京时间)

```bash
# 1) 检查 owner_strategy 落库
sqlite3 data/ftseek_us.db \
  "SELECT owner_strategy, COUNT(*) FROM open_positions WHERE entry_time > datetime('now','-1 hour') GROUP BY owner_strategy;"
# 期望: 至少 1 行, owner_strategy 非 NULL (大概率全部 'main' — 因为 Phase A 还没接 scalping/fast_lane 的 wrap)

# 2) 检查 capital_allocator 事件
journalctl -u ftseek-us -n 200 --no-pager | grep -iE "CapitalAllocator|SymbolLock"
# 期望: 看到 ALLOWED 事件; 如果 OBSERVE_CONFLICT 出现 → 说明真实生产中已发生策略冲突, 提供 enforce 证据

# 3) 检查 signal_arbitration 表 (多策略仲裁)
sqlite3 data/decisions.db \
  "SELECT symbol, winner_strategy, n_candidates, datetime(timestamp,'unixepoch') FROM signal_arbitration ORDER BY id DESC LIMIT 10;"
# 期望: 如果 n_candidates > 1, 说明真的发生了多策略竞争 (Phase B 的强 enforce 会有用)
```

---

## 收盘后 (北京时间次日 04:00)

```bash
# 1) 资金账本对账
PYTHONPATH=$PWD/venv/lib/python3.8/site-packages /Library/Developer/CommandLineTools/usr/bin/python3 -c "
import sys; sys.path = ['venv/lib/python3.8/site-packages','.'] + sys.path
import sqlite3
conn = sqlite3.connect('data/ftseek_us.db')
print('=== 今日 owner_strategy 分布 ===')
for r in conn.execute('SELECT owner_strategy, COUNT(*) FROM trade_records WHERE date(exit_time)=date(\"now\") GROUP BY owner_strategy').fetchall():
    print(f'  {r[0]:15s} {r[1]} 笔')
print('=== 今日活跃 adapter ===')
print('  应有: main / scalping / fast_lane / llm_plan (Phase A 仅 main 真的会写入 — 其他需 Phase B 接 wrap)')
"

# 2) 跑 daily_review (含 F22 的 exit_reason × pnl)
PYTHONPATH=$PWD/venv/lib/python3.8/site-packages /Library/Developer/CommandLineTools/usr/bin/python3 \
  scripts/exit_reason_pnl_report.py --days 1 --output reports/today_exit_quality.md

# 3) 检查 BLOCKED_* 事件 (评估 enforce 模式如果开启会拦截多少笔)
journalctl -u ftseek-us --since today --no-pager | grep -c "BLOCKED_STRATEGY_CAP"
journalctl -u ftseek-us --since today --no-pager | grep -c "OBSERVE_CONFLICT"
# 如 > 0 → 说明 enforce 模式有价值, Phase B 优先级提升
```

---

## 紧急回滚 (如有问题)

```bash
# 一键关闭 coordinator (回到 Phase A 之前状态)
sed -i.bak 's/    enabled: true.*$/    enabled: false  # ROLLBACK 2026-05-14/' config/unified_config.yaml
systemctl restart ftseek-us
journalctl -u ftseek-us -n 20 --no-pager | grep "Coordinator"
# 期望: enabled=False
```

---

## 通过判定 (达到这 4 条 → Phase A 验收通过, 进入 Phase B 准备)

- [ ] owner_strategy 字段在所有 today 的 trade_records / open_positions / trade_attribution 中非空
  - 主线信号 → owner=main; scalping 路径 → owner=scalping
  - 用 `python scripts/coordinator_status.py --owner-summary` 可视化
- [ ] CapitalAllocator 事件日志可见, bucket 视图 (snapshot.by_strategy) 与 PositionStore 加总差 ≤ 0.5%
  - main bucket 应包含 main + fast_lane 占用之和
  - 用 `python scripts/coordinator_status.py --capital-from-db` 反查
- [ ] signal_arbitration 表有数据写入
  - 即使 n_candidates=1 也算 (主线信号)
  - 若 n_candidates > 1 → 多策略竞争发生 (验证 Phase A 多源信号采集)
- [ ] 0 新失败的 pytest (与 baseline 6 failed 一致)
  - 25 条 coordinator 单测 + 既有 risk subset 全绿

---

## Phase B 触发条件

满足任一即建议立即启动 Phase B:
1. 当日 `OBSERVE_CONFLICT` > 5 次 → SymbolLock enforce 价值大
2. 当日 `BLOCKED_STRATEGY_CAP` > 3 次 → CapitalAllocator enforce 价值大
3. 当日多策略 n_candidates > 1 的仲裁 > 10 次 → SignalArbitrator 进入主路径

否则按原计划下周启动 Phase B (ExitDispatcher + 强制 enforce).
