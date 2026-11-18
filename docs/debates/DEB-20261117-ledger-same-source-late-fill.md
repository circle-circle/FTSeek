# 议题：账本同源补口 + 迟成交证据回灌（P1 测量完整性，零交易行为变更）

## 状态

- [ ] 进行中
- [ ] 待决策
- [x] 已决策（P1 两变量 + 离线工具洞③ 已落地并**重启生效**；2026-11-18 追加
      `audit_path` fail-close 守卫 —— 见「事故记录」节，该守卫在生产是空操作、不需紧急重启）
- [ ] 已验证（待重启后首个交易日核账本收口；存量分叉已清零；**2026-11-18 09:30 CST 进度**：
      重启后 reconcile 零触发、三态待处理合计 0 行 ⇒ 零触发是正确行为，首次生产验证须等
      11-18 ET session）
- [ ] 已关闭

## 背景

2026-11-17 夜盘 DA 链路产出 14 条指令、6 笔成交。逐笔复核账本时发现两类**测量面**缺陷，
都不改变交易行为，但会让下游读数系统性说谎：

### 缺陷 1：`open_positions` 与指令真值分叉，且分叉**永久卡死**

RFC-195 已立 `_align_open_position_stops`（`app/core/position_sync.py`）把恢复路径抢先写入的
合成 SL 对齐到指令真值。11-17 夜盘日志 + 生产库复算实证它**只对齐了 SL 一条腿**：

```
22:51:40,160 [US Sync·P3] US.BWXT 已补录未平 PLAYBOOK_SIM trade_record (id=1911, sl=$127.03)
22:51:40,167 [US Sync] US.BWXT open_positions 止损对齐指令真值 sl=127.03 (行数=1)
22:51:40,228 [US Sync·P3] US.ELV  已补录未平 PLAYBOOK_SIM trade_record (id=1912, sl=$420.28)
22:51:40,229 [US Sync] US.ELV  open_positions 止损对齐指令真值 sl=420.28 (行数=1)
22:51:40,329 [US Sync·P3] US.CAT  已补录未平 PLAYBOOK_SIM trade_record (id=1913, sl=$609.86)
             ← CAT 无对齐行 (BWXT/ELV 都在补录后 1~7ms 打出, CAT 没有)
```

合成值的写方是发现路径 `position_sync.py:2077-2112`（`initial_tp = cost×(1∓5%)` 固定百分比，
与指令 TP 完全无关；SL = cost±ATR×2，地板写死 15%），日志逐值可对：

```
22:50:38 [US Sync] US.BWXT SL=$133.25 (10.9%) TP=$114.18 (5.0%) ATR=$6.53×2  hard_stop=$138.22
22:50:38 [US Sync] US.ELV  SL=$412.52 (6.1%)  TP=$461.39 (5.0%) ATR=$13.45×2 hard_stop=$373.51
22:51:09 [US Sync] US.CAT  SL=$642.62 (11.2%) TP=$549.20 (5.0%) ATR=$32.26×2 hard_stop=$664.81
           ↑ cost=578.10: 578.10+32.26×2=642.62 ✓  ×0.95=549.195 ✓  ×1.15=664.815 ✓
```

| symbol | 指令真值 SL/TP | open_positions 合成 TP | 对齐后 | 残留分叉 |
|---|---|---|---|---|
| US.BWXT | 127.03 / **110.48** | **114.18**（日志实证） | SL 已对齐 | TP 差 +3.35% |
| US.ELV | 420.28 / **475.73** | 461.39（发现路径）→ 现存行实测 **493.22** | SL 已对齐 | TP 差 +3.68% |
| US.CAT | **609.86 / 530.38** | 642.62 / 549.195 | **未对齐**（无日志行） | SL 与 TP 全残留 |

出场链只读 `trade_records.metadata_json`（`exit_monitor_flow.py:88` / `exit_engine.py:329`），
playbook 仓又被主线护栏按 `is_playbook_open_position` 跳过 ⇒ **无交易影响**。
但 `open_positions.initial_sl/current_sl` 是重启恢复面（`position_monitor.py:375-413`、
`scheduler_core.py:1847-1884`）与推送/看板/旧风控告警的取值面 ⇒ 同一笔仓两表两个止损止盈。

**真实数据 A/B 复算**（生产库复制到 /tmp，不碰原库；ELV 是唯一仍存活的行）：

| | 旧代码 | 新代码 |
|---|---|---|
| `_align_open_position_stops(ELV, sl=420.28, tp=475.73)` | **rowcount=0** | **rowcount=1** |
| ELV `initial_tp` | 493.22（永久卡死） | **475.73** ✓ |
| ELV `initial_sl` / `strategy_id` | 未动 | 未动（SL 已同源 ⇒ 不改写；标签不回写） |

