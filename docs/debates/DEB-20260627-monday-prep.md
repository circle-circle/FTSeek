# Agent-vs-Agent Debate: 周一开盘前全链路预检

## 状态
- [x] 进行中
- [ ] 已决策
- [ ] 已验证
- [ ] 已关闭

## 背景

6/24(周二)正常运行25条指令, 6/25只剩1条, 6/26(Friday)零条。服务在6/26 21:00 CST(=09:00 ET market open)重启, 整个盘前生成阶段服务不稳定。6/27(周六)修复了多个问题但未提交代码(238 files changed uncommitted)。

目标: 确保6/30(周一)开盘前全链路无阻塞。

## 数据与实证

### 阻塞1: 6/26零指令生成 — 服务在PRE_MARKET阶段不稳定

时间线(6/26):
```
04:00 CST(16:00 ET Thu) → AFTER_HOURS, 盘后追踪6/25
04:45 CST → shutdown
04:46 CST → restart, 仍是AFTER_HOURS
06:15 CST → restart again
06:36 CST → restart again
... 持续重启 ...
21:00 CST(09:00 ET Fri) → shutdown (market open!)
21:01 CST → restart → TRADING phase
  → "DB已有3条指令，跳过生成(幂等)" (但DB现在0条)
```

**根因**: 服务在CST 04:00-21:00期间反复重启(=ET 16:00 Thu - 09:00 Fri), PRE_MARKET阶段(ET 04:00-09:30 = CST 16:00-21:30)服务不稳定, `run_playbook_generate`可能在某次重启时跑了但生成的指令后来被覆盖/丢失。

**验证**: DB现在0条6/26指令, 但日志说"已有3条" → 指令消失, 可能是ON CONFLICT覆盖或DB重置。

### 阻塞2: 238个文件未提交 — 服务运行的代码 ≠ 磁盘代码

```
git diff --stat HEAD: 238 files changed, 12269 insertions(+), 148143 deletions(-)
```

关键未提交文件(影响交易路径):
- app/playbook/instruction_generator.py (仓位双轨制修复+BUY SL 2.5%)
- app/playbook/pipeline.py (buying_power动态化+_fill检查)
- app/playbook/playbook_service.py (_fill不fallback)
- app/playbook/ic_tracker.py (IC backfill日志)
- app/data/futu_lv3_provider.py (缓存新鲜度)
- app/core/scheduler_core.py (phase检测+playbook调用)
- config/playbook_horizon.yaml (BUY SL 2.5%)

**验证**: `git diff HEAD --name-only -- app/playbook/` → 10个文件未提交

### 阻塞3: 6/25只生成1条指令 — 投票/筛选链路可能断裂

6/24正常生成25条(14 BUY+11 SHORT), 6/25只1条(UNH BUY)。

**可能根因**: 
- 投票模型API失败(qwen/deepseek/glm)
- 数据收集失败(Finnhub/OpenD)
- 市场环境判断为NO_TRADE

**验证**: 6/25的日志在ftseek_us.log-20260625.gz中, 需检查VotingCoordinator输出。

### 阻塞4: IC backfill仍返回0 — 6/27修复未验证

```
日志: "Backfilled forward returns for 0 instructions" (每2min重复)
DB: 53/58有T+1, 但6/25的1条没有
```

6/27修复了futu_lv3_provider的缓存新鲜度检查, 但服务重启后IC backfill只在`run_playbook_track`中调用, 周末(WEEKEND phase)不调用。

**验证**: 周一PRE_MARKET/AFTER_HOURS时`run_playbook_track`会跑, 那时验证修复效果。

### 阻塞5: 服务可能不稳定 — 6/26多次重启

6/26从04:00到21:00 CST服务重启了至少4次。需检查:
- systemd配置是否正确(auto-restart)
- 内存/CPU是否够用
- OpenD连接是否稳定

**验证**: `journalctl -u ftseek-us.service --since "2026-06-26" --until "2026-06-27" | grep -c "Started\|Stopped"`

## Ponytail代码审核

### P1: `_fill`重试3次sleep 0.5-1.5s — 阻塞下单流程

```python
for _retry in range(3):
    _time.sleep(0.5 * (_retry + 1))  # 0.5+1.0+1.5 = 3s total
    _fill = self._query_filled_price_for_order(...)
```

- 市价单通常立即成交, 查询应该1次就够
- 3次重试sleep 3秒 → 批量执行时25条指令 × 3s = 75秒阻塞
- ponytail: 改为1次查询, 失败则log warning + fallback(但不用entry_price)

