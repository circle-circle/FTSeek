# 议题：全链路复盘 — 毛期望 = 0 的判决、退出几何互斥、尺子失效，与 R-1~R-6 安全补丁

## 状态

- [x] 进行中
- [ ] 待决策
- [ ] 已决策
- [ ] 已验证
- [ ] 已关闭

> 角色：本文作者 = 起案方（Qoder，用户令"全链路检查复盘框架/逻辑/策略/代码/风险/合理性，重点交易策略逻辑"→ 追加"先出 R1-R6 补丁做修模，然后引入 DEB 文档"）。
> 方法：3 个独立子代理并行深读（入场侧 / 退出侧+风控 / 架构+数据+测试）+ 主控直查 `data/ftseek_us.db`、`data/signal_snapshots.db` 与全部相关源码。**凡子代理论断与主控复核冲突处，一律以主控亲验为准并在文中标注修正**。
> 按宪章三角色制，**辩论方证伪式攻击待补**（合格标准：独立重跑 §二/§三 全部数字 + 至少一个攻击点）。
> 本文不改动八月终裁框架判据，但 §六 对"终裁是否具备数据基础"给出否定结论，需用户裁决。
> **补丁状态**：R-1~R-6 已落盘、全量 1474 passed、ConfigValidator 通过、**尚未重启服务**（改动需重启生效）。

## 背景

2026-08-23（ET 08-22 周六盘后）应用户要求做全链路复盘。基线：富途模拟盘，
`playbook.execute_mode: futu_simulate`，Playbook 单管线，主管线三锁冻结。

立项理由与 DEB-20260820 相同但结论更进一步：该案已指出"方向无 alpha、费用吃掉一切"，
本文补上**两个此前缺失的定量环节**——
① 毛期望的精确置信区间与统计功效；② 退出参数换版频率对全部历史结论的作用。
这两点合起来把"要不要继续调退出"从判断题变成了可计算题。

## 目标

1. 给出毛期望的**点估计 + 置信区间 + MDE**，而非只给符号。
2. 回答：**过去七周的退出侧迭代循环，在统计上是否有可能得出正确结论？**
3. 修掉与策略结论无关、但会造成资金损失或不可恢复状态的工程缺陷（R-1~R-6）。
4. 给出 8/31 终裁的输入与 9 月排期建议。

---

## 一、口径声明（对宪章七律逐条自检）

| 项 | 口径 |
|---|---|
| 样本域 | `trade_records WHERE source_type='PLAYBOOK_SIM' AND exit_time IS NOT NULL`。**正向白名单**，非黑名单（遵 D3 前置，防口径事故#1 第四次重演） |
| 干净窗口 | `trade_date >= '2026-07-01'`，n=321。理由见 §三：6 月 9 笔含 `SL_ATR_STOP` 管路 bug 三笔（-$2,416），属调整期事故非策略读数 |
| 盈亏口径 | 净额 = `COALESCE(pnl_net, realized_pnl)`；毛额 = `realized_pnl`。**费用已扣**（遵 D1 净额 SSOT） |
| 统计口径 | 单笔为观测单位时给 sd/SE/t；**跨信号 IC 一律另给按日聚类值**（见 §四，此为本文对既有记分牌的主要方法论异议） |
| 前视 | 全部为已实现平仓记录，无前瞻 |
| 环境 | `AUTO_TRADE_ACCOUNT=sim`，`TRADE_ENV=1`，无真实资金 |
| 复现 | 本文每张表下方给出可直接执行的 SQL / python 片段 |

---

## 二、判决一：毛期望精确等于零

干净窗口 n=321（2026-07-02 → 08-21）：

| 指标 | 值 |
|---|---|
| **毛盈亏/笔** | **+$0.10**（sd=60.4，SE=$3.37，**t=+0.03**，95%CI **[−$6.51, +$6.70]**） |
| 费用/笔 | −$4.54 |
| 净/笔 | −$4.44（t=−1.33） |
| 累计 | 毛 +$31.2 / 费 −$1,457.1 / 净 −$1,426.0 |

**全部净亏损 = 费用账单。** 毛边际不是"薄正"也不是"薄负"，是在 ±$6.7 的置信带内精确为零。

胜负几何（全期 n=330）：赢 n=180（54.5%）均 +$38.6 / MFE 1.35%；亏 n=150 均 −$54.7 / MAE 1.47%。
R:R = 0.71，而 WR 54.5% 的毛打平 R:R = 0.835 → 毛期望 −$3.85/笔，与上表 CI 一致。

**月度分解是核心证据**：

| 月 | n | 胜率 | 赢均 | 亏均 | R:R | 毛盈亏合计 |
|---|---|---|---|---|---|---|
| 7月 | 182 | 51.6% | +0.86% | −0.955% | 0.90 | **+$15.8** |
| 8月 | 139 | 59.0% | +0.756% | −1.085% | 0.70 | **+$15.3** |

胜率提升 7.4pp，毛利变化 −$0.5。这是**最优停止定理**的实证：无漂移过程上任何有界停止规则的
E[PnL] 恒为 0，调参只在"胜率"与"R:R"之间搬运，不改变乘积。

> **对 handoff.md §核心诊断的修正**：现记"信号 ≈ 掷硬币偏正，不是问题；漏血在退出"。
> 应改为：**信号精确是掷硬币（t=0.03）；退出既未杀赢家也未救亏损，它在数学上不可能改变毛期望；
> 漏血 100% 是费用。** 这条误诊把工程力量导向了唯一可证明无效的杠杆。

```sql
-- 复现
SELECT COUNT(*), ROUND(AVG(realized_pnl),4), ROUND(AVG(COALESCE(pnl_net,realized_pnl)),4)
FROM trade_records WHERE source_type='PLAYBOOK_SIM' AND exit_time IS NOT NULL
  AND trade_date>='2026-07-01';
```

---

## 三、判决二：调整期换参频率使七周内所有退出结论均不可得（本文最重要发现）

用户指出"前期弯路很多，7/8 月才沿 playbook 管线调试，历史数据多为调整期"。本文将其量化。

`metadata_json.$.config_version`（退出参数哈希，07-20 军规护栏的实际载体）分布：

| version | n | 窗口 | 净/笔 | SE | t | **该窗口 MDE** |
|---|---|---|---|---|---|---|
| (null) | 73 | 07-02→07-17 | +$2.61 | 7.63 | +0.34 | $21.4 |
| exit-acf193b6 | 54 | 07-20→07-23 | −$15.16 | 4.30 | −3.52 | $12.1 |
| exit-8ef6dbb1 | 31 | 07-24→07-29 | −$2.30 | 7.49 | −0.31 | $21.0 |
| exit-ed362592 | 54 | 07-30→08-06 | −$6.70 | 5.15 | −1.30 | $14.4 |
| exit-1c191a21 | 48 | 08-06→08-14 | −$19.76 | 6.78 | −2.91 | $19.0 |
| exit-7e4b2dc4 | 11 | 08-17 | +$24.10 | 13.23 | +1.82 | $37.1 |
| **exit-d0931263（当前）** | **50** | **08-17→08-21** | **+$6.38** | **14.40** | **+0.44** | **$40.3** |

**七周 = 7 个退出参数版本，平均窗口 46 笔 / 7.1 天。**

四条推论：

1. **跨版本极差 = 噪声**。观测极差 $43.85/笔（−19.76 → +24.10）；零期望假设下 7 个 n≈46
   样本均值的预期极差 = 2×1.96×60.1/√46 = **$34.8/笔**。观测/预期 = 1.26 —— 全部版本
   真实期望相同（且为零）这一假设**无法被拒绝**。版本间的好坏排序没有信息量。

2. **功效缺口 6.2 倍**。要测出 +$10/笔需 n=283；实际窗口 n=46。
   `handoff.md §每周迭代循环` 的"只改一个退出参数 → 下周同一张表对比 → 变好保留/变差回滚"，
   是在幅度 ±$21 的噪声上追踪 ~$5-10 的效应。**该循环的输出与掷硬币不可区分**，
   且因为"变好就保留"带有选择方向，它系统性地把噪声固化成参数——这是一台过拟合机器。