旧代码 rowcount=0 的原因就是 WHERE 只判 SL 漂移：ELV 的 SL 早在 22:51:40 已对齐 ⇒
TP 那条腿永远进不了 UPDATE。这是**可复现的实证**，不是推导。

**一处已排除的假阳性（诚实登记）**：现存 5 笔未平 PLAYBOOK 仓里，`US.OXY` 的
`strategy_id='extended_hours_entry'` 看似"误标"，实为**正当标签**——它走盘前/盘后入场
（`entry_price_source='ext_broker_deal_confirm'`）、`playbook_instruction_id=None`
（无指令真值可对）、且两表 SL 均为 55.77 **零分叉**。故 OXY 不构成归属洞的实例，
且 reconcile 分支不会为它调用对齐（无 pending 指令行）。

### 缺陷 2：迟成交（SUBMITTED→confirmed）在两份影子账本里**收不了口**

`auto_execute_flow` 只在本轮下单调用返回的那一刻收口账本，而 SUBMITTED 之后的成交证据是稍后由
`PositionSync._reconcile_playbook_execution_states` 从券商订单回执补录的。11-17 当日：

- `exec_gate_audit.jsonl`：6 笔成交里只有 BA(5658)/COIN(5649) 拿到 `action='executed'`；
  ELV(5647)/CAT(5650)/UBER(5652)/BWXT(5654) 终局停在 `submitted_unconfirmed`，
  四笔事后在 DB 里全部 `execution_state='BOOKED'` 且有 `execution_price`。
  `scripts/gate_ledger.py:106` 按 `if "executed" in actions` 判每指令终局 ⇒ 执行率读成 2/6。
- `execution_tca.jsonl`：同 4 笔的 `fill_price=null` / `planned_to_fill_bps=null`。
  `scripts/execution_tca_report.py:211` 只取 `success is True and fill_price>0`
  ⇒ 计划→成交滑点样本 6 笔只剩 2 笔，**缺口 67%**。

这直接打在 OKR-2026-11 主 KR（DA-only 净 bps/笔）的取数面上：滑点样本缺 2/3 时，
任何"DA 计划 vs 实际成交"的结论都建立在选择性样本上。

## 目标

在**不改变任何交易行为**的前提下，把两处账本收口补全，并用契约测试把口径钉死。

## 立案前强制检查（P0.5）

- [x] **同轴既有影子枚举**（grep `app/playbook/shadow_registry.py` REGISTRY，非凭记忆）：
  - `execution_tca_spread_shadow`（OBSERVING，`data/shadow/execution_tca.jsonl`，
    `count_mode="where:success=True"`）— 本 DEB **不新立案，只补第二写方**：
    增量 = 迟成交行的 `fill_price/planned_to_fill_bps/exec_to_fill_bps`，
    点差/报价年龄分桶不受影响（回灌行这四个字段一律 None）。
  - `exec_gate_audit`（OBSERVING，永久测量项，无毕业门）— 同样**只补第二写方**：
    增量 = 迟成交指令补一行 `action='executed'`，修正每指令终局判定。
  - 两项 note 已同步登记第二写方与分来源读数要求（注册表是唯一 SSOT）。
  - 同轴**未**新增注册项：本批不引入新产物、新判据、新攒样人群。
- [x] 既有裁决/判据撞车普查：
  - **DEB-20261027 A**：open_positions 的 SL/TP/entry_time 必须与出场链同源 ⇒ 本批是其延伸，不冲突。
  - **RFC-195**：CAS 语义（不得松掉已收紧的 `current_sl`）**原样保留**，本批只扩 TP 与归属判据。
  - **ADR-015**（`owner_strategy → source_type` 派生表）：故**刻意不回写** `strategy_id/owner_strategy` —
    改标签会影响 `order_executor` 放行判定，属行为变更，不夹带进测量批次（已加钉子测试锁住）。
  - **ADR-035**（限价路由，已 KILLED，"不重开"，DEB-20261106:1513/1535/1702）：本批不触碰入场路由。
  - **R-12 / DEB-20260823**（ET 口径）与 **DEB-20260905 §十三**（账本时间列 3 尺度混装）：
    回灌行的 `ts/date` 只接受**自描述时区**的时间串；naive 券商串既非 UTC 亦非 CST（是 ET），
    一律回退 `now_et()`，宁可用 reconcile 时刻也不写错尺度（钉子测试 4 例）。
  - C 类红线：不新增任何 confidence/quality 闸门（C12），不改入场形态/追价上限（C16），
    不动 SL/TP 生成逻辑（C17/C18）— 本批纯账本收口。

## 数据与实证

2026-11-17 当日真实数据（离线干跑，写 tmp 账本，未触碰生产 jsonl）：

