# DEB-20260928 · EXT `$50` 执行池价格影子（record-only）

- **立案日**: 2026-09-28
- **发起人**: 用户（“盘前和盘后能否直接选择股价大于50的，区别于盘中筛选，减少低价股票的干扰？ 减少对富途服务的无效请求连接”→ 确认）
- **状态**: **影子已落地（零交易行为变更）**
- **类型**: EXT 候选池运行成本/干扰观测，不是供给配额、不是绩效影子
- **关联**: `DEB-20260928-supply-price-tier-shadow.md`（ broader 价格层影子已撤回；本案仅观测 EXT 候选池，不复活供给配额）、`DESIGN-20260923-eth-execution-plan.md`（事件计划链已由 `scripts/eth_event_plan.py` 落地，本轮不改）

---

## 一、边界

1. 生产价格闸仍是 `EXT_ENTRY_MIN_PRICE=5` 默认值；`$50` 仅写入 `data/ext_entry_exec_price_shadow.jsonl`。
2. 影子复用每轮已经取得的榜单/快照价格，**不新增任何富途请求**。
3. 不改候选顺序、闸门顺序、下单判定、配额、平仓、事件计划状态机。
4. 现有 `eth_event_plan.py` 计划链保持只读，不切换执行器消费方式。

## 〇、P0.5 立案前强制检查（2026-09-28 复核后补登 — 立案时漏查注册表，违反同日 template 新增强制项，如实留痕）

- **同轴既有影子枚举**（grep `app/playbook/shadow_registry.py`，非凭记忆）：
  | 在册影子 | 关系 | 本案增量 |
  |---|---|---|
  | `eth_ext_entry_prefilter` | 同人群（盘前入场候选）、同轴（price_band 五档含 $50~200），粒度更细 | 该影子末行 2026-09-18、`EXT_PREFILTER_SHADOW` 默认 false 且 crontab 未传 ⇒ **已停采**（n=5/30 幽灵在册，处置属另案）。本案不是它的复活：本案按轮聚合记 `$5→$50` 二分反事实 + 排除明细，开关走 crontab 行内联、真在采（9/28 18:05 CST 起已有产物） |
  | `ticket_size_breakeven` | 费用轴机制推导：费用地板随价格递减（$5 档 52.6bps 全表最贵）**已定案** | 本案为运行成本/干扰观测轴，不在费用/绩效轴重复攒样（§三 禁止 2） |
  | `DEB-20260928-supply-price-tier-shadow`（兄弟案） | 更广价格层影子 | 已撤回；本案仅观测 EXT 候选池，不复活供给配额 |
- 既有裁决撞车普查：不触碰 `EXT_ENTRY_MIN_PRICE`（冻结 $5）、9-17 延长时段裁决、C4 影子先行纪律（本案即 record-only 影子）。

## 二、底牌

- 2026-09-28 05:50 ET 前 23 轮：1,054 个候选票次，864 次被现行 `$5` 价格闸拒绝，重复干扰明显。
  - **数据来源（复核后补标）**：轮数/候选票次 = `logs/ext_entry.log` 的 `[ext_entry] 闸计数[premarket] 候选=N` 行（该 log 行首无时间戳，须用前一条带时间戳行做锚；CST≤17:50:30 = ET≤05:50:30，23 轮 Σ候选 1054 / Σprice_gate 864）；拒绝数第二源交叉验证 = `data/extended_hours_price_gate_rejects.jsonl`（ts≤09:50:30Z，23 轮 Σn=864，逐轮一致）。**该 jsonl 无 candidates_n 字段，1,054 不能从它求得** — 复核者请勿走错路径。
- 首个 dry-run 冒烟（06:02 ET，不进判读）：47 只候选，现行 `$5` 价池 11 只，`$50` 影子价池 2 只。
- 事件计划链已存在并有 cron：今日 premarket 计划 45 行（6 planned / 39 ambiguous）；执行器尚未消费计划，因此请求量未因计划自动下降。

## 三、预注册读法

```
观测窗: 2026-09-28 18:05 CST 起的 cron 产物, 至少 3 个交易日 / ≥60 行。
必须报告:
  1. candidates_n / current_price_pool_n / shadow_execution_pool_n 的中位数与分布;
  2. 每轮 $5→$50 的候选池缩减比例;
  3. 实际 EXT 成交中有多少 entry_price < $50 (被该影子排除的人群);
  4. 影子价池与实际下单/成交的交集。
禁止:
  1. 本影子自动改 EXT_ENTRY_MIN_PRICE;
  2. 用 n<30 的成交样本直接裁决绩效;
  3. 把“影子价池更小”单独解释为“富途请求减少”（固定榜单/日历调用仍在）。
切换生产门槛必须另提裁决并获用户确认。
```

## 四、落地物

- `scripts/extended_hours_entry_executor.py`: `EXT_ENTRY_EXEC_PRICE_SHADOW`、分 session 阈值、JSONL 记录器。
- `tests/test_ext_entry_exec_price_shadow.py`: 默认关闭、分 session 阈值、反事实计数、dry/live 挂钩契约。
- EXT premarket/afterhours cron 前缀显式开启影子并设 `$50`。
- 产物：`data/ext_entry_exec_price_shadow.jsonl`。

## 五、验证

- 定向测试：`pytest tests/test_ext_entry_exec_price_shadow.py tests/test_extended_hours_entry_executor.py tests/test_extended_hours_order_execution.py -q` → 82 passed。
- 语法：`python -m py_compile scripts/extended_hours_entry_executor.py tests/test_ext_entry_exec_price_shadow.py` 通过。
- cron 实测：2026-09-28 18:05 CST 自动落盘一行，`candidates_n=47 / current_price_pool_n=11 / shadow_execution_pool_n=2`；主服务无需重启。

## 六、回滚

1. 删除两条 EXT cron 行中的 `EXT_ENTRY_EXEC_PRICE_SHADOW=true` 与 `EXT_ENTRY_EXEC_MIN_PRICE_*` 前缀。
2. Revert 本案代码与测试；无需重启 `ftseek-us`（cron 每轮新进程）。
3. 删除 `data/ext_entry_exec_price_shadow.jsonl`。