3. **两个"显著"版本正是假阳性的样子**。exit-acf193（t=−3.52）与 exit-1c191（t=−2.91）
   在 7 次多重比较下 Bonferroni 阈值为 |t|>2.69，前者仍"通过"；但版本内同日/同标的聚类
   未做修正（见 §四），实际 t 需再除约 √(每日笔数)。这解释了 handoff 里反复出现的
   "原称 X ❌ → 实测 Y"与"口径事故第 N 次重演"。

4. **当前参数集的 MDE 是 ±$40/笔**，而要战胜的费用只有 $4.54/笔。
   即：**在现行换参节奏下，系统的测量分辨率比它要解决的问题大一个数量级。**

> **这一节同时证明 R-4 的严重性**：`get_playbook_config()` 单例此前全仓无重置点，
> `playbook_horizon.yaml`（退出真源）不在热重载监控内 → 改 yaml 后 exit_config_version
> 哈希冻结在旧值，同一 hash 会横跨多个真实参数状态，上表的分桶本身都可能被低估。

```sql
-- 复现
SELECT COALESCE(json_extract(metadata_json,'$.config_version'),'(null)') ver, COUNT(*) n,
       MIN(trade_date), MAX(trade_date), ROUND(AVG(COALESCE(pnl_net,realized_pnl)),2)
FROM trade_records WHERE source_type='PLAYBOOK_SIM' AND exit_time IS NOT NULL
  AND trade_date>='2026-07-01' GROUP BY ver ORDER BY n DESC;
```

---

## 四、判决三：尺子失效（既有结论的可信度上限）

### 4.1 `okr_latest.json` 的 `signal.ic_t1=+0.1265 (n=1813)` 不可复现

主控用 `signal_snapshots` 全表 4,057 行独立重算：

| 列 | n | pooled IC | t | **按日聚类 daily-mean IC** | t | 天数 |
|---|---|---|---|---|---|---|
| fwd_ret_t1 | 4005 | −0.0369 | −2.34 | **+0.0136** | **+0.62** | 61 |
| alpha_t1 | 4005 | −0.0410 | −2.60 | +0.0139 | +0.63 | 61 |
| alpha_t5 | 1605 | −0.0655 | −2.63 | +0.0230 | +0.54 | 26 |

`strength` 五分桶前瞻收益完全非单调：Q1 +13.2 / Q2 −2.1 / Q3 +45.3 / Q4 −63.6 / Q5 +5.0 bps。
**无任何单调关系**。OKR 的 +0.1265 系 `days_back=30` + 排除 disabled 策略的子集选择产物
（`app/analysis/signal_ic_tracker.py:445-505`），被当作"信号有预测力"的头条 KPI 使用。

### 4.2 `playbook.ic_t1=−0.32 (n=256)` 在测量常量

该 IC = `Spearman(confidence, fwd_return)`（`app/playbook/ic_tracker.py:570`）。而 confidence 已退化：
DB 直方图 `{25:5, 35:242, 50:11, 60:2, 70:92}`，且 08-14→08-21 全部行恒 = 70
（子代理复核，`BridgeVotingAdapter` 只产 0.50±0.20/−0.15 三个离散值，`pipeline.py:781-799`）。
对近似常量做秩相关，结果由并列值处理方式决定。**而它被接成毕业门禁**（`_check_graduation`，
okr `ic_positive: false`）。

### 4.3 方法论主异议：同日聚类未修正，显著性系统性夸大约 8 倍

4,057 信号 / 61 交易日 ≈ 67 信号/日，同日信号高度相关（共同市场因子）。
把它们当独立观测，t 统计被夸大约 √67 ≈ 8 倍。这是一台**稳定生产"显著"发现且永不复现**的装置。

**建议立为宪章第八律**：任何跨信号统计必须给按日聚类值，任何判据必须附 MDE；不带 MDE 的结论不予采信。

### 4.4 归因谬误：两处（含本文作者自己踩的一处）

- 既有审计称"唯一验证优势 = 退出锁利（`SL_TRAIL_LOCK` n=71 净 +$2,886，WR 91.8%）"。
  移动锁利**只在已浮盈的仓上触发**，WR 91.8% 是同义反复，不是 edge。
- **作者自我勘误**：主控中途发现"SHORT 是唯一正向切片（n=60，净 +$19.07/笔）"，
  逐月拆开后证伪 —— 6 月仅 4 笔贡献 +$1,053（含一笔 `FORCED_CLOSE` +$802 outlier）；
  干净窗口 SHORT n=56 毛 +$6.51/笔（t=+0.82）、净 +$1.62（t=+0.21）= **零**。
  同理 LONG 全期 t=−2.27 的"显著负"亦由 6 月 5 笔（含 `SL_ATR_STOP` −$981/−$829）撑出，
  干净窗口 LONG 毛 −$1.26（t=−0.34）= 零。
  **两处都是"把 330 笔切到出现显著为止"的同一个陷阱**，如实记录以立此存照。

---

## 五、退出几何自相矛盾（定量）

复核实际参数（**与 handoff.md 记载有两处出入**）：

| 项 | handoff 记载 | 代码实况 |
|---|---|---|
| `breakeven_at_pct` | 0.5 | **0.4**（`config/playbook_horizon.yaml:57`，30+ 因子档一致；已活体验证） |
| `rr_ratio` | 999（固定止盈关闭） | **2.0**（`playbook_horizon.yaml:373`）；TP 真源 = `vol_buckets.tp_multiplier`，**固定 TP 是通电的** |

推演：
- 保本武装门 = MFE ≥ 0.4%（`pipeline.py:6244`）
- 触发线垫层 pad = fee_cushion + slip_cushion ≈ **0.30–0.35%**（`_compute_dynamic_effective_sl:5710-5713`）
- ⇒ **武装瞬间止损线在峰值下方仅 0.05–0.10pp**，任何 5–10bps 回踩即出场
- 回撤锁 0.8% 需 `peak(1−0.008) > entry(1+pad)`，即 MFE > **1.16%** 才接管
- ⇒ **MFE ∈ [0.4%, 1.16%) 的仓，可实现利润被钉死在 ≈0.35%**
- 亏损侧 SL floor 2.0–2.5% ⇒ **R:R 上限 = 0.14–0.28**

要达 R:R 1.29 需赢家吃到 3.23%，即 MFE ≥ 4.03% —— 是实测 MFE 中位数（1.5%）的 2.7 倍，
且已顶到 TP 线（1.8×2.5=4.5%）。**0.4/0.35/0.8 这组几何与 1.29 目标数学互斥。**

账本铁证：`SL_BREAKEVEN` **n=80（占全部 24%）**，毛 +$207.9，**净 −$174.4** —— 八十笔纯费用捐赠。

按 exit_reason 资金流（全期）：
- 进钱：`SL_TRAIL_LOCK` +$2,886（n=71，但见 §4.4 归因谬误）、`PRE_CLOSE_EXIT` +$826（n=43）
- 出血：`SL_ATR_STOP` **−$2,416（n=3，持仓 0.0h，全在 06-24）** = 主线 ATR 止损打到 Playbook 仓的**管路 bug**；
  `SL_INITIAL` −$1,542（n=8）、`MFE_STALL` −$1,353（n=31，现已断电）、`TIME_EXIT` −$1,195（n=36）

---

## 六、对 8/31 终裁的输入

**建议投 STOP（针对当前策略前提），而非 GO 或"靠调参 PIVOT"。** 理由不是亏了 $1,426（该金额无意义），而是：

1. 毛期望 95%CI = [−$6.51, +$6.70] 覆盖零；全样本 MDE $9.43/笔，当前参数窗口 MDE $40.3/笔
   → **G2「日净盈亏均值>0」与 G3「日毛利均值 t>1」在现有样本下不可能被可靠判定**。