| symbol | 方向 | DA 计划锚 | DB entry | 实际成交 | planned→fill (bps) | exec→fill (bps) |
|---|---|---|---|---|---|---|
| US.BWXT | SHORT | 120.60 | 120.41 | 120.19 | **+34.00** | +18.27 |
| US.CAT | SHORT | 588.50 | 578.07 | 578.10 | **+176.72** | −0.52 |
| US.ELV | LONG | 433.00 | 440.08 | 439.42 | **+148.27** | −15.00 |
| US.UBER | LONG | 68.60 | 68.64 | 68.62 | **+2.92** | −2.91 |

符号契约：方向归一后**正数 = 对交易更不利**（SHORT 成交更低 / LONG 成交更高）。
四笔全部为正 ⇒ 与先前手工复算"6/6 成交劣于 DA 计划"一致。

审计侧回灌行的 `entry/size/notional` 与原 `submitted_unconfirmed` 行**逐值相同**
（ELV: 440.08 / 9 / 3960.72），真实成交只进 `extra` ⇒ 以这三列为分母的既有统计不被扰动。

单位契约（同名不同尺，已在模块头与测试双向钉死）：
- `exec_gate_audit.atr_pct` = **分数**（0.030138），沿用 `auto_execute_flow:2182` 直传 DB 原值惯例；
- `execution_tca.atr_pct` = **百分数**（3.0138），沿用 `auto_execute_flow:1837` 的 ×100 契约。

## 决策

- 选择方案：**两个独立变量，分别可回滚**（一次一变量）
  - **变量 1 · 账本同源**：扩 `_align_open_position_stops` —
    ① TP 真值 >0 且偏差 >0.005 即覆盖（TP 无 trailing 语义，不存在"松掉活止盈"）；
    ② WHERE 改为 `OR(SL 漂移, TP 漂移)`，否则 ① 会被旧 WHERE 吃掉；
    ③ 新增 `playbook_owned` 关键字，**仅**由"刚成功补录一条未平 PLAYBOOK trade_record"的
    调用方显式传 True（归属 SSOT 是 trade_records，不是 open_positions 自报的标签）。
    **证据强度如实标注**：洞①③（TP 覆盖 + OR 漂移 WHERE）有可复现的 A/B 实证（见上表，
    旧 rowcount=0 / 新 rowcount=1）；洞② 的 CAT 实例是**会话内直接观测**到
    `strategy_id=auto_main`，该行已于 23:24:53 平仓清理（`Ghost position: US.CAT
    (DB only, closing)`），现库无法复核；旁证有二 —— (a) 日志证明 CAT 补录成功且
    SL 存在 20.2~32.8 的漂移，却没有对齐行（BWXT/ELV 同周期都在 1~7ms 内打出），
    (b) 同一代码区 `:2342-2353` 的既有注释已把"旧逻辑标 auto_main → 主 ExitMonitor
    接管 → 双重管理"登记为**历史实锤失效模式**。故洞② 按"已知失效模式 + 单例观测"
    处理，不宣称已复现；`playbook_owned` 默认 False，不传即完全等同旧行为。
    ④ 对齐成功后显式 `conn.commit()`，不再依赖下游 `mark_execution_booked` 顺带提交。
    `current_sl` 的 CAS 保护原样保留。
  - **变量 2 · 迟成交证据回灌**：新增 `app/playbook/late_fill_evidence.py`，
    在 reconcile 补录成功分支追加两份账本行；**只追加不改写**，按 `inst_id` 去重，
    自带 `late_fill` / `evidence_source` 来源标记；到达报价无从追溯 ⇒ `ctx=None`，
    `bid/ask/mid/spread_bps/quote_age_ms` 一律 None，禁止拿事后报价伪造。
- 决策依据：两处都是**已裁机制的未完成面**（DEB-20261027 A 同源要求 / RFC-195 对齐意图 /
  RFC-169 链路必带），不是新机制；且都只写账本，不进任何闸门判定分支。
- 保留空间：变量 2 的 `evidence_source` 已参数化，日后 P3 恢复路径若接入可区分来源。
- 负责人：Qoder（本会话）
- 截止时间：待用户授权重启后生效

## 刻意不做（本批边界）

