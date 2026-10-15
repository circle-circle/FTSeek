# RFC-117 UE LONG 接入 DA 质量链（双向 feed + 直写通道动量闸）

- **日期**：2026-09-29 | **状态**：**已落地**（用户当日裁决"3 不用影子模式，有助于提升交易质量的可以直接上"）
- **⚠️ 2026-10-15 状态勘误**：直写 inject 链路已于 2026-10-06 随 UE fresh-event 改造下 cron ⇒ 本 RFC 的「直写通道动量闸」（`long_momentum_gate_enabled`）自此 **dormant**（生产面无调用者；手动跑 inject 仍生效）。`da_candidate_feed_long` 走 DA bridge 路径不受影响。
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

| 笔 | 亏损 | 入场日历史K线复算 | DA 择时闸判定 |
|---|---|---|---|
| RIVN 9/21 | −$319.55 | **空头排列**（MA5 15.40 < MA10 15.70 < MA20 15.98，close 14.99） | 🔴 SKIP（破位硬买拦截） |
| UBER 9/11 | −$99.37 | **空头排列**（MA5 73.70 < MA10 75.16 < MA20 76.33，close 72.56） | 🔴 SKIP |
| QCOM 9/8 | −$227.50 | **多头排列**（MA5 168.32 > MA10 164.90 > MA20 163.22，close 168.74） | 放行（**拦不到，诚实边界**） |

盈利腿对照（NOK +132 / FPS +129 / ASML +46 / STX +44 / WDC +42，全 TRAIL_LOCK）：**5/5 放行，误杀 0**。

反事实结论：在 8 笔焦点样本内，择时闸拦 2/3 大亏（−$419），零误杀；
全历史 point-in-time 口径见 §1b。UE LONG 缺的不是信号质量（中位为正），
是 SHORT 腿已有的那道**免费过滤**。

### 1b. 全历史重放更正（2026-09-29 同日，生产函数级，`scripts/ue_long_gate_replay_20260929.py`）

上线前用**已提交的 `_long_breakdown_trend`**（非简化版）对全部 50 笔 UE LONG 成交
在各自真实入场日重放，取数改为 `request_history_kline(end=入场日前一交易日)` 的
point-in-time 口径，**三处更正**：

1. **拦截面更大**：拦 9 笔（§1 窗口只见 2 笔），含新发现 TSM 7/27 −$99.52、
   TSM 7/30 +$56.09、USAR 9/4 −$158.05。拦截组净 **−$611.35、笔均 −$67.93**；
   有数据放行组 41 笔净 **−$216.00、笔均 −$5.27**、WR 56%
   ——闸挑出的群体比平均差 **13 倍**，区分度强。
2. **“误杀 0”是 8 笔焦点样本口径，全历史口径为误杀 3 笔**：NVMI 9/2 +$10.04、
   ASTS 9/2 +$20.62、TSM 7/30 +$56.09（合计 +$86.75）。
   拦 −$611.35 vs 误杀 +$86.75 ≈ **7:1**，净效应仍强正，但如实更正表述。
3. **no_data=0**：point-in-time `request_history_kline` 消除了旧 60-bar 截断伪影；
   7 月样本不再因“当前最近 60 根”被误判为缺数。

> 边界注记（2026-09-29 复核补）：重放与生产同为 QFQ 前复权（生产 `get_daily_kline`
> 不显式传 autype=futu 默认 QFQ），口径一致 ✓；但 QFQ 以**查询日**为锚，重放含入场日
> 之后的分红/拆股复权因子，对窗口内有除息的标的（如 TSM/QCOM）MA 排序存在理论
> 二阶失真。拦:误杀 7:1 的边际下不足以翻案，仅如实标注。另：重放脚本直连
> OpenQuoteContext 逐笔取数，不经过生产 `_acquire_daily_kline_quota`（60次/30s
> 账户级闸）——手动一次性脚本，**勿与 EOD 批量同跑**。

**仍漏网（结构性边界，闸按设计不拦）**：QCOM −$227.50（多头排列）、
UCTT −$145.29（短期偏多）、TTD −$92.44（短期偏空）、FCX −$82.13（短期偏空）、
GNK −$81.00（短期偏多）、EOG −$74.57（短期偏多）。六笔合计 −$703，
均为“上行/震荡结构内的回调亏损”——扩门去拦它们会误伤放行组的 OKTA +61 / WDC +42 /
FPS +129 / NOK +132 同形态盈利单，故不扩（§5 明确不做）。