2. 唯一被工程验证过的"优势"经检验均为条件化于结果的归因谬误（§4.4）。
3. 测量装置以约 8 倍夸大显著性（§4.3），且退出参数以 7.1 天/次的节奏变动（§三）
   → **在修好尺子并冻结参数之前，任何终裁都是在噪声上做决策。**

---

## 七、已落地：R-1~R-6 安全补丁（与策略结论无关，独立成立）

> 全部为 fail-close 方向、全部带配置开关可回滚、零策略参数改动。
> 定位方法：3 子代理并行深读 + 主控逐条亲验（3 处子代理论断经主控证伪，见文末勘误）。

### R-1 Playbook 路径补 VIX 门 + per-symbol 互斥
- **缺陷**：`order_executor._submit_order_inner` 有「VIX 新鲜度 fail-close + VIX>28 硬拒」两道门
  （`order_executor.py:3199-3232`），但 Playbook 不经 order_executor —— `dual_broker.submit_order`
  直连 `ctx.place_order`（`dual_broker.py:223/415/660`，全文零 `order_executor` 引用）。
  `grep -n "vix" app/playbook/pipeline.py` 零拦截命中，`entry_vix` 只被记账不被消费。
- **另**：`duplicate_position_guard` 读 `open_positions`（`playbook_service.py:1056`），
  而该表在下单成功**之后**才写（:1556）→ TOCTOU；`try_claim_execution` 只对同一
  `instruction_id` 原子，挡不住不同 instruction_id 的同标的并发（代码注释自述 7/2 GOOG SHORT×2 实锤）。
- **修法**：`playbook_service.execute_instruction` 补 VIX 两道门（新鲜度 fail-close + VIXGuard 阈值硬拒）；
  `_execute_via_dual_broker` 改为 per-symbol 非阻塞互斥包装层，真实逻辑移入 `_execute_via_dual_broker_inner`，
  使「查重→下单→写 open_positions」成为临界区。
- **开关**：`risk.playbook_vix_gate_enabled: true`

### R-2 孤儿仓不再自动伪造平仓
- **缺陷**：`position_sync.py` 对"broker 无 + open_positions 无 + 非 playbook 仓 + 建仓>1天"
  直接 `UPDATE exit_price=entry_price, realized_pnl=0`。三个判据全是单点判断，
  且 broker 侧只查单一 env（`:577` `_envs_to_query` 单元素）。误判后果不可逆：
  券商真实持仓被写上 `exit_time` → `monitor_playbook_sim_exits` 的 `WHERE exit_time IS NULL`
  **永久查不到 → 裸仓无止损且不自愈**；同时向毕业统计注入伪造 pnl=0 样本。
- **修法**：默认仅告警 + 落 `data/audit/orphan_trade_records.jsonl` 待人工 adopt。
- **开关**：`reconcile.orphan_autoclose_enabled: false`（原行为 = true）

### R-3 `TRADE_ENV` 进程内固化 + 路由一致性断言
- **缺陷**：`db_utils.is_live_mode()` 每次调用重读 `.env.us`（docstring 自述"无需重启进程"），
  而 broker 路由用 `os.getenv("AUTO_TRADE_ACCOUNT")`（`position_sync.py:576`）需重启 → **不同源**。
  运行期改一个字符 → `get_trade_records_db()` 立刻指向 `ftseek_us_live.db` 而 broker 仍 SIMULATE
  → `monitor_playbook_sim_exits` 查到 `rows=[]` 直接 `return 0`（`pipeline.py:6890`）
  → **全部在仓静默失去止损监控，零告警**。此前零测试覆盖。
- **修法**：首次解析后固化（`_LIVE_MODE_CACHE`），并与 `AUTO_TRADE_ACCOUNT` 做一致性校验，
  不一致记 CRITICAL `ENV_ROUTING_MISMATCH`。逃生口 `FTSEEK_ALLOW_RUNTIME_TRADE_ENV_SWITCH=1`（仅测试）。

### R-4 退出 SSOT 纳入失效检测
- **缺陷**：`get_playbook_config()` 单例全仓无重置点；`ConfigHotReloader` 只监控
  `unified_config.yaml` 且回调只同步 `ConfigContainer`；`config/playbook_horizon.yaml`
  （LIVE 退出真源）完全不在监控内。双重后果：① 改 yaml 后半个系统看新值；
  ② `_exit_config_version` 哈希冻结 → §三 的分桶被低估，07-20 军规护栏静默失效。
- **修法**：按 `playbook_horizon.yaml` 的 mtime 自动失效重建（节流 5s，因退出循环每仓每轮都调），
  新增 `reset_playbook_config()`，重载时 WARNING 打印 `exit_config_version` 新旧对照。

### R-5 资本安全水位硬门接入
- **缺陷**：`can_open_position()` 全仓唯一消费者是 `web_dashboard/order/validator.py:162`，
  **Playbook 路径从未调用** → RED(−10~−20%) / BLACKOUT(>−35%) / 系统锁定 / `_save_failed`
  全部拦不住；且 sizing 侧 `pos_mult=0` → size=0 被 `instruction_generator.py:510`
  的 `max(1, position_size)` 兜成 **1 股** → 回撤 −20% 仍开仓。
- **修法**：`execute_instruction` 接入 `can_open_position()` 硬门，异常 fail-close。
- **开关**：`risk.capital_safety.playbook_hard_gate: true`

### R-6 下单路径最小安全网
- **缺陷**：`grep -rln "_submit_order" tests/` = **0**；`dual_broker` 下单、`position_sync`
  2,414 行对账、三态切换全部零覆盖。"1445 tests passed"对资金安全无指示意义。
- **修法**：新增 `tests/risk/test_r1_r5_entry_gate_contracts.py`，21 例锁住 R-1~R-5 的
  fail-close 语义（VIX 三态 / 开关可回滚 / 互斥并发+异常释放 / 水位三种拒绝原因 /
  路由一致性告警 / 单例失效）。

### 验证
- 全量 `pytest -q`：**1474 passed**（原 1445 + 新增 21 + 8 参数化），零失败。
- `ConfigValidator(ConfigLoader()).validate_all()` → `is_valid: True 全部通过`。
- 活体验证：`get_playbook_config()` 单例复用/reset 重建/mtime 失效均按预期；
  `is_live_mode()=False`、`main_db=ftseek_us.db`、无 `ENV_ROUTING_MISMATCH`。
- **副产物（真 bug）**：`tests/test_playbook_service.py::TestDirectionGateC` 5 例因新门 fail-close 而失败，
  根因是该 helper 的 `capital_safety` mock 让 `can_open_position()` 返回 MagicMock 而非 2-tuple。
  已扩展 `_patch_gates_ok` 放行新门 —— **补丁行为正确，是既有 mock 不完整**，未改产品代码。
- **门序发现**：上游 OKR 熔断门（`playbook_service.py:721`）也调 `get_capital_safety_system()` 取 equity，
  故资本状态不可读时它先 fail-close。两门互为冗余，任一失效另一仍兜住；契约测试据此调整断言。

---

## 七之二、R-7 Playbook 排他性专项（用户追问"是否存在逻辑上双线管理"）

### 7.1 2026-06-24 事故成因已确证：护栏当时不存在，非护栏失效

| 项 | 证据 |
|---|---|
| 事故 | `PLAYBOOK_SIM` 仓被 `SL_ATR_STOP`（主线 ExitRuleRegistry 规则）平掉 3 笔，全 06-24，持仓 0.0–0.1h，净 **−$2,416.2**（ORCL −975.6 / XOM −822.8 / LMT −600.9）；另 `PLAYBOOK_SHADOW` 4 笔 06-23 −$465.4 |
| 护栏落地 | `is_playbook_open_position` 首现于 commit **da49266d（2026-07-16）**；`_close_position` 兜底为 **00c8e795（同日）** |
| 结论 | **事故早于护栏 3 周 = 当时根本没有排除逻辑** |