1. **P3 恢复路径不加对齐调用**：实测该路径写进 `open_positions` 与 `trade_records` 的是
   **同一份合成值**（ATR×mult + `max_loss_pct` 2% 硬底，或 ±8%/±15% 兜底，
   `position_sync.py:2296-2320` + `Position(initial_sl=sl_price, initial_tp=tp_price)`），
   对齐它是空操作。

   **该路径的真实缺陷与真实量级（2026-11-18 复算，修正本 DEB 初稿的高估）**：
   `_backfill_playbook_trade_record` 把入参 `sl/tp` 原样写进 `_meta_p3`
   （`position_sync.py:877-878`）⇒ P3 恢复出的仓，其**出场链 SSOT**
   （`trade_records.metadata_json.stop_loss`）是合成值而非指令真值，
   即使 remark 能反查出 `playbook_instruction_id`。这是代码事实。
   但按 `trace_id` 家族归因实测全量 899 条 PLAYBOOK 记录：

   | 补录来源 | trace_id 形态 | 笔数 | SL/TP 与指令真值分叉 |
   |---|---|---|---|
   | 下单轮直接落库 | `playbook-*` | 740 | — |
   | reconcile（用**指令真值**） | `playbook-reconcile-{iid}-{attempt}` | 78 | 0 |
   | **P3 恢复（用合成值）** | `sync-{symbol}-{ts}-{uuid}` | **4** | **3** |
   | 无 trace_id | — | 76 | — |

   即：P3 路径**四个月只触发 4 次**（2026-07-20 三笔 TTE/UBER/E，2026-08-20 一笔 BULL），
   其中 3 笔分叉（SL 偏 +1.15%~+2.60%、TP 偏 +10.67%~+16.66%，三笔均已平仓），
   第 4 笔（BULL）根本反查不到指令 id ⇒ 即便加"取真值"也救不了。
   占全部 PLAYBOOK 记录 0.44%，最近一次触发距今约 3 个月。

   **故本 DEB 初稿把它称作"真的交易行为缺陷"属高估，予以更正**：它是一条**近休眠的
   潜在边界**，不是活体风险。相应地，**不立影子攒样**——按 4 次/4 个月的触发率，
   n≥30 永远攒不到；这类小样本人群该走**机制推导**而非抽样：指令真值是
   DEB-20261027 A（同源要求）+ RFC-169（链路必带）已裁定的应然取值，
   用合成止损管理一笔指令已知的仓位，方向上无争议地错。
   但修它仍然改的是活体出场止损 ⇒ **属行为变更，交用户裁**，不夹带进本测量批次。
   建议形态（待裁）：P3 在 `instruction_id` 可反查时改取指令 `stop_loss/take_profit`，
   反查不到才回落合成值；并补一条 `metadata.stop_loss_source ∈ {instruction, synthetic}`
   落库标记（新字段 ⇒ 按军规须带基数自检 + 单位声明 + 契约测试）。
2. **不回写 `strategy_id`/`owner_strategy`**（见 ADR-015 撞车普查）。
3. **不改 `open_positions.entry_time` 尺度**：CAT 那类 naive-CST 值仍在，
   属 DEB-20260905 §十三 已登记的存量病灶，需要独立的尺度契约测试，不夹带。

## 执行项

- [x] `app/core/position_sync.py`：`_align_open_position_stops` 扩 TP 覆盖 + OR 漂移 WHERE
      + `playbook_owned` + 显式 commit；reconcile 调用点传 `playbook_owned=True`。
- [x] `app/playbook/late_fill_evidence.py`（新增）：双账本回灌 + 去重 + 尺度守卫。
- [x] `app/playbook/execution_tca_shadow.py`：`finalize_and_record` / `_append_row` 增
      `path` 注入缝（默认值不变，生产调用点不传）。
- [x] `app/core/position_sync.py` reconcile 补录成功分支接 `record_late_fill`。
- [x] `app/playbook/shadow_registry.py`：两条既有影子的 note 登记第二写方与分来源读数要求。
- [x] `scripts/align_playbook_recovery_display.py`：**同一个洞③ 的第三处实例**（离线修复工具）。
      原 `find_rows` 要求 `abs(old_sl - truth_sl) > SL_EPSILON` **且** TP 也分叉才算候选 ⇒
      RFC-195 运行时已修好 SL、TP 仍是合成值的行永远进不了候选。实测重启后跑该工具报
      `无需对齐 (0 行)`，而生产库实际存留 1 行分叉（US.ELV TP 493.22 vs 真值 475.73）。
      改为 `OR(SL 分叉, TP 分叉)`，并让**无分叉的那一腿原样保留**（不制造"改了但其实没改"
      的审计噪声）、`reason` 细分为 `recovery_tp_pollution` / `recovery_sl_pollution` /
      `recovery_sl_tp_pollution`。修后报告：`候选 1 行: US.ELV SL 420.28→420.28
      TP 493.22→475.73 reason=recovery_tp_pollution`。`apply_rows` 的五值全 CAS 与
      审计先落盘（`data/audits/playbook_recovery_display_<ts>.jsonl`）原样未动。
      scripts/ 改动 ⇒ **不需重启**。
- [x] `tests/test_rfc195_open_position_stop_align.py`：+10 例（TP 覆盖 / SL 已对齐仍修 TP /
      CAS 保留 / 真值缺失不抹零 / 0.005 绝对价容差 / 归属证明后覆盖误标行 /
      默认仍拒误标行 / 不改标签 / 调用点钉子）。
