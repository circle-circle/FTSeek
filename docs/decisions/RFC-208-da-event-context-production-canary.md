---
title: RFC-208 — EventContext 直接注入 DA 生产 Prompt（单变量 canary）
status: canary_quality_passed_pending_forward_economics
approved_by: 用户「请确认如何直接接入，今晚验证效果」
related: RFC-207、RFC-206、DEB-20261106 §65–66
---

# RFC-208 — EventContext 直接注入 DA 生产 Prompt

## 1. 授权与唯一变量

用户明确要求直接接入并在 2026-11-13 晚验证。唯一信号源变量是：

> 在 DA 原始数据段之后，按标的注入经过时间/grounding 校验的 EventContext 证据段。

不变项：主模型/挑战者、思考预算、原始数据采集、候选排序、Signal Gate、entry/SL/TP、仓位、
Playbook、EXT 退出、四锁（SIM、ALLOW_REAL_TRADING=false）。

本项占用 11 月 C1 唯一新入场假设；DA cohort 自注入首卡重计，不与原 Prompt 历史样本混合。
其它模型/Prompt/退出实验不并入本 canary。

**不在本批**：生产 SKIP 契约、模型替换、R3 硬门、EXT 自动退出、任何 live 交易。

## 2. 接入链路与回滚

```text
06:30 ET（冬夏令时一致）：EventStore + 当日 watchlist → 新 EventContext 简报（最多2次LLM）
DA main carrier（EST 07:55–08:15 / EDT 06:55–07:15）
  → 原候选/原19维采集
  → 按 symbol compact EventContext（非当日/无效/过期/缺失则原行为并留原因）
  → 原双模型/Gate/Playbook（仅 SIM）
```

- 简报生成：`DA_EVENT_CONTEXT_SHADOW=1` + `--execute`；无当日 checkpoint 时仅用 watchlist+EventStore，
  supplemental=0，禁止把昨天 raw 当今天。
- 生产注入：独立 `DA_EVENT_CONTEXT_INJECT`，缺省 false。简报必须 `record_only=true`、状态 OK/ALREADY_CURRENT、
  validation_errors=[]、**as_of 的 ET 日期=当前 ET 日** 且年龄≤26h；失败不阻断 DA，明确记 `injection_skipped:<reason>`。
- 每卡保留 `brief_id/injection_hash/event_ids/_prompt_hash/_user_prompt`；checkpoint 指纹包含 injection_hash，
  禁止误用原 Prompt 缓存。**哈希不含 writer status**，OK↔ALREADY_CURRENT 不改变内容身份，避免误作废旧缓存/误报 REVIEW。
  新字段消费方=raw checkpoint + 质量/归因评估器，不接 Gate。
- **一行回滚**：`.env.us` 写 `DA_EVENT_CONTEXT_INJECT=0`，下一轮恢复原 Prompt，无需改模型或订单。

### 生产消费者指纹（2026-11-14 复核）

`/root/.hermes/skills/deep-stock-analysis/scripts/` 当前生产文件与下列 SHA256 一致；任何后续同步须同时更新此表与 handoff：

- `llm_synth.py`：`88e29ca6d6499f94b56c80a5f698533104803cc4e3c7c339d06cc05dc21b05b8`
- `pipeline.py`：`68981f39768f51158702875f977bda617882b32af185d39edb08df14e236ff0c`
- `config.py`：`f0f91e8d0057f4c827dc477231409d57ab0b4f91dbf2d93042390c64bca3012d`

## 3. 今晚对照

1. **Treatment**：真实 DA 注入批次的 checkpoint、bridge、Playbook 指令。
2. **Control**：同一批 raw checkpoint，以 `DA_EVENT_CONTEXT_INJECT=0` 做隔离基线回放；不写生产 DB。
3. 对比 direction/score/有效卡/研究完整性/价格几何/event_role/event_ids/延迟，区分相关注入与随机翻向。
   `event_role=NONE` 的方向变化不得归因 EventContext；单晚不作收益或模型优劣结论。

## 4. 今晚验收判据

### 4.1 运行安全（必须全过）

- 真实 DA 批次完成或有明确 finalize；无重复订单/无 real 交易/无绕过现有 Gate。
- brief_id/injection_hash 一致，注入卡可回查原始 Prompt；无过期/未校验简报注入。
- 现有四锁、持仓与退出台账不回归；启动/运行无新增 Traceback/CRITICAL。

### 4.2 指令质量（与 control 同 raw 对照）

- valid_card_rate 不低于 control −5pp；research_complete_rate 不低于 control −5pp。
- 价格几何不得新增无效卡；事件 ID 必须属于该股实际注入 facts，禁止 UNLINKED 冒充 grounded。
- 注入覆盖率、event_role、方向与价位变化全部逐股披露；只报现象，不挑“好看”的标的。