**回归证据（强）**：主线可归因的全部 exit_reason 在 PLAYBOOK_* 仓上的最后一笔日期均 ≤ 2026-07-15
（`SL_ATR_STOP`→06-24、`EOD_FORCE_CLOSE`→07-02、`BROKER_GHOST_CLOSE`→07-15、`TRAILING_STOP`→07-15）。
07-16 之后 **252 笔** playbook 平仓，exit_reason 仅含 playbook 原生族，**5 周零污染**。

```sql
-- 复现（应返回空集）
SELECT trade_date, symbol, exit_reason FROM trade_records
WHERE source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') AND exit_time IS NOT NULL
  AND trade_date >= '2026-07-16'
  AND exit_reason IN ('SL_ATR_STOP','TRAILING_STOP','TRAILING_STOP_BY_MAIN',
                      'EXIT_MONITOR_CLOSE','FORCED_CLOSE_OKR_AUDIT');
```

### 7.2 排他性的真实机制 ≠ 逐条平仓路径拦截

**关键架构认识**：排他性靠的是 **playbook 仓永不进入主线内存 `position_store`**。
主线全部退出机械（`monitor_loop` → `_check_positions` → `position_store.managed()`
→ DEM/ExitRuleRegistry/FLAT_ALL）只消费 `position_store`，因此是**"断粮"而非"断电"**。
`is_playbook_open_position` 的角色是**边界看门人**（恢复 / 对账 / 补录三个入口），不是平仓点拦截器。

今日活体证据（重启后日志）：
```
[Reconciler·playbook] US.WEAV / US.BABA → 跳过 broker_only 补录 (不进主线 store)
[持仓恢复·playbook] 跳过 US.WEAV / US.BABA (不纳入主线 PositionStore)
```
当前 2 笔未平仓（全 `PLAYBOOK_SIM`，零遗留/手动仓）已在启动时被挡在主线 store 外。

### 7.3 逐路径判定（今天是否通电）

| # | 路径 | 排他依据 | 判据 | 今天 |
|---|---|---|---|---|
| 1 | `position_monitor.monitor_loop` | 无内联护栏；靠 :352 恢复排除 + :1142 对账排除 | 消费 `position_store` | **通电但断粮** |
| 2 | `position_exit_monitor` DEM 逐仓 | :799 `owner_strategy` 匹配（弱，空值即漏，正是 07-16 修的 bug）+ :882 `_pb_check_em` 查 DB（真闸门） | :799 fail-open / :882 见 7.4 | **通电但断粮** |
| 2b | `trailing_stop`（= `SL_ATR_STOP`） | :259-285 Dispatcher 缺省禁用，**Registry 内层缺省=启用，仅靠此路径存活** | 恒启用（日志实证告警） | **规则活着，但收不到 playbook 仓** |
| 2c | **DEM `FLAT_ALL` / `REDUCE_50`** | :719/:750 直接迭代 `managed_positions` 调 `execute_close_fn`，**在 :882 闸门之前** | 原仅剩 `_close_position` 兜底（且 fail-open） | **原为结构性缺口 → R-7c 已修** |
| 3 | `order_executor._close_position` | :4277-4285 调 `is_playbook_open_position` | 原整块 `except: pass` = fail-open | **R-7b 已修** |
| 4 | `is_playbook_open_position` | 唯一闸门 | `OperationalError`→True；**其他异常→False = fail-open** | **R-7a 已修** |
| 5 | `position_sync` | 四道：:713 ghost / :768 孤儿 / :1370 新仓 / :2300 超配减仓；:1377 ghost 窗口默认归 playbook_sim | 最完善 | 通电·已守 |
| 6 | `manual_position_guard` | 模块自述只告警不平仓 | 无平仓能力 | 通电·**无害** |
| 7 | `emergency_interceptor` / KillSwitch | `USEmergencyInterceptor` 实例化在 **main.py:520-521 已注释** | — | **未通电**（仅作入场闸，无强平） |
| 8 | `account_circuit_breaker` LIQUIDATE | 全部 3 个消费点只拒新单 | — | **"日亏3%强平"未实现**；真正组合强平是 DEM FLAT_ALL（另一套） |
| 9 | `_force_close_orphaned_sim_positions` | 在 `pipeline.py:7646`，由 :2773 调 | — | 通电，但**属 playbook 自身**，非双线 |
| 10 | Dashboard 手动平仓 | `web_dashboard/order/executor.py` grep playbook = **0 命中**，直调 `place_order` | 无护栏 | **通电 · 剩余最大缺口（人工触发）** |
| 11 | 入场三锁 | ①`signal_pipeline.py:182-183` 硬编码 ②`order_executor:3128-3134` 白名单 ③`main_scan_enabled` | ③原代码缺省 **True** = fail-open | **R-7d 已修**；①②完好 |

### 7.4 四项 fail-close 加固（R-7a~d）

**R-7a `is_playbook_open_position` 通用异常 → fail-close**（`pipeline.py:5777+`）
返回 `False` 的语义就是"主线可以平这个仓"，故一个 schema/路径级结构性错误即可让主线对**全部标的**接管。
原注释顾虑 fail-close 会"让主线对所有 symbol 永久失管、漏平真实风险仓" —— 该顾虑今已弱化：
Playbook 有独立退出宿主，闸门失效时"主线接管"**严格劣于**"主线跳过 + CRITICAL 告警"。
且实测当前零遗留仓，fail-close 今日零代价。

**R-7b `_close_position` 兜底护栏去掉 `except: pass`**（`order_executor.py:4273+`）
纵深防御的最后一层不能自身静默失效——它是 `FLAT_ALL/REDUCE_50` 的唯一拦截点。

**R-7c DEM `managed_positions` 源头剔除 playbook 仓**（`position_exit_monitor.py:681+`）
不是再加一道拦截，而是让组合层与逐仓层**共享同一份已过滤集合**，从结构上消除双线可能。
护栏导入失败时整段跳过组合层（与既有 `is_strategy_managed` 导入失败的安全降级同构）。

**R-7d `main_scan_enabled` 缺省 True → False**（`signal_pipeline.py:251/771`，两处）
入场侧三锁中唯一 fail-open 的一道；异常分支亦改为保持静默并告警。

**新增开关**：`risk.playbook_guard_fail_mode: closed`（改 `open` 退回补丁前行为）

### 7.5 验证
- 新增 `tests/risk/test_r7_playbook_exclusivity.py` **14 例**，含一条**账本不变量测试**
  （断言 07-16 后 playbook 仓不再出现主线来源 exit_reason，可持续守护回归）。
- 全量 `pytest -q`：**1488 passed**，零失败。
- 重启 + `startup_smoke_check.py --wait 90`：**七条断言全通过**，零 Traceback/CRITICAL。
- 活体：`WEAV`/`BABA` 两笔 playbook 仓在 Reconciler 与持仓恢复两个入口均被正确排除。

### 7.6 结论与剩余缺口

**结论：排他性今天成立。** 入场侧三锁完好（现三锁全为 fail-close）；退出侧靠 store 边界隔离
+ 三层护栏（边界看门人 / DEM 源头过滤 / `_close_position` 兜底），R-7 后**三层全部 fail-close**。
账本层面 5 周 252 笔零污染。

**剩余缺口（均需人工触发，未修）**：
1. **Dashboard `/api/risk/emergency-close`（`risk.py:306`）与 `/api/orders/close`（`trade.py:83`）零 playbook 感知**，
   emergency-close 直接迭代 broker 原始持仓全平。缓解：需 `confirm=CONFIRM` + `EMERGENCY_CONFIRM_TOKEN`
   （未配置则 503）。**一按即穿透全部护栏** —— 建议加 playbook 仓二次确认，待批准。
2. `/api/positions/takeover`（`trade.py:173`）可改仓位 owner 为 `auto`；对 playbook 仓的实际可达性**未证实**。
3. `PLAYBOOK_SHADOW` 不在 `is_playbook_open_position` 白名单（`pipeline.py:5800`）——
   影子仓无真钱，危险度低，但 2026-06-23 那 4 笔正是 SHADOW 被主线平掉。