- [x] `tests/test_late_fill_evidence.py`（新增，25 例）：计划值口径 / 去重 / sqlite3.Row 归一 /
      bps 符号双向 / atr_pct 双尺度 / 影子开关 / 垃圾输入零写盘 / TEST 标的排除 /
      写方炸了不上抛且不连带 / naive 券商串拒收 / 正负偏移与 Z 三种 aware 形态 / 调用点钉子。
- [x] **重启生效**（用户 2026-11-18 授权）：`systemctl restart ftseek-us` 06:37 CST，
      启动冒烟 **7/7 全绿**（ConfigValidator/漂移检测/热重载/SSOT 审计 0 BLOCKING/双臂安全/
      退出快循环/零 Traceback）；重启后日志 ERROR|CRITICAL|Traceback 计数 **0**；
      5 笔未平 PLAYBOOK 仓全部正确恢复且被主线正确跳过（`[持仓恢复] 完成：0/5 条记录恢复`
      + 逐笔 `跳过 US.xxx (未平 PLAYBOOK_SIM 仓, 由 monitor_playbook_sim_exits 自管)`）。
      重启前两核已做：①未平仓 5 笔（LMT/OXY/GOOG/META/ELV，全 PLAYBOOK_SIM，无在途出场标记）；
      ②`git status --porcelain -- app/ scripts/` 逐条核归属 — 他人未跟踪 WIP
      `scripts/verdict_outcomes_ledger.py` 虽已在 crontab（`10 6 * * 2-6`，别人接的线），
      但对 `app/`·`config/`·`main.py` **零 import** ⇒ 重启不会把它带上线；
      本批 P0 的 4 个 `da_*.py` 同为离线工具（唯一引用是注册表 `eval_script` 文档指针）。
- [x] **存量分叉清零**：`scripts/align_playbook_recovery_display.py --apply`（用户授权）写入 1 行，
      US.ELV `initial_tp` 493.22→**475.73**、`take_profit_json` `[]`→`[{"ratio":1.0,"price":475.73}]`，
      SL 420.28 与 `strategy_id` 未动；审计留痕 `data/audits/playbook_recovery_display_20261117T225411Z.jsonl`
      （含全部旧值，可回退）。复跑工具报 `无需对齐 (0 行)`；5 笔未平仓两表 SL/TP **逐笔一致**。

## 验证项

- [x] 新增 + 相关既有测试全绿：`test_rfc195_open_position_stop_align` 14 例、
      `test_late_fill_evidence` 25 例（含参数化）、`test_execution_tca_shadow`、
      `test_open_position_sl_tp_contract`、`test_position_sync_queue_shadow` — 66 passed；
      关键词面 `-k "position_sync or tca or rfc195 or late_fill or exec_gate or sl_tp"` 223 passed；
      注册表面 `-k "shadow_registry or registry"` 85 passed。
      **口径更正（2026-11-18）**：上面那个 **223 请读作「约 223，差 1 未归因」** —— 追加
      fail-close 守卫的 5 例后同命令实测 **229**，剔除这 5 例得 **224**（≠223）。已排除他人 WIP
      测试文件、`test_okr_2026_11_scoring`、四个 `test_da_*`、按 `docs/debates` 取数的两文件
      （均 0 命中），40 个匹配文件里也无数据驱动的动态参数化 ⇒ 差 1 出在当初那个聚合数本身。
      0 失败、无测试丢失。要引用请直接用命令重跑，不要抄这个数。详见 handoff 2026-11-18 09:51 CST 条。
- [x] `scripts/check_undefined_names.py` 三个改动文件 exit=0（pre-commit §5.5 同一门禁）。
- [x] 离线干跑：4 笔真实指令回灌后数值与手工复算一致（见「数据与实证」表）。
      **口径更正（2026-11-18）**：该干跑的**审计侧是 monkeypatch 捕获的调用**，不是真写文件
      （故当时"未触碰生产 jsonl"成立，但成立的原因是打桩、不是 `audit_path`）；TCA 侧才是
      真写盘落 tmp。正因审计侧从未真写过，`audit_path` 是半个缝这件事当时无从暴露 ——
      见「事故记录」节。
- [x] **（2026-11-18 补）调用点对象形状静态证明**：`row` 来自
      `SELECT * FROM playbook_instructions`（实测 74 列），回灌读的 7 个键
      （`atr_pct/direction/entry_price/id/position_size/symbol/vol_bucket`）全部在列 ⇒
      不存在 KeyError 被 `except` 吞成静默失败的路径；`observation['price']/['dealt_qty']`
      由既有生产代码 `position_sync.py:1012` 已在用 ⇒ 形状已被生产证明。