### 4.3 不作今晚收益结论

今晚没有完整出场与费用后样本，不能宣称净收益、节损或 alpha。后续至少积累 ≥30 position-event pairs、
≥10 ET 日，按可成交价、费用、日聚类 CI/MDE 评估；任何转常态生产仍须另裁。

## 5. 停止条件

出现以下任一项立即 `DA_EVENT_CONTEXT_INJECT=0`：

- 注入失效/过期简报、event_id 无法回指、checkpoint 缓存混用；
- 有效卡或研究完整率下降超过5pp、出现重复订单/绕门/生产错误；
- 指令供给异常集中，或注入导致系统性不可达价位且无法用既有执行层解释。

保留原始与注入产物供复查，不删除证据。

## 6. 部署前 smoke（2026-11-13）

- 20:10 首轮简报因 `NEUTRAL` 不在 sign 枚举被拒；20:16 次轮因 `counter_evidence` 为空被拒。
  未把失败简报注入 DA，未用模糊匹配或编字段放行。
- Prompt 最终升 `market_event_brief_v4`：事件 ID 逐字复制、NEUTRAL 归 UNKNOWN、反证传导字段必填。
  最新有效简报 `f23c760ecea071d25112`：1122 events、48 facts、3 transmissions、validation_errors=[]。
- NVDA 生产同构 smoke 注入成功：Prompt 含 brief_id 和生产文案，主/挑战者正常返回，契约零违规，
  `advisory_only=true / gate_visible=false`。
- 联合回归 **281 passed**；棘轮+offline full-chain **59 passed**；`check_undefined_names` 与 diff 检查通过。
- registry 重启 MainPID=2851003，启动冒烟 **7/7**，BA/HON/ASML 三仓不变、非 BOOKED=0。
- 21:00 主批及同 raw control 尚待验收，不提前宣称成功或收益。

## 7. 首夜真实验收（2026-11-13）

- 最新有效简报 `f23c760ecea071d25112` 在真实批次完成 32 个逐股注入，bridge 最终写入 **19** 条指令，
  无重复 symbol、无 real 交易、无新增 Traceback/CRITICAL；BA/HON/ASML 三仓不变、非 BOOKED=0。
- Treatment 与 control 同为 qwen3.7-max、同 raw checkpoint：valid_card=32/32、research_complete=32/32、
  price_geometry=100%、ungrounded_event=0；control 未出现质量下降，`canary_pass=true`。
- 方向一致 26/32。6 个翻向中仅 NVDA（PRIMARY）与 CSCO（CONTRARY）实际引用共享事件，
  其余 ANET/MSFT/RIVN/TSLA 的 `event_role=NONE`，不得归因 EventContext。
- NVDA 从 control SKIP→BUY，理由是政治恐慌后的估值修复；**原 Gate 仍以 bearish 动量+BUY 拦截**，
  未进入 bridge，未形成真实订单。CSCO 从 BUY→SHORT，保留于 bridge。
- 评估产物：`data/shadow/da_event_prompt_canary/latest.json`；
  control=`data/shadow/da_control_20261113_all32_v1.jsonl`；`--eval` 返回 OK。
- **证据质量复核未通过**：新增只读校验后 `status=WARN`、`evidence_quality_pass=false`，
  27/32 标的存在至少一项 unsupported indicator / valuation contradiction / small peer sample。
  NVDA 的“KDJ低位金叉”无生产实现，PE(TTM)=21.01 与 dynamic=33.916/normalized=34.87 且仅4个可用 peer，
  说明“结构有效”不等于“论据可靠”。`canary_pass` 仍仅指原结构/控制判据，不能单独用于晋级。
- **翻向不可归因**：NVDA 三次 treatment=2 SKIP/1 SHORT/0 BUY，CSCO control 与 treatment 均为2 SHORT/1 BUY；
  重复样本含 ungrounded ID，故 `valid_cards=0`。详见 `docs/debates/DEB-20261113-event-context-repeatability.md`。
- **首夜只判质量/安全，不判收益。** 没有完整出场与费后样本，继续按 §4.3 积累，不转常态生产。

## 8. 执行侧补充（entry_confirm / KDJ）

`docs/debates/DEB-20261113-entry-confirm-indicators-review.md` 已独立复核：
现役 entry_confirm 结论仍是 INSUFFICIENT，不删除、不加码；KDJ 趋势为负增量且误杀好单，明确不接。
后续只评估计划锚侧、日内 VWAP 侧和时间确认，先做离线预注册，不改变今晚执行行为。