**Retrospective 修正**：我在 §8.1 原把 R-7 描述为"关掉主线 ATR 止损对 Playbook 仓的可达路径"。
经审计该描述不准确 —— `trailing_stop`/`SL_ATR_STOP` 规则本身不必关（它服务遗留/手动仓），
真正要修的是**三处 fail-open 与一处结构性绕过**。R-7 按修正后范围实施。

---

## 七之三、R-8 时区专项（用户令"深度全面检查是否还有其他异常逻辑冲突"）

### 8.0 环境前提（实测）
宿主时区 = **Asia/Shanghai (CST, UTC+8)**，`TZ` 未设置，`time.tzname=('CST','CST')`，`daylight=0`。
交易市场 = 美股 ET（EDT=UTC-4 / EST=UTC-5）。两者差 **12h（夏）/ 13h（冬）**，
且 **ET 下午 = 北京次日凌晨** —— 任何"日期"语义的裸 `datetime.now()` 都会把 ET 下午事件记到 +1 天。
`pytz==2026.1.post1` 在 venv 内（fallback 路径现网不可达，但见 8.4）。DST 切换：**2026-11-01 起 EST**。

### 8.1 真 bug 与修复

**R-8a 遥测/审计日期错位（两个 emitter + 一个复盘落盘）**

| 位置 | 后果 | 状态 |
|---|---|---|
| `pipeline.py:4269` `_emit_trigger_miss` | 15,150 行中 **9,049 行（59.7%）** date +1 天；文件内 `2026-08-22 Sat` 非交易日即铁证 | ✅ 改 ET |
| `pipeline.py:3098` `_emit_exec_gate_audit` | **本轮新发现（测试抓出）**。它是"每个到达执行漏斗的候选单"的审计真源，比 trigger_miss 更核心 —— 执行漏斗归因/拦截率分层全靠它 | ✅ 改 ET |
| `catalyst/postmarket_review.py:302` | 盘后复盘在 ET 收盘后跑（ET 16:00+ = CST 次日 04:00+）→ 交易日 D 的复盘被标成 D+1，**文件名也错一天** | ✅ 改 ET |

受污染的下游（按 date 分组或与 `playbook_instructions.date`(ET) 关联）：
`scripts/early_acceptance_check.py:168`、`first_night_gate_report.py:101`、
`stale_anchor_shadow_tag.py:175`（`inst["date"]==today_et` 对 ET 下午行**恒不成立**）、
`gate_block_counterfactual.py:79,101`、`gate_cost_report.py:48`、`trigger_miss_analysis.py:57`、
`shadow_board.py`、`data_freshness_watchdog.py`。
→ **门效果"尺子"被污染，参数裁决基于错日样本**。这与 §四「尺子失效」是同一类问题的第四个实例。

新行加 `tz:"ET"` + `ts_cst` 双标记，使下游能区分修复前后并剔除历史污染（契约测试已锁）。

**R-8b 半日市未被识别（有日期的定时炸弹）**

`app/core/scheduler_triggers.py:83` `get_market_phase` 硬编码 `hour < 16`。它是**主调度的市场时段 SSOT**，
消费方：`signal_pipeline.py:161`、`llm_pipeline.py:172`、`scheduler_core._is_market_open`、
`position_monitor.py`、`market_data_stage.py:123`、`signal_stage.py:446`。
`app/utils/timezone.py::is_market_open` 同样固定 `<= 16.0`。

**关键实证**：`holiday_calendar` 里 `US_EARLY_CLOSE_DATES` 与 `get_us_close_minutes_et` **早已存在**
（2026-07-25 加），但这两条路径从未使用它。且 —— `get_market_phase` docstring 自述
"节假日（含半日市）当作 WEEKEND" **对 2026-11-27 不成立**，我实测：

```
2026-11-27 | is_us_holiday=False | close_mins=780(13:00) | in_EARLY_CLOSE=True | in_HOLIDAYS=False
2026-12-24 | is_us_holiday=True  | close_mins=780        | in_EARLY_CLOSE=True | in_HOLIDAYS=True
```

即 **2026-11-27（感恩节次日）与 2027-11-26 不在假日表里**，只能靠半日市逻辑兜住 —— 而它不存在。
修复前该日 ET 13:00–16:00 被判 `TRADING`，3 小时向已闭市市场生成信号；
playbook 入场触发窗截止 14:00 ET，故 **13:00–14:00 会真下单**。
（PRE_CLOSE_EXIT 侧本来就正确，`pre_close.py:51` / `pipeline.py:3969,6944` 三处已半日市感知。）

修法：两处均改用 `get_us_close_minutes_et(当日)` 动态取收盘分钟数，并据此修正 `AFTER_HOURS` 起点；
取值失败兜底 960（与该函数 docstring 约定一致）。另 `is_market_open` 上界 `<=` 改 `<`（16:00 整已收盘）。

**R-8c `_et_trade_date` 的 DST 错误（SSOT 口径）**

`app/utils/timezone.py:99` 对 naive 输入用固定 `-timedelta(hours=12)`，
原注释断言"减 12h 对日期边界始终正确"**不成立**：CST→EDT 差 12h，CST→**EST 差 13h**。我实测：

| 输入（naive CST） | 修复前 | 真值 |
|---|---|---|
| 2026-08-15 12:30（夏） | 2026-08-15 | 2026-08-15 ✅ |
| **2026-01-15 12:30（冬）** | **2026-01-15** | **2026-01-14 ❌** |
| **2026-12-20 12:45（冬）** | **2026-12-20** | **2026-12-19 ❌** |

错位窗口 = CST 12:00–12:59（对应 ET 前一日 23:00–23:59，美股完全闭市）→ **历史实际血量 ≈ 0**。
但它是全仓交易日口径 SSOT，且注释是错的，故改为按 CST 时区正确换算（pytz 缺失时回落原行为）。
修复后三档全部与真值一致（已实测）。

### 8.2 ⚠️ 我纠正的两处子代理误判（重要，避免误修）

审计子代理把 **A2/A3 列为 P0**：声称 `_is_overnight_position`（`pipeline.py:476`）与
`_horizon_max_hold_hit` 读到的是 **CST** 时间，导致 `OVERNIGHT_GAP_STOP` 永不触发、
`MAX_HOLD_T5` 晚一天。**我逐条追踪数据来源后判定该结论错误**：

1. 退出循环 `monitor_playbook_sim_exits`（`pipeline.py:6899`）的 `entry_time` 取自
   **`trade_records`**，而该表存的是 **ET**（近期行带 `-04:00`，旧行为 ET naive）。
   实证：`US.AVGO entry_time=2026-08-21T10:49:21-04:00` = ET 上午，合理。
   故 naive 分支 `_entry.date()` 拿到的已经是 ET 日期，**函数本身正确**。
2. 子代理的证据取自 **`open_positions.entry_time`**（`US.WEAV|2026-08-19T21:36:54`），
   那是 **CST**，但该表**不被退出链消费**（消费方仅 `position_sync:719` 读 action_log、
   `data_service:1101`、`order/executor.py:666` 读 thesis）。
3. 另一独立事实：`OVERNIGHT_GAP_STOP` **本就是死规则** —— `pipeline.py:6188-6189`
   在 `overnight_hold_enabled: false` 时强制 `_skip_allowed=False`，而 `:6457` 要求该标志。
   实测历史触发次数 = **0**。即便时区有问题也早已被配置层短路。

**→ A2/A3 未修，因为它们不是 bug。** 这与我在 §4.4 自证伪 SHORT 切片是同一类纪律：
子代理给的 file:line 都对，但数据流向假设错了。

### 8.3 本轮新发现（未修，需决策）

**两表 `entry_time` 相差 12 小时，同一个仓两种时间约定**：

| 表 | US.WEAV | US.BABA | 约定 |
|---|---|---|---|
| `open_positions` | `2026-08-19T21:36:54` | `2026-08-21T21:47:57` | **CST naive** |
| `trade_records` | `2026-08-19T09:36:54` | `2026-08-21T09:47:57` | **ET naive** |