- [x] **（2026-11-18 补）`execution_time` 格式普查**：882 条非空样本 = tz-aware 837（94.9%）
      + naive 45（5.1%）；**naive 全部落在 2026-06-22→07-20 此后绝迹**，11-17 四笔全为
      `-05:00` ⇒ 第 1 轮的负偏移修复是承重的（否则这四笔 100% 退化成 `now_et()`），
      而拒绝 naive 的回落路径对现役数据属休眠。
- [x] **（2026-11-18 补）行为面证明 + 由此暴露的事故与修正**：用真实 `SELECT *` 的
      sqlite3.Row 驱动回灌，TCA 侧 4 行全部正确（`planned_to_fill_bps` = BWXT +33.9967 /
      ELV +148.2679 / CAT +176.7205 / UBER +2.9155，`ts_et` 保住真实成交时刻，
      `atr_pct` 百分数尺度，`quote_source='late_fill_no_arrival_quote'`）；audit 侧暴露
      `audit_path` 陷阱并误写生产 4 行 ⇒ 已按用户授权截除 + 备份 + 留痕，并加 fail-close
      守卫与 5 例钉子（`test_late_fill_evidence` 25 → **30 passed**，其中 2 例经变异验证）。
      详见「事故记录」节。
- [ ] 重启后首个交易日核：`exec_gate_audit` 中 `submitted_unconfirmed` 指令是否都拿到
      配对的 `executed`(late_fill=true) 行；`execution_tca` 中 `success=true` 行数
      是否等于当日真实成交笔数。
      **进度（2026-11-18 09:30 CST）**：重启后 reconcile **零次触发** —— 06:38:00 启动对账
      `Futu: 5 | playbook自管: 5 | 一致性: ✓`，09:03:59 即 `Market closed, sync paused`；
      且当前 `UNKNOWN/SUBMITTED/FILLED_UNBOOKED` 三态**合计 0 行** ⇒ 无可回灌对象，
      零触发是正确行为、不是路径坏了。首次生产验证须等 11-18 ET  session。
- [ ] 核 `open_positions` 与 `trade_records.metadata_json` 的 SL/TP 对每笔未平 PLAYBOOK 仓
      逐笔相等（`tests/test_open_position_sl_tp_contract.py` 的生产库回归例已覆盖 SL，
      TP 侧靠本批新例 + 人工核一次）。
      **进度（2026-11-18）**：ELV 存量分叉已于 06:54 CST 经批准的
      `align_playbook_recovery_display.py --apply` 清零（TP→475.73，留痕
      `data/audits/playbook_recovery_display_20261117T225411Z.jsonl`），5 笔未平仓两表逐笔一致；
      但**前瞻性运行时路径**（reconcile 确认分支里的对齐调用）仍未经生产实跑。
- [ ] 何时关闭：连续 5 个 ET 交易日两处账本零分叉，且 `execution_tca_report` 的 filled
      样本数 = 当日实际成交数。

## 事故记录（2026-11-18）：本批的 `audit_path` 是半个缝，验证脚本因此误写生产账本

**这是本批自己引入的缺陷，由本批的验证动作暴露。**

### 病灶

`_backfill_audit` 接受 `audit_path` 参数，但下游
`app/playbook/pipeline_parts/risk_gates.py:433` 的 `_emit_exec_gate_audit(inst, *, action,
entry, size, notional, extra)` **没有 path 参数** —— `output_path` 由 `module_file` 推导
（`parents[2]/data/shadow/exec_gate_audit.jsonl`），结构上不可能指向别处。于是：

| 面 | `audit_path` 是否生效 |
|---|---|
| 去重读 `_tail_rows(path)` | ✅ 生效 |
| 写入 `_emit_exec_gate_audit(...)` | ❌ **恒落生产** |

⇒ 传 `audit_path=tmp` 而不同时 monkeypatch 模块常量 `_AUDIT_PATH`，得到的是
「去重读空文件（永不去重）+ 写生产」的**静默陷阱**。对比：TCA 侧的
`execution_tca_shadow.finalize_and_record(row, path=...)` 是本批真加的缝，读写同源 ⇒ 没中招
（实测生产 `execution_tca.jsonl` 0 命中、mtime 02:57 CST 未动）。

**原 25 例为什么没抓到**：它们整体 monkeypatch 了 `pipeline._emit_exec_gate_audit`
（`audit_calls` 夹具），所以从未验证 `audit_path` 是否真被 honored —— 桩把缝的存在性一起桩掉了。

### 实际发生的事

2026-11-18 09:30:19 CST 一次行为面验证（用真实 `SELECT *` 的 sqlite3.Row 驱动回灌，
目的是证明调用点对象形状不会 KeyError）向生产 `data/shadow/exec_gate_audit.jsonl` 末尾
追加了 **4 行**：

- 行号 8839–8842 / 共 8842（污染前 8838），连续在文件末尾；
- `action=executed`、`late_fill=true`、`evidence_source='verify_real_row'`（我临时造的标签）、
  `ts=2026-11-17T20:30:19-05:00` = `now_et()` 于发出时刻（09:30:19 CST 11-18），**不是** reconcile 时刻；