## 2. 改动（双保险，两条通道各补一处）

### A. cmd_inject 直写通道 → 破位硬买拦截（`scripts/llm_universe_scan.py`）

- 新增 `_long_breakdown_trend(router, symbol, asof_date)`：MA5/10/20 trend 分档
  改为调用仓内共享纯函数 `app/playbook/momentum.py:classify_ma_trend`
  （含 `round(...,2)` 口径钉和 20 根已完成日线最低要求），
  只用严格早于 asof 的已完成日线（盘前可见=昨收），同日去重取最后一条
  （与 `compute_screening_atr` 同口径）。
- `_cmd_inject_impl` 合格集合循环内、`_compute_numbers` 之前：
  `direction=="LONG"` 且 gate 开 → trend=="空头排列" → `continue`（不生成指令）
  + `_log_inject_loop_reject` 落归因行（`long_breakdown(...)` 含 MA 数值）。
- **只对 LONG**：DA signal_gate 对 SHORT 无此硬门（多头排列做空走降档），
  且 UE SHORT 是在产正 alpha 源，不加门。
- **缺数 fail-open**：bars<20 / router 异常 / None → `("no_data", None)` → 放行
  （与 screening UNKNOWN"缺数≠拒绝"同纪律，不因取数故障误杀）。
- **三态审计**：`data/shadow/ue_long_momentum_gate.jsonl` 逐候选记录
  `blocked / passed / no_data`，不再只看被拦行。

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

### D. 下游记录与呈现适配（record-only，不改交易行为）

- **记录**：DA v0.89 在 bridge candidate、`playbook_instructions.raw_instruction`
  与 `signal_snapshots.factors_json` 三处保留 `ue_origin / ue_direction /
  ue_confidence / ue_scanned_at`；后续可统计 UE 原方向与 DA 最终方向的
  确认/翻转，不再只留下 `origin=ue_candidate`。
- **数据统计**：`direction_hit_scorecard.py` 来源切分改为
  `DA自选 / DA-UE确认 / UE直写 / topup` 四分源，并附 UE LONG 动量闸三态汇总；
  SQL 口径收敛到 `app/review/source_attribution.py`。
- **日报/周报**：信号漏斗新增来源×方向分布，推送正文同步渲染，
  避免 `DA-UE确认` 被合并进 DA 总体后不可见。
- **UE 推送**：`llm_universe_scan.py push` 在执行闸行后追加
  `LONG动量闸: 拦X/过Y/缺Z`，按 `(date,symbol)` 去重并保留最后判定。
- 首次真库复核：`DA-UE确认` 已执行 n=11（BUY 4 / SHORT 7），T+1 命中 54.5%，
  方向收益 +0.824%；n<30 仅确认"统计轴已可见"，不作策略结论。三态闸日志当前
  rows=0，符合刚上线且尚未经历下一轮 inject 的状态。

## 3. 验证（Gate 5，全部实测）

| 验证项 | 结果 |
|---|---|
| 新单测 `tests/test_ue_long_momentum_gate.py`（11 语义臂 + 2 源级契约钉） | **13/13 passed** |
| UE 家族回归（11 文件：da_candidate_selection / ue_funnel_batch / ue_planned_* / ue_confirm_gate / ue_contract_validator / ue_evidence_* 等） | **452/452 passed** |
| census 守卫 `tests/test_config_key_census.py` | **17/17 passed**（baseline fixture 未动，防作弊核实） |
| 真 router 集成冒烟（UBER/RIVN/NOK/AAPL + get_daily_bars 键名） | **绿**，MA 值与反事实逐字吻合 |
| 测试契约更新 | `only_short_kept` → `both_directions_kept == [["US.DDD","LONG"],["US.EEE","SHORT"]]` |
| P1 复核（共享 trend 函数 + 双开关行为测试 + 三态日志） | **99/99 passed** |
| P2 复核（DA 生产 CWD 导入 + UE/DA momentum 同一真源 + 方向文案契约） | **67/67 passed** |
| 三态观察汇总器 | `tests/test_ue_long_gate_observation.py` **3/3 passed** |
| 下游来源/记录/推送适配 | `tests/test_review_source_attribution.py` + 相关契约 **全部通过** |

## 4. 预注册观察判据（不改尺子，到期只报数）

上线后 10 个交易日（至 2026-10-13），只读观察，**不预设 enforce 变更**：