同一逻辑事件，两处相差恰好 12h。目前无害（两表各自的消费方口径自洽，退出链只读
`trade_records`），但**任何跨表 JOIN 或把 `open_positions.entry_time` 喂给按 ET 判断的函数
都会立刻出错**。属于埋着的口径地雷。修它需要数据迁移（存量行改写），建议单独立项而非顺手改。

另 `trade_records.entry_time` 自身格式混用：ET-aware 323 行 / ET-naive 353 行 / CST-aware 1 行
（`US.BULL`，`position_sync.py:63,68` 写的 `datetime.now().astimezone()`）。那 1 行会使
`_et_trade_date` 走 aware 分支按 CST 转 ET，日期正确，但它使 `trade_date` 列写成 CST 日 →
账户熔断日亏查询（按 `_et_trade_date(now_et())`）可能漏计该笔。n=1，低危，已记录。

### 8.4 分档清单（完整审计结论）

**已修（真 bug）**：R-8a×3、R-8b×2、R-8c。

**隐患（条件触发，未修）**：
- `timezone.py:32-40` pytz 缺失 fallback：`ET_OFFSET` 在 import 时由 `time.localtime().tm_isdst`
  决定，而 Asia/Shanghai 恒 0 → **永久 EST**，夏令时慢 1 小时；且 fallback 版 `to_et()` 直接
  `return dt` 不做转换。现网不可达（crontab 全部显式用 `venv/bin/python`），但手工
  `python3 scripts/x.py` 即落入。建议改 `zoneinfo`（stdlib，无依赖）。
- **crontab 注释写 "ET xx:xx" 但 cron 是本地 CST，2026-11-01 起 EST 全部偏 1 小时**。
  高危项：`5 4 * * 2-6 live_reconcile_check.py`（注释盘后，EST 下变 **ET 15:05 盘中对账**）、
  `45 4 * * 2-6 e2_momentum_shadow_daily.py`（EST 下 ET 15:45 盘中，`replay_bars` 未就绪）、
  hermes `0 4 * * 2-6` 盘后综合复盘（EST 下 ET 15:00 盘中做"盘后"复盘）。
  → **建议 11-01 前批量校准，或给 cron 加 ET 换算包装**。
- hermes `DEB-20260815 硬止损监控` 的 DoW：`*/30 21-23,0-4 * * 2-6`。CST `21-23` 段应为 `1-5`
  才对应 ET 周一至周五，统一挂 `2-6` 使 **周一 ET 09:00-12:00 无监控**（该脚本负责触线时把
  `playbook.auto_execute` 置 false）。**待用户确认后修**（改 hermes 配置不在本轮授权范围）。
- `order_executor.py:2950` 反向冷却门：`now_et() - exit_dt` 遇 legacy naive 行抛 TypeError 被
  `except` 吞 → `return True` 放行冷却门。现网新行为 ET-aware，仅历史行触发。

**无害（明确勿动，防后人误修）**：
纯 elapsed 测量（`pipeline.py:7026` held_minutes 同侧相减、`execution/idempotency.py` 幂等窗、
各处 `time.time()` 缓存 TTL/节流）；纯本地日志/推送时间戳（`notifier/*`）；
`except` 兜底里的裸 `now()`（主路径已 ET，仅 timezone import 失败时可达 = 死路径）；
日志去重键用 CST 日（只影响多打一条日志）；
LLM 降级兜底的 `date`（`agent_bridge.py:713`、`debate_planner.py:749` —— `targets: []` 空计划，
且盘前跑时 CST 与 ET 同日）；
**日内计数器/熔断这一批本来就是对的**：`absolute_circuit_breaker.py:72,99`、
`account_circuit_breaker.py:77`、`drawdown_control.py:125`、`symbol_daily_loss_guard.py`、
`pdt_guard.py`、`trade_store.py:841,929` 全部 `_et_trade_date(now_et())` 口径；
**数据新鲜度无时区错配**（重点复核）：`data_health_guard.py:112` 用 `time.time() - ts.timestamp()`，
provider 写 naive CST，`.timestamp()` 按本地解释 → 口径自洽，age 无负数/超大值。

### 8.5 验证
- 新增 `tests/test_r8_timezone_contracts.py` **29 例**（DST 七档参数化 / 半日市逐时段 /
  兜底降级 / 遥测源码断言 / 历史污染可度量）。**该测试在编写过程中直接抓出 R-8a 的第二个
  emitter（`_emit_exec_gate_audit`）**，是本轮测试产生真实价值的实例。
- 全量 `pytest -q`：**1517 passed**，零失败。
- 重启 + `startup_smoke_check.py --wait 90`：七条断言全通过（09:27 CST）。
- 活体实测：DST 三档与真值一致；半日市 2026-11-27 在 13:00 转闭市、普通日 2026-11-24 仍 16:00。

---

## 七之四、R-9 ~ R-13（用户令"全修复并更新"）

### 9.1 R-9 Dashboard 紧急全平的 playbook 感知 ✅已落地

`app/web_dashboard/routes/risk.py::emergency_close` 原直接迭代券商原始持仓调
`ManualOrderService.close_position`，**零 playbook 感知** —— 一按即穿透 R-7 建立的全部
排他性护栏，与 `monitor_playbook_sim_exits` 争抢同一批仓。

**设计取舍**：不简单拦掉（真紧急时人就是要全平），而是消除**最危险的语义 ——「静默部分平仓」**
（人以为账户已全平，实际 playbook 仓还在裸奔）：
- 默认：跳过 playbook 自管仓，但在响应里显著报出（`skipped_playbook` + `fully_flat: false`）
- `include_playbook: true`：连 playbook 仓一起平，**且先激活 Kill Switch** 防止平完当日指令
  立刻重新入场（用 Kill Switch 而非改 yaml，因 `ConfigWriter.update_param` 有 `PARAM_MAP`
  白名单且 `playbook.auto_execute` 不在其中；Kill Switch 是本项目设计的紧急机制，
  `pipeline.py:3177-3179` 对它 fail-close）
- 判定异常 → **保守视为全是 playbook 仓**，要求显式确认，不可静默双线平仓

### 9.2 R-12 两表 `entry_time` 口径统一 ✅已落地（含数据迁移）

根因定位到 `app/storage/trade_store.py:949` `upsert_position_simple` 用裸 `datetime.now()`
（宿主 CST），而同一笔仓写进 `trade_records` 的是 ET（playbook_service 用 `now_et()`）。

- **写入侧**：改用 `now_et()`
- **存量迁移**：`scripts/migrate_open_positions_entry_time_r12.py`。
  设计要点 —— **优先取 `trade_records` 的 ET 值作真源直接写入，而非盲目做时区推算**；
  无真源时才在「按 CST 解释后落在 ET 可交易时段（04:00-20:00）」的前提下换算，否则跳过并报告。
  已是 ET 的行自动跳过 ⇒ **幂等**，默认预演、`--apply` 才写。
- 执行结果：2 行迁移完成并复核一致，复跑显示「待迁移 0 行」证实幂等。

```
US.WEAV  2026-08-19T21:36:54 → 2026-08-19T09:36:54.660513-04:00  (取 trade_records 真源)
US.BABA  2026-08-21T21:47:57 → 2026-08-21T09:47:57.399253-04:00  (取 trade_records 真源)
```

### 9.3 R-13 timezone fallback + naive 比较加固 ✅已落地

**(a) fallback 链改 `pytz → zoneinfo → 固定偏移`**（`app/utils/timezone.py`）
原实现两个真缺陷：① `ET_OFFSET` 在 **import 时**由 `time.localtime().tm_isdst` 决定，
而宿主 Asia/Shanghai 无 DST 恒为 0 → **永久固定 EST(-5)**，夏令时全部慢 1 小时；
② fallback 版 `to_et()` 直接 `return dt` **不做任何转换**（实测返回 CST 墙钟且丢 tzinfo）。
`zoneinfo` 是 Python 3.9+ 标准库、无外部依赖且正确处理 DST，故插在固定偏移之前。
新增 `_localize()` 助手统一 pytz `.localize()` 与 zoneinfo `.replace(tzinfo=)` 的语义差异，
全文 `CST.localize(` / `ET.localize(` 调用已归零。顺带删掉 `now_et()`/`now_cst()`
两个分支代码完全相同的无意义重复。