- inst_id 5654 BWXT / 5647 ELV / 5650 CAT / 5652 UBER。

**内容逐值核过是对的**：`entry/size/notional/atr_pct` 与该指令原 `submitted_unconfirmed` 行
完全相同（ELV 440.08×9=3960.72 / CAT 578.07×7=4046.49 / UBER 68.64×56=3843.84 /
BWXT 120.41×31=3732.71），也等于指令表 `entry_price`/`position_size`。错的是**出处与时点**。

**对读数的影响**：`scripts/gate_ledger.py:106` 按 `if "executed" in actions` 判终局
⇒ 11-17 执行率会从诚实的 **2/6** 读成 **6/6**。成交本身是真的（四笔全 BOOKED 且有真实
`execution_price`），所以 6/6 在事实上不算假数；但任何按 `evidence_source='sync_reconcile'`
过滤的人会读到 0，从而错误地判定"生产回灌从未触发"——**两个方向都会误导**。
另：这四笔已 BOOKED，而 reconcile 只选 `UNKNOWN/SUBMITTED/FILLED_UNBOOKED`
⇒ 真回灌对它们永远不会再触发，这 4 行会成为唯一记录并占掉去重位。

### 处置（用户 2026-11-18 授权「截掉末 4 行 + 备份 + 写审计留痕」）

按**行 bytes 过滤**后 `os.replace` 原子替换（保留行未经 json 重序列化 ⇒ 字节级原样；坏行不趁机改）：

- 备份：`/tmp/exec_gate_audit.backup-20261118T014102Z.jsonl`（4,781,249 bytes，字节级相同，
  sha256 `b36170f37bf9c902…`）；另有一份更早的 `…T013818Z.jsonl` 同内容；
- 留痕：`data/audits/exec_gate_audit_excision_20261118T014102Z.jsonl`（含被删 4 行完整原值、
  行号、原因、备份路径与 sha256、一行回滚命令）；
- 结果：8842 → **8838 行**、0 坏行、`verify_real_row` 残留 0、末行回到原有的
  `2026-11-17T13:57:32-05:00 US.META skipped_fail`；
- **读数已复原**：11-17 `executed` 只剩 inst 5649/5658，四笔回到 `submitted_unconfirmed`
  ⇒ 执行率重新读成 **2/6**；
- 回滚：`cp /tmp/exec_gate_audit.backup-20261118T014102Z.jsonl data/shadow/exec_gate_audit.jsonl`。

写入方是 per-write `open("a")` + `with` 自动关闭（无长持句柄）⇒ `os.replace` 不会丢并发追加；
且脚本按标签过滤而非按快照截断，即便期间有追加也会保留。当时 09:03:59 CST 已
`Market closed, sync paused`，无并发写入。

### 修正（已实施）

`_backfill_audit` 开头加 **fail-close 守卫**：`Path(path).resolve() != Path(_AUDIT_PATH).resolve()`
即打 WARNING 并 `return False`，把静默陷阱变成**响亮空操作**。

- **生产是空操作**：调用点不传 `audit_path` ⇒ `audit_target = _AUDIT_PATH` ⇒ 守卫直接放行
  ⇒ 生产行为逐字节不变，**不需紧急重启**。~~（随下次重启生效即可）~~
  **【2026-11-18 10:21 更正】不是"随下次重启"，而是已经自动生效**：本仓有 G-02 部署断层守卫
  （`scripts/deploy_freshness_check.py`，crontab 每 10 分钟），`drift = max(app/**/*.py mtime)
  − 进程启动时刻 > 120s` 且在北京 09:00–12:00 安全窗内即自动 `systemctl restart`。
  本守卫代码 09:46:42 落盘 ⇒ **09:49:23 CST 被自动部署**（MainPID 3343704 → 3360745，
  G-02 日志 `🚨 部署断层自愈: 代码比进程新 11332s (最新 app/playbook/late_fill_evidence.py)
  → systemctl restart 成功, 新代码已入进程`）。启动冒烟 **7/7 全绿**、重启后
  ERROR/CRITICAL/Traceback 计数 **0**、启动对账 `Futu: 5 | playbook自管: 5 | 一致性 ✓`、
  5 笔仓正确恢复且被主线跳过、ELV `initial_tp=475.73`（授权修复挺过重启）。
  **G-02 声明的重启代价实测为零**：ELV `trail_state_json` 的 `highest_price=439.42` **恰等于
  cost** ⇒ 清零是空操作；其余 4 笔 `trail_state_json` 为空；5 笔全部 `current_sl == initial_sl`
  （无收紧过的止损可丢）；且当时 `Market closed, sync paused` ⇒ 无在途订单/出场决策被打断
  （正是 G-02 安全窗的设计用途）。
