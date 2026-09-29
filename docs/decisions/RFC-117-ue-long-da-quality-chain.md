# RFC-117 UE LONG 接入 DA 质量链（双向 feed + 直写通道动量闸）

- **日期**：2026-09-29 | **状态**：**已落地**（用户当日裁决"3 不用影子模式，有助于提升交易质量的可以直接上"）
- **用户裁决原文**（2026-09-29）：「1+2 同意 但是2 只用glm-5.3和deepseek-v4.1-flash 两个不同模型对比即可，3 不用影子模式，有助于提升交易质量的可以直接上」
- **类型**：L2（交易行为变更：UE LONG 指令供给面收窄 + DA 深分析池扩面）
- **回滚**：`universe_expansion.long_momentum_gate_enabled=false`（关直写闸）/ `universe_expansion.da_candidate_feed_long=false`（关 feed LONG 腿，恢复 2026-08-03 只取 SHORT 语义）——两开关独立，YAML 热重载或下轮 cron 新进程生效

---

## 1. 病灶（全部实测，非推测）

UE（universe_expansion）产出走**两条通道，质量链不对称**：

| 通道 | 路径 | 过 DA 深分析/择时闸？ |
|---|---|---|
| UE SHORT | da_feed 窗（07:40 ET scan）→ `_load_ue_short_candidates` → DA 深分析池（cap 3） | ✅（2026-08-03 起） |
| UE LONG | premarket 窗（09:10 ET scan）→ `cmd_inject` **直写** playbook_instructions | ❌ **零 gate** |

账本证据（≥9/5，n=12 UE LONG 成交，净 −$245，中位 +$4.86，WR 67%）：
亏损 **100% 来自 3 笔尾部**，全部是 premarket 窗直写、全部 09:35-09:45 开盘窗入场、mfe≈0：

| 笔 | 亏损 | 入场日动量（真 router 复算） | DA 择时闸判定 |
|---|---|---|---|
| RIVN 9/21 | −$319.55 | **空头排列**（MA5 15.14 < MA10 15.22 < MA20 15.56，close 14.79） | 🔴 SKIP（破位硬买拦截） |
| UBER 9/11 | −$99.37 | **空头排列**（69.26 < 70.09 < 72.05，close 68.16） | 🔴 SKIP |
| QCOM 9/8 | −$227.50 | 短期偏多（多头排列但价跌破 MA5） | 放行（**拦不到，诚实边界**） |

盈利腿对照（NOK +132 / FPS +129 / ASML +46 / STX +44 / WDC +42，全 TRAIL_LOCK）：**5/5 放行，误杀 0**。

反事实结论：择时闸拦 2/3 大亏（−$419），零误杀。UE LONG 缺的不是信号质量（中位为正），
是 SHORT 腿已有的那道**免费过滤**。

## 2. 改动（双保险，两条通道各补一处）

### A. cmd_inject 直写通道 → 破位硬买拦截（`scripts/llm_universe_scan.py`）

- 新增 `_long_breakdown_trend(router, symbol, asof_date)`：MA5/10/20 trend 分档
  **逐字复刻** DA `deep_analyzer.py:200-209`（含 `round(...,2)` 口径钉），
  只用严格早于 asof 的已完成日线（盘前可见=昨收），同日去重取最后一条
  （与 `compute_screening_atr` 同口径）。
- `_cmd_inject_impl` 合格集合循环内、`_compute_numbers` 之前：
  `direction=="LONG"` 且 gate 开 → trend=="空头排列" → `continue`（不生成指令）
  + `_log_inject_loop_reject` 落归因行（`long_breakdown(...)` 含 MA 数值）。
- **只对 LONG**：DA signal_gate 对 SHORT 无此硬门（多头排列做空走降档），
  且 UE SHORT 是在产正 alpha 源，不加门。
- **缺数 fail-open**：bars<20 / router 异常 / None → `("no_data", None)` → 放行
  （与 screening UNKNOWN"缺数≠拒绝"同纪律，不因取数故障误杀）。

### B. da_feed 通道 → LONG 候选进 DA 深分析池（skill `pipeline.py`，仓外）

- `_load_ue_short_candidates` 改双向（SHORT+LONG），**函数名不重命名**
  （调用点/测试契约稳定，语义扩展写进 docstring）。
- `borrow_feasible` 源头过滤按方向分支（LONG 无需借券）；streak 硬闸两向共用。
- `ue_candidate_context` 文案方向感知（修前对 LONG 候选谎称"SHORT候选"=输入层失实）。
- `_select_deep_candidates` 的 `direction_hint` 从硬编码 `"SHORT"` 改 `""`
  （每候选自带 `ue_direction`，`_da_direction_hint` 优先读它）。
- 实测 da_feed 窗每日 2-4 个 LONG 候选此前是**死数据**（无消费方），接入后进 DA 池；
  方向仍由 DA LLM 独立判定（确认/加厚/翻转），不预设。

### C. 配置（`config/unified_config.yaml` universe_expansion 段）