**(b) 反向冷却门（防 whipsaw）的 fail-open**（`app/pipeline/order_executor.py:2947`）
原实现把 `return False`（拦截）写在 try 内、**gap 计算之后**。而 `trade_records.exit_time`
格式混用（ET-aware 347 行 / naive 327 行，naive 全为 ≤2026-07-20 的 legacy 行），
命中 naive 行时 `now_et() - exit_dt` = aware 减 naive → `TypeError` → 被 `except` 吞成
debug 日志 → 落到函数末尾 `return True` = **放行入场，冷却门形同虚设**。
**关键认识：是否拦截取决于 SQL 是否查到行（SQL 已按 `cutoff_iso` 过滤），gap 仅用于日志。**
故把拦截决策移出 gap 计算的依赖；naive legacy 行按 ET 补全时区（与该列语义一致）。

### 9.4 R-10 / R-11 cron 修复 ✅已由用户手工执行并核验（2026-08-23）

两者需修改**工作区外**文件（`/root/.hermes/cron/jobs.json`、系统 crontab），
被 CLI 权限策略硬拦（策略不接受 AskUserQuestion 回答作为工作区外写入的凭据）。
故把已验证逻辑固化为仓内脚本（幂等、默认预演、自动备份），由用户执行 —— **已完成**：

**R-10** `scripts/fix_hermes_stoploss_cron_r10.py`
问题：`DEB-20260815 硬止损监控` expr = `*/30 21-23,0-4 * * 2-6`。两个 CST 小时段
对应**不同的 ET 交易日**，不可能共用一个 DoW：

| CST 段 | 对应 ET | 正确 DoW |
|---|---|---|
| 21-23 | 同日 09:00-11:59 | **1-5** |
| 0-4 | 前日 12:00-16:59 | **2-6** |

两段合挂 `2-6` ⇒ **ET 周一 09:00-11:59 完全无监控**（每周开盘首 3 小时裸奔，
而该脚本负责触线时把 `playbook.auto_execute` 置 false）；ET 周六上午空跑。
修法照抄仓内**已有正确范式** —— `ftseek-intraday-diagnose`（`*/30 21-23 * * 1-5`）与
`ftseek-intraday-diagnose-late`（`*/30 0-4 * * 2-6`）就是拆成两个 job 各带正确 DoW。
本脚本同样拆「上午段/下午段」。幂等、默认预演、自动备份。

**R-11** `scripts/fix_crontab_dst_r11.py`
crontab 跑本地 CST（**无 DST**），ET **有 DST** ⇒ 注释写 ET 的条目 **2026-11-01 起偏 1 小时**。
落在 CST 04:xx 的**盘后**定点任务在冬令时会跑进**盘中**：

| 脚本 | CST | EDT→ET | EST→ET | 改为 |
|---|---|---|---|---|
| `live_reconcile_check.py` | 04:05 | 16:05 ✅ | **15:05 ❌盘中** | 05:10 |
| `position_path_collector.py` | 04:35 | 16:35 ✅ | **15:35 ❌盘中** | 05:15 |
| `e2_momentum_shadow_daily.py` | 04:45 | 16:45 ✅ | **15:45 ❌盘中** | 05:20 |

移到 CST 05:1x 后 **EDT→ET 17:1x / EST→ET 16:1x，两季均盘后**，
且**一次性修完不需换季维护**；原相对执行顺序完整保留（→ backfill 05:30）。
脚本同时修正上一行注释里写死的 `CST 04:45 = ET 16:45` 之类断言。

**刻意不动**（防后人误改）：`*/30 0-4`、`*/5 0-4`、`*/15 0-5` 等**区间型盘中监控**——
CST 0-4 = ET 12:00-16:00 正是盘中，本来就该在盘中跑；`0 4 * * *` disk_cleanup 与市场无关；
已在 CST 05:xx 的条目两季均安全。

**执行方式**：
```bash
cd /opt/ftseek/FTSeek-US
./venv/bin/python scripts/fix_hermes_stoploss_cron_r10.py --apply
XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart hermes-gateway   # R-10 需重载调度
./venv/bin/python scripts/fix_crontab_dst_r11.py --apply
```

**落地核验（2026-08-23，只读复核）**：

R-10 —— hermes 已拆为两个 job，与仓内正确范式一致：
```
id=0104bac6e9fd  */30 21-23 * * 1-5   硬止损监控·上午段   enabled=True next=08-24 21:00
id=5d1b25f714ba  */30 0-4  * * 2-6    硬止损监控·下午段   enabled=True next=08-25 00:00
对照 ftseek-intraday-diagnose      */30 21-23 * * 1-5
     ftseek-intraday-diagnose-late */30 0-4  * * 2-6
```

R-11 —— crontab 三条已在 CST 05:1x（原相对顺序保留，均早于 05:30 backfill）：
```
10 5 * * 2-6  live_reconcile_check.py
15 5 * * 2-6  position_path_collector.py
20 5 * * 2-6  e2_momentum_shadow_daily.py
残留 CST 04:xx 定点任务仅 `0 4 * * * disk_cleanup.sh`（与市场无关，刻意不动）
```

覆盖度实测（08-24 周一 → 08-28 周五，按 30min 槽枚举）：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| ET 周一 09:30-11:59 硬止损监控 | **无覆盖** | ✅ 覆盖 |
| ET 周一至周五 09:30-16:00 | 缺周一上午 | ✅ **73 槽全覆盖，零缺口** |
| ET 周末空跑 | 每周六 09:00-11:59 **6 槽** | ✅ **0 槽** |
| 其余非盘中槽 | — | 5 槽 ET 盘前 09:xx（盘前预警）+ 12 槽 ET 盘后 16:xx（收盘核对），均有用途 |

### 9.5 验证
- 新增 `tests/test_r9_r13_contracts.py` **15 例 + 1 skip**：
  R-9 感知/不静默/Kill Switch/fail-safe；R-12 写入侧 ET + 两表一致性 + 迁移脚本幂等；
  R-13 `to_et/to_cst` 真转换、`tm_isdst` 快照消除（只扫非注释行）、
  **跨季 DST 断言**（同墙钟夏 −4h / 冬 −5h）、冷却门拦截不依赖 gap。
- 全量 `pytest -q`：**1532 passed / 1 skipped**，零失败。
- 重启 + `startup_smoke_check.py --wait 90`：七条断言全通过（09:59 CST）。
- 备份：`data/backups_r9_r13/`（crontab / hermes jobs / 主库 各一份，操作前）。




### 回滚
```yaml
risk.playbook_vix_gate_enabled: false            # R-1 退回无 VIX 门
risk.capital_safety.playbook_hard_gate: false    # R-5 退回无水位硬门
reconcile.orphan_autoclose_enabled: true         # R-2 退回自动伪造平仓
risk.playbook_guard_fail_mode: open              # R-7a/b 退回护栏 fail-open
strategy.main_scan_enabled: true                 # R-7d 显式开启主线扫描 (语义不变)
```
R-1 互斥层 / R-3 固化 / R-4 失效检测 / R-7c 源头过滤为代码级，回滚 = `git revert` 本次提交。

### 部署状态
**已完成**（2026-08-23 08:25 CST，ET 周六盘后安全窗口）：
- `systemctl restart ftseek-us` ×2（R-1~R-6 于 07:57，R-7 于 08:25）
- `scripts/startup_smoke_check.py --wait 90` **七条机器断言全通过，exit=0**，零 Traceback/CRITICAL
- 活体确认 `WEAV`/`BABA` 两笔 playbook 仓被主线两个入口正确排除，无 `ENV_ROUTING_MISMATCH`

---

## 八、建议与方案（待用户裁决）