- 离线要跑通写入面，必须 monkeypatch 模块常量 `_AUDIT_PATH`（测试夹具正是如此），使读与写同源。
- **刻意不改 `risk_gates.py`**：给 `_emit_exec_gate_audit` 加 path 缝虽能让离线真写，但那要动
  交易主路径文件；守卫方案零主路径改动即消除陷阱。已加根因钉子
  `test_downstream_emitter_really_has_no_path_parameter`——若哪天 emitter 真支持了 path 注入，
  该例先红，提示把守卫改成透传。
- 新增 5 例（25 → **30 passed**），其中 2 例经**变异验证**：把守卫短路成 `if False:` 后
  恰好这 2 例红（`{'audit': True} != {'audit': False}`），还原后 30 全绿、0 残留标记。

## 回滚

- 变量 1：还原 `_align_open_position_stops` 与其唯一调用点（RFC-195 原形态）+ 重启。
- 变量 2：删掉 `position_sync.py` 里的 `record_late_fill` 调用（或整删
  `late_fill_evidence.py`）+ 重启；已写入的 jsonl 行保留（append-only，不改写历史）。
- 两者互不依赖，可单独回滚。

## 讨论记录

### 第 1 轮

- Qoder：两处都是"已裁机制的未完成面"，不是新机制；只写账本、不进闸门分支，
  故按 RFC-195 / DEB-20261027 A 的既有治理先例走 DEB 落档而非另立 RFC。
- Qoder（自查反驳）：初版方案曾打算在 P3 恢复路径也加对齐调用，读代码后发现是空操作
  （两表同一份合成值），并顺带查出更重的缺陷——合成止损进了出场 SSOT。
  该缺陷属行为变更，已从本批剔除并登记为独立提案，避免"顺手修"把测量批次变成行为批次。
- Qoder（自查反驳）：初版 `_fill_instant` 只认 `+` 偏移与 `Z`，把 ET 冬令时的 `-05:00`
  误判成 naive ⇒ 回灌行时间戳退化成 reconcile 时刻。已由参数化钉子测试（正/负/Z 三形态）锁死。

### 第 2 轮（2026-11-18，提交后验证）

- Qoder：提交归账后回头清"未验证"项。重启（06:37 CST）后 09:03:59 即 `Market closed,
  sync paused` ⇒ reconcile 零次，回灌路径**生产零触发**，所以那一项现在验不了；改为验
  "它会不会静默失败"。
- Qoder：`row` 来自 `SELECT * FROM playbook_instructions`（74 列），逐一核对回灌读的
  7 个键（`atr_pct/direction/entry_price/id/position_size/symbol/vol_bucket`）全部在列 ⇒
  无 KeyError 被 `except` 吞掉的风险；`observation['price']/['dealt_qty']` 由既有生产代码
  `:1012` 已在用 ⇒ 形状已被证明。
- Qoder：**`execution_time` 格式普查出关键事实** —— 882 条非空样本里 837 条（94.9%）tz-aware、
  45 条（5.1%）naive，而 **naive 全部落在 2026-06-22→07-20，此后绝迹**；11-17 那四笔全是
  `-05:00`。⇒ ①第 1 轮那个负偏移修复是**承重的**（没有它这四笔 100% 退化成 `now_et()`）；
  ②`_fill_instant` 拒绝 naive 的回落路径对现役数据属**休眠**（最后一条 naive 距今约 4 个月）。
  另：那 5 条 in-era naive 里的 TTE/E/UBER 07-20 正是 trace_id 家族归因中 P3 分叉的三笔
  —— 时间窗重叠，**非因果声明**。
- Qoder（自查，事故）：为做上述行为面证明，用真实 `SELECT *` 的 Row 驱动了一次回灌并传
  `audit_path=/tmp/...`，结果 **audit 侧返回 True 却产出 0 行**。追查即本文件「事故记录」节的
  陷阱：`audit_path` 只管去重读、写恒落生产 ⇒ 4 行进了生产账本末尾。**TCA 侧未中招**
  （缝是真的）。已按用户授权截除 + 备份 + 留痕，并加 fail-close 守卫与 5 例钉子（含变异验证）。
- Qoder（方法论反省）：这次事故的成因与第 1 轮的 `_fill_instant` 同型 —— **桩把要验的东西
  一起桩掉了**。`audit_calls` 夹具 monkeypatch 整个 emitter，于是"缝是否真生效"这个问题
  在测试里根本无法被问到。教训：给只写生产的下游加"可注入路径"时，必须有一例
  **不打桩、只给 tmp 路径**的测试来证明注入真的改变了落点；否则那个参数只是看起来可注入。
  本批 TCA 侧有这样的例（走真实写盘落 tmp），audit 侧没有 ⇒ 差异正好对应中没中招。