```yaml
da_candidate_feed_long: true      # B 的独立回滚开关
long_momentum_gate_enabled: true  # A 的独立回滚开关
```

`da_candidate_feed_long` 纯仓外消费 → `config_key_census.py` EXTERNAL_CONSUMED 补登记
（登记前 `test_gate_no_new_dead_keys` 红，登记后 17/17 绿——豁免承重实证）。

## 3. 验证（Gate 5，全部实测）

| 验证项 | 结果 |
|---|---|
| 新单测 `tests/test_ue_long_momentum_gate.py`（11 语义臂 + 2 源级契约钉） | **13/13 passed** |
| UE 家族回归（11 文件：da_candidate_selection / ue_funnel_batch / ue_planned_* / ue_confirm_gate / ue_contract_validator / ue_evidence_* 等） | **452/452 passed** |
| census 守卫 `tests/test_config_key_census.py` | **17/17 passed**（baseline fixture 未动，防作弊核实） |
| 真 router 集成冒烟（UBER/RIVN/NOK/AAPL + get_daily_bars 键名） | **绿**，MA 值与反事实逐字吻合 |
| 测试契约更新 | `only_short_kept` → `both_directions_kept == [["US.DDD","LONG"],["US.EEE","SHORT"]]` |

## 4. 预注册观察判据（不改尺子，到期只报数）

上线后 10 个交易日（至 2026-10-13），只读观察，**不预设 enforce 变更**：

1. **拦截归因面**：`data/inject_reject.jsonl` 中 `long_breakdown(...)` 行数 ≥1
   （若恒 0：要么无空头排列 LONG 候选=正常，要么接线死——用当日 scan surfaced
   LONG 候选的 trend 复算交叉验证，判"无供给"vs"死门"）；
2. **误杀面**：被拦标的若 DA 同日独立给出 BUY 且成交盈利，记 1 次误杀对照
   （两通道信息集不同，DA 放行≠UE 拦错，但连续多笔同向则提请重裁阈值口径）；
3. **供给面**：UE LONG 成交笔数变化（基线 ≥9/5 约 0.7 笔/日）；da_feed LONG
   进 DA 池后的采纳率（instruction 转化率）。

## 5. 明确不做

- **不做影子期**（用户明裁"直接上"）；
- **不动 SHORT 腿**任何过滤/门；
- **不动 QCOM 型边界**（"短期偏多但价跌破 MA5"不拦——扩到该形态=改择时闸语义，
  须另立 DEB 且 DA 侧同改，防两把尺子）;
- **不改 UE prompt / 模型**（bakeoff 同日裁决维持 glm-5.3，见 §6）。

## 6. 同日关联裁决（UE 模型 bakeoff，任务2）

用户裁"只用 glm-5.3 和 deepseek-v4.1-flash 两模型对比"→ 已跑
（同 brief 3 轮交替，生产同构参数，产物 `data/shadow/ue_model_bakeoff_20260929.jsonl`）：

| 指标 | glm-5.3（现役） | deepseek-v4.1-flash |
|---|---|---|
| schema 通过 | 3/3 | 3/3 |
| 延迟 p50/max | 76s/82s | 95s/139s |
| trig 填充 | **18/21=86%** | 16/23=70% |
| 3 轮成本 | $0.095 | **$0.020** |

**裁决：primary 不动 glm-5.3**。理由：①换模型的原始动机（撞顶率 20%）已被
RFC-105 修复锚（9/18 `reasoning_effort: high`）消灭——锚后 19 次主调用 0 撞顶
0 失败 0 顶班；②质量面 glm 占优（n=3 在噪声内但方向不利）；③成本差 ~$2/月
量级不值得作废 glm-5.3 时代归因样本。与 RFC-105 §6"保持不动"先例一致。
**重提条件**：deepseek 顶班期（fallback 自动触发）PnL 归因样本 ≥15 笔且显著
优于同期 glm 主调用，或 glm 再现连续 ≥3 次 >300s 延迟恶化（RFC-105 同款条款）。

## 7. 变更文件清单

| 仓 | 文件 | 改动 |
|---|---|---|
| FTSeek-US | `scripts/llm_universe_scan.py` | +`_long_breakdown_trend`（~70行）+ inject 循环拦截（~13行） |
| FTSeek-US | `config/unified_config.yaml` | +2 开关键 + RFC-117 注释 |
| FTSeek-US | `scripts/config_key_census.py` | EXTERNAL_CONSUMED +1 条 |
| FTSeek-US | `tests/test_ue_long_momentum_gate.py` | 新文件（13 用例） |
| FTSeek-US | `tests/playbook/da_selection_driver.py` + `test_da_candidate_selection.py` | 契约断言更新 |
| /root skill | `.hermes/skills/deep-stock-analysis/scripts/pipeline.py` | feed 双向 + 文案方向感知 + hint 透传 |

反事实复算脚本：`/root/.hermes/cache/scratch/ue_long_cf_gate.py`（72h 后清理，
关键数字已固化本文 §1；正式复算走 §4 观察判据的归因行）。