### 8.1 立即（安全，与策略决策解耦）
1. ~~重启 + 启动冒烟，激活 R-1~R-6~~ → **✅ 已完成**（见「部署状态」）
2. ~~补 R-7~~ → **✅ 已完成，见 §七之二**。审计后范围修正：不是关 ATR 规则
   （`trailing_stop` 服务遗留/手动仓，不该关），而是修**三处 fail-open + 一处结构性绕过**。
3. **补 R-8（待批准）**：修 `_emit_trigger_miss` 裸 `datetime.now()`（`pipeline.py:4270`，宿主 TZ=Asia/Shanghai）
   → 15,150 行遥测约 60% 记到 +1 天，文件内出现 `2026-08-15 Sat` 非交易日即铁证。
4. **补 R-9（待批准）**：Dashboard `/api/risk/emergency-close`（`risk.py:306`）与
   `/api/orders/close`（`trade.py:83`）加 playbook 仓感知/二次确认 —— §7.6 剩余缺口#1，
   当前一按即穿透全部护栏。

### 8.2 修尺子（做任何策略结论之前）
4. 全部 IC / 记分牌统计改按日聚类；撤下 `signal.ic_t1=+0.1265` 头条。
5. **停用** confidence-based playbook IC 作为毕业门禁 —— 它在测量常量。
6. 宪章增第八律：判据须附 MDE；无 MDE 结论不予采信。

### 8.3 策略（重点）
先接受约束：客单 ~$4.5K + $1.99/腿佣金地板 ⇒ 往返成本 ≈10bps
（实测分位：<2K=31.7bps / 2-4K=14.4 / 4-6K=8.7 / 6-10K=6.7 / >10K=1.8）。
**需要的不是更好的退出，是 >10bps/笔的事前 edge**；实测 0.1bps ± 3.4。

| 杠杆 | 判断 |
|---|---|
| **A. 降频** | **唯一确定为正**。毛期望为零时每笔是 −$4.54 确定性捐赠、机会成本为零。321→40 笔省 $1,275，且买到修 R-7/R-8 与攒同参数样本的时间 |
| **B. 冻结退出参数 ≥283 笔** | **§三 的直接推论**。当前 7.1 天/次换参 = 在 ±$21 噪声上追 $5-10 效应。不冻结则 9 月重复七周的循环 |
| **C. 几何二选一，禁骑墙** | 现 0.4/0.35/0.8 保证 R:R≈0.2 + WR≈55% = 精确 E=0。(i) 趋势跟随：武装门 0.4%→≥1.5%、放宽回撤锁、接受 WR 降至 35-40% 换 R:R>1.5；(ii) 均值回归：接受 R:R 0.3 但需 WR>77% **且**成本<3bps（名义>$15K）。**推荐 (i)**，因其与"寻找漂移"相容。⚠️ 但须清楚：**毛期望为零时两者期望值都还是零**，改几何只是让真 edge 出现时能兑现 |
| **D. 入场侧找真漂移** | 唯一能产生利润的路径。要求预注册假设 + 按日聚类推断 + 事前 MDE + 样本外验证。诚实先验：已在 n=440 候选 / 1,813 信号 / 4,057 快照上找过，全为零 |

**不推荐**"提高名义额摊薄费用"作为主要手段 —— 那是在零 edge 过程上加杠杆
（虽然 `admission.min_notional_usd` 由 3000 提到 ~8000 在"若仍交易"前提下是对的，8.7bps→~5bps）。

### 8.4 北极星建议改写
现"每笔期望 ≥ $0"在 8.7 个月内不可测量（需 n=1,386），这是它三个月未收敛的真正原因。
建议改为：**"事前特征对 T+1 alpha 的按日聚类 IC，在预注册样本外窗口 t>2"** —— 可在数周内判定。

---

## 九、作者勘误与对子代理论断的修正（立此存照）

| # | 子代理/文档论断 | 主控亲验结论 |
|---|---|---|
| 1 | "AbsoluteCircuitBreaker 不在 LIVE 路径" | **证伪**。`playbook_service.py:749-764` 已接入且 fail-close。真实缺口是 VIX / per-symbol 互斥 / `can_open_position`，R-1/R-5 据修正后范围实施 |
| 2 | `ARCHITECTURE.md §2` "→ order_executor → ~25 道内联风控门" | **确认为错**。`dual_broker` 全文零 `order_executor` 引用。文档需同步修正 |
| 3 | handoff "admission record-only 全阈值 0" | **证伪**。实为 `enforce: true / min_notional_usd: 3000 / min_entry_price: 5.0` |
| 4 | handoff "`breakeven_at_pct`=0.5、`rr_ratio`=999" | **证伪**。实为 0.4 与 2.0，固定 TP 通电 |
| 5 | 作者自己中途的"SHORT 是唯一正向切片" | **自我证伪**，见 §4.4。6 月 outlier artifact |

---

## 待办

- [x] **用户裁决（2026-08-23）**：不 STOP，前期都是学费；延续打磨模拟交易链路；**打标（毕业）前严格不切实盘**；更新为贴合实际的可测量目标。落地：`docs/OKR-2026-09.md`（修尺子 + 参数冻结 + 预注册单假设 + 实盘四锁硬门）、OKR-2026-08 提前结算 SUPERSEDED、宪章第 9 律立、handoff 北极星/诊断/每周节奏换轨。**§8.3 杠杆处理**：A（降频）否决——模拟照常跑以攒单版本样本；B（冻结）采纳为全月冻结（融合冲突：不降频、冻结参数）；C（几何）无限期搁置（毛期望为零时改几何期望值仍为零，且其推荐变体是未验证假设）；D（入场找真漂移）采纳为预注册研究程序（10 月前零实盘依赖，快照+回放上检验一个假设）。8/31 终裁作废，以预注册毕业判据 + 用户签字取代。
- [x] R-1~R-6 已落地并重启激活，启动冒烟七条断言全通过
- [x] R-7 Playbook 排他性四项 fail-close 加固已落地并重启验证（§七之二）
- [x] R-8 时区专项已落地并重启验证（§七之三）：3 处遥测/复盘日期错位 + 2 处半日市盲区 + 1 处 DST 换算
- [x] R-9 Dashboard 紧急全平 playbook 感知（禁止静默部分平仓）已落地（§9.1）
- [x] R-12 两表 `entry_time` 口径统一 + 存量 2 行迁移已完成、幂等已验证（§9.2）
- [x] R-13 timezone fallback 改 zoneinfo + 反向冷却门 fail-open 修复已落地（§9.3）
- [x] **R-10 / R-11 已由用户手工执行并核验（2026-08-23）**：hermes 硬止损监控拆两 job
      → **ET 周一 09:30-11:59 覆盖已补齐（原每周固定失守 3 小时）**，ET 周末空跑 6 槽→0；
      crontab 三条盘后任务移至 CST 05:1x → EDT/EST 两季均落盘后，不需换季维护。
      实测 ET 周一至周五 09:30-16:00 共 73 槽全覆盖零缺口。（详见 §9.4 落地核验）
- [ ] **辩论方**：独立重跑 §二/§三 全部数字 + 至少一个攻击点。建议攻击靶点：
      ① §三 的"预期极差"计算是否正确刻画了 7 桶多重比较；
      ② §五 pad≈0.30-0.35% 的 fee_cushion 实测缺失率（fee-aware 失败时回落 0.4 → pad 0.6% > 武装门 0.4%，
         是否存在"武装即触发"实例）；
      ③ §4.1 daily-mean IC 用 `len(vv)>=8` 门槛是否引入选择偏差；
      ④ §7.2「排他性靠 store 断粮而非逐路径拦截」这一架构判断是否穷尽（我未做故障注入验证）
- [ ] `docs/ARCHITECTURE.md` 修正两处：§2 执行链路（勘误 #2，dual_broker 不经 order_executor）；
      §3 补充排他性真实机制 = **store 边界隔离**（恢复/对账/补录三个看门人入口）而非逐平仓点拦截
- [ ] `handoff.md` §核心诊断与§退出旋钮地图修正（勘误 #3/#4）