1. **拦截归因面**：`data/shadow/ue_long_momentum_gate.jsonl` 中 `decision=blocked`
   行数 ≥1（若恒 0：要么无空头排列 LONG 候选=正常，要么接线死——用当日 scan surfaced
   LONG 候选的 trend 复算交叉验证，判"无供给"vs"死门"）；
2. **三态分母**：用 `scripts/ue_long_gate_observation.py --days 14 --json`
   同时报 `blocked / passed / no_data`；若 `valid_rows=0` 判"门未接线/未启用"，
   若 `no_data` 连续显著占比则先查行情链路，不用拦截数冒充分母；
3. **误杀面**：被拦标的若 DA 同日独立给出 BUY 且成交盈利，记 1 次误杀对照
   （两通道信息集不同，DA 放行≠UE 拦错，但连续多笔同向则提请重裁阈值口径）；
4. **供给面**：UE LONG 成交笔数变化（基线 ≥9/5 约 0.7 笔/日）；da_feed LONG
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
| FTSeek-US | `app/playbook/momentum.py` | 新增 MA trend 共享纯函数 |
| FTSeek-US | `config/unified_config.yaml` | +2 开关键 + RFC-117 注释 |
| FTSeek-US | `scripts/config_key_census.py` | EXTERNAL_CONSUMED +1 条 |
| FTSeek-US | `tests/test_ue_long_momentum_gate.py` | 新文件（13 用例） |
| FTSeek-US | `tests/playbook/da_selection_driver.py` + `test_da_candidate_selection.py` | 契约断言更新 |
| FTSeek-US | `scripts/ue_long_gate_replay_20260929.py` | point-in-time 重放脚本 |
| FTSeek-US | `tests/conftest.py` | 三态日志路径沙箱登记 |
| /root skill | `.hermes/skills/deep-stock-analysis/scripts/pipeline.py` | feed 双向 + 文案方向感知 + hint 透传 + `_UE_CONFIG_PATH` 常量化 |
| /root skill | `.hermes/skills/deep-stock-analysis/scripts/deep_analyzer.py` | trend 改用共享纯函数（import 带 fail-open 降级臂，见 §8） |
| FTSeek-US | `scripts/ue_long_gate_observation.py` | 三态日志只读汇总器（含 (date,symbol) 去重，见 §8） |
| FTSeek-US | `tests/test_ue_long_gate_observation.py` | 汇总器单测（含去重用例） |
| FTSeek-US | `app/review/source_attribution.py` | 四分源 SQL 单一真源 |
| FTSeek-US | `scripts/direction_hit_scorecard.py` | 四分源 + LONG 动量闸汇总 |
| FTSeek-US | `app/review/daily_review.py` / `weekly_review.py` / `scheduler_triggers.py` | 日报、周报、推送来源分布 |
| /root skill | `.hermes/skills/deep-stock-analysis/scripts/ic_writer.py` | snapshot factors 保留 UE 子链元数据 |

反事实复算脚本：`/root/.hermes/cache/scratch/ue_long_cf_gate.py`（72h 后清理，
关键数字已固化本文 §1；正式复算走 §4 观察判据的归因行）。

## 8. 复核修订（2026-09-29 多Agent复核批，用户裁「继续」）

复核确认整体合理后落地的三处修正（行为口径零变化）：

1. **P1 — DA 侧跨仓 import 改 fail-open**：`deep_analyzer.py` 顶部
   `sys.path.append + from app.playbook.momentum import` 原为裸 import，
   FTSeek 仓路径变动即整条 DA 盘前链 import 失败。改为 try/except + 同口径
   本地降级副本（与同文件 `anomaly_parse` 同款纪律）；仓内测试
   `test_da_uses_repo_classifier_from_skill_cwd` 补 `co_filename` 前缀断言
   （`__module__` 字符串相等不能证明文件同源），走降级臂即测试转红报警。
2. **P2 — 三态汇总器去重**：inject 候选循环在 `if dry: return 0` 之前，且
   inject 重跑幂等 ⇒ 闸日志 append 会对同 (date,symbol) 重复落行，§4.2 的
   分母指标（no_data 占比等）随重跑膨胀。`ue_long_gate_observation.summarize()`
   现按 (date,symbol) 去重保留最后一次判定，报告新增 `raw_rows`/`dup_rows`。
   （生产写入侧不改 — 保留原始逐跑记录可审计，去重只在只读汇总层。）
3. **版本棘轮**：DA skill v0.87 → **v0.88**（SKILL.md / references/CHANGELOG.md /
   `test_da_output_contract.py` 三处同步）。