### P2: `_query_filled_price_for_order`访问dual_broker私有方法

```python
ctx, acc_id, trd_env, lock = dual._ctx_and_acc(target_env)  # 私有方法
```

- `_ctx_and_acc`是DualBroker的私有方法, 不应该从外部调用
- ponytail: 在DualBroker上加public方法`query_order_price(futu_order_id, target_env)`

### P3: buying_power检查25%阈值 — 没有数据支撑

```python
if _buying_power > 0 and _new_notional > _buying_power * 0.25:
```

- 25%是任意选的, 没有回测数据
- 当前$82K账户单仓$6,560 = 8%, 远低于25%
- 切实盘后如果BP=$50K, 25%=$12,500, 单仓$6,560仍通过
- ponytail: 删掉这个检查, max_position_pct=8%已经是单仓上限

## 可选方案

### 方案A: 提交代码 + 重启 + 验证全链路
1. git add + commit所有交易路径修改
2. 清__pycache__ + 重启服务
3. 手动触发run_playbook_generate验证
4. 检查VotingCoordinator输出

### 方案B: 只修P0阻塞, P2记录
1. 提交代码(阻塞2)
2. Ponytail修复P1(_fill重试太慢)
3. 删除P3(25%检查无数据)
4. 重启验证

### 方案C: 全部提交+Ponytail审核+周一前验证

## 讨论记录

### 第 1 轮：hermes

**立场：方案C — 全部提交 + Ponytail审核 + 周一前验证**

**阻塞1 (P0)** — 服务不稳定是根因
- 6/26服务在PRE_MARKET期间反复重启 → 指令未生成或丢失
- 修复: 检查systemd配置, 确保服务不会在PRE_MARKET期间重启
- 如果是代码crash导致的重启 → 找到crash根因修复

**阻塞2 (P0)** — 必须提交代码
- 238个文件未提交, 服务运行的是重启时加载的磁盘代码
- 但: 服务已经在08:53重启过, 加载了磁盘代码(含6/27修复)
- 风险: 如果服务再次重启(systemctl restart), 会重新加载 — 但代码在磁盘上所以没问题
- 真正风险: git checkout/git pull会覆盖磁盘修改
- 修复: git commit保存所有修改

**阻塞3 (P1)** — 6/25只1条指令需排查
- 检查6/25日志中VotingCoordinator输出
- 如果是API失败 → 需要重试机制
- 如果是NO_TRADE regime → 检查VIX/SPY数据

**阻塞4 (P2)** — IC backfill周一自然验证
- 周一AFTER_HOURS时run_playbook_track会跑 → backfill应该工作
- 如果仍返回0 → 新鲜度检查修复无效, 需进一步调试

**Ponytail P1** — _fill重试3秒太慢
- 改为1次查询, 失败则log + 用filled_price=0标记未成交
- 不在主路径sleep 3秒

**Ponytail P3** — 删除25%检查
- max_position_pct=8%已是单仓上限, 不需要额外25%检查
- 25%无数据支撑, 是复杂度增加无收益

**总结**: 
1. git commit提交所有修改
2. Ponytail修复_fill重试(改1次) + 删除25%检查
3. 检查6/26服务重启原因(systemd/crash)
4. 周一PRE_MARKET前手动验证run_playbook_generate

### 第 1 轮：claude-code

**立场：Critic + 部分认同 + 两点修正**

1. 6/26 重启：journalctl 约 12 次 restart，全部显示 `ftseek-us.service: Succeeded.`，无 OOM/crash/exit code。不是 crash，是 systemd 正常 stop/start 循环，怀疑外部脚本或人工反复重启。

2. 6/25 仅 1 条：VotingCoordinator 在 6/25 日志里无报错；真正阻塞是 `AttributeError: DAY_TRADE`（exit_rule_registry 枚举已删）。不是投票失败，是退出规则崩溃导致生成链路中断。

3. P1 同意：3s 重试阻塞批量下单路径，改为 1 次查询 + warning 合理。

4. P3 同意但有前提：确认 `max_position_pct=8%` 已在 instruction_generator 生效并有单测，否则直接删除 25% 会裸奔。

5. 未覆盖风险：238 个未提交文件是最大周一隐患；必须 commit + 清 pycache + PRE_MARKET 手动验证，否则磁盘/运行态不一致会再次导致指令丢失。
